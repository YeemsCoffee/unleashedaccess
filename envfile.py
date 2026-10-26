"""Minimal .env loader (KEY=VALUE lines); real environment variables win."""
import os


def load_env_file(path=None):
    """Load .env (or the file named by ENV_FILE) into os.environ."""
    path = path or os.environ.get("ENV_FILE") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:  # -sig: tolerate a BOM from Windows editors
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
