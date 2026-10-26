"""Unleashed dashboard. Pages read a local copy of Unleashed data (unleashed/store.py),
kept current by a background sync from the Unleashed API (unleashed/sync.py)."""
import csv
import datetime as dt
import hmac
import io
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, g, redirect, render_template,
                   request, session, url_for)

from accounts import ROLES, AccountError, check_new_password, temporary_password
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


def _parse_utc(timestamp):
    return dt.datetime.fromisoformat(timestamp).replace(tzinfo=dt.timezone.utc)


def ago(timestamp):
    """'4 min ago' for a UTC timestamp from the store."""
    if not timestamp:
        return "never"
    secs = (dt.datetime.now(dt.timezone.utc) - _parse_utc(timestamp)).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400 * 2:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


@bp.app_template_filter("local")
def local_time(timestamp):
    """A UTC store timestamp shown in the business's time zone."""
    if not timestamp:
        return "—"
    tz = _ext()["display_tz"]
    t = _parse_utc(timestamp)
    return (t.astimezone(tz) if tz else t.astimezone()).strftime("%Y-%m-%d %H:%M")


def _sync_status():
    """(last run, last successful run), read once per request."""
    if "sync_status" not in g:
        store = _ext()["store"]
        last = store.last_run()
        ok = last if last and last["status"] == "ok" else store.last_success()
        g.sync_status = (last, ok)
    return g.sync_status


def _sync_running():
    ext = _ext()
    return bool(ext["syncer"] and ext["syncer"].running) or ext["store"].sync_in_progress()


@bp.app_context_processor
def _ctx():
    ext = _ext()
    last, ok = _sync_status()
    mode = current_app.config["AUTH_MODE"]
    user = current_user()
    return {"unleashed_mode": ext["mode"], "money_short": charts.money_short,
            "sync_last": last, "sync_ok": ok, "ago": ago,
            "can_sync": ext["syncer"] is not None, "source_error": ext["source_error"],
            "login_enabled": mode != "open", "auth_mode": mode,
            "signed_in": user is not None if mode == "accounts" else bool(session.get("signed_in")),
            "current_user": user, "is_admin": is_admin()}


# Pages that work before the first sync has finished.
_OPEN_ENDPOINTS = {"dash.connection", "dash.api_log", "dash.sync_now", "dash.login",
                   "dash.logout", "dash.healthz", "dash.account", "dash.team",
                   "dash.team_add", "dash.team_update"}
# Pages that work without signing in.
_PUBLIC_ENDPOINTS = {"dash.login", "dash.logout", "dash.healthz"}


def current_user():
    """The signed-in team member (accounts mode), checked against the database once per request.

    A deactivated account, or a password changed or reset since this sign-in,
    ends the session.
    """
    if current_app.config["AUTH_MODE"] != "accounts":
        return None
    if "current_user" not in g:
        user = None
        user_id = session.get("user_id")
        if user_id is not None:
            user = current_app.extensions["users"].get(user_id)
            if not user or not user["active"] or user["session_version"] != session.get("v"):
                user = None
        g.current_user = user
    return g.current_user


def is_admin():
    """Admins manage the team and the Unleashed connection. Without accounts, everyone can."""
    if current_app.config["AUTH_MODE"] != "accounts":
        return True
    user = current_user()
    return bool(user and user["role"] == "admin")


def admin_only(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return render_template("dash/error.html", error="Only admins can do that. Ask an "
                                   "admin on your team if you need it.", status=403), 403
        return view(*args, **kwargs)
    return wrapper


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") \
        else url_for("dash.overview")


@bp.before_request
def _require_login():
    mode = current_app.config["AUTH_MODE"]
    if mode == "open" or request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if mode == "password":
        if not session.get("signed_in"):
            return redirect(url_for("dash.login", next=request.full_path))
        return None
    user = current_user()
    if user is None:
        session.clear()
        return redirect(url_for("dash.login", next=request.full_path))
    if user["must_change_password"] and request.endpoint != "dash.account":
        return redirect(url_for("dash.account"))
    return None


@bp.before_request
def _require_synced_data():
    if request.endpoint in _OPEN_ENDPOINTS:
        return None
    last, ok = _sync_status()
    if ok is None:
        return render_template("dash/first_sync.html", run=last, running=_sync_running())
    return None


@bp.get("/healthz")
def healthz():
    """For load balancer / container health checks: the web process is up.

    Deliberately doesn't touch Unleashed or the database, so an outage there
    doesn't make AWS replace healthy servers in a loop.
    """
    return {"status": "ok"}


@bp.route("/login", methods=["GET", "POST"])
def login():
    mode = current_app.config["AUTH_MODE"]
    if mode == "open":
        return redirect(url_for("dash.overview"))
    error, email = None, ""
    if request.method == "POST":
        if mode == "accounts":
            email = request.form.get("email", "")
            user, error = current_app.extensions["users"].authenticate(
                email, request.form.get("password", ""))
            if user:
                session.clear()
                session.permanent = True
                session["user_id"], session["v"] = user["id"], user["session_version"]
                if user["must_change_password"]:
                    return redirect(url_for("dash.account"))
                return redirect(_safe_next(request.args.get("next")))
        else:
            given = request.form.get("password", "").encode()
            if hmac.compare_digest(given, current_app.config["DASHBOARD_PASSWORD"].encode()):
                session.clear()
                session.permanent = True
                session["signed_in"] = True
                return redirect(_safe_next(request.args.get("next")))
            error = "That password isn't right."
    return render_template("dash/login.html", error=error, email=email)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("dash.login"))


