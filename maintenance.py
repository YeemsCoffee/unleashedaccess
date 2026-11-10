"""The Maintenance tab, modelled on SAP Plant Maintenance (maintenance_store.py): functional
locations, equipment, maintenance plans (tasks) and their task lists, requests and work
orders, a calendar and the history of work done and what it cost.

Anyone whose access profile opens Maintenance can add and change all of it, record work
and attach files; deleting tasks, locations and equipment is for admins. People who can
only report problems (Maintenance requests) see the Requests page. Equipment, task list,
request and work order pages are in maintenance_views.py.
"""
import datetime as dt
import json

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template,
                   request, url_for)

import dashboard
from dashboard import _csv, _csv_url, _wants_csv, current_user, is_admin
from maintenance_common import (CRITICALITIES, DUE_SOON_DAYS, FILE_TYPES, MAX_OPERATIONS, SIMPTWW, SIMPTWW_KEYS,
                                TASK_STATES, MaintenanceError, fmt_number)
from maintenance_store import COMPLETION_STATUSES, SIMPTWW_TEXT, Maintenance
from production_runs import picture_type

bp = Blueprint("maint", __name__, url_prefix="/maintenance")
# The same sign-in and access checks as every other tab (no waiting for the Unleashed sync).
for check in (dashboard._require_sign_in_for_real_data, dashboard._require_login, dashboard.require_access):
    bp.before_request(check)


@bp.app_template_filter("n")
def _n(value):
    return fmt_number(value)


def _store():
    return current_app.extensions["maintenance"]


def _today():
    return dashboard._ext()["today_fn"]()


def _tz():
    return dashboard._ext()["display_tz"]


def _utc(day):
    """The UTC time ("2026-10-01 07:00:00") the business's `day` starts."""
    start = dt.datetime.combine(day, dt.time())
    start = start.replace(tzinfo=_tz()) if _tz() else start.astimezone()
    return start.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_day(timestamp):
    t = dashboard._parse_utc(timestamp)
    return (t.astimezone(_tz()) if _tz() else t.astimezone()).date()


def can_manage():
    """Whether the signed-in person opens all of Maintenance (not only requests)."""
    return "maintenance" in dashboard.user_areas()


def _people():
    """Who work can be assigned to: active accounts that open Maintenance."""
    if current_app.config["AUTH_MODE"] != "accounts":
        return []
    profile_of = current_app.extensions["profiles"].of_users()
    return [{"id": u["id"], "name": u["name"], "email": u["email"]}
            for u in current_app.extensions["users"].all()
            if u["active"] and (u["role"] == "admin" or "maintenance" in profile_of.get(u["id"], {}).get("areas", []))]


def _me():
    user = current_user()
    return user["id"] if user else None


def _month_start(day):
    return day.replace(day=1)


def _return_to():
    """Where to go back to after adding something (e.g. the calendar month it was added
    from): only a Maintenance page."""
    target = request.values.get("return_to") or ""
    return target if target.startswith("/maintenance/") and "//" not in target else None


def _upload(save):
    """Hand the uploaded file to `save(filename, data)`; flash how it went."""
    upload = request.files.get("file")
    try:
        save(upload.filename if upload else "", upload.read() if upload else b"")
        flash(f"Attached {upload.filename}.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")


