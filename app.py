"""Unleashed Access — Flask app serving the Unleashed dashboard."""
import os
import sqlite3

from flask import Flask

import dashboard

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "unleashedaccess.db")),
        UNLEASHED_MODE=os.environ.get("UNLEASHED_MODE", "mock").lower(),
        UNLEASHED_API_ID=os.environ.get("UNLEASHED_API_ID", ""),
        UNLEASHED_API_KEY=os.environ.get("UNLEASHED_API_KEY", ""),
        UNLEASHED_BASE_URL=os.environ.get("UNLEASHED_BASE_URL", "https://api.unleashedsoftware.com"),
        UNLEASHED_CACHE_SECONDS=int(os.environ.get("UNLEASHED_CACHE_SECONDS", "120")),
    )
    if test_config:
        app.config.update(test_config)

    init_db(app.config["DATABASE_PATH"])
    dashboard.init_app(app)
    return app


def init_db(path):
    conn = sqlite3.connect(path)
    try:
        with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
    finally:
        conn.close()
