"""The waste log: spoiled, expired, spilled or made-wrong stock, recorded from a phone.

Each record is posted to Unleashed as one completed stock adjustment (so stock and COGS move as
for any write-off), and kept here with its reason, so waste can be seen by reason and product.
"""
from unleashed.store import utc_now

REASONS = ("Spoiled", "Expired", "Damaged", "Spilled or dropped", "Made wrong", "Tasting or samples", "Other")
MAX_LINES = 40


class WasteLog:
    def __init__(self, db):
        self.db = db

    def record(self, day, warehouse, lines, adjustment, by):
        """Keep what was posted: `lines` [{"product_code", "description", "qty", "value", "reason", "note"}]
        (qty and value positive: what was lost)."""
        with self.db.connect() as conn:
            for l in lines:
                conn.execute("INSERT INTO waste_entries (day, warehouse, product_code, description, qty, value, reason,"
                             " note, adjustment_number, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (day.isoformat(), warehouse, l["product_code"], l.get("description"), l["qty"], l["value"],
                              l["reason"], l.get("note"), adjustment, by, utc_now()))

    def entries(self, start, end):
        return self.db.query("SELECT * FROM waste_entries WHERE day >= ? AND day <= ? ORDER BY day DESC, id DESC",
                             (start.isoformat(), end.isoformat()))

    def summary(self, start, end):
        """{"total", "by_reason": [{"reason", "value", "lines"}], "by_product": [{"product_code",
        "description", "qty", "value"}], "entries"} for start..end."""
        entries = self.entries(start, end)
        by_reason, by_product = {}, {}
        for e in entries:
            r = by_reason.setdefault(e["reason"], {"reason": e["reason"], "value": 0.0, "lines": 0})
            r["value"] += e["value"] or 0
            r["lines"] += 1
            p = by_product.setdefault(e["product_code"], {"product_code": e["product_code"],
                                                          "description": e["description"], "qty": 0.0, "value": 0.0})
            p["qty"] += e["qty"] or 0
            p["value"] += e["value"] or 0
        return {"total": sum(e["value"] or 0 for e in entries), "entries": entries,
                "by_reason": sorted(by_reason.values(), key=lambda r: -r["value"]),
                "by_product": sorted(by_product.values(), key=lambda p: -p["value"])}
