"""The Transactions tab: recent stock transactions, the full history, stock movement, and
stock adjustments, warehouse transfers and stock counts made in Core and written to
Unleashed (unleashed/stock_writes.py; counts are kept in Core, stock_counts.py)."""
import datetime as dt
from itertools import zip_longest

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from dashboard import _csv, _csv_url, _ext, _person, _range_args, _round, _wants_csv, bp, reports
from order_pages import _blocked, _done, _number, _saved
from stock_counts import CountError, differences
from unleashed.client import UnleashedError
from unleashed.stock_writes import (OPEN_TRANSFERS, StockWriteError, adjustment_reasons, complete_transfer,
                                    create_adjustment, create_transfer, delete_transfer, update_transfer)

RECENT_DAYS = 2  # the Transactions page shows today and yesterday
COUNT_REASON = "Stock count"


# ---------------------------------------------------------------- shared

def _counts():
    return current_app.extensions["stock_counts"]


def _failed(e):
    return str(e) if isinstance(e, StockWriteError) else f"Unleashed said: {e}"


def _warehouses():
    r = reports()
    found = r.warehouses()
    default = next((w.get("WarehouseCode") for w in found if w.get("IsDefault")),
                   found[0].get("WarehouseCode") if found else None)
    return [w for w in found if not w.get("Obsolete")], default


def _warehouse_names():
    return {w.get("WarehouseCode"): w.get("WarehouseName") or w.get("WarehouseCode") for w in reports().warehouses()}


def _stock_products():
    """Products that hold stock (not charges, not obsolete): [{"code", "name", "unit", "group",
    "cost", "batch"}], by code."""
    r = reports()
    charges = r.charge_codes()
    out = []
    for p in r.products():
        code = p.get("ProductCode") or ""
        if not code or p.get("Obsolete") or p.get("NeverDiminishing") or code.upper() in charges:
            continue
        out.append({"code": code, "name": p.get("ProductDescription") or "", "unit": (p.get("UnitOfMeasure") or {}).get("Name"),
                    "group": (p.get("ProductGroup") or {}).get("GroupName") or "", "cost": p.get("DefaultPurchasePrice"),
                    "batch": bool(p.get("IsBatchTracked"))})
    return sorted(out, key=lambda p: p["code"].lower())


def _stock_in(warehouse, fresh=True):
    """{product code: {"qty", "value", "avg"}} in `warehouse` now, read from Unleashed itself
    (not the copy, which can be a sync behind): an adjustment sets the quantity there should be,
    so it has to start from what's there. `fresh=False` (what a form shows) takes an answer
    Unleashed gave in the last couple of minutes."""
    client = _ext()["client"]
    if client is None:  # an instance without the API key reads the copy
        rows = reports().stock_on_hand(warehouse=warehouse)["rows"]
    else:
        if fresh:
            client.clear_cache()
        rows = _live_stock(client, _ext()["today_fn"], warehouse)
    return _by_product(rows)


def _live_stock(client, today_fn, warehouse):
    from unleashed.reports import Reports
    return Reports(client, today_fn=today_fn).stock_on_hand(warehouse=warehouse)["rows"]


def _by_product(rows):
    return {r["product_code"]: {"qty": r["qty"], "value": r["value"],
                                "avg": (r["value"] / r["qty"]) if r["qty"] else (r["avg_cost"] or 0)} for r in rows}


def _stock_levels(warehouses):
    """{warehouse: {product code: [qty, value]}} for the forms to show what's there: every
    warehouse asked of Unleashed at once (each answer takes it a moment)."""
    from concurrent.futures import ThreadPoolExecutor
    codes = [w.get("WarehouseCode") for w in warehouses]
    client, today_fn = _ext()["client"], _ext()["today_fn"]

    def one(code):
        try:
            found = _by_product(_live_stock(client, today_fn, code)) if client is not None else _stock_in(code, fresh=False)
        except UnleashedError:
            return code, {}
        return code, {k: [round(v["qty"], 4), round(v["value"], 4)] for k, v in found.items()}

    if client is None or len(codes) < 2:
        return dict(one(c) for c in codes)
    with ThreadPoolExecutor(max_workers=min(len(codes), 6)) as pool:
        return dict(pool.map(one, codes))