def _download(found):
    """The file, to save; with ?view=1, a picture (checked from its bytes) shown in the page."""
    if found is None:
        abort(404)
    name = found["filename"].replace('"', "")
    shown = picture_type(found["data"]) if request.args.get("view") else None
    if shown:
        return Response(found["data"], mimetype=shown, headers={
            "Content-Disposition": f'inline; filename="{name}"', "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'"})
    return Response(found["data"], mimetype=found["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- overview

@bp.get("/")
def overview():
    today = _today()
    store = _store()
    tasks = store.tasks(today)
    me = _me()
    requests = store.requests("active")
    orders = store.orders("active", today=today)
    return render_template(
        "maint/overview.html", today=today, tasks=tasks, due_soon_days=DUE_SOON_DAYS,
        overdue=[t for t in tasks if t["status"] == "overdue"],
        due_soon=[t for t in tasks if t["due_soon"]],
        mine=[t for t in tasks if me is not None and t["assigned_to"] == me
              and (t["status"] in ("overdue", "in_progress") or t["due_soon"])],
        my_orders=[o for o in orders if me is not None and o["assigned_to"] == me],
        parts=[t for t in tasks if t["order_now"]],
        new_requests=[r for r in requests if r["status"] == "open"], orders=orders,
        down=[r for r in requests if r["breakdown"]],
        completed=store.completed_count(_utc(_month_start(today))))


# ---------------------------------------------------------------- tasks

_STATUS_FILTERS = {"overdue": "Overdue", "soon": f"Due in the next {DUE_SOON_DAYS} days",
                   "in_progress": "In progress", "unassigned": "Unassigned", "usage": "Done by usage",
                   "simptww": "SIMPTWW not filled in"}


@bp.get("/tasks")
def tasks():
    today = _today()
    store = _store()
    location_id = request.args.get("location", type=int)
    location = store.location(location_id) if location_id else None
    state = request.args.get("state") or "active"
    state = state if state in TASK_STATES or state == "all" else "active"
    shown = store.tasks(today, location["id"] if location else None, state=state)
    status, person = request.args.get("status", ""), request.args.get("person", type=int)
    q = (request.args.get("q") or "").strip()
    if status == "overdue":
        shown = [t for t in shown if t["status"] == "overdue"]
    elif status == "soon":
        shown = [t for t in shown if t["due_soon"]]
    elif status == "in_progress":
        shown = [t for t in shown if t["status"] == "in_progress"]
    elif status == "unassigned":
        shown = [t for t in shown if t["assigned_to"] is None]
    elif status == "usage":
        shown = [t for t in shown if t["usage"]]
    elif status == "simptww":
        shown = [t for t in shown if t["simptww_filled"] < len(SIMPTWW)]
    if person:
        shown = [t for t in shown if t["assigned_to"] == person]
    if q:
        ql = q.lower()
        shown = [t for t in shown if any(ql in (t[k] or "").lower() for k in
                                         ("name", "location", "location_code", "equipment_name", "part_name"))]
    if _wants_csv():
        return _csv("maintenance-tasks", [
            ("Task", "name"), ("Location", "location"), ("Location code", "location_code"),
            ("Equipment", "equipment_name"), ("Status", lambda t: t["status"].replace("_", " ")),
            ("Assigned to", "assignee_name"), ("Next due", lambda t: t["next_due"].isoformat() if t["next_due"] else ""),
            ("Every (days)", lambda t: t["frequency_days"] or ""),
            ("Every (usage)", lambda t: f"{fmt_number(t['usage']['interval'])} {t['usage']['unit']}".strip() if t["usage"] else ""),
            ("Steps", lambda t: len(t["steps"]) or ""), ("Part", "part_name"), ("Vendor", "vendor"),
            ("Vendor part number", "vendor_part_number"), ("Lead time (days)", "lead_time_days"),
            ("Order parts by", lambda t: t["order_by"].isoformat() if t["order_by"] else ""),
            ("Last completed", lambda t: (t["last_completed"] or "")[:10]), ("Times done", "times_done"),
            ("State", "state_label"),
            ("SIMPTWW filled in", lambda t: f"{t['simptww_filled']} of {len(SIMPTWW)}"),
            ("Safety", "safety"), ("Instructions", "instructions"), ("Materials", "materials"),
            ("People needed", "people_needed"), ("Skills", "skills"), ("Tools", "tools"),
            ("Where", "where_notes"), ("When", "when_notes"),
        ], shown)
    all_tasks = store.tasks(today, include_templates=True)
    rows = store.locations()
    counts = {}
    for l in rows:
        within = store.under(l["id"])
        counts[l["id"]] = sum(1 for t in all_tasks if t["location_id"] in within)
    return render_template("maint/tasks.html", tasks=shown, locations=rows, counts=counts,
                           location=location, status=status, person=person, people=_people(), q=q,
                           statuses=_STATUS_FILTERS, state=state, states=TASK_STATES,
                           paused=len(store.tasks(today, include_templates=True, state="inactive,obsolete")),
                           unplaced=sum(1 for t in all_tasks if t["location_id"] is None and not t["equipment_id"]),
                           total=sum(1 for t in all_tasks if not t["in_template"]), csv_url=_csv_url())


def _form_options():
    """What the task, request and work order forms choose from."""
    store = _store()
    equipment = store.equipment_list()
    counters = []
    for item in equipment:
        for c in store.db.query("SELECT id, name, unit FROM maint_counters WHERE equipment_id = ? ORDER BY lower(name)",
                                (item["id"],)):
            counters.append({"id": c["id"], "label": f"{item['name']} · {c['name']}" + (f" ({c['unit']})" if c["unit"] else ""),
                             "equipment_id": item["id"]})
    return {"locations": store.locations(), "equipment": equipment, "counters": counters,
            "people": _people()}


def _task_form(task=None, form=None, error=None):
    form = form or {}
    if hasattr(form, "getlist"):  # the steps as typed, to show again
        steps = [{"text": form.get(f"op-text-{i}", ""), "minutes": form.get(f"op-minutes-{i}", "")}
                 for i in range(min(form.get("rows", 0, type=int), MAX_OPERATIONS))]
    else:
        steps = list((task or {}).get("steps") or [])
    steps += [{"text": "", "minutes": ""}] * max(1, 3 - len(steps))
    # Other tasks' steps, to start from: each distinct set once.
    sources, seen = [], set()
    for t in _store().tasks(_today(), include_templates=True, state="all"):
        key = json.dumps([s["text"] for s in t["steps"]])
        if t["steps"] and key not in seen and (task is None or t["id"] != task["id"]):
            seen.add(key)
            sources.append({"label": f"{t['name']}" + (f" · {t['equipment_name'] or t['location']}"
                                                       if t["equipment_name"] or t["location"] else "")
                                     + f" ({len(t['steps'])} step{'s' if len(t['steps']) != 1 else ''})",
                            "steps": t["steps"]})
    sources.sort(key=lambda s: s["label"].lower())
    # The equipment ticked (a new task can be on several), as typed or as it's opened from a page.
    ticked = [v for v in (form.getlist("equipment_id") if hasattr(form, "getlist") else [form.get("equipment_id")]) if v]
    return render_template("maint/task_form.html", task=task, form=form, error=error, today=_today(), ticked=ticked,
                           return_to=_return_to(), steps=steps, step_sources=sources, simptww=SIMPTWW,
                           picture_types=_picture_types(), max_steps=MAX_OPERATIONS,
                           **_form_options()), 400 if error else 200


def _picture_types():
    return ",".join(f".{t}" for t in sorted(FILE_TYPES) if FILE_TYPES[t].startswith("image/"))


def _save_task_pictures(task_id):
    """The pictures sent with the task form, each under its SIMPTWW section, and the ones
    ticked to remove; say which weren't kept. `task_id` can be several tasks (the same task made
    on several equipment): each gets every picture."""
    store = _store()
    ids = task_id if isinstance(task_id, (list, tuple)) else [task_id]
    for one in ids:
        store.remove_pictures(one, request.form.getlist("remove_picture"))
    for key in SIMPTWW_KEYS:
        for upload in request.files.getlist(f"pictures-{key}"):
            if upload and upload.filename:
                data = upload.read()
                for one in ids:
                    try:
                        store.add_attachment(one, upload.filename, data, dashboard._person(), section=key)
                    except MaintenanceError as e:
                        flash(f"{upload.filename} wasn't added: {e}", "warn")
                        break


@bp.route("/tasks/new", methods=["GET", "POST"])
def task_new():
    if request.method == "GET":
        due = dashboard._date_arg("next_due", _today())
        return _task_form(form={"location_id": request.args.get("location", ""), "next_due": due.isoformat(),
                                "equipment_id": request.args.get("equipment", "")})
    try:
        made = _store().save_new_tasks(request.form, {p["id"] for p in _people()}, dashboard._person(), _today())
    except MaintenanceError as e:
        return _task_form(form=request.form, error=str(e))
    name = " ".join(request.form.get("name", "").split())
    flash(f"Added {name}." if len(made) == 1 else f"Added {name} on {len(made)} equipment.", "ok")
    _save_task_pictures(made)
    if len(made) > 1:
        return redirect(_return_to() or url_for("maint.tasks", q=name))
    return redirect(_return_to() or url_for("maint.task", task_id=made[0]))


@bp.get("/tasks/<int:task_id>")
def task(task_id):
    found = _store().task(task_id, _today())
    if found is None:
        abort(404)
    return render_template("maint/task.html", task=found, today=_today(), statuses=COMPLETION_STATUSES,
                           can_delete=is_admin())


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id):
    found = _store().task(task_id, _today())
    if found is None:
        abort(404)
    if request.method == "GET":
        form = {k: found[k] for k in ("name", "location_id", "frequency_days", "assigned_to", "part_name",
                                      "vendor", "vendor_part_number", "lead_time_days", "instructions",
                                      "in_progress", "equipment_id", "counter_id", "usage_interval",
                                      "last_reading", "people_needed")}
        form.update({c: found[c] for c, _ in SIMPTWW_TEXT})
        form["frequency_days"] = found["frequency_days"] or ""
        form["next_due"] = found["stored_due"] if found["frequency_days"] else ""
        return _task_form(found, form)
    # Someone it was assigned to who has since lost access may stay assigned.
    assignable = {p["id"] for p in _people()} | ({found["assigned_to"]} if found["assigned_to"] else set())
    try:
        _store().save_task(task_id, request.form, assignable, dashboard._person(), _today())
    except MaintenanceError as e:
        return _task_form(found, request.form, str(e))
    flash("Saved the task.", "ok")
    _save_task_pictures(task_id)
    return redirect(url_for("maint.task", task_id=task_id))


@bp.post("/tasks/<int:task_id>/complete")
def task_complete(task_id):
    try:
        now = _store().complete(task_id, request.form, _today(), dashboard._person(), _me())
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(url_for("maint.task", task_id=task_id) + "#record")
    done = "Skipped" if request.form.get("status") == "skipped" else "Recorded"
    if now["usage"] and not now["frequency_days"]:
        nxt = f"at {fmt_number(now['usage']['due_at'])} {now['usage']['unit']}".strip()
    else:
        nxt = dashboard.fmt_date(now["next_due"])
    flash(f"{done}. It's next due {nxt}.", "ok")
    return redirect(url_for("maint.task", task_id=task_id))


@bp.post("/tasks/<int:task_id>/reschedule")
def task_reschedule(task_id):
    """Move a task's next due date, from the calendar (dragging it to another day)."""
    try:
        from_date = dt.date.fromisoformat(request.form.get("from", ""))
        to_date = dt.date.fromisoformat(request.form.get("to", ""))
    except ValueError:
        return jsonify(ok=False, message="That isn't a date."), 400
    found = _store().task(task_id, _today())
    try:
        _store().reschedule(task_id, from_date, to_date)
    except MaintenanceError as e:
        return jsonify(ok=False, message=str(e)), 409
    message = f"Moved {found['name']} to {dashboard.fmt_date(to_date)}." + (
        " That date has passed, so it's overdue." if to_date < _today() else "")
    flash(message, "ok")
    return jsonify(ok=True, message=message, next_due=to_date.isoformat())


@bp.post("/tasks/<int:task_id>/state")
def task_state(task_id):
    """Make one task active, inactive or obsolete."""
    return _change_states([task_id], request.form.get("state", ""), redirect(url_for("maint.task", task_id=task_id)))


@bp.post("/tasks/state")
def tasks_state():
    """The same for the tasks ticked on the Tasks list."""
    back = request.form.get("back") or ""
    back = back if back.startswith("/maintenance/tasks") and "//" not in back else url_for("maint.tasks")
    ids = [int(x) for x in request.form.getlist("task") if x.isdigit()]
    if not ids:
        flash("Tick the tasks to change first.", "warn")
        return redirect(back)
    return _change_states(ids, request.form.get("state", ""), redirect(back))


def _change_states(ids, state, back):
    try:
        changed = _store().set_task_state(ids, state, request.form.get("note"), dashboard._person(), _today())
    except MaintenanceError as e:
        flash(str(e), "warn")
        return back
    if not changed:
        flash("Nothing changed: they're already " + TASK_STATES.get(state, state).lower() + ".", "warn")
        return back
    names = changed[0]["name"] if len(changed) == 1 else f"{len(changed)} tasks"
    if state == "active":
        when = "" if len(changed) != 1 else f" It's next due {dashboard.fmt_date(changed[0]['next_due'])}." \
            if changed[0]["next_due"] else " It's due by usage."
        flash(f"{names} {'is' if len(changed) == 1 else 'are'} active again.{when}", "ok")
    elif state == "inactive":
        flash(f"{names} {'is' if len(changed) == 1 else 'are'} inactive: never coming due, until made active again.", "ok")
    else:
        flash(f"{names} {'is' if len(changed) == 1 else 'are'} obsolete: kept for the history, never coming due.", "ok")
    return back


@bp.route("/tasks/<int:task_id>/copy", methods=["GET", "POST"])
def task_copy(task_id):
    """The same task on other equipment: each copy has its own due date, history and counter."""
    store = _store()
    today = _today()
    found = store.task(task_id, today)
    if found is None:
        abort(404)
    equipment = [e for e in store.equipment_list() if e["id"] != found["equipment_id"]]
    # Equipment that already has a task of this name (any state) is marked, not left out.
    have = {t["equipment_id"] for t in store.tasks(today, include_templates=True, state="all")
            if t["equipment_id"] and t["name"].lower() == found["name"].lower()}
    due = dashboard._date_arg("next_due", dt.date.fromisoformat(found["stored_due"]))
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("equipment") if x.isdigit()]
        try:
            due = dt.date.fromisoformat(request.form.get("next_due", ""))
        except ValueError:
            due = None
        try:
            if due is None and found["frequency_days"]:
                raise MaintenanceError("Enter the date the copies are next due.")
            made = store.copy_task(task_id, ids, due, dashboard._person())
        except MaintenanceError as e:
            flash(str(e), "warn")
        else:
            flash(f"Copied {found['name']} to {len(made)} equipment.", "ok")
            return redirect(url_for("maint.tasks", q=found["name"]))
        ticked = {str(i) for i in ids}
    else:
        ticked = set()
    return render_template("maint/task_copy.html", task=found, equipment=equipment, have=have, due=due,
                           ticked=ticked, today=today)


