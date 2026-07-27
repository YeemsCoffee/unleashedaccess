"""SQLite connection handling, schema init, and seed data."""
import datetime
import os
import sqlite3

from flask import current_app, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SEED_ITEMS = [
    # (item_code, display_name, category, unit_of_measure, default_scan_action)
    ("GDN-WHOLE-MILK-JUG", "Whole Milk", "RAW_INGREDIENT", "jug", "USE_ONE"),
    ("GDN-HEAVY-CREAM-JUG", "Heavy Cream", "RAW_INGREDIENT", "jug", "USE_ONE"),
    ("GDN-VANILLA-CREAM", "Vanilla Cream Batch", "PREMADE", "container", "CONTAINER_LIFECYCLE"),
    ("GDN-MILK-BASE", "Milk Base Batch", "PREMADE", "container", "CONTAINER_LIFECYCLE"),
]

SEED_USERS = [
    ("Manager", "MANAGER"),
]


def now():
    """Local wall-clock time as 'YYYY-MM-DD HH:MM:SS' (SQLite-sortable)."""
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def today():
    return datetime.date.today().isoformat()


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE_PATH"])
    return g.db


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(database_path, store_code):
    """Create tables and seed starter items/users if the DB is empty."""
    conn = _connect(database_path)
    try:
        with open(os.path.join(BASE_DIR, "schema.sql")) as f:
            conn.executescript(f.read())
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
            for code, name, category, uom, action in SEED_ITEMS:
                cur = conn.execute(
                    "INSERT INTO items (item_code, display_name, category, unit_of_measure,"
                    " default_scan_action, active) VALUES (?, ?, ?, ?, ?, 1)",
                    (code, name, category, uom, action),
                )
                conn.execute(
                    "INSERT INTO counts (store_code, item_id, current_count, updated_at)"
                    " VALUES (?, ?, 0, ?)",
                    (store_code, cur.lastrowid, now()),
                )
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for name, role in SEED_USERS:
                conn.execute("INSERT INTO users (name, role, active) VALUES (?, ?, 1)", (name, role))
        conn.commit()
    finally:
        conn.close()


def register(app):
    app.teardown_appcontext(close_db)
