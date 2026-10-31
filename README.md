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
  orders, stock adjustments, products, suppliers and customers, every assembly
  and bill of materials, plus the stock snapshots the COGS reports need. This takes a minute or two; the dashboard
  shows progress meanwhile.
- **Every 15 minutes:** it fetches documents modified since the last sync
  (Unleashed `modifiedSince`) and current stock, and re-checks the last 7
  days of stock snapshots, plus any earlier day an edited document touched,
  so backdated changes are caught.
- **Weekly:** a full re-copy, which also drops documents deleted in Unleashed.
- **Sync now** (header) and **Full re-copy** (Settings → Connection) run one on demand.
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

The tabs are Finance, Warehouse, Production and Maintenance, plus Settings for
admins. Which tabs each person opens depends on their access profile (below).

| Tab → page | What it shows |
|------------|---------------|
| Finance → Overview | Running month-to-date COGS, sales, gross margin, inventory value, open purchase orders, latest transactions |
| Finance → COGS | Formula breakdown, daily running COGS chart and table, purchases counted, cross-check, 12-month history |
| Warehouse → Stock on hand | Stock On Hand enquiry, now or as at any date, filterable by product group |
| Warehouse → Valuation | Stock valuation by product group |
| Warehouse → Purchases | Purchase enquiry by status, supplier and date; totals by supplier and product; purchase order detail |
| Warehouse → Sales | Sales enquiry with cost and margin; totals by product and customer; sales order detail |
| Warehouse → Transactions | Full history of purchase receipts, completed production, completed sales and stock adjustments |
| Warehouse → Stock movement | Per-product movements with running balance, reconciled to stock on hand |
| Warehouse → Adjustments | Stock adjustment enquiry |
| Production → Overview | This month's production value, wastage, open orders and their material status, what was made and its unit cost against the recipe |
| Production → Production orders | Assemblies by date, status and product; each order's components, recipe against actually used, and material variance. Start new orders; on open ones, record what's used at each step, then complete them in Unleashed, with yield |
| Production → Material requirements | Open orders' component needs against available stock and incoming purchases; which orders are short |
| Production → Bills of materials | Recipes with cost per unit at today's costs, how many stock can make now, last made, where a component is used |
| Production → Component usage | Components used and wasted, and products made, over any period |
| Maintenance → Overview | Machines down, new requests, open work orders, overdue tasks, tasks due in the next 7 days, what's assigned to you, parts to order; Workload (tasks and work orders for each person) |
| Maintenance → Requests | Problems reported (a machine down, a leak), with priority and status; each request's details, files, and the work order made from it |
| Maintenance → Work orders | Work to do, by priority: assigned, started, completed with findings or cancelled; labor, parts and service costs; files |
| Maintenance → Tasks | Tasks by functional location (a location includes the ones under it) or equipment, filtered by status or person, or found by name; each task's details, checklist, recording work, files and history |
| Maintenance → Task lists | Reusable lists of steps with minutes, ticked off when a task that uses one is recorded |
| Maintenance → Equipment | Machines with make, model, serial number and warranty, where each is installed, its moves, counters and readings, tasks, files and maintenance cost |
| Maintenance → Locations | The functional location tree with codes: find, add, change, copy (templates), delete; each location's master data, what's under it, equipment, open work, tasks and work done |
| Maintenance → Calendar | A month of due dates, the dates each task's schedule brings, open work orders, and work done. Click a day to add a task due then; drag a task's due date to another day to move it (with a mouse or a finger) |
| Maintenance → History | Tasks and work orders done over any dates, at a location or on equipment, by person; labor hours and cost by cost center and by equipment |
| Settings → Connection | Data source, credential status, sync history, "Test live connection" and "Check live data" |
| Settings → Team | Team accounts: add people, give each an access profile (or Admin), reset passwords, deactivate |
| Settings → Access profiles | The kinds of account and the tabs each opens |
| Settings → API log | Every API call made: endpoint, query, status, timing, item count |

**Settings** is only shown to admins (with team accounts on). Every report has
**Export CSV**.

## Access profiles

