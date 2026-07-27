"""Core scan engine: code resolution, count math, and event logging.

Every count change goes through _apply_delta + _log_event so scan_events is a
complete audit trail. No function here changes a count silently.
"""
import datetime

from db import now

# Container statuses that count toward the premade item's current_count.
ACTIVE_STATUSES = ("MADE", "IN_USE")
TERMINAL_STATUSES = ("FINISHED", "WASTED")

CONTAINER_ACTIONS = {
    "IN_USE": {"status": "IN_USE", "stamp": "in_use_at", "delta": 0, "event": "CONTAINER_IN_USE"},
    "FINISHED": {"status": "FINISHED", "stamp": "finished_at", "delta": -1, "event": "CONTAINER_FINISHED"},
    "WASTED": {"status": "WASTED", "stamp": "wasted_at", "delta": -1, "event": "CONTAINER_WASTED"},
}


def normalize_code(raw):
    return (raw or "").strip().upper()


def handle_scan(db, store_code, raw_code, employee_id=None, duplicate_window_seconds=10):
    """Resolve a scanned code and apply its default action.

    Returns a dict with a 'status' key:
      ok           - action applied, show the message
      needs_action - existing premade container; caller shows big buttons
      duplicate    - same code scanned again within the guard window; ignored
      unknown      - code not recognized
      error        - recognized but not actionable (e.g. generic premade code)
    """
    code = normalize_code(raw_code)
    if not code:
        return {"status": "error", "message": "Empty code."}

    # 1. Exact item code match
    item = db.execute("SELECT * FROM items WHERE item_code = ?", (code,)).fetchone()
    if item is not None:
        if not item["active"]:
            return {"status": "error", "message": f"{item['display_name']} is deactivated."}
        if item["category"] == "RAW_INGREDIENT":
            return _use_raw_ingredient(db, store_code, item, code, employee_id, duplicate_window_seconds)
        # Generic PREMADE item code: refuse, otherwise rescans of the same
        # generic label would create a new container every time (double-count).
        return {
            "status": "error",
            "message": (
                f"{item['display_name']} needs a unique batch label "
                f"(like {item['item_code']}-YYYYMMDD-001). Print labels from the Admin screen."
            ),
        }

    # 2. Existing premade container
    container = db.execute(
        "SELECT * FROM premade_containers WHERE container_code = ?", (code,)
    ).fetchone()
    if container is not None:
        return _describe_container(db, container)

    # 3. New premade container: longest active PREMADE item_code prefix match
    premade_items = db.execute(
        "SELECT * FROM items WHERE category = 'PREMADE' AND active = 1"
    ).fetchall()
    match = None
    for it in premade_items:
        prefix = it["item_code"] + "-"
        if code.startswith(prefix) and (match is None or len(it["item_code"]) > len(match["item_code"])):
            match = it
    if match is not None:
        return _make_container(db, store_code, match, code, employee_id)

    return {
        "status": "unknown",
        "message": f"Code {code} not recognized. Add the item in Admin, or check the label.",
    }


def _use_raw_ingredient(db, store_code, item, code, employee_id, window_seconds):
    if _is_duplicate_scan(db, store_code, code, "USE_ONE", window_seconds):
        return {
            "status": "duplicate",
            "message": f"{item['display_name']} was already scanned a moment ago — ignored.",
        }
    before, after = _apply_delta(db, store_code, item["id"], -1)
    _log_event(db, store_code, code, action="USE_ONE", item_id=item["id"],
               delta=-1, before=before, after=after, employee_id=employee_id)
    db.commit()
    unit = item["unit_of_measure"]
    return {
        "status": "ok",
        "kind": "raw_used",
        "message": f"{item['display_name']}: used 1 {unit}. New count: {_fmt(after)}.",
        "item_name": item["display_name"],
        "new_count": after,
    }


def _make_container(db, store_code, item, code, employee_id):
    ts = now()
    cur = db.execute(
        "INSERT INTO premade_containers (container_code, item_id, store_code, status, made_at)"
        " VALUES (?, ?, ?, 'MADE', ?)",
        (code, item["id"], store_code, ts),
    )
    before, after = _apply_delta(db, store_code, item["id"], +1)
    _log_event(db, store_code, code, action="CONTAINER_MADE", item_id=item["id"],
               container_id=cur.lastrowid, delta=+1, before=before, after=after,
               employee_id=employee_id)
    db.commit()
    return {
        "status": "ok",
        "kind": "container_made",
        "message": f"{item['display_name']} marked MADE. Active batches: {_fmt(after)}.",
        "item_name": item["display_name"],
        "container_code": code,
        "new_count": after,
    }


