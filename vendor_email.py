"""Emailing purchase orders to vendors.

Unleashed's API has no "email this supplier their purchase orders" setting and sends no email,
so Core keeps the choice itself: for each vendor, whether they take orders by email, and at
which address (starting from the vendor's email in Unleashed). A placed purchase order to such
a vendor can be emailed from its page; who sent what, to whom and when is kept.

Mail goes out one of two ways, set in the environment; with neither, nothing can be sent and
the pages say so:

  Microsoft 365 (MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET): Core is registered once as an
      app in the organisation's Microsoft Entra, with the Mail.Send application permission. Each
      email is then sent through Microsoft Graph from the mailbox of the person signed in to
      Core (their Core sign-in email must be their Microsoft 365 address), and is kept in their
      Sent Items. Used when set.
  An SMTP server (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY, MAIL_FROM):
      every email from the one MAIL_FROM mailbox, replies going to the person who sent it.

Recipients only ever come from the vendor's saved settings, never from the send form, so the
page can't be used to send mail to anyone else.
"""
import base64
import datetime as dt
import html
import json
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
MAX_ADDRESSES = 5
# Statuses of a purchase order a vendor can be sent: placed with them, not a draft or deleted.
SENDABLE = ("Placed", "Receipted", "Costed", "Complete")


class MailError(ValueError):
    """A problem to show the person (no mail server, a bad address, the server refused)."""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def addresses(value, label="The email address"):
    """The addresses typed in one box (commas, semicolons or spaces between them), checked: a list,
    empty for none. Anything that isn't a plain address is refused, so a header can't be forged."""
    found = [a for a in re.split(r"[,;\s]+", (value or "").strip()) if a]
    if len(found) > MAX_ADDRESSES:
        raise MailError(f"{label}: {MAX_ADDRESSES} addresses at most.")
    for a in found:
        if len(a) > 254 or not EMAIL_RE.match(a):
            raise MailError(f"{label}: “{a[:80]}” isn't an email address.")
    return found


def mail_settings(config):
    """How mail is sent, from the app's settings: {"host", "port", "user", "password", "security",
    "sender"}; None when no server or no sender is set."""
    host, sender = (config.get("SMTP_HOST") or "").strip(), (config.get("MAIL_FROM") or "").strip()
    if not host or not sender or not EMAIL_RE.match(parseaddr(sender)[1] or ""):
        return None
    security = (config.get("SMTP_SECURITY") or "starttls").strip().lower()
    port = int(config.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    return {"host": host, "port": port, "user": config.get("SMTP_USER") or "", "password": config.get("SMTP_PASSWORD") or "",
            "security": security, "sender": sender}


def microsoft_settings(config):
    """Core's app registration in Microsoft Entra: {"tenant", "client_id", "secret"}; None unless
    all three are set. The tenant and client id go into URLs, so anything but an id (or a
    domain, for the tenant) is refused."""
    tenant, client_id = (config.get("MS_TENANT_ID") or "").strip(), (config.get("MS_CLIENT_ID") or "").strip()
    secret = config.get("MS_CLIENT_SECRET") or ""
    if not (tenant and client_id and secret):
        return None
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,120}", tenant) or not re.fullmatch(r"[A-Za-z0-9-]{1,60}", client_id):
        return None
    return {"tenant": tenant, "client_id": client_id, "secret": secret}


def mail_mode(config):
    """How email is sent: "outbox" (kept in MAIL_OUTBOX, for tests), "microsoft" (through Microsoft
    365, from the sender's own mailbox), "smtp" (the mail server, from MAIL_FROM), or None."""
    if config.get("MAIL_OUTBOX") is not None:
        return "outbox"
    if microsoft_settings(config):
        return "microsoft"
    return "smtp" if mail_settings(config) else None


def can_send(config):
    return mail_mode(config) is not None


