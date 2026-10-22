-- Unleashed Access local database (SQLite). Business data lives in Unleashed;
-- this only stores the log of API calls the dashboard makes.

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