With team accounts on, every person is either an **Admin**, who opens every tab
and Settings, or has an **access profile**: a kind of account and the tabs it
opens (Finance, Warehouse, Production, Maintenance). Admins create and change
profiles under Settings → Access profiles and give each person one on the Team
page. A page in a tab someone's profile doesn't open isn't shown to them; the home
page opens the first tab they have. Someone with no Finance, Warehouse or
Production doesn't see the Unleashed sync status either.

**Maintenance requests** is a narrower choice than Maintenance: it opens only the
Requests page, so baristas and store staff can report a problem (with a photo) and
follow what happens to it, without seeing or changing the rest. Maintenance
includes it.

The first time this version runs it creates two profiles: **Standard** (Finance,
Warehouse, Production: what every member could see before) for everyone already
on the team, and **Maintenance technician** (Maintenance only). Without team
accounts (a shared password, or none) everyone opens every tab.

## Maintenance

The maintenance scheduler, brought into Core and kept in its database (the
`maint_*` tables). It follows SAP Plant Maintenance's model, kept simple:

- **Functional locations** form a tree (a store, its kitchen, the espresso
  station in it), each with a **code** that nests: `GAR`, `GAR-KIT`,
  `GAR-KIT-ESP`. A child's code starts with its parent's; changing a code renames
  the codes under it. Find one by code or name. A location's tasks include the
  ones in the locations under it.
- **Master data** on each location: site, cost center, work center (who looks
  after it) and criticality (**A** stops the business, **B** slows it down, **C**
  can wait). Left blank, each comes from the location above, so setting the cost
  center on a store covers everything in it; the page says where a value came
  from.
- **Templates** are reference structures (a standard store: bar, kitchen,
  espresso station, with their tasks). Their tasks never come due. **Copy this
  structure** on any location copies it and everything under it to a new place,
  renaming the codes (`TPL-KIT` becomes `TOR-KIT`), optionally with its tasks.
- **Equipment** is separate from where it is: a machine has its own number
  (`EQ-00001`), make, model, serial number, tag, purchase date and cost, and
  warranty. It's installed at a location, taken to storage or retired, and every
  move is kept with its date and note. Its tasks go with it when it moves. It takes
  its cost center and criticality from where it's installed, unless it has its own.
- **Counters** on equipment (shots, hours, gallons) take readings that only go up;
  the usage rate per day comes from the last 180 days of readings.
- **Tasks** repeat every so many days, or every so much use (every 10,000 shots),
  or both, whichever comes first. A usage task's due date is estimated from the
  usage rate. Dates are in the business's time zone (`TIMEZONE`). A task is
  overdue once its date has passed; one being worked on can be marked in progress.
  Each can be assigned to someone who can open Maintenance, use a **task list**,
  and have a part with its vendor, part number and lead time: the part shows under
  **Parts to order** from the date it should be ordered (due date less the lead
  time), a week ahead.
- **Task lists** are the steps for a kind of job (a burr clean: unplug, remove the
  burrs, vacuum, reassemble), each with minutes, kept apart from the tasks that
  use them so one list serves many machines. Recording the work ticks them off.
- **Recording work** (completed, or skipped with a reason) keeps who did it, when,
  how long, labor hours, parts used and their cost, the steps done, the counter
  reading and notes. A time-based task moves on by whole intervals from the date
  it was due to the first one after today, so it keeps its schedule however late
  it was done; a usage-based one counts its next interval from the reading.
