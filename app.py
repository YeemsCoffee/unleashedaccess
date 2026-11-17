"""Yeems Core: Flask app serving the Unleashed dashboard, production and maintenance."""
import hashlib
import os

from flask import Flask, redirect, request, render_template_string

import dashboard
import maintenance
from access import Profiles
from accounts import AccountError, UserStore, app_secret
from maintenance_store import migrate as migrate_maintenance
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
        ADMIN_PASSWORD=env("ADMIN_PASSWORD", "").strip(),  # a space pasted after it isn't part of it
        ADMIN_NAME=env("ADMIN_NAME", "").strip(),
        ADMIN_RESET=env("ADMIN_RESET", "") == "1",
        # Simpler alternative to accounts: one shared password (ignored once accounts exist).
        DASHBOARD_PASSWORD=env("DASHBOARD_PASSWORD", ""),
        # Real Unleashed data is only shown to signed-in people. ALLOW_NO_SIGN_IN=1 lifts
        # that for a private computer nobody else can reach.
        ALLOW_NO_SIGN_IN=env("ALLOW_NO_SIGN_IN", "") == "1",
        SECRET_KEY=env("SECRET_KEY", ""),
        SESSION_COOKIE_SAMESITE="Lax",  # sign-in cookie isn't sent on other sites' form posts
        # Set SECURE_COOKIES=1 when the dashboard is served over HTTPS.
        SESSION_COOKIE_SECURE=env("SECURE_COOKIES", "") == "1",
        # Email (purchase orders to vendors): the SMTP server Core sends through, and who the
        # mail is from. Without SMTP_HOST and MAIL_FROM nothing is sent. SMTP_SECURITY: starttls
        # (port 587, the default), ssl (465) or none.
        SMTP_HOST=env("SMTP_HOST", "").strip(), SMTP_PORT=env("SMTP_PORT", "").strip(),
        SMTP_USER=env("SMTP_USER", "").strip(), SMTP_PASSWORD=env("SMTP_PASSWORD", ""),
        SMTP_SECURITY=env("SMTP_SECURITY", "starttls").strip().lower(),
        MAIL_FROM=env("MAIL_FROM", "").strip(),
        # Or through Microsoft 365, each email from the mailbox of the person signed in to Core:
        # Core's app registration in Microsoft Entra (with the Mail.Send application permission).
        MS_TENANT_ID=env("MS_TENANT_ID", "").strip(), MS_CLIENT_ID=env("MS_CLIENT_ID", "").strip(),
        MS_CLIENT_SECRET=env("MS_CLIENT_SECRET", "").strip(),
        # The business's name on emails to vendors.
        COMPANY_NAME=env("COMPANY_NAME", "Yeems Coffee").strip(),
        # The morning "needs attention" email goes after this hour (business time zone), and its
        # links start with PUBLIC_URL (e.g. https://dashboard.yeemscoffee.com; none: no links).
        DAILY_EMAIL_HOUR=int(env("DAILY_EMAIL_HOUR", "7") or 7),
        PUBLIC_URL=env("PUBLIC_URL", "").strip().rstrip("/"),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,  # maintenance attachments are up to 16 MB
        # Browsers keep the stylesheet, script, icon and fonts for a year: pages link them with a
        # fingerprint of their contents (static_version), so a changed file is fetched afresh.
        SEND_FILE_MAX_AGE_DEFAULT=365 * 24 * 3600,
    )
    if test_config:
        app.config.update(test_config)

    fingerprints = {}

    @app.url_defaults
    def static_version(endpoint, values):
        if endpoint == "static" and "filename" in values and "v" not in values:
            name = values["filename"]
            if name not in fingerprints:
                try:
                    with open(os.path.join(app.static_folder, name), "rb") as f:
                        fingerprints[name] = hashlib.sha256(f.read()).hexdigest()[:10]
                except OSError:
                    fingerprints[name] = None
            if fingerprints[name]:
                values["v"] = fingerprints[name]

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
        migrate_maintenance(db)  # columns added since the maintenance tables were first made
        db.ensure_columns("prod_runs", {"no_quantity": "INTEGER"})
    app.extensions["db"] = db

    if not cfg["SECRET_KEY"]:
        try:
            cfg["SECRET_KEY"] = app_secret(db)  # generated once, shared by every instance
        except Exception:  # read-only viewer before the key exists
            seed = "unleashedaccess:" + cfg["DASHBOARD_PASSWORD"] + ":" + cfg["DATABASE"]
            cfg["SECRET_KEY"] = hashlib.sha256(seed.encode()).hexdigest()

    users = UserStore(db)
    app.extensions["users"] = users
    profiles = Profiles(db)
    app.extensions["profiles"] = profiles
    cfg["ADMIN_SETUP_NOTE"] = None
    # What the sign-in page says while ADMIN_RESET is set: whether the reset applied and which
    # password to use (it used to do nothing, silently, when it had already been used).
    cfg["ADMIN_RESET_NOTE"] = None
    cfg["ADMIN_PASSWORD"] = (cfg["ADMIN_PASSWORD"] or "").strip()  # a space pasted after it isn't part of it
    # A view-only copy takes no part in setting up accounts, but a reset asked for is still tried.
    if cfg["ADMIN_EMAIL"] and cfg["ADMIN_PASSWORD"] and (not viewer or cfg["ADMIN_RESET"]):
        already = users.reset_applied_at(cfg["ADMIN_EMAIL"], cfg["ADMIN_PASSWORD"]) if cfg["ADMIN_RESET"] else None
        try:
            cfg["ADMIN_SETUP_NOTE"] = users.ensure_admin(
                cfg["ADMIN_EMAIL"], cfg["ADMIN_PASSWORD"], name=cfg["ADMIN_NAME"],
                reset=cfg["ADMIN_RESET"])
        except AccountError as e:
            raise AccountError(f"ADMIN_EMAIL / ADMIN_PASSWORD settings: {e}") from e
        except Exception:  # noqa: BLE001 (a view-only copy whose database login can't write)
            if not viewer:
                raise
            cfg["ADMIN_RESET_NOTE"] = ("ADMIN_RESET is set, but this copy of Core is view-only and couldn't change the "
                                       "account. Apply it on the copy that has the Unleashed API settings.")
        else:
            if cfg["ADMIN_RESET"] and cfg["ADMIN_SETUP_NOTE"]:
                cfg["ADMIN_RESET_NOTE"] = (
                    "Admin access was restored when Core started. Sign in with the ADMIN_EMAIL address and the "
                    "ADMIN_PASSWORD value from the settings, not your old password. You'll then choose a new one.")
            elif cfg["ADMIN_RESET"] and already is not None:
                cfg["ADMIN_RESET_NOTE"] = (
                    f"ADMIN_RESET is set, and was already applied with this ADMIN_PASSWORD{' on ' + already[:16] + ' UTC' if already else ''}"
                    ", so nothing changed this time. If you haven't chosen a new password since, sign in with the "
                    "ADMIN_PASSWORD value. Otherwise set ADMIN_PASSWORD to one never used for a reset and apply again.")
    elif cfg["ADMIN_RESET"]:
        cfg["ADMIN_RESET_NOTE"] = ("ADMIN_RESET is set, but ADMIN_EMAIL or ADMIN_PASSWORD is missing, so nothing was reset. "
                                   "Add both (the password at least 10 characters) and apply.")
    for note in (cfg["ADMIN_SETUP_NOTE"], cfg["ADMIN_RESET_NOTE"]):
        if note:
            app.logger.warning("Team accounts: %s", note)
    if not viewer:
        profiles.seed()
    try:
        has_accounts = users.count() > 0
    except Exception:  # a viewer on a database without the accounts table yet
        has_accounts = False
    # accounts: everyone has their own sign-in; password: one shared password; open: no sign-in
    cfg["AUTH_MODE"] = "accounts" if has_accounts else (
        "password" if cfg["DASHBOARD_PASSWORD"] else "open")

    if cfg["SESSION_COOKIE_SECURE"]:
        @app.before_request
        def _https_only():
            """The sign-in cookie only travels over HTTPS, so send http:// visits to https://.
            Behind a load balancer the app itself sees http; X-Forwarded-Proto says what the
            browser used (health checks, sent straight to the server, carry none)."""
            if request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "http":
                return redirect(request.url.replace("http://", "https://", 1),
                                code=301 if request.method in ("GET", "HEAD") else 308)
            return None

    @app.before_request
    def _own_forms_only():
        """A change (anything but a read) must come from Core's own pages. Browsers say where a
        request comes from (Sec-Fetch-Site, Origin, or the Referer): one from another site, or
        from another site under the same domain, is refused, so a page elsewhere can't act as
        the person signed in here. A request that says nothing (not a browser) is let through:
        it carries no one's sign-in by accident."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        from urllib.parse import urlsplit
        site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
        source = request.headers.get("Origin") or request.headers.get("Referer")
        foreign = site in ("cross-site", "same-site") or (
            source is not None and urlsplit(source).netloc.lower() != (request.host or "").lower())
        if not foreign:
            return None
        return render_template_string(
            "<!doctype html><title>Not done</title><body style=\"font:16px/1.5 system-ui,sans-serif;max-width:560px;"
            "margin:48px auto;padding:0 16px\"><h1 style=\"font-size:22px\">That wasn't done</h1><p>The request came "
            "from another website, not from a Core page, so Core refused it. If you pressed a button in Core, go back, "
            "reload the page and try again.</p>"), 403

    @app.after_request
    def _security_headers(response):
        """Headers that limit what a browser lets other sites do with Core's pages. Set only where
        a page hasn't set its own (file downloads have a stricter policy)."""
        h = response.headers
        h.setdefault("X-Frame-Options", "DENY")  # not shown inside another site's frame
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Content-Security-Policy",
                     "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
        if cfg["SESSION_COOKIE_SECURE"]:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if response.mimetype == "text/html":  # pages aren't kept by the browser after sign-out
            h.setdefault("Cache-Control", "no-store")
        return response

    dashboard.init_app(app)
    maintenance.init_app(app)
    return app
