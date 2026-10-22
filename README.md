# Gardena Consumption Tracker

A dead-simple barcode/QR scan tracker for the Gardena café store. Employees scan
milk/cream jugs and premade batch labels; counts update automatically and every
change is logged. Not a full inventory system — just scan-driven counts.

**Employee workflow: scan → (maybe tap one big button) → done.**

---

## 1. Recommended architecture

Single small server-rendered web app, one process, one file database:

```
┌─────────────────────────────┐
│  Phone / tablet browser     │
│  (camera scanner via        │
│   html5-qrcode, vendored)   │
└──────────────┬──────────────┘
               │  JSON  (POST /api/scan, /api/container-action, /api/adjust)
┌──────────────▼──────────────┐
│  Flask app (app.py)         │
│  - resolves scanned code    │
│  - applies count change     │
│  - logs ScanEvent           │
│  - serves 4 HTML screens    │
└──────────────┬──────────────┘
               │
        ┌──────▼──────┐
        │  SQLite     │  gardena.db (single file, WAL mode)
        └─────────────┘
```

Why this shape:

- **No native app.** A mobile web page with camera access works on any phone or
  a cheap wall-mounted tablet. Nothing to install.
- **Server is the source of truth.** The scanner just sends the raw code; the
  server decides what it means and what happens. All rules live in one place.
- **Everything is an event.** Counts are only ever changed alongside a row in
  `scan_events` — no silent changes, corrections included.
- **Works offline-at-the-edge.** The scanner JS is vendored (no CDN), so the app
  only needs the phone to reach the local server, not the internet.

## 2. Recommended tech stack

| Layer     | Choice                              | Why |
|-----------|-------------------------------------|-----|
| Backend   | **Python Flask** (~600 lines total) | Simplest of your options for a 4-screen app. No build step, no ORM ceremony. |
| Database  | **SQLite** (WAL mode)               | One store, a handful of scans per minute — SQLite is more than enough and is zero-ops. Schema is plain SQL, so moving to Postgres/Supabase later is a straight port. |
| Frontend  | **Server-rendered Jinja + ~200 lines of vanilla JS** | No framework needed for 4 screens. |
| Scanner   | **html5-qrcode** (vendored in `static/vendor/`) | Reads QR *and* 1D barcodes in-browser on iOS/Android/desktop. |
| QR labels | **segno** (pure-Python)             | Generates printable QR label sheets server-side. |

Explicitly deferred (as you asked): Unleashed integration, forecasting,
multi-store, recipe-based deductions, real auth. The event log means all of
these can be added later without changing how employees scan.

> **Camera note:** browsers only allow camera access on `https://` or on
> `http://localhost`. For store use, either run it on the device (localhost),
> use a tunnel (Tailscale/ngrok), or put it behind any TLS proxy (Caddy makes
> this a 2-line config). The manual-entry box works regardless.

## 3. Database schema

See [`schema.sql`](schema.sql). Matches your data model:

- **items** — `id, item_code, display_name, category (RAW_INGREDIENT|PREMADE), unit_of_measure, default_scan_action, active`
- **counts** — `id, store_code, item_id, current_count, updated_at` (unique per store+item)
- **premade_containers** — `id, container_code, item_id, store_code, status (MADE|IN_USE|FINISHED|WASTED), made_at, in_use_at, finished_at, wasted_at`
- **scan_events** — `id, scanned_code, store_code, item_id?, container_id?, action, quantity_delta, quantity_before, quantity_after, employee_id?, timestamp, note` — the append-only audit log. Corrections are `ADJUSTMENT` events here (no separate table needed).
- **users** — `id, name, role, active`

Count semantics:

- Raw ingredient `current_count` = jugs on hand.
- Premade `current_count` = **active** containers (MADE + IN_USE). `MADE` +1,
  `FINISHED`/`WASTED` −1, `IN_USE` no change.

## 4. Barcode/QR code design

All codes are plain uppercase text, so they work as QR **or** Code-128, and can
be typed by hand if a label is damaged.