def _batches(code, warehouse):
    """The batches of a product in a warehouse now: [{"number", "qty", "expiry"}], soonest expiring first."""
    client = _ext()["client"] or _ext()["live_client"]
    try:
        items = client.get_all("BatchNumbers", {"ProductCode": code, "WarehouseCode": warehouse})
    except UnleashedError:
        return []
    from unleashed.client import parse_date
    out = [{"number": b.get("Number"), "qty": float(b.get("Quantity") or 0),
            "expiry": parse_date(b.get("ExpiryDate")).isoformat() if b.get("ExpiryDate") else None}
           for b in items if (b.get("WarehouseCode") in (None, warehouse))
           and (b.get("ProductCode") or "").lower() == code.lower() and float(b.get("Quantity") or 0) > 0]
    return sorted(out, key=lambda b: (b["expiry"] or "9999", b["number"] or ""))


def _code(typed, products):
    typed = (typed or "").strip()
    match = {p["code"].lower(): p["code"] for p in products}
    return match.get(typed.lower(), typed)


def _reasons():
    client = _ext()["client"]
    return adjustment_reasons(client) if client is not None else []


def _new_value(have, new_qty, unit_cost):
    """The stock value after an adjustment to `new_qty`: what's taken out goes at the average
    cost; what's added at `unit_cost`."""
    qty, value, avg = have["qty"], have["value"], have["avg"]
    if new_qty <= qty:
        return avg * new_qty if qty > 0 else 0.0
    return max(value, 0.0) + (new_qty - max(qty, 0.0)) * (unit_cost if unit_cost is not None else avg)


# ---------------------------------------------------------------- recent, history

@bp.get("/transactions")
def transactions():
    r = reports()
    today = r.today
    start = today - dt.timedelta(days=RECENT_DAYS - 1)
    rows = r.transactions(start, today)[::-1]
    return render_template("dash/transactions.html", rows=rows, start=start, today=today,
                           open_transfers=r.transfers(status=",".join(OPEN_TRANSFERS)),
                           counting=_counts().counts("counting"), names=_warehouse_names())


@bp.get("/transactions/history")
def transactions_history():
    r = reports()
    start, end = _range_args()
    doc_type = request.args.get("type") or None
    product = request.args.get("product") or None
    rows = r.transactions(start, end, product=product, doc_type=doc_type)[::-1]
    if _wants_csv():
        return _csv("transactions", [
            ("Date", lambda x: x["date"].isoformat()), ("Type", "type"), ("Number", "number"),
            ("Party", "party"), ("Warehouse", "warehouse"), ("Product Code", "product_code"), ("Qty", "qty"),
            ("Value", lambda x: _round(x["value"], 4)),
        ], rows)
    return render_template("dash/transactions_history.html", rows=rows, start=start, end=end,
                           doc_type=doc_type, product=product, products=r.products(), csv_url=_csv_url())


# ---------------------------------------------------------------- adjustments

def _adjustment_form(form=None, error=None, batch_rows=None, status=200):
    warehouses, default = _warehouses()
    products = _stock_products()
    reasons = _reasons()
    form = form or {}
    lines = []
    if hasattr(form, "getlist"):
        for code, change, new, cost, note in zip_longest(form.getlist("product"), form.getlist("change"),
                                                         form.getlist("new_qty"), form.getlist("unit_cost"),
                                                         form.getlist("line_comment"), fillvalue=""):
            if code or change or new:
                lines.append({"product_code": code, "change": change, "new_qty": new, "unit_cost": cost, "comments": note})
    lines += [{"product_code": "", "change": "", "new_qty": "", "unit_cost": "", "comments": ""}] * max(1, 3 - len(lines))
    return render_template("dash/adjustment_form.html", form=form, error=error, lines=lines, products=products,
                           names={p["code"]: p["name"] for p in products}, warehouses=warehouses,
                           warehouse=form.get("warehouse") or default, reasons=reasons,
                           reason=form.get("reason") or "", levels=_stock_levels(warehouses),
                           batch_rows=batch_rows or {}, write_problem=_write_problem_text()), status