@bp.post("/tasks/<int:task_id>/delete")
def task_delete(task_id):
    if not is_admin():
        flash("Only admins can delete tasks.", "warn")
        return redirect(url_for("maint.task", task_id=task_id))
    found = _store().task(task_id, _today())
    if found:
        _store().delete_task(task_id)
        flash(f"Deleted {found['name']}, its history and its files.", "ok")
    return redirect(url_for("maint.tasks"))


@bp.post("/tasks/<int:task_id>/attachments")
def attachment_add(task_id):
    _upload(lambda name, data: _store().add_attachment(task_id, name, data, dashboard._person()))
    return redirect(url_for("maint.task", task_id=task_id) + "#files")


@bp.get("/attachments/<int:attachment_id>")
def attachment(attachment_id):
    return _download(_store().attachment(attachment_id))


@bp.post("/attachments/<int:attachment_id>/delete")
def attachment_delete(attachment_id):
    task_id = _store().delete_attachment(attachment_id)
    flash("Removed the file.", "ok")
    return redirect(url_for("maint.task", task_id=task_id) + "#files" if task_id else url_for("maint.tasks"))


# ---------------------------------------------------------------- locations

@bp.get("/locations")
def locations():
    store = _store()
    editing = store.location(request.args.get("edit", type=int)) if request.args.get("edit") else None
    form = {"parent_id": request.args.get("parent", "")} if not editing and request.args.get("parent") else None
    if form:
        parent = store.location(int(form["parent_id"]))
        form["code"] = f"{parent['code']}-" if parent and parent["code"] else ""
    return _locations_page(editing=editing, form=form)


