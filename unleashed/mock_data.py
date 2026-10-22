"""Deterministic mock dataset for the mock Unleashed server.

Simulates ~13 months of Gardena café activity day by day through a small
inventory engine (weighted-average landed cost, freight apportioned into
product cost on receipt), so stock-on-hand, purchases, sales and adjustments
all reconcile the way a real Unleashed account does:

    opening value + received purchases (incl. freight) - closing value
        == cost of sales + net adjustment write-offs
"""
import bisect
import datetime as dt
import math
import random
import uuid

WAREHOUSES = [
    {"WarehouseCode": "GDN", "WarehouseName": "Gardena Store", "IsDefault": True},
]

# code, description, group, unit, supplier, base cost, sell price, avg daily use
PRODUCTS = [
    ("MILK-WHL-GAL", "Whole Milk 1 gal", "Dairy", "Each", "SUP-DAIRY", 4.20, 13.50, 9.0),
    ("CRM-HVY-HG", "Heavy Cream 1/2 gal", "Dairy", "Each", "SUP-DAIRY", 9.50, 28.00, 2.5),
    ("MILK-OAT-QT", "Oat Milk Barista 32 oz", "Dairy", "Each", "SUP-DAIRY", 3.10, 11.00, 4.0),
    ("MILK-ALM-QT", "Almond Milk Barista 32 oz", "Dairy", "Each", "SUP-DAIRY", 2.90, 10.00, 1.5),
    ("COF-ESP-5LB", "Espresso Blend 5 lb", "Coffee & Tea", "Bag", "SUP-ROAST", 42.00, 160.00, 1.4),
    ("COF-DEC-5LB", "Decaf Blend 5 lb", "Coffee & Tea", "Bag", "SUP-ROAST", 45.00, 165.00, 0.2),
    ("TEA-MATCHA-1KG", "Ceremonial Matcha 1 kg", "Coffee & Tea", "Bag", "SUP-ROAST", 68.00, 250.00, 0.08),
    ("SYR-VAN-750", "Vanilla Syrup 750 ml", "Syrups & Sauces", "Bottle", "SUP-SYRUP", 7.20, 26.00, 0.9),
    ("SYR-CAR-750", "Caramel Syrup 750 ml", "Syrups & Sauces", "Bottle", "SUP-SYRUP", 7.20, 26.00, 0.5),
    ("SAU-CHOC-64", "Chocolate Sauce 64 oz", "Syrups & Sauces", "Bottle", "SUP-SYRUP", 14.50, 48.00, 0.3),
    ("CUP-12-HOT", "Hot Cup 12 oz (case 1000)", "Packaging", "Case", "SUP-PACK", 78.00, 190.00, 0.12),
    ("CUP-16-COLD", "Cold Cup 16 oz (case 1000)", "Packaging", "Case", "SUP-PACK", 92.00, 220.00, 0.15),
    ("LID-HOT", "Hot Lid (case 1000)", "Packaging", "Case", "SUP-PACK", 41.00, 100.00, 0.12),
]

# code, name, order weekdays (Mon=0), lead days, freight per PO, days of cover
SUPPLIERS = [
    ("SUP-DAIRY", "Mock Dairy Distributors", (0, 3), 1, 18.00, 5),
    ("SUP-ROAST", "Mock Coffee Roasters", (0,), 3, 22.50, 12),
    ("SUP-SYRUP", "Mock Syrup & Sauce Co", (1,), 4, 15.00, 16),
    ("SUP-PACK", "Mock Packaging Supply", (2,), 5, 45.00, 24),
]

CUSTOMERS = [
    ("GDN-POS", "Gardena POS (daily sales)"),
    ("WHSL-01", "Mock Wholesale Cafe"),
    ("CATER-01", "Mock Catering Client"),
]

# Mon..Sun demand multipliers
WEEKDAY_FACTOR = (0.9, 0.9, 0.95, 1.0, 1.1, 1.3, 1.2)
MONTHLY_INFLATION = 0.004