@bp.route("/account", methods=["GET", "POST"])
def account():
    if current_app.config["AUTH_MODE"] != "accounts":
        return redirect(url_for("dash.overview"))
    users = current_app.extensions["users"]
    user = current_user()
    error = None
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        try:
            if not check_password_hash(user["password_hash"], request.form.get("current_password", "")):
                raise AccountError("Your current password isn't right.")
            if new == request.form.get("current_password", ""):
                raise AccountError("Choose a password different from the current one.")
            check_new_password(new, confirm)
            updated = users.set_password(user["id"], new)
            session["v"] = updated["session_version"]  # stay signed in here, out everywhere else
            g.pop("current_user", None)
            return redirect(url_for("dash.overview"))
        except AccountError as e:
            error = str(e)
    return render_template("dash/account.html", user=user, error=error)


@bp.get("/team")
@admin_only
def team():
    if current_app.config["AUTH_MODE"] != "accounts":
        return render_template("dash/team.html", accounts=False)
    return render_template("dash/team.html", accounts=True,
                           members=current_app.extensions["users"].all(), roles=ROLES)


@bp.post("/team/add")
@admin_only
def team_add():
    users = current_app.extensions["users"]
    form = request.form
    password = temporary_password()
    try:
        added = users.create(form.get("email"), form.get("name"), password,
                             role=form.get("role", "member"), must_change=True)
        notice = {"ok": True, "person": added, "password": password,
                  "message": f"Added {added['name']}."}
    except AccountError as e:
        notice = {"ok": False, "message": str(e)}
    return render_template("dash/team.html", accounts=True, members=users.all(), roles=ROLES,
                           notice=notice, form=form if not notice["ok"] else None)


@bp.post("/team/<int:user_id>")
@admin_only
def team_update(user_id):
    users = current_app.extensions["users"]
    person = users.get(user_id)
    if person is None:
        abort(404)
    action, me = request.form.get("action"), current_user()
    notice = {"ok": True}
    try:
        if action == "reset":
            notice.update(person=person, password=users.reset_password(user_id),
                          message=f"New temporary password for {person['name']}. Their other "
                                  "sessions are signed out.")
        elif action in ("deactivate", "activate"):
            if action == "deactivate" and me and me["id"] == user_id:
                raise AccountError("You can't deactivate your own account.")
            users.set_active(user_id, action == "activate")
            notice["message"] = f"{person['name']} is now {'active' if action == 'activate' else 'deactivated and signed out'}."
        elif action == "role":
            role = request.form.get("role")
            if me and me["id"] == user_id and role != "admin":
                raise AccountError("Ask another admin to change your own role.")
            users.set_role(user_id, role)
            notice["message"] = f"{person['name']} is now {'an admin' if role == 'admin' else 'a member'}."
        else:
            abort(400)
    except AccountError as e:
        notice = {"ok": False, "message": str(e)}
    return render_template("dash/team.html", accounts=True, members=users.all(), roles=ROLES,
                           notice=notice)


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
    rows = _ext()["db"].query("SELECT * FROM unleashed_api_log ORDER BY id DESC LIMIT 300")
    return render_template("dash/api_log.html", rows=rows)


@bp.route("/connection", methods=["GET", "POST"])
def connection():
    if request.method == "POST" and not is_admin():
        return admin_only(lambda: None)()
    from unleashed.diagnostics import run_checks
    from unleashed.reports import Reports

    ext = _ext()
    result, checks, fields = None, None, None
    if request.method == "POST" and ext["has_credentials"]:
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
    store = ext["store"]
    windows = {LABEL: store.get_state(f"window:{ep}") for ep, LABEL in
               (("PurchaseOrders", "Purchase orders"), ("SalesOrders", "Sales orders"),
                ("StockAdjustments", "Stock adjustments"))}
    counts = {ep: store.document_count(ep) for ep in ("PurchaseOrders", "SalesOrders",
                                                     "StockAdjustments")}
    return render_template("dash/connection.html", result=result, checks=checks,
                           fields=fields, ext=ext, runs=store.runs(10), windows=windows,
                           counts=counts, running=_sync_running())