def _locations_page(editing=None, form=None, error=None):
    store = _store()
    tasks = store.tasks(_today(), include_templates=True)
    equipment = store.equipment_list()
    q = (request.args.get("q") or "").strip()
    rows = store.locations()
    for l in rows:
        within = store.under(l["id"])
        l["tasks"] = sum(1 for t in tasks if t["location_id"] in within)
        l["here"] = sum(1 for t in tasks if t["location_id"] == l["id"])
        l["equipment"] = sum(1 for e in equipment if e["location_id"] in within)
        l["children"] = sum(1 for o in rows if o["parent_id"] == l["id"])
    shown = [l for l in rows if not q or q.lower() in (l["code"] or "").lower() or q.lower() in l["path"].lower()]
    blocked = store.under(editing["id"]) if editing else set()
    return render_template("maint/locations.html", locations=rows, shown=shown, editing=editing, blocked=blocked,
                           form=form or {}, error=error, q=q, can_delete=is_admin()), 400 if error else 200


@bp.post("/locations")
def location_save():
    form = request.form
    location_id = form.get("id", type=int)
    try:
        saved = _store().save_location(location_id, form)
    except MaintenanceError as e:
        editing = _store().location(location_id) if location_id else None
        return _locations_page(editing=editing, form=form, error=str(e))
    flash(f"Saved {' '.join(form.get('name', '').split())}.", "ok")
    return redirect(url_for("maint.location", location_id=saved))


