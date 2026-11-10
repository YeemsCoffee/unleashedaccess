"""Report enquiries built purely on the Unleashed API.

Each method mirrors an Unleashed report/enquiry screen and gets all of its
data through UnleashedClient — so pointing the client at the real API
reproduces the same reports from live data.
"""
import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from zoneinfo import ZoneInfo

from .client import UnleashedError, business_timezone, has_time, parse_date, parse_moment
from .store import transfer_moved

RECEIVED_PO_STATUSES = ("Receipted", "Costed", "Complete")
OPEN_PO_STATUSES = ("Parked", "Placed")
PO_STATUSES = ("Parked", "Placed", "Receipted", "Costed", "Complete", "Deleted")
SO_STATUSES = ("Parked", "Placed", "Backordered", "Completed", "Deleted")
# Adjustments in these states never moved stock, so reports leave them out.
INACTIVE_ADJUSTMENT_STATUSES = ("deleted", "parked")
# Where a PO cost line's amount may live, most specific (base currency, ex tax) first.
COST_LINE_AMOUNT_FIELDS = ("BCCost", "BCSubTotal", "BCLineTotal", "BCAmount", "BCTotal", "SubTotal",
                           "LineTotal", "Amount", "Total", "Cost", "Value")
# Products whose purchase order lines are charges (counted as shipping, not goods), besides
# the ones chosen in Settings -> COGS ("charges").
SHIPPING_CODES = ("FREIGHT", "SHIPPING", "DELIVERY", "FUEL", "FUEL-SURCHARGE", "SURCHARGE")
# Where Unleashed keeps a purchase order's cost lines (freight and the like): not in the order
# itself but from GET /PurchaseOrders/{guid}/Costs, kept on the order under this key.
PO_COSTS_KEY = "PurchaseOrderCosts"
COMPLETED_ASSEMBLY_STATUSES = ("completed", "complete")
COMPLETED_RETURN_STATUSES = ("completed", "complete")
# Assemblies in these states never moved stock, so reports leave them out.
INACTIVE_ASSEMBLY_STATUSES = ("deleted",)
# Where an assembly line's cost may live. Without one, components are costed at their
# current average cost and the order is marked as estimated.
ASSEMBLY_LINE_UNIT_COST_FIELDS = ("AverageLandedPriceAtTimeOfAssembly", "AverageLandedPrice",
                                  "UnitCost", "AverageCost", "AvgCost", "Cost")
ASSEMBLY_LINE_TOTAL_FIELDS = ("TotalCost", "LineTotal", "LineCost", "TotalLineCost")
# Without the stored copy (which finds documents by when they moved stock), a sales order
# or production order completed in a month is looked for among those dated this long before.
SALE_LOOKBACK_DAYS = 120
# What Unleashed's stock "as at" a date can be: the stock at the end of that day, or its
# start, in the business's time zone, UTC or New Zealand time (Unleashed's home). key ->
# (label, time zone (None: the business's), "end" or "start").
STOCK_CUTOFFS = {
    "end": ("The end of that day", None, "end"),
    "start": ("The start of that day", None, "start"),
    "utc_end": ("The end of that day in UTC", "UTC", "end"),
    "utc_start": ("The start of that day in UTC", "UTC", "start"),
    "nz_end": ("The end of that day in New Zealand time", "Pacific/Auckland", "end"),
    "nz_start": ("The start of that day in New Zealand time", "Pacific/Auckland", "start"),
}


def stock_cutoff(key, day):
    """The moment Unleashed's stock "as at" `day` shows, read as `key` (STOCK_CUTOFFS)."""
    _, zone, part = STOCK_CUTOFFS.get(key) or STOCK_CUTOFFS["end"]
    tz = business_timezone() if zone is None else ZoneInfo(zone)
    return dt.datetime.combine(day + dt.timedelta(days=1 if part == "end" else 0), dt.time(0), tzinfo=tz)


def end_of_day(day):
    """The moment `day` ends in the business's time zone."""
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=business_timezone())


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return nxt - dt.timedelta(days=1)


def add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def _warehouse_code(value):
    """A document's warehouse code, whether Unleashed gives the warehouse or just its code."""
    return value.get("WarehouseCode") if isinstance(value, dict) else value or None


