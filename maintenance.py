"""The Maintenance tab: recurring maintenance tasks by functional location, who does
them, when they're due, and what was done (maintenance_store.py).

Anyone whose access profile opens Maintenance can add and change tasks and locations,
record work and attach files; deleting a task or a location is for admins.
"""
import datetime as dt

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template,
                   request, url_for)

import dashboard
from dashboard import _csv, _csv_url, _wants_csv, current_user, is_admin
from maintenance_store import (ATTACHMENT_TYPES, COMPLETION_STATUSES, DUE_SOON_DAYS, Maintenance,
                               MaintenanceError)

bp = Blueprint("maint", __name__, url_prefix="/maintenance")
# The same sign-in and access checks as every other tab (no waiting for the Unleashed sync).
for check in (dashboard._require_sign_in_for_real_data, dashboard._require_login, dashboard.require_access):
    bp.before_request(check)


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


def _people():
    """Who tasks can be assigned to: active accounts that open Maintenance."""
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


# ---------------------------------------------------------------- overview

@bp.get("/")
def overview():
    today = _today()
    tasks = _store().tasks(today)
    me = _me()
    return render_template(
        "maint/overview.html", today=today, tasks=tasks, due_soon_days=DUE_SOON_DAYS,
        overdue=[t for t in tasks if t["status"] == "overdue"],
        due_soon=[t for t in tasks if t["due_soon"]],
        mine=[t for t in tasks if me is not None and t["assigned_to"] == me
              and (t["status"] == "overdue" or t["due_soon"] or t["status"] == "in_progress")],
        parts=[t for t in tasks if t["order_now"]],
        completed=_store().completed_count(_utc(_month_start(today))))


# ---------------------------------------------------------------- tasks

_STATUS_FILTERS = {"overdue": "Overdue", "soon": f"Due in the next {DUE_SOON_DAYS} days",
                   "in_progress": "In progress", "unassigned": "Unassigned"}


@bp.get("/tasks")
def tasks():
    today = _today()
    store = _store()
    location_id = request.args.get("location", type=int)
    location = store.location(location_id) if location_id else None
    shown = store.tasks(today, location["id"] if location else None)
    status, person = request.args.get("status", ""), request.args.get("person", type=int)
    if status == "overdue":
        shown = [t for t in shown if t["status"] == "overdue"]
    elif status == "soon":
        shown = [t for t in shown if t["due_soon"]]
    elif status == "in_progress":
        shown = [t for t in shown if t["status"] == "in_progress"]
    elif status == "unassigned":
        shown = [t for t in shown if t["assigned_to"] is None]
    if person:
        shown = [t for t in shown if t["assigned_to"] == person]
    if _wants_csv():
        return _csv("maintenance-tasks", [
            ("Task", "name"), ("Location", "location"), ("Status", lambda t: t["status"].replace("_", " ")),
            ("Assigned to", "assignee_name"), ("Next due", lambda t: t["next_due"].isoformat()),
            ("Every (days)", "frequency_days"), ("Part", "part_name"), ("Vendor", "vendor"),
            ("Vendor part number", "vendor_part_number"), ("Lead time (days)", "lead_time_days"),
            ("Order parts by", lambda t: t["order_by"].isoformat() if t["order_by"] else ""),
            ("Last completed", lambda t: (t["last_completed"] or "")[:10]), ("Times done", "times_done"),
        ], shown)
    all_tasks = store.tasks(today)
    counts = {}
    for l in store.locations():
        within = store.under(l["id"])
        counts[l["id"]] = sum(1 for t in all_tasks if t["location_id"] in within)
    return render_template("maint/tasks.html", tasks=shown, locations=store.locations(), counts=counts,
                           location=location, status=status, person=person, people=_people(),
                           statuses=_STATUS_FILTERS, unplaced=sum(1 for t in all_tasks if t["location_id"] is None),
                           total=len(all_tasks), csv_url=_csv_url())


def _task_form(task=None, form=None, error=None):
    status = 400 if error else 200
    return render_template("maint/task_form.html", task=task, form=form or {}, error=error,
                           locations=_store().locations(), people=_people(), today=_today(),
                           return_to=_return_to()), status


def _return_to():
    """Where to go back to after adding a task (e.g. the calendar month it was added from):
    only a Maintenance page."""
    target = request.values.get("return_to") or ""
    return target if target.startswith("/maintenance/") and "//" not in target else None


@bp.route("/tasks/new", methods=["GET", "POST"])
def task_new():
    if request.method == "GET":
        due = dashboard._date_arg("next_due", _today())
        return _task_form(form={"location_id": request.args.get("location", ""), "next_due": due.isoformat()})
    try:
        task_id = _store().save_task(None, request.form, {p["id"] for p in _people()}, dashboard._person())
    except MaintenanceError as e:
        return _task_form(form=request.form, error=str(e))
    flash(f"Added {' '.join(request.form.get('name', '').split())}.", "ok")
    return redirect(_return_to() or url_for("maint.task", task_id=task_id))


