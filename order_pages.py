"""Sales and purchase orders made, changed, completed and received in Core, written to
Unleashed (unleashed/order_writes.py). Anyone who can open Sales (or Purchases) can use them;
Unleashed records the change as the API user, and Core says who did it in its message."""
import datetime as dt
from itertools import zip_longest

from flask import abort, flash, redirect, render_template, request, url_for

from dashboard import (_ext, _person, _sync_soon, _write_problem, bp, reports)
from unleashed.client import UnleashedError, parse_date
from unleashed.order_writes import (OPEN_PURCHASES, OPEN_SALES, RECEIVED_PURCHASES, OrderError,
                                    complete_purchase_order, complete_sales_order, create_purchase_order,
                                    create_sales_order, delete_purchase_order, delete_sales_order,
                                    receive_purchase_order, update_purchase_order, update_sales_order)
from unleashed.sync import RECHECK_FROM


# ---------------------------------------------------------------- shared

def _number(value):
    try:
        return float(str(value).replace(",", "").strip()) if str(value or "").strip() else None
    except ValueError:
        return None


def _date(value, default=None):
    try:
        return dt.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _lines_from_form(form):
    """The order lines typed: [{"guid", "product_code", "qty", "unit_price"}], blank rows left out."""
    lines = []
    for guid, code, qty, price in zip_longest(form.getlist("line_guid"), form.getlist("product"),
                                              form.getlist("qty"), form.getlist("price"), fillvalue=""):
        if not (code or "").strip() and not (qty or "").strip():
            continue
        lines.append({"guid": guid or None, "product_code": (code or "").strip(), "qty": _number(qty) or 0,
                      "unit_price": _number(price)})
    return lines


def _saved(endpoint, doc):
    """Keep what Unleashed now holds in the shared copy, so it shows straight away, and have
    the stock on the days it moved fetched again (as for documents changed in Unleashed)."""
    ext = _ext()
    if not doc or ext.get("store") is None:
        return
    _, dates = ext["store"].upsert_documents(endpoint, [doc])
    if dates:
        first, pending = min(dates).isoformat(), ext["store"].get_state(RECHECK_FROM)
        ext["store"].set_state(RECHECK_FROM, min(first, pending) if pending else first)
    ext["store_client"].clear_cache()


def _live(endpoint, guid):
    """The order as Unleashed holds it now (not the copy), or None."""
    client = _ext()["client"]
    client.clear_cache()
    try:
        doc = client.get(f"{endpoint}/{guid}")
    except UnleashedError:
        return None
    return doc if isinstance(doc, dict) and doc.get("Guid") else None


def _choices(kind):
    r = reports()
    products = [p for p in r.products() if not p.get("Obsolete")]
    if kind == "sales":
        products = [p for p in products if p.get("IsSellable") is not False]
    price = "DefaultSellPrice" if kind == "sales" else "DefaultPurchasePrice"
    options = sorted(({"code": p.get("ProductCode"), "name": p.get("ProductDescription") or "",
                       "price": p.get(price), "unit": (p.get("UnitOfMeasure") or {}).get("Name")}
                      for p in products), key=lambda p: (p["code"] or "").lower())
    warehouses = r.warehouses()
    default = next((w.get("WarehouseCode") for w in warehouses if w.get("IsDefault")),
                   warehouses[0].get("WarehouseCode") if warehouses else None)
    return {"product_options": options, "warehouses": warehouses, "default_warehouse": default,
            "products_raw": products}


def _failed(e):
    return str(e) if isinstance(e, OrderError) else f"Unleashed said: {e}"


def _blocked(back):
    problem = _write_problem()
    if problem:
        flash(problem, "warn")
        return redirect(back)
    return None


def _done(text, number):
    flash(f"{text} {number}. Unleashed records it as the API user; {_person()} did it in Core.", "ok")


# ---------------------------------------------------------------- sales orders