@bp.get("/locations/<int:location_id>")
def location(location_id):
    store = _store()
    found = store.location(location_id)
    if found is None:
        abort(404)
    today = _today()
    within = store.under(location_id)
    return render_template(
        "maint/location.html", loc=found, today=today,
        children=[l for l in store.locations() if l["parent_id"] == location_id],
        equipment=store.equipment_list(location_id=location_id),
        tasks=store.tasks(today, location_id), requests=store.requests("active", location_id=location_id),
        orders=store.orders("active", location_id=location_id, today=today),
        done=store.work_done(location_id=location_id)[:10], locations=store.locations(),
        blocked=within, can_delete=is_admin(),
        contents=store.location_contents(location_id) if is_admin() else None)


@bp.post("/locations/<int:location_id>/copy")
def location_copy(location_id):
    try:
        new_id, places, tasks_copied = _store().copy_location(location_id, request.form, dashboard._person(), _today())
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(url_for("maint.location", location_id=location_id) + "#copy")
    flash(f"Copied {places} location{'s' if places != 1 else ''}"
          + (f" and {tasks_copied} task{'s' if tasks_copied != 1 else ''}" if tasks_copied else "") + ".", "ok")
    return redirect(url_for("maint.location", location_id=new_id))


@bp.post("/locations/<int:location_id>/delete")
def location_delete(location_id):
    if not is_admin():
        flash("Only admins can delete locations.", "warn")
    else:
        try:
            gone = _store().delete_location(location_id, dashboard._person(), _today())
            if gone:
                parts = [f"{gone['location']['name']}"
                         + (f" and the {gone['below']} location{'s' if gone['below'] != 1 else ''} under it"
                            if gone['below'] else "")]
                if gone["tasks"]:
                    parts.append(f"{gone['tasks']} task{'s' if gone['tasks'] != 1 else ''}")
                moved = len(gone["equipment"])
                flash("Deleted " + " with ".join(parts) + "."
                      + (f" Moved {moved} piece{'s' if moved != 1 else ''} of equipment to storage." if moved else ""),
                      "ok")
        except MaintenanceError as e:
            flash(str(e), "warn")
            return redirect(url_for("maint.location", location_id=location_id))
    return redirect(url_for("maint.locations"))


