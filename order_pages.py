"""Sales and purchase orders made, changed, completed and received in Core, written to
Unleashed (unleashed/order_writes.py). Anyone who can open Sales (or Purchases) can use them;
Unleashed records the change as the API user, and Core says who did it in its message."""
import datetime as dt
import re
from itertools import zip_longest

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from dashboard import (_ext, _person, _sync_soon, _write_problem, bp, current_user, reports)
from unleashed.client import UnleashedError, parse_date
from unleashed.order_writes import (OPEN_PURCHASES, OPEN_SALES, RECEIVED_PURCHASES, OrderError, complete_credit,
                                    purchasable_units, purchase_unit, same_unit,
                                    complete_purchase_order, complete_sales_order, complete_supplier_return,
                                    create_credit, create_purchase_order, create_sales_order,
                                    create_supplier_return, delete_credit, delete_purchase_order, delete_sales_order,
                                    delete_supplier_return, receive_purchase_order, update_purchase_order,
                                    update_sales_order)
from unleashed.sync import RECHECK_FROM
from vendor_email import (SENDABLE, MailError, addresses, build_message, can_send, mail_settings, named, send)


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


def _lines_from_form(form, codes=None):
    """The order lines typed: [{"guid", "product_code", "qty", "unit_price"}], blank rows left out,
    then the charges (freight, fuel surcharge: one of each, at the amount, with a note). `codes`:
    {lowercase code: code} to match typed product codes to; a line given something else keeps it,
    for the page to say so."""
    lines = []
    # A purchase order's lines each have a unit too (blank: the product's purchasing unit).
    units = form.getlist("unit") or [""] * len(form.getlist("product"))
    for guid, code, qty, price, unit in zip_longest(form.getlist("line_guid"), form.getlist("product"),
                                                    form.getlist("qty"), form.getlist("price"), units, fillvalue=""):
        if not (code or "").strip() and not (qty or "").strip():
            continue
        code = (code or "").strip()
        code = (codes or {}).get(code.lower(), code)
        lines.append({"guid": guid or None, "product_code": code, "qty": _number(qty) or 0,
                      "unit_price": _number(price), **({"unit_name": unit.strip()} if (unit or "").strip() else {})})
    for guid, code, amount, note in zip_longest(form.getlist("charge_guid"), form.getlist("charge"),
                                                form.getlist("charge_amount"), form.getlist("charge_note"), fillvalue=""):
        if not (code or "").strip() or _number(amount) is None:
            continue
        lines.append({"guid": guid or None, "product_code": code.strip(), "qty": 1, "unit_price": _number(amount),
                      "comments": (note or "").strip()[:1024] or None, "charge": True})
    return lines


def _unknown_products(lines, choices):
    """The typed product codes that aren't products, for an error message."""
    known = {o["code"] for o in choices["product_options"]} | {o["code"] for o in choices["charge_options"]}
    return [l["product_code"] for l in lines if l["product_code"] not in known]


