"""Local SQLite copy of Unleashed data, and a client that answers API queries from it.

The sync engine (sync.py) fills the store from the Unleashed API; dashboard
pages read through LocalStoreClient, which mimics UnleashedClient's get/get_all
for the endpoints the reports use. Raw Unleashed JSON is stored as-is, so report
parsing is identical whether data comes from the API or the store.
"""
import datetime as dt
import json
import sqlite3

from .client import UnleashedError, parse_date

# Reference lists, replaced wholesale on every sync: endpoint -> key field.
REFERENCE_ENDPOINTS = {
    "Products": "Guid", "ProductGroups": "GroupName", "Warehouses": "WarehouseCode",
    "Suppliers": "Guid", "Customers": "Guid",
}
DOCUMENT_ENDPOINTS = ("PurchaseOrders", "SalesOrders", "StockAdjustments")
CURRENT = "current"


def now_text():
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


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


class LocalStore:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, sql, args=(), many=False):
        conn = self.connect()
        try:
            (conn.executemany if many else conn.execute)(sql, args)
            conn.commit()
        finally:
            conn.close()

    def _all(self, sql, args=()):
        conn = self.connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------ state

    def get_state(self, key, default=None):
        rows = self._all("SELECT value FROM ul_sync_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key, value):
        self._run("INSERT INTO ul_sync_state (key, value) VALUES (?, ?)"
                  " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def ensure_source(self, source_id):
        """Wipe stored data if it came from a different account or mode."""
        if self.get_state("source") != source_id:
            conn = self.connect()
            try:
                for table in ("ul_records", "ul_documents", "ul_stock_snapshots",
                              "ul_sync_runs", "ul_sync_state"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("INSERT INTO ul_sync_state (key, value) VALUES ('source', ?)",
                             (source_id,))
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------ reference data

    def replace_records(self, kind, items, key_field):
        conn = self.connect()
        try:
            conn.execute("DELETE FROM ul_records WHERE kind = ?", (kind,))
            conn.executemany(
                "INSERT OR REPLACE INTO ul_records (kind, key, data) VALUES (?, ?, ?)",
                [(kind, str(i.get(key_field) or n), json.dumps(i)) for n, i in enumerate(items)])
            conn.commit()
        finally:
            conn.close()

    def records(self, kind):
        return [json.loads(r["data"]) for r in
                self._all("SELECT data FROM ul_records WHERE kind = ? ORDER BY rowid", (kind,))]

    # ------------------------------------------------------------ documents

    def upsert_documents(self, endpoint, items):
        """Insert/update documents. Returns (changed count, affected stock dates)."""
        changed, dates = 0, []
        conn = self.connect()
        try:
            for d in items:
                guid = d.get("Guid") or document_columns(endpoint, d)[0]
                data = json.dumps(d, sort_keys=True)
                old = conn.execute("SELECT data, effective_date FROM ul_documents"
                                   " WHERE kind = ? AND guid = ?", (endpoint, guid)).fetchone()
                if old is not None and old["data"] == data:
                    continue
                number, status, doc_date, party, effective = document_columns(endpoint, d)
                conn.execute(
                    "INSERT OR REPLACE INTO ul_documents (kind, guid, number, status, doc_date,"
                    " party_code, effective_date, data, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (endpoint, guid, number, status, doc_date, party, effective, data, now_text()))
                changed += 1
                dates += [x for x in (effective, old["effective_date"] if old else None) if x]
            conn.commit()
        finally:
            conn.close()
        return changed, [dt.date.fromisoformat(x) for x in dates]

    def prune_documents(self, endpoint, keep_guids):
        """After a full sync: drop documents Unleashed no longer returned."""
        conn = self.connect()
        try:
            stored = [r["guid"] for r in conn.execute(
                "SELECT guid FROM ul_documents WHERE kind = ?", (endpoint,))]
            gone = [(endpoint, g) for g in stored if g not in keep_guids]
            conn.executemany("DELETE FROM ul_documents WHERE kind = ? AND guid = ?", gone)
            conn.commit()
        finally:
            conn.close()
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
            sql += f" AND lower(status) IN ({','.join('?' * len(statuses))})"
            args += [s.lower() for s in statuses]
        if party:
            sql += " AND lower(party_code) = lower(?)"
            args.append(party)
        if number:
            sql += " AND number LIKE ?"
            args.append(f"%{number}%")
        sql += " ORDER BY doc_date, number"
        return [json.loads(r["data"]) for r in self._all(sql, args)]

    def document_by_guid(self, endpoint, guid):
        rows = self._all("SELECT data FROM ul_documents WHERE kind = ? AND guid = ?",
                         (endpoint, guid))
        return json.loads(rows[0]["data"]) if rows else None

    def document_count(self, endpoint):
        return self._all("SELECT COUNT(*) AS n FROM ul_documents WHERE kind = ?",
                         (endpoint,))[0]["n"]

    # ------------------------------------------------------------ stock snapshots

    def snapshot(self, as_at):
        rows = self._all("SELECT data FROM ul_stock_snapshots WHERE as_at = ?", (as_at,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_snapshot(self, as_at, items):
        self._run("INSERT OR REPLACE INTO ul_stock_snapshots (as_at, data, fetched_at)"
                  " VALUES (?, ?, ?)", (as_at, json.dumps(items), now_text()))

    def snapshot_dates(self):
        return {r["as_at"] for r in self._all("SELECT as_at FROM ul_stock_snapshots")
                if r["as_at"] != CURRENT}

    def snapshot_fetched_at(self, as_at):
        rows = self._all("SELECT fetched_at FROM ul_stock_snapshots WHERE as_at = ?", (as_at,))
        return rows[0]["fetched_at"] if rows else None

    def delete_snapshots(self, from_date=None, keep=None):
        """Drop dated snapshots on/after from_date, or (with keep) all not in keep."""
        if keep is not None:
            gone = [(d,) for d in self.snapshot_dates() if d not in keep]
            self._run("DELETE FROM ul_stock_snapshots WHERE as_at = ?", gone, many=True)
        elif from_date is not None:
            self._run("DELETE FROM ul_stock_snapshots WHERE as_at != ? AND as_at >= ?",
                      (CURRENT, from_date.isoformat()))

    # ------------------------------------------------------------ sync runs

    def start_run(self, kind):
        conn = self.connect()
        try:
            cur = conn.execute("INSERT INTO ul_sync_runs (kind, status, step, started_at)"
                               " VALUES (?, 'running', 'Starting', ?)", (kind, now_text()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def update_run(self, run_id, step):
        self._run("UPDATE ul_sync_runs SET step = ? WHERE id = ?", (step, run_id))

    def finish_run(self, run_id, status, detail=None, error=None):
        self._run("UPDATE ul_sync_runs SET status = ?, step = NULL, detail = ?, error = ?,"
                  " finished_at = ? WHERE id = ?", (status, detail, error, now_text(), run_id))

    def mark_interrupted(self):
        self._run("UPDATE ul_sync_runs SET status = 'failed', error = 'Interrupted (app stopped)',"
                  " finished_at = ? WHERE status = 'running'", (now_text(),))

    def runs(self, limit=10):
        return self._all("SELECT * FROM ul_sync_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_run(self):
        rows = self.runs(1)
        return rows[0] if rows else None

    def last_success(self):
        rows = self._all("SELECT * FROM ul_sync_runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def has_data(self):
        return self.last_success() is not None

    def prune_api_log(self, days):
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
        self._run("DELETE FROM unleashed_api_log WHERE timestamp < ?", (cutoff,))


class LocalStoreClient:
    """Answers the reports' API queries from the local store.

    Falls back to `live` (an UnleashedClient) for anything the store can't answer:
    date ranges older than the synced window, unknown order numbers, other endpoints.
    `fetch_snapshot(as_at)` fetches and stores a stock snapshot that isn't stored yet.
    """
    mode = "store"

    def __init__(self, store, live=None, today_fn=dt.date.today, fetch_snapshot=None):
        self.store = store
        self.live = live
        self.today_fn = today_fn
        self.fetch_snapshot = fetch_snapshot

    def clear_cache(self):
        pass

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
        if endpoint in REFERENCE_ENDPOINTS:
            return self._reference(endpoint, params)
        if endpoint == "StockOnHand":
            return self._stock(params)
        if endpoint in DOCUMENT_ENDPOINTS:
            return self._documents(endpoint, params)
        return self._live(endpoint, params, f"{endpoint} isn't stored locally")

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
            return self._live("StockOnHand", params, "Per-warehouse stock isn't stored locally")
        as_at = params.get("asAtDate")
        key = CURRENT if not as_at or as_at[:10] >= self.today_fn().isoformat() else as_at[:10]
        rows = self.store.snapshot(key)
        if rows is None:
            if not self.fetch_snapshot:
                raise UnleashedError(f"No stock snapshot stored for {key}")
            rows = self.fetch_snapshot(key)
        code = params.get("productCode")
        if code:
            rows = [r for r in rows if (r.get("ProductCode") or "").lower() == code.lower()]
        return rows

    def _documents(self, endpoint, params):
        start = params.get("startDate")
        window = self.store.get_state(f"window:{endpoint}")
        if start and window and start[:10] < window:
            return self._live(endpoint, params, f"{endpoint} before {window} aren't stored locally")
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