| Kind | Pattern | Example |
|------|---------|---------|
| Raw ingredient | `{STORE}-{ITEM}` (the item_code itself) | `GDN-WHOLE-MILK-JUG` |
| Premade container | `{PREMADE-ITEM-CODE}-{YYYYMMDD}-{SEQ}` | `GDN-VANILLA-CREAM-20260727-001` |

Resolution rule (server-side): exact `item_code` match → raw-ingredient scan;
exact `container_code` match → existing container; otherwise longest
`item_code + "-"` prefix match against active PREMADE items → **new** container.
So new premade codes never need pre-registration — printing a label sheet and
scanning a label is enough.

The Admin screen prints QR label sheets (with the code text under each QR) and
auto-suggests the next sequence number for today.

## 5. Exact scan workflows

**Raw ingredient (milk/cream jug):**

1. Scan `GDN-WHOLE-MILK-JUG` → server logs `USE_ONE`, count 30 → 29.
2. Screen shows: **“Whole Milk: used 1 jug. New count: 29.”** Nothing to tap.
3. Double-scan guard: the same code within 10 s (configurable) is ignored and
   reported as a duplicate — prevents accidental double-decrements.

**Premade container label:**

1. First scan of `GDN-VANILLA-CREAM-20260727-001` → container created as
   `MADE`, premade count +1. Screen: **“Vanilla Cream Batch marked MADE.”**
2. Later scans of the same label → three big buttons:
   **Mark In Use / Mark Finished / Mark Wasted**
   (a container already `IN_USE` only shows Finished/Wasted).
3. `IN_USE` → status change only, count unchanged.
   `FINISHED` → count −1. `WASTED` → count −1 and logged as waste.
4. `FINISHED`/`WASTED` are terminal; scanning a dead label just reports its
   status. Managers fix mistakes on the Corrections screen.

**No double-counting:** jug scans are the *only* thing that moves raw
milk/cream counts; premade label scans only move premade counts. (Recipe-based
deductions can be added later as extra `scan_events` triggered by
`CONTAINER_MADE` — the schema already supports it.)

## 6. Page-by-page UI plan

| Page | Route | What's on it |
|------|-------|--------------|
| **Scan** | `/` | Full-width camera viewfinder, employee picker (remembered per device), manual-entry box. Result appears as a big green/amber card; container rescans swap in three huge action buttons. Auto-resumes scanning ~2.5 s after a result. |
| **Daily Summary** | `/summary` | Today: whole milk used, heavy cream used, premade made/finished/wasted; current count per item; recent event log. |
| **Corrections** | `/correct` | Per item: current count → new count + who + why. Saving writes an `ADJUSTMENT` scan_event. No silent changes possible. |
| **Admin** | `/admin` | Add/edit/deactivate items, add employees, set starting counts (also logged as adjustments), print QR label sheets (`/labels`). |

## 7. MVP build plan

1. ✅ Schema + seed data (Whole Milk, Heavy Cream, Vanilla Cream Batch, Milk Base Batch)
2. ✅ Scan resolution engine + `/api/scan` + `/api/container-action` (the core)
3. ✅ Scan screen with camera + manual entry
4. ✅ Daily summary screen
5. ✅ Corrections screen (adjustment events)
6. ✅ Admin screen + printable QR label sheets
7. ✅ Automated tests for the scan engine (`tests/`)
8. Next (later): PIN per employee, CSV export, recipe deductions, Unleashed sync, Postgres/Supabase if multi-store.

## 8. Project structure

```
gardenaconsumption/
├── app.py              # Flask app: pages + JSON API + scan engine glue
├── scan_logic.py       # core rules: code resolution, count math, event logging
├── db.py               # SQLite connection, init, seed
├── schema.sql          # tables (matches the data model above)
├── run.py              # dev entry point (loads .env)
├── dashboard.py        # Unleashed dashboard pages (/unleashed)
├── mock_unleashed.py   # run the mock Unleashed API standalone
├── unleashed/          # client.py, reports.py, mock_server.py, mock_data.py, charts.py
├── requirements.txt
├── templates/          # base, scan, summary, correct, admin, labels
├── static/
│   ├── styles.css
│   ├── scanner.js      # camera + scan UX
│   └── vendor/html5-qrcode.min.js   # vendored — no CDN/internet needed
└── tests/test_scan_logic.py
```

