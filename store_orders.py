"""Store ordering: stores ask the warehouse for stock.

A store is an Unleashed customer ticked as a store (Settings -> COGS). Each store has par levels
kept here, in Core: for each product it carries, how many it should have after a delivery. Store
staff count what they have, and the request is the difference (par - on hand), which they can
change. The warehouse reviews a request and Core makes the sales order to that store in
Unleashed; from then on it's an ordinary sales order (picked, completed, counted in the store's
COGS).
"""
from unleashed.store import utc_now

MAX_LINES = 300
STATUSES = {"submitted": "Waiting for the warehouse", "ordered": "Sales order made", "cancelled": "Cancelled"}


class StoreOrderError(ValueError):
    """A problem to show the person."""


def _qty(value, label, required=False):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        if required:
            raise StoreOrderError(f"{label} is needed.")
        return None
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        raise StoreOrderError(f"{label} must be a number.") from None
    if number < 0 or number > 1_000_000:
        raise StoreOrderError(f"{label} must be between 0 and 1,000,000.")
    return number


class StoreOrders:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ par levels

    def pars(self, store):
        """[{"product_code", "par"}] for a store, in the order they're listed."""
        return self.db.query("SELECT product_code, par FROM store_pars WHERE store_code = ? ORDER BY position, product_code",
                             (store,))

    def stores_with_pars(self):
        return {r["store_code"]: r["n"] for r in
                self.db.query("SELECT store_code, COUNT(*) AS n FROM store_pars GROUP BY store_code")}

    def save_pars(self, store, rows, known, by):
        """Replace a store's par levels with `rows` [(product code, par as typed)]: blank rows are
        left out, a par of 0 takes the product off. `known`: the product codes that exist."""
        clean, seen = [], set()
        for code, par in rows[:MAX_LINES]:
            code = (code or "").strip()
            if not code and not str(par or "").strip():
                continue
            if code not in known:
                raise StoreOrderError(f"{code or 'A row'}: pick a product from the list.")
            if code in seen:
                raise StoreOrderError(f"{code} is on the list twice.")
            amount = _qty(par, f"The par for {code}", required=True)
            seen.add(code)
            if amount:
                clean.append((code, amount))
        with self.db.connect() as conn:
            conn.execute("DELETE FROM store_pars WHERE store_code = ?", (store,))
            for i, (code, amount) in enumerate(clean):
                conn.execute("INSERT INTO store_pars (store_code, product_code, par, position, updated_by, updated_at)"
                             " VALUES (?, ?, ?, ?, ?, ?)", (store, code, amount, i, by, utc_now()))
        return len(clean)

    # ------------------------------------------------------------ requests

    def submit(self, store, lines, notes, needed_by, by, known):
        """A store's request: `lines` [{"product_code", "on_hand", "qty"}] as typed (a line asks for
        nothing when its quantity is empty or 0). Returns its id."""
        wanted = []
        for l in lines[:MAX_LINES]:
            code = (l.get("product_code") or "").strip()
            if not code:
                continue
            if code not in known:
                raise StoreOrderError(f"{code}: pick a product from the list.")
            qty = _qty(l.get("qty"), f"The quantity of {code}")
            on_hand = _qty(l.get("on_hand"), f"What's on hand of {code}")
            if qty:
                wanted.append((code, on_hand, qty))
        if not wanted:
            raise StoreOrderError("Nothing is asked for: put a quantity against at least one product.")
        with self.db.connect() as conn:
            request_id = conn.query(
                "INSERT INTO store_requests (store_code, status, notes, needed_by, requested_by, requested_at)"
                " VALUES (?, 'submitted', ?, ?, ?, ?) RETURNING id",
                (store, (notes or "").strip()[:1000] or None, needed_by, by, utc_now()))[0]["id"]
            for i, (code, on_hand, qty) in enumerate(wanted):
                conn.execute("INSERT INTO store_request_lines (request_id, product_code, on_hand, qty, position)"
                             " VALUES (?, ?, ?, ?, ?)", (request_id, code, on_hand, qty, i))
        return request_id

    def requests(self, store=None, status=None, limit=200):
        sql = ("SELECT r.*, (SELECT COUNT(*) FROM store_request_lines l WHERE l.request_id = r.id) AS lines"
               " FROM store_requests r")
        where, args = [], []
        if store:
            where.append("r.store_code = ?")
            args.append(store)
        if status:
            where.append("r.status = ?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE r.status WHEN 'submitted' THEN 0 ELSE 1 END, r.id DESC LIMIT ?"
        return [dict(r, status_label=STATUSES.get(r["status"], r["status"])) for r in self.db.query(sql, args + [limit])]

    def request(self, request_id):
        rows = self.db.query("SELECT * FROM store_requests WHERE id = ?", (request_id,))
        if not rows:
            return None
        found = dict(rows[0], status_label=STATUSES.get(rows[0]["status"], rows[0]["status"]))
        found["lines"] = self.db.query("SELECT * FROM store_request_lines WHERE request_id = ? ORDER BY position",
                                       (request_id,))
        return found

    def ordered(self, request_id, sales_order, by):
        return self.db.execute("UPDATE store_requests SET status = 'ordered', sales_order = ?, handled_by = ?,"
                               " handled_at = ? WHERE id = ? AND status = 'submitted'",
                               (sales_order, by, utc_now(), request_id)) == 1

    def cancel(self, request_id, why, by):
        return self.db.execute("UPDATE store_requests SET status = 'cancelled', cancel_reason = ?, handled_by = ?,"
                               " handled_at = ? WHERE id = ? AND status = 'submitted'",
                               ((why or "").strip()[:300] or None, by, utc_now(), request_id)) == 1
