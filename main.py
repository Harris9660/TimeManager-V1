import argparse
import datetime
import logging
import math
import os
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error
from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException

LONDON = ZoneInfo("Europe/London")

# =====================
# Configuration
# =====================
SCOPES = ["https://www.googleapis.com/auth/calendar"]
PROJECT_DIR = Path(__file__).resolve().parent
API_ERRORS = (HttpError, GoogleAuthError, HttpLib2Error, RequestException, OSError, OAuth2Error)

WORK_START = datetime.time(9, 0)
WORK_END = datetime.time(18, 0)
GRID_MIN = 15
BUFFER_MIN = 5
NOW_BUFFER_MIN = 30
HORIZON_DAYS = 14
DEADLINE_SAFETY_HOURS = 12
LIST_LOOKAHEAD_DAYS = 30  # for delete/reorder listing

# Scoring & Preemption
PRIORITY_WEIGHT = {1: 1, 2: 2, 3: 3, 4: 5, 5: 8}
DEADLINE_SCALE_HOURS = 24.0
MIN_BLOCK_HOURS = 0.25
SWITCH_THRESHOLD_DEFAULT = 0.10  # 10% better score triggers preemption suggestion

LOG_LEVEL = os.environ.get("TM_LOG", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("timer-manager")

# =====================
# Utilities
# =====================
Interval = Tuple[datetime.datetime, datetime.datetime]


def now_london() -> datetime.datetime:
    if LONDON:
        return datetime.datetime.now(tz=LONDON)
    return datetime.datetime.now(datetime.timezone.utc)


def to_utc_z(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def fmt_range_human(s: datetime.datetime, e: datetime.datetime) -> str:
    if LONDON:
        s = s.astimezone(LONDON)
        e = e.astimezone(LONDON)
    day = s.strftime("%a %d %b")
    return f"{day} · {s.strftime('%H:%M')}–{e.strftime('%H:%M')} {s.strftime('%Z')}"


def parse_easy_date(s: str) -> datetime.date:
    s = s.strip().lower()
    today = now_london().date()
    if s in ("today", "tod"):
        return today
    if s in ("tomorrow", "tmr", "tom"):
        return today + datetime.timedelta(days=1)
    return datetime.date.fromisoformat(s)


def end_of_workday(d: datetime.date) -> datetime.datetime:
    e = datetime.datetime.combine(d, WORK_END)
    return e.replace(tzinfo=LONDON) if LONDON else e.replace(tzinfo=datetime.timezone.utc)


# =====================
# Google Calendar Auth
# =====================

def authenticate(*, interactive: bool = True) -> Optional[object]:
    creds: Optional[Credentials] = None
    token_path = PROJECT_DIR / "token.json"
    try:
        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except (ValueError, KeyError):
                log.warning("Invalid token.json; Google authorization is needed again.")
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except API_ERRORS:
                    log.warning("Could not refresh Google authorization.")
                    creds = None
            if not creds or not creds.valid:
                if not interactive:
                    log.error("Run main.py interactively to authorize Google Calendar before using --auto.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(PROJECT_DIR / "credentials.json"), SCOPES
                )
                creds = flow.run_local_server(port=0)
            # Replace atomically so an interrupted write cannot corrupt the token.
            temporary = token_path.with_suffix(".json.tmp")
            try:
                with temporary.open("w") as token:
                    os.chmod(temporary, 0o600)
                    token.write(creds.to_json())
                temporary.replace(token_path)
            finally:
                temporary.unlink(missing_ok=True)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except (ValueError, KeyError, *API_ERRORS) as error:
        # OAuth exceptions can contain sensitive response data; only print the type.
        log.error("Google Calendar setup failed (%s). Check credentials.json and your connection.",
                  type(error).__name__)
        return None


# =====================
# CRUD
# =====================

def list_events(service: object, max_results: int = 10, time_min_dt: Optional[datetime.datetime] = None) -> bool:
    if max_results <= 0:
        raise ValueError("Max results must be positive.")
    try:
        events = _fetch_events(service, time_min_dt or now_london(), limit=max_results)
        if not events:
            print("No upcoming events found.")
            return True
        print("Upcoming events:\n")
        for ev in events:
            title = ev.get("summary", "No title")
            flags = _summarize_flags(ev)
            if "dateTime" in ev.get("start", {}):
                s, e = _parse_event_interval(ev)
                print(f"- {fmt_range_human(s, e)}: {title} {flags}")
            else:
                sd = datetime.date.fromisoformat(ev["start"]["date"])
                print(f"- {sd.strftime('%a %d %b')} · All day: {title} {flags}")
        return True
    except API_ERRORS as error:
        log.error(f"Error while listing events: {error}")
        return False


def add_event(
    service: object,
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
    summary: str,
    description: str = "",
    recurrence_rule: Optional[str] = None,
    color_id: Optional[str] = None,
    extended_props: Optional[Dict[str, str]] = None,
) -> Dict:
    if end_dt <= start_dt:
        raise ValueError("End time must be after start time.")
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/London"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/London"},
    }
    if recurrence_rule:
        body["recurrence"] = [recurrence_rule]
    if color_id:
        body["colorId"] = color_id
    if extended_props:
        body["extendedProperties"] = {"private": extended_props}
    try:
        created = service.events().insert(calendarId="primary", body=body).execute()
        print(f"Event created: {created.get('htmlLink')}")
        return created
    except API_ERRORS as error:
        log.error(f"Add event error: {error}")
        return {}


def delete_event(service: object, event_id: str) -> bool:
    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True
    except API_ERRORS as error:
        log.error(f"Delete event error: {error}")
        return False


