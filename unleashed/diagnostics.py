"""Live data check: verifies the assumptions the reports make about an account's data.

Each check returns {"name", "status" (ok | warn | fail), "detail"}. Field-name
listings show keys only, never values, so they are safe to share.
"""
import datetime as dt

from .client import UnleashedError
from .reports import (INACTIVE_ADJUSTMENT_STATUSES, RECEIVED_PO_STATUSES, cost_line_keys,
                      month_start)


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
        statuses = ",".join(RECEIVED_PO_STATUSES)
        raw = client.get_all("PurchaseOrders", {"startDate": start.isoformat(),
                                                "endDate": today.isoformat(),
                                                "orderStatus": statuses})
        if not raw:
            return "warn", "No received purchase orders in the last 90 days to check"
        source = "the purchase order list"
        sample = raw
        if not any(cost_line_keys(p) for p in raw):
            sample = [client.get(f"PurchaseOrders/{p['Guid']}") for p in raw[:5] if p.get("Guid")]
            source = ("each purchase order's detail (the list leaves cost lines out, so the "
                      "dashboard fetches PO detail; first loads are a bit slower)")
        for p in sample:
            for key in cost_line_keys(p):
                if p.get(key):
                    fields["Purchase order cost line"] = sorted(p[key][0].keys())
                    break

        pos = reports.purchase_orders(start, today, status=statuses)
        found = [p for p in pos if p["cost_lines"]]
        if not found:
            if not any(cost_line_keys(p) for p in sample):
                return "warn", ("No cost-lines field found on purchase orders (checked the list and "
                                "the detail of 5 POs). See the purchase order field names below.")
            return "warn", "Cost lines field exists, but no received PO in 90 days has cost lines"
        unreadable = [p["number"] for p in found if any(c["field"] is None for c in p["cost_lines"])]
        if unreadable:
            return "warn", (f"Cost lines found, but no amount field could be read on "
                            f"{', '.join(unreadable[:3])}. See the cost line field names below.")
        used = sorted({c["field"] for p in found for c in p["cost_lines"]})
        total = sum(p["shipping"] for p in found)
        spot = max(found, key=lambda p: p["shipping"])
        return "ok", (f"Cost lines on {len(found)} of {len(pos)} received POs in 90 days "
                      f"(${total:,.2f}), read from {', '.join(used)} via {source}. Spot-check: "
                      f"{spot['number']} shows ${spot['shipping']:,.2f} in cost lines; compare it "
                      "with that PO in Unleashed.")

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
        by_status = {}
        for a in raw:
            st = a.get("Status") or "(no status)"
            entry = by_status.setdefault(st, {"lines": 0, "missing": 0, "examples": []})
            for l in a.get("StockAdjustmentLines") or []:
                entry["lines"] += 1
                if l.get("QuantityChange") is None and l.get("ValueChange") is None:
                    entry["missing"] += 1
                    if a["AdjustmentNumber"] not in entry["examples"]:
                        entry["examples"].append(a["AdjustmentNumber"])
                    if st.lower() not in INACTIVE_ADJUSTMENT_STATUSES:
                        fields.setdefault("Stock adjustment line (no change fields)", sorted(l.keys()))
                elif "Stock adjustment line" not in fields:
                    fields["Stock adjustment line"] = sorted(l.keys())
        if raw:
            fields["Stock adjustment"] = sorted(raw[0].keys())
        if not any(e["lines"] for e in by_status.values()):
            return "ok", "No stock adjustments in the last 90 days"

        parts = []
        for st, e in sorted(by_status.items()):
            part = f"{st}: {e['lines']} line(s), {e['missing']} without quantity/value change"
            if e["missing"]:
                part += f" (e.g. {', '.join(e['examples'][:4])})"
            parts.append(part)
        summary = "By adjustment status: " + "; ".join(parts) + "."
        active_missing = sum(e["missing"] for st, e in by_status.items()
                             if st.lower() not in INACTIVE_ADJUSTMENT_STATUSES)
        inactive_missing = sum(e["missing"] for st, e in by_status.items()
                               if st.lower() in INACTIVE_ADJUSTMENT_STATUSES)
        if active_missing:
            return "warn", (summary + f" {active_missing} line(s) on active adjustments have no "
                            "change fields; those show their new quantity and value instead, and "
                            "are left out of the COGS cross-check. The COGS figure itself is not "
                            "affected.")
        if inactive_missing:
            return "ok", (summary + " Every line without changes is on a deleted or parked "
                          "adjustment, which never moved stock; reports leave them out.")
        return "ok", summary

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
