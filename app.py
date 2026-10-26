"""Unleashed Access — Flask app serving the Unleashed dashboard."""
import hashlib
import os

from flask import Flask

import dashboard
from accounts import UserStore, app_secret
from unleashed.db import Database, database_url_from_env, is_postgres_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(test_config=None):
    app = Flask(__name__)
    env = os.environ.get
    app.config.update(
        # A shared PostgreSQL database (DATABASE_URL, or AWS-style DB_*/RDS_*/DB_SECRET
        # settings); without one, data lives in a SQLite file on this computer.
        DATABASE_URL=database_url_from_env(os.environ),
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
        # Team accounts: the first admin (or, with ADMIN_RESET=1, restoring admin access).
        ADMIN_EMAIL=env("ADMIN_EMAIL", "").strip(),
        ADMIN_PASSWORD=env("ADMIN_PASSWORD", ""),
        ADMIN_NAME=env("ADMIN_NAME", "").strip(),
        ADMIN_RESET=env("ADMIN_RESET", "") == "1",
        # Simpler alternative to accounts: one shared password (ignored once accounts exist).
        DASHBOARD_PASSWORD=env("DASHBOARD_PASSWORD", ""),
        SECRET_KEY=env("SECRET_KEY", ""),
        SESSION_COOKIE_SAMESITE="Lax",  # sign-in cookie isn't sent on other sites' form posts
        # Set SECURE_COOKIES=1 when the dashboard is served over HTTPS.
        SESSION_COOKIE_SECURE=env("SECURE_COOKIES", "") == "1",
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

    db = Database(cfg["DATABASE"])
    viewer = cfg["UNLEASHED_MODE"] == "viewer"
    if viewer:
        db.require_schema()  # created by the syncing instance; viewers may be read-only
    else:
        db.init_schema(os.path.join(BASE_DIR, "schema.sql"))
    app.extensions["db"] = db

    if not cfg["SECRET_KEY"]:
        try:
            cfg["SECRET_KEY"] = app_secret(db)  # generated once, shared by every instance
        except Exception:  # read-only viewer before the key exists
            seed = "unleashedaccess:" + cfg["DASHBOARD_PASSWORD"] + ":" + cfg["DATABASE"]
            cfg["SECRET_KEY"] = hashlib.sha256(seed.encode()).hexdigest()

    users = UserStore(db)
    app.extensions["users"] = users
    cfg["ADMIN_SETUP_NOTE"] = None
    if cfg["ADMIN_EMAIL"] and cfg["ADMIN_PASSWORD"] and not viewer:
        cfg["ADMIN_SETUP_NOTE"] = users.ensure_admin(
            cfg["ADMIN_EMAIL"], cfg["ADMIN_PASSWORD"], name=cfg["ADMIN_NAME"],
            reset=cfg["ADMIN_RESET"])
    try:
        has_accounts = users.count() > 0
    except Exception:  # a viewer on a database without the accounts table yet
        has_accounts = False
    # accounts: everyone has their own sign-in; password: one shared password; open: no sign-in
    cfg["AUTH_MODE"] = "accounts" if has_accounts else (
        "password" if cfg["DASHBOARD_PASSWORD"] else "open")

    dashboard.init_app(app)
    return app