- **Requests** (SAP's notifications) are problems reported: what's wrong, which
  equipment or where, whether the machine is down, details and a photo. Priority is
  very high for a machine down, otherwise from the criticality of the equipment or
  place. A request becomes a **work order**, or is closed or rejected with a
  reason.
- **Work orders** (`WO-00001`) are assigned, needed by a date, started and
  completed with findings (which closes their request) or cancelled (which opens
  it again). Labor (hours × rate), parts, services and other costs are added as
  lines while the work order is open.
- **History** shows tasks and work orders done at a location (and under it) or on
  a piece of equipment, with labor hours and cost by cost center and by
  equipment. Each location and piece of equipment shows its own.
- **Files** (PDF, Word, Excel, pictures, text; up to 16 MB) go with tasks,
  equipment, requests and work orders, stored in the database so every server has
  them.
- Anyone who can open Maintenance does all of this. Deleting a task (with its
  history and files), a location, a piece of equipment or a counter is for admins.
  Someone with only Maintenance requests reports problems and adds files to their
  own requests.

## COGS (running, month to date)

```
COGS = last month's ending inventory value   (StockOnHand?asAtDate=<last day of prior month>)
     + purchases received this month          (purchase orders by ReceivedDate: goods + shipping)
     − current inventory value                (StockOnHand now)
```

The COGS page also cross-checks the result against cost of sales (average
landed cost × quantity on completed sales orders) plus stock write-offs, and
flags any gap.

**Stock on hand from one warehouse.** Under **By store → Stores and stock on
hand**, an admin can choose the warehouse whose stock on hand COGS counts (for
example the main warehouse, "Warehouse"), instead of every warehouse. Stock that
leaves it counts as used, wherever it goes. The sync keeps that warehouse's
stock history too, and COGS switches to it once a sync has downloaded it (until
then it counts every warehouse, and the page says so).

**By store.** Stores are Unleashed customers that stock is sent to on sales
orders; the admin ticks which customers are stores in the same place. A store's
COGS is what was sent to it on completed sales orders that month, at
Unleashed's average landed cost when it went out (not the price on the order).
What wasn't sent to a store is shown underneath, so the rows add up to COGS:

```
COGS = sent to each store (cost of completed sales orders to it)
     + other customers (cost of their completed sales orders)
     + stock written off (adjustments in the COGS warehouse)
     + not on an order or write-off (usage not recorded, production losses, …)
```

The last 12 months table gets a column per store, and **Export CSV** in the By
store section downloads the split.

## Production

Production reads Unleashed **assemblies** (production orders) and **bills of
materials** (recipes), organised like an ERP's production module:

| ERP idea | Here | From Unleashed |
|----------|------|----------------|
| Production order | Production orders | Assemblies: Parked = open, Completed = made |
| Recipe / bill of materials | Bills of materials | Bills of materials, costed at today's average costs |
| Material availability check (MRP) | Material requirements | Open assemblies' components against stock on hand, sales allocations and open purchase orders |
| Goods issue / goods receipt | Warehouse → Transactions (type Production) | Completed assemblies: components out, product in |
| Order costing and variances | Production order detail, Component usage | Components used against the recipe × the order quantity |

- **Components used** = each assembly line's quantity plus its wastage.
- **Cost**: from the assembly line's cost when Unleashed gives one; otherwise
  estimated at today's average cost and marked with *. **Check live data**
  reports which cost field your account returns.
- **Material requirements** gives stock to open orders in due-date (Assemble
  by) order, so an order is Ready only if what's left after earlier orders
  covers it. Unleashed allocates a parked assembly's components, so its
  Available figure has already taken them out; that's added back first (never
  more than Unleashed has allocated), so an order isn't short of stock set
  aside for it. A production order's page shows the same figures.
- Assemblies only move value between products, so COGS is unchanged by them.
- If the API user can't read assemblies or bills of materials, everything else
  still syncs, and the Production pages say why they're empty.

### Starting, recording and closing out a production order

Production orders can be started here or in Unleashed. **New production
order** (Production overview, Production orders, or **Make it** on a bill of
materials) takes the product, quantity, assemble-by date, the **From**
warehouse (components are taken from it) and the **To** warehouse (what's made
goes into it), and comments. It shows what the order needs against available
stock in all warehouses (**Check materials**), and creates a parked assembly in
Unleashed (`POST /Assemblies`, with `SourceWarehouse` and
`DestinationWarehouse`), which Unleashed numbers and fills with components from
the bill of materials.

Unleashed fills a parked assembly's component quantities from the bill of
materials. Here, the team records what they **actually** use, step by step, and
closing the order out replaces Unleashed's figures with those.

