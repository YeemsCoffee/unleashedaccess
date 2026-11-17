"""Store ordering pages: stores ask the warehouse for stock (store_orders.py).

Store staff (the "Store ordering" access, or anyone in Warehouse) count what they have against
their store's par levels and send a request. The warehouse turns it into a sales order to the
store in Unleashed, or cancels it. Par levels are set by the warehouse.
"""
import datetime as dt

from flask import abort, current_app, flash, redirect, render_template, request, url_for

import dashboard
from dashboard import _ext, _person, bp, reports, user_areas
from order_pages import _blocked, _choices, _done, _failed, _saved
from store_orders import StoreOrderError
from unleashed.client import UnleashedError
from unleashed.order_writes import OrderError, create_sales_order


def _orders():
    return current_app.extensions["store_orders"]


def _is_warehouse():
    return "warehouse" in user_areas()


def _stores():
    """The stores: Unleashed customers ticked as stores under Settings -> COGS, [(code, name)]."""
    r = reports()
    names = {c.get("CustomerCode"): c.get("CustomerName") or c.get("CustomerCode") for c in r.customers()}
    return [(code, names.get(code, code)) for code in r.cogs_settings()["stores"]]


def _store_or_404(code):
    stores = dict(_stores())
    if code not in stores:
        abort(404)
    return code, stores[code]


def _products():
    """{product code: {"code", "name", "unit", "price"}} that can be sent to a store."""
    return {o["code"]: o for o in _choices("sales")["product_options"]}


@bp.get("/store-orders")
def store_orders():
    stores = _stores()
    status = request.args.get("status") or None
    store = request.args.get("store") or None
    found = _orders().requests(store=store, status=status)
    names = dict(stores)
    return render_template("dash/store_orders.html", stores=stores, names=names, requests=found, status=status,
                           store=store, pars=_orders().stores_with_pars(), warehouse=_is_warehouse(),
                           waiting=sum(1 for r in _orders().requests(status="submitted")))


@bp.route("/store-orders/new", methods=["GET", "POST"])
def store_order_new():
    code, name = _store_or_404(request.values.get("store") or "")
    products = _products()
    pars = [p for p in _orders().pars(code) if p["product_code"] in products]
    error = None
    if request.method == "POST":
        lines = [{"product_code": c, "on_hand": request.form.get(f"on_hand-{c}"), "qty": request.form.get(f"qty-{c}")}
                 for c in request.form.getlist("product")]
        needed = request.form.get("needed_by") or None
        try:
            if needed:
                dt.date.fromisoformat(needed)
            request_id = _orders().submit(code, lines, request.form.get("notes"), needed, _person(), set(products))
        except ValueError as e:
            error = str(e) if isinstance(e, StoreOrderError) else "Enter the date it's needed by as a date."
        else:
            flash(f"Sent {name}'s request to the warehouse.", "ok")
            return redirect(url_for("dash.store_order", request_id=request_id))
    html = render_template("dash/store_order_new.html", code=code, name=name, pars=pars, products=products,
                           form=request.form, error=error, warehouse=_is_warehouse(),
                           today=reports().today)
    return (html, 400) if error else html


@bp.get("/store-orders/<int:request_id>")
def store_order(request_id):
    found = _orders().request(request_id)
    if found is None:
        abort(404)
    stores = dict(_stores())
    products = _products()
    warehouses = reports().warehouses()
    default = next((w.get("WarehouseCode") for w in warehouses if w.get("IsDefault")),
                   warehouses[0].get("WarehouseCode") if warehouses else None)
    return render_template("dash/store_order.html", r=found, store_name=stores.get(found["store_code"], found["store_code"]),
                           products=products, warehouse=_is_warehouse(), warehouses=warehouses, default_warehouse=default,
                           write_problem=dashboard._write_problem())


@bp.post("/store-orders/<int:request_id>/order")
def store_order_make(request_id):
    """The warehouse makes the sales order to the store, with the quantities it's sending."""
    found = _orders().request(request_id)
    if found is None:
        abort(404)
    back = url_for("dash.store_order", request_id=request_id)
    if not _is_warehouse():
        flash("Only someone in Warehouse makes the sales order.", "warn")
        return redirect(back)
    if found["status"] != "submitted":
        flash(f"This request is {found['status_label'].lower()}.", "warn")
        return redirect(back)
    blocked = _blocked(back)
    if blocked:
        return blocked
    products = _products()
    lines = []
    try:
        for l in found["lines"]:
            raw = (request.form.get(f"qty-{l['id']}") or "").strip()
            qty = float(raw.replace(",", "")) if raw else 0.0
            if qty < 0:
                raise ValueError
            if qty:
                price = (products.get(l["product_code"]) or {}).get("price")
                lines.append({"product_code": l["product_code"], "qty": qty, "unit_price": float(price or 0)})
    except ValueError:
        flash("Each quantity to send must be a number, 0 or more.", "warn")
        return redirect(back)
    if not lines:
        flash("Nothing is being sent: put a quantity against at least one product, or cancel the request.", "warn")
        return redirect(back)
    order = {"customer": found["store_code"], "warehouse": request.form.get("warehouse") or None,
             "order_date": reports().today, "required_date": dt.date.fromisoformat(found["needed_by"]) if found["needed_by"] else None,
             "reference": f"Store request {request_id}", "comments": found["notes"], "lines": lines}
    try:
        made = create_sales_order(_ext()["client"], order, _choices("sales")["products_raw"])
    except (OrderError, UnleashedError) as e:
        flash(f"The sales order wasn't made: {_failed(e)}", "warn")
        return redirect(back)
    _saved("SalesOrders", made)
    _orders().ordered(request_id, made.get("OrderNumber"), _person())
    _done("Made sales order", made.get("OrderNumber"))
    return redirect(url_for("dash.sales_detail", number=made.get("OrderNumber")))


@bp.post("/store-orders/<int:request_id>/cancel")
def store_order_cancel(request_id):
    found = _orders().request(request_id)
    if found is None:
        abort(404)
    if _orders().cancel(request_id, request.form.get("reason"), _person()):
        flash("Cancelled the request.", "ok")
    else:
        flash(f"This request is {found['status_label'].lower()}, so it can't be cancelled.", "warn")
    return redirect(url_for("dash.store_orders"))


@bp.route("/store-orders/pars", methods=["GET", "POST"])
def store_pars():
    """The warehouse sets each store's par levels."""
    code, name = _store_or_404(request.values.get("store") or "")
    if not _is_warehouse():
        flash("Par levels are set by someone in Warehouse.", "warn")
        return redirect(url_for("dash.store_orders"))
    products = _products()
    error = None
    if request.method == "POST":
        rows = list(zip(request.form.getlist("product"), request.form.getlist("par")))
        codes = {c.lower(): c for c in products}
        rows = [(codes.get((c or "").strip().lower(), (c or "").strip()), p) for c, p in rows]
        try:
            n = _orders().save_pars(code, rows, set(products), _person())
        except StoreOrderError as e:
            error = str(e)
        else:
            flash(f"Saved {name}'s par levels ({n} product{'s' if n != 1 else ''}).", "ok")
            return redirect(url_for("dash.store_orders"))
        pars = [{"product_code": c, "par": p} for c, p in rows if (c or "").strip() or (p or "").strip()]
    else:
        pars = _orders().pars(code)
    pars = list(pars) + [{"product_code": "", "par": ""}] * max(3, 8 - len(pars))
    html = render_template("dash/store_pars.html", code=code, name=name, pars=pars, products=products, error=error,
                           names={c: p["name"] for c, p in products.items()})
    return (html, 400) if error else html
