"""Unleashed dashboard: every number on these pages comes through the Unleashed API."""
import csv
import datetime as dt
import io
import sqlite3

from flask import (Blueprint, Response, abort, current_app, redirect, render_template,
                   request, url_for)

from unleashed import charts
from unleashed.client import HttpTransport, UnleashedClient, UnleashedError
from unleashed.reports import (OPEN_PO_STATUSES, PO_STATUSES, SO_STATUSES, add_months,
                               month_end, month_start)

bp = Blueprint("dash", __name__)


def _ext():
    return current_app.extensions["unleashed"]


def reports():
    return _ext()["reports"]


# ---------------------------------------------------------------- filters

@bp.app_template_filter("money")
def money(v, cents=True):
    if v is None:
        return "—"
    s = f"${abs(v):,.2f}" if cents else f"${abs(v):,.0f}"
    return f"-{s}" if v < -0.005 else s


@bp.app_template_filter("qty")
def qty(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


@bp.app_template_filter("pct")
def pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


@bp.app_template_filter("d")
def fmt_date(v):
    return f"{v:%b} {v.day}, {v:%Y}" if v else "—"


@bp.app_template_filter("month_name")
def month_name(v):
    return v.strftime("%B %Y")


@bp.app_context_processor
def _ctx():
    return {"unleashed_mode": _ext()["mode"], "money_short": charts.money_short}


@bp.errorhandler(UnleashedError)
def _api_error(e):
    return render_template("dash/error.html", error=str(e), status=e.status_code), 502


# ---------------------------------------------------------------- helpers

def _date_arg(name, default):
    v = request.args.get(name)
    try:
        return dt.date.fromisoformat(v) if v else default
    except ValueError:
        return default


def _month_arg(default):
    v = request.args.get("month")
    try:
        return dt.date.fromisoformat(v + "-01") if v else default
    except ValueError:
        return default


def _range_args():
    today = reports().today
    return _date_arg("start", month_start(today)), _date_arg("end", today)


def _wants_csv():
    return request.args.get("format") == "csv"


def _round(v, places=2):
    return "" if v is None else round(v, places)


def _csv(filename, columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in columns])
    for r in rows:
        w.writerow([(c[1](r) if callable(c[1]) else r.get(c[1])) for c in columns])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})


def _csv_url():
    args = request.args.to_dict()
    args["format"] = "csv"
    return url_for(request.endpoint, **request.view_args, **args)


def _cogs_chart(daily, month):
    pts = [(f'{r["date"]:%b} {r["date"].day}', r["cogs_to_date"],
            f'{r["date"]:%a %b} {r["date"].day} · COGS to date {money(r["cogs_to_date"])} '
            f'(day {money(r["cogs_day"])})') for r in daily]
    return charts.line_chart(pts, f"Cumulative COGS, {month_name(month)}")


# ---------------------------------------------------------------- pages

@bp.get("/")
def overview():
    r = reports()
    today = r.today
    cogs = r.cogs(today, daily=True)
    last = r.cogs(add_months(month_start(today), -1),
                  through=add_months(month_start(today), -1).replace(
                      day=min(today.day, month_end(add_months(month_start(today), -1)).day)))
    sales = r.sales_enquiry(month_start(today), today, status="Completed")
    open_pos = r.purchase_orders(today - dt.timedelta(days=60), today + dt.timedelta(days=30),
                                 status=",".join(OPEN_PO_STATUSES))
    recent = r.transactions(today - dt.timedelta(days=2), today)[-12:][::-1]
    return render_template(
        "dash/overview.html", cogs=cogs, last=last, sales=sales, open_pos=open_pos,
        open_po_value=sum(o["total"] for o in open_pos), recent=recent,
        chart=_cogs_chart(cogs["daily"], cogs["month"]), today=today,
        gross_margin=(sales["revenue"] - cogs["cogs"]) if sales["revenue"] else None,
    )


@bp.get("/cogs")
def cogs_page():
    r = reports()
    month = _month_arg(month_start(r.today))
    if month > r.today:
        month = month_start(r.today)
    c = r.cogs(month, daily=True)
    if _wants_csv():
        return _csv(f"cogs-{month:%Y-%m}", [
            ("Date", lambda x: x["date"].isoformat()),
            ("Purchases to date", lambda x: round(x["purchases_to_date"], 2)),
            ("Inventory value", lambda x: round(x["inventory"], 2)),
            ("COGS for day", lambda x: round(x["cogs_day"], 2)),
            ("COGS to date", lambda x: round(x["cogs_to_date"], 2)),
        ], c.get("daily", []))
    history = r.cogs_history(12)
    bars = [(h["month"].strftime("%b"), h["cogs"],
             f'{month_name(h["month"])} · COGS {money(h["cogs"])}'
             + (" (month to date)" if h["is_current"] else "")) for h in history]
    return render_template(
        "dash/cogs.html", c=c, recon=r.cogs_reconciliation(month), history=history,
        chart=_cogs_chart(c.get("daily", []), month),
        history_chart=charts.bar_chart(bars, "COGS by month, last 12 months"),
        month=month, csv_url=_csv_url(),
    )


