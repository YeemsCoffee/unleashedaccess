"""Production runs: production techs follow a product's operating procedure on each
production order, recording what they use and the checks the procedure asks for
(weights, temperatures, confirmations) step by step. Closing the order out sends the
component totals to Unleashed as the quantities used (unleashed/assembly_writes.py).

Procedures are kept per made product (its bill of materials), since Unleashed has
none: general notes, then steps, each with instructions, the components it uses and
the fields to fill in. A run keeps the procedure it started with, so editing a
procedure doesn't change orders already being recorded. Components on the order that
no step lists are recorded at the last step; a product without a procedure gets a
single step with every component.

A batch-tracked product's order also records the batches made (number, quantity,
expiry), which close-out sends to Unleashed. If Unleashed doesn't keep them as sent,
the order is left parked ("sent") for its batches to be checked and it completed in
Unleashed.
"""
import datetime as dt
import json
import math
import uuid

from unleashed.store import utc_now

DEFAULT_STEP = "Production"
FIELD_TYPES = {"number": "Number", "text": "Text", "check": "Yes / no"}
# The most steps a procedure, and fields a step, can have.
MAX_STEPS, MAX_FIELDS = 50, 30
RECORDING, CLOSING, CLOSED, FAILED = "recording", "closing", "closed", "failed"
SENT = "sent"  # quantities in Unleashed; its batches are checked, and it's completed, there
MAX_BATCHES = 50
# Completing an order that stopped mid-way (the app restarted) can be retried after this.
STALE_CLOSING_SECONDS = 10 * 60


class RunError(ValueError):
    """A problem to show the person (missing quantity, order already closed)."""


