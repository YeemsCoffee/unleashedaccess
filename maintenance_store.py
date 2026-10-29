"""Maintenance: recurring maintenance tasks, organised by functional location.

Brought into Core from the separate maintenance scheduler, and kept in Core's database.
A task is due every `frequency_days`, next on `next_due` (a date in the business's time
zone). Completing it, or skipping it, records who did it and how, and moves it on by
whole intervals from the date it was due until it's due after today, so it keeps its
schedule however late it was done. A task is overdue once its due date has passed; one
being worked on can be marked in progress.

Functional locations form a tree (a building, its rooms, the equipment in them). A
location's tasks include those of the locations under it. Files (SOPs, job aids,
photos) are stored with their task in the database, so every server has them.
"""
import datetime as dt

from unleashed.store import utc_now

DUE_SOON_DAYS = 7
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
# What can be attached, and the type each is served as.
ATTACHMENT_TYPES = {
    "pdf": "application/pdf", "txt": "text/plain", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
COMPLETION_STATUSES = ("completed", "skipped")


class MaintenanceError(ValueError):
    """A problem to show the person (missing name, location in use)."""


class Maintenance:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ locations

    def locations(self):
        """Every functional location, in tree order: each with "depth", "path" (names from
        the top, " › " between) and "parent_id"."""
        rows = self.db.query("SELECT id, name, description, parent_id FROM maint_locations")
        children = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r)
        ids = {r["id"] for r in rows}
        ordered = []

        def walk(parent, depth, path):
            for r in sorted(children.get(parent, []), key=lambda r: r["name"].lower()):
                full = path + [r["name"]]
                ordered.append(dict(r, depth=depth, path=" › ".join(full)))
                walk(r["id"], depth + 1, full)

        walk(None, 0, [])
        # Ones whose parent is gone sit at the top rather than vanish.
        for orphan in [r for r in rows if r["parent_id"] is not None and r["parent_id"] not in ids]:
            ordered.append(dict(orphan, depth=0, path=orphan["name"]))
            walk(orphan["id"], 1, [orphan["name"]])
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

    def save_location(self, location_id, name, description, parent_id):
        """Create a location (`location_id` None) or change one. Returns its id."""
        name = " ".join((name or "").split())
        if not name:
            raise MaintenanceError("Give the location a name.")
        if len(name) > 100:
            raise MaintenanceError("Location names can be 100 characters at most.")
        description = (description or "").strip()[:2000] or None
        if parent_id is not None:
            if self.location(parent_id) is None:
                raise MaintenanceError("That parent location doesn't exist any more.")
            if location_id is not None and parent_id in self.under(location_id):
                raise MaintenanceError("A location can't go under itself or one of the locations under it.")
        same = self.db.query("SELECT id FROM maint_locations WHERE lower(name) = ? AND "
                             + ("parent_id IS NULL" if parent_id is None else "parent_id = ?"),
                             (name.lower(),) + (() if parent_id is None else (parent_id,)))
        if any(r["id"] != location_id for r in same):
            where = f"under {self.location(parent_id)['path']}" if parent_id else "at the top level"
            raise MaintenanceError(f"There's already a location called {name} {where}.")
        if location_id is None:
            return self.db.query("INSERT INTO maint_locations (name, description, parent_id, created_at)"
                                 " VALUES (?, ?, ?, ?) RETURNING id",
                                 (name, description, parent_id, utc_now()))[0]["id"]
        if self.location(location_id) is None:
            raise MaintenanceError("That location doesn't exist any more.")
        self.db.execute("UPDATE maint_locations SET name = ?, description = ?, parent_id = ? WHERE id = ?",
                        (name, description, parent_id, location_id))
        return location_id

    def delete_location(self, location_id):
        location = self.location(location_id)
        if location is None:
            return
        if self.db.query("SELECT 1 FROM maint_locations WHERE parent_id = ?", (location_id,)):
            raise MaintenanceError(f"{location['name']} has locations under it. Move or delete them first.")
        if self.db.query("SELECT 1 FROM maint_tasks WHERE location_id = ?", (location_id,)):
            raise MaintenanceError(f"{location['name']} has tasks. Move or delete them first.")
        self.db.execute("DELETE FROM maint_locations WHERE id = ?", (location_id,))

    # ------------------------------------------------------------ tasks

    def tasks(self, today, location_id=None):
        """Every task (or those at `location_id` and below), soonest due first, with its
        status, location, assignee and how often it's been done."""
        rows = self.db.query(
            "SELECT t.*, u.name AS assignee_name,"
            " (SELECT COUNT(*) FROM maint_completions c WHERE c.task_id = t.id) AS times_done,"
            " (SELECT COUNT(*) FROM maint_attachments a WHERE a.task_id = t.id) AS files"
            " FROM maint_tasks t LEFT JOIN app_users u ON u.id = t.assigned_to")
        paths = {l["id"]: l["path"] for l in self.locations()}
        if location_id is not None:
            within = self.under(location_id)
            rows = [r for r in rows if r["location_id"] in within]
        tasks = [_task(r, paths, today) for r in rows]
        return sorted(tasks, key=lambda t: (t["next_due"], t["name"].lower()))

    def task(self, task_id, today):
        rows = self.db.query(
            "SELECT t.*, u.name AS assignee_name,"
            " (SELECT COUNT(*) FROM maint_completions c WHERE c.task_id = t.id) AS times_done,"
            " (SELECT COUNT(*) FROM maint_attachments a WHERE a.task_id = t.id) AS files"
            " FROM maint_tasks t LEFT JOIN app_users u ON u.id = t.assigned_to WHERE t.id = ?", (task_id,))
        if not rows:
            return None
        task = _task(rows[0], {l["id"]: l["path"] for l in self.locations()}, today)
        task["attachments"] = self.db.query(
            "SELECT id, filename, content_type, size, uploaded_by, uploaded_at FROM maint_attachments"
            " WHERE task_id = ? ORDER BY uploaded_at DESC, id DESC", (task_id,))
        task["completions"] = self.completions(task_id=task_id)
        return task

    def save_task(self, task_id, form, assignable, by):
        """Create a task (`task_id` None) or change one, from form fields. `assignable`:
        the ids of people it may be assigned to. Returns its id."""
        def text(key, limit):
            return " ".join((form.get(key) or "").split())[:limit] or None

        name = text("name", 150)
        if not name:
            raise MaintenanceError("Give the task a name.")
        frequency = _whole(form.get("frequency_days"), "How often", minimum=1, required=True)
        try:
            next_due = dt.date.fromisoformat(form.get("next_due") or "")
        except ValueError:
            raise MaintenanceError("Enter the date it's next due.") from None
        lead_time = _whole(form.get("lead_time_days"), "Lead time", minimum=0) or 0
        location_id = _whole(form.get("location_id"), "Location")
        if location_id is not None and self.location(location_id) is None:
            raise MaintenanceError("That location doesn't exist any more.")
        assigned_to = _whole(form.get("assigned_to"), "Assigned to")
        if assigned_to is not None and assigned_to not in assignable:
            raise MaintenanceError("Assign it to someone who can open Maintenance.")
        values = (name, location_id, frequency, next_due.isoformat(), assigned_to,
                  1 if form.get("in_progress") else 0, text("part_name", 150), text("vendor", 100),
                  text("vendor_part_number", 100), lead_time,
                  (form.get("instructions") or "").strip()[:5000] or None)
        if task_id is None:
            return self.db.query(
                "INSERT INTO maint_tasks (name, location_id, frequency_days, next_due, assigned_to,"
                " in_progress, part_name, vendor, vendor_part_number, lead_time_days, instructions,"
                " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                values + (by, utc_now()))[0]["id"]
        if not self.db.query("SELECT 1 FROM maint_tasks WHERE id = ?", (task_id,)):
            raise MaintenanceError("That task doesn't exist any more.")
        self.db.execute(
            "UPDATE maint_tasks SET name = ?, location_id = ?, frequency_days = ?, next_due = ?,"
            " assigned_to = ?, in_progress = ?, part_name = ?, vendor = ?, vendor_part_number = ?,"
            " lead_time_days = ?, instructions = ? WHERE id = ?", values + (task_id,))
        return task_id

    def delete_task(self, task_id):
        with self.db.connect() as conn:
            for table in ("maint_completions", "maint_attachments"):
                conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM maint_tasks WHERE id = ?", (task_id,))

    def complete(self, task_id, form, today, by, by_id=None):
        """Record the task as done (or skipped) and move it on to its next due date.
        Returns that date."""
        status = form.get("status") or "completed"
        if status not in COMPLETION_STATUSES:
            raise MaintenanceError("Mark it completed or skipped.")
        duration = _whole(form.get("duration_minutes"), "Duration", minimum=0)
        labor = _number(form.get("labor_hours"), "Labor hours")
        notes = (form.get("notes") or "").strip()[:5000] or None
        if status == "skipped" and not notes:
            raise MaintenanceError("Say why it was skipped.")
        with self.db.connect() as conn:
            task = conn.query_one("SELECT next_due, frequency_days FROM maint_tasks WHERE id = ?", (task_id,))
            if task is None:
                raise MaintenanceError("That task doesn't exist any more.")
            due = dt.date.fromisoformat(task["next_due"])
            following = next_due_after(due, task["frequency_days"], today)
            now = utc_now()
            conn.execute(
                "INSERT INTO maint_completions (task_id, status, scheduled_date, completed_at, completed_by,"
                " completed_by_id, notes, duration_minutes, labor_hours, parts_used)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, status, due.isoformat(), now, by, by_id, notes, duration, labor,
                 (form.get("parts_used") or "").strip()[:2000] or None))
            # A changed due date means someone else got there first: don't move it twice.
            if conn.execute("UPDATE maint_tasks SET next_due = ?, in_progress = 0, last_completed = ?"
                            " WHERE id = ? AND next_due = ?",
                            (following.isoformat(), now, task_id, due.isoformat())) != 1:
                raise MaintenanceError("Someone else just recorded this task. Check its history.")
        return following

    # ------------------------------------------------------------ history

    def completions(self, since=None, before=None, task_id=None, person=None):
        """Completions, newest first, with their task's name and location. `since` and
        `before` are UTC times, as recorded ("2026-10-01 07:00:00")."""
        sql = ("SELECT c.*, t.name AS task_name, t.location_id FROM maint_completions c"
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
        paths = {l["id"]: l["path"] for l in self.locations()}
        for r in rows:
            r["location"] = paths.get(r["location_id"])
        return rows

    def completed_count(self, since):
        """Tasks completed (not skipped) since `since` (a UTC time)."""
        return self.db.query("SELECT COUNT(*) AS n FROM maint_completions WHERE status = 'completed'"
                             " AND completed_at >= ?", (since,))[0]["n"]

    # ------------------------------------------------------------ attachments

    def add_attachment(self, task_id, filename, data, by):
        filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not filename:
            raise MaintenanceError("Choose a file to attach.")
        if extension not in ATTACHMENT_TYPES:
            raise MaintenanceError("Attach a PDF, Word or Excel file, a picture (PNG, JPG, GIF) or a text file.")
        if not data:
            raise MaintenanceError(f"{filename} is empty.")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise MaintenanceError(f"{filename} is too big: files can be 16 MB at most.")
        if not self.db.query("SELECT 1 FROM maint_tasks WHERE id = ?", (task_id,)):
            raise MaintenanceError("That task doesn't exist any more.")
        self.db.execute(
            "INSERT INTO maint_attachments (task_id, filename, content_type, size, data, uploaded_by, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename[:200], ATTACHMENT_TYPES[extension], len(data), data, by, utc_now()))

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
        """For each person (dicts with "id", "name", "email") and anyone else tasks are
        assigned to, and for unassigned tasks: how many tasks, overdue and due soon."""
        tasks = self.tasks(today)
        rows = {p["id"]: {**p, "tasks": 0, "overdue": 0, "due_soon": 0} for p in people}
        unassigned = {"id": None, "name": "Unassigned", "email": "", "tasks": 0, "overdue": 0, "due_soon": 0}
        for t in tasks:
            row = rows.setdefault(t["assigned_to"], {"id": t["assigned_to"], "name": t["assignee_name"] or "Someone",
                                                     "email": "", "tasks": 0, "overdue": 0, "due_soon": 0}) \
                if t["assigned_to"] is not None else unassigned
            row["tasks"] += 1
            row["overdue"] += t["status"] == "overdue"
            row["due_soon"] += t["due_soon"]
        ordered = sorted(rows.values(), key=lambda r: (-r["tasks"], r["name"].lower()))
        return ordered + ([unassigned] if unassigned["tasks"] else [])

    def calendar(self, month, today, local_day, since, before):
        """{date: [entries]} for the month starting `month`: when each task is due (its next
        due date, then the dates its schedule brings after it) and what was done.
        `local_day` turns a UTC time into the business's date; `since` and `before` are the
        month's bounds as UTC times."""
        last = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        days = {}
        for t in self.tasks(today):
            due, every = t["next_due"], dt.timedelta(days=t["frequency_days"])
            if month <= due <= last:
                days.setdefault(due, []).append({"task": t, "kind": "overdue" if t["status"] == "overdue" else "due"})
            # Then its schedule: from the next due date, or for an overdue one, from today.
            d = next_due_after(due, t["frequency_days"], today)
            if d < month:
                d += every * ((month - d).days // t["frequency_days"])
                if d < month:
                    d += every
            while d <= last:
                days.setdefault(d, []).append({"task": t, "kind": "planned"})
                d += every
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


def _task(row, paths, today):
    task = dict(row)
    due = dt.date.fromisoformat(row["next_due"])
    days = (due - today).days
    task["next_due"] = due
    task["location"] = paths.get(row["location_id"])
    task["status"] = "overdue" if days < 0 else "in_progress" if row["in_progress"] else "pending"
    task["due_label"] = ("Due today" if days == 0 else f"Due in {days} day{'s' if days != 1 else ''}"
                         if days > 0 else f"{-days} day{'s' if days != -1 else ''} overdue")
    task["due_soon"] = 0 <= days < DUE_SOON_DAYS
    # Parts with a lead time have to be ordered that many days before the task is due.
    task["order_by"] = (due - dt.timedelta(days=row["lead_time_days"])
                        if row["part_name"] and row["lead_time_days"] else None)
    task["order_now"] = task["order_by"] is not None and task["order_by"] <= today + dt.timedelta(days=DUE_SOON_DAYS)
    task["last_completed"] = row["last_completed"]
    return task


def _whole(value, label, minimum=None, required=False):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        if required:
            raise MaintenanceError(f"{label} is needed.")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MaintenanceError(f"{label} must be a whole number.") from None
    if minimum is not None and number < minimum:
        raise MaintenanceError(f"{label} must be at least {minimum}.")
    return number


def _number(value, label):
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        raise MaintenanceError(f"{label} must be a number.") from None
    if not 0 <= number < 10000:
        raise MaintenanceError(f"{label} must be between 0 and 10,000.")
    return number