@bp.get("/stock-on-hand")
def stock_on_hand():
    r = reports()
    as_at = _date_arg("as_at", None)
    group = request.args.get("group") or None
    soh = r.stock_on_hand(as_at=as_at, group=group)
    if _wants_csv():
        return _csv("stock-on-hand", [
            ("Product Code", "product_code"), ("Description", "description"),
            ("Group", "group"), ("Warehouse", "warehouse"), ("Qty On Hand", "qty"),
            ("Allocated", "allocated"), ("Available", "available"), ("On Purchase", "on_purchase"),
            ("Avg Cost", "avg_cost"), ("Total Value", "value"),
            ("Days Since Last Sale", "days_since_sale"),
        ], soh["rows"])
    return render_template("dash/stock_on_hand.html", soh=soh, groups=r.product_groups(),
                           group=group, as_at=as_at, csv_url=_csv_url())


@bp.get("/stock-valuation")
def stock_valuation():
    r = reports()
    as_at = _date_arg("as_at", None)
    val = r.stock_valuation(as_at=as_at)
    if _wants_csv():
        return _csv("stock-valuation", [
            ("Product Group", "group"), ("Products", "products"), ("Qty", "qty"),
            ("Value", lambda x: round(x["value"], 2)), ("Share", lambda x: round(x["share"], 4)),
        ], val["rows"])
    return render_template("dash/valuation.html", val=val, as_at=as_at, csv_url=_csv_url())


@bp.get("/stock-movement")
def stock_movement():
    r = reports()
    products = r.products()
    product = request.args.get("product") or (products[0]["ProductCode"] if products else None)
    start, end = _range_args()
    mv = r.stock_movement(product, start, end) if product else None
    if _wants_csv() and mv:
        return _csv(f"stock-movement-{product}", [
            ("Date", lambda x: x["date"].isoformat()), ("Type", "type"), ("Number", "number"),
            ("Party", "party"), ("Qty", "qty"), ("Value", lambda x: _round(x["value"], 4)),
            ("Balance Qty", "balance_qty"), ("Balance Value", lambda x: round(x["balance_value"], 4)),
        ], mv["rows"])
    return render_template("dash/movement.html", mv=mv, products=products, product=product,
                           start=start, end=end, csv_url=_csv_url())


@bp.get("/purchases")
def purchases():
    r = reports()
    start, end = _range_args()
    status = request.args.get("status") or None
    supplier = request.args.get("supplier") or None
    pe = r.purchase_enquiry(start, end, status, supplier)
    if _wants_csv():
        return _csv("purchase-enquiry", [
            ("Order Number", "number"), ("Order Date", lambda x: x["order_date"].isoformat()),
            ("Received Date", lambda x: x["received_date"].isoformat() if x["received_date"] else ""),
            ("Supplier", "supplier_name"), ("Status", "status"),
            ("Goods", lambda x: round(x["goods"], 2)), ("Shipping", lambda x: round(x["shipping"], 2)),
            ("Total", lambda x: round(x["total"], 2)),
        ], pe["orders"])
    return render_template("dash/purchases.html", pe=pe, start=start, end=end, status=status,
                           supplier=supplier, statuses=PO_STATUSES, suppliers=r.suppliers(),
                           csv_url=_csv_url())


@bp.get("/purchases/<number>")
def purchase_detail(number):
    po = reports().purchase_order(number)
    if po is None:
        abort(404)
    return render_template("dash/purchase_detail.html", po=po)


@bp.get("/sales")
def sales():
    r = reports()
    start, end = _range_args()
    status = request.args.get("status") or None
    customer = request.args.get("customer") or None
    se = r.sales_enquiry(start, end, status, customer)
    if _wants_csv():
        return _csv("sales-enquiry", [
            ("Order Number", "number"), ("Order Date", lambda x: x["order_date"].isoformat()),
            ("Customer", "customer_name"), ("Status", "status"),
            ("Sub Total", lambda x: round(x["subtotal"], 2)), ("Cost", lambda x: round(x["cost"], 2)),
            ("Margin", lambda x: round(x["margin"], 2)),
        ], se["orders"])
    return render_template("dash/sales.html", se=se, start=start, end=end, status=status,
                           customer=customer, statuses=SO_STATUSES, customers=r.customers(),
                           csv_url=_csv_url())


@bp.get("/sales/<number>")
def sales_detail(number):
    so = reports().sales_order(number)
    if so is None:
        abort(404)
    return render_template("dash/sales_detail.html", so=so)