def setup_report(config):
    """Which email settings Core can see, by name only (never a value), so a misspelled or
    missing one shows: [{"way", "settings": [{"name", "state": "set" | "missing" | "not valid"}],
    "ready"}] for Microsoft 365 and the mail server."""
    def state(name, valid=True):
        if not (config.get(name) or "").strip():
            return "missing"
        return "set" if valid else "not valid"

    tenant, client_id = (config.get("MS_TENANT_ID") or "").strip(), (config.get("MS_CLIENT_ID") or "").strip()
    microsoft = [{"name": "MS_TENANT_ID", "state": state("MS_TENANT_ID", bool(re.fullmatch(r"[A-Za-z0-9.-]{1,120}", tenant)))},
                 {"name": "MS_CLIENT_ID", "state": state("MS_CLIENT_ID", bool(re.fullmatch(r"[A-Za-z0-9-]{1,60}", client_id)))},
                 {"name": "MS_CLIENT_SECRET", "state": state("MS_CLIENT_SECRET")}]
    sender = parseaddr((config.get("MAIL_FROM") or "").strip())[1]
    smtp = [{"name": "SMTP_HOST", "state": state("SMTP_HOST")},
            {"name": "MAIL_FROM", "state": state("MAIL_FROM", bool(EMAIL_RE.match(sender or "")))}]
    return [{"way": "Microsoft 365", "settings": microsoft, "ready": microsoft_settings(config) is not None},
            {"way": "A mail server", "settings": smtp, "ready": mail_settings(config) is not None}]


NOT_SET_UP = ("Email isn't set up: an admin connects Microsoft 365 (MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET) "
              "or a mail server (SMTP_HOST, MAIL_FROM); see the README.")

# ---------------------------------------------------------------- Microsoft 365 (Graph)

GRAPH = "https://graph.microsoft.com/v1.0"
_tokens, _token_lock = {}, threading.Lock()  # (tenant, client id) -> (token, good until)