@bp.get("/tasks/<int:task_id>")
def task(task_id):
    found = _store().task(task_id, _today())
    if found is None:
        abort(404)
    return render_template("maint/task.html", task=found, today=_today(), statuses=COMPLETION_STATUSES,
                           types=", ".join(sorted(ATTACHMENT_TYPES)), can_delete=is_admin())


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id):
    found = _store().task(task_id, _today())
    if found is None:
        abort(404)
    if request.method == "GET":
        form = {k: found[k] for k in ("name", "location_id", "frequency_days", "assigned_to", "part_name",
                                      "vendor", "vendor_part_number", "lead_time_days", "instructions",
                                      "in_progress")}
        form["next_due"] = found["next_due"].isoformat()
        return _task_form(found, form)
    # Someone it was assigned to who has since lost access may stay assigned.
    assignable = {p["id"] for p in _people()} | ({found["assigned_to"]} if found["assigned_to"] else set())
    try:
        _store().save_task(task_id, request.form, assignable, dashboard._person())
    except MaintenanceError as e:
        return _task_form(found, request.form, str(e))
    flash("Saved the task.", "ok")
    return redirect(url_for("maint.task", task_id=task_id))


@bp.post("/tasks/<int:task_id>/complete")
def task_complete(task_id):
    try:
        following = _store().complete(task_id, request.form, _today(), dashboard._person(), _me())
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(url_for("maint.task", task_id=task_id) + "#record")
    done = "Skipped" if request.form.get("status") == "skipped" else "Recorded"
    flash(f"{done}. It's next due {dashboard.fmt_date(following)}.", "ok")
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
    upload = request.files.get("file")
    try:
        _store().add_attachment(task_id, upload.filename if upload else "", upload.read() if upload else b"",
                                dashboard._person())
        flash(f"Attached {upload.filename}.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.task", task_id=task_id) + "#files")


@bp.get("/attachments/<int:attachment_id>")
def attachment(attachment_id):
    found = _store().attachment(attachment_id)
    if found is None:
        abort(404)
    name = found["filename"].replace('"', "")
    return Response(found["data"], mimetype=found["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"})


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
    return _locations_page(editing=editing)


def _locations_page(editing=None, form=None, error=None):
    store = _store()
    tasks = store.tasks(_today())
    rows = store.locations()
    for l in rows:
        within = store.under(l["id"])
        l["tasks"] = sum(1 for t in tasks if t["location_id"] in within)
        l["here"] = sum(1 for t in tasks if t["location_id"] == l["id"])
        l["children"] = sum(1 for o in rows if o["parent_id"] == l["id"])
    blocked = store.under(editing["id"]) if editing else set()
    return render_template("maint/locations.html", locations=rows, editing=editing, blocked=blocked,
                           form=form or {}, error=error, can_delete=is_admin()), 400 if error else 200


@bp.post("/locations")
def location_save():
    form = request.form
    location_id = form.get("id", type=int)
    parent_id = form.get("parent_id", type=int)
    try:
        _store().save_location(location_id, form.get("name"), form.get("description"), parent_id)
    except MaintenanceError as e:
        editing = _store().location(location_id) if location_id else None
        return _locations_page(editing=editing, form=form, error=str(e))
    flash(f"Saved {' '.join(form.get('name', '').split())}.", "ok")
    return redirect(url_for("maint.locations"))


@bp.post("/locations/<int:location_id>/delete")
def location_delete(location_id):
    if not is_admin():
        flash("Only admins can delete locations.", "warn")
    else:
        try:
            _store().delete_location(location_id)
            flash("Deleted the location.", "ok")
        except MaintenanceError as e:
            flash(str(e), "warn")
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
            (f"Due in the next {DUE_SOON_DAYS} days", "due_soon")], rows)
    most = max([r["tasks"] for r in rows] + [1])
    return render_template("maint/workload.html", rows=rows, most=most, due_soon_days=DUE_SOON_DAYS,
                           csv_url=_csv_url())


@bp.get("/history")
def history():
    today = _today()
    start = dashboard._date_arg("start", _month_start(today))
    end = dashboard._date_arg("end", today)
    person = request.args.get("person") or None
    rows = _store().completions(_utc(start), _utc(end + dt.timedelta(days=1)), person=person)
    if _wants_csv():
        return _csv("maintenance-history", [
            ("Task", "task_name"), ("Location", "location"), ("Status", "status"),
            ("Was due", "scheduled_date"), ("Done at", lambda c: dashboard.local_time(c["completed_at"])),
            ("Done by", "completed_by"), ("Duration (minutes)", "duration_minutes"),
            ("Labor hours", "labor_hours"), ("Parts used", "parts_used"), ("Notes", "notes")], rows)
    everyone = sorted({c["completed_by"] for c in _store().completions()})
    return render_template("maint/history.html", rows=rows, start=start, end=end, person=person,
                           everyone=everyone, csv_url=_csv_url(),
                           hours=sum(c["labor_hours"] or 0 for c in rows),
                           done=sum(1 for c in rows if c["status"] == "completed"))


def init_app(app):
    app.extensions["maintenance"] = Maintenance(app.extensions["db"])
    app.register_blueprint(bp)