def _sales_form(found=None, form=None, error=None):
    r = reports()
    choices = _choices("sales")
    stores = r.cogs_settings()["stores"]
    customers = sorted(r.customers(), key=lambda c: (c.get("CustomerCode") not in stores,
                                                      (c.get("CustomerName") or "").lower()))
    if form is not None and hasattr(form, "getlist"):
        values = {k: form.get(k, "") for k in ("customer", "warehouse", "order_date", "required_date",
                                                "reference", "comments")}
        lines = _lines_from_form(form)
    elif found:
        values = {"customer": found["customer_code"], "warehouse": found["warehouse"] or choices["default_warehouse"],
                  "order_date": found["order_date"].isoformat() if found["order_date"] else "",
                  "required_date": found["required_date"].isoformat() if found.get("required_date") else "",
                  "reference": found.get("reference") or "", "comments": found.get("comments") or ""}
        lines = [{"guid": l["guid"], "product_code": l["product_code"], "qty": l["qty"],
                  "unit_price": l["unit_price"]} for l in found["lines"]]
    else:
        values = {"customer": request.args.get("customer", ""), "warehouse": choices["default_warehouse"],
                  "order_date": reports().today.isoformat(), "required_date": "", "reference": "", "comments": ""}
        lines = []
    lines += [{"guid": None, "product_code": "", "qty": None, "unit_price": None}] * max(1, 3 - len(lines))
    html = render_template("dash/order_form.html", kind="sales", found=found, values=values, lines=lines,
                           parties=[(c.get("CustomerCode"), c.get("CustomerName") or c.get("CustomerCode"))
                                    for c in customers if not c.get("Obsolete")],
                           stores=stores, error=error, write_problem=_write_problem(), **choices)
    return (html, 400) if error else html


def _sales_order_from(form):
    return {"customer": form.get("customer", ""), "warehouse": form.get("warehouse") or None,
            "order_date": _date(form.get("order_date"), reports().today),
            "required_date": _date(form.get("required_date")),
            "reference": (form.get("reference") or "").strip()[:500],
            "comments": (form.get("comments") or "").strip()[:2048], "lines": _lines_from_form(form)}


@bp.route("/sales/new", methods=["GET", "POST"])
def sales_order_new():
    if request.method == "GET":
        return _sales_form()
    blocked = _blocked(url_for("dash.sales_order_new"))
    if blocked:
        return blocked
    order = _sales_order_from(request.form)
    if not order["customer"]:
        return _sales_form(form=request.form, error="Choose the customer.")
    try:
        made = create_sales_order(_ext()["client"], order, _choices("sales")["products_raw"])
    except (OrderError, UnleashedError) as e:
        return _sales_form(form=request.form, error=_failed(e))
    _saved("SalesOrders", made)
    _done("Made sales order", made.get("OrderNumber"))
    return redirect(url_for("dash.sales_detail", number=made.get("OrderNumber")))


def _open_sales_order(number):
    so = reports().sales_order(number)
    if so is None:
        abort(404)
    return so


@bp.route("/sales/<number>/edit", methods=["GET", "POST"])
def sales_order_edit(number):
    so = _open_sales_order(number)
    back = url_for("dash.sales_detail", number=number)
    if so["status"] not in OPEN_SALES:
        flash(f"{number} is {so['status'].lower()}, so it can't be changed.", "warn")
        return redirect(back)
    if request.method == "GET":
        return _sales_form(found=so)
    blocked = _blocked(back)
    if blocked:
        return blocked
    order = _sales_order_from(request.form)
    order["customer"] = so["customer_code"]
    try:
        changed = update_sales_order(_ext()["client"], so["guid"], order, _choices("sales")["products_raw"])
    except (OrderError, UnleashedError) as e:
        _saved("SalesOrders", _live("SalesOrders", so["guid"]))  # whatever part of it was kept
        return _sales_form(found=so, form=request.form, error=_failed(e))
    _saved("SalesOrders", changed)
    _done("Saved", number)
    return redirect(back)