class Reports:
    def __init__(self, client, today_fn=dt.date.today,
                 shipping_codes=SHIPPING_CODES, max_workers=1, cogs_settings_fn=None):
        self.client = client
        self.today_fn = today_fn
        self.shipping_codes = {c.upper() for c in shipping_codes}
        self.max_workers = max_workers
        self._details_have_cost_lines = None
        # {"warehouses": codes whose stock on hand COGS counts (None: every warehouse), "pending":
        # ones chosen but not ready yet, "stores": [customer codes]} (store.cogs_settings)
        self.cogs_settings_fn = cogs_settings_fn or (
            lambda: {"warehouses": None, "pending": None, "stores": [], "stock_as_at": "end"})

    def cogs_settings(self):
        return self.cogs_settings_fn()

    def charge_codes(self):
        """Product codes whose purchase order lines are charges (freight, fuel surcharge)."""
        return self.shipping_codes | {c.upper() for c in self.cogs_settings().get("charges") or []}

    def _map(self, fn, args):
        """fn over args, concurrently when max_workers > 1 (independent API reads)."""
        args = list(args)
        if self.max_workers <= 1 or len(args) <= 1:
            return [fn(a) for a in args]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(fn, args))

    @property
    def today(self):
        return self.today_fn()

    # ------------------------------------------------------------ reference

    def products(self):
        return self.client.get_all("Products")

    def product_groups(self):
        return sorted(g["GroupName"] for g in self.client.get_all("ProductGroups"))

    def suppliers(self):
        return self.client.get_all("Suppliers")

    def customers(self):
        return self.client.get_all("Customers")

    def warehouses(self):
        return self.client.get_all("Warehouses")

    def batch_tracked(self, product_code):
        """Whether Unleashed tracks the product by batch number (False if it doesn't say)."""
        return any(p.get("ProductCode") == product_code and p.get("IsBatchTracked")
                   for p in self.client.get_all("Products", {"productCode": product_code}))

    # ------------------------------------------------------------ stock

    def stock_on_hand(self, as_at=None, warehouse=None, group=None, product=None):
        """Stock On Hand enquiry (optionally as at a past date)."""
        items = self.client.get_all("StockOnHand", {
            "asAtDate": as_at.isoformat() if as_at else None,
            "warehouseCode": warehouse, "productCode": product,
        })
        rows = [{
            "product_code": i["ProductCode"],
            "description": i.get("ProductDescription"),
            "group": i.get("ProductGroupName") or "",
            "warehouse": i.get("WarehouseCode") or i.get("Warehouse"),
            "qty": i.get("QtyOnHand") or 0,
            "allocated": i.get("AllocatedQty") or 0,
            "available": i.get("AvailableQty") or 0,
            "on_purchase": i.get("OnPurchase") or 0,
            "avg_cost": i.get("AvgCost") or 0,
            "value": i.get("TotalCost") or 0,
            "days_since_sale": i.get("DaysSinceLastSale"),
        } for i in items]
        if group:
            rows = [r for r in rows if r["group"] == group]
        rows.sort(key=lambda r: (r["group"], r["product_code"]))
        return {"as_at": as_at, "rows": rows,
                "total_qty": sum(r["qty"] for r in rows),
                "total_value": sum(r["value"] for r in rows)}

    def inventory_value(self, as_at=None, warehouse=None):
        return self.stock_on_hand(as_at=as_at, warehouse=warehouse)["total_value"]

    def stock_valuation(self, as_at=None):
        """Stock Valuation report, grouped by product group."""
        soh = self.stock_on_hand(as_at=as_at)
        groups = defaultdict(lambda: {"qty": 0, "value": 0, "products": 0})
        for r in soh["rows"]:
            g = groups[r["group"] or "(no group)"]
            g["qty"] += r["qty"]
            g["value"] += r["value"]
            g["products"] += 1
        total = soh["total_value"] or 1
        rows = [{"group": k, **v, "share": v["value"] / total} for k, v in groups.items()]
        rows.sort(key=lambda r: -r["value"])
        return {"as_at": as_at, "rows": rows, "total_value": soh["total_value"],
                "detail": soh["rows"]}

    # ------------------------------------------------------------ purchases

    def purchase_orders(self, start=None, end=None, status=None, supplier=None):
        items = self.client.get_all("PurchaseOrders", {
            "startDate": start.isoformat() if start else None,
            "endDate": end.isoformat() if end else None,
            "orderStatus": status, "supplierCode": supplier,
        })
        charges = self.charge_codes()  # once: it reads the settings
        return [self._po(i, charges) for i in self._with_cost_lines(items)]

    def purchase_order(self, order_number):
        items = self.client.get_all("PurchaseOrders", {"orderNumber": order_number})
        exact = [i for i in items if i["OrderNumber"] == order_number]
        return self._po(self._with_cost_lines(exact)[0]) if exact else None

    def _with_cost_lines(self, items):
        """Received orders with their cost lines. The stored copy has them (the sync fetches
        them); read straight from Unleashed, they're fetched here."""
        if not items or getattr(self.client, "finds_by_completion", False) \
                or any(cost_line_keys(i) for i in items):
            return items
        return with_po_costs(self.client, items, self._map)

    def purchase_enquiry(self, start, end, status=None, supplier=None):
        orders = self.purchase_orders(start, end, status, supplier)
        orders = [o for o in orders if o["status"] != "Deleted" or status == "Deleted"]
        by_supplier = defaultdict(lambda: {"orders": 0, "goods": 0, "shipping": 0, "total": 0})
        by_product = defaultdict(lambda: {"description": "", "qty": 0, "value": 0})
        for o in orders:
            s = by_supplier[o["supplier_name"]]
            s["orders"] += 1
            s["goods"] += o["goods"]
            s["shipping"] += o["shipping"]
            s["total"] += o["goods"] + o["shipping"]
            for l in o["lines"]:
                p = by_product[l["product_code"]]
                p["description"] = l["description"]
                p["qty"] += l["qty"]
                p["value"] += l["line_total"]
        return {
            "orders": sorted(orders, key=lambda o: (o["order_date"], o["number"]), reverse=True),
            "by_supplier": sorted(({"supplier": k, **v} for k, v in by_supplier.items()),
                                  key=lambda r: -r["total"]),
            "by_product": sorted(({"product_code": k, **v} for k, v in by_product.items()),
                                 key=lambda r: -r["value"]),
            "goods": sum(o["goods"] for o in orders),
            "shipping": sum(o["shipping"] for o in orders),
            "total": sum(o["goods"] + o["shipping"] for o in orders),
        }

    def received_purchases(self, start, end):
        """POs whose stock was received between start and end (inclusive)."""
        lookback = start - dt.timedelta(days=180)
        orders = self.purchase_orders(lookback, end, status=",".join(RECEIVED_PO_STATUSES))
        return sorted((o for o in orders
                       if o["received_date"] and start <= o["received_date"] <= end),
                      key=lambda o: (o["received_date"], o["number"]))

    def _po(self, i, charges=None):
        charges = self.charge_codes() if charges is None else charges
        goods, lines, cost_lines = 0.0, [], []
        for l in i.get("PurchaseOrderLines", []):
            prod = l.get("Product") or {}
            code = prod.get("ProductCode", "")
            total = _first(l.get("BCSubTotal"), l.get("BCLineTotal"), l.get("LineTotal"))
            if code.upper() in charges:
                cost_lines.append({"description": l.get("Comments") or prod.get("ProductDescription") or code,
                                   "supplier": None, "amount": total, "field": "PurchaseOrderLines",
                                   "guid": l.get("Guid"), "product_code": code, "comments": l.get("Comments"),
                                   "qty": l.get("OrderQuantity") or 1,
                                   "unit_price": _first(l.get("BCUnitPrice"), l.get("UnitPrice"))})
                continue
            goods += total
            lines.append({"guid": l.get("Guid"), "product_code": code, "description": prod.get("ProductDescription"),
                          "qty": l.get("OrderQuantity") or 0,
                          "received_qty": l.get("ReceiptQuantity"),
                          "unit_price": _first(l.get("BCUnitPrice"), l.get("UnitPrice")),
                          "line_total": total})
        for key in cost_line_keys(i):
            for c in i.get(key) or []:
                field, amount = cost_line_amount(c)
                cost_lines.append({
                    "description": c.get("Description") or c.get("Reference") or c.get("Comments") or "Additional cost",
                    "supplier": (c.get("Supplier") or {}).get("SupplierName"),
                    "amount": amount, "field": f"{key}.{field}" if field else None})
        shipping = sum(c["amount"] for c in cost_lines)
        supplier = i.get("Supplier") or {}
        return {
            "guid": i.get("Guid"), "number": i["OrderNumber"], "status": i.get("OrderStatus"),
            "order_date": parse_date(i.get("OrderDate")),
            "required_date": parse_date(i.get("DeliveryDate") or i.get("RequiredDate")),
            "reference": i.get("SupplierRef"), "comments": i.get("Comments"),
            "received_date": parse_date(i.get("ReceivedDate") or i.get("CompletedDate")),
            "received_at": parse_moment(i.get("ReceivedDate") or i.get("CompletedDate")),
            "received_timed": has_time(i.get("ReceivedDate") or i.get("CompletedDate")),
            "completed_date": parse_date(i.get("CompletedDate")),
            "supplier_code": supplier.get("SupplierCode"),
            "supplier_name": supplier.get("SupplierName"),
            "warehouse": _warehouse_code(i.get("Warehouse") or i.get("DeliveryWarehouse")),
            "goods": goods, "shipping": shipping, "tax": _first(i.get("BCTaxTotal"), i.get("TaxTotal")),
            "total": goods + shipping, "lines": lines, "cost_lines": cost_lines,
        }

    # ------------------------------------------------------------ sales

    def sales_orders(self, start=None, end=None, status=None, customer=None):
        items = self.client.get_all("SalesOrders", {
            "startDate": start.isoformat() if start else None,
            "endDate": end.isoformat() if end else None,
            "orderStatus": status, "customerCode": customer,
        })
        return [self._so(i) for i in items]

    def sales_order(self, order_number):
        items = self.client.get_all("SalesOrders", {"orderNumber": order_number})
        exact = [i for i in items if i["OrderNumber"] == order_number]
        return self._so(exact[0]) if exact else None

    def sales_enquiry(self, start, end, status=None, customer=None):
        orders = self.sales_orders(start, end, status, customer)
        orders = [o for o in orders if o["status"] != "Deleted" or status == "Deleted"]
        by_product = defaultdict(lambda: {"description": "", "qty": 0, "revenue": 0, "cost": 0})
        by_customer = defaultdict(lambda: {"orders": 0, "revenue": 0, "cost": 0})
        for o in orders:
            c = by_customer[o["customer_name"]]
            c["orders"] += 1
            c["revenue"] += o["subtotal"]
            c["cost"] += o["cost"]
            for l in o["lines"]:
                p = by_product[l["product_code"]]
                p["description"] = l["description"]
                p["qty"] += l["qty"]
                p["revenue"] += l["line_total"]
                p["cost"] += l["cost"]
        revenue = sum(o["subtotal"] for o in orders)
        cost = sum(o["cost"] for o in orders)

        def with_margin(rows):
            for r in rows:
                r["margin"] = r["revenue"] - r["cost"]
                r["margin_pct"] = r["margin"] / r["revenue"] if r["revenue"] else None
            return rows

        return {
            "orders": sorted(orders, key=lambda o: (o["order_date"], o["number"]), reverse=True),
            "by_product": sorted(with_margin([{"product_code": k, **v} for k, v in by_product.items()]),
                                 key=lambda r: -r["revenue"]),
            "by_customer": sorted(with_margin([{"customer": k, **v} for k, v in by_customer.items()]),
                                  key=lambda r: -r["revenue"]),
            "revenue": revenue, "cost": cost, "margin": revenue - cost,
            "margin_pct": (revenue - cost) / revenue if revenue else None,
        }

    def _so(self, i):
        lines = []
        for l in i.get("SalesOrderLines", []):
            prod = l.get("Product") or {}
            qty = l.get("OrderQuantity") or 0
            unit_cost = l.get("AverageLandedPriceAtTimeOfSale") or 0
            lines.append({"guid": l.get("Guid"), "product_code": prod.get("ProductCode"),
                          "description": prod.get("ProductDescription"),
                          "batches": [{"number": b.get("Number"), "qty": b.get("Quantity")}
                                      for b in l.get("BatchNumbers") or []],
                          "qty": qty, "unit_price": _first(l.get("BCUnitPrice"), l.get("UnitPrice")),
                          "line_total": _first(l.get("BCLineTotal"), l.get("LineTotal")),
                          "unit_cost": unit_cost, "cost": qty * unit_cost})
        customer = i.get("Customer") or {}
        subtotal = _first(i.get("BCSubTotal"), i.get("SubTotal"))
        cost = sum(l["cost"] for l in lines)
        return {
            "guid": i.get("Guid"), "number": i["OrderNumber"], "status": i.get("OrderStatus"),
            "order_date": parse_date(i.get("OrderDate")),
            "completed_date": parse_date(i.get("CompletedDate")),
            "completed_at": parse_moment(i.get("CompletedDate") or i.get("OrderDate")),
            "completed_timed": has_time(i.get("CompletedDate")),
            "required_date": parse_date(i.get("RequiredDate")),
            "reference": i.get("CustomerRef"), "comments": i.get("Comments"),
            "warehouse": _warehouse_code(i.get("Warehouse")),
            "customer_code": customer.get("CustomerCode"),
            "customer_name": customer.get("CustomerName"),
            "subtotal": subtotal, "tax": _first(i.get("BCTaxTotal"), i.get("TaxTotal")),
            "total": _first(i.get("BCTotal"), i.get("Total")),
            "cost": cost, "margin": subtotal - cost, "lines": lines,
        }

    # ------------------------------------------------------------ returns

    def credit_notes(self, start=None, end=None, status=None, customer=None):
        """Credit notes (customer returns and credits) dated start..end."""
        items = self.client.get_all("CreditNotes", {
            "startDate": start.isoformat() if start else None, "endDate": end.isoformat() if end else None,
            "creditStatus": status, "customerCode": customer})
        credits = [self._credit(i) for i in items]
        return [c for c in credits if (not start or (c["date"] and c["date"] >= start))
                and (not end or (c["date"] and c["date"] <= end))]

    def credit_note(self, number):
        items = self.client.get_all("CreditNotes", {"creditNumber": number})
        exact = [i for i in items if i.get("CreditNumber") == number]
        return self._credit(exact[0]) if exact else None

    def _credit(self, i):
        lines = []
        for l in i.get("CreditLines") or []:
            prod = l.get("Product") or {}
            qty = l.get("CreditQuantity") or 0
            back = bool(l.get("Return") if l.get("Return") is not None else l.get("ReturnToStock"))
            unit_cost = l.get("AverageLandedPriceAtTimeOfSale") or 0
            batches = l.get("BatchNumbers") or l.get("PendingBatchNumbers") or []
            lines.append({"guid": l.get("Guid"), "product_code": prod.get("ProductCode"),
                          "description": prod.get("ProductDescription"), "qty": qty,
                          "price": l.get("CreditPrice") or 0,
                          "line_total": _first(l.get("BCLineTotal"), l.get("LineTotal")),
                          "reason": l.get("Reason"), "returned": back, "unit_cost": unit_cost,
                          "cost": qty * unit_cost if back else 0.0, "comments": l.get("Comments"),
                          "batches": [{"number": b.get("Number"), "qty": b.get("Quantity")} for b in batches]})
        status = i.get("CreditStatus") or i.get("Status") or ""
        customer = i.get("Customer") or {}
        return {
            "guid": i.get("Guid"), "number": i.get("CreditNumber"), "status": status,
            "completed": status.lower() in COMPLETED_RETURN_STATUSES,
            "date": parse_date(i.get("CreditDate")), "at": parse_moment(i.get("CreditDate")),
            "timed": has_time(i.get("CreditDate")),
            "customer_code": customer.get("CustomerCode"), "customer_name": customer.get("CustomerName"),
            "warehouse": _warehouse_code(i.get("Warehouse")), "reference": i.get("Reference"),
            "comments": i.get("Comments"), "sales_order": (i.get("SalesOrder") or {}).get("OrderNumber"),
            "subtotal": _first(i.get("BCSubTotal"), i.get("SubTotal")), "tax": _first(i.get("BCTaxTotal"), i.get("TaxTotal")),
            "total": _first(i.get("BCTotal"), i.get("Total")), "lines": lines,
            "cost": sum(l["cost"] for l in lines),
        }

    def supplier_returns(self, start=None, end=None, status=None, supplier=None):
        """Supplier returns (stock sent back to suppliers) dated start..end."""
        items = self.client.get_all("SupplierReturns", {
            "startDate": start.isoformat() if start else None, "endDate": end.isoformat() if end else None,
            "supplierReturnStatus": status, "supplierCode": supplier})
        returns = [self._supplier_return(i) for i in items]
        return [r for r in returns if (not start or (r["date"] and r["date"] >= start))
                and (not end or (r["date"] and r["date"] <= end))]

    def supplier_return(self, number):
        items = self.client.get_all("SupplierReturns", {"supplierReturnNumber": number})
        exact = [i for i in items if i.get("SupplierReturnNumber") == number]
        return self._supplier_return(exact[0]) if exact else None

    def _supplier_return(self, i):
        lines = []
        for l in i.get("SupplierReturnLines") or []:
            prod = l.get("Product") or {}
            qty = _first(l.get("ReturnQuantity"), l.get("PurchaseUnitReturnQuantity")) or 0
            before, avg_before, avg_after = (l.get("StockOnHandBeforeReturn"), l.get("AverageLandedPriceBeforeReturn"),
                                             l.get("AverageLandedPriceAfterReturn"))
            if None not in (before, avg_before, avg_after):  # what left stock, as Unleashed recorded it
                value = before * avg_before - (before - qty) * avg_after
            else:
                value = qty * (l.get("ReturnCost") or l.get("ReturnPrice") or 0)
            lines.append({"guid": l.get("Guid"), "product_code": prod.get("ProductCode"),
                          "description": prod.get("ProductDescription"), "ordered": l.get("OrderQuantity") or 0,
                          "qty": qty, "price": l.get("ReturnPrice") or 0, "order_price": l.get("OrderPrice"),
                          "reason": l.get("ReturnReason"), "value": value if qty else 0.0,
                          "comments": l.get("Comments")})
        status = i.get("Status") or ""
        supplier = i.get("Supplier") or {}
        batches = {(pb.get("Product") or {}).get("ProductCode"): [{"number": b.get("BatchNumber"), "qty": b.get("Quantity")}
                                                                   for b in pb.get("Batches") or []]
                   for pb in i.get("ProductBatches") or []}
        return {
            "guid": i.get("Guid"), "number": i.get("SupplierReturnNumber"), "status": status,
            "completed": status.lower() in COMPLETED_RETURN_STATUSES,
            "date": parse_date(i.get("ReturnDate")), "at": parse_moment(i.get("ReturnDate")),
            "timed": has_time(i.get("ReturnDate")),
            "supplier_code": supplier.get("SupplierCode"), "supplier_name": supplier.get("SupplierName"),
            "purchase_order": (i.get("PurchaseOrder") or {}).get("OrderNumber"),
            "warehouse": _warehouse_code(i.get("Warehouse")), "reference": i.get("SupplierRef"),
            "comments": i.get("Comments"), "subtotal": _first(i.get("BaseSubTotal"), i.get("SubTotal")),
            "total": _first(i.get("BaseTotal"), i.get("Total")), "lines": lines, "batches": batches,
            "value": sum(l["value"] for l in lines),
        }

    def _completed_returns(self, endpoint, start, through, warehouses=None):
        """Credit notes or supplier returns completed (dated) from `start` to `through`, in one
        of `warehouses` when some are given."""
        status = "Completed,Complete"
        if getattr(self.client, "finds_by_completion", False):
            status_key = "creditStatus" if endpoint == "CreditNotes" else "supplierReturnStatus"
            items = self.client.get_all(endpoint, {status_key: status, "completedStartDate": start.isoformat(),
                                                   "completedEndDate": through.isoformat()})
            docs = [(self._credit if endpoint == "CreditNotes" else self._supplier_return)(i) for i in items]
        elif endpoint == "CreditNotes":
            docs = self.credit_notes(start, through)
        else:
            docs = self.supplier_returns(start, through)
        return [d for d in docs if d["completed"] and d["date"] and start <= d["date"] <= through
                and (not warehouses or not d["warehouse"] or d["warehouse"] in warehouses)]

    # ------------------------------------------------------------ adjustments

    def stock_adjustments(self, start, end):
        items = self.client.get_all("StockAdjustments", {
            "startDate": start.isoformat(), "endDate": end.isoformat()})
        rows = []
        for a in items:
            if (a.get("Status") or "").lower() in INACTIVE_ADJUSTMENT_STATUSES:
                continue
            for l in a.get("StockAdjustmentLines") or []:
                prod = l.get("Product") or {}
                rows.append({"number": a["AdjustmentNumber"], "guid": a.get("Guid"),
                             "date": parse_date(a.get("AdjustmentDate")),
                             "at": parse_moment(a.get("AdjustmentDate")),
                             "timed": has_time(a.get("AdjustmentDate")),
                             "warehouse": _warehouse_code(a.get("Warehouse")),
                             "reason": a.get("AdjustmentReason"), "status": a.get("Status"),
                             "product_code": prod.get("ProductCode"),
                             "description": prod.get("ProductDescription"),
                             # Unleashed's NewQuantity and NewActualValue are the signed change
                             # ("Lost 2 somewhere": -2, -18.884), not the totals after it.
                             "qty_change": l.get("NewQuantity"),
                             "value_change": l.get("NewActualValue"),
                             "comments": l.get("Comments")})
        rows.sort(key=lambda r: (r["date"], r["number"]), reverse=True)
        return {"rows": rows,
                "value_change": sum(r["value_change"] or 0 for r in rows),
                "missing_change": sum(1 for r in rows if r["value_change"] is None)}

    def stock_adjustment(self, number):
        """One adjustment, any status: {"number", "guid", "date", "warehouse", "reason", "status",
        "lines": [{"product_code", "description", "qty_change", "value_change", "comments",
        "batches"}]}, or None."""
        items = self.client.get_all("StockAdjustments", {"adjustmentNumber": number})
        a = next((i for i in items if i.get("AdjustmentNumber") == number), None)
        if a is None:
            return None
        return {"number": number, "guid": a.get("Guid"), "date": parse_date(a.get("AdjustmentDate")),
                "warehouse": _warehouse_code(a.get("Warehouse")), "reason": a.get("AdjustmentReason"),
                "status": a.get("Status"), "created_by": a.get("CreatedBy"),
                "lines": [{"product_code": (l.get("Product") or {}).get("ProductCode"),
                           "description": (l.get("Product") or {}).get("ProductDescription"),
                           "qty_change": l.get("NewQuantity"), "value_change": l.get("NewActualValue"),
                           "comments": l.get("Comments"),
                           "batches": [{"number": b.get("Number"), "qty": b.get("Quantity")}
                                       for b in l.get("BatchNumbers") or []]}
                          for l in a.get("StockAdjustmentLines") or []]}

    # ------------------------------------------------------------ transfers

    def transfers(self, start=None, end=None, status=None):
        """Warehouse stock transfers, newest first: those that moved stock (completed) from
        `start` to `end`, or with `status` (e.g. "Parked,Placed") any date."""
        params = {"transferStatus": status or "Parked,Placed,Completed"}
        if start and not status and getattr(self.client, "finds_by_completion", False):
            params.update({"completedStartDate": start.isoformat(), "completedEndDate": (end or self.today).isoformat()})
        try:
            items = self.client.get_all("WarehouseStockTransfers", params)
        except UnleashedError as e:
            if e.status_code in (401, 403, 404):  # this account (or API user) can't see transfers
                return []
            raise
        found = [self._transfer(i) for i in items]
        if start or end:
            found = [t for t in found if t["date"] and (not start or t["date"] >= start) and (not end or t["date"] <= end)]
        return sorted(found, key=lambda t: (t["date"] or dt.date.min, t["number"] or ""), reverse=True)

    def transfer(self, number):
        items = self.client.get_all("WarehouseStockTransfers", {"transferStatus": "Parked,Placed,Completed"})
        found = next((i for i in items if i.get("TransferOrderNumber") == number or i.get("Guid") == number), None)
        return self._transfer(found) if found else None

    def _transfer(self, i):
        status = i.get("TransferStatus") or ""
        moved = transfer_moved(i)
        done = status.lower() == "completed"
        lines = []
        for l in i.get("TransferDetails") or []:
            prod = l.get("Product") or {}
            cost = l.get("AverageLandedPriceAtTimeOfTransfer")
            qty = float(l.get("TransferQuantity") or 0)
            lines.append({"guid": l.get("Guid"), "product_code": prod.get("ProductCode"),
                          "description": prod.get("ProductDescription"), "qty": qty, "comments": l.get("Comments"),
                          "unit_cost": cost, "value": qty * cost if cost is not None else None})
        source, destination = i.get("SourceWarehouse") or {}, i.get("DestinationWarehouse") or {}
        return {"guid": i.get("Guid"), "number": i.get("TransferOrderNumber"), "status": status, "completed": done,
                "date": parse_date(moved) if moved else None,
                "at": parse_moment(moved) if done and moved else None,
                "timed": bool(done and moved and has_time(moved)),
                "order_date": parse_date(i.get("OrderDate")) if i.get("OrderDate") else None,
                "source": source.get("WarehouseCode"), "source_name": source.get("WarehouseName") or source.get("WarehouseCode"),
                "destination": destination.get("WarehouseCode"),
                "destination_name": destination.get("WarehouseName") or destination.get("WarehouseCode"),
                "comments": i.get("Comments"), "lines": lines,
                "value": sum(l["value"] or 0 for l in lines)}

    def _transferred(self, start, through, warehouses):
        """Stock value transferred into `warehouses` from other warehouses, start..through
        (none counts when every warehouse is counted: it only moved)."""
        if not warehouses or through < start:
            return 0.0, []
        moved = [t for t in self.transfers(start, through) if t["completed"]
                 and t["destination"] in warehouses and t["source"] not in warehouses]
        return sum(t["value"] for t in moved), moved

    # ------------------------------------------------------------ production

    def unit_costs(self):
        """{product code: current average cost}, or its default purchase price when none is in
        stock: for costing BOMs, and assembly lines that carry no cost."""
        costs = {p.get("ProductCode"): p.get("DefaultPurchasePrice") or 0 for p in self.products()}
        for r in self.stock_on_hand()["rows"]:
            if r["avg_cost"]:
                costs[r["product_code"]] = r["avg_cost"]
        return costs

    def assemblies(self, start=None, end=None, product=None):
        """Assemblies (production orders) dated start..end, any status."""
        items = self.client.get_all("Assemblies", {
            "startDate": start.isoformat() if start else None,
            "endDate": end.isoformat() if end else None, "productCode": product})
        costs = self.unit_costs() if items else {}
        orders = [self._assembly(i, costs) for i in items]
        # Also filtered here, in case the API ignores a filter.
        return [o for o in orders
                if (not start or (o["date"] and o["date"] >= start))
                and (not end or (o["date"] and o["date"] <= end))
                and (not product or o["product_code"] == product)]

    def assembly(self, number):
        items = self.client.get_all("Assemblies", {"assemblyNumber": number})
        exact = [i for i in items if i.get("AssemblyNumber") == number]
        return self._assembly(exact[0], self.unit_costs()) if exact else None

    def _assembly(self, i, costs):
        status = i.get("AssemblyStatus") or i.get("Status") or ""
        completed = status.lower() in COMPLETED_ASSEMBLY_STATUSES
        lines = []
        for l in i.get("AssemblyLines") or []:
            prod = l.get("Product") or {}
            code = prod.get("ProductCode")
            used, wastage = l.get("Quantity") or 0, l.get("WastageQuantity") or 0
            consumed = used + wastage
            unit = _field(l, ASSEMBLY_LINE_UNIT_COST_FIELDS)
            total = _field(l, ASSEMBLY_LINE_TOTAL_FIELDS)
            estimated = unit is None and total is None
            if unit is None:
                unit = total / consumed if total is not None and consumed else costs.get(code, 0)
            lines.append({"product_code": code, "description": prod.get("ProductDescription"),
                          "qty": used, "wastage": wastage, "consumed": consumed,
                          "unit_cost": unit, "cost": consumed * unit, "estimated": estimated})
        prod = i.get("Product") or {}
        qty = i.get("Quantity") or 0
        cost = sum(l["cost"] for l in lines)
        date = parse_date(i.get("AssemblyDate"))
        source = i.get("SourceWarehouse") or i.get("Warehouse") or {}
        destination = i.get("DestinationWarehouse") or i.get("Warehouse") or {}
        # The finished product's batches: created ones, or on a parked assembly, ones entered
        # to be created when it's completed.
        made_batches, pending = _batches(i.get("BatchNumbers")), _batches(i.get("PendingBatchNumbers"))
        return {
            "guid": i.get("Guid"), "number": i.get("AssemblyNumber"), "status": status,
            "completed": completed,
            "open": not completed and status.lower() not in INACTIVE_ASSEMBLY_STATUSES,
            "product_code": prod.get("ProductCode"), "description": prod.get("ProductDescription"),
            "qty": qty, "date": date, "due": parse_date(i.get("AssembleBy")) or date,
            "completed_date": (parse_date(i.get("CompletedDate")) or date) if completed else None,
            "completed_at": parse_moment(i.get("CompletedDate") or i.get("AssemblyDate")) if completed else None,
            "completed_timed": has_time(i.get("CompletedDate")),
            "from_warehouse": source.get("WarehouseName") or source.get("WarehouseCode"),
            "to_warehouse": destination.get("WarehouseName") or destination.get("WarehouseCode"),
            "batches": made_batches or pending, "batches_pending": not made_batches and bool(pending),
            "comments": i.get("Comments"), "lines": lines, "cost": cost,
            "unit_cost": cost / qty if qty else 0, "estimated": any(l["estimated"] for l in lines),
            # What Unleashed adds to the made product's value beyond its components (labour etc.).
            "supplier_costs": _first(i.get("SupplierCostsSubTotal"),
                                     sum(_first(c.get("BCSubTotal"), c.get("SubTotal"), c.get("Amount"), 0) or 0
                                         for c in i.get("SupplierCosts") or [])) or 0,
        }

    def bills_of_materials(self):
        items = self.client.get_all("BillOfMaterials")
        costs = self.unit_costs() if items else {}
        return [self._bom(b, costs) for b in items]

    def _bom(self, b, costs):
        lines = []
        for l in b.get("BillOfMaterialsLines") or []:
            prod = l.get("Product") or {}
            code = prod.get("ProductCode")
            qty, wastage = l.get("Quantity") or 0, l.get("WastageQuantity") or 0
            unit = costs.get(code) or (l["TotalLineCost"] / qty if l.get("TotalLineCost") and qty else 0)
            lines.append({"product_code": code, "description": prod.get("ProductDescription"),
                          "qty": qty, "wastage": wastage, "per_unit": qty + wastage,
                          "unit_cost": unit, "cost": (qty + wastage) * unit})
        prod = b.get("Product") or {}
        return {
            "guid": b.get("Guid"), "number": b.get("BillNumber"),
            "product_code": prod.get("ProductCode"), "description": prod.get("ProductDescription"),
            "obsolete": bool(b.get("Obsolete")), "auto_assemble": bool(b.get("CanAutoAssemble")),
            "lines": lines, "unit_cost": sum(l["cost"] for l in lines),
        }

    # ------------------------------------------------------------ transactions

    def transactions(self, start, end, product=None, doc_type=None):
        """Unified transaction history (Stock Movement / Transaction Enquiry)."""
        rows = []
        if doc_type in (None, "Purchase"):
            for o in self.received_purchases(start, end):
                goods = o["goods"] or 1
                for l in o["lines"]:
                    qty = l["received_qty"] if l["received_qty"] is not None else l["qty"]
                    value = l["line_total"] + o["shipping"] * l["line_total"] / goods
                    rows.append(_txn(o["received_date"], "Purchase", o["number"],
                                     o["supplier_name"], l, qty, value, warehouse=o.get("warehouse")))
        if doc_type in (None, "Production"):
            for a in self._completed_assemblies(start, end):
                for l in a["lines"]:  # components used, then the product made
                    rows.append(_txn(a["completed_date"], "Production", a["number"],
                                     f"Used for {a['product_code']}", l, -l["consumed"], -l["cost"]))
                rows.append(_txn(a["completed_date"], "Production", a["number"], "Made", a,
                                 a["qty"], a["cost"]))
        if doc_type in (None, "Sale"):
            for o in self._completed_sales(start, end):
                for l in o["lines"]:
                    rows.append(_txn(o["completed_date"] or o["order_date"], "Sale", o["number"],
                                     o["customer_name"], l, -l["qty"], -l["cost"], warehouse=o.get("warehouse")))
        if doc_type in (None, "Credit"):
            for c in self._completed_returns("CreditNotes", start, end):
                for l in c["lines"]:
                    if l["returned"]:
                        rows.append(_txn(c["date"], "Credit", c["number"], c["customer_name"], l, l["qty"], l["cost"],
                                         warehouse=c.get("warehouse")))
        if doc_type in (None, "Supplier return"):
            for r in self._completed_returns("SupplierReturns", start, end):
                for l in r["lines"]:
                    if l["qty"]:
                        rows.append(_txn(r["date"], "Supplier return", r["number"], r["supplier_name"], l,
                                         -l["qty"], -l["value"], warehouse=r.get("warehouse")))
        if doc_type in (None, "Adjustment"):
            for a in self.stock_adjustments(start, end)["rows"]:
                rows.append(_txn(a["date"], "Adjustment", a["number"], a["reason"], a,
                                 a["qty_change"], a["value_change"], warehouse=a.get("warehouse")))
        if doc_type in (None, "Transfer"):
            for t in self.transfers(start, end):
                if not t["completed"]:
                    continue
                for l in t["lines"]:  # out of one warehouse, into the other
                    value = l["value"] or 0.0
                    rows.append(_txn(t["date"], "Transfer", t["number"], f"{t['source_name']} → {t['destination_name']}",
                                     l, -l["qty"], -value, warehouse=t["source"]))
                    rows.append(_txn(t["date"], "Transfer", t["number"], f"{t['source_name']} → {t['destination_name']}",
                                     l, l["qty"], value, warehouse=t["destination"]))
        if product:
            rows = [r for r in rows if r["product_code"] == product]
        order = {"Purchase": 0, "Production": 1, "Sale": 2, "Credit": 3, "Supplier return": 4, "Adjustment": 5,
                 "Transfer": 6}
        rows.sort(key=lambda r: (r["date"], order[r["type"]], r["number"]))
        return rows

    def stock_movement(self, product_code, start, end):
        """Per-product movement with running balance, reconciled to stock on hand."""
        opening = self.stock_on_hand(as_at=start - dt.timedelta(days=1), product=product_code)
        closing = self.stock_on_hand(as_at=min(end, self.today), product=product_code)
        open_row = next((r for r in opening["rows"] if r["product_code"] == product_code), None)
        close_row = next((r for r in closing["rows"] if r["product_code"] == product_code), None)
        qty = open_row["qty"] if open_row else 0
        value = open_row["value"] if open_row else 0
        rows = self.transactions(start, end, product=product_code)
        for r in rows:
            qty += r["qty"] or 0
            value += r["value"] or 0
            r["balance_qty"], r["balance_value"] = qty, value
        return {
            "opening_qty": open_row["qty"] if open_row else 0,
            "opening_value": open_row["value"] if open_row else 0,
            "rows": rows, "closing_qty": qty, "closing_value": value,
            "reported_qty": close_row["qty"] if close_row else 0,
            "reported_value": close_row["value"] if close_row else 0,
        }

    # ------------------------------------------------------------ COGS

    def cogs(self, month=None, daily=False, through=None, warehouses=None):
        """COGS = last month's ending inventory + this month's purchases
        (incl. shipping) - current inventory value.

        Inventory is the stock on hand in `warehouses`, by default the COGS warehouses
        (cogs_settings), or in every warehouse when none are chosen: stock that leaves them
        counts as used. It's the stock at midnight in the business's time zone: Unleashed's
        stock as at the nearest date, moved to midnight with the documents in between
        (_stock_at_end). `through` caps the period (default: today for the current month,
        month end for past months)."""
        today = self.today
        start = month_start(month or today)
        end = month_end(start)
        through = min(through or end, end, today)
        prior_end = start - dt.timedelta(days=1)
        if warehouses is None:
            warehouses = self.cogs_settings()["warehouses"]
        is_current = through >= today
        flows = self._flows(prior_end, through, warehouses)

        opening, opening_by = self._stock_at_end(prior_end, warehouses, flows)
        received = self.received_purchases(start, through) if through >= start else []
        goods = sum(o["goods"] for o in received)
        shipping = sum(o["shipping"] for o in received)
        # Stock sent back to suppliers wasn't used: it comes off what was bought.
        sent_back = self._completed_returns("SupplierReturns", start, through, warehouses) if through >= start else []
        returned = sum(r["value"] for r in sent_back)
        # Stock brought in from warehouses COGS doesn't count wasn't used up there: like a purchase.
        transferred, transfers_in = self._transferred(start, through, warehouses)
        closing, closing_by = self._stock_at_end(None if is_current else through, warehouses, flows)
        cogs = opening + goods + shipping - returned + transferred - closing

        result = {
            "month": start, "through": through, "prior_end": prior_end,
            "is_current": is_current, "opening": opening, "goods": goods,
            "shipping": shipping, "returned": returned, "supplier_returns": sent_back,
            "transferred": transferred, "transfers_in": transfers_in,
            "purchases": goods + shipping - returned + transferred, "closing": closing,
            "cogs": cogs, "received": received, "days_elapsed": max((through - start).days + 1, 0),
            "days_in_month": (end - start).days + 1, "warehouses": warehouses,
            "opening_by": opening_by, "closing_by": closing_by,
        }
        if daily and through >= start:
            result["daily"] = self._daily_cogs(start, through, opening, received, warehouses, flows, sent_back,
                                               transfers_in)
        return result

    # ------------------------------------------------------------ stock at a moment

    def stock_reading(self):
        """How Unleashed's stock "as at" a date is read (a STOCK_CUTOFFS key): the one chosen in
        Settings -> COGS, or on automatic (the default) the one that explains the last weeks'
        documents best (stock_timing), worked out again whenever the copy changes."""
        settings = self.cogs_settings()
        chosen = settings.get("stock_as_at")
        if chosen in STOCK_CUTOFFS:
            return chosen
        try:
            return self.stock_timing(settings.get("warehouses"))["best"] or "end"
        except UnleashedError:  # a day's stock isn't in the copy yet
            return "end"

    def _snapshot_for(self, day):
        """(date to ask Unleashed's stock "as at", the moment that stock is at) for the stock
        at the end of `day`: the date whose stock is nearest that moment (cogs_settings
        "stock_as_at", STOCK_CUTOFFS). (None, None): the stock now, when the nearest date is
        today or later (its stock is only had as it is now)."""
        key = self.stock_reading()
        target = end_of_day(day)
        best = min((day + dt.timedelta(days=k) for k in (-1, 0, 1, 2)),
                   key=lambda d: abs((stock_cutoff(key, d) - target).total_seconds()))
        if best >= self.today:
            return None, None
        return best, stock_cutoff(key, best)

    def _flows(self, first_day, last_day, warehouses=None):
        """Every document's stock movement from the day before `first_day` to now (or two
        days after `last_day`), in `warehouses`: [{"at", "product_code", "qty", "value",
        "warehouse", "kind", "number"}]. Enough to move a stock snapshot to any moment
        between."""
        start = first_day - dt.timedelta(days=2)
        end = min(last_day + dt.timedelta(days=3), self.today)
        if end < start:
            return []
        moves = []

        def here(warehouse):
            return not warehouses or not warehouse or warehouse in warehouses

        for o in self.received_purchases(start, end):
            if not here(o.get("warehouse")) or not o.get("received_at"):
                continue
            goods = o["goods"] or 1
            for l in o["lines"]:
                qty = l["received_qty"] if l["received_qty"] is not None else l["qty"]
                moves.append({"at": o["received_at"], "timed": o.get("received_timed"),
                              "product_code": l["product_code"], "qty": qty or 0,
                              "value": l["line_total"] + o["shipping"] * l["line_total"] / goods,
                              "warehouse": o.get("warehouse"), "kind": "Purchase", "number": o["number"]})
            if not o["lines"] and o["shipping"]:
                moves.append({"at": o["received_at"], "timed": o.get("received_timed"), "product_code": None,
                              "qty": 0, "value": o["shipping"],
                              "warehouse": o.get("warehouse"), "kind": "Purchase", "number": o["number"]})
        for o in self._completed_sales(start, end, warehouses):
            for l in o["lines"]:
                moves.append({"at": o["completed_at"], "timed": o.get("completed_timed", True),
                              "product_code": l["product_code"], "qty": -(l["qty"] or 0),
                              "value": -(l["cost"] or 0), "warehouse": o["warehouse"], "kind": "Sale",
                              "number": o["number"]})
        for r in self.stock_adjustments(start, end)["rows"]:
            if here(r["warehouse"]) and r.get("at"):
                moves.append({"at": r["at"], "timed": r.get("timed"), "product_code": r["product_code"],
                              "qty": r["qty_change"] or 0,
                              "value": r["value_change"] or 0, "warehouse": r["warehouse"], "kind": "Adjustment",
                              "number": r["number"]})
        for c in self._completed_returns("CreditNotes", start, end, warehouses):
            for l in c["lines"]:
                if l["returned"] and c["at"]:
                    moves.append({"at": c["at"], "timed": c["timed"], "product_code": l["product_code"],
                                  "qty": l["qty"], "value": l["cost"], "warehouse": c["warehouse"],
                                  "kind": "Credit", "number": c["number"]})
        for r in self._completed_returns("SupplierReturns", start, end, warehouses):
            for l in r["lines"]:
                if l["qty"] and r["at"]:
                    moves.append({"at": r["at"], "timed": r["timed"], "product_code": l["product_code"],
                                  "qty": -l["qty"], "value": -l["value"], "warehouse": r["warehouse"],
                                  "kind": "Supplier return", "number": r["number"]})
        for t in self.transfers(start, end):
            if not t["completed"] or not t["at"]:
                continue
            for l in t["lines"]:
                for warehouse, sign in ((t["source"], -1), (t["destination"], 1)):
                    if here(warehouse):
                        moves.append({"at": t["at"], "timed": t["timed"], "product_code": l["product_code"],
                                      "qty": sign * l["qty"], "value": sign * (l["value"] or 0), "warehouse": warehouse,
                                      "kind": "Transfer", "number": t["number"]})
        for a in self._completed_assemblies(start, end):
            if not a.get("completed_at"):
                continue
            for l in a["lines"]:
                moves.append({"at": a["completed_at"], "timed": a.get("completed_timed"),
                              "product_code": l["product_code"], "qty": -l["consumed"],
                              "value": -l["cost"], "warehouse": None, "kind": "Production", "number": a["number"]})
            moves.append({"at": a["completed_at"], "timed": a.get("completed_timed"),
                          "product_code": a["product_code"], "qty": a["qty"],
                          "value": a["cost"] + (a["supplier_costs"] or 0), "warehouse": None, "kind": "Production",
                          "number": a["number"]})
        return [m for m in moves if m["at"]]

    @staticmethod
    def _between(flows, after, upto):
        """The documents' movements from just after `after` up to `upto` (moments; None: no
        limit)."""
        return [m for m in flows if (after is None or m["at"] > after) and (upto is None or m["at"] <= upto)]

    def _stock_at_end(self, day, warehouses=None, flows=None, by_product=False):
        """The stock at the end of `day` (None: now) in `warehouses`: Unleashed's stock as at
        the nearest date, with the documents between that moment and midnight added or taken
        away. (value, {warehouse: value} or None); with `by_product`, {product code: {"qty",
        "value", "description"}} instead."""
        read_at = getattr(self.client, "current_stock_at", lambda: None)()  # when "now" was read
        if day is None or day >= self.today:
            as_at, cut, target = None, None, None
        else:
            as_at, cut = self._snapshot_for(day)
            target = end_of_day(day)
        moves = []
        if target is None:
            if read_at is not None:  # now: what documents moved after the stock was read
                flows = self._flows(self.today, self.today, warehouses) if flows is None else flows
                # Only documents with a time: one dated today with none may be before or after.
                moves = [m for m in self._between(flows, read_at, None) if m.get("timed")]
        else:
            flows = self._flows(day, day, warehouses) if flows is None else flows
            if cut is None:  # the stock as it was read today: take away what moved since midnight
                # A document dated today with no time of day is in that stock too (it was in
                # Unleashed when the copy was made), whenever in the day it's taken to be.
                moves = [dict(m, qty=-m["qty"], value=-m["value"]) for m in self._between(flows, target, None)
                         if read_at is None or m["at"] <= read_at or not m.get("timed")]
            elif cut < target:
                moves = self._between(flows, cut, target)
            elif cut > target:
                moves = [dict(m, qty=-m["qty"], value=-m["value"]) for m in self._between(flows, target, cut)]
        if by_product:
            products = defaultdict(lambda: {"qty": 0.0, "value": 0.0, "description": None})
            for w in warehouses or [None]:
                for r in self.stock_on_hand(as_at=as_at, warehouse=w)["rows"]:
                    p = products[r["product_code"]]
                    p["qty"] += r.get("qty") or 0
                    p["value"] += r.get("value") or 0
                    p["description"] = p["description"] or r.get("description")
            for m in moves:
                p = products[m["product_code"] or "(costs with no product)"]
                p["qty"] += m["qty"]
                p["value"] += m["value"]
            return products
        value, by = self._inventory(as_at, warehouses)
        shift = sum(m["value"] for m in moves)
        if by is not None and moves:
            by = dict(by)
            for m in moves:
                w = m["warehouse"] if m["warehouse"] in by else next(iter(by))
                by[w] += m["value"]
        return value + shift, by

    def _as_at_end(self, day):
        """The date to ask Unleashed's stock "as at" for the stock nearest the end of `day`
        (None: now). See _snapshot_for."""
        return None if day is None or day >= self.today else self._snapshot_for(day)[0]

    def stock_timing(self, warehouses=None):
        """When in the day Unleashed's stock "as at" a date is, worked out from the stock on
        each day of last month and this month against each day's documents, product by
        product: for each way of reading it (STOCK_CUTOFFS), the stock (at average cost) whose
        daily change the documents don't explain. The right reading explains the most:
        {"days", "scores": {key: $}, "best": the reading that explains the most, "verdict": it,
        or None when every reading explains about the same, "label", "in_use": the reading COGS
        uses, "auto": whether that's chosen automatically, "in_use_ok": whether it explains
        about as much as the best, "residual": $ unexplained read the way in use,
        "residual_days": the days most of it is on, with their products}."""
        chosen = self.cogs_settings().get("stock_as_at")
        chosen = chosen if chosen in STOCK_CUTOFFS else None
        memo = getattr(self.client, "memo", None)
        key = ("stock_timing", self.today, warehouses and tuple(warehouses), chosen)
        found = memo(key, lambda: self._stock_timing(warehouses, chosen)) if memo else \
            self._stock_timing(warehouses, chosen)
        return dict(found)

    def _stock_timing(self, warehouses, chosen):
        today, first = self.today, add_months(month_start(self.today), -1)
        days = [first + dt.timedelta(days=i) for i in range(1, (today - first).days - 1)]
        if len(days) < 5:
            return {"days": 0, "scores": {}, "verdict": None, "label": None, "residual": 0.0,
                    "residual_days": [], "end": 0.0, "start": 0.0, "best": None, "in_use": chosen or "end",
                    "auto": chosen is None, "in_use_ok": True}
        dates = [days[0] - dt.timedelta(days=1)] + days

        def stock(d):
            rows = defaultdict(lambda: [0.0, 0.0, None])  # qty, value, description
            for w in warehouses or [None]:
                for r in self.stock_on_hand(as_at=d, warehouse=w)["rows"]:
                    row = rows[r["product_code"]]
                    row[0] += r["qty"] or 0
                    row[1] += r["value"] or 0
                    row[2] = row[2] or r["description"]
            return rows

        snaps = dict(zip(dates, self._map(stock, dates)))
        flows = self._flows(dates[0], days[-1], warehouses)
        unit = {}
        for rows in snaps.values():
            for code, (qty, value, _) in rows.items():
                if qty and value:
                    unit[code] = abs(value / qty)
        names = {code: row[2] for rows in snaps.values() for code, row in rows.items()}

        def gaps(key):
            """[(day, {product: (unexplained qty, unexplained value)})] read as `key`."""
            out = []
            for prev, day in zip(dates, days):
                moved = defaultdict(lambda: [0.0, 0.0])
                for m in self._between(flows, stock_cutoff(key, prev), stock_cutoff(key, day)):
                    moved[m["product_code"]][0] += m["qty"]
                    moved[m["product_code"]][1] += m["value"]
                before, after = snaps[prev], snaps[day]
                found = {}
                for code in set(before) | set(after) | set(moved):
                    if code is None:
                        continue
                    qty = (after[code][0] if code in after else 0) - (before[code][0] if code in before else 0) \
                        - moved[code][0]
                    if abs(qty) > 1e-6:
                        found[code] = (qty, qty * unit.get(code, 0))
                out.append((day, found))
            return out

        scores = {key: sum(abs(v) for _, found in gaps(key) for _, v in found.values()) for key in STOCK_CUTOFFS}
        ranked = sorted(STOCK_CUTOFFS, key=lambda k: scores[k])  # ties keep STOCK_CUTOFFS' order
        best = ranked[0]
        # Two readings a few hours apart can explain almost the same (little moves overnight): the
        # best still stands, as long as some reading is clearly worse. None: they're all alike.
        def close(k):
            return scores[k] <= scores[best] * 1.25 + 0.01
        verdict = best if scores[best] < 0.01 or not all(close(k) for k in STOCK_CUTOFFS) else None
        in_use = chosen or best
        residual_days = []
        for day, found in gaps(in_use):
            total = sum(v for _, v in found.values())
            if found:
                top = sorted(found.items(), key=lambda kv: -abs(kv[1][1]))[:5]
                residual_days.append({"date": day, "value": total, "abs": sum(abs(v) for _, v in found.values()),
                                      "products": [{"product_code": c, "description": names.get(c), "qty": q,
                                                    "value": v} for c, (q, v) in top]})
        residual_days.sort(key=lambda r: -r["abs"])
        return {"days": len(days), "scores": scores, "verdict": verdict, "best": best, "in_use": in_use,
                "auto": chosen is None, "in_use_ok": close(in_use),
                "label": STOCK_CUTOFFS[verdict][0] if verdict else None,
                "residual": sum(r["abs"] for r in residual_days), "residual_days": residual_days[:10],
                "end": scores["end"], "start": scores["start"]}

    def _inventory(self, as_at, warehouses=None):
        """(Inventory value as at a date (None: now), {warehouse: value}) in `warehouses`,
        or (value, None) in every warehouse."""
        if not warehouses:
            return self.inventory_value(as_at=as_at), None
        by = {w: self.inventory_value(as_at=as_at, warehouse=w) for w in warehouses}
        return sum(by.values()), by

    def warehouse_names(self, codes):
        """"Warehouse, Cold Storage and Gardena": the warehouses' names, in their order."""
        names = {w.get("WarehouseCode"): w.get("WarehouseName") or w.get("WarehouseCode")
                 for w in self.warehouses()}
        named = [names.get(c, c) for c in codes]
        return f"{', '.join(named[:-1])} and {named[-1]}" if len(named) > 1 else "".join(named)

    def _daily_cogs(self, start, through, opening, received, warehouses=None, flows=None, sent_back=(),
                    transfers_in=()):
        by_day = defaultdict(float)
        for o in received:
            by_day[o["received_date"]] += o["goods"] + o["shipping"]
        for r in sent_back:
            by_day[r["date"]] -= r["value"]
        for t in transfers_in:
            by_day[t["date"]] += t["value"]
        days = [start + dt.timedelta(days=i) for i in range((through - start).days + 1)]
        closings = self._map(lambda d: self._stock_at_end(d, warehouses, flows)[0], days)
        rows, purchases = [], 0.0
        for day, closing in zip(days, closings):
            purchases += by_day[day]
            running = opening + purchases - closing
            rows.append({"date": day, "purchases_to_date": purchases,
                         "inventory": closing, "cogs_to_date": running,
                         "cogs_day": running - (rows[-1]["cogs_to_date"] if rows else 0)})
        return rows

    def _completed_sales(self, start, through, warehouses=None):
        """Sales orders completed from `start` to `through` (by completed date, whenever they
        were placed), shipped from one of `warehouses` when some are given (an order that
        doesn't say counts)."""
        if getattr(self.client, "finds_by_completion", False):
            orders = [self._so(i) for i in self.client.get_all("SalesOrders", {
                "orderStatus": "Completed", "completedStartDate": start.isoformat(),
                "completedEndDate": through.isoformat()})]
        else:
            orders = self.sales_orders(start - dt.timedelta(days=SALE_LOOKBACK_DAYS), through, status="Completed")
        return [o for o in orders if start <= (o["completed_date"] or o["order_date"]) <= through
                and (not warehouses or not o["warehouse"] or o["warehouse"] in warehouses)]

    def _completed_assemblies(self, start, through):
        """Production orders completed from `start` to `through`, whenever they were dated."""
        if getattr(self.client, "finds_by_completion", False):
            items = self.client.get_all("Assemblies", {"completedStartDate": start.isoformat(),
                                                       "completedEndDate": through.isoformat()})
            costs = self.unit_costs() if items else {}
            orders = [self._assembly(i, costs) for i in items]
        else:
            orders = self.assemblies(start - dt.timedelta(days=SALE_LOOKBACK_DAYS), through)
        return [a for a in orders if a["completed"] and a["completed_date"] and start <= a["completed_date"] <= through]

    def _write_offs(self, start, through, warehouses=None):
        """(Stock value written off by adjustments, lines without a value change), in one of
        `warehouses` when some are given."""
        rows = [r for r in self.stock_adjustments(start, through)["rows"]
                if not warehouses or not r["warehouse"] or r["warehouse"] in warehouses]
        return (-sum(r["value_change"] or 0 for r in rows),
                sum(1 for r in rows if r["value_change"] is None))

    def cogs_reconciliation(self, month=None, warehouses=None):
        """Cross-check the formula against cost of sales + adjustments."""
        start = month_start(month or self.today)
        through = min(month_end(start), self.today)
        if warehouses is None:
            warehouses = self.cogs_settings()["warehouses"]
        cost_of_sales = sum(o["cost"] for o in self._completed_sales(start, through, warehouses))
        # What customers sent back into stock wasn't sold after all.
        returns_in = sum(c["cost"] for c in self._completed_returns("CreditNotes", start, through, warehouses))
        written_off, missing = self._write_offs(start, through, warehouses)
        return {"cost_of_sales": cost_of_sales, "returns_in": returns_in, "adjustments_out": written_off,
                "adjustments_missing": missing, "total": cost_of_sales - returns_in + written_off}

    def cogs_by_store(self, cogs):
        """`cogs` (a cogs() result) split by store. The stores are customers (cogs_settings):
        what was sent to a store on completed sales orders, valued at Unleashed's average
        landed cost when it went (not the price on the order), is that store's COGS. The
        rest wasn't sent to a store: other customers' orders, stock written off, and what
        no order or write-off explains (usage not recorded, production losses, stock
        moved out of the COGS warehouses). The rows add up to `cogs`."""
        start, through, warehouses, total = cogs["month"], cogs["through"], cogs["warehouses"], cogs["cogs"]
        codes = self.cogs_settings()["stores"]
        names = {c.get("CustomerCode"): c.get("CustomerName") for c in self.customers()} if codes else {}
        stores = {code: {"code": code, "name": names.get(code) or code, "orders": 0, "cost": 0.0, "returned": 0.0}
                  for code in codes}
        other = {"orders": 0, "cost": 0.0, "returned": 0.0}
        written_off = 0.0
        if through >= start:
            for o in self._completed_sales(start, through, warehouses):
                row = stores.get(o["customer_code"], other)
                row["orders"] += 1
                row["cost"] += o["cost"]
            for c in self._completed_returns("CreditNotes", start, through, warehouses):
                row = stores.get(c["customer_code"], other)  # what came back from it isn't its cost
                row["returned"] = row.get("returned", 0.0) + c["cost"]
                row["cost"] -= c["cost"]
            written_off = self._write_offs(start, through, warehouses)[0]
        rows = sorted(stores.values(), key=lambda r: (-r["cost"], r["name"].lower()))
        sent = sum(r["cost"] for r in rows)
        rest = total - sent - other["cost"] - written_off

        def share(value):
            return value / total if total > 0 else None

        for r in rows:
            r["share"] = share(r["cost"])
        return {"stores": rows, "sent": sent, "sent_share": share(sent),
                "other_customers": other["cost"], "other_orders": other["orders"], "other_returned": other["returned"],
                "other_share": share(other["cost"]), "written_off": written_off,
                "written_off_share": share(written_off), "rest": rest, "rest_share": share(rest),
                "cogs": total}

    def daily_unexplained(self, cogs):
        """"Not on an order or write-off" day by day, so drift shows the day it starts: for each
        day of `cogs`'s month (a cogs() result), the stock at the end of it, what that day's
        documents explain (purchases received less returns to suppliers, cost of sales less
        customer returns, write-offs, production), and the rest, with the products it's on.
        Each day stands alone, so a difference on one day doesn't carry into the next; the days
        add up to the month's figure (cogs_by_store's "rest"). None for a month whose daily
        stock isn't kept (before last month)."""
        start, through, warehouses = cogs["month"], cogs["through"], cogs["warehouses"]
        if start < add_months(month_start(self.today), -1) or through < start:
            return None
        days = [start + dt.timedelta(days=i) for i in range((through - start).days + 1)]
        flows = self._flows(cogs["prior_end"], through, warehouses)
        stock = {d: self._stock_at_end(None if (cogs["is_current"] and d == through) else d, warehouses, flows,
                                       by_product=True)
                 for d in [cogs["prior_end"]] + days}
        moved = defaultdict(lambda: defaultdict(float))  # day -> product -> value the documents moved
        docs = defaultdict(lambda: defaultdict(list))  # day -> product -> the documents, to show the working
        added = defaultdict(float)  # day -> what production added beyond its components (labour etc.)

        def note(day, code, kind, number, qty, value, **extra):
            moved[day][code] += value
            docs[day][code].append({"kind": kind, "number": number, "qty": qty, "value": value, **extra})

        for o in cogs["received"]:
            goods = o["goods"] or 1
            elsewhere = bool(warehouses and o.get("warehouse") and o["warehouse"] not in warehouses)
            for l in o["lines"]:
                share = o["shipping"] * l["line_total"] / goods
                qty = l["received_qty"] if l.get("received_qty") is not None else l["qty"]
                note(o["received_date"], l["product_code"], "Purchase", o["number"], qty, l["line_total"] + share,
                     goods=l["line_total"], shipping=share, warehouse=o.get("warehouse"), elsewhere=elsewhere)
            if not o["lines"] and o["shipping"]:
                note(o["received_date"], "(costs with no product)", "Purchase", o["number"], 0, o["shipping"],
                     shipping=o["shipping"], warehouse=o.get("warehouse"), elsewhere=elsewhere)
        for r in cogs.get("supplier_returns") or []:
            for l in r["lines"]:
                note(r["date"], l["product_code"], "Supplier return", r["number"], -l["qty"], -l["value"],
                     warehouse=r.get("warehouse"))
        for o in self._completed_sales(start, through, warehouses):
            for l in o["lines"]:
                note(o["completed_date"] or o["order_date"], l["product_code"], "Sale", o["number"], -(l["qty"] or 0),
                     -l["cost"], warehouse=o.get("warehouse"))
        for c in self._completed_returns("CreditNotes", start, through, warehouses):
            for l in c["lines"]:
                note(c["date"], l["product_code"], "Credit", c["number"], l["qty"], l["cost"], warehouse=c.get("warehouse"))
        for r in self.stock_adjustments(start, through)["rows"]:
            if not warehouses or not r["warehouse"] or r["warehouse"] in warehouses:
                note(r["date"], r["product_code"], "Adjustment", r["number"], r["qty_change"] or 0,
                     r["value_change"] or 0, warehouse=r["warehouse"])
        # Brought in from a warehouse COGS doesn't count: like a purchase (cogs() counts it with them).
        # Sent out to one counts as used, as stock leaving the warehouses always has, so it stays out.
        for t in cogs.get("transfers_in") or []:
            for l in t["lines"]:
                note(t["date"], l["product_code"], "Transfer in", t["number"], l["qty"], l["value"] or 0,
                     warehouse=t["destination"])
        for a in self._completed_assemblies(start, through):
            for l in a["lines"]:
                note(a["completed_date"], l["product_code"], "Production", a["number"], -l["consumed"], -l["cost"])
            note(a["completed_date"], a["product_code"], "Production", a["number"], a["qty"], a["cost"])
            added[a["completed_date"]] += a["supplier_costs"] or 0
        names = {code: row["description"] for rows in stock.values() for code, row in rows.items()}
        empty = {"qty": 0.0, "value": 0.0}
        rows, running, prev = [], 0.0, cogs["prior_end"]
        for d in days:
            before, after = stock[prev], stock[d]
            gaps = {}
            for code in set(before) | set(after) | set(moved[d]):
                gap = (before[code]["value"] if code in before else 0.0) + moved[d][code] \
                    - (after[code]["value"] if code in after else 0.0)
                gaps[code] = gap
            value = sum(gaps.values()) - added[d]
            running += value
            products = []
            for code, gap in sorted(gaps.items(), key=lambda kv: -abs(kv[1]))[:8]:
                if abs(gap) < 0.005:
                    continue
                b, a = before.get(code, empty), after.get(code, empty)
                entries = docs[d][code]
                products.append({
                    "product_code": code, "description": names.get(code), "value": gap,
                    "before_qty": b["qty"], "before_value": b["value"], "after_qty": a["qty"], "after_value": a["value"],
                    "docs": entries, "moved": moved[d][code], "moved_qty": sum(e["qty"] or 0 for e in entries),
                    # Stock that changed by a different amount than the documents say, in quantity: the
                    # documents and the stock disagree on what happened (not on its cost).
                    "qty_gap": b["qty"] + sum(e["qty"] or 0 for e in entries) - a["qty"],
                    "elsewhere": any(e.get("elsewhere") for e in entries),
                    "unit_before": b["value"] / b["qty"] if b["qty"] > 1e-9 else None,
                    "unit_after": a["value"] / a["qty"] if a["qty"] > 1e-9 else None,
                })
                q = products[-1]
                # The stock that stayed, revalued: its unit value (Unleashed's average cost) moved, e.g.
                # with a receipt at a different price. That's value no document records either.
                q["revalued"] = (min(b["qty"], a["qty"]) * (q["unit_before"] - q["unit_after"])
                                 if q["unit_before"] is not None and q["unit_after"] is not None else 0.0)
            rows.append({
                "date": d, "stock": sum(r["value"] for r in after.values()),
                "change": sum(r["value"] for r in after.values()) - sum(r["value"] for r in before.values()),
                "explained": sum(moved[d].values()) + added[d], "production_added": added[d],
                "value": value, "running": running, "products": products,
            })
            prev = d
        return {"days": rows, "total": running}

    def unexplained(self, cogs):
        """Where "not on an order or write-off" (cogs_by_store's rest) comes from: for each
        product, the stock value the month's documents say it should have (opening +
        received ± production − sold − written off) against what it has, and the things
        that are known to put value there. Σ product gaps − production value added = rest."""
        start, through, warehouses = cogs["month"], cogs["through"], cogs["warehouses"]
        products = defaultdict(lambda: {"opening": 0.0, "received": 0.0, "production": 0.0, "sold": 0.0,
                                        "written_off": 0.0, "closing": 0.0, "description": None})

        flows = self._flows(cogs["prior_end"], through, warehouses)

        def stock(day, key):
            for code, row in self._stock_at_end(day, warehouses, flows, by_product=True).items():
                p = products[code]
                p[key] += row["value"]
                p["description"] = p["description"] or row["description"]

        stock(cogs["prior_end"], "opening")
        stock(None if cogs["is_current"] else through, "closing")
        findings = {"partly_received": [], "elsewhere": [], "production_added": 0.0, "production_orders": []}
        if through >= start:
            for o in cogs["received"]:
                goods = o["goods"] or 1
                for l in o["lines"]:
                    products[l["product_code"]]["received"] += l["line_total"] + o["shipping"] * l["line_total"] / goods
                if not o["lines"] and o["shipping"]:  # costs with no goods to put them on
                    products["(shipping on orders without goods)"]["received"] += o["shipping"]
                short = sum((l["qty"] - l["received_qty"]) * (l["unit_price"] or 0) for l in o["lines"]
                            if l["received_qty"] is not None and l["received_qty"] < l["qty"] - 1e-9)
                if short > 0.005:
                    findings["partly_received"].append({"number": o["number"], "value": short})
                if warehouses and o.get("warehouse") and o["warehouse"] not in warehouses:
                    findings["elsewhere"].append({"number": o["number"], "warehouse": o["warehouse"],
                                                  "value": o["total"]})
            for o in self._completed_sales(start, through, warehouses):
                for l in o["lines"]:
                    products[l["product_code"]]["sold"] += l["cost"]
            for c in self._completed_returns("CreditNotes", start, through, warehouses):
                for l in c["lines"]:
                    products[l["product_code"]]["sold"] -= l["cost"]  # came back into stock
            for r in cogs.get("supplier_returns") or []:
                for l in r["lines"]:
                    products[l["product_code"]]["received"] -= l["value"]  # went back to the supplier
            for r in self.stock_adjustments(start, through)["rows"]:
                if not warehouses or not r["warehouse"] or r["warehouse"] in warehouses:
                    products[r["product_code"]]["written_off"] -= r["value_change"] or 0
            if warehouses:  # in from other warehouses (like bought), out to them (counted as used)
                for t in self.transfers(start, through):
                    if t["completed"] and (t["source"] in warehouses) != (t["destination"] in warehouses):
                        for l in t["lines"]:
                            key = "received" if t["destination"] in warehouses else "sold"
                            products[l["product_code"]][key] += l["value"] or 0
            for a in self._completed_assemblies(start, through):
                for l in a["lines"]:
                    products[l["product_code"]]["production"] -= l["cost"]
                products[a["product_code"]]["production"] += a["cost"]
                if a["supplier_costs"]:
                    findings["production_added"] += a["supplier_costs"]
                    findings["production_orders"].append({"number": a["number"], "value": a["supplier_costs"]})
        rows = []
        for code, p in products.items():
            expected = p["opening"] + p["received"] + p["production"] - p["sold"] - p["written_off"]
            rows.append(dict(p, product_code=code, expected=expected, gap=expected - p["closing"]))
        rows.sort(key=lambda r: -abs(r["gap"]))
        return {"products": [r for r in rows if abs(r["gap"]) >= 0.5], "gap": sum(r["gap"] for r in rows),
                "findings": findings}

    def sent_to(self, cogs, customer):
        """What went to `customer` (a store) in `cogs`'s month (a cogs() result): its completed
        sales orders, and their lines by product, at Unleashed's average landed cost when they
        went (not the price on the order)."""
        start, through = cogs["month"], cogs["through"]
        orders = [o for o in self._completed_sales(start, through, cogs["warehouses"])
                  if o["customer_code"] == customer] if through >= start else []
        products = {}
        for o in orders:
            for l in o["lines"]:
                p = products.setdefault(l["product_code"], {"product_code": l["product_code"],
                                                            "description": l["description"], "qty": 0.0, "cost": 0.0})
                p["qty"] += l["qty"] or 0
                p["cost"] += l["cost"] or 0
        credits = [c for c in self._completed_returns("CreditNotes", start, through, cogs["warehouses"])
                   if c["customer_code"] == customer] if through >= start else []
        for c in credits:  # what it sent back comes off
            for l in c["lines"]:
                if l["returned"]:
                    p = products.setdefault(l["product_code"], {"product_code": l["product_code"],
                                                                "description": l["description"], "qty": 0.0, "cost": 0.0})
                    p["qty"] -= l["qty"] or 0
                    p["cost"] -= l["cost"] or 0
        cost = sum(o["cost"] for o in orders) - sum(c["cost"] for c in credits)
        return {"orders": sorted(orders, key=lambda o: (o["completed_date"] or o["order_date"], o["number"])),
                "credits": credits, "returned": sum(c["cost"] for c in credits),
                "products": sorted(products.values(), key=lambda p: (-p["cost"], p["product_code"] or "")),
                "cost": cost, "share": cost / cogs["cogs"] if cogs["cogs"] > 0 else None}

    def cogs_history(self, months=12, by_store=False, warehouses=None):
        first = add_months(month_start(self.today), -(months - 1))

        def month_row(m):
            c = self.cogs(m, warehouses=warehouses)
            # Revenue from the same orders COGS counts: completed in the month, whenever placed.
            through = min(month_end(m), self.today)
            c["revenue"] = sum(o["subtotal"] for o in self._completed_sales(m, through)) - \
                sum(x["subtotal"] or 0 for x in self._completed_returns("CreditNotes", m, through))
            c["gross_margin"] = c["revenue"] - c["cogs"]
            c["cogs_pct"] = c["cogs"] / c["revenue"] if c["revenue"] else None
            if by_store:
                c["by_store"] = self.cogs_by_store(c)
            return c

        memo = getattr(self.client, "memo", None)
        if memo is not None:  # kept until the copy changes (a sync, or a write from Core), per settings
            settings = self.cogs_settings()
            key = ("cogs_history", self.today, warehouses and tuple(warehouses), by_store,
                   repr(sorted((k, repr(v)) for k, v in settings.items())))
            row = month_row
            month_row = lambda m: memo(key + (m,), lambda: row(m))  # noqa: E731
        return [dict(c) for c in self._map(month_row, [add_months(first, i) for i in range(months)])]


