"""Gardena Consumption Tracker — Flask app (pages + JSON API)."""
import datetime
import os
import re

import segno
from flask import Flask, jsonify, redirect, render_template, request, url_for

import dashboard
import db as database
import scan_logic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        STORE_CODE=os.environ.get("STORE_CODE", "GDN"),
        DATABASE_PATH=os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "gardena.db")),
        DUPLICATE_SCAN_SECONDS=int(os.environ.get("DUPLICATE_SCAN_SECONDS", "10")),
        UNLEASHED_MODE=os.environ.get("UNLEASHED_MODE", "mock").lower(),
        UNLEASHED_API_ID=os.environ.get("UNLEASHED_API_ID", ""),
        UNLEASHED_API_KEY=os.environ.get("UNLEASHED_API_KEY", ""),
        UNLEASHED_BASE_URL=os.environ.get("UNLEASHED_BASE_URL", "https://api.unleashedsoftware.com"),
        UNLEASHED_CACHE_SECONDS=int(os.environ.get("UNLEASHED_CACHE_SECONDS", "120")),
    )
    if test_config:
        app.config.update(test_config)

    database.init_db(app.config["DATABASE_PATH"], app.config["STORE_CODE"])
    database.register(app)
    dashboard.init_app(app)

    store = lambda: app.config["STORE_CODE"]

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def scan_page():
        return render_template("scan.html", users=_active_users())

    @app.get("/summary")
    def summary_page():
        return render_template("summary.html", **_summary_data())

    @app.get("/correct")
    def correct_page():
        conn = database.get_db()
        items = conn.execute(
            "SELECT i.*, c.current_count FROM items i"
            " LEFT JOIN counts c ON c.item_id = i.id AND c.store_code = ?"
            " WHERE i.active = 1 ORDER BY i.category, i.display_name",
            (store(),),
        ).fetchall()
        adjustments = conn.execute(
            "SELECT e.*, i.display_name, u.name AS employee_name FROM scan_events e"
            " LEFT JOIN items i ON i.id = e.item_id"
            " LEFT JOIN users u ON u.id = e.employee_id"
            " WHERE e.action = 'ADJUSTMENT' AND e.store_code = ?"
            " ORDER BY e.id DESC LIMIT 20",
            (store(),),
        ).fetchall()
        return render_template("correct.html", items=items, users=_active_users(),
                               adjustments=adjustments)

    @app.get("/admin")
    def admin_page():
        conn = database.get_db()
        items = conn.execute(
            "SELECT i.*, c.current_count FROM items i"
            " LEFT JOIN counts c ON c.item_id = i.id AND c.store_code = ?"
            " ORDER BY i.category, i.display_name",
            (store(),),
        ).fetchall()
        users = database.get_db().execute("SELECT * FROM users ORDER BY name").fetchall()
        return render_template("admin.html", items=items, users=users)

    @app.get("/labels")
    def labels_page():
        """Printable QR label sheet.

        ?item_id=..&count=N       -> N unique container labels for a PREMADE item
        ?item_id=..               -> a sheet of identical item-code labels (raw items)
        """
        conn = database.get_db()
        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (request.args.get("item_id"),)
        ).fetchone()
        if item is None:
            return redirect(url_for("admin_page"))
        count = max(1, min(60, request.args.get("count", default=6, type=int)))

        if item["category"] == "PREMADE":
            date_str = request.args.get("date") or datetime.date.today().strftime("%Y%m%d")
            start = request.args.get("start", type=int) or _next_label_seq(conn, item, date_str)
            codes = [f"{item['item_code']}-{date_str}-{start + i:03d}" for i in range(count)]
        else:
            codes = [item["item_code"]] * count

        labels = [{"code": c, "qr": segno.make(c, error="m").svg_data_uri(scale=3)} for c in codes]
        return render_template("labels.html", item=item, labels=labels)

    # -------------------------------------------------------------------- api

    @app.post("/api/scan")
    def api_scan():
        payload = request.get_json(silent=True) or {}
        result = scan_logic.handle_scan(
            database.get_db(), store(), payload.get("code"),
            employee_id=payload.get("employee_id") or None,
            duplicate_window_seconds=app.config["DUPLICATE_SCAN_SECONDS"],
        )
        return jsonify(result)

    @app.post("/api/container-action")
    def api_container_action():
        payload = request.get_json(silent=True) or {}
        result = scan_logic.apply_container_action(
            database.get_db(), store(), payload.get("container_code"),
            payload.get("action"), employee_id=payload.get("employee_id") or None,
        )
        return jsonify(result)

    @app.post("/api/adjust")
    def api_adjust():
        payload = request.get_json(silent=True) or request.form
        result = scan_logic.apply_adjustment(
            database.get_db(), store(), payload.get("item_id"), payload.get("new_count"),
            employee_id=payload.get("employee_id") or None,
            note=payload.get("note") or None,
        )
        if not request.is_json:
            return redirect(url_for("correct_page"))
        return jsonify(result)

    @app.get("/api/summary")
    def api_summary():
        data = _summary_data()
        return jsonify({
            "date": data["date"],
            "raw_usage": [dict(r) for r in data["raw_usage"]],
            "premade": [dict(r) for r in data["premade"]],
            "counts": [dict(r) for r in data["counts"]],
        })

    # ---------------------------------------------------------- admin actions

    @app.post("/admin/items")
    def admin_add_item():
        conn = database.get_db()
        code = scan_logic.normalize_code(request.form.get("item_code"))
        name = (request.form.get("display_name") or "").strip()
        category = request.form.get("category")
        uom = (request.form.get("unit_of_measure") or "").strip() or (
            "container" if category == "PREMADE" else "jug")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", code or "") or not name \
                or category not in ("RAW_INGREDIENT", "PREMADE"):
            return redirect(url_for("admin_page"))
        action = "CONTAINER_LIFECYCLE" if category == "PREMADE" else "USE_ONE"
        try:
            cur = conn.execute(
                "INSERT INTO items (item_code, display_name, category, unit_of_measure,"
                " default_scan_action, active) VALUES (?, ?, ?, ?, ?, 1)",
                (code, name, category, uom, action),
            )
            conn.execute(
                "INSERT INTO counts (store_code, item_id, current_count, updated_at)"
                " VALUES (?, ?, 0, ?)",
                (store(), cur.lastrowid, database.now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        return redirect(url_for("admin_page"))

    @app.post("/admin/items/<int:item_id>/toggle")
    def admin_toggle_item(item_id):
        conn = database.get_db()
        conn.execute("UPDATE items SET active = 1 - active WHERE id = ?", (item_id,))
        conn.commit()
        return redirect(url_for("admin_page"))

    @app.post("/admin/users")
    def admin_add_user():
        name = (request.form.get("name") or "").strip()
        role = request.form.get("role") if request.form.get("role") in (
            "EMPLOYEE", "MANAGER", "ADMIN") else "EMPLOYEE"
        if name:
            conn = database.get_db()
            conn.execute("INSERT INTO users (name, role, active) VALUES (?, ?, 1)", (name, role))
            conn.commit()
        return redirect(url_for("admin_page"))

    @app.post("/admin/users/<int:user_id>/toggle")
    def admin_toggle_user(user_id):
        conn = database.get_db()
        conn.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
        conn.commit()
        return redirect(url_for("admin_page"))

    # ---------------------------------------------------------------- helpers

    def _active_users():
        return database.get_db().execute(
            "SELECT * FROM users WHERE active = 1 ORDER BY name").fetchall()

    def _next_label_seq(conn, item, date_str):
        prefix = f"{item['item_code']}-{date_str}-"
        row = conn.execute(
            "SELECT MAX(CAST(substr(container_code, ?) AS INTEGER)) AS max_seq"
            " FROM premade_containers WHERE container_code LIKE ?",
            (len(prefix) + 1, prefix + "%"),
        ).fetchone()
        return (row["max_seq"] or 0) + 1

    def _summary_data():
        conn = database.get_db()
        day = database.today()
        raw_usage = conn.execute(
            "SELECT i.display_name, i.unit_of_measure,"
            "       COALESCE(SUM(-e.quantity_delta), 0) AS used"
            " FROM items i LEFT JOIN scan_events e ON e.item_id = i.id"
            "   AND e.action = 'USE_ONE' AND e.store_code = ? AND date(e.timestamp) = ?"
            " WHERE i.category = 'RAW_INGREDIENT' AND i.active = 1"
            " GROUP BY i.id ORDER BY i.display_name",
            (store(), day),
        ).fetchall()
        premade = conn.execute(
            "SELECT i.display_name,"
            "  COALESCE(SUM(e.action = 'CONTAINER_MADE'), 0) AS made,"
            "  COALESCE(SUM(e.action = 'CONTAINER_FINISHED'), 0) AS finished,"
            "  COALESCE(SUM(e.action = 'CONTAINER_WASTED'), 0) AS wasted"
            " FROM items i LEFT JOIN scan_events e ON e.item_id = i.id"
            "   AND e.store_code = ? AND date(e.timestamp) = ?"
            " WHERE i.category = 'PREMADE' AND i.active = 1"
            " GROUP BY i.id ORDER BY i.display_name",
            (store(), day),
        ).fetchall()
        counts = conn.execute(
            "SELECT i.display_name, i.category, i.unit_of_measure, c.current_count, c.updated_at"
            " FROM counts c JOIN items i ON i.id = c.item_id"
            " WHERE c.store_code = ? AND i.active = 1 ORDER BY i.category, i.display_name",
            (store(),),
        ).fetchall()
        events = conn.execute(
            "SELECT e.*, i.display_name, u.name AS employee_name FROM scan_events e"
            " LEFT JOIN items i ON i.id = e.item_id"
            " LEFT JOIN users u ON u.id = e.employee_id"
            " WHERE e.store_code = ? AND date(e.timestamp) = ?"
            " ORDER BY e.id DESC LIMIT 50",
            (store(), day),
        ).fetchall()
        return {"date": day, "raw_usage": raw_usage, "premade": premade,
                "counts": counts, "events": events}

    return app