def _write_problem_text():
    from dashboard import _write_problem
    problem = _write_problem()
    return problem.replace("Production orders can't be started or closed out", "Stock can't be changed") \
        .replace("Production orders are started and closed out", "Stock is changed") if problem else None


@bp.route("/adjustments/new", methods=["GET", "POST"])
def adjustment_new():
    if request.method == "GET":
        return _adjustment_form(form={"warehouse": request.args.get("warehouse") or ""})
    blocked = _blocked(url_for("dash.adjustment_new"))
    if blocked:
        return blocked
    form = request.form
    warehouse = form.get("warehouse") or ""
    reason = (form.get("reason") or form.get("reason_other") or "").strip()
    products = _stock_products()
    by_code = {p["code"]: p for p in products}
    try:
        have = _stock_in(warehouse) if warehouse else {}
    except UnleashedError as e:
        return _adjustment_form(form, f"Couldn't read the stock in {warehouse} from Unleashed: {e}", status=400)
    lines, unknown, need_batches = [], [], {}
    for typed, change, new, cost, note in zip_longest(form.getlist("product"), form.getlist("change"),
                                                      form.getlist("new_qty"), form.getlist("unit_cost"),
                                                      form.getlist("line_comment"), fillvalue=""):
        if not (typed or "").strip() and not (change or "").strip() and not (new or "").strip():
            continue
        code = _code(typed, products)
        if code not in by_code:
            unknown.append(code or "(blank)")
            continue
        now = have.get(code, {"qty": 0.0, "value": 0.0, "avg": by_code[code]["cost"] or 0.0})
        new_qty = _number(new)
        if new_qty is None and _number(change) is not None:
            new_qty = now["qty"] + _number(change)
        if new_qty is None:
            return _adjustment_form(form, f"Enter how much {code} changes by, or how much there should be.", status=400)
        if new_qty < 0:
            return _adjustment_form(form, f"That would leave {new_qty:g} of {code} in {warehouse}: there can't be less than 0.",
                                    status=400)
        unit_cost = _number(cost)
        if unit_cost is not None and unit_cost < 0:
            return _adjustment_form(form, f"The cost of {code} can't be below 0.", status=400)
        line = {"product_code": code, "new_qty": new_qty, "comments": note,
                "new_value": _new_value(now, new_qty, unit_cost if unit_cost is not None else (now["avg"] or by_code[code]["cost"] or 0)),
                "had": now["qty"]}
        if by_code[code]["batch"]:
            if form.get(f"batches-shown-{code}"):
                batches = []
                for number, qty, expiry in zip_longest(form.getlist(f"batch-number-{code}"), form.getlist(f"batch-qty-{code}"),
                                                       form.getlist(f"batch-expiry-{code}"), fillvalue=""):
                    number, qty = (number or "").strip(), _number(qty)
                    if not number and qty is None:
                        continue
                    if not number:
                        return _adjustment_form(form, f"Give the batch number for {qty:g} of {code}.",
                                                _batch_rows(form, warehouse), status=400)
                    if qty is None or qty < 0:
                        return _adjustment_form(form, f"Enter how much of batch {number} of {code} there should be.",
                                                _batch_rows(form, warehouse), status=400)
                    batches.append({"number": number[:50], "qty": qty, "expiry": expiry or None})
                line["batches"] = batches
                line["new_qty"] = sum(b["qty"] for b in batches)
                line["new_value"] = _new_value(now, line["new_qty"], unit_cost if unit_cost is not None else (now["avg"] or by_code[code]["cost"] or 0))
            else:
                need_batches[code] = new_qty
        lines.append(line)
    if unknown:
        return _adjustment_form(form, f"{', '.join(unknown)}: not a product that holds stock. Pick one from the list as you "
                                      "type its code or name.", status=400)
    if need_batches:
        return _adjustment_form(form, f"{', '.join(need_batches)} {'is' if len(need_batches) == 1 else 'are'} tracked by batch: "
                                      "say how much of each batch there should be below, then make the adjustment.",
                                _batch_rows(form, warehouse, need_batches), status=400)
    changing = [l for l in lines if abs(l["new_qty"] - l["had"]) > 1e-9 or l.get("batches") is not None]
    if not changing:
        return _adjustment_form(form, "Nothing changes: enter a change or the quantity there should be.", status=400)
    try:
        made = create_adjustment(_ext()["client"], {"warehouse": warehouse, "reason": reason, "lines": changing})
    except (StockWriteError, UnleashedError) as e:
        return _adjustment_form(form, _failed(e), _batch_rows(form, warehouse) if any(
            by_code[l["product_code"]]["batch"] for l in lines) else None, status=400)
    _saved("StockAdjustments", made)
    _done("Made stock adjustment", made.get("AdjustmentNumber"))
    return redirect(url_for("dash.adjustment_detail", number=made.get("AdjustmentNumber")))


