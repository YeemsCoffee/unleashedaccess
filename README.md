# Unleashed Access

A finance and inventory dashboard where **Unleashed is the engine**: every
number on every page is fetched through the Unleashed REST API. Nothing is
read from a local copy of the data.

It includes a **mock Unleashed server**, so the whole dashboard works before
it's connected to a live account. Switching to real data is one setting.

```
Dashboard pages ──► Reports (unleashed/reports.py) ──► UnleashedClient ──┬─► api.unleashedsoftware.com   (UNLEASHED_MODE=live, default when credentials are set)
                                                                         └─► mock Unleashed server        (UNLEASHED_MODE=mock, default without credentials)
```

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: add API credentials
python run.py                 # http://localhost:8000
```

Tests: `python -m pytest tests/ -q`

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

In live mode, the daily COGS chart's stock snapshots are fetched four at a
time, and rate-limited responses (HTTP 429/503) are retried with backoff.
Responses are cached: queries pinned to a past date for an hour, everything
else for `UNLEASHED_CACHE_SECONDS` (default 120). **Refresh data** clears the
cache. Expect the first load of the Overview and COGS pages to take a few
seconds; later views are instant.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNLEASHED_MODE` | `live` if credentials are set, else `mock` | `live` or `mock` |
| `UNLEASHED_API_ID` / `UNLEASHED_API_KEY` | — | Unleashed API credentials |
| `UNLEASHED_BASE_URL` | `https://api.unleashedsoftware.com` | API host |
| `UNLEASHED_CACHE_SECONDS` | `120` | Response cache lifetime |
| `DATABASE_PATH` | `unleashedaccess.db` | SQLite file for the API call log |
| `PORT` | `8000` | Web server port |

## Project structure

```
unleashedaccess/
├── app.py              # Flask app factory
├── dashboard.py        # dashboard pages and wiring (mock or live client)
├── run.py              # entry point (loads .env)
├── envfile.py          # minimal .env loader
├── mock_unleashed.py   # run the mock Unleashed API standalone
├── schema.sql          # API call log table
├── unleashed/
│   ├── client.py       # Unleashed REST client: signing, paging, dates, logging
│   ├── reports.py      # report enquiries + COGS, all via the client
│   ├── mock_server.py  # mock Unleashed API
│   ├── mock_data.py    # simulated dataset + inventory engine
│   └── charts.py       # server-side SVG charts
├── templates/dash/     # page templates
├── static/             # dash.css, dash.js
└── tests/test_unleashed.py
```