def parse_procedure(data):
    """{"notes", "steps"} from stored JSON. Older records hold just a list of steps."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, list):
        data = {"notes": "", "steps": data}
    return {"notes": data.get("notes") or "",
            "steps": [{"name": s.get("name") or "", "instructions": s.get("instructions") or "",
                       "components": list(s.get("components") or []),
                       "fields": [dict(f) for f in s.get("fields") or []]}
                      for s in data.get("steps") or []]}


class ProductionRuns:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ procedures

    def procedure_for(self, product_code):
        rows = self.db.query("SELECT steps FROM prod_steps WHERE product_code = ?", (product_code,))
        return parse_procedure(rows[0]["steps"]) if rows else {"notes": "", "steps": []}

    def set_procedure(self, product_code, notes, steps, user):
        """Replace a product's procedure. `steps`: [{"name", "instructions", "components":
        [codes], "fields": [{"id", "label", "type", "unit", "min", "max", "required"}]}].
        Steps without a name and fields without a label are dropped."""
        clean = []
        for s in steps:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            fields, ids = [], set()
            for f in s.get("fields") or []:
                label = (f.get("label") or "").strip()
                if not label:
                    continue
                kind = f.get("type") if f.get("type") in FIELD_TYPES else "number"
                field_id = f.get("id") or ""
                if not field_id or field_id in ids:
                    field_id = uuid.uuid4().hex[:8]
                ids.add(field_id)
                number = kind == "number"
                fields.append({"id": field_id, "label": label[:100], "type": kind,
                               "unit": (f.get("unit") or "").strip()[:20] if number else "",
                               "min": _number(f.get("min")) if number else None,
                               "max": _number(f.get("max")) if number else None,
                               "required": bool(f.get("required"))})
            clean.append({"name": name[:100], "instructions": (s.get("instructions") or "").strip()[:5000],
                          "components": list(dict.fromkeys(s.get("components") or [])),
                          "fields": fields})
        notes = (notes or "").strip()[:5000]
        if not clean and not notes:
            self.db.execute("DELETE FROM prod_steps WHERE product_code = ?", (product_code,))
            return
        self.db.execute(
            "INSERT INTO prod_steps (product_code, steps, updated_by, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (product_code) DO UPDATE SET steps = excluded.steps,"
            " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (product_code, json.dumps({"notes": notes, "steps": clean}), user, utc_now()))

    # ------------------------------------------------------------ runs

    def run(self, guid):
        rows = self.db.query("SELECT * FROM prod_runs WHERE assembly_guid = ?", (guid,))
        if not rows:
            return None
        run = dict(rows[0])
        run["procedure"] = parse_procedure(run["steps"])
        run["sent"] = json.loads(run["sent"]) if run["sent"] else None
        return run

    def records(self, guid):
        entries = {r["step_index"]: json.loads(r["entries"]) for r in self.db.query(
            "SELECT step_index, entries FROM prod_step_entries WHERE assembly_guid = ?", (guid,))}
        return {r["step_index"]: {"quantities": json.loads(r["quantities"]),
                                  "entries": entries.get(r["step_index"], {}),
                                  "by": r["recorded_by"], "at": r["recorded_at"]}
                for r in self.db.query("SELECT * FROM prod_step_records WHERE assembly_guid = ?",
                                       (guid,))}

    def view(self, order):
        """An order's procedure, what's been recorded at each step, and totals per component.

        `order` is a production order with "components" (Production.order): what Unleashed
        calculated it needs, from the bill of materials.
        """
        run = self.run(order["guid"])
        procedure = run["procedure"] if run else self.procedure_for(order["product_code"])
        definition = procedure["steps"] or [{"name": DEFAULT_STEP, "instructions": "",
                                             "components": [], "fields": []}]
        records = self.records(order["guid"]) if run else {}
        # The recipe quantity: Unleashed's calculation while open; once completed or sent,
        # the components hold what was used, and "planned" holds the recipe.
        sent = order["completed"] or (run is not None and run["status"] == SENT)
        components = {c["product_code"]: dict(c, recipe=c["planned"] if sent
                                              and c.get("planned") is not None else c["consumed"])
                      for c in order["components"]}
        steps, listed = [], set()
        for i, s in enumerate(definition):
            codes = [c for c in s["components"] if c in components]  # may repeat across steps
            listed.update(codes)
            steps.append({"index": i, "name": s["name"], "instructions": s["instructions"],
                          "codes": codes, "fields": s["fields"]})
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
            s["fields"] = [_field_view(f, rec["entries"].get(f["id"]) if rec else None)
                           for f in s["fields"]]
            for code, qty in (rec["quantities"] if rec else {}).items():
                if code in recorded:
                    recorded[code] += qty
            s["current"] = not rec and current is None
            if s["current"]:
                current = s["index"]
        status, error = (run["status"], run["error"]) if run else (RECORDING, None)
        if status == CLOSING and run["updated_at"] < utc_now(-STALE_CLOSING_SECONDS):
            status, error = FAILED, ("Completing this order was interrupted before it finished. Check "
                                     "it in Unleashed, then complete it again.")
        # Steps that record something must be saved before completing; the others needn't be.
        blocking = [s for s in steps if not s["done"]
                    and (s["rows"] or any(f["required"] for f in s["fields"]))]
        return {
            "procedure": {"notes": procedure["notes"], "steps": definition},
            "notes": procedure["notes"], "steps": steps, "started": run is not None,
            "status": status, "error": error, "locked": status in (CLOSING, CLOSED, SENT),
            "all_done": current is None, "blocking": blocking, "can_complete": not blocking,
            "closed_by": run["closed_by"] if run else None,
            "closed_at": run["closed_at"] if run else None, "sent": run["sent"] if run else None,
            "out_of_range": [dict(f, step=s["name"]) for s in steps for f in s["fields"]
                             if f["out_of_range"]],
            "totals": [{"product_code": code, "description": c["description"], "recipe": c["recipe"],
                        "recorded": recorded[code], "difference": recorded[code] - c["recipe"]}
                       for code, c in components.items()],
        }

    def save_step(self, order, index, quantities, entries, user):
        """Record step `index`: {component code: quantity used} for all its components, and
        {field id: value as entered} for the fields its procedure asks for."""
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
        checks = {f["id"]: _entry(f, entries.get(f["id"])) for f in step["fields"]}
        guid = order["guid"]
        with self.db.connect() as conn:
            _start_run(conn, order, view)
            # Only while the order is open here: never after completing has started.
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
            conn.execute(
                "INSERT INTO prod_step_entries (assembly_guid, step_index, entries) VALUES (?, ?, ?)"
                " ON CONFLICT (assembly_guid, step_index) DO UPDATE SET entries = excluded.entries",
                (guid, index, json.dumps(checks)))

    # ------------------------------------------------------------ close-out

    def begin_close(self, order, view):
        """Claim the order for completing; False if someone else is completing it or it's done."""
        with self.db.connect() as conn:
            _start_run(conn, order, view)
            return conn.execute(
                "UPDATE prod_runs SET status = ?, error = NULL, updated_at = ? WHERE assembly_guid = ?"
                " AND (status IN (?, ?) OR (status = ? AND updated_at < ?))",
                (CLOSING, utc_now(), order["guid"], RECORDING, FAILED, CLOSING,
                 utc_now(-STALE_CLOSING_SECONDS))) == 1

    def close_failed(self, guid, error):
        self.db.execute("UPDATE prod_runs SET status = ?, error = ?, updated_at = ?"
                        " WHERE assembly_guid = ?", (FAILED, error, utc_now(), guid))

    def planned_quantities(self):
        """{assembly guid: quantity planned} for orders completed or sent from here (Unleashed
        then holds the quantity made), for working out yield."""
        planned = {}
        for r in self.db.query("SELECT assembly_guid, sent FROM prod_runs WHERE status IN (?, ?)",
                               (CLOSED, SENT)):
            sent = json.loads(r["sent"]) if r["sent"] else {}
            if sent.get("planned"):
                planned[r["assembly_guid"]] = sent["planned"]
        return planned

    def closed(self, guid, sent, user, status=CLOSED):
        """Record a completed order (or with SENT, one left parked for its batches to be
        entered in Unleashed): `sent` is what Unleashed now holds, plus "planned" (and
        "batches")."""
        now = utc_now()
        self.db.execute("UPDATE prod_runs SET status = ?, error = NULL, sent = ?, closed_by = ?,"
                        " closed_at = ?, updated_at = ? WHERE assembly_guid = ?",
                        (status, json.dumps(sent), user, now, now, guid))


