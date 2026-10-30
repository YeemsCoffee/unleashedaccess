"""Keeps the web server up when the app can't start, and says why.

Without this, a startup failure (database unreachable, a wrong setting) makes
gunicorn exit and the load balancer shows a bare "502 Bad Gateway". Instead,
every page explains the problem, /healthz reports it, and startup is retried
every RETRY_SECONDS, so a database that was still being created comes online
by itself.
"""
import html
import re
import sys
import threading
import time
import traceback

RETRY_SECONDS = 30


def explain(exc):
    """(headline, what to do) for a startup error, in plain language."""
    text = f"{exc.__class__.__name__}: {exc}"
    lower = text.lower()
    if "no password supplied" in lower:
        return ("The app didn't send a database password.",
                "Add the environment property DB_PASSWORD (exactly that name) with the RDS master "
                "password. If it's there, check the name for typos or spaces.")
    if "role" in lower and "does not exist" in lower:
        return ("The database has no user with that name.",
                "Set DB_USER to the RDS master username (shown on the database's Configuration tab).")
    if "password authentication failed" in lower:
        user = re.search(r'for user "([^"]+)"', text)
        tried = f' for the user "{user.group(1)}"' if user else ""
        return (f"The database rejected the password{tried}.",
                "Set DB_USER to the RDS master username (RDS → your database → Configuration → "
                "Master username; without DB_USER the app uses postgres) and DB_PASSWORD to its "
                "password.")
    if "does not exist" in lower and "database" in lower:
        return ("The database server has no database with that name, and DB_USER isn't "
                "allowed to create it.",
                "Use the RDS master username for DB_USER (it can create the database), or set "
                "DB_NAME to postgres, the database every server has.")
    if "no pg_hba.conf entry" in lower or "ssl" in lower and "required" in lower:
        return ("The database refused the connection's security settings.",
                "Leave DB_SSLMODE unset (the default, require, is what RDS expects).")
    if "could not translate host" in lower or "name or service not known" in lower:
        return ("DB_HOST isn't an address the app can find.",
                "Copy the RDS endpoint exactly, without https:// or :5432, e.g. "
                "unleashedaccess-db.abc123.us-west-1.rds.amazonaws.com.")
    if any(s in lower for s in ("pooltimeout", "couldn't get a connection", "timed out", "timeout",
                                "could not connect", "connection refused", "no route to host")):
        return ("The app can't reach the database.",
                "Check that DB_HOST is the RDS endpoint, that the database's security group "
                "(unleashedaccess-db) allows PostgreSQL from unleashedaccess-app, and that the app's "
                "instances use unleashedaccess-app. A database that's still being created also "
                "causes this; the app retries every 30 seconds.")
    if "valid email" in lower:
        return ("The first admin account couldn't be created.", "ADMIN_EMAIL isn't a valid email address.")
    if "password" in lower and "at least" in lower:
        return ("The first admin account couldn't be created.",
                "ADMIN_PASSWORD needs at least 10 characters.")
    if "db_secret" in lower:
        return ("DB_SECRET isn't valid.", "It must be the secret's JSON text from Secrets Manager.")
    return ("The app couldn't start.", "The details below, and the app's logs, say why.")


class StartupGuard:
    """A WSGI app: the real app once it has started, an explanation page until then."""

    def __init__(self, factory, on_ready=None, retry_seconds=RETRY_SECONDS):
        self.factory = factory
        self.on_ready = on_ready
        self.retry_seconds = retry_seconds
        self.app = None
        self.error = None
        self._last_try = 0.0
        self._lock = threading.Lock()
        self._start()

    def _start(self):
        self._last_try = time.monotonic()
        try:
            app = self.factory()
        except Exception as e:  # noqa: BLE001 - any startup failure is shown, not raised
            self.error = e
            print(" ! The app couldn't start; showing the reason on every page and retrying every "
                  f"{self.retry_seconds} seconds:", file=sys.stderr)
            traceback.print_exc()
            return
        self.app, self.error = app, None
        if self.on_ready:
            self.on_ready(app)

    def __call__(self, environ, start_response):
        if self.app is None and time.monotonic() - self._last_try >= self.retry_seconds:
            with self._lock:
                if self.app is None and time.monotonic() - self._last_try >= self.retry_seconds:
                    self._start()
        if self.app is not None:
            return self.app(environ, start_response)
        return self._problem_page(environ, start_response)

    def _problem_page(self, environ, start_response):
        headline, todo = explain(self.error)
        detail = f"{self.error.__class__.__name__}: {self.error}".strip()
        if environ.get("PATH_INFO") == "/healthz":
            body = '{"status": "error", "problem": %s}' % _json_string(headline)
            start_response("503 Service Unavailable", [("Content-Type", "application/json"),
                                                       ("Retry-After", str(self.retry_seconds))])
            return [body.encode()]
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{self.retry_seconds}">
<title>Yeems Core can't start</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:680px;margin:48px auto;padding:0 16px;color:#1b1b1b}}
h1{{font-size:22px}}.todo{{background:#fdf0d9;padding:12px 14px;border-radius:8px}}
pre{{white-space:pre-wrap;background:#f4f3f0;padding:12px;border-radius:8px;font-size:13px}}
.muted{{color:#6b6a66;font-size:14px}}</style></head><body>
<h1>Yeems Core can't start yet</h1>
<p><strong>{html.escape(headline)}</strong></p>
<p class="todo">{html.escape(todo)}</p>
<p class="muted">After you change a setting, the app restarts by itself. It also tries again every
{self.retry_seconds} seconds, and this page refreshes on its own.</p>
<p class="muted">Technical details:</p><pre>{html.escape(detail)}</pre>
</body></html>"""
        start_response("503 Service Unavailable", [("Content-Type", "text/html; charset=utf-8"),
                                                   ("Retry-After", str(self.retry_seconds))])
        return [page.encode()]


def _json_string(s):
    import json
    return json.dumps(s)
