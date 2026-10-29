"""Report enquiries built purely on the Unleashed API.

Each method mirrors an Unleashed report/enquiry screen and gets all of its
data through UnleashedClient — so pointing the client at the real API
reproduces the same reports from live data.
"""
import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from .client import parse_date

RECEIVED_PO_STATUSES = ("Receipted", "Costed", "Complete")
OPEN_PO_STATUSES = ("Parked", "Placed")
PO_STATUSES = ("Parked", "Placed", "Receipted", "Costed", "Complete", "Deleted")
SO_STATUSES = ("Parked", "Placed", "Backordered", "Completed", "Deleted")
# Adjustments in these states never moved stock, so reports leave them out.
INACTIVE_ADJUSTMENT_STATUSES = ("deleted", "parked")
# Where a PO cost line's amount may live, most specific (base currency, ex tax) first.
COST_LINE_AMOUNT_FIELDS = ("BCSubTotal", "BCLineTotal", "BCAmount", "BCTotal", "SubTotal",
                           "LineTotal", "Amount", "Total", "Cost", "Value")
COMPLETED_ASSEMBLY_STATUSES = ("completed", "complete")
# Assemblies in these states never moved stock, so reports leave them out.
INACTIVE_ASSEMBLY_STATUSES = ("deleted",)
# Where an assembly line's cost may live. Without one, components are costed at their
# current average cost and the order is marked as estimated.
ASSEMBLY_LINE_UNIT_COST_FIELDS = ("AverageLandedPriceAtTimeOfAssembly", "AverageLandedPrice",
                                  "UnitCost", "AverageCost", "AvgCost", "Cost")
ASSEMBLY_LINE_TOTAL_FIELDS = ("TotalCost", "LineTotal", "LineCost", "TotalLineCost")


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return nxt - dt.timedelta(days=1)


def add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


