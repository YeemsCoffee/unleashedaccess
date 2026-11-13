"""Emailing purchase orders to vendors.

Unleashed's API has no "email this supplier their purchase orders" setting and sends no email,
so Core keeps the choice itself: for each vendor, whether they take orders by email, and at
which address (starting from the vendor's email in Unleashed). A placed purchase order to such
a vendor can be emailed from its page; who sent what, to whom and when is kept.

Mail goes out through an SMTP server named in the environment (SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY, MAIL_FROM); without one, nothing can be sent and the
pages say so. Recipients only ever come from the vendor's saved settings, never from the send
form, so the page can't be used to send mail to anyone else.
"""
import datetime as dt
import html
import re
import smtplib
import ssl
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


def can_send(config):
    return config.get("MAIL_OUTBOX") is not None or mail_settings(config) is not None


def send(config, message):
    """Send an EmailMessage. With MAIL_OUTBOX (a list, for tests and trying things out) it's
    kept there instead of sent."""
    outbox = config.get("MAIL_OUTBOX")
    if outbox is not None:
        outbox.append(message)
        return
    settings = mail_settings(config)
    if settings is None:
        raise MailError("Email isn't set up: an admin sets SMTP_HOST and MAIL_FROM (see the README).")
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


def default_subject(po, company):
    return f"Purchase order {po['number']} from {company}"


def build_message(po, to, cc, sender, company, note="", reply_to=None, deliver_to=None, supplier_codes=None):
    """The email of a purchase order: plain text, and the same as HTML. `supplier_codes`:
    {product code: the vendor's own code for it}, shown beside ours when known."""
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
    text += ["Please reply to this email to confirm the order, or to tell us of anything you can't supply.", "",
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