def _batch_rows(form, warehouse, wanted=None):
    """{product code: {"rows": [{"number", "had", "qty", "expiry", "existing"}], "target"}} for the batch-tracked
    products on the form: the batches there now (with what was typed for them), and room for a new one."""
    products = {p["code"]: p for p in _stock_products()}
    out = {}
    codes = [_code(c, products.values()) for c in form.getlist("product")]
    for code in codes:
        if code not in products or not products[code]["batch"] or code in out:
            continue
        typed = {}
        if form.get(f"batches-shown-{code}"):
            for number, qty, expiry in zip_longest(form.getlist(f"batch-number-{code}"), form.getlist(f"batch-qty-{code}"),
                                                   form.getlist(f"batch-expiry-{code}"), fillvalue=""):
                if (number or "").strip():
                    typed[number.strip()] = (qty, expiry)
        rows = []
        for b in _batches(code, warehouse):
            qty, expiry = typed.pop(b["number"], (("%g" % b["qty"]), b["expiry"]))
            rows.append({"number": b["number"], "had": b["qty"], "qty": qty, "expiry": b["expiry"], "existing": True})
        for number, (qty, expiry) in typed.items():
            rows.append({"number": number, "had": 0, "qty": qty, "expiry": expiry, "existing": False})
        rows.append({"number": "", "had": 0, "qty": "", "expiry": "", "existing": False})
        out[code] = {"rows": rows, "target": (wanted or {}).get(code), "name": products[code]["name"]}
    return out


@bp.get("/adjustments/<number>")
def adjustment_detail(number):
    found = reports().stock_adjustment(number)
    if found is None:
        abort(404)
    return render_template("dash/adjustment_detail.html", a=found, names=_warehouse_names())


# ---------------------------------------------------------------- transfers

@bp.get("/transfers")
def transfers():
    r = reports()
    start, end = _range_args()
    return render_template("dash/transfers.html", open=r.transfers(status=",".join(OPEN_TRANSFERS)),
                           done=r.transfers(start, end), start=start, end=end)


def _transfer_from(form, products):
    lines = []
    for guid, typed, qty, note in zip_longest(form.getlist("line_guid"), form.getlist("product"), form.getlist("qty"),
                                              form.getlist("line_comment"), fillvalue=""):
        if not (typed or "").strip() and not (qty or "").strip():
            continue
        lines.append({"guid": guid or None, "product_code": _code(typed, products), "qty": _number(qty) or 0,
                      "comments": note})
    return {"source": form.get("source") or "", "destination": form.get("destination") or "",
            "comments": form.get("comments") or "", "lines": lines}


def _transfer_form(found=None, form=None, error=None, status=200):
    warehouses, default = _warehouses()
    products = _stock_products()
    if form is not None and hasattr(form, "getlist"):
        t = _transfer_from(form, products)
        lines = [dict(l, qty=f"{l['qty']:g}" if l["qty"] else "") for l in t["lines"]]
    elif found:
        t = {"source": found["source"], "destination": found["destination"], "comments": found["comments"] or ""}
        lines = [{"guid": l["guid"], "product_code": l["product_code"], "qty": f"{l['qty']:g}", "comments": l["comments"] or ""}
                 for l in found["lines"]]
    else:
        others = [w.get("WarehouseCode") for w in warehouses if w.get("WarehouseCode") != default]
        t = {"source": request.args.get("from") or default, "destination": request.args.get("to") or (others[0] if others else ""),
             "comments": ""}
        lines = []
    lines += [{"guid": None, "product_code": "", "qty": "", "comments": ""}] * max(1, 3 - len(lines))
    return render_template("dash/transfer_form.html", found=found, t=t, lines=lines, error=error, products=products,
                           names={p["code"]: p["name"] for p in products}, warehouses=warehouses,
                           levels=_stock_levels(warehouses), write_problem=_write_problem_text()), status