def _next_number(endpoint, prefix):
    """The number Unleashed should give the next order (its numbering goes on from the highest
    one in the copy): "PO-00000281". Unleashed gives the final one when it's made."""
    best = None
    for number in _ext()["store"].document_numbers(endpoint) if _ext().get("store") else []:
        m = re.fullmatch(r"(\D*?)(\d+)", number or "")
        if m and m.group(1) == prefix and (best is None or int(m.group(2)) > int(best.group(2))):
            best = m
    if best is None:
        return None
    return f"{prefix}{int(best.group(2)) + 1:0{len(best.group(2))}d}"


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
    charges = r.charge_codes()

    last_paid = _last_paid() if kind == "purchases" else {}

    def unit_choice(p, unit):
        """A unit the product can be bought in, at its price: for the stock unit the default purchase
        price; for another (a case of 12), the supplier's price for one in Unleashed, or what was
        last paid for one. (The default purchase price is for a stock unit, and which way Unleashed's
        conversion rate runs isn't documented, so Core doesn't convert one into the other.)"""
        if unit["stock"]:
            return {"name": unit.get("Name"), "price": p.get(price), "per": 1, "stock": True}
        supplier = p.get("Supplier") or {}
        paid = last_paid.get((p.get("ProductCode"), (unit.get("Name") or "").lower())) or {}
        cost = supplier.get("SupplierProductPrice") if same_unit(supplier.get("UnitOfMeasure"), unit) and \
            supplier.get("SupplierProductPrice") is not None else paid.get("price")
        return {"name": unit.get("Name"), "price": cost, "per": paid.get("per"), "stock": False}

    def option(p):
        base = (p.get("UnitOfMeasure") or {}).get("Name")
        if kind != "purchases":
            return {"code": p.get("ProductCode"), "name": p.get("ProductDescription") or "", "price": p.get(price),
                    "unit": base}
        # Every unit it can be bought in; the one Unleashed says it's bought in comes chosen.
        units = [unit_choice(p, u) for u in purchasable_units(p)] or [{"name": base, "price": p.get(price), "per": 1,
                                                                        "stock": True}]
        unit = purchase_unit(p)
        chosen = next((u for u in units if unit and (u["name"] or "").lower() == (unit.get("Name") or "").lower()),
                      units[0])
        return {"code": p.get("ProductCode"), "name": p.get("ProductDescription") or "", "price": chosen["price"],
                "unit": chosen["name"], "stock_unit": base if not chosen["stock"] else None,
                "stock_per_unit": chosen["per"] if not chosen["stock"] else None, "units": units}

    options = sorted((option(p) for p in products if (p.get("ProductCode") or "").upper() not in charges),
                     key=lambda p: (p["code"] or "").lower())
    charge_options = sorted((option(p) for p in r.products() if not p.get("Obsolete")
                             and (p.get("ProductCode") or "").upper() in charges),
                            key=lambda p: (p["code"] or "").lower()) if kind == "purchases" else []
    warehouses = r.warehouses()
    default = next((w.get("WarehouseCode") for w in warehouses if w.get("IsDefault")),
                   warehouses[0].get("WarehouseCode") if warehouses else None)
    return {"product_options": options, "charge_options": charge_options, "warehouses": warehouses,
            "options_by_code": {o["code"]: o for o in options},
            "default_warehouse": default, "products_raw": products,
            "codes": {o["code"].lower(): o["code"] for o in options + charge_options if o["code"]},
            "product_names": {o["code"]: o["name"] for o in options + charge_options}}


def _last_paid():
    """{(product code, purchasing unit): {"price": what one was last paid for, "per": the stock
    units in one}} from the last six months' purchase orders, for products bought by the case."""
    r, found = reports(), {}
    try:
        orders = r.purchase_orders(r.today - dt.timedelta(days=183), r.today)
    except UnleashedError:
        return found
    for o in sorted(orders, key=lambda o: o["order_date"] or dt.date.min):
        if o["status"] == "Deleted":
            continue
        for l in o["lines"]:
            if l.get("stock_per_unit") and l.get("order_unit") and l.get("order_price") is not None:
                found[(l["product_code"], l["order_unit"].lower())] = {"price": round(l["order_price"], 4),
                                                                      "per": l["stock_per_unit"]}
    return found


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
        lines = _lines_from_form(form, choices["codes"])
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
                           charges=[], next_number=None if found else _next_number("SalesOrders", "SO-"),
                           parties=[(c.get("CustomerCode"), c.get("CustomerName") or c.get("CustomerCode"))
                                    for c in customers if not c.get("Obsolete")],
                           stores=stores, error=error, write_problem=_write_problem(), **choices)
    return (html, 400) if error else html


