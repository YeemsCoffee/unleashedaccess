-- Unleashed Access database (SQLite or PostgreSQL). Business data lives in Unleashed;
-- this holds a copy of it, the API call log, team accounts and production records.

CREATE TABLE IF NOT EXISTS unleashed_api_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    mode        TEXT NOT NULL,
    method      TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    query       TEXT,
    status_code INTEGER,
    duration_ms INTEGER,
    item_count  INTEGER,
    error       TEXT
);

-- Local copy of Unleashed data (filled by unleashed/sync.py; raw API JSON).
CREATE TABLE IF NOT EXISTS ul_records (
    kind TEXT NOT NULL,
    key  TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS ul_documents (
    kind           TEXT NOT NULL,   -- PurchaseOrders | SalesOrders | StockAdjustments
    guid           TEXT NOT NULL,
    number         TEXT,
    status         TEXT,
    doc_date       TEXT,            -- order/adjustment date (ISO)
    party_code     TEXT,            -- supplier or customer code
    effective_date TEXT,            -- when it moved stock (received/completed date)
    data           TEXT NOT NULL,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (kind, guid)
);
CREATE INDEX IF NOT EXISTS idx_ul_documents_date ON ul_documents(kind, doc_date);
CREATE INDEX IF NOT EXISTS idx_ul_documents_number ON ul_documents(kind, number);

CREATE TABLE IF NOT EXISTS ul_stock_snapshots (
    as_at      TEXT PRIMARY KEY,    -- ISO date (end of day), or 'current'
    data       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ul_sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- full | incremental
    status      TEXT NOT NULL,      -- running | ok | failed
    step        TEXT,
    detail      TEXT,
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS ul_sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row: which app instance is syncing, so instances sharing a database
-- never sync at the same time. Times are UTC.
CREATE TABLE IF NOT EXISTS ul_sync_lock (
    id         INTEGER PRIMARY KEY,
    owner      TEXT,
    expires_at TEXT
);
INSERT INTO ul_sync_lock (id, owner, expires_at) VALUES (1, NULL, NULL) ON CONFLICT (id) DO NOTHING;

-- Team accounts (accounts.py). Times are UTC.
CREATE TABLE IF NOT EXISTS app_users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    email                TEXT NOT NULL UNIQUE,   -- lowercase
    name                 TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL,          -- admin | member
    active               INTEGER NOT NULL,
    must_change_password INTEGER NOT NULL,
    session_version      INTEGER NOT NULL,       -- bumped to sign the person out everywhere
    failed_sign_ins      INTEGER NOT NULL,
    locked_until         TEXT,
    last_sign_in_at      TEXT,
    created_at           TEXT NOT NULL
);

-- App-wide settings, e.g. the generated key that signs sign-in cookies.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Production (production_runs.py). Procedures are kept here, not in Unleashed, which has none.
-- The operating procedure for making a product: JSON {"notes", "steps": [{"name",
-- "instructions", "components": [codes], "fields": [...]}]} (older rows: just the steps).
CREATE TABLE IF NOT EXISTS prod_steps (
    product_code TEXT PRIMARY KEY,
    steps        TEXT NOT NULL,
    updated_by   TEXT,
    updated_at   TEXT NOT NULL
);

-- A production order (Unleashed assembly) the team is recording, with the steps it
-- started with, and how its close-out to Unleashed went.
CREATE TABLE IF NOT EXISTS prod_runs (
    assembly_guid   TEXT PRIMARY KEY,
    assembly_number TEXT NOT NULL,
    steps           TEXT NOT NULL,
    status          TEXT NOT NULL,      -- recording | closing | closed | failed | sent
    error           TEXT,
    sent            TEXT,               -- JSON: what close-out sent to Unleashed
    closed_by       TEXT,
    closed_at       TEXT,
    updated_at      TEXT NOT NULL
);

-- What was used at each step: JSON {component code: quantity}.
CREATE TABLE IF NOT EXISTS prod_step_records (
    assembly_guid TEXT NOT NULL,
    step_index    INTEGER NOT NULL,
    quantities    TEXT NOT NULL,
    recorded_by   TEXT,
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY (assembly_guid, step_index)
);

-- What was entered in the procedure's fields at each step: JSON {field id: value}.
CREATE TABLE IF NOT EXISTS prod_step_entries (
    assembly_guid TEXT NOT NULL,
    step_index    INTEGER NOT NULL,
    entries       TEXT NOT NULL,
    PRIMARY KEY (assembly_guid, step_index)
);

-- Access profiles (access.py): the tabs each kind of account opens. Admins open everything.
-- areas: JSON list of finance | warehouse | production | maintenance.
CREATE TABLE IF NOT EXISTS access_profiles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    areas      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The profile each (non-admin) account has.
CREATE TABLE IF NOT EXISTS app_user_profiles (
    user_id    INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL
);

-- Maintenance (maintenance.py). Dates are the business's calendar dates (YYYY-MM-DD);
-- times are UTC.
-- Functional locations: a tree (parent_id) of the places maintenance is done. A code
-- under a coded parent starts with the parent's code (GAR, GAR-KIT, GAR-KIT-ESP). Site,
-- cost center, work center and criticality left blank come from the location above. A
-- template (and everything under it) is a reference structure to copy: its tasks never
-- come due. Columns added after the first version are also added by maintenance_store.migrate.
CREATE TABLE IF NOT EXISTS maint_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    parent_id   INTEGER,
    created_at  TEXT NOT NULL,
    code        TEXT,
    site        TEXT,
    cost_center TEXT,
    work_center TEXT,
    criticality TEXT,                  -- A | B | C
    is_template INTEGER
);

