import copy
import datetime as dt
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main as tm


def at(hour, minute=0, *, day=9, second=0):
    return dt.datetime(2026, 10, day, hour, minute, second, tzinfo=tm.LONDON)


def event(event_id, start, end, *, priority=3, auto=True, commitment=False, deadline=None):
    props = {"managed": "1", "auto": "1" if auto else "0", "priority": str(priority)}
    if commitment:
        props["commitment"] = "1"
    if deadline:
        props["deadline"] = deadline
    return {"id": event_id, "summary": event_id, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}, "extendedProperties": {"private": props}}


class Request:
    def __init__(self, execute):
        self.execute = execute


class Calendar:
    """In-memory Calendar API: filters, pagination, writes, and injected failures."""
    def __init__(self, events=(), *, page_size=2500, fail_patch_calls=()):
        self.data = {ev["id"]: copy.deepcopy(ev) for ev in events}
        self.page_size = page_size
        self.fail_patch_calls = set(fail_patch_calls)
        self.patch_calls = []
        self.list_calls = []
        self.insert_calls = []
        self.delete_calls = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        self._check_calendar(kwargs)
        lower = dt.datetime.fromisoformat(kwargs["timeMin"].replace("Z", "+00:00"))
        upper = dt.datetime.fromisoformat(kwargs["timeMax"].replace("Z", "+00:00")) if "timeMax" in kwargs else None
        matching = [ev for ev in self.data.values() if ev.get("status") != "cancelled"
                    and tm._parse_event_interval(ev)[1] > lower
                    and (upper is None or tm._parse_event_interval(ev)[0] < upper)]
        matching.sort(key=lambda ev: tm._parse_event_interval(ev)[0])
        offset = int(kwargs.get("pageToken", 0))
        size = min(self.page_size, kwargs.get("maxResults", 2500))
        response = {"items": copy.deepcopy(matching[offset:offset + size])}
        if offset + size < len(matching):
            response["nextPageToken"] = str(offset + size)
        return Request(lambda: response)

    def patch(self, **kwargs):
        self._check_calendar(kwargs)
        self.patch_calls.append(copy.deepcopy(kwargs))
        call_number = len(self.patch_calls)

        def execute():
            if call_number in self.fail_patch_calls:
                raise OSError("Simulated connection failure")
            self.data[kwargs["eventId"]].update(copy.deepcopy(kwargs["body"]))
            return copy.deepcopy(self.data[kwargs["eventId"]])
        return Request(execute)

    def insert(self, **kwargs):
        self._check_calendar(kwargs)
        self.insert_calls.append(copy.deepcopy(kwargs))
        created = {"id": "new", **copy.deepcopy(kwargs["body"])}
        self.data["new"] = created
        return Request(lambda: created)

    def delete(self, **kwargs):
        self._check_calendar(kwargs)
        self.delete_calls.append(kwargs)
        return Request(lambda: self.data.pop(kwargs["eventId"]))

    @staticmethod
    def _check_calendar(kwargs):
        if kwargs["calendarId"] != "primary":
            raise AssertionError("Wrong calendar")


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(tm, "now_london", return_value=at(10))
        self.clock.start()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.addCleanup(self.clock.stop)

    def assert_clear(self, calendar):
        intervals = sorted([(*tm._parse_event_interval(ev), ev["id"]) for ev in calendar.data.values()])
        for (_, end, first), (start, _, second) in zip(intervals, intervals[1:]):
            self.assertGreaterEqual(start, end + dt.timedelta(minutes=tm.BUFFER_MIN), (first, second))

    def test_insert_uses_primary_calendar(self):
        calendar = Calendar()
        self.assertTrue(tm.add_event(calendar, at(12), at(13), "Task"))
        self.assertEqual(calendar.insert_calls[0]["calendarId"], "primary")

    def test_events_paginate(self):
        calendar = Calendar([event(str(i), at(11 + i), at(12 + i)) for i in range(4)], page_size=1)
        self.assertEqual(len(tm._fetch_events(calendar, at(10), at(18))), 4)
        self.assertEqual(len(calendar.list_calls), 4)

    def test_list_respects_limit_across_pages(self):
        calendar = Calendar([event(str(i), at(11 + i), at(12 + i)) for i in range(4)], page_size=1)
        self.assertTrue(tm.list_events(calendar, max_results=3))
        self.assertEqual(len(calendar.list_calls), 3)

    def test_pagination_accepts_empty_intermediate_page(self):
        calendar = Mock()
        calendar.events.return_value.list.return_value.execute.side_effect = [
            {"items": [], "nextPageToken": "next"}, {"items": [event("a", at(11), at(12))]}]
        self.assertEqual(len(tm._fetch_events(calendar, at(10), at(18))), 1)

    def test_buffers_include_event_ending_before_lookup(self):
        calendar = Calendar([event("before", at(10), at(10, 26), auto=False)])
        self.assertTrue(tm._has_conflict(calendar, at(10, 30), at(11)))

    def test_buffers_include_event_starting_after_lookup(self):
        calendar = Calendar([event("after", at(11, 4), at(12), auto=False)])
        self.assertTrue(tm._has_conflict(calendar, at(10, 30), at(11)))

    def test_exact_buffer_gap_is_allowed(self):
        calendar = Calendar([event("before", at(10), at(10, 25), auto=False)])
        self.assertFalse(tm._has_conflict(calendar, at(10, 30), at(11)))

    def test_transparent_and_declined_events_do_not_block(self):
        free = event("free", at(10), at(12))
        free["transparency"] = "transparent"
        declined = event("declined", at(10), at(12))
        declined["attendees"] = [{"self": True, "responseStatus": "declined"}]
        self.assertFalse(tm._fetch_busy(Calendar([free, declined]), at(10), at(12)))

    def test_commitment_still_blocks_when_transparent(self):
        commitment = event("commit", at(10), at(12), commitment=True)
        commitment["transparency"] = "transparent"
        self.assertTrue(tm._fetch_busy(Calendar([commitment]), at(10), at(12)))

    def test_grid_never_rounds_seconds_backwards(self):
        self.assertEqual(tm._snap_to_grid(at(10, 30, second=1), 15), at(10, 45))
        self.assertEqual(tm._snap_to_grid(at(10, 30), 15), at(10, 30))
        self.assertEqual(tm._snap_to_grid(at(10, 59, second=59), 15), at(11))

    def test_past_date_never_schedules_in_past(self):
        slot = tm._first_free_slot(Calendar(), at(9, day=8).date(), 60)
        self.assertEqual(slot, (at(10, 30), at(11, 30)))
        self.assertEqual(tm._plan_first_free_given_busy([], at(9, day=8).date(), 60, 3, None), slot)

    def test_high_priority_respects_requested_future_date(self):
        slot = tm._first_free_slot(Calendar(), at(9, day=10).date(), 60, priority=5)
        self.assertEqual(slot, (at(9, day=10), at(10, day=10)))

    def test_all_day_event_blocks_entire_day(self):
        all_day = {"id": "day", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-10"}}
        slot = tm._first_free_slot(Calendar([all_day]), at(10).date(), 60)
        self.assertEqual(slot[0], at(9, day=10))

    def test_reorder_preserves_commitments_and_current_task(self):
        running = event("running", at(9), at(11))
        commit = event("commit", at(12), at(13), commitment=True)
        future = event("future", at(14), at(15))
        calendar = Calendar([running, commit, future])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual({call["eventId"] for call in calendar.patch_calls}, {"future"})
        self.assertEqual(calendar.data["running"], running)
        self.assertEqual(calendar.data["commit"], commit)
        self.assert_clear(calendar)

    def test_reorder_scores_tasks_and_keeps_buffers(self):
        calendar = Calendar([event("low", at(14), at(15), priority=1),
                             event("high", at(15), at(16), priority=5)])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["high"])[0], at(10, 30))
        self.assertEqual(tm._parse_event_interval(calendar.data["low"])[0], at(11, 45))
        self.assert_clear(calendar)

    def test_impossible_reorder_changes_nothing(self):
        calendar = Calendar([event("late", at(14), at(15), deadline="2026-10-09")])
        original = copy.deepcopy(calendar.data)
        self.assertFalse(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(calendar.data, original)
        self.assertFalse(calendar.patch_calls)

    def test_reorder_recurring_occurrences_keep_their_dates(self):
        first = event("first", at(14, day=10), at(15, day=10), priority=1)
        second = event("second", at(14, day=11), at(15, day=11), priority=5)
        for occurrence in (first, second):
            occurrence["recurringEventId"] = "series"
            occurrence["originalStartTime"] = copy.deepcopy(occurrence["start"])
        calendar = Calendar([first, second])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["first"]),
                         (at(9, day=10), at(10, day=10)))
        self.assertEqual(tm._parse_event_interval(calendar.data["second"]),
                         (at(9, day=11), at(10, day=11)))
        self.assertEqual(calendar.data["first"]["originalStartTime"], first["originalStartTime"])
        self.assert_clear(calendar)

    def test_reorder_recurring_occurrence_uses_current_date_after_exception(self):
        occurrence = event("exception", at(14, day=11), at(15, day=11))
        occurrence["recurringEventId"] = "series"
        occurrence["originalStartTime"] = {"dateTime": at(14, day=10).isoformat()}
        calendar = Calendar([occurrence])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["exception"])[0], at(9, day=11))

    def test_reorder_recurring_occurrence_date_is_in_london(self):
        occurrence = event("utc", dt.datetime(2026, 10, 9, 23, 30, tzinfo=dt.timezone.utc),
                           dt.datetime(2026, 10, 10, 0, 30, tzinfo=dt.timezone.utc))
        occurrence["recurringEventId"] = "series"
        calendar = Calendar([occurrence])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["utc"])[0], at(9, day=10))

    def test_reorder_recurring_occurrence_today_stays_future(self):
        occurrence = event("today", at(14), at(15))
        occurrence["recurringEventId"] = "series"
        calendar = Calendar([occurrence])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["today"])[0], at(10, 30))

    def test_reorder_cannot_spill_recurring_occurrence_into_another_day(self):
        occurrence = event("repeat", at(14, day=10), at(15, day=10), priority=1)
        occurrence["recurringEventId"] = "series"
        blocker = event("blocked-day", at(9, day=10), at(18, day=10), commitment=True)
        ordinary = event("ordinary", at(14, day=11), at(15, day=11), priority=5)
        calendar = Calendar([occurrence, blocker, ordinary])
        original = copy.deepcopy(calendar.data)
        self.assertFalse(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)
        self.assertEqual(calendar.data, original)

    def test_reorder_recurring_occurrence_beyond_normal_planning_horizon(self):
        future_start = at(14) + dt.timedelta(days=20)
        occurrence = event("later", future_start, future_start + dt.timedelta(hours=1))
        occurrence["recurringEventId"] = "series"
        calendar = Calendar([occurrence])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        new_start, new_end = tm._parse_event_interval(calendar.data["later"])
        self.assertEqual(new_start.date(), future_start.date())
        self.assertEqual(new_end.date(), future_start.date())
        self.assertEqual(new_start.hour, 9)

    def test_reorder_checks_entire_day_at_lookup_boundary(self):
        last_day = at(9) + dt.timedelta(days=tm.LIST_LOOKAHEAD_DAYS)
        occurrence = event("last", last_day, last_day + dt.timedelta(hours=1))
        occurrence["recurringEventId"] = "series"
        # This blocker starts after the original timeMax (10:00), but still
        # prevents placing this occurrence later on the same day.
        morning = event("morning", last_day, last_day + dt.timedelta(hours=3), auto=False)
        afternoon = event("afternoon", last_day + dt.timedelta(hours=3),
                          last_day + dt.timedelta(hours=9), auto=False)
        calendar = Calendar([occurrence, morning, afternoon])
        original = copy.deepcopy(calendar.data)
        self.assertFalse(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)
        self.assertEqual(calendar.data, original)

    def test_reorder_nonrecurring_task_can_still_change_date(self):
        calendar = Calendar([event("ordinary", at(14, day=11), at(15, day=11))])
        self.assertTrue(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["ordinary"])[0], at(10, 30))

    def test_preview_never_prompts_or_writes(self):
        calendar = Calendar([event("task", at(14), at(15))])
        with patch("builtins.input", side_effect=AssertionError("Unexpected prompt")):
            self.assertTrue(tm.run_daily_auto(calendar))
        self.assertFalse(calendar.patch_calls)

    def test_kickoff_avoids_other_auto_tasks(self):
        calendar = Calendar([event("other", at(10, 30), at(11, 30), priority=1),
                             event("best", at(15), at(16), priority=5)])
        self.assertTrue(tm.kickoff_top_task_now(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["best"])[0], at(11, 45))
        self.assertEqual([c["eventId"] for c in calendar.patch_calls], ["best"])
        self.assert_clear(calendar)

    def test_kickoff_moves_to_next_workday_at_night(self):
        calendar = Calendar([event("best", at(15, day=10), at(16, day=10))])
        with patch.object(tm, "now_london", return_value=at(20)):
            self.assertTrue(tm.kickoff_top_task_now(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["best"])[0], at(9, day=10))

    def test_kickoff_cannot_move_commitment(self):
        calendar = Calendar([event("commit", at(15), at(16), commitment=True)])
        self.assertTrue(tm.kickoff_top_task_now(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)

    def test_preemption_preserves_unrelated_auto_tasks(self):
        calendar = Calendar([event("current", at(9), at(11), priority=1),
                             event("best", at(15), at(15, 30), priority=5),
                             event("other", at(10, 45), at(11, 45), priority=1)])
        self.assertTrue(tm.preempt_now(calendar, auto_apply=True))
        self.assertEqual(tm._parse_event_interval(calendar.data["current"])[0], at(12))
        self.assertEqual({c["eventId"] for c in calendar.patch_calls}, {"current", "best"})
        self.assert_clear(calendar)

    def test_preemption_blocked_by_another_auto_task(self):
        calendar = Calendar([event("current", at(9), at(11), priority=1),
                             event("best", at(15), at(16), priority=5),
                             event("other", at(10, 45), at(11, 45), priority=1)])
        self.assertFalse(tm.preempt_now(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)

    def test_preemption_respects_candidate_deadline(self):
        calendar = Calendar([event("current", at(9), at(11), priority=1),
                             event("best", at(15), at(16), priority=5, deadline="2026-10-09")])
        self.assertFalse(tm.preempt_now(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)

    def test_preemption_respects_working_hours(self):
        calendar = Calendar([event("current", at(17), at(19), priority=1),
                             event("best", at(15, day=10), at(16, day=10), priority=5)])
        with patch.object(tm, "now_london", return_value=at(17, 45)):
            self.assertFalse(tm.preempt_now(calendar, auto_apply=True))
        self.assertFalse(calendar.patch_calls)

    def test_preemption_reschedules_only_remaining_time_at_switch(self):
        calendar = Calendar([event("current", at(9), at(11), priority=1),
                             event("best", at(15), at(15, 30), priority=5)])
        with patch.object(tm, "now_london", return_value=at(10, 7)):
            self.assertTrue(tm.preempt_now(calendar, auto_apply=True))
        s, e = tm._parse_event_interval(calendar.data["current"])
        self.assertEqual(e - s, dt.timedelta(minutes=45))
        self.assertEqual(tm._parse_event_interval(calendar.data["best"])[0], at(10, 15))

    def test_partial_reorder_failure_restores_original_times(self):
        calendar = Calendar([event("one", at(14), at(15)), event("two", at(16), at(17))],
                            fail_patch_calls={2})
        original = copy.deepcopy(calendar.data)
        with self.assertLogs(tm.log, level="ERROR"):
            self.assertFalse(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertEqual(calendar.data, original)

    def test_partial_preemption_failure_restores_original_times(self):
        calendar = Calendar([event("current", at(9), at(11), priority=1),
                             event("best", at(15), at(16), priority=5)], fail_patch_calls={2})
        original = copy.deepcopy(calendar.data)
        with self.assertLogs(tm.log, level="ERROR"):
            self.assertFalse(tm.preempt_now(calendar, auto_apply=True))
        self.assertEqual(calendar.data, original)

    def test_failed_rollback_is_reported(self):
        calendar = Calendar([event("one", at(14), at(15)), event("two", at(16), at(17))],
                            fail_patch_calls={2, 4})
        with self.assertLogs(tm.log, level="ERROR") as logs:
            self.assertFalse(tm.reorder_managed_events(calendar, auto_apply=True))
        self.assertTrue(any("Could not restore event" in line for line in logs.output))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_add_one_shot_does_not_request_command(self):
        calendar = Calendar()
        with patch.object(tm, "now_london", return_value=at(10)), patch("builtins.input", side_effect=[
                "tomorrow", "12:00", "13:00", "Task", "", "", "", "n", "none"]) as inputs:
            self.assertTrue(tm.interactive_loop(calendar, command="add"))
        self.assertEqual(len(calendar.insert_calls), 1)
        self.assertFalse(any("Enter command" in c.args[0] for c in inputs.call_args_list))

    def test_auto_add_and_commitment_metadata(self):
        calendar = Calendar()
        with patch.object(tm, "now_london", return_value=at(10)), patch("builtins.input", side_effect=[
                "today", "", "", "Task", "", "4", "", "y", "none", "60"]):
            self.assertTrue(tm.interactive_loop(calendar, command="add"))
        self.assertEqual(tm._parse_event_interval(calendar.data["new"]), (at(10, 30), at(11, 30)))
        self.assertTrue(tm._is_commitment(calendar.data["new"]))

    def test_delete_one_shot_executes_and_exits(self):
        calendar = Calendar([event("task", at(14), at(15))])
        with patch.object(tm, "now_london", return_value=at(10)), patch("builtins.input", side_effect=["", "1", "y"]):
            self.assertTrue(tm.interactive_loop(calendar, command="delete"))
        self.assertNotIn("task", calendar.data)

    def test_invalid_recurrence_does_not_create_event(self):
        calendar = Calendar()
        with patch("builtins.input", side_effect=[
                "tomorrow", "12:00", "13:00", "Task", "", "", "", "n", "daily", "0", ""]), \
                self.assertLogs(tm.log, level="ERROR"):
            self.assertFalse(tm.interactive_loop(calendar, command="add"))
        self.assertFalse(calendar.insert_calls)

    def test_partial_times_rejected(self):
        calendar = Calendar()
        with patch("builtins.input", side_effect=["tomorrow", "12:00", "", "Task", ""]), \
                self.assertLogs(tm.log, level="ERROR"):
            self.assertFalse(tm.interactive_loop(calendar, command="add"))
        self.assertFalse(calendar.insert_calls)

    def test_interactive_loop_recovers_from_calendar_failure(self):
        with patch("builtins.input", side_effect=["reorder", "exit"]), \
                patch.object(tm, "reorder_managed_events", side_effect=OSError("offline")), \
                self.assertLogs(tm.log, level="ERROR"):
            self.assertTrue(tm.interactive_loop(Calendar()))

    def test_interactive_eof_exits_cleanly(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertTrue(tm.interactive_loop(Calendar()))

    def test_yes_flag_before_and_after_subcommand(self):
        for args in (["-y", "reorder"], ["reorder", "-y"], ["-y", "preempt"], ["kickoff", "-y"]):
            with self.subTest(args=args), patch("sys.argv", ["main.py", *args]):
                self.assertTrue(tm.parse_args().yes)

    def test_invalid_cli_options_fail_before_authentication(self):
        for args in (["list", "--max", "0"], ["preempt", "--threshold", "nan"],
                     ["preempt", "--threshold", "-1"], ["--auto", "delete"]):
            with self.subTest(args=args), patch("sys.argv", ["main.py", *args]), \
                    patch.object(tm, "authenticate") as auth, redirect_stdout(io.StringIO()), \
                    patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
                tm.main()
            self.assertEqual(error.exception.code, 2)
            auth.assert_not_called()

    def test_cli_reports_failed_calendar_operation(self):
        with patch("sys.argv", ["main.py", "reorder", "-y"]), \
                patch.object(tm, "authenticate", return_value=Calendar()), \
                patch.object(tm, "reorder_managed_events", return_value=False):
            self.assertEqual(tm.cli(), 1)


class AuthAndDateTests(unittest.TestCase):
    def test_no_headless_browser_when_token_missing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tm, "PROJECT_DIR", Path(folder)), \
                patch.object(tm.InstalledAppFlow, "from_client_secrets_file") as flow, \
                self.assertLogs(tm.log, level="ERROR"):
            self.assertIsNone(tm.authenticate(interactive=False))
            flow.assert_not_called()

    def test_invalid_token_reauthenticates_using_project_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "token.json").write_text("invalid JSON")
            credentials = Mock(valid=True)
            credentials.to_json.return_value = '{"test": true}'
            with patch.object(tm, "PROJECT_DIR", project), \
                    patch.object(tm.InstalledAppFlow, "from_client_secrets_file") as flow, \
                    patch.object(tm, "build", return_value="calendar"), self.assertLogs(tm.log):
                flow.return_value.run_local_server.return_value = credentials
                self.assertEqual(tm.authenticate(), "calendar")
            flow.assert_called_once_with(str(project / "credentials.json"), tm.SCOPES)
            self.assertEqual((project / "token.json").read_text(), '{"test": true}')
            self.assertEqual((project / "token.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse((project / "token.json.tmp").exists())

    def test_missing_credentials_is_handled(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tm, "PROJECT_DIR", Path(folder)), \
                self.assertLogs(tm.log, level="ERROR"):
            self.assertIsNone(tm.authenticate())

    def test_refresh_failure_does_not_open_headless_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "token.json").write_text("{}")
            credentials = Mock(valid=False, expired=True, refresh_token="test")
            credentials.refresh.side_effect = tm.GoogleAuthError("invalid_grant")
            with patch.object(tm, "PROJECT_DIR", project), \
                    patch.object(tm.Credentials, "from_authorized_user_file", return_value=credentials), \
                    patch.object(tm.InstalledAppFlow, "from_client_secrets_file") as flow, \
                    self.assertLogs(tm.log):
                self.assertIsNone(tm.authenticate(interactive=False))
            flow.assert_not_called()

    def test_recurrence_rejects_count_and_until_combination(self):
        with self.assertRaises(ValueError):
            tm.build_rrule("daily", count=2, until_date=at(10).date())

    def test_recurrence_until_includes_evening_in_london(self):
        self.assertIn("UNTIL=20261009T225959Z", tm.build_rrule("daily", until_date=at(10).date()))

    def test_invalid_weekday_is_rejected(self):
        with self.assertRaises(ValueError):
            tm.build_rrule("weekly", byday=["MO", "BAD"])

    def test_london_summer_and_winter_offsets(self):
        summer = tm._parse_local_time(dt.date(2026, 7, 1), "09:00")
        winter = tm._parse_local_time(dt.date(2026, 12, 1), "09:00")
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(winter.utcoffset(), dt.timedelta(0))

    def test_nonexistent_london_time_is_rejected(self):
        with self.assertRaises(ValueError):
            tm._parse_local_time(dt.date(2026, 3, 29), "01:30")

    def test_calendar_datetime_without_offset_uses_declared_timezone(self):
        s, e = tm._parse_event_interval({"start": {"dateTime": "2026-10-09T09:00:00", "timeZone": "Europe/London"},
                                        "end": {"dateTime": "2026-10-09T10:00:00", "timeZone": "Europe/London"}})
        self.assertEqual(s, at(9))
        self.assertEqual(e, at(10))


if __name__ == "__main__":
    unittest.main()