def _http(method, url, headers=None, data=None):
    """One HTTPS request: (status, parsed JSON or {}). The default way Core reaches Microsoft."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    try:
        return status, json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        return status, {}


def _transport(config):
    return config.get("MS_GRAPH_TRANSPORT") or _http


def _microsoft_token(config, settings):
    """An access token for Microsoft Graph as the app itself (client credentials), kept until
    shortly before it runs out."""
    key = (settings["tenant"], settings["client_id"])
    with _token_lock:
        token, until = _tokens.get(key, (None, 0))
        if token and until > time.time():
            return token
    try:
        status, body = _transport(config)(
            "POST", f"https://login.microsoftonline.com/{settings['tenant']}/oauth2/v2.0/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode({"client_id": settings["client_id"], "client_secret": settings["secret"],
                                    "scope": "https://graph.microsoft.com/.default",
                                    "grant_type": "client_credentials"}).encode())
    except OSError as e:
        raise MailError(f"Core couldn't reach Microsoft to sign in ({type(e).__name__}). Nothing was sent; try again.") from None
    if status != 200 or not body.get("access_token"):
        # Microsoft's error names what's wrong; its description (which quotes ids) stays out of the page.
        code, detail = body.get("error") or f"HTTP {status}", body.get("error_description") or ""
        if "AADSTS7000215" in detail or "AADSTS7000222" in detail or code == "invalid_client":
            why = "the app's secret (MS_CLIENT_SECRET) is wrong or has expired: make a new one in Microsoft Entra"
        elif "AADSTS700016" in detail or code == "unauthorized_client":
            why = "the app (MS_CLIENT_ID) isn't registered in this Microsoft organisation"
        elif "AADSTS90002" in detail or code == "invalid_request" and "tenant" in detail.lower():
            why = "the organisation (MS_TENANT_ID) wasn't found"
        else:
            why = f"Microsoft said: {code}"
        raise MailError(f"Microsoft refused Core's sign-in: {why}. Nothing was sent.")
    with _token_lock:
        _tokens[key] = (body["access_token"], time.time() + max(int(body.get("expires_in") or 0) - 120, 30))
    return body["access_token"]


def _recipients(header):
    return [{"emailAddress": {"address": a}} for a in addresses(header or "")]


def _send_through_microsoft(config, settings, message, mailbox):
    """Send from `mailbox` (a person's Microsoft 365 address) with Graph's sendMail; it's kept
    in their Sent Items."""
    if not mailbox or not EMAIL_RE.match(mailbox):
        raise MailError("Core sends from your own Microsoft 365 mailbox, so sign in to Core with your own account "
                        "(its email must be your Microsoft 365 address).")
    html_part = message.get_body(preferencelist=("html",))
    plain_part = message.get_body(preferencelist=("plain",))
    body = {"contentType": "HTML", "content": html_part.get_content()} if html_part is not None else \
        {"contentType": "Text", "content": plain_part.get_content() if plain_part is not None else ""}
    payload = {"message": {"subject": str(message["Subject"] or ""), "body": body,
                           "toRecipients": _recipients(message["To"]), "ccRecipients": _recipients(message["Cc"])},
               "saveToSentItems": True}
    attached = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": part.get_filename() or "attachment",
                 "contentType": part.get_content_type(),
                 "contentBytes": base64.b64encode(part.get_payload(decode=True) or b"").decode("ascii")}
                for part in message.iter_attachments()]
    if attached:
        payload["message"]["attachments"] = attached
    url = f"{GRAPH}/users/{urllib.parse.quote(mailbox, safe='@.')}/sendMail"
    for attempt in (1, 2):
        token = _microsoft_token(config, settings)
        try:
            status, reply = _transport(config)("POST", url, {"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"},
                                               json.dumps(payload).encode())
        except OSError as e:
            raise MailError(f"Core couldn't reach Microsoft ({type(e).__name__}). It may not have been sent: check "
                            "your Sent Items before trying again.") from None
        if status == 401 and attempt == 1:  # a token Microsoft no longer takes: get a fresh one once
            with _token_lock:
                _tokens.pop((settings["tenant"], settings["client_id"]), None)
            continue
        break
    if status in (200, 202):
        return
    code = ((reply.get("error") or {}).get("code") or f"HTTP {status}")[:80]
    if status == 403 or code in ("ErrorAccessDenied", "AccessDenied", "Authorization_RequestDenied"):
        why = (f"Core isn't allowed to send as {mailbox}. In Microsoft Entra, the app needs the Mail.Send application "
               "permission with admin consent; if sending is limited to a group, they need to be in it")
    elif status == 404 or code in ("ErrorInvalidUser", "ResourceNotFound", "MailboxNotEnabledForRESTAPI",
                                   "Request_ResourceNotFound", "ErrorMailboxNotEnabledForRESTAPI"):
        why = (f"{mailbox} has no Microsoft 365 mailbox. Your Core sign-in email must be your Microsoft 365 address "
               "(an admin can change it on the Team page)")
    elif status == 429 or code in ("ApplicationThrottled", "TooManyRequests"):
        why = "Microsoft is limiting how fast mail is sent right now. Wait a minute and try again"
    elif code in ("ErrorInvalidRecipients", "ErrorInvalidSmtpAddress"):
        why = "Microsoft refused one of the vendor's addresses. Check it under Vendor emails"
    else:
        why = f"Microsoft said: {code}"
    raise MailError(f"The email wasn't sent: {why}.")


def send(config, message, mailbox=None):
    """Send an EmailMessage. Through Microsoft 365 it goes from `mailbox` (the sender's own
    address); through the mail server, from the message's From. With MAIL_OUTBOX (a list, for
    tests and trying things out) it's kept there instead of sent."""
    mode = mail_mode(config)
    if mode == "outbox":
        config["MAIL_OUTBOX"].append(message)
        return
    if mode == "microsoft":
        return _send_through_microsoft(config, microsoft_settings(config), message, mailbox)
    settings = mail_settings(config)
    if settings is None:
        raise MailError(NOT_SET_UP)
    try:
        if settings["security"] == "ssl":
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=20)
        with server:
            if settings["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
            if settings["user"]:
                server.login(settings["user"], settings["password"])
            server.send_message(message)
    except smtplib.SMTPRecipientsRefused as e:
        raise MailError(f"The mail server refused the address: {', '.join(e.recipients)}.") from None
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server refused Core's sign-in (SMTP_USER / SMTP_PASSWORD).") from None
    except (smtplib.SMTPException, OSError) as e:  # the message stays out of the page: it can name hosts
        raise MailError(f"The email couldn't be sent ({type(e).__name__}). Nothing went to the vendor; try again.") from None


def _qty(value):
    return f"{float(value or 0):,.4f}".rstrip("0").rstrip(".")


def _money(value):
    return f"${float(value or 0):,.2f}"


def _day(value):
    return f"{value:%b} {value.day}, {value.year}" if value else ""


def order_lines(po):
    """A purchase order's lines as the vendor reads them: in the unit ordered, at its price."""
    return [{"code": l["product_code"], "description": l.get("description") or "", "qty": _qty(l["order_qty"]),
             "unit": l.get("order_unit") or "", "price": _money(l["order_price"]), "total": _money(l["line_total"])}
            for l in po["lines"]]


def order_charges(po):
    """Freight and other charges that are on the order itself (not costs added in Unleashed
    afterwards, which the vendor didn't quote)."""
    return [{"description": c["description"], "amount": _money(c["amount"])}
            for c in po.get("cost_lines") or [] if c.get("field") == "PurchaseOrderLines"]


def order_pdf(po, company, deliver_to=None, supplier_codes=None, note=""):
    """The purchase order as a PDF (bytes): what the vendor is sent as an attachment. The same
    facts, lines, charges and totals as the email, a page or more of US Letter."""
    from simple_pdf import PAGE_HEIGHT, PAGE_WIDTH, Pdf, width, wrap
    supplier_codes = supplier_codes or {}
    left, right, bottom = 54.0, PAGE_WIDTH - 54.0, 60.0
    pdf = Pdf(title=f"Purchase order {po['number']}", author=company)
    # Columns: product code, description (wrapped), quantity and unit, price, total (right-aligned).
    col_desc, col_qty, col_price, col_total = left + 112, right - 190, right - 86, right
    desc_width = col_qty - 62 - col_desc

    def table_head(y):
        pdf.box(left, y - 6, right - left, 20, gray=0.92)
        for x, label, align in ((left + 6, "Product", "left"), (col_desc, "Description", "left"),
                                (col_qty, "Quantity", "right"), (col_price, "Price", "right"),
                                (col_total - 6, "Total", "right")):
            pdf.text(x, y, label, size=9, bold=True, align=align)
        return y - 22

    def page_head():
        pdf.text(left, PAGE_HEIGHT - 60, company, size=16, bold=True)
        pdf.text(right, PAGE_HEIGHT - 58, "PURCHASE ORDER", size=13, bold=True, align="right", gray=0.25)
        pdf.text(right, PAGE_HEIGHT - 76, po["number"], size=12, align="right")
        pdf.line(left, PAGE_HEIGHT - 86, right, PAGE_HEIGHT - 86, thickness=1)
        return PAGE_HEIGHT - 108

    def room(y, needed):
        """Start another page (with the heading and the table's head) when `needed` won't fit."""
        if y - needed >= bottom:
            return y
        pdf.new_page()
        return table_head(page_head())

    y = page_head()
    # Who it's to, and the order's facts, side by side.
    pdf.text(left, y, "TO", size=8, bold=True, gray=0.4)
    pdf.text(left, y - 14, po.get("supplier_name") or po.get("supplier_code") or "", size=11, bold=True)
    facts = [("Order date", _day(po.get("order_date"))), ("Deliver by", _day(po.get("required_date"))),
             ("Your reference", po.get("reference") or "")]
    fy = y
    for label, value in [f for f in facts if f[1]]:
        pdf.text(right - 150, fy, label, size=9, gray=0.4)
        pdf.text(right, fy, value, size=10, align="right")
        fy -= 15
    y -= 34
    if deliver_to or po.get("warehouse"):
        pdf.text(left, y, "DELIVER TO", size=8, bold=True, gray=0.4)
        for line in wrap(deliver_to or po.get("warehouse"), 280, size=10):
            y -= 14
            pdf.text(left, y, line, size=10)
        y -= 8
    y = min(y, fy) - 10
    if (note or "").strip():
        for line in wrap(note.strip(), right - left, size=10):
            pdf.text(left, y, line, size=10)
            y -= 14
        y -= 8

    y = table_head(y)
    for l in order_lines(po):
        described = wrap(l["description"], desc_width, size=10)
        theirs = supplier_codes.get(l["code"])
        codes = wrap(l["code"], 100, size=10) + ([f"Yours: {c}" for c in wrap(theirs, 70, size=8)] if theirs else [])
        rows = max(len(described), len(codes))
        y = room(y, rows * 13 + 8)
        for i, part in enumerate(codes):
            ours = i < len(codes) - (1 if theirs else 0) or not theirs
            pdf.text(left + 6, y - i * 13, part, size=10 if ours else 8, gray=0.0 if ours else 0.4)
        for i, part in enumerate(described):
            pdf.text(col_desc, y - i * 13, part, size=10)
        pdf.text(col_qty, y, f"{l['qty']} {l['unit']}".strip(), size=10, align="right")
        pdf.text(col_price, y, l["price"], size=10, align="right")
        pdf.text(col_total - 6, y, l["total"], size=10, align="right")
        y -= rows * 13 + 4
        pdf.line(left, y + 6, right, y + 6, thickness=0.3, gray=0.75)
        y -= 6
    for c in order_charges(po):
        y = room(y, 22)
        pdf.text(col_desc, y, wrap(c["description"], col_price - col_desc, size=10)[0], size=10)
        pdf.text(col_total - 6, y, c["amount"], size=10, align="right")
        y -= 17
        pdf.line(left, y + 6, right, y + 6, thickness=0.3, gray=0.75)
        y -= 6

    charged = sum(c["amount"] or 0 for c in po.get("cost_lines") or [] if c.get("field") == "PurchaseOrderLines")
    totals = [("Goods", _money(po.get("goods")), False)]
    if charged:
        totals.append(("Charges", _money(charged), False))
    if po.get("tax"):
        totals.append(("Tax", _money(po["tax"]), False))
    totals.append(("Total", _money((po.get("goods") or 0) + charged + (po.get("tax") or 0)), True))
    y = room(y, len(totals) * 16 + 12) - 4
    for label, value, strong in totals:
        pdf.text(col_price, y, label, size=11 if strong else 10, bold=strong, align="right")
        pdf.text(col_total - 6, y, value, size=11 if strong else 10, bold=strong, align="right")
        y -= 16
    if po.get("comments"):
        lines = wrap(po["comments"], right - left, size=10)
        y = room(y, len(lines) * 13 + 30) - 10
        pdf.text(left, y, "COMMENTS", size=8, bold=True, gray=0.4)
        for line in lines:
            y -= 13
            pdf.text(left, y, line, size=10)
    count = pdf.page_number
    for page in range(count):
        pdf.text(left, 36, f"{company} · Purchase order {po['number']}", size=8, gray=0.45, page=page)
        pdf.text(right, 36, f"Page {page + 1} of {count}", size=8, gray=0.45, align="right", page=page)
    return pdf.render()


def default_subject(po, company):
    return f"Purchase order {po['number']} from {company}"


def build_message(po, to, cc, sender, company, note="", reply_to=None, deliver_to=None, supplier_codes=None):
    """The email of a purchase order: plain text, the same as HTML, and the order as a PDF
    attached (PO-….pdf). `supplier_codes`: {product code: the vendor's own code for it}, shown
    beside ours when known."""
    supplier_codes = supplier_codes or {}
    lines, charges = order_lines(po), order_charges(po)
    charged = sum(c["amount"] or 0 for c in po.get("cost_lines") or [] if c.get("field") == "PurchaseOrderLines")
    total = (po.get("goods") or 0) + charged + (po.get("tax") or 0)
    facts = [("Purchase order", po["number"]), ("Order date", _day(po.get("order_date"))),
             ("Deliver by", _day(po.get("required_date"))), ("Deliver to", deliver_to or po.get("warehouse") or ""),
             ("Your reference", po.get("reference") or "")]
    facts = [(k, v) for k, v in facts if v]
    note = (note or "").strip()

    text = [f"{po.get('supplier_name') or 'Hello'},", "", f"Please supply the following for {company}.", ""]
    if note:
        text += [note, ""]
    text += [f"{k}: {v}" for k, v in facts] + [""]
    for l in lines:
        theirs = supplier_codes.get(l["code"])
        text.append(f"- {l['qty']} {l['unit']} x {l['description']} ({l['code']}{', your code ' + theirs if theirs else ''})"
                    f" at {l['price']} = {l['total']}".replace("  ", " "))
    for c in charges:
        text.append(f"- {c['description']}: {c['amount']}")
    text += ["", f"Goods: {_money(po.get('goods'))}"]
    if po.get("tax"):
        text.append(f"Tax: {_money(po['tax'])}")
    text += [f"Total: {_money(total)}", ""]
    if po.get("comments"):
        text += [f"Comments: {po['comments']}", ""]
    text += ["The order is attached as a PDF.", "",
             "Please reply to this email to confirm the order, or to tell us of anything you can't supply.", "",
             company]

    e = html.escape
    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #ddd'>{e(l['code'])}"
        f"{'<br><span style=color:#666>Your code: ' + e(supplier_codes[l['code']]) + '</span>' if supplier_codes.get(l['code']) else ''}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd'>{e(l['description'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap'>{e(l['qty'])} {e(l['unit'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{e(l['price'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{e(l['total'])}</td></tr>" for l in lines)
    rows += "".join(
        f"<tr><td colspan='4' style='padding:6px 10px;border-bottom:1px solid #ddd'>{e(c['description'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:right'>{e(c['amount'])}</td></tr>" for c in charges)
    totals = f"<tr><td colspan='4' style='padding:6px 10px;text-align:right'>Goods</td><td style='padding:6px 10px;text-align:right'>{_money(po.get('goods'))}</td></tr>"
    if po.get("tax"):
        totals += f"<tr><td colspan='4' style='padding:6px 10px;text-align:right'>Tax</td><td style='padding:6px 10px;text-align:right'>{_money(po['tax'])}</td></tr>"
    totals += f"<tr><td colspan='4' style='padding:6px 10px;text-align:right'><strong>Total</strong></td><td style='padding:6px 10px;text-align:right'><strong>{_money(total)}</strong></td></tr>"
    body = (
        f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:720px'>"
        f"<p>{e(po.get('supplier_name') or 'Hello')},</p><p>Please supply the following for {e(company)}.</p>"
        + (f"<p style='white-space:pre-wrap'>{e(note)}</p>" if note else "")
        + "<table style='border-collapse:collapse;margin:0 0 14px'>"
        + "".join(f"<tr><td style='padding:2px 14px 2px 0;color:#666'>{e(k)}</td><td style='padding:2px 0'><strong>{e(v)}</strong></td></tr>" for k, v in facts)
        + "</table><table style='border-collapse:collapse;width:100%'><thead><tr>"
        + "".join(f"<th style='padding:6px 10px;border-bottom:2px solid #222;text-align:{a}'>{h}</th>"
                  for h, a in (("Product", "left"), ("Description", "left"), ("Quantity", "right"), ("Price", "right"), ("Total", "right")))
        + f"</tr></thead><tbody>{rows}</tbody><tfoot>{totals}</tfoot></table>"
        + (f"<p style='white-space:pre-wrap'><strong>Comments:</strong> {e(po['comments'])}</p>" if po.get("comments") else "")
        + "<p>The order is attached as a PDF.</p>"
        + "<p>Please reply to this email to confirm the order, or to tell us of anything you can't supply.</p>"
        f"<p>{e(company)}</p></div>")

    message = EmailMessage()
    message["Subject"] = default_subject(po, company)
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    domain = parseaddr(sender)[1].rsplit("@", 1)[-1] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content("\n".join(text))
    message.add_alternative(body, subtype="html")
    message.add_attachment(order_pdf(po, company, deliver_to=deliver_to, supplier_codes=supplier_codes, note=note),
                           maintype="application", subtype="pdf", filename=pdf_name(po))
    return message