def move_event(service: object, event_id: str, start_dt: datetime.datetime, end_dt: datetime.datetime) -> bool:
    if end_dt <= start_dt:
        raise ValueError("End time must be after start time.")
    body = {
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/London"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/London"},
    }
    try:
        service.events().patch(calendarId="primary", eventId=event_id, body=body).execute()
        return True
    except API_ERRORS as error:
        log.error(f"Move event error: {error}")
        return False


# =====================
# Scheduling helpers
# =====================

def _bounds_for_date(d: datetime.date) -> Interval:
    s = datetime.datetime.combine(d, WORK_START)
    e = datetime.datetime.combine(d, WORK_END)
    if LONDON:
        s = s.replace(tzinfo=LONDON)
        e = e.replace(tzinfo=LONDON)
    else:
        s = s.replace(tzinfo=datetime.timezone.utc)
        e = e.replace(tzinfo=datetime.timezone.utc)
    return (s, e)


def _parse_event_interval(ev: Dict) -> Interval:
    if "dateTime" in ev.get("start", {}):
        def parse_endpoint(endpoint: Dict) -> datetime.datetime:
            value = datetime.datetime.fromisoformat(endpoint["dateTime"].replace("Z", "+00:00"))
            if value.tzinfo is None:
                zone = ZoneInfo(endpoint.get("timeZone", "Europe/London"))
                value = value.replace(tzinfo=zone)
            return value
        s = parse_endpoint(ev["start"])
        e = parse_endpoint(ev["end"])
    else:
        s_date = datetime.date.fromisoformat(ev["start"]["date"])
        e_date = datetime.date.fromisoformat(ev["end"]["date"])
        s = datetime.datetime.combine(s_date, datetime.time(0, 0))
        e = datetime.datetime.combine(e_date, datetime.time(0, 0))
        if LONDON:
            s = s.replace(tzinfo=LONDON)
            e = e.replace(tzinfo=LONDON)
        else:
            s = s.replace(tzinfo=datetime.timezone.utc)
            e = e.replace(tzinfo=datetime.timezone.utc)
    return (s, e)


