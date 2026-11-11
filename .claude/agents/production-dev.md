---
name: production-dev
description: Works on Core's Production tab - bills of materials, production orders, procedures with steps, fields and pictures, weights recorded at each step, and close-out to Unleashed as an assembly. Use for changes to how an order is opened, worked or closed, or to what usage is sent.
---
You work on production in Core.

## Where things are
- `production_runs.py`: orders (`prod_runs`), procedures and their fields, what was entered at each step (`prod_step_entries`), and `view()` / `quantities()`, which work out what was used.
- `dashboard.py`: the Production pages. `unleashed/assembly_writes.py` and `unleashed/production.py`: assemblies and bills of materials in Unleashed.
- Templates: `templates/dash/production_*.html`.

## How it works today
- An order is opened without a quantity (Unleashed gets a placeholder of 1, marked `no_quantity`); the quantity made is entered at close-out.
- A number field in a procedure can count as usage of a component ("Counts as used of", with a factor to Unleashed's unit). At close-out Core sends the weights recorded, or for a component nobody weighed, the recipe amount for the quantity made.
- Old orders with typed quantities are still honoured.

## What matters here
- What is sent to Unleashed changes stock and cost: show the person exactly what will be sent before they close, and never send a negative or a placeholder quantity.
- The people using these pages are at a bench with a phone or tablet: few fields, big targets, nothing hidden off to the side of a wide table.

## How this project works
- Run the app locally on mock data with `UNLEASHED_MODE=mock python run.py` (set the mode explicitly: a `.env` on the machine may point at the real account). Tests: `python -m pytest -q` (about five minutes; use `-k` for the area you touched, then the whole suite before finishing). All tests are in `tests/test_unleashed.py` and use `make_app` with the mock Unleashed server.
- Never write to the live Unleashed account and never print, stage or commit `.env` or a credential. Anything not proven against the live API is said to be untested live.
- New routes go in `_PAGE_OF` in `dashboard.py` so they belong to a tab (a test enforces it). New database columns go in `schema.sql` and in the code that adds columns to existing databases (`ensure_columns` in `app.py`, `NEW_COLUMNS` in `maintenance_store.py`), for both SQLite and PostgreSQL.
- Pages are server-rendered Jinja with a little JavaScript in `static/dash.js`; forms must work at phone width with no sideways scrolling. Write page text plainly, for the people who run the shop.
- Update `README.md` when behaviour changes. Commit only when asked.

## Report
What you changed (`file:line`), how you checked it (tests run and their result; what you looked at in a browser), and what is still untested or undecided.
