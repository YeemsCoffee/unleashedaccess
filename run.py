import os

from envfile import load_env_file

load_env_file()

# With the debug reloader, this file runs in a watcher process and a server process;
# only the server process should run the background sync.
if os.environ.get("FLASK_DEBUG") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    os.environ["SYNC_ON_STARTUP"] = "off"

from app import create_app  # noqa: E402
from startup import StartupGuard  # noqa: E402


def report(app):
    """Startup summary for the server log."""
    ext = app.extensions["unleashed"]
    if ext["mode"] == "live":
        print(f" * Unleashed Access: LIVE data from {ext['base_url']}")
    elif ext["mode"] == "viewer":
        print(" * Unleashed Access: VIEW-ONLY, reading the shared database (another instance syncs it)")
    else:
        print(" * Unleashed Access: MOCK data (simulated). For real data, set UNLEASHED_MODE=live and the"
              " API ID/key (in .env, or the server's environment settings).")
    print(f" * Database: {ext['db'].label}")
    if ext["source_error"]:
        print(f" ! {ext['source_error']}")
    if app.config["ADMIN_SETUP_NOTE"]:
        print(f" * Team accounts: {app.config['ADMIN_SETUP_NOTE']} (it must choose a new password at"
              " first sign-in; you can then remove ADMIN_PASSWORD from the settings)")
    if app.config["AUTH_MODE"] == "accounts":
        n = app.extensions["users"].count()
        print(f" * Sign-in: team accounts ({n} {'person' if n == 1 else 'people'})")
    elif app.config["AUTH_MODE"] == "password":
        print(" * Sign-in: one shared DASHBOARD_PASSWORD")
    else:
        print(" * Sign-in: none. Anyone who can reach this address can see the dashboard; set"
              " ADMIN_EMAIL and ADMIN_PASSWORD to turn on team accounts")
    if ext["syncer"] and app.config["SYNC_ON_STARTUP"] == "background":
        print(f" * Syncing Unleashed data in the background, then every "
              f"{app.config['SYNC_INTERVAL_MINUTES']} minutes")


# What gunicorn serves (`gunicorn run:app`): the dashboard, or, if it can't start,
# a page explaining why (retried every 30 seconds) instead of a bare 502.
app = StartupGuard(create_app, on_ready=report)

if __name__ == "__main__":
    from werkzeug.serving import run_simple

    debug = os.environ.get("FLASK_DEBUG") == "1"
    run_simple("0.0.0.0", int(os.environ.get("PORT", "8000")), app,
               use_reloader=debug, use_debugger=debug, threaded=True)