def _merge(intervals: List[Interval]) -> List[Interval]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1][1] = max(le, e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _subtract(working: List[Interval], busy: List[Interval]) -> List[Interval]:
    free: List[Interval] = []
    busy = _merge(busy)
    for ws, we in working:
        cur = [(ws, we)]
        for bs, be in busy:
            nxt: List[Interval] = []
            for cs, ce in cur:
                if be <= cs or bs >= ce:
                    nxt.append((cs, ce))
                else:
                    if bs > cs:
                        nxt.append((cs, bs))
                    if be < ce:
                        nxt.append((be, ce))
            cur = nxt
        free.extend([(s, e) for s, e in cur if (e - s).total_seconds() > 0])
    return free


def _snap_to_grid(t: datetime.datetime, grid_min: int) -> datetime.datetime:
    """Snap UP to the next grid boundary (never in the past)."""
    if grid_min <= 0 or 60 % grid_min:
        raise ValueError("Grid must be a positive divisor of 60 minutes.")
    if t.second or t.microsecond:
        t += datetime.timedelta(minutes=1)
    t = t.replace(second=0, microsecond=0)
    mod = t.minute % grid_min
    if mod == 0:
        return t
    return t + datetime.timedelta(minutes=(grid_min - mod))


def _fetch_events(service: object, start: datetime.datetime,
                  end: Optional[datetime.datetime] = None, *, limit: Optional[int] = None) -> List[Dict]:
    if end is not None and end <= start:
        raise ValueError("Calendar lookup end must be after start.")
    if limit is not None and limit <= 0:
        raise ValueError("Event limit must be positive.")
    params = dict(calendarId="primary", timeMin=to_utc_z(start),
                  singleEvents=True, orderBy="startTime", showDeleted=False,
                  timeZone="Europe/London", maxResults=min(limit or 2500, 2500))
    if end is not None:
        params["timeMax"] = to_utc_z(end)
    events = []
    while True:
        response = service.events().list(**params).execute()
        events.extend(ev for ev in response.get("items", []) if ev.get("status") != "cancelled")
        if limit is not None and len(events) >= limit:
            return events[:limit]
        page_token = response.get("nextPageToken")
        if not page_token:
            return events
        params["pageToken"] = page_token


def _is_managed(ev: Dict) -> bool:
    props = ev.get("extendedProperties", {}).get("private", {})
    return props.get("managed") == "1"


def _is_auto(ev: Dict) -> bool:
    props = ev.get("extendedProperties", {}).get("private", {})
    return props.get("auto") == "1"


def _is_commitment(ev: Dict) -> bool:
    props = ev.get("extendedProperties", {}).get("private", {})
    return props.get("commitment") == "1"


def _get_prop(ev: Dict, key: str, default: Optional[str] = None) -> Optional[str]:
    return ev.get("extendedProperties", {}).get("private", {}).get(key, default)


def _is_auto_task(ev: Dict) -> bool:
    return (_is_managed(ev) and _is_auto(ev) and not _is_commitment(ev)
            and ev.get("status") != "cancelled" and "dateTime" in ev.get("start", {}))


def _future_auto_tasks(events: List[Dict], now: datetime.datetime) -> List[Dict]:
    return [ev for ev in events if _is_auto_task(ev) and _parse_event_interval(ev)[0] >= now]


def _task_details(ev: Dict) -> Tuple[int, int, Optional[datetime.datetime]]:
    s, e = _parse_event_interval(ev)
    duration = int(math.ceil((e - s).total_seconds() / 60))
    priority = int(_get_prop(ev, "priority", "3"))
    if duration <= 0 or not 1 <= priority <= 5:
        raise ValueError(f"Invalid duration or priority for event {ev.get('id')}.")
    deadline = _get_prop(ev, "deadline")
    return duration, priority, end_of_workday(datetime.date.fromisoformat(deadline)) if deadline else None


def _fetch_busy(service: object, start: datetime.datetime, end: datetime.datetime,
                *, exclude_ids=()) -> List[Interval]:
    buffer = datetime.timedelta(minutes=BUFFER_MIN)
    # Include neighbours whose buffers reach into this window.
    events = _fetch_events(service, start - buffer, end + buffer)
    busy: List[Interval] = []
    for ev in events:
        if ev.get("id") in exclude_ids:
            continue
        if ev.get("transparency") == "transparent" and not _is_commitment(ev):
            continue
        if any(a.get("self") and a.get("responseStatus") == "declined"
               for a in ev.get("attendees", [])) and not _is_commitment(ev):
            continue
        s, e = _parse_event_interval(ev)
        s = max(start, s - buffer)
        e = min(end, e + buffer)
        if s < e:
            busy.append((s, e))
    return _merge(busy)


def _conflicts_in_range(busy: List[Interval], s: datetime.datetime, e: datetime.datetime) -> bool:
    for bs, be in busy:
        if not (e <= bs or s >= be):
            return True
    return False


def _has_conflict(service: object, s: datetime.datetime, e: datetime.datetime) -> bool:
    busy = _fetch_busy(service, s, e)
    return _conflicts_in_range(busy, s, e)


def priority_to_color(pr: int) -> str:
    return {5: "11", 4: "6", 3: "9", 2: "8", 1: "7"}.get(pr, "9")


# =====================
# Scoring functions (Smith's Rule-inspired)
# =====================

def priority_weight(pr: int) -> float:
    return PRIORITY_WEIGHT.get(pr, 3)


def deadline_factor(deadline_dt: Optional[datetime.datetime], now: datetime.datetime) -> float:
    if not deadline_dt:
        return 1.0
    hours = max((deadline_dt - now).total_seconds() / 3600.0, 0.0)
    # ∈ (1, 2]
    return 1.0 + (DEADLINE_SCALE_HOURS / (hours + DEADLINE_SCALE_HOURS))


def task_score(priority: int, duration_min: int, deadline_dt: Optional[datetime.datetime], now: datetime.datetime) -> float:
    dur_h = max(duration_min / 60.0, MIN_BLOCK_HOURS)
    return (priority_weight(priority) * deadline_factor(deadline_dt, now)) / dur_h


# =====================
# Recurrence helpers
# =====================

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def build_rrule(preset: str, *, byday: Optional[List[str]] = None,
                until_date: Optional[datetime.date] = None,
                count: Optional[int] = None) -> Optional[str]:
    """Return an RFC 5545 RRULE string or None.
    preset: 'none' | 'daily' | 'weekly' | 'custom'
    """
    preset = (preset or "none").strip().lower()
    if preset in ("none", "no", "n"):
        return None
    if count is not None and count <= 0:
        raise ValueError("Recurrence count must be positive.")
    if count is not None and until_date is not None:
        raise ValueError("Use a recurrence count OR an until date, not both.")
    if preset == "daily":
        parts = ["FREQ=DAILY"]
    elif preset == "weekly":
        parts = ["FREQ=WEEKLY"]
        if byday:
            if any(d not in WEEKDAYS for d in byday):
                raise ValueError("Recurrence days must be MO,TU,WE,TH,FR,SA,SU.")
            parts.append("BYDAY=" + ",".join(dict.fromkeys(byday)))
    elif preset == "custom":
        # Caller should pass a full custom rule through 'byday' as [<full_rule_str>]
        if byday and len(byday) == 1 and byday[0].startswith("RRULE:"):
            return byday[0]
        raise ValueError(
            "Custom RRULE must be provided as a full 'RRULE:...' string in byday[0]")
    else:
        raise ValueError("Unknown recurrence preset")

    if count is not None:
        parts.append(f"COUNT={int(count)}")
    if until_date is not None:
        # Include every occurrence on the selected London date, even after work hours.
        until_dt = datetime.datetime.combine(until_date, datetime.time(23, 59, 59),
                                             tzinfo=LONDON or datetime.timezone.utc)
        until_dt = until_dt.astimezone(datetime.timezone.utc)
        parts.append("UNTIL=" + until_dt.strftime("%Y%m%dT%H%M%SZ"))

    return "RRULE:" + ";".join(parts)


# =====================
# Interactive inputs
# =====================

def _summarize_flags(ev: Dict) -> str:
    flags = []
    if _is_managed(ev):
        flags.append("[M]")
    if _is_auto(ev):
        flags.append("[A]")
    if _is_commitment(ev):
        flags.append("[C]")
    pr = _get_prop(ev, "priority")
    if pr:
        flags.append(f"[P{pr}]")
    ddl = _get_prop(ev, "deadline")
    if ddl:
        flags.append(f"[DDL {ddl}]")
    return " ".join(flags)


def get_priority_deadline_commitment() -> Tuple[int, Optional[datetime.datetime], str, Dict[str, str]]:
    # Priority
    while True:
        p = input("Priority [1(low)-5(high), default 3]: ").strip()
        if p == "":
            pr = 3
            break
        if p.isdigit() and 1 <= int(p) <= 5:
            pr = int(p)
            break
        print("Enter a number 1..5.")
    prefix = f"[P{pr}] "
    props = {"priority": str(pr), "managed": "1"}  # mark as managed

    # Deadline
    ddl_str = input(
        "Deadline (YYYY-MM-DD / today / tomorrow) [optional]: ").strip()
    if ddl_str:
        try:
            ddl_date = parse_easy_date(ddl_str)
            ddl_dt = end_of_workday(ddl_date)
            props["deadline"] = ddl_date.isoformat()
        except ValueError:
            raise ValueError("Invalid deadline. Use YYYY-MM-DD / today / tomorrow.")
    else:
        ddl_dt = None

    # Commitment
    is_commit = (
        input("Mark as commitment (hard block)? [y/N]: ").strip().lower() or "n") == "y"
    if is_commit:
        props["commitment"] = "1"

    return pr, ddl_dt, prefix, props


def get_recurrence_rule_interactive() -> Optional[str]:
    choice = (
        input("Recurrence? [none/daily/weekly/custom]: ").strip().lower() or "none")
    if choice in ("none", "no", "n"):
        return None
    if choice == "daily":
        count = input(
            "Count (number of occurrences) [blank = no limit]: ").strip()
        until = input("Until date (YYYY-MM-DD) [blank = none]: ").strip()
        count_val = int(count) if count else None
        until_date = parse_easy_date(until) if until else None
        return build_rrule("daily", count=count_val, until_date=until_date)
    if choice == "weekly":
        by = input(
            "Days (e.g., MO,WE,FR) [blank = every week]: ").strip().upper()
        days = [d.strip() for d in by.split(",") if d.strip()] if by else None
        count = input(
            "Count (number of occurrences) [blank = no limit]: ").strip()
        until = input("Until date (YYYY-MM-DD) [blank = none]: ").strip()
        count_val = int(count) if count else None
        until_date = parse_easy_date(until) if until else None
        return build_rrule("weekly", byday=days, count=count_val, until_date=until_date)
    if choice == "custom":
        rule = input(
            "Enter full RRULE string (e.g., RRULE:FREQ=MONTHLY;BYDAY=MO): ").strip()
        rule = rule.upper()
        if not rule.startswith("RRULE:"):
            rule = "RRULE:" + rule
        return rule
    raise ValueError("Unknown recurrence choice. Use none/daily/weekly/custom.")


# =====================
# REORDER logic (ratio-based)
# =====================

def _within_workday(s: datetime.datetime, e: datetime.datetime) -> bool:
    local = s.astimezone(LONDON or datetime.timezone.utc)
    ws, we = _bounds_for_date(local.date())
    return ws <= s < e <= we


def _apply_moves(service: object, moves: List[Tuple[Dict, datetime.datetime, datetime.datetime]]) -> bool:
    """Apply a plan, attempting to restore original intervals if any move fails."""
    attempted = []
    for event, start, end in moves:
        attempted.append(event)
        if move_event(service, event["id"], start, end):
            continue
        restored = True
        for original in reversed(attempted):
            try:
                service.events().patch(
                    calendarId="primary", eventId=original["id"],
                    body={"start": original["start"], "end": original["end"]},
                ).execute()
            except API_ERRORS:
                restored = False
                log.error("Could not restore event %s; check its time in Google Calendar.", original["id"])
        print("❌ Update failed; original times restored." if restored
              else "⚠️ Update and rollback failed; check the affected events in Google Calendar.")
        return False
    return True


def _first_free_slot(
    service: object,
    start_date: datetime.date,
    duration_min: int,
    priority: int = 3,
    deadline_dt: Optional[datetime.datetime] = None,
    horizon_days: int = HORIZON_DAYS,
    grid_min: int = GRID_MIN,
) -> Optional[Interval]:
    if duration_min <= 0:
        raise ValueError("Duration must be positive.")
    now = now_london()
    earliest_start_today = now + datetime.timedelta(minutes=NOW_BUFFER_MIN)
    effective_start_date = max(start_date, now.date())

    deadline_cap = None
    if deadline_dt:
        deadline_cap = deadline_dt - \
            datetime.timedelta(hours=DEADLINE_SAFETY_HOURS)
        if deadline_cap <= earliest_start_today:
            return None

    for day_offset in range(horizon_days):
        day = effective_start_date + datetime.timedelta(days=day_offset)
        ws, we = _bounds_for_date(day)
        if day == now.date():
            ws = max(ws, earliest_start_today)
        if deadline_cap and ws > deadline_cap:
            break
        day_end_cap = min(we, deadline_cap) if deadline_cap else we
        if ws >= day_end_cap:
            continue

        busy = _fetch_busy(service, ws, day_end_cap)
        free = _subtract([(ws, day_end_cap)], busy)
        for fs, fe in free:
            start = _snap_to_grid(max(fs, ws), grid_min)
            while start + datetime.timedelta(minutes=duration_min) <= fe:
                end = start + datetime.timedelta(minutes=duration_min)
                if deadline_cap and end > deadline_cap:
                    break
                if not _conflicts_in_range(busy, start, end):
                    return (start, end)
                start += datetime.timedelta(minutes=grid_min)
    return None


def _plan_first_free_given_busy(
    busy: List[Interval],
    start_date: datetime.date,
    duration_min: int,
    priority: int,
    deadline_dt: Optional[datetime.datetime],
    grid_min: int = GRID_MIN,
    *,
    fixed_date: Optional[datetime.date] = None,
) -> Optional[Interval]:
    if duration_min <= 0:
        raise ValueError("Duration must be positive.")
    now = now_london()
    earliest_start_today = now + datetime.timedelta(minutes=NOW_BUFFER_MIN)
    if fixed_date is not None and fixed_date < now.date():
        return None
    effective_start_date = fixed_date if fixed_date is not None else max(start_date, now.date())

    deadline_cap = None
    if deadline_dt:
        deadline_cap = deadline_dt - \
            datetime.timedelta(hours=DEADLINE_SAFETY_HOURS)
        if deadline_cap <= earliest_start_today:
            return None

    for day_offset in range(1 if fixed_date is not None else HORIZON_DAYS):
        day = effective_start_date + datetime.timedelta(days=day_offset)
        ws, we = _bounds_for_date(day)
        if day == now.date():
            ws = max(ws, earliest_start_today)
        if deadline_cap and ws > deadline_cap:
            break
        day_end_cap = min(we, deadline_cap) if deadline_cap else we
        if ws >= day_end_cap:
            continue

        free = _subtract([(ws, day_end_cap)], busy)
        for fs, fe in free:
            start = _snap_to_grid(max(fs, ws), grid_min)
            while start + datetime.timedelta(minutes=duration_min) <= fe:
                end = start + datetime.timedelta(minutes=duration_min)
                if deadline_cap and end > deadline_cap:
                    break
                if not _conflicts_in_range(busy, start, end):
                    return (start, end)
                start += datetime.timedelta(minutes=grid_min)
    return None


def reorder_managed_events(service: object, *, auto_apply: bool = False, preview_only: bool = False) -> bool:
    """Auto-reorder auto-managed events by descending score.
    Score = (priority_weight * deadline_factor) / duration_hours
    Recurring occurrences can change time only, keeping their current London date.
    Returns True if applied successfully or if preview_only.
    """
    now = now_london()
    start = now + datetime.timedelta(minutes=NOW_BUFFER_MIN)
    end = now + datetime.timedelta(days=LIST_LOOKAHEAD_DAYS)

    # Pull candidate events (auto-managed)
    all_events = _fetch_events(service, now, end)
    auto_events = _future_auto_tasks(all_events, now)

    if not auto_events:
        print("No auto-managed events to reorder.")
        return True

    # Build task list with scores
    tasks = []
    for ev in auto_events:
        dur, pr, ddl_dt = _task_details(ev)
        score = task_score(pr, dur, ddl_dt, now)
        # Expanded occurrences have recurringEventId, not their parent's recurrence rule.
        fixed_date = (_parse_event_interval(ev)[0].astimezone(LONDON).date()
                      if ev.get("recurringEventId") or ev.get("recurrence") else None)
        tasks.append({
            "event_id": ev["id"],
            "title": ev.get("summary", ""),
            "desc": ev.get("description", ""),
            "duration": dur,
            "priority": pr,
            "deadline": ddl_dt,
            "score": score,
            "fixed_date": fixed_date,
        })

    # Sort by score descending
    tasks.sort(key=lambda t: t["score"], reverse=True)

    # Only the events in this plan can be treated as available time.
    # A recurring occurrence near the lookup boundary may move later that day.
    # Read every blocker through that day's working hours as well.
    busy_end = max(end, _bounds_for_date(end.astimezone(LONDON).date())[1])
    busy = _fetch_busy(service, now, busy_end, exclude_ids={ev["id"] for ev in auto_events})
    plan: List[Tuple[str, datetime.datetime, datetime.datetime, float]] = []

    for t in tasks:
        slot = _plan_first_free_given_busy(
            busy=busy,
            start_date=start.date(),
            duration_min=t["duration"],
            priority=t["priority"],
            deadline_dt=t["deadline"],
            fixed_date=t["fixed_date"],
        )
        if not slot:
            constraint = (f"on its existing date {t['fixed_date']} within working hours and deadline"
                          if t["fixed_date"] is not None else "within the horizon and deadline")
            print(
                f"❌ Cannot place: {t['title']} (P{t['priority']}, score={t['score']:.3f}) {constraint}.")
            return False  # stop; keep calendar unchanged
        s, e = slot
        # reserve on busy timeline (with buffers)
        busy.append((s - datetime.timedelta(minutes=BUFFER_MIN),
                     e + datetime.timedelta(minutes=BUFFER_MIN)))
        busy = _merge(busy)
        plan.append((t["event_id"], s, e, t["score"]))

    # Show plan
    print("\nReorder plan (by score desc):")
    for ev_id, s, e, sc in plan:
        print(f"- {fmt_range_human(s, e)} · score={sc:.3f} · id={ev_id}")

    if preview_only:
        return True

    if not auto_apply:
        ok = input("Apply? [Y/n]: ").strip().lower() or "y"
        if ok != "y":
            print("Aborted.")
            return False

    # Apply (move events)
    originals = {ev["id"]: ev for ev in auto_events}
    success = _apply_moves(service, [(originals[ev_id], s, e) for ev_id, s, e, _ in plan])

    if success:
        print("✅ Reordered.")
    list_events(service, max_results=10, time_min_dt=now_london() -
                datetime.timedelta(minutes=1))
    return success


# =====================
# Real-time preemption: switch now if a higher score exists
# =====================

def _find_current_auto_event(service: object, now: datetime.datetime) -> Optional[Dict]:
    start = now - datetime.timedelta(hours=4)
    end = now + datetime.timedelta(hours=4)
    events = _fetch_events(service, start, end)
    for ev in events:
        if not _is_auto_task(ev):
            continue
        s, e = _parse_event_interval(ev)
        if s <= now < e:
            return ev
    return None


def preempt_now(service: object, *, threshold: float = SWITCH_THRESHOLD_DEFAULT, auto_apply: bool = False) -> bool:
    """If a different auto-managed task has a higher score than the current one, switch now.
    Strategy: compare score(current_remaining) vs best_other. If best_other is higher by 'threshold',
    move current to a later feasible slot and start the better task now (if no commitment conflict).
    """
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("Preemption threshold must be finite and nonnegative.")
    now = now_london()
    current = _find_current_auto_event(service, now)
    if not current:
        print("No current auto-managed task to preempt.")
        return True

    _, ce = _parse_event_interval(current)
    start_now = _snap_to_grid(now, GRID_MIN)
    remaining_min = max(int(math.ceil((ce - start_now).total_seconds() / 60)), 0)
    if remaining_min < GRID_MIN:
        print("Current block ends very soon; skipping preemption.")
        return True

    _, pr_cur, ddl_cur = _task_details(current)
    score_cur = task_score(pr_cur, remaining_min, ddl_cur, now)

    # Consider other auto-managed tasks starting later today/soon
    lookahead_end = now + datetime.timedelta(days=7)
    candidates = _future_auto_tasks(_fetch_events(service, now, lookahead_end), now)
    candidates = [ev for ev in candidates if ev["id"] != current["id"]]
    if not candidates:
        print("No other auto-managed tasks available for preemption.")
        return True

    best = None
    best_score = -1.0
    best_dur = None
    for ev in candidates:
        dur, pr, ddl_dt = _task_details(ev)
        sc = task_score(pr, dur, ddl_dt, now)
        if sc > best_score:
            best = ev
            best_score = sc
            best_dur = dur

    if best is None:
        print("No suitable candidate found.")
        return True

    improvement = (best_score - score_cur) / max(score_cur, 1e-9)
    if best_score <= score_cur or improvement < threshold:
        print(
            f"Keep current task. Best alternative is only {improvement*100:.1f}% higher in score.")
        return True

    # Check that starting 'best' now (for its full duration) doesn't hit commitments
    end_now = start_now + datetime.timedelta(minutes=best_dur)
    if not _within_workday(start_now, end_now):
        print("Cannot preempt: the candidate must fit within working hours.")
        return False
    _, _, best_deadline = _task_details(best)
    if best_deadline and end_now > best_deadline - datetime.timedelta(hours=DEADLINE_SAFETY_HOURS):
        print("Cannot preempt: the candidate would miss its deadline safety window.")
        return False

    excluded = {current["id"], best["id"]}
    busy_now = _fetch_busy(service, start_now, end_now, exclude_ids=excluded)
    if _conflicts_in_range(busy_now, start_now, end_now):
        print("Cannot preempt: another event blocks the candidate's full duration right now.")
        return False

    # Find a new slot for the current task later today/soon, before its deadline
    busy_future = _fetch_busy(
        service, now, now + datetime.timedelta(days=LIST_LOOKAHEAD_DAYS), exclude_ids=excluded)
    # Reserve the candidate occupying [start_now, end_now]
    busy_future.append((start_now - datetime.timedelta(minutes=BUFFER_MIN),
                        end_now + datetime.timedelta(minutes=BUFFER_MIN)))
    busy_future = _merge(busy_future)

    new_slot = _plan_first_free_given_busy(
        busy=busy_future,
        start_date=now.date(),
        duration_min=remaining_min,
        priority=pr_cur,
        deadline_dt=ddl_cur,
    )
    if not new_slot:
        print("❌ Cannot find a safe later slot for the current task; not preempting.")
        return False

    ns, ne = new_slot

    # Preview plan
    print("\nPreemption plan:")
    print(
        f"• NOW  → Start: '{best.get('summary','')}' for {best_dur}m — score={best_score:.3f}")
    print(
        f"• LATER→ Move current to {fmt_range_human(ns, ne)} — score was {score_cur:.3f}")

    if not auto_apply:
        ok = input("Apply switch? [Y/n]: ").strip().lower() or "y"
        if ok != "y":
            print("Aborted.")
            return False

    if not _apply_moves(service, [(current, ns, ne), (best, start_now, end_now)]):
        return False

    print("✅ Switched tasks.")
    list_events(service, max_results=10, time_min_dt=now_london() -
                datetime.timedelta(minutes=1))
    return True


# =====================
# Kickoff (start top task at now + 30m)
# =====================

def kickoff_top_task_now(service: object, *, auto_apply: bool = False) -> bool:
    """
    If no auto-managed task is currently running, pick the highest-score auto-managed task
    and move it to start at now + NOW_BUFFER_MIN (snapped to grid), respecting commitments
    and the deadline safety window.
    """
    now = now_london()
    # If something is already running, don't interfere.
    current = _find_current_auto_event(service, now)
    if current:
        print("An auto-managed task is already in progress; use 'preempt' to switch.")
        return True

    window_end = now + datetime.timedelta(days=LIST_LOOKAHEAD_DAYS)

    # Gather auto-managed candidates
    candidates = _future_auto_tasks(_fetch_events(service, now, window_end), now)
    if not candidates:
        print("No auto-managed tasks to start.")
        return True

    # Choose best by score
    best_ev = None
    best_score = -1.0
    best_dur = None
    best_pr = None
    best_deadline = None

    for ev in candidates:
        dur, pr, ddl_dt = _task_details(ev)
        sc = task_score(pr, dur, ddl_dt, now)
        if sc > best_score:
            best_ev = ev
            best_score = sc
            best_dur = dur
            best_pr = pr
            best_deadline = ddl_dt

    busy_future = _fetch_busy(service, now, window_end, exclude_ids={best_ev["id"]})
    slot = _plan_first_free_given_busy(
        busy_future, now.date(), best_dur, best_pr, best_deadline)
    if not slot:
        print("❌ Cannot find a free slot for the top task within working hours and its deadline.")
        return False
    desired_s, desired_e = slot

    # Apply
    print(
        f"\nKickoff plan: '{best_ev.get('summary','')}' at {fmt_range_human(desired_s, desired_e)} (score={best_score:.3f})")
    if not auto_apply:
        ok = input("Start this task? [Y/n]: ").strip().lower() or "y"
        if ok != "y":
            print("Cancelled.")
            return False

    if move_event(service, best_ev["id"], desired_s, desired_e):
        print("✅ Scheduled to start soon.")
        list_events(service, max_results=10,
                    time_min_dt=now_london() - datetime.timedelta(minutes=1))
        return True
    else:
        print("❌ Failed to move the task.")
        return False


# =====================
# One-shot helpers (headless)
# =====================

def run_daily_auto(service: object, *, auto_apply: bool = False) -> bool:
    """Run daily automation once: reorder auto-managed tasks respecting commitments."""
    print("\n=== Daily Auto-Scheduling ===")
    ok = reorder_managed_events(service, auto_apply=auto_apply, preview_only=not auto_apply)
    if ok:
        print("Done.")
    return ok


# =====================
# CLI (interactive + argparse)
# =====================

def _run_interactive_command(service: object, cmd: str) -> bool:
    if cmd == "list":
        return list_events(service, max_results=10,
                    time_min_dt=now_london() - datetime.timedelta(minutes=1))

    elif cmd == "add":
        date_str = input("Date (YYYY-MM-DD / today / tomorrow): ").strip()
        start_str = input(
            "Start time (HH:MM, 24h) [leave blank to auto]: ").strip()
        end_str = input(
            "End time   (HH:MM, 24h) [leave blank to auto]: ").strip()
        summary = input("Title: ").strip()
        desc = input("Description (optional): ").strip()
        if not summary:
            raise ValueError("Title cannot be empty.")
        if bool(start_str) != bool(end_str):
            raise ValueError("Enter both start and end times, or leave both blank for automatic scheduling.")
        pr, ddl_dt, prefix, props = get_priority_deadline_commitment()
        summary = prefix + summary
        color_id = priority_to_color(pr)

        # Recurrence (applies to both fixed or auto scheduling)
        rrule = get_recurrence_rule_interactive()

        if start_str and end_str:
            try:
                target_date = parse_easy_date(date_str)
                start_dt = _parse_local_time(target_date, start_str)
                end_dt = _parse_local_time(target_date, end_str)
            except ValueError:
                print("❌ Invalid date/time format.")
                return False

            if end_dt <= start_dt:
                print("❌ End time must be after start time.")
                return False

            min_start = now_london() + datetime.timedelta(minutes=NOW_BUFFER_MIN)
            if start_dt < min_start:
                print(
                    f"❌ Start must be at or after {min_start.strftime('%a %d %b %H:%M %Z')}.")
                return False

            if ddl_dt and end_dt > (ddl_dt - datetime.timedelta(hours=DEADLINE_SAFETY_HOURS)):
                print("❌ This would miss the deadline safety window.")
                return False

            if _has_conflict(service, start_dt, end_dt):
                print(
                    "❌ Time conflict with an existing event (commitments are hard blocks). Choose a different time.")
                return False

            # mark as managed but NOT auto (fixed placement)
            props_fixed = {**props, "auto": "0", "managed": "1"}
            if "deadline" not in props_fixed and ddl_dt:
                props_fixed["deadline"] = ddl_dt.date().isoformat()

            created = add_event(service, start_dt, end_dt, summary, desc,
                                rrule, color_id=color_id, extended_props=props_fixed)
            if not created:
                return False
            list_events(service, max_results=10, time_min_dt=now_london(
            ) - datetime.timedelta(minutes=1))
            return True

        # auto-schedule (first free slot)
        try:
            duration_str = input(
                "Duration in minutes (e.g., 60): ").strip()
            duration_min = int(duration_str)
            if duration_min <= 0:
                raise ValueError
        except ValueError:
            print("❌ Duration must be a positive integer (minutes).")
            return False

        try:
            target_date = parse_easy_date(date_str)
        except ValueError:
            print("❌ Invalid date. Use YYYY-MM-DD / today / tomorrow.")
            return False

        slot = _first_free_slot(
            service=service,
            start_date=target_date,
            duration_min=duration_min,
            priority=pr,
            deadline_dt=ddl_dt,
        )
        if not slot:
            if ddl_dt:
                print("❌ No free slot found before the deadline (with safety).")
            else:
                print(
                    f"❌ No free slot found in the next {HORIZON_DAYS} days within working hours.")
            return False

        start_dt, end_dt = slot
        print(
            f"➕ Scheduling '{summary}' at {fmt_range_human(start_dt, end_dt)}")
        props_auto = {**props, "auto": "1", "managed": "1"}
        if "deadline" not in props_auto and ddl_dt:
            props_auto["deadline"] = ddl_dt.date().isoformat()
        created = add_event(service, start_dt, end_dt, summary, desc, rrule,
                            color_id=color_id, extended_props=props_auto)
        if not created:
            return False
        list_events(service, max_results=10,
                    time_min_dt=now_london() - datetime.timedelta(minutes=1))

    elif cmd == "reorder":
        return reorder_managed_events(service)

    elif cmd == "preempt":
        return preempt_now(service)

    elif cmd == "kickoff":
        return kickoff_top_task_now(service)

    elif cmd == "delete":
        # pick event to delete
        now = now_london()
        start = now - datetime.timedelta(minutes=1)
        end = now + datetime.timedelta(days=LIST_LOOKAHEAD_DAYS)
        managed_only = (
            input("Show managed-only? [Y/n]: ").strip().lower() or "y") == "y"
        events = _fetch_events(service, start, end)
        if managed_only:
            events = [e for e in events if _is_managed(e)]
        if not events:
            print("No events found in window.")
            return True
        print("\nSelect event to delete:")
        for idx, ev in enumerate(events, 1):
            s, e = _parse_event_interval(ev)
            flags = _summarize_flags(ev)
            print(
                f"{idx:2d}) {fmt_range_human(s, e)} · {ev.get('summary','(no title)')} {flags} · id={ev['id']}")
        sel = input("Enter number (or 'q' to cancel): ").strip().lower()
        if sel in ("q", "quit", "exit", ""):
            print("Cancelled.")
            return False
        if not sel.isdigit() or not (1 <= int(sel) <= len(events)):
            print("Invalid selection.")
            return False
        ev = events[int(sel) - 1]
        if _is_commitment(ev):
            ok = input(
                "This is marked as a COMMITMENT. Delete anyway? [y/N]: ").strip().lower() or "n"
            if ok != "y":
                print("Cancelled.")
                return False
        ok = input(
            f"Delete '{ev.get('summary','(no title)')}'? [y/N]: ").strip().lower() or "n"
        if ok != "y":
            print("Cancelled.")
            return False
        if delete_event(service, ev["id"]):
            print("✅ Deleted.")
            list_events(service, max_results=10, time_min_dt=now_london(
            ) - datetime.timedelta(minutes=1))
        else:
            print("❌ Delete failed.")
            return False

    elif cmd == "exit":
        print("Goodbye!")
        return True

    else:
        print(
            "Unknown command. Use: list, add, reorder, preempt, kickoff, delete, or exit.")
        return False
    return True


def interactive_loop(service: object, command: Optional[str] = None) -> bool:
    """Run a guided one-shot command or keep accepting interactive commands."""
    while True:
        try:
            cmd = command or input(
                "\nEnter command [list, add, reorder, preempt, kickoff, delete, exit]: ").strip().lower()
            result = _run_interactive_command(service, cmd)
            if command is not None or cmd == "exit":
                return result
        except EOFError:
            print("\nGoodbye!")
            return command is None
        except (ValueError, KeyError, *API_ERRORS) as error:
            log.error("Command failed: %s", error)
            if command is not None:
                return False


def _parse_local_time(day: datetime.date, value: str) -> datetime.datetime:
    time = datetime.datetime.strptime(value, "%H:%M").time()
    local = datetime.datetime.combine(day, time, tzinfo=LONDON or datetime.timezone.utc)
    round_trip = local.astimezone(datetime.timezone.utc).astimezone(local.tzinfo)
    if round_trip.replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("This London time does not exist because of the daylight saving change.")
    return local


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return number


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("Must be finite and nonnegative.")
    return number


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Timer Manager — Google Calendar helper")
    sub = p.add_subparsers(dest="subcmd")

    # one-shot: list
    sp_list = sub.add_parser("list", help="List upcoming events")
    sp_list.add_argument("--max", type=_positive_int, default=10, help="Max results")

    # one-shot: add (guided, still interactive prompts for details)
    sub.add_parser("add", help="Add an event (guided)")

    # one-shot: reorder
    sp_re = sub.add_parser(
        "reorder", help="Reorder auto-managed events by ratio score")
    sp_re.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                       help="Apply without confirmation")
    sp_re.add_argument("--preview", action="store_true",
                       help="Preview only; no changes")

    # one-shot: preempt
    sp_pr = sub.add_parser(
        "preempt", help="If a better-scoring task exists, switch now")
    sp_pr.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                       help="Apply without confirmation")
    sp_pr.add_argument("--threshold", type=_nonnegative_float, default=SWITCH_THRESHOLD_DEFAULT,
                       help="Relative improvement needed to switch (e.g., 0.10 for 10%%)")

    # one-shot: kickoff
    sp_ko = sub.add_parser(
        "kickoff", help="Start the highest-score task at now + 30m (respects commitments)")
    sp_ko.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                       help="Apply without confirmation")

    # one-shot: delete (guided)
    sub.add_parser("delete", help="Delete an event (guided)")

    # daily auto mode
    p.add_argument("--auto", action="store_true",
                   help="Preview daily scheduling headlessly; add -y to apply")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Apply without confirmation (for --auto or reorder)")

    args = p.parse_args()
    if args.auto and args.subcmd:
        p.error("Use --auto by itself, without a subcommand.")
    return args


