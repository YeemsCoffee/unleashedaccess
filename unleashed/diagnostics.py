"""Live data check: verifies the assumptions the reports make about an account's data.

Each check returns {"name", "status" (ok | warn | fail), "detail"}. Field-name
listings show keys only, never values, so they are safe to share.
"""
import datetime as dt

from .client import UnleashedError
from .reports import RECEIVED_PO_STATUSES, month_start


def run_checks(reports):
    today = reports.today
    client = reports.client
    checks, fields = [], {}

    def check(name, fn):
        try:
            status, detail = fn()
        except UnleashedError as e:
            status, detail = "fail", str(e)
        checks.append({"name": name, "status": status, "detail": detail})
        return status

    def connection():
        whs = client.get_all("Warehouses")
        names = ", ".join(w.get("WarehouseName") or w.get("WarehouseCode") or "?" for w in whs)
        return "ok", f"{len(whs)} warehouse(s): {names}"

    if check("Connection and credentials", connection) == "fail":
        return checks, fields

    def products():
        items = client.get_all("Products")
        return ("ok" if items else "warn"), f"{len(items)} products"

    check("Products", products)

    def stock_now():
        soh = reports.stock_on_hand()
        rows = client.get("StockOnHand", {"pageSize": 1}, page=1).get("Items") or []
        if rows:
            fields["Stock on hand row"] = sorted(rows[0].keys())
        return "ok", f"{len(soh['rows'])} product rows, total value ${soh['total_value']:,.2f}"

    check("Stock on hand (now)", stock_now)

    def as_at_supported():
        past = today - dt.timedelta(days=45)
        now_rows = {r["product_code"]: (r["qty"], r["value"]) for r in reports.stock_on_hand()["rows"]}
        past_rows = {r["product_code"]: (r["qty"], r["value"])
                     for r in reports.stock_on_hand(as_at=past)["rows"]}
        if not now_rows:
            return "warn", "No stock on hand rows to compare"
        if now_rows == past_rows:
            return "warn", (f"Stock on hand as at {past} is identical to today for every product. "
                            "Either nothing moved in 45 days, or Unleashed is ignoring asAtDate — "
                            "in which case opening inventory (and COGS) would be wrong.")
        changed = sum(1 for k in now_rows if now_rows.get(k) != past_rows.get(k))
        return "ok", f"As-at stock differs from today for {changed} product(s) — historical stock works"

    check("Historical stock (asAtDate) for opening inventory", as_at_supported)

    def received_pos():
        start = today - dt.timedelta(days=90)
        raw = client.get_all("PurchaseOrders", {
            "startDate": start.isoformat(), "endDate": today.isoformat(),
            "orderStatus": ",".join(RECEIVED_PO_STATUSES)})
        if raw:
            fields["Purchase order"] = sorted(raw[0].keys())
            lines = raw[0].get("PurchaseOrderLines") or []
            if lines:
                fields["Purchase order line"] = sorted(lines[0].keys())
        if not raw:
            return "warn", "No received purchase orders in the last 90 days"
        no_date = [p["OrderNumber"] for p in raw if not (p.get("ReceivedDate") or p.get("CompletedDate"))]
        if no_date:
            return "warn", (f"{len(no_date)} of {len(raw)} received POs have no ReceivedDate or "
                            f"CompletedDate (e.g. {', '.join(no_date[:3])}); they can't be placed in a month")
        return "ok", f"{len(raw)} received POs in the last 90 days, all with a received date"

    check("Purchase orders: received dates", received_pos)

    def shipping():
        start = today - dt.timedelta(days=90)
        pos = reports.purchase_orders(start, today, status=",".join(RECEIVED_PO_STATUSES))
        if not pos:
            return "warn", "No received purchase orders to check"
        with_ship = [p for p in pos if p["shipping"]]
        if not with_ship:
            return "warn", ("No shipping found on any received PO in 90 days. If you pay freight, "
                            "it's recorded somewhere the dashboard isn't reading yet — check the "
                            "purchase order field names below.")
        total = sum(p["shipping"] for p in with_ship)
        return "ok", f"Shipping found on {len(with_ship)} of {len(pos)} POs (${total:,.2f} in 90 days)"

    check("Purchase orders: shipping / freight", shipping)

    def sales_cost():
        start = today - dt.timedelta(days=30)
        raw = client.get_all("SalesOrders", {"startDate": start.isoformat(),
                                             "endDate": today.isoformat(),
                                             "orderStatus": "Completed"})
        lines = [l for so in raw for l in so.get("SalesOrderLines") or []]
        if raw:
            fields["Sales order"] = sorted(raw[0].keys())
            if lines:
                fields["Sales order line"] = sorted(lines[0].keys())
        if not lines:
            return "warn", "No completed sales order lines in the last 30 days"
        costed = sum(1 for l in lines if l.get("AverageLandedPriceAtTimeOfSale") is not None)
        status = "ok" if costed == len(lines) else "warn"
        return status, (f"{len(raw)} completed sales orders; {costed} of {len(lines)} lines carry "
                        "AverageLandedPriceAtTimeOfSale (used for cost of sales and margin)")

    check("Sales orders: cost of sales", sales_cost)

    def adjustments():
        start = today - dt.timedelta(days=90)
        raw = client.get_all("StockAdjustments", {"startDate": start.isoformat(),
                                                  "endDate": today.isoformat()})
        lines = [l for a in raw for l in a.get("StockAdjustmentLines") or []]
        if raw:
            fields["Stock adjustment"] = sorted(raw[0].keys())
            if lines:
                fields["Stock adjustment line"] = sorted(lines[0].keys())
        if not lines:
            return "ok", "No stock adjustments in the last 90 days"
        if not any("QuantityChange" in l or "ValueChange" in l for l in lines):
            return "warn", (f"{len(lines)} adjustment lines, but none have QuantityChange/ValueChange, "
                            "so the Adjustments and Transactions pages will show 0 for them. "
                            "The COGS figure is not affected.")
        return "ok", f"{len(lines)} adjustment lines with quantity and value changes"

    check("Stock adjustments", adjustments)

    def cogs_cross_check():
        c = reports.cogs(month_start(today))
        recon = reports.cogs_reconciliation(month_start(today))
        diff = c["cogs"] - recon["total"]
        detail = (f"Formula: ${c['cogs']:,.2f} (opening ${c['opening']:,.2f} + purchases "
                  f"${c['purchases']:,.2f} − current ${c['closing']:,.2f}). Cost of sales + "
                  f"write-offs: ${recon['total']:,.2f}. Difference ${diff:,.2f}.")
        tolerance = max(5.0, abs(c["cogs"]) * 0.02)
        return ("ok" if abs(diff) <= tolerance else "warn"), detail

    check("COGS this month vs cost of sales", cogs_cross_check)
    return checks, fields
