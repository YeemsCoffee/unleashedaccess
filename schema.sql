-- Gardena Consumption Tracker schema (SQLite)

CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code           TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    category            TEXT NOT NULL CHECK (category IN ('RAW_INGREDIENT', 'PREMADE')),
    unit_of_measure     TEXT NOT NULL DEFAULT 'jug',
    default_scan_action TEXT NOT NULL DEFAULT 'USE_ONE',
    active              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS counts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_code    TEXT NOT NULL,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    current_count REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    UNIQUE (store_code, item_id)
);

CREATE TABLE IF NOT EXISTS premade_containers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    container_code TEXT NOT NULL UNIQUE,
    item_id        INTEGER NOT NULL REFERENCES items(id),
    store_code     TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('MADE', 'IN_USE', 'FINISHED', 'WASTED')),
    made_at        TEXT,
    in_use_at      TEXT,
    finished_at    TEXT,
    wasted_at      TEXT
);

-- Append-only audit log. Every count change (scans AND manager corrections)
-- writes a row here. action values:
--   USE_ONE, CONTAINER_MADE, CONTAINER_IN_USE, CONTAINER_FINISHED,
--   CONTAINER_WASTED, ADJUSTMENT
CREATE TABLE IF NOT EXISTS scan_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_code    TEXT NOT NULL,
    store_code      TEXT NOT NULL,
    item_id         INTEGER REFERENCES items(id),
    container_id    INTEGER REFERENCES premade_containers(id),
    action          TEXT NOT NULL,
    quantity_delta  REAL NOT NULL DEFAULT 0,
    quantity_before REAL,
    quantity_after  REAL,
    employee_id     INTEGER REFERENCES users(id),
    timestamp       TEXT NOT NULL,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_scan_events_timestamp ON scan_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_scan_events_code ON scan_events(scanned_code, timestamp);

CREATE TABLE IF NOT EXISTS users (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    role   TEXT NOT NULL DEFAULT 'EMPLOYEE' CHECK (role IN ('EMPLOYEE', 'MANAGER', 'ADMIN')),
    active INTEGER NOT NULL DEFAULT 1
);

-- Every call the dashboard makes to the Unleashed API (mock or live).
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