def main() -> int:
    args = parse_args()
    service = authenticate(interactive=not args.auto)
    if not service:
        return 1
    # Headless daily auto
    if args.auto:
        return 0 if run_daily_auto(service, auto_apply=args.yes) else 1

    # Subcommands
    if args.subcmd == "list":
        return 0 if list_events(service, max_results=args.max,
                               time_min_dt=now_london() - datetime.timedelta(minutes=1)) else 1
    if args.subcmd == "add":
        return 0 if interactive_loop(service, command="add") else 1
    if args.subcmd == "reorder":
        return 0 if reorder_managed_events(service, auto_apply=args.yes,
                                          preview_only=args.preview) else 1
    if args.subcmd == "preempt":
        return 0 if preempt_now(service, threshold=args.threshold, auto_apply=args.yes) else 1
    if args.subcmd == "kickoff":
        return 0 if kickoff_top_task_now(service, auto_apply=args.yes) else 1
    if args.subcmd == "delete":
        return 0 if interactive_loop(service, command="delete") else 1

    # Default to interactive loop (backwards compatible)
    return 0 if interactive_loop(service) else 1


def cli() -> int:
    try:
        return main()
    except (ValueError, KeyError, *API_ERRORS) as error:
        log.error("Command failed: %s", error)
        return 1
    except EOFError:
        print("\nInput ended; cancelled.")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