@bp.route("/sales/<number>/complete", methods=["GET", "POST"])
def sales_order_complete(number):
    so = _open_sales_order(number)
    back = url_for("dash.sales_detail", number=number)
    if so["status"] not in OPEN_SALES:
        flash(f"{number} is {so['status'].lower()}, so it can't be completed.", "warn")
        return redirect(back)
    r = reports()
    client = _ext()["client"]
    tracked = [l for l in so["lines"] if r.batch_tracked(l["product_code"])]
    available = {}
    for l in tracked:
        try:
            batches = client.get_all("BatchNumbers", {"ProductCode": l["product_code"],
                                                      "WarehouseCode": so["warehouse"]}) if client else []
        except UnleashedError:
            batches = []
        available[l["guid"]] = sorted(
            ({"number": b.get("Number") or b.get("BatchNumber"), "qty": float(b.get("Quantity") or 0),
              "expiry": parse_date(b.get("ExpiryDate"))} for b in batches if float(b.get("Quantity") or 0) > 0),
            key=lambda b: (b["expiry"] or dt.date.max, b["number"] or ""))

    def page(error=None, chosen=None):
        if chosen is None:  # first expiring first
            chosen = {}
            for l in tracked:
                need, picks = l["qty"], []
                for b in available[l["guid"]]:
                    if need <= 1e-9:
                        break
                    take = min(need, b["qty"])
                    picks.append({"number": b["number"], "qty": take})
                    need -= take
                chosen[l["guid"]] = picks or [{"number": "", "qty": l["qty"]}]
        html = render_template("dash/sales_complete.html", so=so, tracked=tracked, available=available,
                               chosen=chosen, error=error, write_problem=_write_problem())
        return (html, 400) if error else html

    if request.method == "GET":
        return page()
    blocked = _blocked(back)
    if blocked:
        return blocked
    chosen = {}
    for l in tracked:
        picks = [{"number": n.strip(), "qty": _number(q) or 0}
                 for n, q in zip_longest(request.form.getlist(f"batch-{l['guid']}"),
                                         request.form.getlist(f"batch-qty-{l['guid']}"), fillvalue="")
                 if (n or "").strip() or (q or "").strip()]
        chosen[l["guid"]] = picks
        if not picks or any(not p["number"] or p["qty"] <= 0 for p in picks):
            return page(f"Choose the batches of {l['product_code']} that go out, with how many of each.", chosen)
    try:
        done = complete_sales_order(client, so["guid"], chosen or None)
    except (OrderError, UnleashedError) as e:
        _saved("SalesOrders", _live("SalesOrders", so["guid"]))
        return page(_failed(e), chosen)
    _saved("SalesOrders", done)
    _done("Completed", number)
    _sync_soon()  # the stock moved: fetch it
    return redirect(back)