1. **Operating procedure** (admins, once per product): Bills of materials → a
   bill → **Write the procedure**. Add notes shown at the top of every order
   (equipment, safety, hygiene), then the steps in order. Each step has
   instructions, the components it uses (a component can be used at several
   steps; one ticked nowhere is recorded at the last step), and fields the tech
   fills in: a **number** with a unit and an acceptable range (a weight, a
   temperature), **text** (a lot code), or **yes / no** (a check). Fields can
   be required. A step's number sets its order. **+ Add a step** and **+ Add a
   field** add more as you write; empty ones are left out when you save. Each
   step can show **pictures** (PNG, JPEG, GIF or WebP, up to 10): the browser
   makes phone photos smaller (1600 px) before sending them, and they're kept in
   the database. An order keeps the pictures its procedure had when it started;
   a picture removed from the procedure is deleted once no order being recorded
   shows it.
   Without a procedure, an order is recorded in a single step with every
   component.
2. **Record** (anyone, e.g. production techs): open a parked order under
   Production orders. The procedure is there: notes, then each step's
   instructions. At each step, enter how much of each component was used,
   wastage included, fill in the step's fields, and **Save step**. A number
   outside its range is flagged. A saved step shows who saved it and when, and
   can be updated until the order is completed. An order keeps the procedure it started
   with, so editing the procedure doesn't change orders already underway.
3. **Complete** (anyone): **Complete order** appears at the top and bottom of
   the procedure once every step that records something is saved (steps with
   nothing to record don't hold it up; until then it lists the steps still to
   save). Enter how much was made, which shows the product yield and each
   material's yield (below), and the date, check the totals against
   Unleashed's calculation, then
   **Complete in Unleashed**. Anything recorded outside its range is listed
   there first. For each component, the sum over all steps is
   sent as the quantity used (wastage 0). The quantity made and the assembly
   date are set, and the assembly is completed, which moves the stock.

**Batch-tracked products** (`IsBatchTracked` on the product in Unleashed): the
Complete page also asks for the batches made, one row per batch with its number
(up to 50 characters), quantity and expiry date, adding up to the quantity made.
They go in the same update as the quantities, as the assembly's
`PendingBatchNumbers` (`Number`, `Quantity`, `ExpiryDate`), which Unleashed turns
into batches when the assembly is completed. Core reads them back before
completing; if Unleashed didn't keep them exactly as sent, the quantities stay in
Unleashed but the order is left parked, and its page says to check the batches
in Unleashed and complete it there. The order page always compares the batches
recorded here with the ones Unleashed holds (pending or created), marking any
that are missing or differ.

Unleashed doesn't allow a completed assembly to be edited, so completing reads
the quantities back first. If Unleashed didn't keep them, or refuses to complete
(e.g. not enough stock), or anything else goes wrong, the order stays parked,
the reason shows on the order, and it can be completed again. A completion cut
off part-way (the app restarted) can be retried after 10 minutes.

**Yield** is two separate measures:

- **Material yield**, for each material: what the bill of materials calls for to
  make the quantity actually made (its wastage allowance included) ÷ what was
  used of that material. Under 100% means more was used than the recipe allows.
  It shows per material on the order and on the completion page (as the quantity
  made is typed), and per material over any period in Component usage. It works
  for every completed order with a bill of materials, including ones completed
  in Unleashed.
- **Product yield**, for the finished product: quantity made ÷ quantity
  planned. Unleashed only keeps the quantity made, so the plan is recorded when
  an order is completed here; product yield shows on the order, in Production
  orders (and its CSV), on the Production overview and in Component usage, by
  product and overall.

Both are flagged under 95%. Procedures, step quantities and field entries live in
this app's database (`prod_steps`, `prod_runs`, `prod_step_records`,
`prod_step_entries`); Unleashed only receives the totals. A completed order's
page keeps everything recorded, read-only, as its batch record. Starting and closing out orders needs the instance with the Unleashed API key
(a view-only instance can record steps but not start or complete orders), and
an API key allowed to create and change assemblies.

## The mock Unleashed server