def _short(transfer):
    """The lines asking for more than the source warehouse has, in words (none if all are there)."""
    have = _stock_in(transfer["source"])
    short = [f"{l['product_code']} ({have.get(l['product_code'], {}).get('qty', 0):g} there)" for l in transfer["lines"]
             if have.get(l["product_code"], {}).get("qty", 0) + 1e-9 < l["qty"]]
    return short


@bp.route("/transfers/new", methods=["GET", "POST"])
def transfer_new():
    if request.method == "GET":
        return _transfer_form()
    blocked = _blocked(url_for("dash.transfer_new"))
    if blocked:
        return blocked
    products = _stock_products()
    t = _transfer_from(request.form, products)
    known = {p["code"] for p in products}
    unknown = [l["product_code"] or "(blank)" for l in t["lines"] if l["product_code"] not in known]
    if unknown:
        return _transfer_form(form=request.form, error=f"{', '.join(unknown)}: not a product that holds stock. Pick one "
                                                       "from the list as you type its code or name.", status=400)
    then = request.form.get("then") or "park"
    if then == "complete":
        try:
            short = _short(t)
        except UnleashedError as e:
            return _transfer_form(form=request.form, error=f"Couldn't read the stock in {t['source']}: {e}", status=400)
        if short:
            return _transfer_form(form=request.form, error=f"{t['source']} hasn't enough of {', '.join(short)} to move "
                                                           "now. Save it parked, or change the quantities.", status=400)
    client = _ext()["client"]
    try:
        made = create_transfer(client, t, place=then in ("place", "complete"))
    except (StockWriteError, UnleashedError) as e:
        return _transfer_form(form=request.form, error=_failed(e), status=400)
    _saved("WarehouseStockTransfers", made)
    number = made.get("TransferOrderNumber")
    if then == "complete":
        try:
            made = complete_transfer(client, made["Guid"])
        except (StockWriteError, UnleashedError) as e:
            flash(f"Made transfer {number}, but it didn't complete: {_failed(e)} It's placed; complete it below.", "warn")
            return redirect(url_for("dash.transfer_detail", number=number))
        _saved("WarehouseStockTransfers", made)
        _done("Made and completed transfer", number)
    else:
        _done("Made transfer", number)
    return redirect(url_for("dash.transfer_detail", number=number))


def _open_transfer(number):
    found = reports().transfer(number)
    if found is None:
        abort(404)
    return found


@bp.get("/transfers/<number>")
def transfer_detail(number):
    found = _open_transfer(number)
    short = []
    if found["status"] in OPEN_TRANSFERS:
        try:
            short = _short(found)
        except UnleashedError:
            short = []
    return render_template("dash/transfer_detail.html", t=found, short=short, open_statuses=OPEN_TRANSFERS,
                           write_problem=_write_problem_text())


@bp.route("/transfers/<number>/edit", methods=["GET", "POST"])
def transfer_edit(number):
    found = _open_transfer(number)
    back = url_for("dash.transfer_detail", number=number)
    if found["status"] not in OPEN_TRANSFERS:
        flash(f"{number} is {found['status'].lower()}, so it can't be changed.", "warn")
        return redirect(back)
    if request.method == "GET":
        return _transfer_form(found=found)
    blocked = _blocked(back)
    if blocked:
        return blocked
    products = _stock_products()
    t = _transfer_from(request.form, products)
    known = {p["code"] for p in products}
    unknown = [l["product_code"] or "(blank)" for l in t["lines"] if l["product_code"] not in known]
    if unknown:
        return _transfer_form(found, request.form, f"{', '.join(unknown)}: not a product that holds stock.", status=400)
    then = request.form.get("then") or "save"
    client = _ext()["client"]
    try:
        made = update_transfer(client, found["guid"], t, place=True if then in ("place", "complete") else None)
    except (StockWriteError, UnleashedError) as e:
        return _transfer_form(found, request.form, _failed(e), status=400)
    _saved("WarehouseStockTransfers", made)
    if then == "complete":
        return _complete(found["guid"], number)
    _done("Saved transfer", number)
    return redirect(back)