-- Recurring maintenance tasks (maintenance plans): due every frequency_days (0: not by
-- time), next on next_due, and/or every usage_interval on an equipment counter, counted
-- from last_reading. A task on a piece of equipment follows it when it moves.
CREATE TABLE IF NOT EXISTS maint_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    location_id        INTEGER,
    frequency_days     INTEGER NOT NULL,
    next_due           TEXT NOT NULL,
    assigned_to        INTEGER,            -- app_users.id
    in_progress        INTEGER NOT NULL,
    part_name          TEXT,
    vendor             TEXT,
    vendor_part_number TEXT,
    lead_time_days     INTEGER NOT NULL,
    instructions       TEXT,
    last_completed     TEXT,
    created_by         TEXT,
    created_at         TEXT NOT NULL,
    equipment_id       INTEGER,
    tasklist_id        INTEGER,
    counter_id         INTEGER,
    usage_interval     REAL,
    last_reading       REAL
);

-- Each time a task was done (or skipped), which moved it on to its next due date.
CREATE TABLE IF NOT EXISTS maint_completions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL,
    status           TEXT NOT NULL,        -- completed | skipped
    scheduled_date   TEXT NOT NULL,
    completed_at     TEXT NOT NULL,
    completed_by     TEXT NOT NULL,
    completed_by_id  INTEGER,
    notes            TEXT,
    duration_minutes INTEGER,
    labor_hours      REAL,
    parts_used       TEXT,
    parts_cost       REAL,
    reading          REAL,                 -- the counter reading when it was done
    checklist        TEXT                  -- JSON [{"text", "done"}]: its task list's steps
);

-- Files kept with a task (SOPs, job aids, photos), stored here so every server has them.
CREATE TABLE IF NOT EXISTS maint_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    data         BLOB NOT NULL,
    uploaded_by  TEXT,
    uploaded_at  TEXT NOT NULL
);

-- Equipment: individual machines (make, model, serial), installed at a functional
-- location, in storage, or retired; maint_equipment_moves is where each has been.
CREATE TABLE IF NOT EXISTS maint_equipment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    category       TEXT,
    tag            TEXT,                -- the asset tag on the machine
    manufacturer   TEXT,
    model          TEXT,
    serial_number  TEXT,
    purchase_date  TEXT,
    purchase_cost  REAL,
    warranty_until TEXT,
    criticality    TEXT,
    status         TEXT NOT NULL,       -- installed | storage | retired
    location_id    INTEGER,
    notes          TEXT,
    created_by     TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maint_equipment_moves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    action       TEXT NOT NULL,         -- installed | dismantled | retired
    location_id  INTEGER,
    on_date      TEXT NOT NULL,
    note         TEXT,
    by_name      TEXT,
    recorded_at  TEXT NOT NULL
);

-- Counters (measuring points) on equipment, e.g. shots pulled, and their readings, for
-- tasks done by usage.
CREATE TABLE IF NOT EXISTS maint_counters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    name         TEXT NOT NULL,
    unit         TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maint_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    counter_id  INTEGER NOT NULL,
    value       REAL NOT NULL,
    read_on     TEXT NOT NULL,
    note        TEXT,
    by_name     TEXT,
    recorded_at TEXT NOT NULL
);

-- Task lists: reusable steps (operations) for doing a task, JSON [{"text", "minutes"}].
CREATE TABLE IF NOT EXISTS maint_tasklists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    operations  TEXT NOT NULL,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL
);

-- Requests (SAP notifications): a problem reported, e.g. a machine down.
CREATE TABLE IF NOT EXISTS maint_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    description    TEXT,
    location_id    INTEGER,
    equipment_id   INTEGER,
    priority       INTEGER NOT NULL,    -- 1 very high .. 4 low
    breakdown      INTEGER NOT NULL,    -- 1: not working at all
    status         TEXT NOT NULL,       -- open | ordered | closed | rejected
    order_id       INTEGER,
    reported_by    TEXT,
    reported_by_id INTEGER,
    reported_at    TEXT NOT NULL,
    closed_by      TEXT,
    closed_at      TEXT,
    resolution     TEXT
);

-- Work orders: the job of fixing something, with its costs in maint_order_costs.
CREATE TABLE IF NOT EXISTS maint_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    description  TEXT,
    request_id   INTEGER,
    location_id  INTEGER,
    equipment_id INTEGER,
    assigned_to  INTEGER,
    priority     INTEGER NOT NULL,
    status       TEXT NOT NULL,         -- open | in_progress | completed | cancelled
    due_date     TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    completed_by TEXT,
    findings     TEXT
);

CREATE TABLE IF NOT EXISTS maint_order_costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- labor | part | service | other
    description TEXT NOT NULL,
    quantity    REAL NOT NULL,          -- hours for labor
    unit_cost   REAL NOT NULL,
    added_by    TEXT,
    added_at    TEXT NOT NULL
);

-- Files kept with equipment (manuals), requests and work orders (photos).
CREATE TABLE IF NOT EXISTS maint_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type  TEXT NOT NULL,         -- equipment | request | order
    object_id    INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    data         BLOB NOT NULL,
    uploaded_by  TEXT,
    uploaded_at  TEXT NOT NULL
)