`unleashed/mock_server.py` implements the endpoints the dashboard uses, with
the same request signing (`api-auth-id` / `api-auth-signature`), paging and
JSON shapes as the real API: `/Products`, `/ProductGroups`, `/Warehouses`,
`/Suppliers`, `/Customers`, `/StockOnHand` (including `asAtDate`),
`/PurchaseOrders`, `/SalesOrders`, `/StockAdjustments`, `/Assemblies`,
`/BillOfMaterials`, plus updating and completing a parked assembly (`PUT /Assemblies/{guid}`, `POST /Assemblies/{guid}/Complete`).

Its data (`unleashed/mock_data.py`) is about 13 months of simulated café
history: dairy, coffee, syrup and packaging purchase orders with freight,
daily point-of-sale sales orders, wholesale and catering orders, month-end
spoilage write-offs, and house-made cold brew, sweet cream and matcha
assembled from bills of materials (with some batches wasting more than the
recipe, and open orders that are short of matcha). A small weighted-average-cost inventory engine drives it,
so the books reconcile exactly.

In mock mode the server is also reachable at `/mock-unleashed/...`, or run it
on its own with `python mock_unleashed.py` (port 8100).

## Going live

1. In `.env`, set `UNLEASHED_API_ID` and `UNLEASHED_API_KEY` (Unleashed →
   Integration → Unleashed API Access). `.env` is gitignored, so keys are
   never committed.
