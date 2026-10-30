"""Maintenance pages for equipment and its counters, task lists, requests, work orders and
their files (the rest of the tab is in maintenance.py)."""
from flask import abort, flash, redirect, render_template, request, url_for

import dashboard
from dashboard import _csv, _csv_url, _wants_csv, is_admin
from maintenance import (_download, _form_options, _me, _people, _return_to, _store, _today, _upload, bp,
                         can_manage)
from maintenance_assets import EQUIPMENT_STATUSES
from maintenance_common import MAX_OPERATIONS, PRIORITIES, MaintenanceError, date, fmt_number, number
from maintenance_work import COST_KINDS, ORDER_STATUSES, REQUEST_STATUSES


def _not_allowed(message):
    return render_template("dash/error.html", heading="Not for your account", error=message, status=403), 403


# ---------------------------------------------------------------- equipment

@bp.get("/equipment")
def equipment_list():
    store = _store()
    status = request.args.get("status") or ""
    location_id = request.args.get("location", type=int)
    q = (request.args.get("q") or "").strip()
    rows = store.equipment_list(status=status if status in EQUIPMENT_STATUSES else None, q=q,
                                location_id=location_id)
    if _wants_csv():
        return _csv("maintenance-equipment", [
            ("Number", "number"), ("Asset tag", "tag"), ("Name", "name"), ("Category", "category"),
            ("Status", "status_label"), ("Location", "location"), ("Manufacturer", "manufacturer"),
            ("Model", "model"), ("Serial number", "serial_number"), ("Criticality", "criticality_used"),
            ("Cost center", "cost_center"),
            ("Purchased", lambda r: r["purchase_date"].isoformat() if r["purchase_date"] else ""),
            ("Purchase cost", "purchase_cost"),
            ("Warranty until", lambda r: r["warranty_until"].isoformat() if r["warranty_until"] else "")], rows)
    return render_template("maint/equipment_list.html", rows=rows, status=status, statuses=EQUIPMENT_STATUSES,
                           q=q, location_id=location_id, locations=store.locations(), today=_today(),
                           csv_url=_csv_url())


def _equipment_form(item=None, form=None, error=None):
    return render_template("maint/equipment_form.html", item=item, form=form or {}, error=error,
                           locations=[l for l in _store().locations() if not l["in_template"]],
                           today=_today()), 400 if error else 200


@bp.route("/equipment/new", methods=["GET", "POST"])
def equipment_new():
    if request.method == "GET":
        return _equipment_form(form={"location_id": request.args.get("location", "")})
    try:
        new_id = _store().save_equipment(None, request.form, dashboard._person(), _today())
    except MaintenanceError as e:
        return _equipment_form(form=request.form, error=str(e))
    flash(f"Added {' '.join(request.form.get('name', '').split())}.", "ok")
    return redirect(url_for("maint.equipment", equipment_id=new_id))


@bp.get("/equipment/<int:equipment_id>")
def equipment(equipment_id):
    store = _store()
    item = store.equipment(equipment_id)
    if item is None:
        abort(404)
    today = _today()
    done = store.work_done(equipment_id=equipment_id)
    return render_template(
        "maint/equipment.html", item=item, today=today, tasks=store.tasks(today, equipment_id=equipment_id),
        requests=store.requests(equipment_id=equipment_id)[:10],
        orders=store.orders(equipment_id=equipment_id, today=today)[:10], done=done[:15],
        cost=sum(r["cost"] for r in done), hours=sum(r["labor_hours"] for r in done),
        locations=[l for l in store.locations() if not l["in_template"]], can_delete=is_admin())


@bp.route("/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
def equipment_edit(equipment_id):
    item = _store().equipment(equipment_id)
    if item is None:
        abort(404)
    if request.method == "GET":
        form = dict(item)
        for key in ("purchase_date", "warranty_until"):
            form[key] = item[key].isoformat() if item[key] else ""
        return _equipment_form(item, form)
    try:
        _store().save_equipment(equipment_id, request.form, dashboard._person(), _today())
    except MaintenanceError as e:
        return _equipment_form(item, request.form, str(e))
    flash("Saved the equipment.", "ok")
    return redirect(url_for("maint.equipment", equipment_id=equipment_id))


@bp.post("/equipment/<int:equipment_id>/move")
def equipment_move(equipment_id):
    form = request.form
    try:
        on = date(form.get("on_date"), "the date") or _today()
        _store().move_equipment(equipment_id, form.get("action"), form.get("location_id", type=int), on,
                                form.get("note"), dashboard._person(), _today())
        flash({"install": "Installed it.", "storage": "Moved it to storage.", "retire": "Retired it."}[form.get("action")],
              "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.equipment", equipment_id=equipment_id))