def _complete(guid, number):
    try:
        made = complete_transfer(_ext()["client"], guid)
    except (StockWriteError, UnleashedError) as e:
        flash(f"{number} didn't complete: {_failed(e)}", "warn")
        return redirect(url_for("dash.transfer_detail", number=number))
    _saved("WarehouseStockTransfers", made)
    _done("Completed transfer", number)
    return redirect(url_for("dash.transfer_detail", number=number))


@bp.post("/transfers/<number>/complete")
def transfer_complete(number):
    found = _open_transfer(number)
    blocked = _blocked(url_for("dash.transfer_detail", number=number))
    return blocked or _complete(found["guid"], number)


@bp.post("/transfers/<number>/delete")
def transfer_delete(number):
    found = _open_transfer(number)
    blocked = _blocked(url_for("dash.transfer_detail", number=number))
    if blocked:
        return blocked
    try:
        delete_transfer(_ext()["client"], found["guid"])
    except (StockWriteError, UnleashedError) as e:
        flash(f"{number} wasn't deleted: {_failed(e)}", "warn")
        return redirect(url_for("dash.transfer_detail", number=number))
    if _ext().get("store") is not None:  # gone from the copy, whether Unleashed keeps it as deleted or not
        _ext()["store"].delete_document("WarehouseStockTransfers", found["guid"])
        _ext()["store_client"].clear_cache()
    flash(f"Deleted transfer {number}.", "ok")
    return redirect(url_for("dash.transfers"))


# ---------------------------------------------------------------- stock counts

@bp.get("/stock-counts")
def stock_counts():
    return render_template("dash/stock_counts.html", counts=_counts().counts(), names=_warehouse_names())


@bp.route("/stock-counts/new", methods=["GET", "POST"])
def stock_count_new():
    warehouses, default = _warehouses()
    groups = reports().product_groups()
    form = request.form if request.method == "POST" else {"warehouse": request.args.get("warehouse") or default,
                                                          "include_zero": "1"}

    def page(error=None, status=200):
        return render_template("dash/stock_count_new.html", form=form, warehouses=warehouses, groups=groups,
                               error=error, today=reports().today), status

    if request.method == "GET":
        return page()
    warehouse, group = form.get("warehouse") or "", form.get("group") or ""
    try:
        have = _stock_in(warehouse) if warehouse else {}
    except UnleashedError as e:
        return page(f"Couldn't read the stock in {warehouse} from Unleashed: {e}", 400)
    rows = []
    for p in _stock_products():
        if group and p["group"] != group:
            continue
        there = have.get(p["code"])
        if not there and not form.get("include_zero"):
            continue
        row = {"product_code": p["code"], "description": p["name"], "group": p["group"],
               "expected": there["qty"] if there else 0.0,
               "avg_cost": (there["avg"] if there else None) or p["cost"] or 0.0, "batches": None}
        if p["batch"]:
            row["batches"] = [b for b in _batches(p["code"], warehouse)]
        rows.append(row)
    where = _warehouse_names().get(warehouse, warehouse)
    name = form.get("name") or f"{where} {'· ' + group + ' ' if group else ''}count {reports().today.isoformat()}"
    try:
        count_id = _counts().start(name, warehouse, group, rows, _person())
    except CountError as e:
        return page(str(e), 400)
    flash(f"Started {name}: {len(rows)} product{'s' if len(rows) != 1 else ''} to count.", "ok")
    return redirect(url_for("dash.stock_count", count_id=count_id))


