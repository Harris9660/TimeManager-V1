# Timer Manager

Turn tasks into focused work blocks on your Google Calendar.

Timer Manager is a Python command-line tool that finds free time, prioritises
work, and rearranges scheduled tasks while respecting commitments. It uses
your primary Google Calendar and displays times in `Europe/London`, including
daylight saving changes.

## Features

- Add tasks at fixed times or automatically find a free slot.
- Set priorities from 1 to 5, deadlines, and recurring schedules.
- Mark commitments as hard blocks that automation cannot move.
- Reorder tasks by weighted priority, deadline urgency, and duration.
- Keep recurring occurrences on the same date during reordering.
- Start the highest-scoring task with `kickoff`, or switch tasks with `preempt`.
- Preview scheduling changes before applying them.

## Requirements

- Python **3.10 or newer** and `pip`.
- A Google account with Google Calendar enabled.
- A Google Cloud project with the Calendar API enabled and a desktop OAuth client.

## Quick start

### 1. Install dependencies

Download or clone this repository, then open a terminal in the project folder.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The examples below use `python` from the activated environment. On Windows,
use `.venv\Scripts\python.exe` in place of `python`.

### 2. Set up Google Calendar access

1. Create or select a project in the [Google Cloud console](https://console.cloud.google.com/).
2. Enable the **Google Calendar API** and configure the OAuth consent screen.
3. Create an OAuth client with application type **Desktop app**.
4. Download its JSON file and save it as `credentials.json` beside `main.py`.

See [Google's Calendar Python quickstart](https://developers.google.com/workspace/calendar/api/quickstart/python)
for the full setup instructions.

### 3. Sign in and run

```bash
python main.py
```

On the first run, a browser opens so you can sign in and authorise Calendar
access. The app requests the `https://www.googleapis.com/auth/calendar` scope
and saves your authorisation in `token.json` for later runs. Both authentication
files are resolved relative to `main.py`.

At the interactive prompt, enter `list`, `add`, `reorder`, `preempt`, `kickoff`,
`delete`, or `exit`.

## Commands

| Command | What it does |
| --- | --- |
| `python main.py` | Open the interactive prompt. |
| `python main.py list` | Show the next 10 upcoming events. |
| `python main.py list --max 25` | Show up to 25 upcoming events. |
| `python main.py add` | Run the guided task-creation prompts once. |
| `python main.py delete` | Select and confirm an event to delete. |
| `python main.py reorder` | Show a reorder plan and ask whether to apply it. |
| `python main.py reorder --preview` | Show a reorder plan without changing events. |
| `python main.py reorder -y` | Apply a reorder plan without confirmation. |
| `python main.py kickoff` | Find a free slot for the highest-scoring task and ask to move it. |
| `python main.py preempt` | Propose switching from the current task to a higher-scoring task. |
| `python main.py --help` | Show command-line help. |

`kickoff` and `preempt` also accept `-y` to apply changes without confirmation.
Use `python main.py <command> --help` for command-specific options.

For example, require a 25% score improvement before switching tasks:

```bash
python main.py preempt --threshold 0.25
```

### Adding a task

The guided prompts ask for a date, title, description, priority, optional
deadline, commitment status, and recurrence.

- **Fixed time:** enter both start and end times as `HH:MM` using the 24-hour clock.
- **Automatic placement:** leave both times blank, then enter a duration in minutes.
- **Dates:** use `YYYY-MM-DD`, `today`, or `tomorrow`.
- **Recurrence:** choose `none`, `daily`, `weekly`, or a custom `RRULE`.
  Daily and weekly presets accept either a positive occurrence count or an
  until date. Until dates include the whole London date.

## Scheduling behaviour

### Priorities and protected events

Reordering sorts movable tasks by weighted priority and deadline urgency,
divided by task duration. Higher scores are placed first.

Only future, timed events marked as both managed and auto-scheduled are
candidates. Commitments, fixed events, busy all-day events, and tasks already
in progress remain in place. Events marked as free or declined by you do not
block time, unless they are commitments.

Automatic placement respects the requested start date and never schedules in
the past. Working hours apply every calendar day, including weekends.
`kickoff` looks for a slot at least 30 minutes ahead; `preempt` can switch at
the next 15-minute boundary. Both respect other tasks and working hours.

### Recurring events keep their date when reordered

Each recurring occurrence keeps its **current London calendar date** during
`reorder`. Only its time changes, with its duration preserved.

If an occurrence cannot fit on that date within working hours, buffers, and
its deadline, the entire reorder stops before applying any changes. This rule
also applies to reordering through `--auto`.

### Deadlines

A deadline date means **18:00 London time** on that date. Scheduling also
reserves a **12-hour safety margin** before the deadline.

With the defaults, a task due today must finish by 06:00 today, which is outside
working hours. Choose a later deadline or adjust `DEADLINE_SAFETY_HOURS` in
`main.py` if you want a smaller margin.

### Limits and failed updates

Task creation checks the first recurring occurrence for conflicts. Review
later occurrences in Google Calendar.

If a reorder or preemption update fails partway through, the app attempts to
restore the original event times. If a restore also fails, it reports the
affected event ID so you can check that event in Calendar.

## Headless scheduling

Authorise Calendar access interactively before using headless mode.

Preview the daily reorder without prompts or event changes:

```bash
python main.py --auto
```

Apply it without prompts:

```bash
python main.py --auto -y
```

Each invocation runs once; the app does not install a background scheduler.
Headless mode can refresh an existing token, but exits with an error when a
new browser sign-in is required. Use `--auto` without a subcommand.

## Configuration

Edit the constants near the top of `main.py` to change the defaults.

| Setting | Default | Purpose |
| --- | --- | --- |
| `WORK_START` / `WORK_END` | `09:00` / `18:00` | Working hours in London time. |
| `GRID_MIN` | `15` | Automatic start-time grid in minutes. |
| `BUFFER_MIN` | `5` | Gap around existing and planned events in minutes. |
| `NOW_BUFFER_MIN` | `30` | Minimum lead time for automatic placement and kickoff. |
| `HORIZON_DAYS` | `14` | Days searched for unconstrained task placement. |
| `LIST_LOOKAHEAD_DAYS` | `30` | Event lookup window for reorder, kickoff, and delete. |
| `DEADLINE_SAFETY_HOURS` | `12` | Margin before a task's deadline. |
| `SWITCH_THRESHOLD_DEFAULT` | `0.10` | Minimum relative score improvement for preemption. |

Set `TM_LOG` to a Python logging level to change log verbosity:

```bash
TM_LOG=DEBUG python main.py list
```

## Development

Run the offline regression tests:

```bash
python -m unittest -v
```

The tests simulate Calendar operations and require no Google sign-in or live
calendar access.

VS Code is configured to use `.venv/bin/python` on macOS and Linux. If imports
appear unresolved, run **Python: Select Interpreter**, select the project's
virtual environment, and open a new terminal. On Windows, select
`.venv\Scripts\python.exe`.

## Authentication files

`credentials.json`, `token.json`, temporary token files, and `.venv/` are
excluded by `.gitignore`. Keep authentication files private and supply your
own credentials when setting up the project.

## Project files

| File | Purpose |
| --- | --- |
| `main.py` | Calendar integration, scheduling, and CLI. |
| `test_main.py` | Offline regression tests. |
| `requirements.txt` | Python dependencies. |
| `.vscode/settings.json` | Workspace Python settings. |
| `README.md` | Formatted GitHub documentation. |
| `README.txt` | A matching copy of the documentation. |

Keep `README.md` and `README.txt` in sync when updating the documentation.
