"""Maintenance, modelled on SAP Plant Maintenance and kept in Core's database.

Functional locations (maint_locations) are a tree of the places maintenance is done: a
store, its kitchen, the espresso machine's position in it. Each can have a code (a
child's starts with its parent's: GAR, GAR-KIT, GAR-KIT-ESP) and master data (site,
cost center, work center, criticality A/B/C); what a location leaves blank comes from
the one above. A location marked as a template (with everything under it) is a reference
structure to copy for a new store: its tasks never come due.

Tasks are maintenance plans. A task is due every `frequency_days` from `next_due` (a date
in the business's time zone), or every `usage_interval` on an equipment counter counted
from `last_reading`, or by both, whichever comes first. Recording it (done, or skipped)
moves it on: a time-based task by whole intervals from the date it was due to the first
after today, so it keeps its schedule however late it was done; one that also counts
usage, from today, as both its clocks start again. A task is overdue once its date has
passed or its usage is reached. A usage-based task's due date is projected from the
counter's usage per day. A task can follow a task list (its steps, ticked off when it's
recorded) and be on a piece of equipment, moving with it.

Equipment, counters and files are in maintenance_assets.py; task lists, requests and
work orders in maintenance_work.py.
"""
import datetime as dt
import json
import math

from maintenance_assets import Assets
from maintenance_common import (CRITICALITIES, DUE_SOON_DAYS, MaintenanceError, checked_file, code, date,
                                fmt_number, long_text, number, text, whole)
from maintenance_work import Work
from unleashed.store import utc_now

COMPLETION_STATUSES = ("completed", "skipped")
INHERITED = ("site", "cost_center", "work_center", "criticality")
# Columns added since the first version, for databases it made (Database.ensure_columns).
NEW_COLUMNS = {
    "maint_locations": {"code": "TEXT", "site": "TEXT", "cost_center": "TEXT", "work_center": "TEXT",
                        "criticality": "TEXT", "is_template": "INTEGER"},
    "maint_tasks": {"equipment_id": "INTEGER", "tasklist_id": "INTEGER", "counter_id": "INTEGER",
                    "usage_interval": "REAL", "last_reading": "REAL"},
    "maint_completions": {"parts_cost": "REAL", "reading": "REAL", "checklist": "TEXT"},
}


def migrate(db):
    for table, columns in NEW_COLUMNS.items():
        db.ensure_columns(table, columns)