def _describe_container(db, container):
    item = db.execute("SELECT * FROM items WHERE id = ?", (container["item_id"],)).fetchone()
    status = container["status"]
    if status in TERMINAL_STATUSES:
        stamp = container["finished_at"] if status == "FINISHED" else container["wasted_at"]
        return {
            "status": "ok",
            "kind": "container_info",
            "message": f"{item['display_name']} ({container['container_code']}) was already "
                       f"marked {status} at {stamp}. Nothing changed.",
        }
    actions = [{"action": "IN_USE", "label": "Mark In Use"}] if status == "MADE" else []
    actions += [
        {"action": "FINISHED", "label": "Mark Finished"},
        {"action": "WASTED", "label": "Mark Wasted"},
    ]
    return {
        "status": "needs_action",
        "kind": "container",
        "message": f"{item['display_name']} — currently {status}. What happened?",
        "item_name": item["display_name"],
        "container_code": container["container_code"],
        "container_status": status,
        "actions": actions,
    }


def apply_container_action(db, store_code, container_code, action, employee_id=None):
    """Apply IN_USE / FINISHED / WASTED to an existing container."""
    code = normalize_code(container_code)
    spec = CONTAINER_ACTIONS.get(action)
    if spec is None:
        return {"status": "error", "message": f"Unknown action {action}."}
    container = db.execute(
        "SELECT * FROM premade_containers WHERE container_code = ?", (code,)
    ).fetchone()
    if container is None:
        return {"status": "error", "message": f"No container with code {code}."}
    if container["status"] in TERMINAL_STATUSES:
        return {"status": "error",
                "message": f"Container is already {container['status']} — nothing changed."}
    if action == "IN_USE" and container["status"] == "IN_USE":
        return {"status": "error", "message": "Container is already IN_USE — nothing changed."}

    item = db.execute("SELECT * FROM items WHERE id = ?", (container["item_id"],)).fetchone()
    db.execute(
        f"UPDATE premade_containers SET status = ?, {spec['stamp']} = ? WHERE id = ?",
        (spec["status"], now(), container["id"]),
    )
    before = after = _current_count(db, store_code, item["id"])
    if spec["delta"]:
        before, after = _apply_delta(db, store_code, item["id"], spec["delta"])
    _log_event(db, store_code, code, action=spec["event"], item_id=item["id"],
               container_id=container["id"], delta=spec["delta"], before=before, after=after,
               employee_id=employee_id, note="WASTE" if action == "WASTED" else None)
    db.commit()

    if action == "IN_USE":
        message = f"{item['display_name']} marked IN USE."
    elif action == "FINISHED":
        message = f"{item['display_name']} marked FINISHED. Active batches: {_fmt(after)}."
    else:
        message = f"{item['display_name']} marked WASTED and logged. Active batches: {_fmt(after)}."
    return {"status": "ok", "kind": "container_updated", "message": message,
            "new_status": spec["status"], "new_count": after}


def apply_adjustment(db, store_code, item_id, new_count, employee_id=None, note=None):
    """Manager correction: set an item's count, logged as an ADJUSTMENT event."""
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return {"status": "error", "message": "Unknown item."}
    try:
        new_count = float(new_count)
    except (TypeError, ValueError):
        return {"status": "error", "message": "New count must be a number."}
    before = _current_count(db, store_code, item_id)
    delta = new_count - before
    db.execute(
        "UPDATE counts SET current_count = ?, updated_at = ? WHERE store_code = ? AND item_id = ?",
        (new_count, now(), store_code, item_id),
    )
    _log_event(db, store_code, item["item_code"], action="ADJUSTMENT", item_id=item_id,
               delta=delta, before=before, after=new_count, employee_id=employee_id,
               note=note or "Manual correction")
    db.commit()
    return {"status": "ok",
            "message": f"{item['display_name']} set to {_fmt(new_count)} "
                       f"(was {_fmt(before)}).",
            "new_count": new_count}


# --- internals -------------------------------------------------------------

def _current_count(db, store_code, item_id):
    row = db.execute(
        "SELECT current_count FROM counts WHERE store_code = ? AND item_id = ?",
        (store_code, item_id),
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO counts (store_code, item_id, current_count, updated_at) VALUES (?, ?, 0, ?)",
            (store_code, item_id, now()),
        )
        return 0.0
    return row["current_count"]


def _apply_delta(db, store_code, item_id, delta):
    before = _current_count(db, store_code, item_id)
    after = before + delta
    db.execute(
        "UPDATE counts SET current_count = ?, updated_at = ? WHERE store_code = ? AND item_id = ?",
        (after, now(), store_code, item_id),
    )
    return before, after


def _log_event(db, store_code, scanned_code, action, item_id=None, container_id=None,
               delta=0, before=None, after=None, employee_id=None, note=None):
    db.execute(
        "INSERT INTO scan_events (scanned_code, store_code, item_id, container_id, action,"
        " quantity_delta, quantity_before, quantity_after, employee_id, timestamp, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scanned_code, store_code, item_id, container_id, action,
         delta, before, after, employee_id, now(), note),
    )


def _is_duplicate_scan(db, store_code, code, action, window_seconds):
    if not window_seconds:
        return False
    cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=window_seconds)) \
        .isoformat(sep=" ", timespec="seconds")
    row = db.execute(
        "SELECT id FROM scan_events WHERE store_code = ? AND scanned_code = ? AND action = ?"
        " AND timestamp >= ? LIMIT 1",
        (store_code, code, action, cutoff),
    ).fetchone()
    return row is not None


def _fmt(n):
    return int(n) if float(n).is_integer() else n
