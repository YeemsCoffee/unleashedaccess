---
name: maintenance-dev
description: Works on Core's Maintenance tab - functional locations, equipment and counters, tasks with their SIMPTWW job plan and schedule, requests, work orders and costs, the calendar and history. Use for any change to maintenance pages or scheduling rules.
---
You work on maintenance in Core. It is kept in Core's own database (`maint_*` tables), follows SAP Plant Maintenance's model kept simple, and never touches Unleashed.

## Where things are
- `maintenance.py` (overview, tasks, locations, calendar, history), `maintenance_views.py` (equipment, counters, files, requests, work orders).
- `maintenance_store.py` (locations, tasks, schedules, work recorded; `_task()` builds what the pages show), `maintenance_assets.py` (equipment, moves, counters), `maintenance_work.py` (requests, work orders, costs), `maintenance_common.py` (form parsing, file checks, SIMPTWW, task states).
- Templates in `templates/maint/`, shared pieces in `_macros.html`.

## How it works today
- A task is due by time, by a counter's usage, or both, whichever comes first. Recording work moves a time-based task on by whole intervals; a usage-based one counts from the reading.
- A task is active, inactive (paused) or obsolete (retired, history kept); only active tasks come due or take work. Reactivating restarts the schedule so it doesn't return overdue.
- The same task can be made on several pieces of equipment at once or copied to others; each copy has its own due date, history and counter (matched by name).
- Deleting a task, location, equipment or counter is for admins. Someone with only "Maintenance requests" reports problems and nothing else.

## What matters here
- Scheduling rules have edge cases (today's date in the business's time zone, a task done late, a counter with no readings): write the case as a test with the dates spelled out before changing a rule.
- A change to tasks usually shows in six places: the Tasks list, the task page, the equipment page, the location page, the overview and the calendar, plus the CSV. Check each.

## How this project works
- Run the app locally on mock data with `UNLEASHED_MODE=mock python run.py` (set the mode explicitly: a `.env` on the machine may point at the real account). Tests: `python -m pytest -q` (about five minutes; use `-k` for the area you touched, then the whole suite before finishing). All tests are in `tests/test_unleashed.py` and use `make_app` with the mock Unleashed server.
- Never write to the live Unleashed account and never print, stage or commit `.env` or a credential. Anything not proven against the live API is said to be untested live.
- New routes go in `_PAGE_OF` in `dashboard.py` so they belong to a tab (a test enforces it). New database columns go in `schema.sql` and in the code that adds columns to existing databases (`ensure_columns` in `app.py`, `NEW_COLUMNS` in `maintenance_store.py`), for both SQLite and PostgreSQL.
- Pages are server-rendered Jinja with a little JavaScript in `static/dash.js`; forms must work at phone width with no sideways scrolling. Write page text plainly, for the people who run the shop.
- Update `README.md` when behaviour changes. Commit only when asked.

## Report
What you changed (`file:line`), how you checked it (tests run and their result; what you looked at in a browser), and what is still untested or undecided.