class Maintenance(Assets, Work):
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ locations

    def locations(self):
        """Every functional location, in tree order, each with "depth", "path" (names from
        the top, " › " between), "label" (code and name), "in_template", and its master data
        as given and as used ("eff_site" ..., with "from_<field>" naming where it came from)."""
        rows = self.db.query("SELECT * FROM maint_locations")
        children = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r)
        ids = {r["id"] for r in rows}
        ordered = []

        def walk(parent, depth, path, inherited, in_template):
            for r in sorted(children.get(parent, []), key=lambda r: ((r["code"] or "~"), r["name"].lower())):
                full = path + [r["name"]]
                template = in_template or bool(r["is_template"])
                own = {f: r[f] for f in INHERITED}
                used = {f: (own[f], r["name"]) if own[f] else inherited.get(f, (None, None)) for f in INHERITED}
                row = dict(r, depth=depth, path=" › ".join(full), in_template=template,
                           label=f"{r['code']} · {r['name']}" if r["code"] else r["name"])
                for f in INHERITED:
                    row[f"eff_{f}"], source = used[f]
                    row[f"from_{f}"] = source if not own[f] and used[f][0] else None
                ordered.append(row)
                walk(r["id"], depth + 1, full, used, template)

        walk(None, 0, [], {}, False)
        # Ones whose parent is gone sit at the top rather than vanish.
        for orphan in [r for r in rows if r["parent_id"] is not None and r["parent_id"] not in ids]:
            children[("orphan", orphan["id"])] = [orphan]
            walk(("orphan", orphan["id"]), 0, [], {}, False)
        return ordered

    def location(self, location_id):
        return next((l for l in self.locations() if l["id"] == location_id), None)

    def under(self, location_id):
        """`location_id` and every location below it."""
        found, todo = set(), [location_id]
        rows = self.db.query("SELECT id, parent_id FROM maint_locations")
        while todo:
            current = todo.pop()
            found.add(current)
            todo += [r["id"] for r in rows if r["parent_id"] == current and r["id"] not in found]
        return found

    def search_locations(self, q):
        q = (q or "").strip().lower()
        return [l for l in self.locations() if q in (l["code"] or "").lower() or q in l["path"].lower()]

    def save_location(self, location_id, form):
        """Create a location (`location_id` None) or change one, from form fields. Changing
        a code changes the codes under it that start with it. Returns its id."""
        name = text(form, "name", 100)
        if not name:
            raise MaintenanceError("Give the location a name.")
        parent_id = whole(form.get("parent_id"), "Under")
        new_code = code(form.get("code"))
        criticality = (form.get("criticality") or "").upper() or None
        if criticality and criticality not in CRITICALITIES:
            raise MaintenanceError("Criticality is A, B or C.")
        current = self.location(location_id) if location_id is not None else None
        if location_id is not None and current is None:
            raise MaintenanceError("That location doesn't exist any more.")
        parent = None
        if parent_id is not None:
            parent = self.location(parent_id)
            if parent is None:
                raise MaintenanceError("That parent location doesn't exist any more.")
            if location_id is not None and parent_id in self.under(location_id):
                raise MaintenanceError("A location can't go under itself or one of the locations under it.")
        same = self.db.query("SELECT id FROM maint_locations WHERE lower(name) = ? AND "
                             + ("parent_id IS NULL" if parent_id is None else "parent_id = ?"),
                             (name.lower(),) + (() if parent_id is None else (parent_id,)))
        if any(r["id"] != location_id for r in same):
            where = f"under {parent['path']}" if parent else "at the top level"
            raise MaintenanceError(f"There's already a location called {name} {where}.")
        if new_code:
            if parent and parent["code"] and not new_code.startswith(parent["code"] + "-"):
                raise MaintenanceError(f"Codes under {parent['label']} start with {parent['code']}-.")
            taken = self.db.query("SELECT id FROM maint_locations WHERE upper(code) = ?", (new_code,))
            if any(r["id"] != location_id for r in taken):
                raise MaintenanceError(f"Another location already has the code {new_code}.")
        values = (name, long_text(form, "description", 2000), parent_id, new_code, text(form, "site", 60),
                  text(form, "cost_center", 40), text(form, "work_center", 60), criticality,
                  1 if form.get("is_template") else 0)
        if location_id is None:
            return self.db.query(
                "INSERT INTO maint_locations (name, description, parent_id, code, site, cost_center, work_center,"
                " criticality, is_template, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                values + (utc_now(),))[0]["id"]
        subtree = self.under(location_id)
        if values[-1] or (parent and parent["in_template"]):
            installed = [r for r in self.db.query("SELECT location_id FROM maint_equipment WHERE location_id IS NOT NULL")
                         if r["location_id"] in subtree]
            if installed:
                raise MaintenanceError("Equipment is installed here, so it can't be part of a template.")
        with self.db.connect() as conn:
            conn.execute("UPDATE maint_locations SET name = ?, description = ?, parent_id = ?, code = ?, site = ?,"
                         " cost_center = ?, work_center = ?, criticality = ?, is_template = ? WHERE id = ?",
                         values + (location_id,))
            old = current["code"]
            if old and new_code and old != new_code:
                for child in self.locations():
                    if child["id"] in subtree and child["id"] != location_id \
                            and (child["code"] or "").startswith(old + "-"):
                        renamed = new_code + child["code"][len(old):]
                        if conn.query("SELECT 1 FROM maint_locations WHERE upper(code) = ? AND id <> ?",
                                      (renamed, child["id"])):
                            raise MaintenanceError(f"Renaming {child['code']} to {renamed} would clash with "
                                                   "another location's code.")
                        conn.execute("UPDATE maint_locations SET code = ? WHERE id = ?", (renamed, child["id"]))
        return location_id

    def delete_location(self, location_id):
        location = self.location(location_id)
        if location is None:
            return
        for sql, why in (("SELECT 1 FROM maint_locations WHERE parent_id = ?", "has locations under it. Move or delete them"),
                         ("SELECT 1 FROM maint_tasks WHERE location_id = ?", "has tasks. Move or delete them"),
                         ("SELECT 1 FROM maint_equipment WHERE location_id = ?", "has equipment installed. Move it"),
                         ("SELECT 1 FROM maint_requests WHERE location_id = ?", "has requests, kept for their history. Keep it"),
                         ("SELECT 1 FROM maint_orders WHERE location_id = ?", "has work orders, kept for their history. Keep it")):
            if self.db.query(sql, (location_id,)):
                raise MaintenanceError(f"{location['name']} {why} first." if "Keep it" not in why
                                       else f"{location['name']} {why}.")
        self.db.execute("DELETE FROM maint_locations WHERE id = ?", (location_id,))

    def copy_location(self, source_id, form, by, today):
        """Copy a location and everything under it (a template, or a store set up already)
        to a new place: new name and code for the top one, codes below re-prefixed, the
        tasks on locations copied too (due on first_due), if asked. The copy is never a
        template. Returns (new id, how many locations, how many tasks)."""
        source = self.location(source_id)
        if source is None:
            raise MaintenanceError("That location doesn't exist any more.")
        name = text(form, "name", 100)
        if not name:
            raise MaintenanceError("Name the copy, e.g. Torrance.")
        new_code = code(form.get("code"))
        parent_id = whole(form.get("parent_id"), "Under")
        if parent_id is not None and parent_id in self.under(source_id):
            raise MaintenanceError("A copy can't go inside what's being copied.")
        with_tasks = bool(form.get("with_tasks"))
        first_due = date(form.get("first_due"), "the date the copied tasks are first due") or today
        within = self.under(source_id)
        subtree = [l for l in self.locations() if l["id"] in within]  # tree order: parents first

        def recode(old):
            if not old or not new_code:
                return None
            if old == source["code"]:
                return new_code
            if source["code"] and old.startswith(source["code"] + "-"):
                return new_code + old[len(source["code"]):]
            return None

        # The top copy goes through save_location's checks (name, code, parent).
        top = {"name": name, "code": new_code or "", "parent_id": str(parent_id) if parent_id else "",
               "description": source["description"] or "", **{f: source[f] or "" for f in INHERITED}}
        new_ids = {source_id: self.save_location(None, top)}
        try:
            for loc in subtree[1:]:
                child = {"name": loc["name"], "code": recode(loc["code"]) or "", "parent_id": str(new_ids[loc["parent_id"]]),
                         "description": loc["description"] or "", **{f: loc[f] or "" for f in INHERITED}}
                new_ids[loc["id"]] = self.save_location(None, child)
            copied = 0
            if with_tasks:
                for t in self.db.query("SELECT * FROM maint_tasks WHERE equipment_id IS NULL"):
                    if t["location_id"] in new_ids:
                        self.db.execute(
                            "INSERT INTO maint_tasks (name, location_id, frequency_days, next_due, assigned_to,"
                            " in_progress, part_name, vendor, vendor_part_number, lead_time_days, instructions,"
                            " tasklist_id, created_by, created_at) VALUES (?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (t["name"], new_ids[t["location_id"]], t["frequency_days"] or 1, first_due.isoformat(),
                             t["part_name"], t["vendor"], t["vendor_part_number"], t["lead_time_days"],
                             t["instructions"], t["tasklist_id"], by, utc_now()))
                        copied += 1
        except MaintenanceError:
            # Undo the part copied so far, so a clash doesn't leave half a store behind.
            with self.db.connect() as conn:
                for new_id in new_ids.values():
                    conn.execute("DELETE FROM maint_tasks WHERE location_id = ?", (new_id,))
                    conn.execute("DELETE FROM maint_locations WHERE id = ?", (new_id,))
            raise
        return new_ids[source_id], len(new_ids), copied

    def template_ids(self):
        return {l["id"] for l in self.locations() if l["in_template"]}

    # ------------------------------------------------------------ tasks

    def _task_context(self, today):
        locations = {l["id"]: l for l in self.locations()}
        states = self.counter_states()
        return {
            "today": today, "locations": locations,
            "equipment": {r["id"]: r for r in self.db.query("SELECT id, name, status FROM maint_equipment")},
            "counters": {r["id"]: dict(r, **states.get(r["id"], {}))
                         for r in self.db.query("SELECT id, name, unit, equipment_id FROM maint_counters")},
            "tasklists": {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM maint_tasklists")},
        }

    _TASK_SQL = ("SELECT t.*, u.name AS assignee_name,"
                 " (SELECT COUNT(*) FROM maint_completions c WHERE c.task_id = t.id) AS times_done,"
                 " (SELECT COUNT(*) FROM maint_attachments a WHERE a.task_id = t.id) AS files"
                 " FROM maint_tasks t LEFT JOIN app_users u ON u.id = t.assigned_to")

    def tasks(self, today, location_id=None, equipment_id=None, include_templates=False):
        """Tasks, soonest due first, with status, location, equipment, assignee and how often
        they've been done: all of them, or those at `location_id` and below, or on
        `equipment_id`. Tasks in templates only show when asked for, or when looking inside one."""
        ctx = self._task_context(today)
        rows = self.db.query(self._TASK_SQL)
        if location_id is not None:
            within = self.under(location_id)
            rows = [r for r in rows if r["location_id"] in within]
            include_templates = include_templates or ctx["locations"].get(location_id, {}).get("in_template", False)
        if equipment_id is not None:
            rows = [r for r in rows if r["equipment_id"] == equipment_id]
        tasks = [_task(r, ctx) for r in rows]
        if not include_templates:
            tasks = [t for t in tasks if not t["in_template"]]
        return sorted(tasks, key=lambda t: (t["next_due"] or dt.date.max, t["name"].lower()))

    def task(self, task_id, today):
        rows = self.db.query(self._TASK_SQL + " WHERE t.id = ?", (task_id,))
        if not rows:
            return None
        task = _task(rows[0], self._task_context(today))
        task["attachments"] = self.db.query(
            "SELECT id, filename, content_type, size, uploaded_by, uploaded_at FROM maint_attachments"
            " WHERE task_id = ? ORDER BY uploaded_at DESC, id DESC", (task_id,))
        task["completions"] = self.completions(task_id=task_id)
        for c in task["completions"]:
            c["steps"] = json.loads(c["checklist"]) if c.get("checklist") else []
        task["tasklist"] = self.tasklist(task["tasklist_id"]) if task["tasklist_id"] else None
        return task

    def save_task(self, task_id, form, assignable, by, today=None):
        """Create a task (`task_id` None) or change one, from form fields. `assignable`:
        the ids of people it may be assigned to. Returns its id."""
        name = text(form, "name", 150)
        if not name:
            raise MaintenanceError("Give the task a name.")
        equipment_id = whole(form.get("equipment_id"), "Equipment")
        counter_id = whole(form.get("counter_id"), "Counter")
        usage = number(form.get("usage_interval"), "Usage between", maximum=1e12) if counter_id else None
        if counter_id and not usage:
            raise MaintenanceError("Say how much use between times, e.g. every 10,000 shots.")
        frequency = whole(form.get("frequency_days"), "How often", minimum=1, required=not counter_id) or 0
        due_text = form.get("next_due") or ""
        try:
            next_due = dt.date.fromisoformat(due_text) if (frequency or due_text) else (today or dt.date.today())
        except ValueError:
            raise MaintenanceError("Enter the date it's next due.") from None
        lead_time = whole(form.get("lead_time_days"), "Lead time", minimum=0) or 0
        location_id = whole(form.get("location_id"), "Location")
        if equipment_id is not None:
            item = self.db.query("SELECT location_id, status, name FROM maint_equipment WHERE id = ?", (equipment_id,))
            if not item:
                raise MaintenanceError("That equipment doesn't exist any more.")
            if item[0]["status"] == "retired":
                raise MaintenanceError(f"{item[0]['name']} is retired.")
            location_id = item[0]["location_id"]  # a task on equipment is wherever the equipment is
        elif location_id is not None and self.location(location_id) is None:
            raise MaintenanceError("That location doesn't exist any more.")
        last_reading = None
        if counter_id is not None:
            counter = self.db.query("SELECT equipment_id, name FROM maint_counters WHERE id = ?", (counter_id,))
            if not counter or counter[0]["equipment_id"] != equipment_id:
                raise MaintenanceError("Choose a counter on the task's equipment.")
            given = number(form.get("last_reading"), "Last done at", maximum=1e12)
            last_reading = given if given is not None else self.counter_states().get(counter_id, {}).get("value", 0)
        tasklist_id = whole(form.get("tasklist_id"), "Task list")
        if tasklist_id is not None and self.tasklist(tasklist_id) is None:
            raise MaintenanceError("That task list doesn't exist any more.")
        assigned_to = whole(form.get("assigned_to"), "Assigned to")
        if assigned_to is not None and assigned_to not in assignable:
            raise MaintenanceError("Assign it to someone who can open Maintenance.")
        values = (name, location_id, frequency, next_due.isoformat(), assigned_to,
                  1 if form.get("in_progress") else 0, text(form, "part_name", 150), text(form, "vendor", 100),
                  text(form, "vendor_part_number", 100), lead_time, long_text(form, "instructions"),
                  equipment_id, tasklist_id, counter_id, usage)
        if task_id is None:
            return self.db.query(
                "INSERT INTO maint_tasks (name, location_id, frequency_days, next_due, assigned_to, in_progress,"
                " part_name, vendor, vendor_part_number, lead_time_days, instructions, equipment_id, tasklist_id,"
                " counter_id, usage_interval, last_reading, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                values + (last_reading, by, utc_now()))[0]["id"]
        if not self.db.query("SELECT 1 FROM maint_tasks WHERE id = ?", (task_id,)):
            raise MaintenanceError("That task doesn't exist any more.")
        self.db.execute(
            "UPDATE maint_tasks SET name = ?, location_id = ?, frequency_days = ?, next_due = ?, assigned_to = ?,"
            " in_progress = ?, part_name = ?, vendor = ?, vendor_part_number = ?, lead_time_days = ?, instructions = ?,"
            " equipment_id = ?, tasklist_id = ?, counter_id = ?, usage_interval = ?, last_reading = ? WHERE id = ?",
            values + (last_reading, task_id))
        return task_id

    def reschedule(self, task_id, from_date, to_date):
        """Move the task's next due date from `from_date` to `to_date` (as on the calendar).
        Refused if it's no longer due on `from_date`: someone else moved or recorded it."""
        if not dt.date(2000, 1, 1) <= to_date <= dt.date(2100, 12, 31):
            raise MaintenanceError("Pick a date between 2000 and 2100.")
        if self.db.execute("UPDATE maint_tasks SET next_due = ? WHERE id = ? AND next_due = ? AND frequency_days > 0",
                           (to_date.isoformat(), task_id, from_date.isoformat())) != 1:
            if not self.db.query("SELECT 1 FROM maint_tasks WHERE id = ?", (task_id,)):
                raise MaintenanceError("That task doesn't exist any more.")
            raise MaintenanceError("Its due date changed since this page was opened. Reload to see where it is now.")

    def delete_task(self, task_id):
        with self.db.connect() as conn:
            for table in ("maint_completions", "maint_attachments"):
                conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM maint_tasks WHERE id = ?", (task_id,))

    def complete(self, task_id, form, today, by, by_id=None):
        """Record the task as done (or skipped) and move it on. Returns the task as it is
        now (its "next_due", or for usage, "usage")."""
        task = self.task(task_id, today)
        if task is None:
            raise MaintenanceError("That task doesn't exist any more.")
        if task["in_template"]:
            raise MaintenanceError("This task is in a template: copy the template to use it.")
        status = form.get("status") or "completed"
        if status not in COMPLETION_STATUSES:
            raise MaintenanceError("Mark it completed or skipped.")
        duration = whole(form.get("duration_minutes"), "Duration", minimum=0)
        labor = number(form.get("labor_hours"), "Labor hours")
        parts_cost = number(form.get("parts_cost"), "Parts cost", maximum=1_000_000)
        notes = long_text(form, "notes")
        if status == "skipped" and not notes:
            raise MaintenanceError("Say why it was skipped.")
        reading = None
        if task["counter_id"]:
            reading = number(form.get("reading"), "The counter reading", maximum=1e12)
            if reading is not None and reading != task["usage"]["current"]:
                self.add_reading(task["counter_id"], reading, today, f"Recorded with {task['name']}", by, today)
            elif reading is None:
                reading = task["usage"]["current"]
        steps = [{"text": op["text"], "done": bool(form.get(f"op-{i}"))}
                 for i, op in enumerate(task["tasklist"]["operations"])] if task["tasklist"] else []
        stored_due = dt.date.fromisoformat(task["stored_due"])
        if task["frequency_days"] and task["counter_id"]:
            following = today + dt.timedelta(days=task["frequency_days"])  # both clocks start again
        elif task["frequency_days"]:
            following = next_due_after(stored_due, task["frequency_days"], today)
        else:
            following = today  # usage only: the date isn't used
        scheduled = (task["next_due"] or today).isoformat()
        with self.db.connect() as conn:
            now = utc_now()
            conn.execute(
                "INSERT INTO maint_completions (task_id, status, scheduled_date, completed_at, completed_by,"
                " completed_by_id, notes, duration_minutes, labor_hours, parts_used, parts_cost, reading, checklist)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, status, scheduled, now, by, by_id, notes, duration, labor,
                 long_text(form, "parts_used", 2000), parts_cost, reading, json.dumps(steps) if steps else None))
            # A changed due date means someone else got there first: don't move it twice.
            if conn.execute("UPDATE maint_tasks SET next_due = ?, in_progress = 0, last_completed = ?,"
                            " last_reading = COALESCE(?, last_reading) WHERE id = ? AND next_due = ?",
                            (following.isoformat(), now, reading, task_id, task["stored_due"])) != 1:
                raise MaintenanceError("Someone else just recorded this task. Check its history.")
        return self.task(task_id, today)

    # ------------------------------------------------------------ history

    def completions(self, since=None, before=None, task_id=None, person=None):
        """Completions, newest first, with their task's name and location. `since` and
        `before` are UTC times, as recorded ("2026-10-01 07:00:00")."""
        sql = ("SELECT c.*, t.name AS task_name, t.location_id, t.equipment_id FROM maint_completions c"
               " JOIN maint_tasks t ON t.id = c.task_id WHERE 1 = 1")
        args = []
        if since:
            sql, args = sql + " AND c.completed_at >= ?", args + [since]
        if before:
            sql, args = sql + " AND c.completed_at < ?", args + [before]
        if task_id is not None:
            sql, args = sql + " AND c.task_id = ?", args + [task_id]
        if person:
            sql, args = sql + " AND c.completed_by = ?", args + [person]
        rows = self.db.query(sql + " ORDER BY c.completed_at DESC, c.id DESC", args)
        locations = {l["id"]: l for l in self.locations()}
        for r in rows:
            loc = locations.get(r["location_id"])
            r["location"] = loc["path"] if loc else None
        return rows

    def work_done(self, since=None, before=None, location_id=None, equipment_id=None, person=None):
        """Everything done: tasks recorded (completed or skipped) and work orders completed,
        newest first, each with where, by whom, labor hours and cost."""
        locations = {l["id"]: l for l in self.locations()}
        equipment = {r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM maint_equipment")}
        within = self.under(location_id) if location_id is not None else None
        rows = []
        for c in self.completions(since, before, person=person):
            rows.append({"kind": "task", "id": c["task_id"], "title": c["task_name"], "status": c["status"],
                         "at": c["completed_at"], "by": c["completed_by"], "location_id": c["location_id"],
                         "equipment_id": c["equipment_id"], "labor_hours": c["labor_hours"] or 0.0,
                         "cost": c["parts_cost"] or 0.0, "notes": c["notes"], "parts_used": c["parts_used"],
                         "duration_minutes": c["duration_minutes"], "scheduled": c["scheduled_date"]})
        sql, args = "SELECT * FROM maint_orders WHERE status = 'completed'", []
        if since:
            sql, args = sql + " AND completed_at >= ?", args + [since]
        if before:
            sql, args = sql + " AND completed_at < ?", args + [before]
        if person:
            sql, args = sql + " AND completed_by = ?", args + [person]
        totals = self._order_totals()
        for o in self.db.query(sql, args):
            t = totals.get(o["id"], {"cost": 0.0, "labor_hours": 0.0})
            rows.append({"kind": "order", "id": o["id"], "title": f"WO-{o['id']:05d} {o['title']}", "status": "completed",
                         "at": o["completed_at"], "by": o["completed_by"], "location_id": o["location_id"],
                         "equipment_id": o["equipment_id"], "labor_hours": t["labor_hours"], "cost": t["cost"],
                         "notes": o["findings"], "parts_used": None, "duration_minutes": None, "scheduled": o["due_date"]})
        if within is not None:
            rows = [r for r in rows if r["location_id"] in within]
        if equipment_id is not None:
            rows = [r for r in rows if r["equipment_id"] == equipment_id]
        for r in rows:
            loc = locations.get(r["location_id"])
            r["location"] = loc["path"] if loc else None
            r["cost_center"] = loc["eff_cost_center"] if loc else None
            r["equipment"] = equipment.get(r["equipment_id"])
        return sorted(rows, key=lambda r: r["at"] or "", reverse=True)

    def completed_count(self, since):
        """Tasks completed (not skipped) since `since` (a UTC time)."""
        return self.db.query("SELECT COUNT(*) AS n FROM maint_completions WHERE status = 'completed'"
                             " AND completed_at >= ?", (since,))[0]["n"]

    # ------------------------------------------------------------ attachments

    def add_attachment(self, task_id, filename, data, by):
        filename, content_type = checked_file(filename, data)
        if not self.db.query("SELECT 1 FROM maint_tasks WHERE id = ?", (task_id,)):
            raise MaintenanceError("That task doesn't exist any more.")
        self.db.execute(
            "INSERT INTO maint_attachments (task_id, filename, content_type, size, data, uploaded_by, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, filename, content_type, len(data), data, by, utc_now()))

    def attachment(self, attachment_id):
        rows = self.db.query("SELECT * FROM maint_attachments WHERE id = ?", (attachment_id,))
        if not rows:
            return None
        row = rows[0]
        row["data"] = bytes(row["data"])  # PostgreSQL may hand back a memoryview
        return row

    def delete_attachment(self, attachment_id):
        rows = self.db.query("SELECT task_id FROM maint_attachments WHERE id = ?", (attachment_id,))
        self.db.execute("DELETE FROM maint_attachments WHERE id = ?", (attachment_id,))
        return rows[0]["task_id"] if rows else None

    # ------------------------------------------------------------ planning

    def workload(self, people, today):
        """For each person (dicts with "id", "name", "email") and anyone else work is
        assigned to, and for unassigned work: tasks, overdue and due soon, and open work orders."""
        blank = {"tasks": 0, "overdue": 0, "due_soon": 0, "orders": 0, "late_orders": 0}
        rows = {p["id"]: {**p, **blank} for p in people}
        unassigned = {"id": None, "name": "Unassigned", "email": "", **blank}

        def row_for(person_id, name):
            if person_id is None:
                return unassigned
            return rows.setdefault(person_id, {"id": person_id, "name": name or "Someone", "email": "", **blank})

        for t in self.tasks(today):
            row = row_for(t["assigned_to"], t["assignee_name"])
            row["tasks"] += 1
            row["overdue"] += t["status"] == "overdue"
            row["due_soon"] += t["due_soon"]
        for o in self.orders("active", today=today):
            row = row_for(o["assigned_to"], o["assignee_name"])
            row["orders"] += 1
            row["late_orders"] += o["late"]
        ordered = sorted(rows.values(), key=lambda r: (-(r["tasks"] + r["orders"]), r["name"].lower()))
        return ordered + ([unassigned] if unassigned["tasks"] or unassigned["orders"] else [])

    def calendar(self, month, today, local_day, since, before):
        """{date: [entries]} for the month starting `month`: when each task is due (its next
        due date, then the dates its schedule brings after it), work orders by the date
        they're needed, and what was done. `local_day` turns a UTC time into the business's
        date; `since` and `before` are the month's bounds as UTC times."""
        last = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        days = {}
        for t in self.tasks(today):
            due = t["next_due"]
            if due is None:
                continue
            if month <= due <= last:
                days.setdefault(due, []).append({"task": t, "kind": "overdue" if t["status"] == "overdue" else "due"})
            if not t["frequency_days"]:
                continue  # usage-based: only the projected date
            every = dt.timedelta(days=t["frequency_days"])
            # Then its schedule: from the next due date, or for an overdue one, from today.
            d = next_due_after(due, t["frequency_days"], today)
            if d < month:
                d += every * ((month - d).days // t["frequency_days"])
                if d < month:
                    d += every
            while d <= last:
                days.setdefault(d, []).append({"task": t, "kind": "planned"})
                d += every
        for o in self.orders("active", today=today):
            if o["due"] and month <= o["due"] <= last:
                days.setdefault(o["due"], []).append({"order": o, "kind": "overdue" if o["late"] else "order"})
        for c in self.completions(since, before):
            days.setdefault(local_day(c["completed_at"]), []).append({"completion": c, "kind": c["status"]})
        return days


def next_due_after(due, frequency_days, today):
    """The task's next due date once the one on `due` is done: whole intervals on from
    `due`, the first after `today`."""
    following = due + dt.timedelta(days=frequency_days)
    if following <= today:
        following += dt.timedelta(days=frequency_days * ((today - following).days // frequency_days + 1))
    return following


def _days_label(days):
    if days == 0:
        return "Due today"
    if days > 0:
        return f"Due in {days} day{'s' if days != 1 else ''}"
    return f"{-days} day{'s' if days != -1 else ''} overdue"


def _task(row, ctx):
    today = ctx["today"]
    task = dict(row)
    location = ctx["locations"].get(row["location_id"])
    task["location"] = location["path"] if location else None
    task["location_code"] = location["code"] if location else None
    task["in_template"] = bool(location and location["in_template"])
    item = ctx["equipment"].get(row["equipment_id"])
    task["equipment_name"] = item["name"] if item else None
    task["equipment_status"] = item["status"] if item else None
    task["tasklist_name"] = ctx["tasklists"].get(row["tasklist_id"])
    task["stored_due"] = row["next_due"]
    task["frequency_days"] = row["frequency_days"] or 0
    time_due = dt.date.fromisoformat(row["next_due"]) if task["frequency_days"] else None

    # Usage: counted from the reading when it was last done.
    usage, usage_date, reached = None, None, False
    counter = ctx["counters"].get(row["counter_id"]) if row["counter_id"] and row["usage_interval"] else None
    if counter:
        start = row["last_reading"] or 0.0
        current = counter.get("value", start)
        due_at = start + row["usage_interval"]
        left = due_at - current
        reached = left <= 0
        rate = counter.get("rate")
        if reached:
            usage_date = today
        elif rate:
            usage_date = today + dt.timedelta(days=math.ceil(left / rate))
        usage = {"counter": counter["name"], "unit": counter["unit"] or "", "current": current, "due_at": due_at,
                 "left": max(left, 0.0), "reached": reached, "rate": rate, "projected": usage_date,
                 "read_on": counter.get("read_on"), "interval": row["usage_interval"]}
    task["usage"] = usage
    dates = [d for d in (time_due, usage_date) if d is not None]
    due = min(dates) if dates else None
    task["next_due"] = due
    task["time_due"] = time_due
    overdue = reached or (time_due is not None and time_due < today)
    task["status"] = "overdue" if overdue else "in_progress" if row["in_progress"] else "pending"
    unit = usage["unit"] if usage else ""
    if usage and (reached or time_due is None or (usage_date and usage_date < time_due)):
        n = fmt_number
        if reached:
            task["due_label"] = f"Due: {n(usage['current'])} of {n(usage['due_at'])} {unit}".strip()
        elif usage_date:
            task["due_label"] = f"In {n(usage['left'])} {unit} (about {usage_date:%b} {usage_date.day})".replace("  ", " ")
        else:
            task["due_label"] = f"At {n(usage['due_at'])} {unit} (now {n(usage['current'])})".replace("  ", " ")
    else:
        task["due_label"] = _days_label((due - today).days) if due else "—"
    task["due_soon"] = not overdue and due is not None and 0 <= (due - today).days < DUE_SOON_DAYS
    # The calendar can move a date-based due date; a usage-based one comes from the counter.
    task["movable"] = time_due is not None and (usage_date is None or time_due <= usage_date)
    # Parts with a lead time have to be ordered that many days before the task is due.
    task["order_by"] = (due - dt.timedelta(days=row["lead_time_days"])
                        if due and row["part_name"] and row["lead_time_days"] else None)
    task["order_now"] = task["order_by"] is not None and task["order_by"] <= today + dt.timedelta(days=DUE_SOON_DAYS)
    return task
