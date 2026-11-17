"""What needs attention today, for each person by the tabs they open, on a page and in a morning
email.

  Warehouse:    products at or below their minimum, purchase orders overdue for delivery,
                batches expired or expiring within 3 days, store requests waiting.
  Maintenance:  machines down, tasks overdue, new requests.
  Production:   production orders due today or earlier and not completed.
  Finance:      yesterday's COGS not explained by any document (over $25).

The morning email is opt-in (each person, on their Account page). It goes once a day, after
DAILY_EMAIL_HOUR in the business's time zone, from whichever instance's sync gets there first
(the day is claimed in the database), and only to people with something to see.
"""
import datetime as dt
import html
import threading
from email.message import EmailMessage

from flask import url_for

from unleashed.client import UnleashedError

EXPIRING_DAYS = 3
UNEXPLAINED_AT = 25.0  # dollars of yesterday's COGS no document explains
SHOWN = 8  # items listed under each heading; the rest are counted


def _qty(value):
    return f"{float(value or 0):,.4f}".rstrip("0").rstrip(".")


def _money(value):
    return f"${float(value or 0):,.2f}"


def gather(app, areas, today):
    """[{"title", "area", "count", "items": [{"text", "url"}], "more": url, "note"}] of what needs
    attention in `areas`, sections with nothing left out. Run with an app (and request) context."""
    sections = []
    ext = app.extensions["unleashed"]
    reports = ext.get("reports")

    def section(area, title, items, more=None, note=None, count=None):
        if items or note:
            sections.append({"area": area, "title": title, "count": len(items) if count is None else count,
                             "items": items[:SHOWN], "more": more, "note": note})

    def safely(fn):
        try:
            return fn()
        except UnleashedError:
            return None

    if "warehouse" in areas and reports is not None:
        stock = safely(lambda: reports.stock_on_hand()["rows"]) or []
        low = [r for r in stock if r.get("recommended")]
        section("warehouse", "At or below their minimum",
                [{"text": f"{r['description'] or r['product_code']}: {_qty(r['qty'])} on hand, minimum {_qty(r['par_min'])}"
                         f", order {_qty(r['recommended'])}" + (f" from {r['supplier_name']}" if r.get("supplier_name") else ""),
                  "url": url_for("dash.stock_on_hand", to_order=1)} for r in low],
                more=url_for("dash.stock_on_hand", to_order=1))
        placed = safely(lambda: reports.purchase_orders(today - dt.timedelta(days=120), today, status="Placed")) or []
        late = sorted((o for o in placed if o.get("required_date") and o["required_date"] < today),
                      key=lambda o: o["required_date"])
        section("warehouse", "Purchase orders overdue for delivery",
                [{"text": f"{o['number']} from {o['supplier_name']}: due {o['required_date']:%b} {o['required_date'].day}",
                  "url": url_for("dash.purchase_detail", number=o["number"])} for o in late],
                more=url_for("dash.purchases", status="Placed"))
        batches = safely(lambda: reports.expiring_batches(EXPIRING_DAYS))
        section("warehouse", f"Batches expired or expiring within {EXPIRING_DAYS} days",
                [{"text": f"{b['description'] or b['product_code']}, batch {b['number']}: {_qty(b['qty'])} in {b['warehouse']}, "
                          + (f"expired {b['expiry']:%b} {b['expiry'].day}" if b["days_left"] < 0
                             else "expires today" if b["days_left"] == 0 else f"expires {b['expiry']:%b} {b['expiry'].day}"),
                  "url": url_for("dash.batches", product=b["product_code"], batch=b["number"]) + "#trace"}
                 for b in batches or []], more=url_for("dash.batches", within=EXPIRING_DAYS))
    if ("warehouse" in areas or "store_orders" in areas) and "store_orders" in app.extensions:
        waiting = app.extensions["store_orders"].requests(status="submitted")
        names = {}
        if reports is not None:
            names = {c.get("CustomerCode"): c.get("CustomerName") for c in safely(reports.customers) or []}
        section("warehouse", "Store requests waiting for the warehouse",
                [{"text": f"Request {r['id']} from {names.get(r['store_code']) or r['store_code']}"
                          + (f", needed by {r['needed_by']}" if r.get("needed_by") else ""),
                  "url": url_for("dash.store_order", request_id=r["id"])} for r in waiting],
                more=url_for("dash.store_orders", status="submitted"))
    if "maintenance" in areas and "maintenance" in app.extensions:
        store = app.extensions["maintenance"]
        requests = store.requests("active")
        section("maintenance", "Machines down",
                [{"text": f"{r['number']}: {r['title']}" + (f" ({r['location_path']})" if r.get("location_path") else ""),
                  "url": url_for("maint.request", request_id=r["id"])} for r in requests if r.get("breakdown")],
                more=url_for("maint.requests"))
        overdue = [t for t in store.tasks(today) if t["status"] == "overdue"]
        section("maintenance", "Maintenance tasks overdue",
                [{"text": f"{t['name']}" + (f" ({t['equipment_name'] or t['location']})" if t.get("equipment_name") or t.get("location") else "")
                          + f": {t['due_label']}", "url": url_for("maint.task", task_id=t["id"])} for t in overdue],
                more=url_for("maint.tasks", status="overdue"))
        section("maintenance", "New maintenance requests",
                [{"text": f"{r['number']}: {r['title']}", "url": url_for("maint.request", request_id=r["id"])}
                 for r in requests if r["status"] == "open" and not r.get("breakdown")], more=url_for("maint.requests"))
    if "production" in areas and reports is not None:
        due = [a for a in safely(lambda: reports.assemblies(today - dt.timedelta(days=60), today)) or []
               if a.get("open") and a.get("due") and a["due"] <= today]
        section("production", "Production orders due",
                [{"text": f"{a['number']}: {_qty(a['qty'])} {a['description'] or a['product_code']}, due "
                          f"{a['due']:%b} {a['due'].day}", "url": url_for("dash.production_order", number=a["number"])}
                 for a in sorted(due, key=lambda a: a["due"])], more=url_for("dash.production_orders"))
    if "finance" in areas and reports is not None:
        yesterday = today - dt.timedelta(days=1)
        row = None
        try:
            daily = reports.daily_unexplained(reports.cogs(yesterday))
            row = next((d for d in (daily or {}).get("days") or [] if d["date"] == yesterday), None)
        except (UnleashedError, KeyError, ValueError, TypeError):
            row = None
        if row and abs(row["value"]) >= UNEXPLAINED_AT:
            tops = ", ".join(p.get("description") or p["product_code"] for p in (row.get("products") or [])[:3])
            section("finance", "COGS not explained yesterday",
                    [{"text": f"{_money(row['value'])} of COGS on {yesterday:%b} {yesterday.day} isn't on any document"
                              + (f" (mostly {tops})" if tops else ""),
                      "url": url_for("dash.cogs_unaccounted", month=yesterday.strftime("%Y-%m")) + "#daily"}])
    return sections