@bp.get("/adjustments")
def adjustments():
    start, end = _range_args()
    adj = reports().stock_adjustments(start, end)
    if _wants_csv():
        return _csv("stock-adjustments", [
            ("Number", "number"), ("Date", lambda x: x["date"].isoformat()), ("Reason", "reason"),
            ("Product Code", "product_code"), ("Qty Change", "qty_change"),
            ("Value Change", lambda x: _round(x["value_change"], 4)), ("Comments", "comments"),
        ], adj["rows"])
    return render_template("dash/adjustments.html", adj=adj, start=start, end=end,
                           csv_url=_csv_url())


@bp.get("/transactions")
def transactions():
    r = reports()
    start, end = _range_args()
    doc_type = request.args.get("type") or None
    product = request.args.get("product") or None
    rows = r.transactions(start, end, product=product, doc_type=doc_type)[::-1]
    if _wants_csv():
        return _csv("transactions", [
            ("Date", lambda x: x["date"].isoformat()), ("Type", "type"), ("Number", "number"),
            ("Party", "party"), ("Product Code", "product_code"), ("Qty", "qty"),
            ("Value", lambda x: _round(x["value"], 4)),
        ], rows)
    return render_template("dash/transactions.html", rows=rows, start=start, end=end,
                           doc_type=doc_type, product=product, products=r.products(),
                           csv_url=_csv_url())


@bp.get("/api-log")
def api_log():
    conn = sqlite3.connect(current_app.config["DATABASE_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM unleashed_api_log ORDER BY id DESC LIMIT 300").fetchall()
    finally:
        conn.close()
    return render_template("dash/api_log.html", rows=rows)


@bp.route("/connection", methods=["GET", "POST"])
def connection():
    from unleashed.diagnostics import run_checks
    from unleashed.reports import Reports

    ext = _ext()
    result, checks, fields = None, None, None
    if request.method == "POST":
        client = ext["live_client"]
        client.clear_cache()
        if request.form.get("action") == "check":
            checks, fields = run_checks(Reports(client, today_fn=ext["reports"].today_fn,
                                                max_workers=4))
        else:
            try:
                whs = client.get_all("Warehouses")
                result = {"ok": True, "message": f"Connected to Unleashed. {len(whs)} warehouse(s): "
                          + ", ".join(w.get("WarehouseName", "?") for w in whs)}
            except UnleashedError as e:
                result = {"ok": False, "message": str(e)}
    return render_template("dash/connection.html", result=result, checks=checks,
                           fields=fields, ext=ext)


@bp.post("/refresh")
def refresh():
    _ext()["client"].clear_cache()
    return redirect(request.referrer or url_for("dash.overview"))


# ---------------------------------------------------------------- wiring

def init_app(app):
    """Build the API client (mock or live) and register the dashboard."""
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    from unleashed.client import WsgiTransport
    from unleashed.mock_server import MOCK_API_ID, MOCK_API_KEY, create_mock_app
    from unleashed.reports import Reports

    cfg = app.config
    db_path = cfg["DATABASE_PATH"]

    def log_call(entry):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO unleashed_api_log (timestamp, mode, method, endpoint, query,"
                " status_code, duration_ms, item_count, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (dt.datetime.now().isoformat(sep=" ", timespec="seconds"), entry["mode"],
                 entry["method"], entry["endpoint"], entry["query"], entry["status_code"],
                 entry["duration_ms"], entry["item_count"], entry["error"]))
            conn.commit()
        finally:
            conn.close()

    live_client = UnleashedClient(cfg["UNLEASHED_API_ID"], cfg["UNLEASHED_API_KEY"],
                                  cfg.get("UNLEASHED_TRANSPORT") or HttpTransport(cfg["UNLEASHED_BASE_URL"]),
                                  log_fn=log_call, cache_seconds=cfg["UNLEASHED_CACHE_SECONDS"],
                                  mode="live")
    mode = cfg["UNLEASHED_MODE"]
    if mode == "live":
        client = live_client
    else:
        mode = "mock"
        mock_app = create_mock_app(MOCK_API_ID, MOCK_API_KEY)
        client = UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(mock_app),
                                 log_fn=log_call, cache_seconds=cfg["UNLEASHED_CACHE_SECONDS"],
                                 mode="mock")
        # Also expose the mock over HTTP at /mock-unleashed for curl/Postman.
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/mock-unleashed": mock_app})

    app.extensions["unleashed"] = {
        "mode": mode, "client": client, "live_client": live_client,
        "reports": Reports(client, max_workers=4 if mode == "live" else 1),
        "base_url": cfg["UNLEASHED_BASE_URL"],
        "has_credentials": bool(cfg["UNLEASHED_API_ID"] and cfg["UNLEASHED_API_KEY"]),
    }
    app.register_blueprint(bp)