# ---------------------------------------------------------------- planning

@bp.get("/calendar")
def calendar():
    today = _today()
    month = dashboard._month_arg(_month_start(today))
    following = (month + dt.timedelta(days=32)).replace(day=1)
    days = _store().calendar(month, today, _local_day, _utc(month), _utc(following))
    # Whole weeks, Sunday first, from the Sunday on or before the 1st.
    first = month - dt.timedelta(days=(month.weekday() + 1) % 7)
    weeks, d = [], first
    while d < following or (d - first).days % 7:
        if (d - first).days % 7 == 0:
            weeks.append([])
        weeks[-1].append(d)
        d += dt.timedelta(days=1)
    return render_template("maint/calendar.html", month=month, weeks=weeks, days=days, today=today,
                           previous=(month - dt.timedelta(days=1)).replace(day=1), following=following)


@bp.get("/workload")
def workload():
    rows = _store().workload(_people(), _today())
    if _wants_csv():
        return _csv("maintenance-workload", [
            ("Person", "name"), ("Email", "email"), ("Tasks", "tasks"), ("Overdue", "overdue"),
            (f"Due in the next {DUE_SOON_DAYS} days", "due_soon"), ("Open work orders", "orders"),
            ("Late work orders", "late_orders")], rows)
    most = max([r["tasks"] + r["orders"] for r in rows] + [1])
    return render_template("maint/workload.html", rows=rows, most=most, due_soon_days=DUE_SOON_DAYS,
                           csv_url=_csv_url())


