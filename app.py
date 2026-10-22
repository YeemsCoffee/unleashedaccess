"""Unleashed Access — Flask app serving the Unleashed dashboard."""
import hashlib
import os

from flask import Flask

import dashboard
from unleashed.db import Database, is_postgres_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(test_config=None):
    app = Flask(__name__)
    env = os.environ.get
    app.config.update(
        # DATABASE_URL (postgresql://...) shares one database between people and machines;
        # without it, data lives in a SQLite file on this computer.
        DATABASE_URL=env("DATABASE_URL", "").strip(),
        DATABASE_PATH=env("DATABASE_PATH", os.path.join(BASE_DIR, "unleashedaccess.db")),
        ALLOW_DATABASE_RESET=env("ALLOW_DATABASE_RESET", "") == "1",
        # live: sync from Unleashed; mock: simulated data; viewer: read a shared database
        # that another instance syncs (no API key needed). Default chosen below.
        UNLEASHED_MODE=(env("UNLEASHED_MODE") or "").strip().lower(),
        UNLEASHED_API_ID=env("UNLEASHED_API_ID", "").strip(),
        UNLEASHED_API_KEY=env("UNLEASHED_API_KEY", "").strip(),
        UNLEASHED_BASE_URL=env("UNLEASHED_BASE_URL", "https://api.unleashedsoftware.com"),
        UNLEASHED_CACHE_SECONDS=int(env("UNLEASHED_CACHE_SECONDS", "120")),
        # background: sync on start, then every SYNC_INTERVAL_MINUTES; blocking: finish the
        # first sync before serving; off: only when "Sync now" is pressed.
        SYNC_ON_STARTUP=env("SYNC_ON_STARTUP", "background").strip().lower(),
        SYNC_INTERVAL_MINUTES=int(env("SYNC_INTERVAL_MINUTES", "15")),
        SYNC_HISTORY_MONTHS=int(env("SYNC_HISTORY_MONTHS", "13")),
        # The business's time zone: decides "today" and month boundaries (e.g. America/Los_Angeles).
        TIMEZONE=env("TIMEZONE", "").strip(),
        # When set, everyone must enter this password to see the dashboard.
        DASHBOARD_PASSWORD=env("DASHBOARD_PASSWORD", ""),
        SECRET_KEY=env("SECRET_KEY", ""),
        SESSION_COOKIE_SAMESITE="Lax",  # sign-in cookie isn't sent on other sites' form posts
    )
    if test_config:
        app.config.update(test_config)

    cfg = app.config
    cfg["DATABASE"] = cfg["DATABASE_URL"] or cfg["DATABASE_PATH"]
    if not cfg["UNLEASHED_MODE"]:
        if cfg["UNLEASHED_API_ID"] and cfg["UNLEASHED_API_KEY"]:
            cfg["UNLEASHED_MODE"] = "live"
        elif is_postgres_url(cfg["DATABASE"]):
            cfg["UNLEASHED_MODE"] = "viewer"
        else:
            cfg["UNLEASHED_MODE"] = "mock"
    if not cfg["SECRET_KEY"]:
        # Stable across restarts and workers; only signs the login cookie.
        seed = "unleashedaccess:" + cfg["DASHBOARD_PASSWORD"] + ":" + cfg["DATABASE"]
        cfg["SECRET_KEY"] = hashlib.sha256(seed.encode()).hexdigest()

    db = Database(cfg["DATABASE"])
    if cfg["UNLEASHED_MODE"] == "viewer":
        db.require_schema()  # created by the syncing instance; viewers may be read-only
    else:
        db.init_schema(os.path.join(BASE_DIR, "schema.sql"))
    app.extensions["db"] = db
    dashboard.init_app(app)
    return app
