"""Copies Unleashed data into the local store, in full or incrementally.

Full sync (first run, then weekly): reference lists, every document in the
history window, and the stock snapshots the reports need. Incremental sync
(every few minutes): reference lists, documents modified since the last sync
(Unleashed `modifiedSince`), current stock, and re-fetched snapshots for recent
days plus any day a changed document touched, so backdated edits are caught.
"""
import datetime as dt
import os
import socket
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from .client import UnleashedError
from .reports import add_months, cost_line_keys, month_start
from .store import (ALL_DATES, CURRENT, DOCUMENT_ENDPOINTS, OPTIONAL_ENDPOINTS,
                    REFERENCE_ENDPOINTS, UNWINDOWED_ENDPOINTS, document_columns, utc_now)

LOCK_SECONDS = 15 * 60  # renewed at every step, so only a stalled sync lets it lapse

LABELS = {"PurchaseOrders": "purchase orders", "SalesOrders": "sales orders",
          "StockAdjustments": "stock adjustments", "Assemblies": "assemblies"}


class Syncer:
    def __init__(self, client, store, today_fn=dt.date.today, history_months=13,
                 po_lookback_days=183, recheck_days=7, full_every_days=7, max_workers=4,
                 log_retention_days=30):
        self.client = client
        self.store = store
        self.today_fn = today_fn
        self.history_months = history_months
        self.po_lookback_days = po_lookback_days
        self.recheck_days = recheck_days
        self.full_every_days = full_every_days
        self.max_workers = max_workers
        self.log_retention_days = log_retention_days
        self._lock = threading.Lock()
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.on_complete = []  # called after each successful sync (e.g. to drop read caches)

    @property
    def running(self):
        """A sync is running here or in another instance sharing the database."""
        return self._lock.locked() or self.store.sync_in_progress()

    def needs_full(self):
        last = self.store.get_state("last_full_started")
        if not last or not self.store.has_data():
            return True
        return last < utc_now(-self.full_every_days * 86400)

    def required_snapshot_dates(self, today=None):
        """Month ends for the history window, plus every day of last month and this month."""
        today = today or self.today_fn()
        first = month_start(today)
        dates = {add_months(first, -i) - dt.timedelta(days=1) for i in range(self.history_months)}
        day = add_months(first, -1)
        while day < today:
            dates.add(day)
            day += dt.timedelta(days=1)
        return sorted(d for d in dates if d < today)

    def sync(self, full=None):
        """Run one sync. Returns the run's summary dict; never raises."""
        if not self._lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            if not self.store.acquire_lock(self.owner, LOCK_SECONDS):
                return {"status": "busy", "reason": "another instance is syncing"}
            try:
                return self._sync(self.needs_full() if full is None else full)
            finally:
                self.store.release_lock(self.owner)
        finally:
            self._lock.release()

    def fetch_snapshot(self, as_at):
        """Fetch one stock-on-hand snapshot ('current' or an ISO date) and store it."""
        params = {} if as_at == CURRENT else {"asAtDate": as_at}
        rows = self.client.get_all("StockOnHand", params)
        self.store.save_snapshot(as_at, rows)
        return rows

    # ------------------------------------------------------------ internals

    def _sync(self, full):
        store, today = self.store, self.today_fn()
        run = store.start_run("full" if full else "incremental")
        started = utc_now()
        last_success = store.get_state("last_success_started")
        self.client.clear_cache()
        counts = {}
        try:
            self._step(run, "Products, suppliers, customers and bills of materials")
            for endpoint, key in REFERENCE_ENDPOINTS.items():
                fetched = self._fetch(endpoint, {}, counts)
                if fetched is not None:
                    store.replace_records(endpoint, fetched[0], key)
                    counts[endpoint] = len(fetched[0])

            history_start = add_months(month_start(today), -self.history_months)
            changed_dates = []
            for endpoint in DOCUMENT_ENDPOINTS:
                self._step(run, f"Downloading {LABELS[endpoint]}")
                # Everything on a full sync, or the first time an endpoint is copied.
                fresh = full or not last_success or not store.get_state(f"window:{endpoint}")
                window = None
                if not fresh:
                    since = dt.date.fromisoformat(last_success[:10]) - dt.timedelta(days=1)
                    params = {"modifiedSince": since.isoformat()}
                elif endpoint in UNWINDOWED_ENDPOINTS:
                    params = {}
                else:
                    window = history_start
                    if endpoint == "PurchaseOrders":
                        window -= dt.timedelta(days=self.po_lookback_days)
                    params = {"startDate": window.isoformat()}
                fetched = self._fetch(endpoint, params, counts)
                if fetched is None:
                    continue
                items, filtered = fetched
                if endpoint == "PurchaseOrders":
                    items = self._with_detail(endpoint, items, run, cost_line_keys, "cost lines",
                                              "po_detail_has_cost_lines")
                elif endpoint == "Assemblies":
                    items = self._with_detail(endpoint, items, run,
                                              lambda a: a.get("AssemblyLines") is not None,
                                              "components", "assembly_detail_has_lines")
                changed, dates = store.upsert_documents(endpoint, items)
                changed_dates += dates
                pruned = 0
                if fresh or not filtered:  # the complete set: drop what Unleashed no longer has
                    pruned = store.prune_documents(endpoint, {i.get("Guid") for i in items})
                    store.set_state(f"window:{endpoint}", window.isoformat() if window else ALL_DATES)
                counts[endpoint] = f"{len(items)} fetched, {changed} changed" + \
                    (f", {pruned} removed" if pruned else "")

            required = self.required_snapshot_dates(today)
            if full:
                store.delete_snapshots(keep={d.isoformat() for d in required})
                todo = required
            else:
                recheck_from = today - dt.timedelta(days=self.recheck_days)
                if changed_dates:
                    recheck_from = min(recheck_from, min(changed_dates))
                have = store.snapshot_dates()
                required_iso = {d.isoformat() for d in required}
                # Extra snapshots fetched on demand may be stale now; they refetch when needed.
                store.delete_snapshots(keep={d for d in have if d in required_iso
                                             or d < recheck_from.isoformat()})
                todo = [d for d in required if d >= recheck_from or d.isoformat() not in have]
            self._fetch_snapshots(todo, run)
            self._step(run, "Current stock on hand")
            self.fetch_snapshot(CURRENT)
            counts["stock snapshots"] = len(todo) + 1

            store.prune_api_log(self.log_retention_days)
            detail = "; ".join(f"{k}: {v}" for k, v in counts.items())
            store.set_state("last_success_started", started)
            if full:
                store.set_state("last_full_started", started)
            store.finish_run(run, "ok", detail=detail)
            for callback in self.on_complete:
                callback()
            return {"status": "ok", "full": full, "counts": counts}
        except Exception as e:  # keep the previous data; record why this run failed
            traceback.print_exc(file=sys.stderr)
            store.finish_run(run, "failed", error=str(e) or e.__class__.__name__)
            return {"status": "failed", "full": full, "error": str(e)}

    def _step(self, run, text):
        self.store.update_run(run, text)
        self.store.renew_lock(self.owner, LOCK_SECONDS)

    def _fetch(self, endpoint, params, counts):
        """(items, whether `params` filtered them), or None for a production endpoint
        this account can't use (recorded, so the Production pages can say why)."""
        try:
            items = self.client.get_all(endpoint, params)
        except UnleashedError as e:
            if endpoint not in OPTIONAL_ENDPOINTS:
                raise
            if params and e.status_code == 400:  # a filter this endpoint doesn't take
                return self._fetch(endpoint, {}, counts)
            self.store.set_state(f"unavailable:{endpoint}", str(e))
            counts[endpoint] = "not available"
            return None
        if endpoint in OPTIONAL_ENDPOINTS and self.store.get_state(f"unavailable:{endpoint}"):
            self.store.set_state(f"unavailable:{endpoint}", "")
        return items, bool(params)

    def _with_detail(self, endpoint, items, run, has, what, state_key):
        """Fetch each document's detail when the list leaves `what` out (e.g. PO cost
        lines, where shipping lives). `state_key` remembers when detail doesn't help."""
        if not items or any(has(i) for i in items) or self.store.get_state(state_key) == "no":
            return items
        label = LABELS[endpoint]
        self._step(run, f"Downloading {what} for {len(items)} {label}")
        done = [0]

        def detail(i):
            if not i.get("Guid"):
                return i
            d = self.client.get(f"{endpoint}/{i['Guid']}")
            done[0] += 1
            if done[0] % 25 == 0:
                self._step(run, f"Downloading {what} ({done[0]} of {len(items)} {label})")
            return d if isinstance(d, dict) and document_columns(endpoint, d)[0] else i

        details = self._map(detail, items)
        self.store.set_state(state_key, "yes" if any(has(d) for d in details) else "no")
        return details

    def _fetch_snapshots(self, dates, run):
        if not dates:
            return
        done = [0]

        def one(d):
            self.fetch_snapshot(d.isoformat())
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(dates):
                self._step(run, f"Stock on hand history ({done[0]} of {len(dates)} days)")

        self._map(one, dates)

    def _map(self, fn, args):
        args = list(args)
        if self.max_workers <= 1 or len(args) <= 1:
            return [fn(a) for a in args]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(fn, args))


class SyncScheduler:
    """Background thread: syncs on start, then every `interval_minutes`, or when triggered."""

    def __init__(self, syncer, interval_minutes=15):
        self.syncer = syncer
        self.interval = interval_minutes * 60 if interval_minutes > 0 else None
        self._wake = threading.Event()
        self._full_requested = False
        self.thread = None

    @property
    def started(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if not self.started:
            self.thread = threading.Thread(target=self._loop, name="unleashed-sync", daemon=True)
            self.thread.start()

    def trigger(self, full=False):
        self._full_requested = self._full_requested or full
        self._wake.set()

    def _loop(self):
        while True:
            full, self._full_requested = self._full_requested, False
            self.syncer.sync(full=True if full else None)
            self._wake.wait(timeout=self.interval)
            self._wake.clear()