# ---------------------------------------------------------------- the morning email

def wants_email(db, user_id):
    rows = db.query("SELECT daily_email FROM user_prefs WHERE user_id = ?", (user_id,))
    return bool(rows and rows[0]["daily_email"])


def set_wants_email(db, user_id, on):
    db.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))
    db.execute("INSERT INTO user_prefs (user_id, daily_email) VALUES (?, ?)", (user_id, 1 if on else 0))


def message_for(sections, user, sender, company, base_url, today):
    """The email of what needs attention: plain text and HTML, links made absolute with `base_url`
    (none when it isn't set)."""
    def link(path):
        return (base_url.rstrip("/") + path) if base_url and path else None

    count = sum(s["count"] for s in sections)
    subject = f"{company} Core: {count} thing{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention today"
    text = [f"Good morning {user.get('name') or ''},".replace(" ,", ","), "", f"What needs attention on {today:%A %b} {today.day}:", ""]
    parts = []
    for s in sections:
        text.append(f"{s['title']} ({s['count']})")
        parts.append(f"<h3 style='margin:16px 0 4px;font-size:15px'>{html.escape(s['title'])} ({s['count']})</h3><ul style='margin:0;padding-left:18px'>")
        for item in s["items"]:
            url = link(item.get("url"))
            text.append(f"  - {item['text']}" + (f"  {url}" if url else ""))
            parts.append("<li>" + (f"<a href='{html.escape(url)}'>{html.escape(item['text'])}</a>" if url else html.escape(item["text"])) + "</li>")
        if s["count"] > len(s["items"]):
            more = link(s.get("more"))
            text.append(f"  … and {s['count'] - len(s['items'])} more" + (f": {more}" if more else ""))
            parts.append(f"<li>… and {s['count'] - len(s['items'])} more" + (f": <a href='{html.escape(more)}'>see all</a>" if more else "") + "</li>")
        parts.append("</ul>")
        text.append("")
    home = link("/attention")
    text += [f"All of it in Core: {home}" if home else "", "", "You get this because it's ticked on your Account page in Core."]
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, sender, user["email"]
    message.set_content("\n".join(text))
    message.add_alternative("<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:640px'>"
                            f"<p>{html.escape(text[0])}</p><p>What needs attention on {today:%A %b} {today.day}:</p>"
                            + "".join(parts)
                            + (f"<p style='margin-top:18px'><a href='{html.escape(home)}'>Open Core</a></p>" if home else "")
                            + "<p style='color:#777;font-size:12px'>You get this because it's ticked on your Account page in Core.</p></div>",
                            subtype="html")
    return message


