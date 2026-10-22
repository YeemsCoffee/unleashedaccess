"""Copy of Unleashed data in a database (SQLite or shared PostgreSQL), and a client
that answers API queries from it.

The sync engine (sync.py) fills the store from the Unleashed API; dashboard
pages read through LocalStoreClient, which mimics UnleashedClient's get/get_all
for the endpoints the reports use. Raw Unleashed JSON is stored as-is, so report
parsing is identical whether data comes from the API or the store.

Timestamps in the store are UTC ("YYYY-MM-DD HH:MM:SS"), since several machines
in different time zones may share one database.
"""
import datetime as dt
import json
import threading
import time

from .client import UnleashedError, parse_date

# Reference lists, replaced wholesale on every sync: endpoint -> key field.
REFERENCE_ENDPOINTS = {
    "Products": "Guid", "ProductGroups": "GroupName", "Warehouses": "WarehouseCode",
    "Suppliers": "Guid", "Customers": "Guid",
}
DOCUMENT_ENDPOINTS = ("PurchaseOrders", "SalesOrders", "StockAdjustments")
CURRENT = "current"


def utc_now(offset_seconds=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _iso(value):
    d = parse_date(value) if value else None
    return d.isoformat() if d else None


def document_columns(endpoint, d):
    """(number, status, doc_date, party_code, effective_date) for a stored document.

    doc_date is what Unleashed's startDate/endDate filter on; effective_date is
    when the document moved stock (used to know which stock snapshots it affects).
    """
    if endpoint == "PurchaseOrders":
        return (d.get("OrderNumber"), d.get("OrderStatus"), _iso(d.get("OrderDate")),
                (d.get("Supplier") or {}).get("SupplierCode"),
                _iso(d.get("ReceivedDate") or d.get("CompletedDate") or d.get("OrderDate")))
    if endpoint == "SalesOrders":
        return (d.get("OrderNumber"), d.get("OrderStatus"), _iso(d.get("OrderDate")),
                (d.get("Customer") or {}).get("CustomerCode"),
                _iso(d.get("CompletedDate") or d.get("OrderDate")))
    return (d.get("AdjustmentNumber"), d.get("Status"), _iso(d.get("AdjustmentDate")), None,
            _iso(d.get("AdjustmentDate")))


class SourceMismatch(Exception):
    """The shared database holds data from a different Unleashed account or mode."""


class LocalStore:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ state

    def get_state(self, key, default=None):
        rows = self.db.query("SELECT value FROM ul_sync_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key, value):
        self.db.execute("INSERT INTO ul_sync_state (key, value) VALUES (?, ?)"
                        " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def ensure_source(self, source_id, source_mode, allow_reset=False):
        """Claim the store for this account/mode.

        A local SQLite copy from another source is simply replaced. A shared
        database is only replaced with allow_reset, since other people use it.
        """
        current = self.get_state("source")
        if current == source_id:
            return
        if current is not None and self.db.shared and not allow_reset:
            raise SourceMismatch(
                f"The shared database holds {self.get_state('source_mode', 'other')} data from a "
                "different Unleashed account or setup, so this app won't sync into it. Point "
                "DATABASE_URL at another database, or set ALLOW_DATABASE_RESET=1 to replace it.")
        with self.db.connect() as conn:
            for table in ("ul_records", "ul_documents", "ul_stock_snapshots",
                          "ul_sync_runs", "ul_sync_state"):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany("INSERT INTO ul_sync_state (key, value) VALUES (?, ?)",
                             [("source", source_id), ("source_mode", source_mode)])

    # ------------------------------------------------------------ cross-instance sync lock

    def acquire_lock(self, owner, ttl_seconds):
        """Take the sync lock if it's free, expired, or already ours."""
        return self.db.execute(
            "UPDATE ul_sync_lock SET owner = ?, expires_at = ? WHERE id = 1"
            " AND (owner IS NULL OR expires_at < ? OR owner = ?)",
            (owner, utc_now(ttl_seconds), utc_now(), owner)) == 1

    def renew_lock(self, owner, ttl_seconds):
        self.db.execute("UPDATE ul_sync_lock SET expires_at = ? WHERE id = 1 AND owner = ?",
                        (utc_now(ttl_seconds), owner))

    def release_lock(self, owner):
        self.db.execute("UPDATE ul_sync_lock SET owner = NULL, expires_at = NULL"
                        " WHERE id = 1 AND owner = ?", (owner,))

    def sync_in_progress(self):
        rows = self.db.query("SELECT owner, expires_at FROM ul_sync_lock WHERE id = 1")
        return bool(rows and rows[0]["owner"] and rows[0]["expires_at"] > utc_now())

    # ------------------------------------------------------------ reference data

    def replace_records(self, kind, items, key_field):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM ul_records WHERE kind = ?", (kind,))
            conn.executemany(
                "INSERT INTO ul_records (kind, key, data) VALUES (?, ?, ?)"
                " ON CONFLICT (kind, key) DO UPDATE SET data = excluded.data",
                [(kind, str(i.get(key_field) or n), json.dumps(i)) for n, i in enumerate(items)])

    def records(self, kind):
        return [json.loads(r["data"]) for r in self.db.query(
            "SELECT data FROM ul_records WHERE kind = ? ORDER BY key", (kind,))]

    # ------------------------------------------------------------ documents

    def upsert_documents(self, endpoint, items):
        """Insert/update documents. Returns (changed count, affected stock dates)."""
        with self.db.connect() as conn:
            existing = {r["guid"]: r for r in conn.query(
                "SELECT guid, data, effective_date FROM ul_documents WHERE kind = ?", (endpoint,))}
            rows, dates = [], []
            for d in items:
                guid = d.get("Guid") or document_columns(endpoint, d)[0]
                data = json.dumps(d, sort_keys=True)
                old = existing.get(guid)
                if old is not None and old["data"] == data:
                    continue
                number, status, doc_date, party, effective = document_columns(endpoint, d)
                rows.append((endpoint, guid, number, status, doc_date, party, effective, data,
                             utc_now()))
                dates += [x for x in (effective, old["effective_date"] if old else None) if x]
            conn.executemany(
                "INSERT INTO ul_documents (kind, guid, number, status, doc_date, party_code,"
                " effective_date, data, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (kind, guid) DO UPDATE SET number = excluded.number,"
                " status = excluded.status, doc_date = excluded.doc_date,"
                " party_code = excluded.party_code, effective_date = excluded.effective_date,"
                " data = excluded.data, synced_at = excluded.synced_at", rows)
        return len(rows), [dt.date.fromisoformat(x) for x in dates]

    def prune_documents(self, endpoint, keep_guids):
        """After a full sync: drop documents Unleashed no longer returned."""
        with self.db.connect() as conn:
            stored = [r["guid"] for r in conn.query(
                "SELECT guid FROM ul_documents WHERE kind = ?", (endpoint,))]
            gone = [(endpoint, g) for g in stored if g not in keep_guids]
            conn.executemany("DELETE FROM ul_documents WHERE kind = ? AND guid = ?", gone)
        return len(gone)

    def query_documents(self, endpoint, start=None, end=None, statuses=None, party=None,
                        number=None):
        sql = "SELECT data FROM ul_documents WHERE kind = ?"
        args = [endpoint]
        if start:
            sql += " AND doc_date >= ?"
            args.append(start)
        if end:
            sql += " AND doc_date <= ?"
            args.append(end)
        if statuses:
            sql += f" AND lower(status) IN ({', '.join('?' * len(statuses))})"
            args += [s.lower() for s in statuses]
        if party:
            sql += " AND lower(party_code) = lower(?)"
            args.append(party)
        if number:
            sql += " AND number LIKE ?"
            args.append(f"%{number}%")
        sql += " ORDER BY doc_date, number"
        return [json.loads(r["data"]) for r in self.db.query(sql, args)]

    def document_by_guid(self, endpoint, guid):
        rows = self.db.query("SELECT data FROM ul_documents WHERE kind = ? AND guid = ?",
                             (endpoint, guid))
        return json.loads(rows[0]["data"]) if rows else None

    def document_count(self, endpoint):
        return self.db.query("SELECT COUNT(*) AS n FROM ul_documents WHERE kind = ?",
                             (endpoint,))[0]["n"]

    # ------------------------------------------------------------ stock snapshots

    def snapshot(self, as_at):
        rows = self.db.query("SELECT data FROM ul_stock_snapshots WHERE as_at = ?", (as_at,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_snapshot(self, as_at, items):
        self.db.execute(
            "INSERT INTO ul_stock_snapshots (as_at, data, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT (as_at) DO UPDATE SET data = excluded.data,"
            " fetched_at = excluded.fetched_at", (as_at, json.dumps(items), utc_now()))

    def snapshots_in_month(self, month):
        """{as_at: rows} for every stored snapshot dated in `month` ("YYYY-MM"), in one query."""
        return {r["as_at"]: json.loads(r["data"]) for r in self.db.query(
            "SELECT as_at, data FROM ul_stock_snapshots WHERE as_at LIKE ?", (f"{month}-%",))}

    def snapshot_dates(self):
        return {r["as_at"] for r in self.db.query("SELECT as_at FROM ul_stock_snapshots")
                if r["as_at"] != CURRENT}

    def delete_snapshots(self, keep):
        """Drop dated snapshots not in `keep`."""
        gone = [(d,) for d in self.snapshot_dates() if d not in keep]
        self.db.executemany("DELETE FROM ul_stock_snapshots WHERE as_at = ?", gone)

    # ------------------------------------------------------------ sync runs

    def start_run(self, kind):
        with self.db.connect() as conn:
            conn.execute("INSERT INTO ul_sync_runs (kind, status, step, started_at)"
                         " VALUES (?, 'running', 'Starting', ?)", (kind, utc_now()))
            return conn.query_one("SELECT MAX(id) AS id FROM ul_sync_runs")["id"]

    def update_run(self, run_id, step):
        self.db.execute("UPDATE ul_sync_runs SET step = ? WHERE id = ?", (step, run_id))

    def finish_run(self, run_id, status, detail=None, error=None):
        self.db.execute("UPDATE ul_sync_runs SET status = ?, step = NULL, detail = ?, error = ?,"
                        " finished_at = ? WHERE id = ?",
                        (status, detail, error, utc_now(), run_id))

    def mark_interrupted(self):
        """Close out runs left 'running' by an instance that stopped mid-sync."""
        if not self.sync_in_progress():
            self.db.execute("UPDATE ul_sync_runs SET status = 'failed',"
                            " error = 'Interrupted (the app stopped mid-sync)', finished_at = ?"
                            " WHERE status = 'running'", (utc_now(),))

    def runs(self, limit=10):
        return self.db.query("SELECT * FROM ul_sync_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_run(self):
        rows = self.runs(1)
        return rows[0] if rows else None

    def last_success(self):
        rows = self.db.query("SELECT * FROM ul_sync_runs WHERE status = 'ok'"
                             " ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def has_data(self):
        return self.last_success() is not None

    def generation(self):
        """Changes whenever a sync finishes; used to invalidate read caches."""
        row = self.db.query("SELECT MAX(id) AS id FROM ul_sync_runs WHERE status = 'ok'")
        return row[0]["id"] if row else None

    def prune_api_log(self, days):
        self.db.execute("DELETE FROM unleashed_api_log WHERE timestamp < ?",
                        (utc_now(-days * 86400),))


class LocalStoreClient:
    """Answers the reports' API queries from the store.

    Falls back to `live` (an UnleashedClient) for anything the store can't answer:
    date ranges older than the synced window, unknown order numbers, other endpoints.
    `fetch_snapshot(as_at)` fetches and stores a stock snapshot that isn't stored yet.
    Results are cached in memory until the next completed sync.
    """
    mode = "store"
    GENERATION_CHECK_SECONDS = 2

    def __init__(self, store, live=None, today_fn=dt.date.today, fetch_snapshot=None):
        self.store = store
        self.live = live
        self.today_fn = today_fn
        self.fetch_snapshot = fetch_snapshot
        self._cache = {}
        self._generation = None
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def clear_cache(self):
        with self._lock:
            self._cache = {}
            self._checked_at = 0.0

    def _memo(self, key, fn):
        now = time.monotonic()
        if now - self._checked_at > self.GENERATION_CHECK_SECONDS:
            generation = self.store.generation()
            with self._lock:
                if generation != self._generation:
                    self._cache, self._generation = {}, generation
                self._checked_at = now
        if key in self._cache:
            return self._cache[key]
        value = fn()
        self._cache[key] = value
        return value

    def get(self, endpoint, params=None, page=None, long_cache=False):
        if "/" in endpoint:
            base, guid = endpoint.split("/", 1)
            doc = self.store.document_by_guid(base, guid)
            if doc is None and self.live:
                return self.live.get(endpoint, params, page, long_cache)
            return doc or {}
        items = self.get_all(endpoint, params)
        return {"Pagination": {"NumberOfItems": len(items), "PageSize": max(len(items), 1),
                               "PageNumber": 1, "NumberOfPages": 1},
                "Items": items}

    def get_all(self, endpoint, params=None, **_):
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        key = (endpoint, tuple(sorted(params.items())))
        if endpoint in REFERENCE_ENDPOINTS:
            return self._memo(key, lambda: self._reference(endpoint, params))
        if endpoint == "StockOnHand":
            return self._stock(params)
        if endpoint in DOCUMENT_ENDPOINTS:
            return self._memo(key, lambda: self._documents(endpoint, params))
        return self._live(endpoint, params, f"{endpoint} isn't in the stored copy")

    def _live(self, endpoint, params, why):
        if not self.live:
            raise UnleashedError(why)
        return self.live.get_all(endpoint, params)

    def _reference(self, endpoint, params):
        items = self.store.records(endpoint)
        if endpoint == "Products":
            code = params.get("productCode")
            if code:
                items = [p for p in items if code.lower() in (p.get("ProductCode") or "").lower()]
            group = params.get("productGroup")
            if group:
                items = [p for p in items if (p.get("ProductGroup") or {}).get("GroupName") == group]
        return items

    def _stock(self, params):
        if params.get("warehouseCode"):
            return self._live("StockOnHand", params, "Per-warehouse stock isn't in the stored copy")
        as_at = params.get("asAtDate")
        key = CURRENT if not as_at or as_at[:10] >= self.today_fn().isoformat() else as_at[:10]
        # A month of daily snapshots loads in one query: the COGS chart reads them all.
        bucket = self._memo(("snapshots", key[:7] if key != CURRENT else CURRENT),
                            lambda: {CURRENT: self.store.snapshot(CURRENT)} if key == CURRENT
                            else self.store.snapshots_in_month(key[:7]))
        rows = bucket.get(key)
        if rows is None:
            if not self.fetch_snapshot:
                raise UnleashedError(f"Stock on hand as at {key} isn't in the shared copy yet "
                                     "(only the syncing instance can fetch it)")
            rows = bucket[key] = self.fetch_snapshot(key)
        code = params.get("productCode")
        if code:
            rows = [r for r in rows if (r.get("ProductCode") or "").lower() == code.lower()]
        return rows

    def _documents(self, endpoint, params):
        start = params.get("startDate")
        window = self._memo(("window", endpoint),
                            lambda: self.store.get_state(f"window:{endpoint}"))
        if start and window and start[:10] < window:
            return self._live(endpoint, params,
                              f"Dates before {window} aren't in the shared copy of Unleashed data")
        statuses = [s.strip() for s in params["orderStatus"].split(",")] \
            if params.get("orderStatus") else None
        number = params.get("orderNumber") or params.get("adjustmentNumber")
        items = self.store.query_documents(
            endpoint, start=start[:10] if start else None,
            end=params["endDate"][:10] if params.get("endDate") else None,
            statuses=statuses, party=params.get("supplierCode") or params.get("customerCode"),
            number=number)
        if number and not items and self.live:
            return self.live.get_all(endpoint, params)
        return items