def pdf_name(po):
    """The attachment's file name: the order's number, kept to plain characters."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(po.get("number") or "purchase-order"))[:60] + ".pdf"


def sender_for(config, user, company):
    """(From header, mailbox, reply-to) for an email sent now by `user` (the signed-in person:
    {"name", "email"}, or None without accounts). Through Microsoft 365 it's from their own
    mailbox (MAIL_FROM's, when nobody's signed in); through the mail server it's from MAIL_FROM
    with replies to them."""
    theirs = (user or {}).get("email") or ""
    theirs = theirs if EMAIL_RE.match(theirs) else ""
    shared = (config.get("MAIL_FROM") or "").strip()
    if mail_mode(config) == "microsoft":
        if theirs:
            return named(user.get("name") or "", theirs), theirs, None
        address = parseaddr(shared)[1]
        return (shared, address, None) if EMAIL_RE.match(address or "") else (f"{company} <no mailbox>", None, None)
    return (shared or f"{company} <not-set-up@example.invalid>", None,
            named(user.get("name") or "", theirs) if theirs else None)


def test_message(to, sender, company, how):
    """A short email to check sending works."""
    message = EmailMessage()
    message["Subject"] = f"{company}: Core can send email"
    message["From"], message["To"] = sender, to
    text = (f"This is a test from Core.\n\nPurchase orders emailed to vendors from Core are sent {how}.\n"
            "Nothing needs doing: if you're reading this, it works.\n")
    message.set_content(text)
    message.add_alternative("<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px'>"
                            + "".join(f"<p>{html.escape(p)}</p>" for p in text.strip().split("\n") if p) + "</div>",
                            subtype="html")
    return message


def named(name, address):
    """"Name <address>" for a header, the name cleaned of anything that could break it."""
    return formataddr((" ".join(re.sub(r"[\r\n\"<>]", " ", name or "").split())[:80], address))


class VendorEmails:
    """Which vendors take purchase orders by email, and the emails sent."""

    def __init__(self, db):
        self.db = db

    def settings(self):
        """{supplier code: {"send": bool, "email": str or None, "cc": str or None, "by", "at"}}."""
        return {r["supplier_code"]: {"send": bool(r["send_po"]), "email": r["email"], "cc": r["cc"],
                                     "by": r["updated_by"], "at": r["updated_at"]}
                for r in self.db.query("SELECT * FROM vendor_email_settings")}

    def setting(self, supplier_code):
        return self.settings().get(supplier_code)

    def save(self, supplier_code, send, email, cc, by):
        """Set a vendor's choice. Ticked needs an address; the addresses are checked either way."""
        to, copies = addresses(email, "The vendor's email"), addresses(cc, "The copy-to address")
        if send and not to:
            raise MailError("Give the address their orders go to.")
        values = (1 if send else 0, ", ".join(to) or None, ", ".join(copies) or None, by, utc_now(), supplier_code)
        if self.db.query("SELECT 1 FROM vendor_email_settings WHERE supplier_code = ?", (supplier_code,)):
            self.db.execute("UPDATE vendor_email_settings SET send_po = ?, email = ?, cc = ?, updated_by = ?,"
                            " updated_at = ? WHERE supplier_code = ?", values)
        else:
            self.db.execute("INSERT INTO vendor_email_settings (send_po, email, cc, updated_by, updated_at, supplier_code)"
                            " VALUES (?, ?, ?, ?, ?, ?)", values)

    def record(self, po, to, cc, subject, by):
        self.db.execute("INSERT INTO po_emails (order_number, order_guid, supplier_code, sent_to, cc, subject, sent_by,"
                        " sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (po["number"], po.get("guid"), po.get("supplier_code"), ", ".join(to), ", ".join(cc) or None,
                         subject, by, utc_now()))

    def sent(self, order_number):
        """The emails sent for an order, newest first: [{"sent_to", "cc", "sent_by", "sent_at"}]."""
        return self.db.query("SELECT * FROM po_emails WHERE order_number = ? ORDER BY id DESC", (order_number,))

    def last_sent(self):
        """{order number: when it was last emailed}, for the purchase list."""
        return {r["order_number"]: r["at"] for r in
                self.db.query("SELECT order_number, MAX(sent_at) AS at FROM po_emails GROUP BY order_number")}