@bp.post("/sales/<number>/delete")
def sales_order_delete(number):
    so = _open_sales_order(number)
    blocked = _blocked(url_for("dash.sales_detail", number=number))
    if blocked:
        return blocked
    try:
        delete_sales_order(_ext()["client"], so["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(url_for("dash.sales_detail", number=number))
    gone = _live("SalesOrders", so["guid"])
    if gone:
        _saved("SalesOrders", gone)
    else:
        _ext()["store"].delete_document("SalesOrders", so["guid"])
        _ext()["store_client"].clear_cache()
    _done("Deleted", number)
    return redirect(url_for("dash.sales"))


# ---------------------------------------------------------------- purchase orders

def _purchase_form(found=None, form=None, error=None):
    r = reports()
    choices = _choices("purchases")
    suppliers = sorted(r.suppliers(), key=lambda s: (s.get("SupplierName") or "").lower())
    if form is not None and hasattr(form, "getlist"):
        values = {k: form.get(k, "") for k in ("supplier", "warehouse", "order_date", "delivery_date",
                                                "reference", "comments", "place")}
        lines = _lines_from_form(form)
    elif found:
        values = {"supplier": found["supplier_code"], "warehouse": found["warehouse"] or choices["default_warehouse"],
                  "order_date": found["order_date"].isoformat() if found["order_date"] else "",
                  "delivery_date": found["required_date"].isoformat() if found.get("required_date") else "",
                  "reference": found.get("reference") or "", "comments": found.get("comments") or "",
                  "place": "1" if found["status"] == "Placed" else ""}
        lines = [{"guid": l["guid"], "product_code": l["product_code"], "qty": l["qty"],
                  "unit_price": l["unit_price"]} for l in found["lines"]]
    else:
        values = {"supplier": request.args.get("supplier", ""), "warehouse": choices["default_warehouse"],
                  "order_date": reports().today.isoformat(), "delivery_date": "", "reference": "", "comments": "",
                  "place": "1"}
        lines = []
    lines += [{"guid": None, "product_code": "", "qty": None, "unit_price": None}] * max(1, 3 - len(lines))
    html = render_template("dash/order_form.html", kind="purchases", found=found, values=values, lines=lines,
                           parties=[(s.get("SupplierCode"), s.get("SupplierName") or s.get("SupplierCode"))
                                    for s in suppliers if not s.get("Obsolete")],
                           stores=[], error=error, write_problem=_write_problem(), **choices)
    return (html, 400) if error else html


def _purchase_order_from(form):
    return {"supplier": form.get("supplier", ""), "warehouse": form.get("warehouse") or None,
            "order_date": _date(form.get("order_date"), reports().today),
            "delivery_date": _date(form.get("delivery_date")),
            "reference": (form.get("reference") or "").strip()[:500],
            "comments": (form.get("comments") or "").strip()[:1024], "lines": _lines_from_form(form)}


@bp.route("/purchases/new", methods=["GET", "POST"])
def purchase_order_new():
    if request.method == "GET":
        return _purchase_form()
    blocked = _blocked(url_for("dash.purchase_order_new"))
    if blocked:
        return blocked
    order = _purchase_order_from(request.form)
    if not order["supplier"]:
        return _purchase_form(form=request.form, error="Choose the supplier.")
    try:
        made = create_purchase_order(_ext()["client"], order, _choices("purchases")["products_raw"],
                                     place=bool(request.form.get("place")))
    except (OrderError, UnleashedError) as e:
        return _purchase_form(form=request.form, error=_failed(e))
    _saved("PurchaseOrders", made)
    _done("Made purchase order", made.get("OrderNumber"))
    return redirect(url_for("dash.purchase_detail", number=made.get("OrderNumber")))


def _open_purchase_order(number):
    po = reports().purchase_order(number)
    if po is None:
        abort(404)
    return po


@bp.route("/purchases/<number>/edit", methods=["GET", "POST"])
def purchase_order_edit(number):
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    if po["status"] not in OPEN_PURCHASES:
        flash(f"{number} is {po['status'].lower()}, so it can't be changed.", "warn")
        return redirect(back)
    if request.method == "GET":
        return _purchase_form(found=po)
    blocked = _blocked(back)
    if blocked:
        return blocked
    order = _purchase_order_from(request.form)
    order["supplier"] = po["supplier_code"]
    try:
        changed = update_purchase_order(_ext()["client"], po["guid"], order, _choices("purchases")["products_raw"],
                                        place=True if request.form.get("place") else None)
    except (OrderError, UnleashedError) as e:
        _saved("PurchaseOrders", _live("PurchaseOrders", po["guid"]))
        return _purchase_form(found=po, form=request.form, error=_failed(e))
    _saved("PurchaseOrders", changed)
    _done("Saved", number)
    return redirect(back)


@bp.route("/purchases/<number>/receive", methods=["GET", "POST"])
def purchase_order_receive(number):
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    if po["status"] not in OPEN_PURCHASES:
        flash(f"{number} is {po['status'].lower()}, so it can't be received.", "warn")
        return redirect(back)
    r = reports()
    tracked = {l["guid"] for l in po["lines"] if r.batch_tracked(l["product_code"])}
    form = request.form

    def page(error=None):
        rows = []
        for l in po["lines"]:
            got = form.get(f"qty-{l['guid']}") if form else None
            batches = [{"number": n, "qty": q, "expiry": e} for n, q, e in zip_longest(
                form.getlist(f"batch-{l['guid']}"), form.getlist(f"batch-qty-{l['guid']}"),
                form.getlist(f"batch-expiry-{l['guid']}"), fillvalue="")] if form else []
            rows.append(dict(l, typed=got if got is not None else f"{l['qty']:g}",
                             tracked=l["guid"] in tracked,
                             batches=batches or [{"number": "", "qty": "", "expiry": ""}]))
        html = render_template("dash/purchase_receive.html", po=po, rows=rows, error=error, today=r.today,
                               complete=form.get("complete") if form else "1", write_problem=_write_problem())
        return (html, 400) if error else html

    if request.method == "GET":
        return page()
    blocked = _blocked(back)
    if blocked:
        return blocked
    receipts, whole = {}, True
    for l in po["lines"]:
        qty = _number(form.get(f"qty-{l['guid']}"))
        if qty is None or qty < 0:
            return page(f"Enter how much of {l['product_code']} arrived (0 if none).")
        whole = whole and abs(qty - l["qty"]) < 1e-9
        batches = []
        if l["guid"] in tracked and qty > 0:
            for n, q, e in zip_longest(form.getlist(f"batch-{l['guid']}"), form.getlist(f"batch-qty-{l['guid']}"),
                                       form.getlist(f"batch-expiry-{l['guid']}"), fillvalue=""):
                if not (n or "").strip() and not (q or "").strip():
                    continue
                if not (n or "").strip() or not _number(q):
                    return page(f"Give each batch of {l['product_code']} a number and a quantity.")
                batches.append({"number": n.strip()[:50], "qty": _number(q), "expiry": _date(e)})
            if not batches:
                return page(f"{l['product_code']} is tracked by batch: enter the batch number (and expiry) "
                            "of what arrived.")
        receipts[l["guid"]] = {"qty": qty, "batches": batches}
    complete = bool(form.get("complete"))
    if complete and not whole:
        return page("Complete the order only when everything ordered arrived. Untick it to receive part of it.")
    try:
        received = receive_purchase_order(_ext()["client"], po["guid"], receipts, complete=complete)
    except (OrderError, UnleashedError) as e:
        _saved("PurchaseOrders", _live("PurchaseOrders", po["guid"]))
        return page(_failed(e))
    _saved("PurchaseOrders", received)
    _done("Received and completed" if complete else "Received", number)
    _sync_soon()
    return redirect(back)


@bp.post("/purchases/<number>/complete")
def purchase_order_complete(number):
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    if po["status"] not in RECEIVED_PURCHASES:
        flash(f"{number} is {po['status'].lower()}: receive it first.", "warn")
        return redirect(back)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        done = complete_purchase_order(_ext()["client"], po["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    _saved("PurchaseOrders", done)
    _done("Completed", number)
    return redirect(back)


@bp.post("/purchases/<number>/delete")
def purchase_order_delete(number):
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        delete_purchase_order(_ext()["client"], po["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    gone = _live("PurchaseOrders", po["guid"])
    if gone:
        _saved("PurchaseOrders", gone)
    else:
        _ext()["store"].delete_document("PurchaseOrders", po["guid"])
        _ext()["store_client"].clear_cache()
    _done("Deleted", number)
    return redirect(url_for("dash.purchases"))