def _sales_order_from(form):
    return {"customer": form.get("customer", ""), "warehouse": form.get("warehouse") or None,
            "order_date": _date(form.get("order_date"), reports().today),
            "required_date": _date(form.get("required_date")),
            "reference": (form.get("reference") or "").strip()[:500],
            "comments": (form.get("comments") or "").strip()[:2048], "lines": _lines_from_form(form, _choices("sales")["codes"])}


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
    unknown = _unknown_products(order["lines"], _choices("sales"))
    if unknown:
        return _sales_form(form=request.form, error=f"{', '.join(unknown)}: not a product in Unleashed. Pick one from the "
                                                    "list as you type its code or name.")
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
    unknown = _unknown_products(order["lines"], _choices("sales"))
    if unknown:
        return _sales_form(found=so, form=request.form, error=f"{', '.join(unknown)}: not a product in Unleashed. Pick "
                                                              "one from the list as you type its code or name.")
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
        lines = _lines_from_form(form, choices["codes"])
    elif found:
        values = {"supplier": found["supplier_code"], "warehouse": found["warehouse"] or choices["default_warehouse"],
                  "order_date": found["order_date"].isoformat() if found["order_date"] else "",
                  "delivery_date": found["required_date"].isoformat() if found.get("required_date") else "",
                  "reference": found.get("reference") or "", "comments": found.get("comments") or "",
                  "place": "1" if found["status"] == "Placed" else ""}
        # As ordered: in the unit each line was bought in, at the price for one.
        def unit_of(l):
            if l.get("stock_per_unit"):
                return l.get("order_unit")
            stock = [u for u in (choices["options_by_code"].get(l["product_code"]) or {}).get("units", []) if u["stock"]]
            return stock[0]["name"] if stock else None
        lines = [{"guid": l["guid"], "product_code": l["product_code"], "qty": l["order_qty"],
                  "unit_price": round(l["order_price"], 4) if l["order_price"] is not None else None,
                  "unit_name": unit_of(l)}
                 for l in found["lines"]]
        lines += [{"guid": c["guid"], "product_code": c["product_code"], "qty": c["qty"], "unit_price": c["unit_price"],
                   "comments": c["comments"], "charge": True}
                  for c in found["cost_lines"] if c.get("field") == "PurchaseOrderLines" and c.get("guid")]
    else:
        values = {"supplier": request.args.get("supplier", ""), "warehouse": choices["default_warehouse"],
                  "order_date": reports().today.isoformat(), "delivery_date": "", "reference": "", "comments": "",
                  "place": "1"}
        lines = []
    charges = [l for l in lines if l.get("charge")]
    lines = [l for l in lines if not l.get("charge")]
    lines += [{"guid": None, "product_code": "", "qty": None, "unit_price": None}] * max(1, 3 - len(lines))
    charges += [{"guid": None, "product_code": "", "unit_price": None, "comments": ""}] * (1 if choices["charge_options"] else 0)
    html = render_template("dash/order_form.html", kind="purchases", found=found, values=values, lines=lines,
                           charges=charges, next_number=None if found else _next_number("PurchaseOrders", "PO-"),
                           parties=[(s.get("SupplierCode"), s.get("SupplierName") or s.get("SupplierCode"))
                                    for s in suppliers if not s.get("Obsolete")],
                           stores=[], error=error, write_problem=_write_problem(), **choices)
    return (html, 400) if error else html


