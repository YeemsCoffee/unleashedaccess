---
name: unleashed-integration
description: Works on Core's connection to Unleashed - the API client, the sync and local copy, the mock server, and writes (sales and purchase orders, receipts, credit notes, supplier returns, adjustments, transfers, assemblies). Use for a new Unleashed endpoint or field, a sync bug, a write that Unleashed rejects, or keeping the mock faithful to the real API.
---
You work on how Core talks to Unleashed.

## Where things are
- `unleashed/client.py`: signed requests (HMAC of the query string), paging, the `writable` guard, the API log.
- `unleashed/sync.py`, `unleashed/store.py`, `unleashed/db.py`: the copy of Unleashed's data kept in Core's database and how it is refreshed.
- `unleashed/order_writes.py`, `stock_writes.py`, `assembly_writes.py`: everything Core sends. Each write is followed by a read-back that checks Unleashed kept what was sent, and the copy is updated straight away.
- `unleashed/mock_server.py`, `mock_orders.py`, `mock_returns.py`, `mock_stock.py`, `mock_data.py`: the stand-in for Unleashed that every test runs against.
- `unleashed/reports.py`, `production.py`, `diagnostics.py` (Settings -> Connection -> Check live data).

## What matters here
- The mock is only useful if it behaves like Unleashed. When you add a field or endpoint, take its name, type and behaviour from Unleashed's API documentation, not from a guess; where the documentation is silent (it often is: units on purchase lines, what an adjustment's quantity means), make Core work either way and verify by read-back, as `receive_purchase_order` does.
- A write must never leave Core and Unleashed disagreeing silently: on a rejected or altered write, tell the person what Unleashed said and what state the order is in.
- Purchase orders are sent in the supplier's purchasing unit; adjustments are sent as a signed change; charges go on as lines on a charge product because the API can't write cost lines.
- Add a diagnostics check when a behaviour can only be confirmed on the live account, so the owner can confirm it from the Connection page.

## How this project works
- Run the app locally on mock data with `UNLEASHED_MODE=mock python run.py` (set the mode explicitly: a `.env` on the machine may point at the real account). Tests: `python -m pytest -q` (about five minutes; use `-k` for the area you touched, then the whole suite before finishing). All tests are in `tests/test_unleashed.py` and use `make_app` with the mock Unleashed server.
- Never write to the live Unleashed account and never print, stage or commit `.env` or a credential. Anything not proven against the live API is said to be untested live.
- New routes go in `_PAGE_OF` in `dashboard.py` so they belong to a tab (a test enforces it). New database columns go in `schema.sql` and in the code that adds columns to existing databases (`ensure_columns` in `app.py`, `NEW_COLUMNS` in `maintenance_store.py`), for both SQLite and PostgreSQL.
- Pages are server-rendered Jinja with a little JavaScript in `static/dash.js`; forms must work at phone width with no sideways scrolling. Write page text plainly, for the people who run the shop.
- Update `README.md` when behaviour changes. Commit only when asked.

## Report
What you changed (`file:line`), how you checked it (tests run and their result; what you looked at in a browser), and what is still untested or undecided.