2. Restart, open **Settings** → **Connection**, and click **Check live data**. This works
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
   - this month's COGS formula agrees with cost of sales plus write-offs;
   - bills of materials and assemblies can be read, assembly lines list their
     components, and which cost field they carry.

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
`DATABASE_URL`. Any provider works: AWS RDS (see [Deploying on AWS](#deploying-on-aws)),
Supabase, Neon, Render, Railway and others. Use the connection string they give you, e.g.
`postgresql://user:password@host:5432/dbname?sslmode=require`.

There are two ways to share. In both, **only one instance has the Unleashed API
key and syncs**; everyone else just reads.

### Option A (recommended): host the dashboard, share a link

Run one copy of the app on a hosting service next to the database. People
open a web address and sign in with their own account. Nobody else installs anything,
and the API key and database password stay on the server.

1. Create the PostgreSQL database and copy its connection string.
2. Deploy this repository as a web service. Elastic Beanstalk, Render, Railway,
   Heroku and similar read the `Procfile` (`gunicorn run:app`, configured by
   `gunicorn.conf.py`); container services use the `Dockerfile`.
3. Set these environment variables on the service:

   | Variable | Value |
   |----------|-------|
   | `DATABASE_URL` | the PostgreSQL connection string |
   | `UNLEASHED_API_ID`, `UNLEASHED_API_KEY` | your Unleashed API credentials |
   | `ADMIN_EMAIL`, `ADMIN_NAME`, `ADMIN_PASSWORD` | your email, name and a temporary password: you sign in as the first admin, choose your own password, then add your team under **Settings** → **Team** |
   | `TIMEZONE` | `America/Los_Angeles` (servers run on UTC; this sets "today" and month boundaries) |

4. Open the service's address, sign in, choose your password, and wait for the
   first copy to finish. Then add your team under **Settings** → **Team**: each person gets their
   own sign-in.

Run one worker (the default in `gunicorn.conf.py`): the background sync runs
inside the web process. Several workers or servers are still safe, because a
lock in the database lets only one of them sync at a time; they would just repeat
the startup work. Health checks should use `/healthz`, which answers without
signing in.

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

## Deploying on AWS

**Click-by-click walkthrough: [docs/aws-setup.md](docs/aws-setup.md).** The
summary below covers the same ground.

The recommended setup keeps everything in your AWS account:

```
Your team ──HTTPS──► Load balancer ──► Elastic Beanstalk (Python): the app + background sync
                                              │                           │
                                              ▼                           ▼
                                  Amazon RDS for PostgreSQL     api.unleashedsoftware.com
```

The database only holds a **copy** of your Unleashed data. If it's ever lost, a
full sync rebuilds it, so RDS defaults (automated backups) are plenty.

### 1. Create the database (Amazon RDS for PostgreSQL)

1. RDS console → **Create database** → **PostgreSQL** (16, 17 or 18; tested on 16 and 18.3).
2. A small instance is plenty: `db.t4g.micro`, 20 GB storage.
3. Set a master username and password. Letting RDS manage it in Secrets
   Manager also works; see `DB_SECRET` below.
4. Under **Additional configuration**, set **Initial database name** to
   `unleashedaccess`.
5. **Public access: No.** Put it in the same VPC as the app (the default VPC is fine).
6. Note the **endpoint** (e.g. `unleashedaccess.abc123.us-west-2.rds.amazonaws.com`).

Create the database separately rather than with Elastic Beanstalk's "add
database" option; that one is deleted along with the environment. (The app
still reads that option's `RDS_*` settings if you use it.)

### 2. Create the app (Elastic Beanstalk, Python platform)

1. Make a zip of the code: `git archive -o unleashedaccess.zip HEAD`. Your
   `.env` isn't included.
2. Elastic Beanstalk console → **Create application** → web server environment,
   platform **Python 3.12** (Amazon Linux 2023), upload the zip. The `Procfile`,
   `gunicorn.conf.py` and `.ebextensions/` in the zip configure the rest,
   including the `/healthz` health check and `TIMEZONE=America/Los_Angeles`.
3. Choose a **load balanced** environment with **1 instance** (`t4g.small` or
   `t3.small`). The load balancer is what serves HTTPS.
4. Add these **environment properties**:

   | Property | Value |
   |----------|-------|
   | `DB_HOST` | the RDS endpoint |
   | `DB_NAME` | `unleashedaccess` |
   | `DB_USER`, `DB_PASSWORD` | the RDS master username and password (no URL-encoding needed) |
   | `UNLEASHED_API_ID`, `UNLEASHED_API_KEY` | your Unleashed API credentials |
   | `ADMIN_EMAIL`, `ADMIN_NAME`, `ADMIN_PASSWORD` | your email, name and a temporary password for the first admin account |
   | `SECURE_COOKIES` | `1` once HTTPS is set up (step 3) |

5. Let the app reach the database: in the RDS instance's **security group**,
   add an inbound rule for **PostgreSQL (5432)** whose source is the Elastic
   Beanstalk instances' security group.
6. The instances also need outbound internet access to reach Unleashed. That's
   automatic in the default VPC; in private subnets it needs a NAT gateway.

### 3. HTTPS

Request a certificate in **AWS Certificate Manager** for a name like
`dashboard.yourdomain.com`, add an **HTTPS (443) listener** with it to the
environment's load balancer (Configuration → Load balancer), and point that name
at the environment (Route 53 alias or CNAME). Then set `SECURE_COOKIES=1`.
Don't share the dashboard over plain HTTP: passwords would travel unencrypted.

### 4. Open it

Go to the address, sign in, and wait a minute or two for the first copy. Deploy
updates with a new zip, or with the EB CLI (`eb init`, `eb create`, `eb deploy`).

### Other AWS options

- **ECS on Fargate:** build the `Dockerfile` (it listens on port 8000), push
  it to ECR, and run **one task** as a service behind a load balancer with health
  check path `/healthz`. ECS can inject the RDS secret from Secrets Manager as
  `DB_SECRET` (plus `DB_HOST` if the secret holds only the login).
- **App Runner:** works for serving pages, but App Runner limits CPU for idle
  instances, which can stall the background sync. If you use it, also run
  `python sync_once.py` on a schedule (e.g. an ECS scheduled task every 15 minutes).
- **EC2 or Lightsail:** run `gunicorn run:app` (or the Docker image) behind a
  reverse proxy with HTTPS.

Rough monthly cost for the recommended setup (`db.t4g.micro`, one small
instance, a load balancer) is in the tens of dollars; check the AWS Pricing
Calculator for your region.

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
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | port `5432` | The same as separate settings (no URL-encoding); Elastic Beanstalk's `RDS_*` names also work |
| `DB_SECRET` | — | JSON of an RDS secret from Secrets Manager (username, password, and optionally host, port, dbname) |
| `DB_SSLMODE` | `require` | SSL mode when the URL is built from the settings above |
| `DATABASE_PATH` | `unleashedaccess.db` | SQLite file used when `DATABASE_URL` isn't set |
| `ALLOW_DATABASE_RESET` | — | `1` lets this instance replace a shared database holding another account's data |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | — | Creates the first admin account (it must choose a new password at first sign-in); turns on team accounts |
| `ADMIN_RESET` | — | `1` restores admin access for `ADMIN_EMAIL` with `ADMIN_PASSWORD` (for when you're locked out). It applies once per password, so a restart while it's still set doesn't undo the password chosen afterwards |
| `DASHBOARD_PASSWORD` | — | Simpler alternative to accounts: one shared password (ignored once accounts exist) |
| `ALLOW_NO_SIGN_IN` | — | Real Unleashed data is only shown after sign-in; `1` lifts that on a private computer |
| `SECRET_KEY` | generated and stored in the database | Signs the sign-in cookie |
| `TIMEZONE` | the computer's time zone | Business time zone for "today", month boundaries and displayed times |
| `SECURE_COOKIES` | — | `1` when served over HTTPS: the sign-in cookie is only sent over HTTPS, and `http://` visits through the load balancer are redirected to `https://` |
| `WEB_CONCURRENCY`, `GUNICORN_THREADS` | `1`, `8` | gunicorn workers and threads (`gunicorn.conf.py`) |
| `ENV_FILE` | `.env` | Settings file to load instead of `.env` |
| `PORT` | `8000` | Web server port |

## Project structure

```
unleashedaccess/
├── app.py              # Flask app factory
├── dashboard.py        # dashboard pages, sign-in and wiring (mock, live or viewer)
├── accounts.py         # team accounts
├── access.py           # access profiles: the tabs each kind of account opens
├── production_runs.py  # operating procedures (with step pictures) and what's recorded at each step
├── maintenance.py      # the Maintenance tab: overview, tasks, locations, calendar, history
├── maintenance_views.py # Maintenance pages: equipment, counters, files, task lists, requests, work orders
├── maintenance_store.py # maintenance locations, tasks, usage schedules, work recorded; new columns
├── maintenance_assets.py # equipment, its moves, counters and readings, files
├── maintenance_work.py # task lists, requests, work orders and their costs
├── maintenance_common.py # maintenance form parsing, codes, priorities, file types
├── run.py              # entry point (loads .env)
├── envfile.py          # minimal .env loader
├── sync_once.py        # run one sync and exit (scheduled jobs)
├── mock_unleashed.py   # run the mock Unleashed API standalone
├── Procfile            # start command for hosting services: gunicorn run:app
├── gunicorn.conf.py    # port (PORT, default 8000), workers, threads
├── Dockerfile          # container image for ECS/Fargate, App Runner, EB Docker
├── .ebextensions/      # Elastic Beanstalk settings (/healthz health check, TIMEZONE)
├── .platform/          # Elastic Beanstalk nginx: uploads up to 20 MB (maintenance files)
├── schema.sql          # data copy, sync history and lock, API log, accounts, profiles, production and maintenance tables
├── unleashed/
│   ├── client.py       # Unleashed REST client: signing, paging, dates, logging
│   ├── reports.py      # report enquiries + COGS (read through a client: API or store)
│   ├── production.py   # production orders, BOM costing, material requirements, usage
│   ├── assembly_writes.py # create production orders in Unleashed; close them out
│   ├── db.py           # SQLite or PostgreSQL access (same SQL for both)
│   ├── store.py        # the data copy + a client that answers queries from it
│   ├── sync.py         # full/incremental sync from the API + background scheduler
│   ├── mock_server.py  # mock Unleashed API
│   ├── mock_data.py    # simulated dataset + inventory engine
│   └── charts.py       # server-side SVG charts
├── templates/dash/     # page templates
├── templates/maint/    # Maintenance page templates
├── static/             # dash.css, dash.js
└── tests/test_unleashed.py
```