@bp.get("/history")
def history():
    today = _today()
    store = _store()
    start = dashboard._date_arg("start", _month_start(today))
    end = dashboard._date_arg("end", today)
    person = request.args.get("person") or None
    location_id = request.args.get("location", type=int)
    equipment_id = request.args.get("equipment", type=int)
    rows = store.work_done(_utc(start), _utc(end + dt.timedelta(days=1)), location_id=location_id,
                           equipment_id=equipment_id, person=person)
    if _wants_csv():
        return _csv("maintenance-history", [
            ("Done at", lambda r: dashboard.local_time(r["at"])), ("Work", "title"),
            ("Type", lambda r: "Work order" if r["kind"] == "order" else "Task"), ("Status", "status"),
            ("Location", "location"), ("Equipment", "equipment"), ("Cost center", "cost_center"),
            ("Done by", "by"), ("Was due", "scheduled"), ("Duration (minutes)", "duration_minutes"),
            ("Labor hours", lambda r: round(r["labor_hours"], 2)), ("Cost", lambda r: round(r["cost"], 2)),
            ("Parts used", "parts_used"), ("Notes", "notes")], rows)
    by_center, by_equipment = {}, {}
    for r in rows:
        center = by_center.setdefault(r["cost_center"] or "(none)", {"cost": 0.0, "hours": 0.0, "jobs": 0})
        center["cost"] += r["cost"]
        center["hours"] += r["labor_hours"]
        center["jobs"] += 1
        if r["equipment_id"]:
            item = by_equipment.setdefault(r["equipment_id"], {"name": r["equipment"], "cost": 0.0, "hours": 0.0, "jobs": 0})
            item["cost"] += r["cost"]
            item["hours"] += r["labor_hours"]
            item["jobs"] += 1
    everyone = sorted({r["by"] for r in store.work_done() if r["by"]})
    return render_template(
        "maint/history.html", rows=rows, start=start, end=end, person=person, everyone=everyone,
        csv_url=_csv_url(), locations=store.locations(),
        equipment=store.equipment_list() + store.equipment_list(status="retired"),
        location_id=location_id, equipment_id=equipment_id,
        hours=sum(r["labor_hours"] for r in rows), cost=sum(r["cost"] for r in rows),
        done=sum(1 for r in rows if r["kind"] == "task" and r["status"] == "completed"),
        skipped=sum(1 for r in rows if r["status"] == "skipped"),
        orders_done=sum(1 for r in rows if r["kind"] == "order"),
        by_center=sorted(by_center.items(), key=lambda kv: -kv[1]["cost"]),
        by_equipment=sorted(by_equipment.values(), key=lambda v: -v["cost"])[:10])


def init_app(app):
    import maintenance_views  # noqa: F401 (adds the equipment, task list, request and work order pages)
    app.extensions["maintenance"] = Maintenance(app.extensions["db"])
    # Globals rather than context, so the macros imported from maint/_macros.html see them.
    app.jinja_env.globals.update(criticalities=CRITICALITIES,
                                 file_types=",".join(f".{t}" for t in sorted(FILE_TYPES)))
    app.register_blueprint(bp)
