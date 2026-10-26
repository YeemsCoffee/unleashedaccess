"""Run one Unleashed sync, then exit: for scheduled jobs (ECS scheduled task, cron).

    python sync_once.py          # incremental (or full, if one is due)
    python sync_once.py --full   # re-copy everything

Exit code 0 when the sync succeeded or another instance was already syncing,
1 when it failed, 2 when this setup can't sync (no API key, or a shared
database that belongs to another account).
"""
import os
import sys

from envfile import load_env_file

load_env_file()
os.environ["SYNC_ON_STARTUP"] = "off"

from app import create_app  # noqa: E402


def main(argv):
    ext = create_app().extensions["unleashed"]
    if ext["syncer"] is None:
        print(ext["source_error"] or "This setup can't sync: set UNLEASHED_API_ID and "
              "UNLEASHED_API_KEY (or UNLEASHED_MODE=mock).", file=sys.stderr)
        return 2
    result = ext["syncer"].sync(full=True if "--full" in argv else None)
    if result["status"] == "ok":
        kind = "full" if result["full"] else "incremental"
        print(f"Sync ok ({kind}): " + "; ".join(f"{k}: {v}" for k, v in result["counts"].items()))
        return 0
    if result["status"] == "busy":
        print("Another instance is already syncing; nothing to do.")
        return 0
    print(f"Sync failed: {result.get('error')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