## Unleashed dashboard (`/unleashed`)

A finance/inventory dashboard where **Unleashed is the engine**: every number
is fetched through the Unleashed REST API via one client
(`unleashed/client.py` — signed requests, paging, `/Date()` parsing). Nothing
is read from a local copy.

```
Dashboard pages ──► Reports (unleashed/reports.py) ──► UnleashedClient ──┬─► real api.unleashedsoftware.com  (UNLEASHED_MODE=live)
                                                                         └─► mock Unleashed server            (UNLEASHED_MODE=mock, default)
```

The **mock Unleashed server** (`unleashed/mock_server.py`) implements the same
endpoints, auth and JSON shapes (`/Products`, `/ProductGroups`, `/Warehouses`,
`/Suppliers`, `/Customers`, `/StockOnHand` incl. `asAtDate`, `/PurchaseOrders`,
`/SalesOrders`, `/StockAdjustments`) over ~13 months of simulated Gardena
history: dairy/coffee/syrup/packaging purchase orders with freight, daily POS
sales orders, wholesale/catering orders, and month-end spoilage adjustments.
It's driven by a small weighted-average-cost inventory engine, so the books
reconcile exactly. It also runs standalone: `python mock_unleashed.py` (port 8100).

| Page | Mirrors Unleashed |
|------|-------------------|
| Overview | Running month-to-date COGS, sales, margin, inventory value, open POs, latest transactions |
| COGS | Formula breakdown, daily running COGS chart + table, purchases counted, cross-check, 12-month history |
| Stock on hand | Stock On Hand enquiry (now or as at any date, by group) |
| Valuation | Stock Valuation by product group |
| Stock movement | Stock Movement enquiry per product with running balance, reconciled to stock on hand |
| Purchases | Purchase Enquiry (status/supplier/date) + by supplier, by product, PO detail |
| Sales | Sales Enquiry with cost & margin, by product, by customer, SO detail |
| Adjustments | Stock Adjustment enquiry |
| Transactions | Full transaction history across purchases, sales, adjustments |
| API log | Every API call made (endpoint, query, status, timing, item count) |
| Connection | Mode, credentials status, "Test live connection" button |

Every report has **Export CSV**.

### COGS (running, month to date)

```
COGS = ending inventory value of last month (StockOnHand?asAtDate=<last day of prior month>)
     + purchases received this month incl. shipping (POs by ReceivedDate: line totals + freight)
     − current inventory value (StockOnHand now)
```

The page also cross-checks the result against cost of sales (`AverageLandedPriceAtTimeOfSale`
× qty on completed sales orders) plus stock write-offs, and flags any gap.

### Going live

1. Copy `.env.example` to `.env`, fill `UNLEASHED_API_ID` / `UNLEASHED_API_KEY`
   (Unleashed → Integration → Unleashed API Access), set `UNLEASHED_MODE=live`.
   `.env` is gitignored — keys are never committed.
2. Use **Connection → Test live connection** to confirm.
3. Fields to verify against your live account on first connect (isolated in
   `unleashed/reports.py`): how freight is recorded on POs (read from a PO
   `Costs` array **or** from lines whose product code is `FREIGHT`/`SHIPPING`/
   `DELIVERY`), and stock-adjustment line fields (`QuantityChange`/`ValueChange`).
   The mock follows the published API shapes, but your account's setup decides
   where shipping lands.

Responses are cached for `UNLEASHED_CACHE_SECONDS` (default 120) to respect
Unleashed's rate limits; **Refresh data** clears the cache.

## Running it

```bash
pip install -r requirements.txt
python run.py                 # http://localhost:8000  (dashboard: /unleashed)
```

The database (`gardena.db`) is created and seeded on first run. Config via env
vars: `STORE_CODE` (default `GDN`), `DATABASE_PATH`, `PORT`,
`DUPLICATE_SCAN_SECONDS` (default `10`).

Tests: `python -m pytest tests/ -q` (or `python -m unittest discover tests`).