@bp.post("/equipment/<int:equipment_id>/delete")
def equipment_delete(equipment_id):
    if not is_admin():
        flash("Only admins can delete equipment.", "warn")
        return redirect(url_for("maint.equipment", equipment_id=equipment_id))
    try:
        _store().delete_equipment(equipment_id)
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(url_for("maint.equipment", equipment_id=equipment_id))
    flash("Deleted the equipment.", "ok")
    return redirect(url_for("maint.equipment_list"))


@bp.post("/equipment/<int:equipment_id>/counters")
def counter_add(equipment_id):
    try:
        _store().add_counter(equipment_id, request.form, dashboard._person(), _today())
        flash(f"Added the {' '.join(request.form.get('name', '').split())} counter.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.equipment", equipment_id=equipment_id) + "#counters")


@bp.post("/counters/<int:counter_id>/readings")
def reading_add(counter_id):
    store = _store()
    counter = store.counter(counter_id)
    if counter is None:
        abort(404)
    try:
        value = number(request.form.get("value"), "The reading", maximum=1e12, required=True)
        on = date(request.form.get("read_on"), "the date") or _today()
        store.add_reading(counter_id, value, on, request.form.get("note"), dashboard._person(), _today())
        flash(f"Recorded {fmt_number(value)} {counter['unit'] or ''}".strip() + ".", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(_return_to() or url_for("maint.equipment", equipment_id=counter["equipment_id"]) + "#counters")


@bp.post("/counters/<int:counter_id>/delete")
def counter_delete(counter_id):
    counter = _store().counter(counter_id)
    if counter is None:
        abort(404)
    if not is_admin():
        flash("Only admins can delete counters.", "warn")
    else:
        try:
            _store().delete_counter(counter_id)
            flash(f"Deleted the {counter['name']} counter and its readings.", "ok")
        except MaintenanceError as e:
            flash(str(e), "warn")
    return redirect(url_for("maint.equipment", equipment_id=counter["equipment_id"]) + "#counters")


# ---------------------------------------------------------------- files (equipment, requests, orders)

_OBJECT_PAGES = {"equipment": ("maint.equipment", "equipment_id"), "request": ("maint.request", "request_id"),
                 "order": ("maint.order", "order_id")}


def _object_page(object_type, object_id):
    endpoint, arg = _OBJECT_PAGES[object_type]
    return url_for(endpoint, **{arg: object_id}) + "#files"


@bp.post("/files/<object_type>/<int:object_id>")
def file_add(object_type, object_id):
    if object_type not in _OBJECT_PAGES:
        abort(404)
    if not can_manage():
        found = _store().request(object_id) if object_type == "request" else None
        if found is None or found["reported_by_id"] != _me():
            return _not_allowed("You can add photos to requests you reported.")
    _upload(lambda name, data: _store().add_file(object_type, object_id, name, data, dashboard._person()))
    return redirect(_object_page(object_type, object_id))


@bp.get("/files/<int:file_id>")
def file(file_id):
    found = _store().file(file_id)
    if found and found["object_type"] != "request" and not can_manage():
        return _not_allowed("This file is only for people who open Maintenance.")
    return _download(found)


@bp.post("/files/<int:file_id>/delete")
def file_delete(file_id):
    if not can_manage():
        return _not_allowed("Only people who open Maintenance remove files.")
    owner = _store().delete_file(file_id)
    flash("Removed the file.", "ok")
    return redirect(_object_page(*owner) if owner else url_for("maint.overview"))


# ---------------------------------------------------------------- task lists

@bp.get("/tasklists")
def tasklists():
    return render_template("maint/tasklists.html", rows=_store().tasklists())


def _tasklist_form(found=None, form=None, error=None):
    if form is not None and hasattr(form, "getlist"):  # typed rows, to show again
        count = form.get("rows", 0, type=int)
        operations = [{"text": form.get(f"op-text-{i}", ""), "minutes": form.get(f"op-minutes-{i}", "")}
                      for i in range(min(count, MAX_OPERATIONS))]
        values = {"name": form.get("name", ""), "description": form.get("description", "")}
    else:
        operations = list((found or {}).get("operations", []))
        values = {"name": (found or {}).get("name", ""), "description": (found or {}).get("description") or ""}
    operations += [{"text": "", "minutes": ""}] * 3
    return render_template("maint/tasklist_form.html", found=found, values=values, operations=operations,
                           max_operations=MAX_OPERATIONS, error=error), 400 if error else 200


@bp.route("/tasklists/new", methods=["GET", "POST"])
def tasklist_new():
    if request.method == "GET":
        return _tasklist_form()
    try:
        new_id = _store().save_tasklist(None, request.form, dashboard._person())
    except MaintenanceError as e:
        return _tasklist_form(form=request.form, error=str(e))
    flash(f"Added the task list {' '.join(request.form.get('name', '').split())}.", "ok")
    return redirect(url_for("maint.tasklist", tasklist_id=new_id))


@bp.route("/tasklists/<int:tasklist_id>", methods=["GET", "POST"])
def tasklist(tasklist_id):
    found = _store().tasklist(tasklist_id)
    if found is None:
        abort(404)
    if request.method == "GET":
        return _tasklist_form(found)
    try:
        _store().save_tasklist(tasklist_id, request.form, dashboard._person())
    except MaintenanceError as e:
        return _tasklist_form(found, request.form, str(e))
    flash("Saved the task list. Tasks using it follow the new steps.", "ok")
    return redirect(url_for("maint.tasklist", tasklist_id=tasklist_id))


@bp.post("/tasklists/<int:tasklist_id>/delete")
def tasklist_delete(tasklist_id):
    try:
        _store().delete_tasklist(tasklist_id)
        flash("Deleted the task list.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(url_for("maint.tasklist", tasklist_id=tasklist_id))
    return redirect(url_for("maint.tasklists"))


# ---------------------------------------------------------------- requests

@bp.get("/requests")
def requests():
    status = request.args.get("status", "active")
    mine = request.args.get("mine") == "1"
    rows = _store().requests(status if status in REQUEST_STATUSES or status == "active" else None,
                             reported_by_id=_me() if mine else None)
    if _wants_csv():
        return _csv("maintenance-requests", [
            ("Number", "number"), ("Problem", "title"), ("Status", "status_label"), ("Priority", "priority_label"),
            ("Machine down", lambda r: "yes" if r["breakdown"] else ""), ("Location", "location"),
            ("Equipment", "equipment_name"), ("Reported by", "reported_by"),
            ("Reported at", lambda r: dashboard.local_time(r["reported_at"])), ("Work order", "order_number"),
            ("Resolution", "resolution")], rows)
    return render_template("maint/requests.html", rows=rows, status=status, statuses=REQUEST_STATUSES, mine=mine,
                           can_manage=can_manage(), csv_url=_csv_url())


def _request_form(form=None, error=None):
    options = _form_options()
    return render_template("maint/request_form.html", form=form or {}, error=error, priorities=PRIORITIES,
                           locations=[l for l in options["locations"] if not l["in_template"]],
                           equipment=[e for e in options["equipment"] if e["status"] == "installed"]), \
        400 if error else 200


@bp.route("/requests/new", methods=["GET", "POST"])
def request_new():
    if request.method == "GET":
        return _request_form({"location_id": request.args.get("location", ""),
                              "equipment_id": request.args.get("equipment", "")})
    store = _store()
    try:
        new_id = store.create_request(request.form, dashboard._person(), _me())
    except MaintenanceError as e:
        return _request_form(request.form, str(e))
    upload = request.files.get("file")
    if upload and upload.filename:
        try:
            store.add_file("request", new_id, upload.filename, upload.read(), dashboard._person())
        except MaintenanceError as e:
            flash(f"The request is in, but its file wasn't: {e}", "warn")
    flash(f"Reported. It's R-{new_id:05d}; you can follow it here.", "ok")
    return redirect(url_for("maint.request", request_id=new_id))


@bp.get("/requests/<int:request_id>", endpoint="request")
def request_page(request_id):
    found = _store().request(request_id)
    if found is None:
        abort(404)
    return render_template("maint/request.html", r=found, can_manage=can_manage(),
                           can_add_files=can_manage() or found["reported_by_id"] == _me())



@bp.post("/requests/<int:request_id>/close")
def request_close(request_id):
    try:
        _store().close_request(request_id, request.form.get("outcome"), request.form.get("resolution"),
                               dashboard._person())
        flash("Closed the request." if request.form.get("outcome") == "closed" else "Rejected the request.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.request", request_id=request_id))


# ---------------------------------------------------------------- work orders

@bp.get("/orders")
def orders():
    today = _today()
    status = request.args.get("status", "active")
    person = request.args.get("person", type=int)
    rows = _store().orders(status if status in ORDER_STATUSES or status == "active" else None, person=person,
                           today=today)
    if _wants_csv():
        return _csv("maintenance-work-orders", [
            ("Number", "number"), ("Title", "title"), ("Status", "status_label"), ("Priority", "priority_label"),
            ("Location", "location"), ("Equipment", "equipment_name"), ("Cost center", "cost_center"),
            ("Assigned to", "assignee_name"), ("Needed by", "due_date"),
            ("Completed at", lambda o: dashboard.local_time(o["completed_at"]) if o["completed_at"] else ""),
            ("Labor hours", lambda o: round(o["labor_hours"], 2)), ("Cost", lambda o: round(o["cost"], 2))], rows)
    return render_template("maint/orders.html", rows=rows, status=status, statuses=ORDER_STATUSES, person=person,
                           people=_people(), csv_url=_csv_url())


def _order_form(found=None, form=None, error=None, from_request=None):
    options = _form_options()
    return render_template("maint/order_form.html", found=found, form=form or {}, error=error, priorities=PRIORITIES,
                           from_request=from_request, people=options["people"],
                           locations=[l for l in options["locations"] if not l["in_template"]],
                           equipment=[e for e in options["equipment"] if e["status"] != "retired"]), \
        400 if error else 200


@bp.route("/orders/new", methods=["GET", "POST"])
def order_new():
    store = _store()
    request_id = request.values.get("request", type=int)
    from_request = store.request(request_id) if request_id else None
    if request.method == "GET":
        form = {"location_id": request.args.get("location", ""), "equipment_id": request.args.get("equipment", "")}
        if from_request:
            form = {"title": from_request["title"], "description": from_request["description"] or "",
                    "location_id": from_request["location_id"], "equipment_id": from_request["equipment_id"],
                    "priority": from_request["priority"]}
        return _order_form(form=form, from_request=from_request)
    try:
        new_id = store.save_order(None, request.form, {p["id"] for p in _people()}, dashboard._person(),
                                  request_id=request_id)
    except MaintenanceError as e:
        return _order_form(form=request.form, error=str(e), from_request=from_request)
    flash(f"Made WO-{new_id:05d}.", "ok")
    return redirect(url_for("maint.order", order_id=new_id))


@bp.get("/orders/<int:order_id>")
def order(order_id):
    found = _store().order(order_id, _today())
    if found is None:
        abort(404)
    return render_template("maint/order.html", o=found, kinds=COST_KINDS, today=_today())


@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
def order_edit(order_id):
    found = _store().order(order_id, _today())
    if found is None:
        abort(404)
    if request.method == "GET":
        return _order_form(found, dict(found))
    assignable = {p["id"] for p in _people()} | ({found["assigned_to"]} if found["assigned_to"] else set())
    try:
        _store().save_order(order_id, request.form, assignable, dashboard._person())
    except MaintenanceError as e:
        return _order_form(found, request.form, str(e))
    flash("Saved the work order.", "ok")
    return redirect(url_for("maint.order", order_id=order_id))


@bp.post("/orders/<int:order_id>/status")
def order_status(order_id):
    action = request.form.get("action")
    try:
        _store().set_order_status(order_id, action, request.form.get("findings"), dashboard._person())
        flash({"start": "Started.", "complete": "Completed.", "cancel": "Cancelled.",
               "reopen": "Reopened."}.get(action, "Saved."), "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.order", order_id=order_id))


@bp.post("/orders/<int:order_id>/costs")
def order_cost_add(order_id):
    try:
        _store().add_cost(order_id, request.form, dashboard._person())
        flash("Added the cost.", "ok")
    except MaintenanceError as e:
        flash(str(e), "warn")
    return redirect(url_for("maint.order", order_id=order_id) + "#costs")


@bp.post("/costs/<int:cost_id>/delete")
def order_cost_delete(cost_id):
    try:
        order_id = _store().delete_cost(cost_id)
    except MaintenanceError as e:
        flash(str(e), "warn")
        return redirect(request.referrer or url_for("maint.orders"))
    flash("Removed the cost.", "ok")
    return redirect(url_for("maint.order", order_id=order_id) + "#costs" if order_id else url_for("maint.orders"))

