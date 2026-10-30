"""Work: task lists (SAP's task lists: the steps for doing a task), requests (SAP's
notifications: a problem reported, such as a machine down) and work orders (the job of
fixing it, with its labor, parts and service costs).

A request is open until a work order is made for it, or it's closed (fixed without one)
or rejected. Completing the work order closes the request with its findings; cancelling
it opens the request again. Cost lines can change until the order is completed.
"""
import datetime as dt
import json

from maintenance_common import (PRIORITIES, PRIORITY_FOR_CRITICALITY, MaintenanceError, date, long_text,
                                number, text, whole)
from unleashed.store import utc_now

REQUEST_STATUSES = {"open": "Open", "ordered": "Work order made", "closed": "Closed", "rejected": "Rejected"}
ORDER_STATUSES = {"open": "Open", "in_progress": "In progress", "completed": "Completed", "cancelled": "Cancelled"}
COST_KINDS = {"labor": "Labor", "part": "Parts", "service": "Service", "other": "Other"}


class Work:
    # ------------------------------------------------------------ task lists

    def tasklists(self):
        uses = {r["tasklist_id"]: r["n"] for r in self.db.query(
            "SELECT tasklist_id, COUNT(*) AS n FROM maint_tasks WHERE tasklist_id IS NOT NULL GROUP BY tasklist_id")}
        rows = [_tasklist(r, uses.get(r["id"], 0)) for r in self.db.query("SELECT * FROM maint_tasklists")]
        return sorted(rows, key=lambda r: r["name"].lower())

    def tasklist(self, tasklist_id):
        return next((t for t in self.tasklists() if t["id"] == tasklist_id), None)

    def save_tasklist(self, tasklist_id, form, by):
        """Create a task list (`tasklist_id` None) or change one: a name, a description and
        operations from the form's op-text-N / op-minutes-N rows. Returns its id."""
        name = text(form, "name", 100)
        if not name:
            raise MaintenanceError("Give the task list a name, e.g. Grinder burr clean.")
        same = [t for t in self.tasklists() if t["name"].lower() == name.lower() and t["id"] != tasklist_id]
        if same:
            raise MaintenanceError(f"There's already a task list called {same[0]['name']}.")
        operations = []
        for i in range(whole(form.get("rows"), "Rows") or 0):
            step = text(form, f"op-text-{i}", 300)
            if step:
                operations.append({"text": step, "minutes": whole(form.get(f"op-minutes-{i}"), f"Minutes for “{step}”",
                                                                   minimum=0)})
        if not operations:
            raise MaintenanceError("Add at least one step.")
        values = (name, long_text(form, "description", 2000), json.dumps(operations), by, utc_now())
        if tasklist_id is None:
            return self.db.query("INSERT INTO maint_tasklists (name, description, operations, updated_by, updated_at)"
                                 " VALUES (?, ?, ?, ?, ?) RETURNING id", values)[0]["id"]
        if self.tasklist(tasklist_id) is None:
            raise MaintenanceError("That task list doesn't exist any more.")
        self.db.execute("UPDATE maint_tasklists SET name = ?, description = ?, operations = ?, updated_by = ?,"
                        " updated_at = ? WHERE id = ?", values + (tasklist_id,))
        return tasklist_id

    def delete_tasklist(self, tasklist_id):
        found = self.tasklist(tasklist_id)
        if found and found["uses"]:
            raise MaintenanceError(f"{found['uses']} task{'s use' if found['uses'] != 1 else ' uses'} {found['name']}. "
                                   "Change them first.")
        self.db.execute("DELETE FROM maint_tasklists WHERE id = ?", (tasklist_id,))

    # ------------------------------------------------------------ requests

    def requests(self, status=None, location_id=None, equipment_id=None, reported_by_id=None):
        """Requests, newest first. `status`: a status, "active" (open or with a work order
        not yet done), or None for all."""
        rows = self.db.query("SELECT r.*, o.status AS order_status FROM maint_requests r"
                             " LEFT JOIN maint_orders o ON o.id = r.order_id ORDER BY r.reported_at DESC, r.id DESC")
        lookup = self._lookup()
        rows = [self._request(r, lookup) for r in rows]
        if status == "active":
            rows = [r for r in rows if r["status"] in ("open", "ordered")]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        if location_id is not None:
            within = self.under(location_id)
            rows = [r for r in rows if r["location_id"] in within]
        if equipment_id is not None:
            rows = [r for r in rows if r["equipment_id"] == equipment_id]
        if reported_by_id is not None:
            rows = [r for r in rows if r["reported_by_id"] == reported_by_id]
        return rows

    def request(self, request_id):
        rows = self.db.query("SELECT r.*, o.status AS order_status FROM maint_requests r"
                             " LEFT JOIN maint_orders o ON o.id = r.order_id WHERE r.id = ?", (request_id,))
        if not rows:
            return None
        found = self._request(rows[0], self._lookup())
        found["files"] = self.files("request", request_id)
        return found

    def _request(self, row, lookup):
        r = dict(row)
        r["number"] = f"R-{row['id']:05d}"
        r["status_label"] = REQUEST_STATUSES.get(row["status"], row["status"])
        r["priority_label"] = PRIORITIES.get(row["priority"], "")
        r.update(self._object_names(row["location_id"], row["equipment_id"], lookup))
        r["order_number"] = f"WO-{row['order_id']:05d}" if row["order_id"] else None
        return r

    def _lookup(self):
        """Locations and equipment names by id, looked up once for a list."""
        return ({l["id"]: l for l in self.locations()},
                {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM maint_equipment")})

    @staticmethod
    def _object_names(location_id, equipment_id, lookup):
        """Where a request or order is: its location's label and path, and its equipment."""
        locations, equipment = lookup
        location = locations.get(location_id)
        return {"location": location["label"] if location else None,
                "location_path": location["path"] if location else None,
                "cost_center": location["eff_cost_center"] if location else None,
                "criticality_used": location["eff_criticality"] if location else None,
                "equipment_name": equipment.get(equipment_id),
                "equipment_number": f"EQ-{equipment_id:05d}" if equipment_id in equipment else None}

    def _where(self, form):
        """(location id, equipment id, criticality) from a form's location_id and equipment_id:
        equipment puts it where that equipment is installed."""
        equipment_id = whole(form.get("equipment_id"), "Equipment")
        location_id = whole(form.get("location_id"), "Location")
        criticality = None
        if equipment_id is not None:
            item = self.equipment(equipment_id)
            if item is None:
                raise MaintenanceError("That equipment doesn't exist any more.")
            location_id = item["location_id"] or location_id
            criticality = item["criticality_used"]
        if location_id is not None:
            location = self.location(location_id)
            if location is None:
                raise MaintenanceError("That location doesn't exist any more.")
            if location["in_template"]:
                raise MaintenanceError(f"{location['path']} is a template, not a real place.")
            criticality = criticality or location["eff_criticality"]
        if location_id is None and equipment_id is None:
            raise MaintenanceError("Say where it is: a location or a piece of equipment.")
        return location_id, equipment_id, criticality

    def create_request(self, form, by, by_id=None):
        title = text(form, "title", 150)
        if not title:
            raise MaintenanceError("Say what's wrong in a few words, e.g. Grinder 2 won't start.")
        location_id, equipment_id, criticality = self._where(form)
        breakdown = 1 if form.get("breakdown") else 0
        priority = whole(form.get("priority"), "Priority")
        if priority is None:
            priority = 1 if breakdown else PRIORITY_FOR_CRITICALITY.get(criticality, 3)
        if priority not in PRIORITIES:
            raise MaintenanceError("Choose a priority.")
        return self.db.query(
            "INSERT INTO maint_requests (title, description, location_id, equipment_id, priority, breakdown, status,"
            " reported_by, reported_by_id, reported_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?) RETURNING id",
            (title, long_text(form, "description"), location_id, equipment_id, priority, breakdown, by, by_id,
             utc_now()))[0]["id"]

    def close_request(self, request_id, outcome, resolution, by):
        """Close a request without a work order (`outcome` closed: it's fixed or not needed)
        or reject it (`outcome` rejected); either way, say why."""
        found = self.request(request_id)
        if found is None:
            raise MaintenanceError("That request doesn't exist any more.")
        if found["status"] != "open":
            raise MaintenanceError(f"{found['number']} is {found['status_label'].lower()}, not open.")
        if outcome not in ("closed", "rejected"):
            raise MaintenanceError("Close it or reject it.")
        resolution = (resolution or "").strip()[:2000]
        if not resolution:
            raise MaintenanceError("Say what was done, or why it's rejected.")
        self.db.execute("UPDATE maint_requests SET status = ?, resolution = ?, closed_by = ?, closed_at = ?"
                        " WHERE id = ? AND status = 'open'", (outcome, resolution, by, utc_now(), request_id))

    # ------------------------------------------------------------ work orders

    def orders(self, status=None, person=None, location_id=None, equipment_id=None, today=None):
        """Work orders, most urgent first. `status`: a status, "active" (open or in progress),
        or None for all."""
        lookup = self._lookup()
        rows = [self._order(r, today, lookup) for r in self.db.query(
            "SELECT o.*, u.name AS assignee_name FROM maint_orders o LEFT JOIN app_users u ON u.id = o.assigned_to")]
        totals = self._order_totals()
        for r in rows:
            r.update(totals.get(r["id"], {"cost": 0.0, "labor_hours": 0.0}))
        if status == "active":
            rows = [r for r in rows if r["status"] in ("open", "in_progress")]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        if person is not None:
            rows = [r for r in rows if r["assigned_to"] == person]
        if location_id is not None:
            within = self.under(location_id)
            rows = [r for r in rows if r["location_id"] in within]
        if equipment_id is not None:
            rows = [r for r in rows if r["equipment_id"] == equipment_id]
        done = ("completed", "cancelled")
        active = sorted((r for r in rows if r["status"] not in done),
                        key=lambda r: (r["priority"], r["due_date"] or "9999-12-31", r["id"]))
        finished = sorted((r for r in rows if r["status"] in done), key=lambda r: r["completed_at"] or "", reverse=True)
        return active + finished

    def _order_totals(self):
        return {r["order_id"]: {"cost": r["cost"] or 0.0, "labor_hours": r["hours"] or 0.0} for r in self.db.query(
            "SELECT order_id, SUM(quantity * unit_cost) AS cost,"
            " SUM(CASE WHEN kind = 'labor' THEN quantity ELSE 0 END) AS hours FROM maint_order_costs GROUP BY order_id")}

    def order(self, order_id, today=None):
        rows = self.db.query("SELECT o.*, u.name AS assignee_name FROM maint_orders o"
                             " LEFT JOIN app_users u ON u.id = o.assigned_to WHERE o.id = ?", (order_id,))
        if not rows:
            return None
        found = self._order(rows[0], today, self._lookup())
        found["costs"] = self.db.query("SELECT * FROM maint_order_costs WHERE order_id = ? ORDER BY id", (order_id,))
        for c in found["costs"]:
            c["total"] = c["quantity"] * c["unit_cost"]
            c["kind_label"] = COST_KINDS.get(c["kind"], c["kind"])
        found["cost"] = sum(c["total"] for c in found["costs"])
        found["labor_hours"] = sum(c["quantity"] for c in found["costs"] if c["kind"] == "labor")
        found["by_kind"] = {label: sum(c["total"] for c in found["costs"] if c["kind"] == kind)
                            for kind, label in COST_KINDS.items()}
        found["request"] = self.request(found["request_id"]) if found["request_id"] else None
        found["files"] = self.files("order", order_id)
        return found

    def _order(self, row, today, lookup):
        o = dict(row)
        o["number"] = f"WO-{row['id']:05d}"
        o["status_label"] = ORDER_STATUSES.get(row["status"], row["status"])
        o["priority_label"] = PRIORITIES.get(row["priority"], "")
        o.update(self._object_names(row["location_id"], row["equipment_id"], lookup))
        o["due"] = dt.date.fromisoformat(row["due_date"]) if row["due_date"] else None
        o["late"] = bool(today and o["due"] and o["due"] < today and row["status"] in ("open", "in_progress"))
        o["locked"] = row["status"] in ("completed", "cancelled")
        return o

    def save_order(self, order_id, form, assignable, by, request_id=None):
        """Create a work order (`order_id` None; for request `request_id`, if given) or
        change one. Returns its id."""
        title = text(form, "title", 150)
        if not title:
            raise MaintenanceError("Give the work order a title.")
        location_id, equipment_id, criticality = self._where(form)
        priority = whole(form.get("priority"), "Priority") or PRIORITY_FOR_CRITICALITY.get(criticality, 3)
        if priority not in PRIORITIES:
            raise MaintenanceError("Choose a priority.")
        assigned_to = whole(form.get("assigned_to"), "Assigned to")
        if assigned_to is not None and assigned_to not in assignable:
            raise MaintenanceError("Assign it to someone who can open Maintenance.")
        due = date(form.get("due_date"), "the date it's needed by")
        values = (title, long_text(form, "description"), location_id, equipment_id, assigned_to, priority,
                  due.isoformat() if due else None)
        if order_id is not None:
            found = self.order(order_id)
            if found is None:
                raise MaintenanceError("That work order doesn't exist any more.")
            if found["locked"]:
                raise MaintenanceError(f"{found['number']} is {found['status_label'].lower()}; reopen it to change it.")
            self.db.execute("UPDATE maint_orders SET title = ?, description = ?, location_id = ?, equipment_id = ?,"
                            " assigned_to = ?, priority = ?, due_date = ? WHERE id = ?", values + (order_id,))
            return order_id
        request = None
        if request_id is not None:
            request = self.request(request_id)
            if request is None:
                raise MaintenanceError("That request doesn't exist any more.")
            if request["status"] != "open":
                raise MaintenanceError(f"{request['number']} is {request['status_label'].lower()}, not open.")
        with self.db.connect() as conn:
            new_id = conn.query(
                "INSERT INTO maint_orders (title, description, location_id, equipment_id, assigned_to, priority,"
                " due_date, request_id, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
                " RETURNING id", values + (request_id, by, utc_now()))[0]["id"]
            if request is not None and conn.execute(
                    "UPDATE maint_requests SET status = 'ordered', order_id = ? WHERE id = ? AND status = 'open'",
                    (new_id, request_id)) != 1:
                raise MaintenanceError(f"{request['number']} just changed. Reload it.")
        return new_id

    def set_order_status(self, order_id, action, findings, by):
        """start, complete (with findings), cancel (with a reason) or reopen a work order."""
        found = self.order(order_id)
        if found is None:
            raise MaintenanceError("That work order doesn't exist any more.")
        findings = (findings or "").strip()[:5000] or None
        allowed = {"start": ("open",), "complete": ("open", "in_progress"), "cancel": ("open", "in_progress"),
                   "reopen": ("completed", "cancelled")}
        if action not in allowed:
            raise MaintenanceError("Choose what to do with the work order.")
        if found["status"] not in allowed[action]:
            raise MaintenanceError(f"{found['number']} is {found['status_label'].lower()}.")
        if action == "cancel" and not findings:
            raise MaintenanceError("Say why it's cancelled.")
        now = utc_now()
        with self.db.connect() as conn:
            if action == "start":
                conn.execute("UPDATE maint_orders SET status = 'in_progress', started_at = ? WHERE id = ?", (now, order_id))
            elif action in ("complete", "cancel"):
                status = "completed" if action == "complete" else "cancelled"
                conn.execute("UPDATE maint_orders SET status = ?, completed_at = ?, completed_by = ?, findings = ?"
                             " WHERE id = ?", (status, now, by, findings, order_id))
                if found["request_id"]:
                    if action == "complete":
                        conn.execute("UPDATE maint_requests SET status = 'closed', closed_by = ?, closed_at = ?,"
                                     " resolution = ? WHERE id = ?",
                                     (by, now, findings or f"Fixed by {found['number']}.", found["request_id"]))
                    else:  # the problem is still there
                        conn.execute("UPDATE maint_requests SET status = 'open', order_id = NULL WHERE id = ?",
                                     (found["request_id"],))
            else:
                conn.execute("UPDATE maint_orders SET status = 'open', completed_at = NULL, completed_by = NULL"
                             " WHERE id = ?", (order_id,))
                if found["request_id"]:
                    conn.execute("UPDATE maint_requests SET status = 'ordered', order_id = ?, closed_at = NULL,"
                                 " closed_by = NULL, resolution = NULL WHERE id = ?", (order_id, found["request_id"]))

    def add_cost(self, order_id, form, by):
        found = self.order(order_id)
        if found is None:
            raise MaintenanceError("That work order doesn't exist any more.")
        if found["locked"]:
            raise MaintenanceError(f"{found['number']} is {found['status_label'].lower()}; reopen it to change its costs.")
        kind = form.get("kind") or ""
        if kind not in COST_KINDS:
            raise MaintenanceError("Choose labor, parts, service or other.")
        description = text(form, "description", 200)
        if not description:
            raise MaintenanceError("Describe the cost, e.g. who worked or which part.")
        quantity = number(form.get("quantity"), "Hours" if kind == "labor" else "Quantity", maximum=100000,
                          required=True)
        unit_cost = number(form.get("unit_cost"), "Rate" if kind == "labor" else "Unit cost", maximum=1_000_000,
                           required=True)
        self.db.execute("INSERT INTO maint_order_costs (order_id, kind, description, quantity, unit_cost, added_by,"
                        " added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (order_id, kind, description, quantity, unit_cost, by, utc_now()))

    def delete_cost(self, cost_id):
        rows = self.db.query("SELECT order_id FROM maint_order_costs WHERE id = ?", (cost_id,))
        if not rows:
            return None
        found = self.order(rows[0]["order_id"])
        if found and found["locked"]:
            raise MaintenanceError(f"{found['number']} is {found['status_label'].lower()}; reopen it to change its costs.")
        self.db.execute("DELETE FROM maint_order_costs WHERE id = ?", (cost_id,))
        return rows[0]["order_id"]


def _tasklist(row, uses):
    t = dict(row)
    t["operations"] = json.loads(row["operations"])
    t["minutes"] = sum(op.get("minutes") or 0 for op in t["operations"])
    t["uses"] = uses
    return t
