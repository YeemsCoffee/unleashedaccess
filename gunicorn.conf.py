"""gunicorn settings, picked up automatically by `gunicorn run:app`."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
# One worker: the background sync runs inside the web process. Threads handle
# concurrent page views. (More workers are safe, since a database lock lets only
# one sync at a time, but each would repeat the startup work.)
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 120
accesslog = "-"
