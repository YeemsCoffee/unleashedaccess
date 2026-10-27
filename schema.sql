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

-- Production (production_runs.py). Steps are kept here, not in Unleashed, which has none.
-- The process steps for making a product: JSON [{"name": ..., "components": [codes]}].
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
    status          TEXT NOT NULL,      -- recording | closing | closed | failed
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