def with_po_costs(client, items, map_fn=lambda f, xs: [f(x) for x in xs], previous=None):
    """`items` (purchase orders as Unleashed lists them), each received one with its cost lines
    from GET /PurchaseOrders/{guid}/Costs under PO_COSTS_KEY. `previous`: {guid: stored order},
    whose costs are kept when the order hasn't changed since. An order whose costs can't be
    read is left without them (and tried again next time)."""
    from .client import UnleashedError

    def one(i):
        if not i.get("Guid") or i.get("OrderStatus") not in RECEIVED_PO_STATUSES or PO_COSTS_KEY in i:
            return i
        old = (previous or {}).get(i["Guid"])
        if old and PO_COSTS_KEY in old and old.get("LastModifiedOn") == i.get("LastModifiedOn"):
            return dict(i, **{PO_COSTS_KEY: old[PO_COSTS_KEY]})
        try:
            data = client.get(f"PurchaseOrders/{i['Guid']}/Costs")
        except UnleashedError:
            return i
        costs = data.get("Items") if isinstance(data, dict) else data
        return dict(i, **{PO_COSTS_KEY: costs or []})

    return map_fn(one, items)


def cost_line_keys(raw_po):
    """Keys of a PO's additional-cost arrays (Unleashed "Cost Lines"); empty if none."""
    return [k for k, v in raw_po.items()
            if "cost" in k.lower() and k != "PurchaseOrderLines"
            and (isinstance(v, list) or (v is None and k.lower().endswith(("lines", "costs"))))]


def cost_line_amount(line):
    """(field name, amount) for a cost line; (None, 0) if no known amount field."""
    for field in COST_LINE_AMOUNT_FIELDS:
        if line.get(field) is not None:
            return field, line[field]
    return None, 0


def _first(*values):
    """First value that isn't None (a null BC field falls back to the plain one)."""
    return next((v for v in values if v is not None), 0)


def _batches(items):
    """Batches as Unleashed lists them on an assembly: [{"number", "qty", "expiry"}]."""
    return [{"number": b.get("Number") or b.get("BatchNumber"), "qty": b.get("Quantity") or 0,
             "expiry": parse_date(b.get("ExpiryDate"))} for b in items or []]

def _field(record, names):
    """The first of `names` that `record` has a value for, or None."""
    return next((record[n] for n in names if record.get(n) is not None), None)


def _txn(date, kind, number, party, line, qty, value, warehouse=None):
    return {"date": date, "type": kind, "number": number, "party": party,
            "product_code": line["product_code"], "description": line.get("description"),
            "qty": qty, "value": value, "warehouse": warehouse}
