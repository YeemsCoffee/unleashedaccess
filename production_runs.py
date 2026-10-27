"""Production runs: the team records what it actually uses at each step of a
production order, and closing the order out sends the totals to Unleashed as the
quantities used (unleashed/assembly_writes.py).

Steps are set per made product (its bill of materials), since Unleashed has none.
A run keeps the steps it started with, so editing a recipe's steps doesn't change
orders already being recorded. Components on the order that no step lists are
recorded at the last step; a product without steps gets a single step with every
component.
"""
import json

from unleashed.store import utc_now

DEFAULT_STEP = "Production"
RECORDING, CLOSING, CLOSED, FAILED = "recording", "closing", "closed", "failed"
# A close-out that stopped mid-way (the app restarted) can be retried after this.
STALE_CLOSING_SECONDS = 10 * 60


class RunError(ValueError):
    """A problem to show the person (missing quantity, order already closed)."""


class ProductionRuns:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ steps per product

    def steps_for(self, product_code):
        rows = self.db.query("SELECT steps FROM prod_steps WHERE product_code = ?", (product_code,))
        return json.loads(rows[0]["steps"]) if rows else []

    def set_steps(self, product_code, steps, user):
        """Replace a product's steps: [{"name", "components": [codes]}]; unnamed steps are dropped."""
        steps = [{"name": s["name"].strip(), "components": list(dict.fromkeys(s["components"]))}
                 for s in steps if s["name"].strip()]
        if not steps:
            self.db.execute("DELETE FROM prod_steps WHERE product_code = ?", (product_code,))
            return
        self.db.execute(
            "INSERT INTO prod_steps (product_code, steps, updated_by, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (product_code) DO UPDATE SET steps = excluded.steps,"
            " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (product_code, json.dumps(steps), user, utc_now()))

    # ------------------------------------------------------------ runs

    def run(self, guid):
        rows = self.db.query("SELECT * FROM prod_runs WHERE assembly_guid = ?", (guid,))
        if not rows:
            return None
        run = dict(rows[0])
        run["steps"] = json.loads(run["steps"])
        run["sent"] = json.loads(run["sent"]) if run["sent"] else None
        return run

    def records(self, guid):
        return {r["step_index"]: {"quantities": json.loads(r["quantities"]), "by": r["recorded_by"],
                                  "at": r["recorded_at"]}
                for r in self.db.query("SELECT * FROM prod_step_records WHERE assembly_guid = ?",
                                       (guid,))}

    def view(self, order):
        """An order's steps and what's been recorded at each, plus totals per component.

        `order` is a production order with "components" (Production.order): what Unleashed
        calculated it needs, from the bill of materials.
        """
        run = self.run(order["guid"])
        definition = (run["steps"] if run else self.steps_for(order["product_code"])) \
            or [{"name": DEFAULT_STEP, "components": []}]
        records = self.records(order["guid"]) if run else {}
        # The recipe quantity: Unleashed's calculation while open; once completed, the
        # components hold what was used, and "planned" holds the recipe.
        components = {c["product_code"]: dict(c, recipe=c["planned"] if order["completed"]
                                              and c.get("planned") is not None else c["consumed"])
                      for c in order["components"]}
        steps, listed = [], set()
        for i, s in enumerate(definition):
            codes = [c for c in s["components"] if c in components]  # may repeat across steps
            listed.update(codes)
            steps.append({"index": i, "name": s["name"], "codes": codes})
        steps[-1]["codes"] += [c for c in components if c not in listed]

        recorded = {code: 0.0 for code in components}
        current = None
        for s in steps:
            rec = records.get(s["index"])
            s["done"] = rec is not None
            s["by"], s["at"] = (rec["by"], rec["at"]) if rec else (None, None)
            s["rows"] = [{"product_code": code, "description": components[code]["description"],
                          "recipe": components[code]["recipe"],
                          "qty": rec["quantities"].get(code) if rec else None} for code in s["codes"]]
            for code, qty in (rec["quantities"] if rec else {}).items():
                if code in recorded:
                    recorded[code] += qty
            s["current"] = not rec and current is None
            if s["current"]:
                current = s["index"]
        status = run["status"] if run else RECORDING
        return {
            "definition": definition, "steps": steps, "started": run is not None,
            "status": status, "error": run["error"] if run else None,
            "locked": status in (CLOSING, CLOSED), "all_done": current is None,
            "closed_by": run["closed_by"] if run else None,
            "closed_at": run["closed_at"] if run else None, "sent": run["sent"] if run else None,
            "totals": [{"product_code": code, "description": c["description"], "recipe": c["recipe"],
                        "recorded": recorded[code], "difference": recorded[code] - c["recipe"]}
                       for code, c in components.items()],
        }

    def save_step(self, order, index, quantities, user):
        """Record what was used at step `index`: {component code: quantity} (all its components)."""
        view = self.view(order)
        step = next((s for s in view["steps"] if s["index"] == index), None)
        if step is None:
            raise RunError("That step isn't part of this order.")
        values = {}
        for row in step["rows"]:
            qty = quantities.get(row["product_code"])
            if qty is None:
                raise RunError(f"Enter how much {row['description'] or row['product_code']} was used "
                               "at this step (0 if none).")
            if qty < 0:
                raise RunError("Quantities can't be negative.")
            values[row["product_code"]] = qty
        guid = order["guid"]
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO prod_runs (assembly_guid, assembly_number, steps, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (assembly_guid) DO NOTHING",
                (guid, order["number"], json.dumps(view["definition"]), RECORDING, utc_now()))
            # Only while the order is open here: never after close-out has started.
            if conn.execute("UPDATE prod_runs SET updated_at = ? WHERE assembly_guid = ?"
                            " AND status IN (?, ?)", (utc_now(), guid, RECORDING, FAILED)) != 1:
                raise RunError("This order is being closed out or is already closed, so its steps "
                               "can't change.")
            conn.execute(
                "INSERT INTO prod_step_records (assembly_guid, step_index, quantities, recorded_by,"
                " recorded_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (assembly_guid, step_index)"
                " DO UPDATE SET quantities = excluded.quantities, recorded_by = excluded.recorded_by,"
                " recorded_at = excluded.recorded_at",
                (guid, index, json.dumps(values), user, utc_now()))

    # ------------------------------------------------------------ close-out

    def begin_close(self, guid):
        """Claim the order for close-out; False if someone else is closing it or it's closed."""
        return self.db.execute(
            "UPDATE prod_runs SET status = ?, error = NULL, updated_at = ? WHERE assembly_guid = ?"
            " AND (status IN (?, ?) OR (status = ? AND updated_at < ?))",
            (CLOSING, utc_now(), guid, RECORDING, FAILED, CLOSING,
             utc_now(-STALE_CLOSING_SECONDS))) == 1

    def close_failed(self, guid, error):
        self.db.execute("UPDATE prod_runs SET status = ?, error = ?, updated_at = ?"
                        " WHERE assembly_guid = ?", (FAILED, error, utc_now(), guid))

    def closed(self, guid, sent, user):
        now = utc_now()
        self.db.execute("UPDATE prod_runs SET status = ?, error = NULL, sent = ?, closed_by = ?,"
                        " closed_at = ?, updated_at = ? WHERE assembly_guid = ?",
                        (CLOSED, json.dumps(sent), user, now, now, guid))
