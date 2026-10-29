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
-- Functional locations: a tree (parent_id) organising the equipment and its tasks.
CREATE TABLE IF NOT EXISTS maint_locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    parent_id   INTEGER,
    created_at  TEXT NOT NULL
);

-- Recurring maintenance tasks: due every frequency_days, next on next_due.
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
    created_at         TEXT NOT NULL
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
    parts_used       TEXT
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
)
