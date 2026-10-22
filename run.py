import os

from envfile import load_env_file

load_env_file()

# With the debug reloader, this file runs in a watcher process and a server process;
# only the server process should run the background sync.
if os.environ.get("FLASK_DEBUG") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    os.environ["SYNC_ON_STARTUP"] = "off"

from app import create_app  # noqa: E402

app = create_app()

_ext = app.extensions["unleashed"]
if _ext["mode"] == "live":
    print(f" * Unleashed Access: LIVE data from {_ext['base_url']}")
elif _ext["mode"] == "viewer":
    print(" * Unleashed Access: VIEW-ONLY, reading the shared database (another instance syncs it)")
else:
    print(" * Unleashed Access: MOCK data (simulated). Set UNLEASHED_MODE=live in .env for real data.")
print(f" * Database: {_ext['db'].label}")
if _ext["source_error"]:
    print(f" ! {_ext['source_error']}")
if not app.config["DASHBOARD_PASSWORD"]:
    print(" * No DASHBOARD_PASSWORD set: anyone who can reach this address can see the dashboard")
if _ext["syncer"] and app.config["SYNC_ON_STARTUP"] == "background":
    print(f" * Syncing Unleashed data in the background, then every "
          f"{app.config['SYNC_INTERVAL_MINUTES']} minutes")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # .env is already loaded by envfile; load_dotenv=False stops Flask suggesting python-dotenv.
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1",
            load_dotenv=False)