_sending = threading.Lock()


def send_daily(app, force_user=None):
    """Send today's emails, once a day, after DAILY_EMAIL_HOUR (business time). `force_user`: send
    that one person's now, whatever the time and whether it went already (the Send me now button).
    Returns how many were sent."""
    from access import AREA_KEYS
    from vendor_email import can_send, send, sender_for
    cfg, db = app.config, app.extensions["db"]
    if cfg.get("AUTH_MODE") != "accounts" or not can_send(cfg):
        return 0
    ext = app.extensions["unleashed"]
    today = ext["today_fn"]()
    now = dt.datetime.now(ext.get("display_tz") or dt.timezone.utc)
    if force_user is None:
        if now.hour < int(cfg.get("DAILY_EMAIL_HOUR") or 7):
            return 0
        # Claim the day, so only one instance (and one thread) sends it.
        with _sending:
            claimed = db.query("SELECT 1 FROM app_settings WHERE key = ?", (f"attention_sent:{today.isoformat()}",))
            if claimed:
                return 0
            try:
                db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)",
                           (f"attention_sent:{today.isoformat()}", now.isoformat(timespec="seconds")))
            except Exception:  # noqa: BLE001 (another instance claimed it first)
                return 0
    users = app.extensions["users"]
    profiles = app.extensions["profiles"]
    company = cfg.get("COMPANY_NAME") or "Yeems Coffee"
    sent = 0
    people = [force_user] if force_user else [u for u in users.all() if u["active"] and wants_email(db, u["id"])]
    for user in people:
        areas = set(AREA_KEYS) if user["role"] == "admin" else profiles.areas_of(user)
        with app.test_request_context(base_url=cfg.get("PUBLIC_URL") or "http://localhost"):
            sections = gather(app, areas, today)
        if not sections and not force_user:
            continue
        sender, mailbox, _ = sender_for(cfg, user, company)
        if not sections:
            sections = [{"title": "Nothing needs attention", "count": 0, "items": [], "more": None}]
        message = message_for(sections, user, sender, company, cfg.get("PUBLIC_URL"), today)
        try:
            send(cfg, message, mailbox=mailbox)
            sent += 1
        except Exception as e:  # noqa: BLE001 (one person's failure doesn't stop the others)
            app.logger.warning("Needs-attention email to %s not sent: %s", user["email"], e)
            if force_user:
                raise
    return sent