@bp.post("/sync")
def sync_now():
    ext = _ext()
    full = request.form.get("full") == "1" and is_admin()
    if ext["scheduler"] and ext["scheduler"].started:
        ext["scheduler"].trigger(full=full)
    elif ext["syncer"]:
        ext["syncer"].sync(full=full or None)
    return redirect(request.referrer or url_for("dash.overview"))


# ---------------------------------------------------------------- wiring

def init_app(app):
    """Build the API client, the data store and its sync, and register the pages."""
    import hashlib

    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    from unleashed.client import WsgiTransport
    from unleashed.mock_server import MOCK_API_ID, MOCK_API_KEY, create_mock_app
    from unleashed.reports import Reports
    from unleashed.store import LocalStore, LocalStoreClient, SourceMismatch, utc_now
    from unleashed.sync import Syncer, SyncScheduler

    cfg = app.config
    db = app.extensions["db"]
    app.permanent_session_lifetime = dt.timedelta(days=30)

    def log_call(entry):
        db.execute(
            "INSERT INTO unleashed_api_log (timestamp, mode, method, endpoint, query,"
            " status_code, duration_ms, item_count, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utc_now(), entry["mode"], entry["method"], entry["endpoint"], entry["query"],
             entry["status_code"], entry["duration_ms"], entry["item_count"], entry["error"]))

    live_client = UnleashedClient(cfg["UNLEASHED_API_ID"], cfg["UNLEASHED_API_KEY"],
                                  cfg.get("UNLEASHED_TRANSPORT") or HttpTransport(cfg["UNLEASHED_BASE_URL"]),
                                  log_fn=log_call, cache_seconds=cfg["UNLEASHED_CACHE_SECONDS"],
                                  mode="live")
    mode = cfg["UNLEASHED_MODE"] if cfg["UNLEASHED_MODE"] in ("live", "mock", "viewer") else "mock"
    client = None
    if mode == "live":
        client = live_client
    elif mode == "mock":
        mock_app = create_mock_app(MOCK_API_ID, MOCK_API_KEY)
        client = UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(mock_app),
                                 log_fn=log_call, cache_seconds=cfg["UNLEASHED_CACHE_SECONDS"],
                                 mode="mock")
        # Also expose the mock over HTTP at /mock-unleashed for curl/Postman.
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/mock-unleashed": mock_app})

    display_tz = None
    if cfg["TIMEZONE"]:
        from zoneinfo import ZoneInfo
        display_tz = ZoneInfo(cfg["TIMEZONE"])
    if cfg.get("TODAY"):
        today_fn = lambda: cfg["TODAY"]  # noqa: E731
    elif display_tz:
        today_fn = lambda: dt.datetime.now(display_tz).date()  # noqa: E731
    else:
        today_fn = dt.date.today

    store = LocalStore(db)
    source_error = None
    if client is not None:
        if mode == "live":
            account = hashlib.sha256(cfg["UNLEASHED_API_ID"].encode()).hexdigest()[:16]
            source = f"live:{cfg['UNLEASHED_BASE_URL']}:{account}"
        else:
            source = "mock"
        try:
            store.ensure_source(source, mode, allow_reset=cfg["ALLOW_DATABASE_RESET"])
        except SourceMismatch as e:
            source_error, client = str(e), None  # read what's there, but never sync into it

    syncer = scheduler = None
    if client is not None:
        syncer = Syncer(client, store, today_fn=today_fn,
                        history_months=cfg["SYNC_HISTORY_MONTHS"],
                        max_workers=4 if mode == "live" else 1)
        scheduler = SyncScheduler(syncer, cfg["SYNC_INTERVAL_MINUTES"])
        store.mark_interrupted()
    store_client = LocalStoreClient(store, live=client, today_fn=today_fn,
                                    fetch_snapshot=syncer.fetch_snapshot if syncer else None)
    if syncer is not None:
        syncer.on_complete.append(store_client.clear_cache)

    app.extensions["unleashed"] = {
        "mode": mode, "client": client, "live_client": live_client, "db": db,
        "store": store, "syncer": syncer, "scheduler": scheduler,
        "reports": Reports(store_client, today_fn=today_fn),
        "base_url": cfg["UNLEASHED_BASE_URL"], "display_tz": display_tz,
        "has_credentials": bool(cfg["UNLEASHED_API_ID"] and cfg["UNLEASHED_API_KEY"]),
        "source_error": source_error,
    }
    app.register_blueprint(bp)

    if syncer is not None:
        startup = cfg["SYNC_ON_STARTUP"]
        if startup == "blocking":
            syncer.sync()
        if startup == "background":
            scheduler.start()
