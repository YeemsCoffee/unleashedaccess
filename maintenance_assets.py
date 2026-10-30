"""Equipment (SAP's equipment master): the machines themselves, where each is installed
and has been, their counters (SAP's measuring points) and readings, and the files kept
with equipment, requests and work orders.

A piece of equipment is installed at a functional location, in storage, or retired.
Moving it records where it was taken from and where it went, on which date, so its
history survives a swap; the tasks on it follow it. A counter (shots pulled, hours run)
only goes up; its readings give the usage per day that dates usage-based tasks.
"""
import datetime as dt

from maintenance_common import (CRITICALITIES, MaintenanceError, checked_file, date, fmt_number, long_text,
                                number, text, whole)
from unleashed.store import utc_now

EQUIPMENT_STATUSES = {"installed": "Installed", "storage": "In storage", "retired": "Retired"}
FILE_OBJECTS = ("equipment", "request", "order")
RATE_DAYS = 180  # usage per day comes from the readings this far back, when there are enough


class Assets:
    # ------------------------------------------------------------ equipment

    def equipment_list(self, status=None, q=None, location_id=None):
        """Equipment, by name. `status`: installed, storage, retired, or None for all but
        retired. `q` matches number, tag, name, category, make, model or serial."""
        locations = {l["id"]: l for l in self.locations()}
        rows = [_equipment(r, locations) for r in self.db.query("SELECT * FROM maint_equipment")]
        if status:
            rows = [r for r in rows if r["status"] == status]
        else:
            rows = [r for r in rows if r["status"] != "retired"]
        if location_id is not None:
            within = self.under(location_id)
            rows = [r for r in rows if r["location_id"] in within]
        if q:
            q = q.lower()
            rows = [r for r in rows if any(q in (r[k] or "").lower() for k in (
                "number", "tag", "name", "category", "manufacturer", "model", "serial_number"))]
        return sorted(rows, key=lambda r: (r["name"].lower(), r["id"]))

    def equipment(self, equipment_id):
        rows = self.db.query("SELECT * FROM maint_equipment WHERE id = ?", (equipment_id,))
        if not rows:
            return None
        locations = {l["id"]: l for l in self.locations()}
        item = _equipment(rows[0], locations)
        item["moves"] = self.db.query("SELECT * FROM maint_equipment_moves WHERE equipment_id = ?"
                                      " ORDER BY on_date DESC, id DESC", (equipment_id,))
        for m in item["moves"]:
            loc = locations.get(m["location_id"])
            m["location"] = loc["label"] if loc else None
        states = self.counter_states()
        item["counters"] = [dict(c, **states.get(c["id"], {}), readings=self.readings(c["id"], 5))
                            for c in self.db.query("SELECT * FROM maint_counters WHERE equipment_id = ?"
                                                   " ORDER BY lower(name)", (equipment_id,))]
        item["files"] = self.files("equipment", equipment_id)
        return item

    def save_equipment(self, equipment_id, form, by, today):
        """Create equipment (`equipment_id` None; installed at form's location_id, if given,
        on its installed_on date) or change its details. Returns its id."""
        name = text(form, "name", 150)
        if not name:
            raise MaintenanceError("Give the equipment a name, e.g. Espresso machine 1.")
        criticality = (form.get("criticality") or "").upper() or None
        if criticality and criticality not in CRITICALITIES:
            raise MaintenanceError("Criticality is A, B or C.")
        purchase_date = date(form.get("purchase_date"), "the purchase date")
        warranty = date(form.get("warranty_until"), "the warranty end date")
        cost = number(form.get("purchase_cost"), "Purchase cost", maximum=10_000_000)
        values = (name, text(form, "category", 60), text(form, "tag", 40), text(form, "manufacturer", 100),
                  text(form, "model", 100), text(form, "serial_number", 100),
                  purchase_date.isoformat() if purchase_date else None, cost,
                  warranty.isoformat() if warranty else None, criticality, long_text(form, "notes"))
        if equipment_id is not None:
            if self.equipment(equipment_id) is None:
                raise MaintenanceError("That equipment doesn't exist any more.")
            self.db.execute(
                "UPDATE maint_equipment SET name = ?, category = ?, tag = ?, manufacturer = ?, model = ?,"
                " serial_number = ?, purchase_date = ?, purchase_cost = ?, warranty_until = ?, criticality = ?,"
                " notes = ? WHERE id = ?", values + (equipment_id,))
            return equipment_id
        location_id = whole(form.get("location_id"), "Location")
        if location_id:
            self._installable(location_id)
        new_id = self.db.query(
            "INSERT INTO maint_equipment (name, category, tag, manufacturer, model, serial_number, purchase_date,"
            " purchase_cost, warranty_until, criticality, notes, status, location_id, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'storage', NULL, ?, ?) RETURNING id",
            values + (by, utc_now()))[0]["id"]
        if location_id:
            on = date(form.get("installed_on"), "the date it was installed") or today
            self.move_equipment(new_id, "install", location_id, on, None, by, today)
        return new_id

    def _installable(self, location_id):
        location = self.location(location_id)
        if location is None:
            raise MaintenanceError("That location doesn't exist any more.")
        if location["in_template"]:
            raise MaintenanceError(f"{location['path']} is a template: equipment is installed at real locations.")
        return location

    def move_equipment(self, equipment_id, action, location_id, on_date, note, by, today):
        """`action`: install (at `location_id`, from wherever it is), storage or retire."""
        item = self.equipment(equipment_id)
        if item is None:
            raise MaintenanceError("That equipment doesn't exist any more.")
        if on_date > today:
            raise MaintenanceError("The date can't be in the future.")
        if item["status"] == "retired":
            raise MaintenanceError(f"{item['name']} is retired.")
        note = (note or "").strip()[:500] or None
        if action == "install":
            location = self._installable(location_id)
            if item["location_id"] == location_id:
                raise MaintenanceError(f"{item['name']} is already installed at {location['path']}.")
            status, where = "installed", location_id
        elif action == "storage":
            if item["status"] == "storage":
                raise MaintenanceError(f"{item['name']} is already in storage.")
            status, where = "storage", None
        elif action == "retire":
            tasks = self.db.query("SELECT COUNT(*) AS n FROM maint_tasks WHERE equipment_id = ?", (equipment_id,))[0]["n"]
            if tasks:
                raise MaintenanceError(f"{item['name']} has {tasks} task{'s' if tasks != 1 else ''}. Delete them, or "
                                       "move them to other equipment, before retiring it.")
            status, where = "retired", None
        else:
            raise MaintenanceError("Choose where it goes.")
        now = utc_now()
        with self.db.connect() as conn:
            if item["location_id"] is not None:
                conn.execute("INSERT INTO maint_equipment_moves (equipment_id, action, location_id, on_date, note,"
                             " by_name, recorded_at) VALUES (?, 'dismantled', ?, ?, ?, ?, ?)",
                             (equipment_id, item["location_id"], on_date.isoformat(),
                              note if action != "install" else None, by, now))
            if action == "install":
                conn.execute("INSERT INTO maint_equipment_moves (equipment_id, action, location_id, on_date, note,"
                             " by_name, recorded_at) VALUES (?, 'installed', ?, ?, ?, ?, ?)",
                             (equipment_id, where, on_date.isoformat(), note, by, now))
            elif action == "retire":
                conn.execute("INSERT INTO maint_equipment_moves (equipment_id, action, location_id, on_date, note,"
                             " by_name, recorded_at) VALUES (?, 'retired', NULL, ?, ?, ?, ?)",
                             (equipment_id, on_date.isoformat(), note, by, now))
            conn.execute("UPDATE maint_equipment SET status = ?, location_id = ? WHERE id = ?",
                         (status, where, equipment_id))
            conn.execute("UPDATE maint_tasks SET location_id = ? WHERE equipment_id = ?", (where, equipment_id))

    def delete_equipment(self, equipment_id):
        item = self.equipment(equipment_id)
        if item is None:
            return
        for table, what in (("maint_tasks", "tasks"), ("maint_requests", "requests"), ("maint_orders", "work orders")):
            if self.db.query(f"SELECT 1 FROM {table} WHERE equipment_id = ?", (equipment_id,)):
                raise MaintenanceError(f"{item['name']} has {what}, so it's kept for their history. Retire it instead.")
        with self.db.connect() as conn:
            counters = [r["id"] for r in conn.query("SELECT id FROM maint_counters WHERE equipment_id = ?", (equipment_id,))]
            for counter_id in counters:
                conn.execute("DELETE FROM maint_readings WHERE counter_id = ?", (counter_id,))
            conn.execute("DELETE FROM maint_counters WHERE equipment_id = ?", (equipment_id,))
            conn.execute("DELETE FROM maint_equipment_moves WHERE equipment_id = ?", (equipment_id,))
            conn.execute("DELETE FROM maint_files WHERE object_type = 'equipment' AND object_id = ?", (equipment_id,))
            conn.execute("DELETE FROM maint_equipment WHERE id = ?", (equipment_id,))

    # ------------------------------------------------------------ counters

    def add_counter(self, equipment_id, form, by, today):
        if self.equipment(equipment_id) is None:
            raise MaintenanceError("That equipment doesn't exist any more.")
        name = text(form, "name", 60)
        if not name:
            raise MaintenanceError("Name the counter, e.g. Shots.")
        unit = text(form, "unit", 20)
        start = number(form.get("value"), "The reading now", maximum=1e12)
        counter_id = self.db.query("INSERT INTO maint_counters (equipment_id, name, unit, created_at) VALUES (?, ?, ?, ?)"
                                   " RETURNING id", (equipment_id, name, unit, utc_now()))[0]["id"]
        if start is not None:
            self.add_reading(counter_id, start, today, None, by, today)
        return counter_id

    def counter(self, counter_id):
        rows = self.db.query("SELECT c.*, e.name AS equipment_name FROM maint_counters c"
                             " JOIN maint_equipment e ON e.id = c.equipment_id WHERE c.id = ?", (counter_id,))
        return dict(rows[0], **self.counter_states().get(counter_id, {})) if rows else None

    def add_reading(self, counter_id, value, on_date, note, by, today):
        """Record the counter at `value` on `on_date`. Counters only go up, so a reading must
        be at least the one before it and at most the one after."""
        counter = self.counter(counter_id)
        if counter is None:
            raise MaintenanceError("That counter doesn't exist any more.")
        if value is None:
            raise MaintenanceError("Enter the reading.")
        if on_date > today:
            raise MaintenanceError("The reading's date can't be in the future.")
        before = self.db.query("SELECT MAX(value) AS v FROM maint_readings WHERE counter_id = ? AND read_on <= ?",
                               (counter_id, on_date.isoformat()))[0]["v"]
        after = self.db.query("SELECT MIN(value) AS v FROM maint_readings WHERE counter_id = ? AND read_on > ?",
                              (counter_id, on_date.isoformat()))[0]["v"]
        unit = counter["unit"] or ""
        n = fmt_number
        if before is not None and value < before:
            raise MaintenanceError(f"{n(value)} is lower than the reading before it ({n(before)} {unit}). Counters only "
                                   "go up; if this one was reset, add a new counter.")
        if after is not None and value > after:
            raise MaintenanceError(f"{n(value)} is higher than a later reading ({n(after)} {unit}).")
        self.db.execute("INSERT INTO maint_readings (counter_id, value, read_on, note, by_name, recorded_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (counter_id, value, on_date.isoformat(), (note or "").strip()[:500] or None, by, utc_now()))

    def readings(self, counter_id, limit=50):
        return self.db.query("SELECT * FROM maint_readings WHERE counter_id = ? ORDER BY read_on DESC, id DESC"
                             f" LIMIT {int(limit)}", (counter_id,))

    def delete_counter(self, counter_id):
        counter = self.counter(counter_id)
        if counter is None:
            return None
        if self.db.query("SELECT 1 FROM maint_tasks WHERE counter_id = ?", (counter_id,)):
            raise MaintenanceError(f"Tasks are scheduled by {counter['name']}. Change them first.")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM maint_readings WHERE counter_id = ?", (counter_id,))
            conn.execute("DELETE FROM maint_counters WHERE id = ?", (counter_id,))
        return counter["equipment_id"]

    def counter_states(self):
        """{counter id: {"value", "read_on", "rate"}}: the latest reading, and usage per day
        from the readings of the last RATE_DAYS (or all of them, when that's too few)."""
        latest = {r["counter_id"]: r for r in self.db.query(
            "SELECT counter_id, MAX(value) AS value, MAX(read_on) AS read_on FROM maint_readings GROUP BY counter_id")}
        since = (dt.date.today() - dt.timedelta(days=RATE_DAYS)).isoformat()
        recent = {r["counter_id"]: r for r in self.db.query(
            "SELECT counter_id, MIN(value) AS value, MIN(read_on) AS read_on FROM maint_readings"
            " WHERE read_on >= ? GROUP BY counter_id", (since,))}
        first = {r["counter_id"]: r for r in self.db.query(
            "SELECT counter_id, MIN(value) AS value, MIN(read_on) AS read_on FROM maint_readings GROUP BY counter_id")}
        states = {}
        for counter_id, last in latest.items():
            rate = None
            for start in (recent.get(counter_id), first.get(counter_id)):
                if start and start["read_on"] < last["read_on"]:
                    days = (dt.date.fromisoformat(last["read_on"]) - dt.date.fromisoformat(start["read_on"])).days
                    rate = (last["value"] - start["value"]) / days if days else None
                    break
            states[counter_id] = {"value": last["value"], "read_on": dt.date.fromisoformat(last["read_on"]),
                                  "rate": rate if rate and rate > 0 else None}
        return states

    # ------------------------------------------------------------ files

    def add_file(self, object_type, object_id, filename, data, by):
        if object_type not in FILE_OBJECTS:
            raise MaintenanceError("Files go with equipment, requests and work orders.")
        filename, content_type = checked_file(filename, data)
        table = {"equipment": "maint_equipment", "request": "maint_requests", "order": "maint_orders"}[object_type]
        if not self.db.query(f"SELECT 1 FROM {table} WHERE id = ?", (object_id,)):
            raise MaintenanceError(f"That {object_type} doesn't exist any more.")
        self.db.execute("INSERT INTO maint_files (object_type, object_id, filename, content_type, size, data,"
                        " uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (object_type, object_id, filename, content_type, len(data), data, by, utc_now()))

    def files(self, object_type, object_id):
        return self.db.query("SELECT id, object_type, object_id, filename, content_type, size, uploaded_by, uploaded_at"
                             " FROM maint_files WHERE object_type = ? AND object_id = ? ORDER BY uploaded_at DESC, id DESC",
                             (object_type, object_id))

    def file(self, file_id):
        rows = self.db.query("SELECT * FROM maint_files WHERE id = ?", (file_id,))
        if not rows:
            return None
        row = rows[0]
        row["data"] = bytes(row["data"])  # PostgreSQL may hand back a memoryview
        return row

    def delete_file(self, file_id):
        rows = self.db.query("SELECT object_type, object_id FROM maint_files WHERE id = ?", (file_id,))
        self.db.execute("DELETE FROM maint_files WHERE id = ?", (file_id,))
        return (rows[0]["object_type"], rows[0]["object_id"]) if rows else None


def _equipment(row, locations):
    item = dict(row)
    location = locations.get(row["location_id"])
    item["number"] = f"EQ-{row['id']:05d}"
    item["location"] = location["label"] if location else None
    item["location_path"] = location["path"] if location else None
    # Its own criticality, or that of where it's installed.
    item["criticality_used"] = row["criticality"] or (location["eff_criticality"] if location else None)
    item["cost_center"] = location["eff_cost_center"] if location else None
    item["status_label"] = EQUIPMENT_STATUSES.get(row["status"], row["status"])
    for key in ("purchase_date", "warranty_until"):
        item[key] = dt.date.fromisoformat(row[key]) if row[key] else None
    return item