def _purchase_order_from(form):
    return {"supplier": form.get("supplier", ""), "warehouse": form.get("warehouse") or None,
            "order_date": _date(form.get("order_date"), reports().today),
            "delivery_date": _date(form.get("delivery_date")),
            "reference": (form.get("reference") or "").strip()[:500],
            "comments": (form.get("comments") or "").strip()[:1024], "lines": _lines_from_form(form, _choices("purchases")["codes"])}


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
    unknown = _unknown_products(order["lines"], _choices("purchases"))
    if unknown:
        return _purchase_form(form=request.form, error=f"{', '.join(unknown)}: not a product in Unleashed. Pick one from "
                                                       "the list as you type its code or name.")
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
    unknown = _unknown_products(order["lines"], _choices("purchases"))
    if unknown:
        return _purchase_form(found=po, form=request.form, error=f"{', '.join(unknown)}: not a product in Unleashed. "
                                                                 "Pick one from the list as you type its code or name.")
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
            rows.append(dict(l, typed=got if got is not None else f"{l['order_qty']:g}",
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
        whole = whole and abs(qty - l["order_qty"]) < 1e-9
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
    for c in po["cost_lines"]:  # charges on the order (freight, fuel surcharge) are received with it
        if c.get("field") == "PurchaseOrderLines" and c.get("guid"):
            receipts[c["guid"]] = {"qty": c["qty"], "batches": []}
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



# ---------------------------------------------------------------- credit notes (customer returns)

def _range():
    today = reports().today
    start = _date(request.args.get("start"), today.replace(day=1) - dt.timedelta(days=60))
    end = _date(request.args.get("end"), today)
    return start, end


@bp.get("/sales/credits")
def credit_notes():
    start, end = _range()
    rows = sorted(reports().credit_notes(start, end), key=lambda c: (c["date"] or dt.date.min, c["number"] or ""),
                  reverse=True)
    return render_template("dash/credits.html", rows=[c for c in rows if c["status"] != "Deleted"],
                           start=start, end=end)


@bp.get("/sales/credits/<number>")
def credit_detail(number):
    found = reports().credit_note(number)
    if found is None:
        abort(404)
    return render_template("dash/credit_detail.html", c=found, write_problem=_write_problem())


@bp.route("/sales/<number>/credit", methods=["GET", "POST"])
def sales_order_credit(number):
    """A credit for (some of) what went out on a completed sales order: how much came back or
    is credited, at what price, why, and whether it goes back into stock."""
    so = _open_sales_order(number)
    back = url_for("dash.sales_detail", number=number)
    if so["status"] != "Completed":
        flash(f"{number} isn't completed: change the order instead of crediting it.", "warn")
        return redirect(back)
    r = reports()
    tracked = {l["guid"] for l in so["lines"] if r.batch_tracked(l["product_code"])}
    form = request.form

    def page(error=None):
        rows = []
        for l in so["lines"]:
            g = l["guid"]
            rows.append(dict(l, tracked=g in tracked,
                             typed_qty=form.get(f"qty-{g}", "") if form else "",
                             typed_price=form.get(f"price-{g}") if form else f"{l['unit_price']:g}",
                             typed_reason=form.get(f"reason-{g}") if form else "Return",
                             typed_back=(form.get(f"back-{g}") if form else "1"),
                             batches=[{"number": n, "qty": q, "expiry": e} for n, q, e in zip_longest(
                                 form.getlist(f"batch-{g}"), form.getlist(f"batch-qty-{g}"),
                                 form.getlist(f"batch-expiry-{g}"), fillvalue="")] if form else
                             ([{"number": b["number"], "qty": "", "expiry": ""} for b in l.get("batches") or []]
                              or [{"number": "", "qty": "", "expiry": ""}])))
        html = render_template("dash/sales_credit.html", so=so, rows=rows, error=error, today=r.today,
                               values={"date": form.get("date") if form else r.today.isoformat(),
                                       "reference": form.get("reference") if form else number,
                                       "comments": form.get("comments", "") if form else "",
                                       "complete": form.get("complete") if form else "1"},
                               write_problem=_write_problem())
        return (html, 400) if error else html

    if request.method == "GET":
        return page()
    blocked = _blocked(back)
    if blocked:
        return blocked
    lines = []
    for l in so["lines"]:
        g = l["guid"]
        qty = _number(form.get(f"qty-{g}")) or 0
        if qty < 0 or qty > l["qty"] + 1e-9:
            return page(f"{l['product_code']}: credit between 0 and the {l['qty']:g} that went out.")
        if not qty:
            continue
        price = _number(form.get(f"price-{g}"))
        back_in = bool(form.get(f"back-{g}"))
        batches = []
        if back_in and g in tracked:
            for n, q, e in zip_longest(form.getlist(f"batch-{g}"), form.getlist(f"batch-qty-{g}"),
                                       form.getlist(f"batch-expiry-{g}"), fillvalue=""):
                if not (n or "").strip() and not (q or "").strip():
                    continue
                if not (n or "").strip() or not _number(q):
                    return page(f"Give each batch of {l['product_code']} coming back a number and a quantity.")
                batches.append({"number": n.strip()[:50], "qty": _number(q), "expiry": _date(e)})
            if not batches:
                return page(f"{l['product_code']} is tracked by batch: enter the batch it comes back as.")
        lines.append({"product_code": l["product_code"], "qty": qty, "price": price,
                      "reason": (form.get(f"reason-{g}") or "Return").strip(), "return": back_in, "batches": batches})
    credit = {"customer": so["customer_code"], "warehouse": so["warehouse"], "date": _date(form.get("date"), r.today),
              "reference": (form.get("reference") or "").strip()[:500], "comments": (form.get("comments") or "").strip()[:1024],
              "lines": lines}
    try:
        made = create_credit(_ext()["client"], credit, complete=bool(form.get("complete")))
    except (OrderError, UnleashedError) as e:
        return page(_failed(e))
    _saved("CreditNotes", made)
    status = made.get("CreditStatus") or made.get("Status")
    _done("Completed credit note" if status == "Completed" else "Made credit note (parked)", made.get("CreditNumber"))
    if status == "Completed":
        _sync_soon()
    return redirect(url_for("dash.credit_detail", number=made.get("CreditNumber")))


@bp.post("/sales/credits/<number>/complete")
def credit_complete(number):
    found = reports().credit_note(number) or abort(404)
    back = url_for("dash.credit_detail", number=number)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        done = complete_credit(_ext()["client"], found["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    _saved("CreditNotes", done)
    _done("Completed", number)
    _sync_soon()
    return redirect(back)


@bp.post("/sales/credits/<number>/delete")
def credit_delete(number):
    found = reports().credit_note(number) or abort(404)
    back = url_for("dash.credit_detail", number=number)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        delete_credit(_ext()["client"], found["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    gone = _live("CreditNotes", found["guid"])
    if gone:
        _saved("CreditNotes", gone)
    else:
        _ext()["store"].delete_document("CreditNotes", found["guid"])
        _ext()["store_client"].clear_cache()
    _done("Deleted", number)
    return redirect(url_for("dash.credit_notes"))


# ---------------------------------------------------------------- supplier returns

@bp.get("/purchases/returns")
def supplier_returns():
    start, end = _range()
    rows = sorted(reports().supplier_returns(start, end), key=lambda x: (x["date"] or dt.date.min, x["number"] or ""),
                  reverse=True)
    return render_template("dash/supplier_returns.html", rows=[x for x in rows if x["status"] != "Deleted"],
                           start=start, end=end)


@bp.get("/purchases/returns/<number>")
def supplier_return_detail(number):
    found = reports().supplier_return(number)
    if found is None:
        abort(404)
    return render_template("dash/supplier_return_detail.html", sr=found, write_problem=_write_problem())


def _return_reasons():
    client = _ext()["client"]
    try:
        reasons = [x.get("Reason") for x in client.get_all("SupplierReturnReasons") if x.get("Reason")] if client else []
    except UnleashedError:
        reasons = []
    return reasons or ["Return"]


@bp.route("/purchases/<number>/return", methods=["GET", "POST"])
def purchase_order_return(number):
    """Send (some of) a completed purchase order's stock back to its supplier."""
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    if po["status"] != "Complete":
        flash(f"{number} is {po['status'].lower()}: only a completed purchase order can be returned.", "warn")
        return redirect(back)
    r = reports()
    client = _ext()["client"]
    form = request.form
    keys, seen = [], {}
    for l in po["lines"]:  # (product, nth on the order), as Unleashed's return lines follow the order's
        n = seen.get(l["product_code"], 0)
        seen[l["product_code"]] = n + 1
        keys.append((l["product_code"], n))
    tracked = {l["product_code"] for l in po["lines"] if r.batch_tracked(l["product_code"])}
    available = {}
    for code in tracked:
        try:
            batches = client.get_all("BatchNumbers", {"ProductCode": code, "WarehouseCode": po["warehouse"]}) if client else []
        except UnleashedError:
            batches = []
        available[code] = [{"number": b.get("Number") or b.get("BatchNumber"), "qty": float(b.get("Quantity") or 0),
                            "expiry": parse_date(b.get("ExpiryDate"))} for b in batches if float(b.get("Quantity") or 0) > 0]

    def field(name, key):
        return f"{name}-{key[0]}-{key[1]}"

    def page(error=None):
        rows = []
        for l, key in zip(po["lines"], keys):
            rows.append(dict(l, key=f"{key[0]}-{key[1]}", tracked=l["product_code"] in tracked,
                             available=available.get(l["product_code"], []),
                             typed_qty=form.get(field("qty", key), "") if form else "",
                             typed_price=form.get(field("price", key)) if form else f"{l['unit_price']:g}",
                             typed_reason=form.get(field("reason", key)) if form else "",
                             batches=[{"number": n, "qty": q} for n, q in zip_longest(
                                 form.getlist(field("batch", key)), form.getlist(field("batch-qty", key)), fillvalue="")]
                             if form else [{"number": "", "qty": ""}]))
        html = render_template("dash/purchase_return.html", po=po, rows=rows, error=error, reasons=_return_reasons(),
                               values={"date": form.get("date") if form else r.today.isoformat(),
                                       "reference": form.get("reference", "") if form else "",
                                       "comments": form.get("comments", "") if form else "",
                                       "complete": form.get("complete") if form else "1"},
                               write_problem=_write_problem())
        return (html, 400) if error else html

    if request.method == "GET":
        return page()
    blocked = _blocked(back)
    if blocked:
        return blocked
    lines = {}
    for l, key in zip(po["lines"], keys):
        qty = _number(form.get(field("qty", key))) or 0
        received = l["received_qty"] if l["received_qty"] is not None else l["qty"]
        if qty < 0 or qty > received + 1e-9:
            return page(f"{l['product_code']}: send back between 0 and the {received:g} received.")
        if not qty:
            continue
        batches = []
        if l["product_code"] in tracked:
            for n, q in zip_longest(form.getlist(field("batch", key)), form.getlist(field("batch-qty", key)), fillvalue=""):
                if not (n or "").strip() and not (q or "").strip():
                    continue
                if not (n or "").strip() or not _number(q):
                    return page(f"Give each batch of {l['product_code']} going back a number and a quantity.")
                batches.append({"number": n.strip()[:50], "qty": _number(q)})
            if not batches:
                return page(f"{l['product_code']} is tracked by batch: choose the batch going back.")
        lines[key] = {"qty": qty, "price": _number(form.get(field("price", key))) or 0,
                      "reason": form.get(field("reason", key)) or "Return", "batches": batches}
    ret = {"date": _date(form.get("date"), r.today), "reference": (form.get("reference") or "").strip()[:500],
           "comments": (form.get("comments") or "").strip()[:1024], "lines": lines}
    try:
        made = create_supplier_return(client, number, ret, complete=bool(form.get("complete")))
    except (OrderError, UnleashedError) as e:
        return page(_failed(e))
    _saved("SupplierReturns", made)
    done = (made.get("Status") or "").lower() in ("completed", "complete")
    _done("Completed supplier return" if done else "Made supplier return (parked)", made.get("SupplierReturnNumber"))
    if done:
        _sync_soon()
    return redirect(url_for("dash.supplier_return_detail", number=made.get("SupplierReturnNumber")))


@bp.post("/purchases/returns/<number>/complete")
def supplier_return_complete(number):
    found = reports().supplier_return(number) or abort(404)
    back = url_for("dash.supplier_return_detail", number=number)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        done = complete_supplier_return(_ext()["client"], found["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    _saved("SupplierReturns", done)
    _done("Completed", number)
    _sync_soon()
    return redirect(back)


@bp.post("/purchases/returns/<number>/delete")
def supplier_return_delete(number):
    found = reports().supplier_return(number) or abort(404)
    back = url_for("dash.supplier_return_detail", number=number)
    blocked = _blocked(back)
    if blocked:
        return blocked
    try:
        delete_supplier_return(_ext()["client"], found["guid"])
    except (OrderError, UnleashedError) as e:
        flash(_failed(e), "warn")
        return redirect(back)
    gone = _live("SupplierReturns", found["guid"])
    if gone:
        _saved("SupplierReturns", gone)
    else:
        _ext()["store"].delete_document("SupplierReturns", found["guid"])
        _ext()["store_client"].clear_cache()
    _done("Deleted", number)
    return redirect(url_for("dash.supplier_returns"))


# ---------------------------------------------------------------- emailing vendors

def _vendor_emails():
    return current_app.extensions["vendor_emails"]


def _deliver_to(code):
    """A warehouse's name and address, as one line for the vendor."""
    w = next((w for w in reports().warehouses() if w.get("WarehouseCode") == code), None)
    if not w:
        return code or ""
    parts = [w.get("WarehouseName") or code, " ".join(x for x in (w.get("StreetNo"), w.get("AddressLine1")) if x),
             w.get("AddressLine2"), w.get("City"), w.get("Region"), w.get("PostCode")]
    return ", ".join(p.strip() for p in parts if p and p.strip())


@bp.route("/purchases/vendors", methods=["GET", "POST"])
def purchase_vendors():
    """Which vendors take purchase orders by email, and where."""
    emails = _vendor_emails()
    suppliers = sorted((s for s in reports().suppliers() if not s.get("Obsolete")),
                       key=lambda s: (s.get("SupplierName") or "").lower())
    saved, error, typed = emails.settings(), None, {}
    if request.method == "POST":
        problems = []
        for s in suppliers:
            code = s.get("SupplierCode")
            send = bool(request.form.get(f"send-{code}"))
            email, cc = request.form.get(f"email-{code}", ""), request.form.get(f"cc-{code}", "")
            typed[code] = {"send": send, "email": email, "cc": cc}
            was = saved.get(code)
            if was is None and not send and not email.strip() and not cc.strip():
                continue  # never set, still not
            try:
                emails.save(code, send, email, cc, _person())
            except MailError as e:
                problems.append(f"{s.get('SupplierName') or code}: {e}")
        if not problems:
            flash("Saved which vendors take orders by email.", "ok")
            return redirect(url_for("dash.purchase_vendors"))
        error, saved = " ".join(problems), emails.settings()
    rows = []
    for s in suppliers:
        code = s.get("SupplierCode")
        mine, theirs = typed.get(code) or saved.get(code), (s.get("Email") or "").strip()
        rows.append({"code": code, "name": s.get("SupplierName") or code, "unleashed_email": theirs,
                     "send": bool(mine and mine["send"]),
                     # Until a vendor is set here, the box starts with their email in Unleashed.
                     "email": (mine["email"] or "") if mine else theirs, "cc": (mine["cc"] or "") if mine else ""})
    html = render_template("dash/purchase_vendors.html", rows=rows, error=error, can_send=can_send(current_app.config),
                           sender=(mail_settings(current_app.config) or {}).get("sender"))
    return (html, 400) if error else html


@bp.route("/purchases/<number>/email", methods=["GET", "POST"])
def purchase_order_email(number):
    """Email a placed purchase order to its vendor: the message is shown first, then sent."""
    po = _open_purchase_order(number)
    back = url_for("dash.purchase_detail", number=number)
    emails, config = _vendor_emails(), current_app.config
    setting = emails.setting(po["supplier_code"])
    if not (setting and setting["send"] and setting["email"]):
        flash(f"{po['supplier_name']} isn't set to take orders by email. Tick them under Purchases → Vendor emails.", "warn")
        return redirect(url_for("dash.purchase_vendors"))
    if po["status"] not in SENDABLE:
        flash(f"{number} is {po['status'].lower()}: place it before emailing it to the vendor."
              if po["status"] == "Parked" else f"{number} is {po['status'].lower()}, so it isn't emailed.", "warn")
        return redirect(back)
    to, cc = addresses(setting["email"]), addresses(setting["cc"])
    company = config.get("COMPANY_NAME") or "Yeems Coffee"
    user = current_user()
    # Who it's from: MAIL_FROM (shown even before the mail server is set, so the page reads true).
    sender = (config.get("MAIL_FROM") or "").strip() or f"{company} <not-set-up@example.invalid>"
    note = (request.form.get("note") or "").strip()[:2000]
    codes = {p.get("ProductCode"): (p.get("Supplier") or {}).get("SupplierProductCode")
             for p in reports().products()
             if (p.get("Supplier") or {}).get("SupplierCode") == po["supplier_code"]
             and (p.get("Supplier") or {}).get("SupplierProductCode")}
    message = build_message(po, to, cc, sender, company, note=note,
                            reply_to=named(user["name"], user["email"]) if user and user.get("email") else None,
                            deliver_to=_deliver_to(po.get("warehouse")), supplier_codes=codes)
    if request.method == "POST":
        if not can_send(config):
            flash("Email isn't set up yet, so nothing was sent. An admin sets SMTP_HOST and MAIL_FROM.", "warn")
            return redirect(url_for("dash.purchase_order_email", number=number))
        try:
            send(config, message)
        except MailError as e:
            flash(str(e), "warn")
            return redirect(url_for("dash.purchase_order_email", number=number))
        emails.record(po, to, cc, message["Subject"], _person())
        flash(f"Emailed {number} to {', '.join(to)}{' (copy to ' + ', '.join(cc) + ')' if cc else ''}.", "ok")
        return redirect(back)
    return render_template("dash/purchase_email.html", po=po, to=to, cc=cc, subject=message["Subject"],
                           sender=message["From"], reply_to=message["Reply-To"],
                           text=message.get_body(preferencelist=("plain",)).get_content(),
                           sent=emails.sent(number), can_send=can_send(config), note=note)