def check_batches(recorded, unleashed):
    """Batches recorded here against the ones Unleashed holds for the finished product,
    matched by number: [{"number", "recorded", "unleashed", "ok"}], where "recorded" or
    "unleashed" is None if only the other has the batch."""
    theirs = {(b["number"] or "").lower(): b for b in unleashed}
    rows = []
    for b in recorded:
        u = theirs.pop(b["number"].lower(), None)
        ok = (u is not None and abs(u["qty"] - b["qty"]) <= 0.0051
              and (u["expiry"].isoformat() if u["expiry"] else None) == b["expiry"])
        rows.append({"number": b["number"], "recorded": b, "unleashed": u, "ok": ok})
    rows += [{"number": u["number"], "recorded": None, "unleashed": u, "ok": False} for u in theirs.values()]
    return rows

def parse_batches(rows, made, made_on):
    """The batches made, [{"number", "qty", "expiry"}], from (number, quantity, expiry)
    rows as typed on the Complete page. Empty rows are skipped; a lone batch without a
    quantity is everything made. Raises RunError."""
    filled = []
    for number, qty_text, expiry_text in rows:
        number, qty_text, expiry_text = ((v or "").strip() for v in (number, qty_text, expiry_text))
        if number or qty_text or expiry_text:
            filled.append((number, qty_text, expiry_text))
    if not filled:
        raise RunError("Enter the batch number and expiry date of what was made.")
    if len(filled) > MAX_BATCHES:
        raise RunError(f"Enter at most {MAX_BATCHES} batches.")
    batches, seen = [], set()
    for number, qty_text, expiry_text in filled:
        if not number:
            raise RunError("Enter a batch number for each batch.")
        if len(number) > 50:
            raise RunError(f"Batch number {number[:50]}… is too long (50 characters at most).")
        if number.lower() in seen:
            raise RunError(f"Batch {number} is entered twice.")
        seen.add(number.lower())
        qty = made if not qty_text and len(filled) == 1 else _number(qty_text)
        if qty is None or qty <= 0:
            raise RunError(f"Enter how much is in batch {number} (more than 0).")
        try:
            expiry = dt.date.fromisoformat(expiry_text)
        except ValueError:
            raise RunError(f"Enter the expiry date of batch {number}.") from None
        if expiry < made_on:
            raise RunError(f"Batch {number} would expire before the day it was made.")
        batches.append({"number": number, "qty": qty, "expiry": expiry.isoformat()})
    total = sum(b["qty"] for b in batches)
    if abs(total - made) > 0.0051:
        raise RunError(f"The batches add up to {total:g}, but {made:g} was made. Make them match.")
    return batches


def _start_run(conn, order, view):
    """The order's run row, created with the procedure it starts with if it has none."""
    conn.execute(
        "INSERT INTO prod_runs (assembly_guid, assembly_number, steps, status, updated_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (assembly_guid) DO NOTHING",
        (order["guid"], order["number"], json.dumps(view["procedure"]), RECORDING, utc_now()))


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _entry(field, raw):
    """A field's value as entered (form text, or "on" for a ticked box), checked."""
    label = field["label"]
    if field["type"] == "check":
        value = raw in ("on", "1", "true", True)
        if field["required"] and not value:
            raise RunError(f"Confirm “{label}” before saving this step.")
        return value
    text = raw.strip() if isinstance(raw, str) else raw
    if field["type"] == "number":
        if text in (None, ""):
            value = None
        else:
            value = _number(text)
            if value is None:
                raise RunError(f"{label} needs a number.")
    else:
        value = (text or "")[:500] or None
    if field["required"] and value is None:
        raise RunError(f"Enter {label} before saving this step.")
    return value


def _field_view(field, value):
    low, high, unit = field.get("min"), field.get("max"), field.get("unit") or ""
    if low is not None and high is not None:
        target = f"{low:g}–{high:g}"
    elif low is not None:
        target = f"at least {low:g}"
    elif high is not None:
        target = f"at most {high:g}"
    else:
        target = ""
    out = field["type"] == "number" and value is not None and (
        (low is not None and value < low) or (high is not None and value > high))
    return dict(field, value=value, out_of_range=out, target=f"{target} {unit}".strip() if target else "")
