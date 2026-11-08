"""Stock counts, kept in Core's database.

Unleashed's API only lists the counts made in Unleashed (GET /StockCounts), so a count is
made here: when it starts, each product in the warehouse (or in one product group) gets a
line with what Unleashed says is there (per batch, for a batch-tracked product). People enter
what they count, over as many saves as it takes and several at once. Posting it sends
Unleashed one completed stock adjustment with the differences (stock_pages.py): each counted
line moves the stock there now by counted - expected, so sales and receipts during the count
aren't undone.
"""
import json

from unleashed.store import utc_now

STATUSES = {"counting": "Counting", "posted": "Posted", "cancelled": "Cancelled"}


class CountError(ValueError):
    """A problem to show the person."""


class StockCounts:
    def __init__(self, db):
        self.db = db

    def start(self, name, warehouse, group, rows, by):
        """A new count of `rows`: [{"product_code", "description", "group", "expected",
        "avg_cost", "batches": [{"number", "qty", "expiry"}] or None}]. Returns its id."""
        name = " ".join((name or "").split())[:150]
        if not name:
            raise CountError("Give the count a name.")
        if not warehouse:
            raise CountError("Choose the warehouse being counted.")
        if not rows:
            raise CountError("There's nothing to count there" + (f" in {group}." if group else "."))
        with self.db.connect() as conn:
            count_id = conn.query(
                "INSERT INTO stock_counts (name, warehouse, product_group, status, created_by, created_at)"
                " VALUES (?, ?, ?, 'counting', ?, ?) RETURNING id", (name, warehouse, group or None, by, utc_now()))[0]["id"]
            for r in rows:
                for b in r.get("batches") or [None]:
                    conn.execute(
                        "INSERT INTO stock_count_lines (count_id, product_code, description, product_group, batch_number,"
                        " expiry_date, expected_qty, avg_cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (count_id, r["product_code"], r.get("description"), r.get("group") or None,
                         b["number"] if b else None, b.get("expiry") if b else None,
                         float(b["qty"] if b else r.get("expected") or 0), r.get("avg_cost")))
        return count_id

    def counts(self, status=None):
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM stock_count_lines l WHERE l.count_id = c.id) AS lines,"
               " (SELECT COUNT(*) FROM stock_count_lines l WHERE l.count_id = c.id AND l.counted_qty IS NOT NULL)"
               " AS counted FROM stock_counts c")
        args = ()
        if status:
            sql += " WHERE c.status = ?"
            args = (status,)
        return self.db.query(sql + " ORDER BY c.created_at DESC, c.id DESC", args)

    def count(self, count_id):
        rows = self.db.query("SELECT * FROM stock_counts WHERE id = ?", (count_id,))
        if not rows:
            return None
        count = dict(rows[0])
        count["lines"] = self.db.query(
            "SELECT * FROM stock_count_lines WHERE count_id = ?"
            " ORDER BY lower(COALESCE(product_group, '')), lower(product_code), expiry_date, batch_number, id", (count_id,))
        for l in count["lines"]:
            l["difference"] = None if l["counted_qty"] is None else l["counted_qty"] - l["expected_qty"]
            l["value_difference"] = None if l["difference"] is None else l["difference"] * (l["avg_cost"] or 0)
        counted = [l for l in count["lines"] if l["counted_qty"] is not None]
        count["counted"] = len(counted)
        count["differences"] = [l for l in counted if abs(l["difference"]) > 1e-9]
        count["value_difference"] = sum(l["value_difference"] for l in counted)
        count["status_label"] = STATUSES.get(count["status"], count["status"])
        return count

    def record(self, count_id, entries, by):
        """Keep what was counted: `entries` [(line id, typed, as it was when the page opened)].
        Only lines whose value was changed on the page are saved, so people counting at once
        don't undo each other. Blank clears a count. Returns how many lines changed."""
        count = self.count(count_id)
        if count is None:
            raise CountError("That count isn't here any more.")
        if count["status"] != "counting":
            raise CountError(f"This count is {count['status_label'].lower()}: it can't change.")
        ids = {l["id"] for l in count["lines"]}
        changed, now = 0, utc_now()
        with self.db.connect() as conn:
            for line_id, typed, was in entries:
                typed, was = (typed or "").strip(), (was or "").strip()
                if line_id not in ids or typed == was:
                    continue
                qty = None
                if typed:
                    try:
                        qty = float(typed.replace(",", ""))
                    except ValueError:
                        raise CountError(f"“{typed}” isn't a number.") from None
                    if qty < 0:
                        raise CountError("A count can't be below 0.")
                conn.execute("UPDATE stock_count_lines SET counted_qty = ?, counted_by = ?, counted_at = ?"
                             " WHERE id = ? AND count_id = ?",
                             (qty, by if qty is not None else None, now if qty is not None else None, line_id, count_id))
                changed += 1
        return changed

    def posted(self, count_id, reason, adjustment_number, by):
        self.db.execute("UPDATE stock_counts SET status = 'posted', reason = ?, adjustment_number = ?, posted_by = ?,"
                        " posted_at = ? WHERE id = ? AND status = 'counting'",
                        (reason, adjustment_number, by, utc_now(), count_id))

    def cancel(self, count_id):
        self.db.execute("UPDATE stock_counts SET status = 'cancelled' WHERE id = ? AND status = 'counting'", (count_id,))


def differences(count):
    """{product code: {"change": counted - expected over its counted lines, "batches":
    {batch number: change} for a batch-tracked product, "description"}}, for the products
    with a counted difference."""
    out = {}
    for l in count["lines"]:
        if l["counted_qty"] is None:
            continue
        p = out.setdefault(l["product_code"], {"change": 0.0, "batches": None, "description": l["description"],
                                               "avg_cost": l["avg_cost"]})
        change = l["counted_qty"] - l["expected_qty"]
        p["change"] += change
        if l["batch_number"]:
            p["batches"] = p["batches"] or {}
            p["batches"][l["batch_number"]] = p["batches"].get(l["batch_number"], 0.0) + change
    return {code: p for code, p in out.items()
            if abs(p["change"]) > 1e-9 or any(abs(v) > 1e-9 for v in (p["batches"] or {}).values())}


def dumps(value):
    return json.dumps(value, separators=(",", ":"))