class MockDataset:
    def __init__(self, today=None, seed=20260727, history_months=13):
        self.today = today or dt.date.today()
        self.rng = random.Random(seed)
        self.start = _add_months(self.today.replace(day=1), -history_months)

        self.warehouses = [dict(w, Guid=self._guid()) for w in WAREHOUSES]
        self.products = {}
        for code, desc, group, uom, sup, cost, price, use in PRODUCTS:
            self.products[code] = {
                "ProductCode": code, "ProductDescription": desc, "ProductGroup": group,
                "UnitOfMeasure": uom, "SupplierCode": sup, "BaseCost": cost,
                "SellPrice": price, "DailyUse": use, "Guid": self._guid(),
            }
        self.suppliers = {
            code: {"SupplierCode": code, "SupplierName": name, "OrderDays": days,
                   "LeadDays": lead, "Freight": freight, "CoverDays": cover, "Guid": self._guid()}
            for code, name, days, lead, freight, cover in SUPPLIERS
        }
        self.customers = {code: {"CustomerCode": code, "CustomerName": name, "Guid": self._guid()}
                          for code, name in CUSTOMERS}

        self.purchase_orders = []
        self.sales_orders = []
        self.stock_adjustments = []
        self.ledger = []  # every stock movement, chronological

        # engine state
        self._qty = {c: 0.0 for c in self.products}
        self._value = {c: 0.0 for c in self.products}
        self._on_order = {c: 0.0 for c in self.products}
        self._snap = {c: ([], [], []) for c in self.products}  # ordinals, qty, value
        self._seq = {"PO": 0, "SO": 0, "SA": 0}

        self._simulate()

    # ------------------------------------------------------------ public api

    def stock_as_at(self, product_code, as_at=None):
        """(qty, value) at end of day `as_at` (None = now)."""
        ords, qtys, vals = self._snap[product_code]
        if as_at is None:
            i = len(ords)
        else:
            i = bisect.bisect_right(ords, as_at.toordinal())
        if i == 0:
            return 0.0, 0.0
        return qtys[i - 1], vals[i - 1]

    def on_order(self, product_code):
        return self._on_order[product_code]

    def allocated(self, product_code):
        return sum(line["OrderQuantity"] for so in self.sales_orders
                   if so["OrderStatus"] in ("Parked", "Placed")
                   for line in so["Lines"] if line["ProductCode"] == product_code)

    def last_sale_date(self, product_code, as_at=None):
        last = None
        for so in self.sales_orders:
            if so["OrderStatus"] == "Completed" and (as_at is None or so["CompletedDate"] <= as_at):
                if any(l["ProductCode"] == product_code for l in so["Lines"]):
                    last = so["CompletedDate"]
        return last

    # ------------------------------------------------------------ simulation

    def _simulate(self):
        day = self.start
        pending_receipts = []  # POs awaiting receipt

        self._opening_balance(day)
        while day <= self.today:
            month_factor = (1 + MONTHLY_INFLATION) ** _months_between(self.start, day)

            for po in [p for p in pending_receipts if p["ReceivedDate"] == day]:
                self._receive(po)
                pending_receipts.remove(po)

            self._pos_sales(day, month_factor)
            if day.weekday() == 4:
                self._wholesale_order(day)
            if self.rng.random() < 0.1:
                self._catering_order(day)

            if (day + dt.timedelta(days=1)).day == 1:
                self._month_end_adjustments(day)

            for sup in self.suppliers.values():
                if day.weekday() in sup["OrderDays"]:
                    po = self._place_po(day, sup, month_factor)
                    if po:
                        pending_receipts.append(po)
            day += dt.timedelta(days=1)

        # anything not yet received stays Placed (open on-order)
        for po in pending_receipts:
            po["OrderStatus"] = "Placed"
            po["ReceivedDate"] = None
            po["CompletedDate"] = None

        self._open_documents()

    def _opening_balance(self, day):
        lines = []
        for p in self.products.values():
            qty = math.ceil(p["DailyUse"] * self.suppliers[p["SupplierCode"]]["CoverDays"] * 0.8) or 1
            lines.append((p["ProductCode"], qty, p["BaseCost"]))
        self._adjustment(day, "Opening Balance", [
            {"ProductCode": c, "Qty": q, "Value": round(q * cost, 2), "Comments": "Opening stock"}
            for c, q, cost in lines
        ])

    def _place_po(self, day, sup, month_factor):
        lines = []
        for p in self.products.values():
            if p["SupplierCode"] != sup["SupplierCode"]:
                continue
            target = p["DailyUse"] * sup["CoverDays"]
            position = self._qty[p["ProductCode"]] + self._on_order[p["ProductCode"]]
            if position >= target * 0.6:
                continue
            qty = max(1, math.ceil(target - position))
            price = round(p["BaseCost"] * month_factor * self.rng.uniform(0.97, 1.03), 2)
            lines.append({"ProductCode": p["ProductCode"], "OrderQuantity": qty, "UnitPrice": price,
                          "LineTotal": round(qty * price, 2)})
        if not lines:
            return None
        for l in lines:
            self._on_order[l["ProductCode"]] += l["OrderQuantity"]
        received = day + dt.timedelta(days=sup["LeadDays"])
        recent = (self.today - received).days <= 3
        po = {
            "Guid": self._guid(), "OrderNumber": self._next("PO"), "OrderDate": day,
            "RequiredDate": received, "ReceivedDate": received, "CompletedDate": received,
            "OrderStatus": "Receipted" if recent else "Complete",
            "SupplierCode": sup["SupplierCode"], "Lines": lines,
            "Freight": round(sup["Freight"] * self.rng.uniform(0.95, 1.1), 2),
        }
        po["SubTotal"] = round(sum(l["LineTotal"] for l in lines), 2)
        self.purchase_orders.append(po)
        return po

    def _receive(self, po):
        sub = po["SubTotal"] or 1
        for line in po["Lines"]:
            code = line["ProductCode"]
            freight_share = po["Freight"] * line["LineTotal"] / sub
            value = line["LineTotal"] + freight_share
            line["ReceiptQuantity"] = line["OrderQuantity"]
            line["LandedValue"] = round(value, 4)
            self._on_order[code] -= line["OrderQuantity"]
            self._move(po["ReceivedDate"], "Purchase", po["OrderNumber"], po["SupplierCode"],
                       code, line["OrderQuantity"], value)

    def _pos_sales(self, day, month_factor):
        lines = []
        for p in self.products.values():
            want = _random_round(self.rng, p["DailyUse"] * WEEKDAY_FACTOR[day.weekday()]
                                 * self.rng.uniform(0.8, 1.2))
            qty = min(want, int(self._qty[p["ProductCode"]]))
            if qty > 0:
                lines.append((p["ProductCode"], qty, round(p["SellPrice"] * month_factor, 2)))
        if lines:
            self._sale(day, "GDN-POS", lines)

    def _wholesale_order(self, day):
        qty = min(self.rng.randint(1, 3), int(self._qty["COF-ESP-5LB"]))
        if qty > 0:
            self._sale(day, "WHSL-01", [("COF-ESP-5LB", qty, 120.00)])

    def _catering_order(self, day):
        lines = []
        for code, q, price in (("COF-ESP-5LB", 1, 150.00), ("MILK-OAT-QT", 4, 9.50),
                               ("CUP-12-HOT", 1, 180.00)):
            if self._qty[code] >= q:
                lines.append((code, q, price))
        if lines:
            self._sale(day, "CATER-01", lines)

    def _sale(self, day, customer, lines, status="Completed"):
        so_lines = []
        for code, qty, price in lines:
            avg = self._value[code] / self._qty[code] if self._qty[code] else 0.0
            so_lines.append({"ProductCode": code, "OrderQuantity": qty, "UnitPrice": price,
                             "LineTotal": round(qty * price, 2),
                             "AverageLandedPriceAtTimeOfSale": round(avg, 4)})
        so = {
            "Guid": self._guid(), "OrderNumber": self._next("SO"), "OrderDate": day,
            "RequiredDate": day, "CompletedDate": day if status == "Completed" else None,
            "OrderStatus": status, "CustomerCode": customer, "Lines": so_lines,
        }
        so["SubTotal"] = round(sum(l["LineTotal"] for l in so_lines), 2)
        self.sales_orders.append(so)
        if status == "Completed":
            for line in so_lines:
                code, qty = line["ProductCode"], line["OrderQuantity"]
                avg = self._value[code] / self._qty[code]
                self._move(day, "Sale", so["OrderNumber"], customer, code, -qty, -qty * avg)
        return so

    def _month_end_adjustments(self, day):
        lines = []
        for p in self.products.values():
            code = p["ProductCode"]
            if p["ProductGroup"] == "Dairy":
                qty = min(self.rng.randint(0, 3), int(self._qty[code]))
                reason = "Spoilage"
            elif self.rng.random() < 0.25:
                qty = min(1, int(self._qty[code]))
                reason = "Stocktake variance"
            else:
                continue
            if qty > 0:
                avg = self._value[code] / self._qty[code]
                lines.append({"ProductCode": code, "Qty": -qty, "Value": -qty * avg,
                              "Comments": reason})
        if lines:
            self._adjustment(day, "Month-end stocktake", lines)

    def _adjustment(self, day, reason, lines):
        sa = {"Guid": self._guid(), "AdjustmentNumber": self._next("SA"), "AdjustmentDate": day,
              "AdjustmentReason": reason, "Status": "Completed", "Lines": []}
        for l in lines:
            code = l["ProductCode"]
            self._move(day, "Adjustment", sa["AdjustmentNumber"], reason, code, l["Qty"], l["Value"])
            sa["Lines"].append({"ProductCode": code, "QuantityChange": l["Qty"],
                                "ValueChange": round(l["Value"], 4),
                                "NewQuantity": self._qty[code],
                                "NewActualValue": round(self._value[code], 4),
                                "Comments": l["Comments"]})
        self.stock_adjustments.append(sa)

    def _open_documents(self):
        """A parked PO draft and an open wholesale order, for realism."""
        sup = self.suppliers["SUP-ROAST"]
        lines = [{"ProductCode": "COF-ESP-5LB", "OrderQuantity": 6, "UnitPrice": 44.00,
                  "LineTotal": 264.00}]
        self.purchase_orders.append({
            "Guid": self._guid(), "OrderNumber": self._next("PO"), "OrderDate": self.today,
            "RequiredDate": self.today + dt.timedelta(days=sup["LeadDays"]),
            "ReceivedDate": None, "CompletedDate": None, "OrderStatus": "Parked",
            "SupplierCode": sup["SupplierCode"], "Lines": lines, "Freight": 22.50, "SubTotal": 264.00,
        })
        self._sale(self.today, "WHSL-01", [("COF-ESP-5LB", 2, 120.00)], status="Placed")
        # A deleted adjustment: never moved stock and carries no change fields.
        self.stock_adjustments.append({
            "Guid": self._guid(), "AdjustmentNumber": self._next("SA"),
            "AdjustmentDate": self.today - dt.timedelta(days=2), "AdjustmentReason": "Entered in error",
            "Status": "Deleted",
            "Lines": [{"ProductCode": "MILK-WHL-GAL", "NewQuantity": 0, "NewActualValue": 0.0,
                       "Comments": "Wrong product"}],
        })

    # ------------------------------------------------------------ primitives

    def _move(self, day, kind, number, party, code, qty, value):
        self._qty[code] += qty
        self._value[code] += value
        if abs(self._qty[code]) < 1e-9:
            self._qty[code], self._value[code] = 0.0, 0.0
        ords, qtys, vals = self._snap[code]
        ords.append(day.toordinal())
        qtys.append(self._qty[code])
        vals.append(self._value[code])
        self.ledger.append({"Date": day, "Type": kind, "Number": number, "Party": party,
                            "ProductCode": code, "Quantity": qty, "Value": value,
                            "QtyAfter": self._qty[code], "ValueAfter": self._value[code]})

    def _next(self, prefix):
        self._seq[prefix] += 1
        return f"{prefix}-{self._seq[prefix]:08d}"

    def _guid(self):
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))


def _random_round(rng, x):
    base = int(x)
    return base + (1 if rng.random() < x - base else 0)


def _add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def _months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)
