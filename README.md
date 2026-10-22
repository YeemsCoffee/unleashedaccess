# Unleashed Access

A finance and inventory dashboard where **Unleashed is the engine**: every
number comes from your Unleashed account through its REST API.

To keep pages fast, the app keeps a **copy** of your Unleashed data in a
database and pages read from that copy (about 0.1 s per page instead of seconds
of API calls). The database is either a SQLite file on your computer (the
default) or a **shared PostgreSQL database**, so several people can use the same
data (see [Sharing with others](#sharing-with-others)). A background sync keeps the copy current:

```
                       background sync (every 15 min: only what changed; weekly: everything)
Unleashed API ──────────────────────────────────────────────► database copy ──────► Reports ──► Dashboard pages
(api.unleashedsoftware.com, or the mock server)                (SQLite or PostgreSQL)  (reports.py)
```

- **First start:** the app copies 13 months of purchase orders, sales
  orders, stock adjustments, products, suppliers and customers, plus the stock
  snapshots the COGS reports need. This takes a minute or two; the dashboard
  shows progress meanwhile.
- **Every 15 minutes:** it fetches documents modified since the last sync
  (Unleashed `modifiedSince`) and current stock, and re-checks the last 7
  days of stock snapshots, plus any earlier day an edited document touched,
  so backdated changes are caught.
- **Weekly:** a full re-copy, which also drops documents deleted in Unleashed.
- **Sync now** (header) and **Full re-copy** (Connection page) run one on demand.
- Date ranges older than the copy fall back to the live API automatically.

It includes a **mock Unleashed server**, so the whole dashboard works before
it's connected to a live account.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: add API credentials
python run.py                 # http://localhost:8000
```

Tests: `python -m pytest tests/ -q` (SQLite). To run them against PostgreSQL,
set `TEST_DATABASE_URL` to an **empty, throwaway** database; the tests drop the
app's tables in it.

## Pages

| Page | What it shows |
|------|---------------|
| Overview | Running month-to-date COGS, sales, gross margin, inventory value, open purchase orders, latest transactions |
| COGS | Formula breakdown, daily running COGS chart and table, purchases counted, cross-check, 12-month history |
| Stock on hand | Stock On Hand enquiry, now or as at any date, filterable by product group |
| Valuation | Stock valuation by product group |
| Stock movement | Per-product movements with running balance, reconciled to stock on hand |
| Purchases | Purchase enquiry by status, supplier and date; totals by supplier and product; purchase order detail |
| Sales | Sales enquiry with cost and margin; totals by product and customer; sales order detail |
| Adjustments | Stock adjustment enquiry |
| Transactions | Full history of purchase receipts, completed sales and stock adjustments |
| API log | Every API call made: endpoint, query, status, timing, item count |
| Connection | Data source, credential status, and a "Test live connection" button |

Every report has **Export CSV**.

## COGS (running, month to date)

```
COGS = last month's ending inventory value   (StockOnHand?asAtDate=<last day of prior month>)
     + purchases received this month          (purchase orders by ReceivedDate: goods + shipping)
     − current inventory value                (StockOnHand now)
```

The COGS page also cross-checks the result against cost of sales (average
landed cost × quantity on completed sales orders) plus stock write-offs, and
flags any gap.

## The mock Unleashed server

`unleashed/mock_server.py` implements the endpoints the dashboard uses, with
the same request signing (`api-auth-id` / `api-auth-signature`), paging and
JSON shapes as the real API: `/Products`, `/ProductGroups`, `/Warehouses`,
`/Suppliers`, `/Customers`, `/StockOnHand` (including `asAtDate`),
`/PurchaseOrders`, `/SalesOrders`, `/StockAdjustments`.

Its data (`unleashed/mock_data.py`) is about 13 months of simulated café
history: dairy, coffee, syrup and packaging purchase orders with freight,
daily point-of-sale sales orders, wholesale and catering orders, and month-end
spoilage write-offs. A small weighted-average-cost inventory engine drives it,
so the books reconcile exactly.

In mock mode the server is also reachable at `/mock-unleashed/...`, or run it
on its own with `python mock_unleashed.py` (port 8100).

## Going live

1. In `.env`, set `UNLEASHED_API_ID` and `UNLEASHED_API_KEY` (Unleashed →
   Integration → Unleashed API Access). `.env` is gitignored, so keys are
   never committed.
2. Restart, open **Connection**, and click **Check live data**. This works
   even while the dashboard still shows mock data. It confirms against your
   real account that:
   - historical stock (`asAtDate`) works, which opening inventory depends on;
   - received purchase orders carry a received date;
   - shipping is found in each purchase order's **cost lines** (additional
     costs), with a spot-check PO to compare against Unleashed. Lines coded
     `FREIGHT`, `SHIPPING` or `DELIVERY` also count. If the PO list leaves
     cost lines out, the dashboard fetches each PO's detail instead;
   - sales order lines carry their cost (`AverageLandedPriceAtTimeOfSale`);
   - stock adjustment lines carry quantity and value changes, broken down by
     adjustment status. Deleted and parked adjustments never moved stock and
     are left out of every report;
   - this month's COGS formula agrees with cost of sales plus write-offs.

   It also lists the field names your account returns (names only, no
   values) so a mismatch can be fixed quickly.
3. Set `UNLEASHED_MODE=live` in `.env` and restart. The header badge changes
   to "Live Unleashed API".

On first start in live mode the dashboard copies your Unleashed data (a minute
or two; progress is shown), then pages load instantly. The sync fetches stock
snapshots four at a time and retries rate-limited responses (HTTP 429/503) with
backoff. With a local SQLite file, switching accounts (or between mock and
live) starts a fresh copy automatically, so data from different sources never
mixes. A shared database is never replaced automatically (see below).

## Sharing with others

Put the data in a hosted **PostgreSQL** database and point the app at it with
`DATABASE_URL`. Any provider works: Supabase, Neon, Render, Railway, AWS RDS and
others. Use the connection string they give you, e.g.
`postgresql://user:password@host:5432/dbname?sslmode=require`.

There are two ways to share. In both, **only one instance has the Unleashed API
key and syncs**; everyone else just reads.

### Option A (recommended): host the dashboard, share a link

Run one copy of the app on a hosting service next to the database. People
open a web address and sign in with a password. Nobody else installs anything,
and the API key and database password stay on the server.

1. Create the PostgreSQL database and copy its connection string.
2. Deploy this repository as a web service (Render, Railway, Fly.io, Heroku and
   similar read the `Procfile`; the start command is
   `gunicorn run:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`).
3. Set these environment variables on the service:

   | Variable | Value |
   |----------|-------|
   | `DATABASE_URL` | the PostgreSQL connection string |
   | `UNLEASHED_API_ID`, `UNLEASHED_API_KEY` | your Unleashed API credentials |
   | `DASHBOARD_PASSWORD` | a strong password to share with your team |
   | `TIMEZONE` | `America/Los_Angeles` (servers run on UTC; this sets "today" and month boundaries) |

4. Open the service's address, sign in, and wait for the first copy to finish.

Keep `--workers 1`: the background sync runs inside the web process. (Several
workers or servers are still safe, because a lock in the database lets only one
of them sync at a time; they would just repeat the startup work.)

### Option B: everyone runs the app, sharing the database

1. On **one** machine, the one that syncs, put `DATABASE_URL` and the Unleashed
   API ID and key in `.env`, then run `python run.py`. It creates the tables and
   keeps the shared copy current.
2. Everyone else clones the repository and puts **only** `DATABASE_URL` in their
   `.env` (no API key). Their app starts in **view-only** mode and reads the
   shared copy.

For view-only people, create a read-only database login rather than sharing the
main password (run once, as the database owner):

```sql
CREATE ROLE dashboard_viewer LOGIN PASSWORD 'choose-a-password';
GRANT USAGE ON SCHEMA public TO dashboard_viewer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_viewer;
```

Start the syncing instance at least once **before** running that `GRANT`, so the
tables exist.

### Safety rules for a shared database

- Only one instance syncs at a time, coordinated through the database. A
  crashed sync's lock expires after 15 minutes.
- An instance configured for a different Unleashed account (or mock data) will
  **not** overwrite a shared database. It shows a warning and stays read-only. To
  deliberately replace the data, start it once with `ALLOW_DATABASE_RESET=1`.
- View-only instances never write to the database and don't need the API key.
- Times are stored in UTC and shown in `TIMEZONE` (or the computer's own time zone).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNLEASHED_MODE` | `live` with credentials; `viewer` with a PostgreSQL `DATABASE_URL` and no credentials; else `mock` | `live`, `viewer` (read a shared copy, no syncing) or `mock` |
| `UNLEASHED_API_ID` / `UNLEASHED_API_KEY` | — | Unleashed API credentials |
| `UNLEASHED_BASE_URL` | `https://api.unleashedsoftware.com` | API host |
| `SYNC_INTERVAL_MINUTES` | `15` | How often the local copy updates (0 = only on "Sync now") |
| `SYNC_HISTORY_MONTHS` | `13` | How far back the local copy goes |
| `SYNC_ON_STARTUP` | `background` | `background`, `blocking` (finish first sync before serving) or `off` |
| `UNLEASHED_CACHE_SECONDS` | `120` | In-memory API response cache, used by the sync and live lookups |
| `DATABASE_URL` | — | PostgreSQL connection string for a shared database |
| `DATABASE_PATH` | `unleashedaccess.db` | SQLite file used when `DATABASE_URL` isn't set |
| `ALLOW_DATABASE_RESET` | — | `1` lets this instance replace a shared database holding another account's data |
| `DASHBOARD_PASSWORD` | — | When set, everyone signs in with this password |
| `SECRET_KEY` | derived from the password | Signs the sign-in cookie; set your own when hosting |
| `TIMEZONE` | the computer's time zone | Business time zone for "today", month boundaries and displayed times |
| `PORT` | `8000` | Web server port |

## Project structure

```
unleashedaccess/
├── app.py              # Flask app factory
├── dashboard.py        # dashboard pages, sign-in and wiring (mock, live or viewer)
├── run.py              # entry point (loads .env)
├── envfile.py          # minimal .env loader
├── mock_unleashed.py   # run the mock Unleashed API standalone
├── Procfile            # start command for hosting services (gunicorn)
├── schema.sql          # data copy, sync history, sync lock and API log tables
├── unleashed/
│   ├── client.py       # Unleashed REST client: signing, paging, dates, logging
│   ├── reports.py      # report enquiries + COGS (read through a client: API or store)
│   ├── db.py           # SQLite or PostgreSQL access (same SQL for both)
│   ├── store.py        # the data copy + a client that answers queries from it
│   ├── sync.py         # full/incremental sync from the API + background scheduler
│   ├── mock_server.py  # mock Unleashed API
│   ├── mock_data.py    # simulated dataset + inventory engine
│   └── charts.py       # server-side SVG charts
├── templates/dash/     # page templates
├── static/             # dash.css, dash.js
└── tests/test_unleashed.py
```