class Reports:
    def __init__(self, client, today_fn=dt.date.today,
                 shipping_codes=("FREIGHT", "SHIPPING", "DELIVERY"), max_workers=1):
        self.client = client
        self.today_fn = today_fn
        self.shipping_codes = {c.upper() for c in shipping_codes}
        self.max_workers = max_workers
        self._details_have_cost_lines = None

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

    def inventory_value(self, as_at=None):
        return self.stock_on_hand(as_at=as_at)["total_value"]

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
        return [self._po(i) for i in self._with_cost_lines(items)]

    def purchase_order(self, order_number):
        items = self.client.get_all("PurchaseOrders", {"orderNumber": order_number})
        exact = [i for i in items if i["OrderNumber"] == order_number]
        return self._po(self._with_cost_lines(exact)[0]) if exact else None

    def _with_cost_lines(self, items):
        """If the list response leaves cost lines out, fetch each PO's detail."""
        if not items or self._details_have_cost_lines is False \
                or any(cost_line_keys(i) for i in items):
            return items

        def detail(i):
            if not i.get("Guid"):
                return i
            d = self.client.get(f"PurchaseOrders/{i['Guid']}",
                                long_cache=i.get("OrderStatus") in ("Complete", "Costed"))
            return d if isinstance(d, dict) and d.get("OrderNumber") else i

        details = self._map(detail, items)
        self._details_have_cost_lines = any(cost_line_keys(d) for d in details)
        return details

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

    def _po(self, i):
        goods, lines, cost_lines = 0.0, [], []
        for l in i.get("PurchaseOrderLines", []):
            prod = l.get("Product") or {}
            code = prod.get("ProductCode", "")
            total = _first(l.get("BCSubTotal"), l.get("BCLineTotal"), l.get("LineTotal"))
            if code.upper() in self.shipping_codes:
                cost_lines.append({"description": prod.get("ProductDescription") or code,
                                   "supplier": None, "amount": total,
                                   "field": "PurchaseOrderLines"})
                continue
            goods += total
            lines.append({"product_code": code, "description": prod.get("ProductDescription"),
                          "qty": l.get("OrderQuantity") or 0,
                          "received_qty": l.get("ReceiptQuantity"),
                          "unit_price": _first(l.get("BCUnitPrice"), l.get("UnitPrice")),
                          "line_total": total})
        for key in cost_line_keys(i):
            for c in i.get(key) or []:
                field, amount = cost_line_amount(c)
                cost_lines.append({
                    "description": c.get("Description") or c.get("Comments") or "Additional cost",
                    "supplier": (c.get("Supplier") or {}).get("SupplierName"),
                    "amount": amount, "field": f"{key}.{field}" if field else None})
        shipping = sum(c["amount"] for c in cost_lines)
        supplier = i.get("Supplier") or {}
        return {
            "guid": i.get("Guid"), "number": i["OrderNumber"], "status": i.get("OrderStatus"),
            "order_date": parse_date(i.get("OrderDate")),
            "required_date": parse_date(i.get("RequiredDate")),
            "received_date": parse_date(i.get("ReceivedDate") or i.get("CompletedDate")),
            "completed_date": parse_date(i.get("CompletedDate")),
            "supplier_code": supplier.get("SupplierCode"),
            "supplier_name": supplier.get("SupplierName"),
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
            lines.append({"product_code": prod.get("ProductCode"),
                          "description": prod.get("ProductDescription"),
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
            "customer_code": customer.get("CustomerCode"),
            "customer_name": customer.get("CustomerName"),
            "subtotal": subtotal, "tax": _first(i.get("BCTaxTotal"), i.get("TaxTotal")),
            "total": _first(i.get("BCTotal"), i.get("Total")),
            "cost": cost, "margin": subtotal - cost, "lines": lines,
        }

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
                rows.append({"number": a["AdjustmentNumber"],
                             "date": parse_date(a.get("AdjustmentDate")),
                             "reason": a.get("AdjustmentReason"), "status": a.get("Status"),
                             "product_code": prod.get("ProductCode"),
                             "description": prod.get("ProductDescription"),
                             "qty_change": l.get("QuantityChange"),
                             "value_change": l.get("ValueChange"),
                             "new_qty": l.get("NewQuantity"),
                             "new_value": l.get("NewActualValue"),
                             "comments": l.get("Comments")})
        rows.sort(key=lambda r: (r["date"], r["number"]), reverse=True)
        return {"rows": rows,
                "value_change": sum(r["value_change"] or 0 for r in rows),
                "missing_change": sum(1 for r in rows if r["value_change"] is None)}

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
        return {
            "guid": i.get("Guid"), "number": i.get("AssemblyNumber"), "status": status,
            "completed": completed,
            "open": not completed and status.lower() not in INACTIVE_ASSEMBLY_STATUSES,
            "product_code": prod.get("ProductCode"), "description": prod.get("ProductDescription"),
            "qty": qty, "date": date, "due": parse_date(i.get("AssembleBy")) or date,
            "completed_date": (parse_date(i.get("CompletedDate")) or date) if completed else None,
            "from_warehouse": source.get("WarehouseName") or source.get("WarehouseCode"),
            "to_warehouse": destination.get("WarehouseName") or destination.get("WarehouseCode"),
            "comments": i.get("Comments"), "lines": lines, "cost": cost,
            "unit_cost": cost / qty if qty else 0, "estimated": any(l["estimated"] for l in lines),
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
                                     o["supplier_name"], l, qty, value))
        if doc_type in (None, "Production"):
            for a in self.assemblies(start, end):
                if not a["completed"] or not start <= a["completed_date"] <= end:
                    continue
                for l in a["lines"]:  # components used, then the product made
                    rows.append(_txn(a["completed_date"], "Production", a["number"],
                                     f"Used for {a['product_code']}", l, -l["consumed"], -l["cost"]))
                rows.append(_txn(a["completed_date"], "Production", a["number"], "Made", a,
                                 a["qty"], a["cost"]))
        if doc_type in (None, "Sale"):
            for o in self.sales_orders(start, end, status="Completed"):
                for l in o["lines"]:
                    rows.append(_txn(o["completed_date"] or o["order_date"], "Sale", o["number"],
                                     o["customer_name"], l, -l["qty"], -l["cost"]))
        if doc_type in (None, "Adjustment"):
            for a in self.stock_adjustments(start, end)["rows"]:
                rows.append(_txn(a["date"], "Adjustment", a["number"], a["reason"], a,
                                 a["qty_change"], a["value_change"]))
        if product:
            rows = [r for r in rows if r["product_code"] == product]
        order = {"Purchase": 0, "Production": 1, "Sale": 2, "Adjustment": 3}
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

    def cogs(self, month=None, daily=False, through=None):
        """COGS = last month's ending inventory + this month's purchases
        (incl. shipping) - current inventory value.

        `through` caps the period (default: today for the current month,
        month end for past months)."""
        today = self.today
        start = month_start(month or today)
        end = month_end(start)
        through = min(through or end, end, today)
        prior_end = start - dt.timedelta(days=1)

        opening = self.inventory_value(as_at=prior_end)
        received = self.received_purchases(start, through) if through >= start else []
        goods = sum(o["goods"] for o in received)
        shipping = sum(o["shipping"] for o in received)
        is_current = through >= today
        closing = self.inventory_value(as_at=None if is_current else through)
        cogs = opening + goods + shipping - closing

        result = {
            "month": start, "through": through, "prior_end": prior_end,
            "is_current": is_current, "opening": opening, "goods": goods,
            "shipping": shipping, "purchases": goods + shipping, "closing": closing,
            "cogs": cogs, "received": received, "days_elapsed": max((through - start).days + 1, 0),
            "days_in_month": (end - start).days + 1,
        }
        if daily and through >= start:
            result["daily"] = self._daily_cogs(start, through, opening, received)
        return result

    def _daily_cogs(self, start, through, opening, received):
        by_day = defaultdict(float)
        for o in received:
            by_day[o["received_date"]] += o["goods"] + o["shipping"]
        days = [start + dt.timedelta(days=i) for i in range((through - start).days + 1)]
        closings = self._map(
            lambda d: self.inventory_value(as_at=None if d >= self.today else d), days)
        rows, purchases = [], 0.0
        for day, closing in zip(days, closings):
            purchases += by_day[day]
            running = opening + purchases - closing
            rows.append({"date": day, "purchases_to_date": purchases,
                         "inventory": closing, "cogs_to_date": running,
                         "cogs_day": running - (rows[-1]["cogs_to_date"] if rows else 0)})
        return rows

    def cogs_reconciliation(self, month=None):
        """Cross-check the formula against cost of sales + adjustments."""
        start = month_start(month or self.today)
        through = min(month_end(start), self.today)
        sales = self.sales_orders(start, through, status="Completed")
        cost_of_sales = sum(o["cost"] for o in sales
                            if start <= (o["completed_date"] or o["order_date"]) <= through)
        adjustments = self.stock_adjustments(start, through)
        adj = adjustments["value_change"]
        return {"cost_of_sales": cost_of_sales, "adjustments_out": -adj,
                "adjustments_missing": adjustments["missing_change"],
                "total": cost_of_sales - adj}

    def cogs_history(self, months=12):
        first = add_months(month_start(self.today), -(months - 1))

        def month_row(m):
            c = self.cogs(m)
            sales = self.sales_enquiry(m, min(month_end(m), self.today), status="Completed")
            c["revenue"] = sales["revenue"]
            c["gross_margin"] = sales["revenue"] - c["cogs"]
            c["cogs_pct"] = c["cogs"] / sales["revenue"] if sales["revenue"] else None
            return c

        return self._map(month_row, [add_months(first, i) for i in range(months)])


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


def _field(record, names):
    """The first of `names` that `record` has a value for, or None."""
    return next((record[n] for n in names if record.get(n) is not None), None)


def _txn(date, kind, number, party, line, qty, value):
    return {"date": date, "type": kind, "number": number, "party": party,
            "product_code": line["product_code"], "description": line.get("description"),
            "qty": qty, "value": value}