@bp.route("/stock-counts/<int:count_id>", methods=["GET", "POST"])
def stock_count(count_id):
    counts = _counts()
    found = counts.count(count_id)
    if found is None:
        abort(404)
    if request.method == "POST":
        entries = [(int(k.split("-", 1)[1]), v, request.form.get(f"orig-{k.split('-', 1)[1]}"))
                   for k, v in request.form.items() if k.startswith("counted-") and k.split("-", 1)[1].isdigit()]
        try:
            changed = counts.record(count_id, entries, _person())
        except CountError as e:
            flash(str(e), "warn")
            return redirect(url_for("dash.stock_count", count_id=count_id))
        flash(f"Saved {changed} count{'s' if changed != 1 else ''}." if changed else "Nothing changed since the page opened.",
              "ok")
        return redirect(url_for("dash.stock_count", count_id=count_id) + (request.form.get("anchor") or ""))
    reasons = _reasons()
    default_reason = next((x for x in reasons if x.lower() == COUNT_REASON.lower()),
                          next((x for x in reasons if "count" in x.lower()), COUNT_REASON))
    return render_template("dash/stock_count.html", c=found, names=_warehouse_names(), reasons=reasons,
                           default_reason=default_reason, hide=request.args.get("blind") == "1",
                           write_problem=_write_problem_text())


@bp.post("/stock-counts/<int:count_id>/post")
def stock_count_post(count_id):
    counts = _counts()
    found = counts.count(count_id)
    if found is None:
        abort(404)
    back = url_for("dash.stock_count", count_id=count_id)
    if found["status"] != "counting":
        flash(f"This count is {found['status_label'].lower()}.", "warn")
        return redirect(back)
    blocked = _blocked(back)
    if blocked:
        return blocked
    if not found["counted"]:
        flash("Nothing's been counted yet.", "warn")
        return redirect(back)
    reason = (request.form.get("reason") or COUNT_REASON).strip()
    changes = differences(found)
    if not changes:
        counts.posted(count_id, reason, None, _person())
        flash("Everything counted matches Unleashed, so there was nothing to adjust. The count is done.", "ok")
        return redirect(back)
    warehouse = found["warehouse"]
    products = {p["code"]: p for p in _stock_products()}
    try:
        have = _stock_in(warehouse)
    except UnleashedError as e:
        flash(f"Couldn't read the stock in {warehouse} from Unleashed: {e}", "warn")
        return redirect(back)
    lines, below = [], []
    for code, change in changes.items():
        now = have.get(code, {"qty": 0.0, "value": 0.0, "avg": 0.0})
        line = {"product_code": code, "comments": f"Stock count: {found['name']}"[:200]}
        if change["batches"]:  # each counted batch moves by its own difference; the others stay as they are
            batches = {b["number"]: b for b in _batches(code, warehouse)}
            for number, moved in change["batches"].items():
                b = batches.setdefault(number, {"number": number, "qty": 0.0, "expiry": None})
                b["qty"] = b["qty"] + moved
            if any(b["qty"] < -1e-9 for b in batches.values()):
                below.append(code)
                continue
            line["batches"] = [b for b in batches.values() if b["qty"] > 1e-9]
            line["new_qty"] = sum(b["qty"] for b in line["batches"])
        else:
            line["new_qty"] = now["qty"] + change["change"]
        if line["new_qty"] < -1e-9:
            below.append(code)
            continue
        line["new_qty"] = max(line["new_qty"], 0.0)
        unit = now["avg"] or change["avg_cost"] or (products.get(code) or {}).get("cost") or 0.0
        line["new_value"] = _new_value(dict(now, avg=now["avg"] or unit), line["new_qty"], unit)
        lines.append(line)
    if below:
        flash(f"{', '.join(below)} would go below 0: stock went out of {warehouse} since the count started. "
              "Recount them, then post.", "warn")
        return redirect(back)
    try:
        made = create_adjustment(_ext()["client"], {"warehouse": warehouse, "reason": reason, "lines": lines})
    except (StockWriteError, UnleashedError) as e:
        flash(f"The count wasn't posted: {_failed(e)}", "warn")
        return redirect(back)
    _saved("StockAdjustments", made)
    counts.posted(count_id, reason, made.get("AdjustmentNumber"), _person())
    _done("Posted the count as stock adjustment", made.get("AdjustmentNumber"))
    return redirect(back)


@bp.post("/stock-counts/<int:count_id>/cancel")
def stock_count_cancel(count_id):
    _counts().cancel(count_id)
    flash("Cancelled the count. Nothing was sent to Unleashed.", "ok")
    return redirect(url_for("dash.stock_counts"))
