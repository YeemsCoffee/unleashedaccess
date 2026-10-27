"""Production: Unleashed assemblies (production orders) and bills of materials,
organised like an ERP's production module.

- Production orders: assemblies by date and status. Each order's components,
  planned (from its bill of materials) against actually used, give its material
  variance.
- Bills of materials: each recipe's standard cost at current average costs, how
  many could be made from stock now, and where a component is used.
- Material requirements: what open orders need, checked against available stock
  in due-date order (first come, first served), with shortages before and after
  incoming purchase orders.
- Usage: components used and wasted, and products made, over a period.
- Yield, two separate measures:
  - each material's yield: what the bill of materials calls for to make the quantity
    actually made ÷ what was used of that material (per order, and per component over a
    period);
  - the finished product's yield: quantity made ÷ quantity planned. Unleashed keeps only
    the quantity made, so planned quantities come from the caller (orders completed in
    this app record them), as `planned`: {assembly guid: quantity planned}.

Everything comes from Reports, so it reads the stored copy or the live API alike.
"""
import datetime as dt
from collections import defaultdict

from .reports import month_end, month_start


class Production:
    def __init__(self, reports):
        self.reports = reports

    @property
    def today(self):
        return self.reports.today

    # ------------------------------------------------------------ bills of materials

    def boms(self, component=None):
        """Bills of materials (optionally only those using `component`), with standard
        cost, what could be made from stock now, and when each product was last made."""
        boms = self.reports.bills_of_materials()
        if component:
            boms = [b for b in boms if any(l["product_code"] == component for l in b["lines"])]
        stock = self._stock()
        last_made = {}
        for a in self.reports.assemblies():
            if a["completed"]:
                code = a["product_code"]
                last_made[code] = max(last_made.get(code, a["completed_date"]), a["completed_date"])
        for b in boms:
            b["buildable"] = _buildable(b, stock)
            b["last_made"] = last_made.get(b["product_code"])
        return sorted(boms, key=lambda b: (b["obsolete"], b["product_code"] or "", b["number"] or ""))

    def bom(self, number, planned=None):
        bom = next((b for b in self.reports.bills_of_materials() if b["number"] == number), None)
        if bom is None:
            return None
        stock = self._stock()
        for l in bom["lines"]:
            l["available"] = stock.get(l["product_code"], {}).get("available", 0)
            l["supports"] = l["available"] / l["per_unit"] if l["per_unit"] else None
        bom["buildable"] = _buildable(bom, stock)
        orders = [a for a in self.reports.assemblies(product=bom["product_code"])
                  if a["open"] or a["completed"]]
        bom["orders"] = _with_yield(sorted(orders, key=_newest_first, reverse=True)[:15], planned)
        return bom

    def preview(self, product_code, qty):
        """What making `qty` of a product needs, from its bill of materials, against available
        stock (ignoring other open orders); None without a bill of materials."""
        bom = bom_for(product_code, self.reports.bills_of_materials())
        if bom is None:
            return None
        stock = self._stock()
        rows = []
        for l in bom["lines"]:
            need = l["per_unit"] * qty
            available = stock.get(l["product_code"], {}).get("available", 0)
            rows.append({"product_code": l["product_code"], "description": l["description"],
                         "need": need, "wastage": l["wastage"] * qty, "available": available,
                         "short": max(need - max(available, 0), 0), "cost": l["unit_cost"] * need})
        return {"bom": bom, "rows": rows, "cost": sum(r["cost"] for r in rows),
                "short": any(r["short"] > 1e-9 for r in rows)}

    # ------------------------------------------------------------ production orders

    def orders(self, start, end, status=None, product=None, planned=None):
        """Production order enquiry, by assembly date. `status`: Open, or an Unleashed status."""
        orders = _with_yield(self.reports.assemblies(start, end, product=product), planned)
        if status == "Open":
            orders = [o for o in orders if o["open"]]
        elif status:
            orders = [o for o in orders if o["status"].lower() == status.lower()]
        else:
            orders = [o for o in orders if o["open"] or o["completed"]]
        done = [o for o in orders if o["completed"]]
        made = _made_by_product(done, self.reports.bills_of_materials())
        return {
            "orders": sorted(orders, key=_newest_first, reverse=True), "completed": len(done),
            "open": sum(1 for o in orders if o["open"]),
            "qty_made": sum(o["qty"] for o in done), "cost": sum(o["cost"] for o in done),
            "by_product": made, "estimated": any(o["estimated"] for o in done),
            "yield": _overall_yield(done),
        }

    def order(self, number, planned=None):
        """One production order: components planned against used, and availability if open."""
        order = self.reports.assembly(number)
        if order is None:
            return None
        _with_yield([order], planned)
        bom = bom_for(order["product_code"], self.reports.bills_of_materials())
        planned = {l["product_code"]: l["per_unit"] * order["qty"] for l in bom["lines"]} if bom else {}
        stock = self._stock() if order["open"] else {}
        components = []
        for n in _needs(order, bom):
            code = n["product_code"]
            plan = planned.get(code, 0) if bom else None
            row = {**n, "planned": plan, "variance": None, "variance_cost": None, "yield": None}
            if order["completed"] and plan is not None:
                row["variance"] = round(n["consumed"] - plan, 6)  # no float dust as "-0.00"
                row["variance_cost"] = row["variance"] * n["unit_cost"]
                row["yield"] = plan / n["consumed"] if n["consumed"] else None
            if order["open"]:
                row["available"] = stock.get(code, {}).get("available", 0)
                row["short"] = max(n["consumed"] - max(row["available"], 0), 0)
            components.append(row)
        used = {c["product_code"] for c in components}
        for l in (bom["lines"] if bom and order["completed"] else []):
            if l["product_code"] not in used:  # on the recipe but not used at all
                plan = l["per_unit"] * order["qty"]
                components.append({"product_code": l["product_code"], "description": l["description"],
                                   "qty": 0, "wastage": 0, "consumed": 0, "unit_cost": l["unit_cost"],
                                   "cost": 0, "estimated": False, "planned": plan, "variance": -plan,
                                   "variance_cost": -plan * l["unit_cost"], "yield": None})
        order.update(
            components=components, bom=bom,
            standard_cost=bom["unit_cost"] * order["qty"] if bom else None,
            material_variance=sum(c["variance_cost"] or 0 for c in components) if order["completed"] else None,
            short=any(c.get("short") for c in components))
        return order

    # ------------------------------------------------------------ material requirements

    def requirements(self, through=None):
        """What open production orders need, against available stock and incoming purchases.

        Orders take stock in due-date order, so an order is Ready only if what's left after
        earlier orders covers it. `through` limits it to orders due by that date.
        """
        boms = self.reports.bills_of_materials()
        made_here = {b["product_code"] for b in boms if not b["obsolete"]}
        orders = [a for a in self.reports.assemblies()
                  if a["open"] and (through is None or a["due"] is None or a["due"] <= through)]
        orders.sort(key=lambda o: (o["due"] or dt.date.max, o["number"] or ""))
        stock = self._stock()
        left = {code: s["available"] for code, s in stock.items()}
        components = {}
        for o in orders:
            needs = _needs(o, bom_for(o["product_code"], boms))
            o["shortages"] = []
            for n in needs:
                code = n["product_code"]
                covered = min(max(left.get(code, 0), 0), n["consumed"])
                left[code] = left.get(code, 0) - n["consumed"]
                if n["consumed"] - covered > 1e-9:
                    o["shortages"].append({**n, "missing": n["consumed"] - covered})
                c = components.setdefault(code, {
                    "product_code": code, "description": n["description"], "required": 0,
                    "orders": 0, "unit_cost": n["unit_cost"], "made_here": code in made_here})
                c["required"] += n["consumed"]
                c["orders"] += 1
            o["ready"] = not o["shortages"]
            o["overdue"] = o["due"] is not None and o["due"] < self.today
            o["planned_cost"] = sum(n["cost"] for n in needs)
        for code, c in components.items():
            s = stock.get(code, {})
            c["on_hand"] = s.get("qty", 0)
            c["available"] = s.get("available", 0)
            c["on_purchase"] = s.get("on_purchase", 0)
            c["shortage"] = max(c["required"] - max(c["available"], 0), 0)
            c["shortage_after_po"] = max(c["shortage"] - c["on_purchase"], 0)
            c["shortage_value"] = c["shortage_after_po"] * c["unit_cost"]
        return {
            "through": through, "orders": orders,
            # Short even after incoming purchases first, then short until they arrive.
            "components": sorted(components.values(), key=lambda c: (
                0 if c["shortage_after_po"] else 1 if c["shortage"] else 2, c["product_code"] or "")),
            "short_orders": sum(1 for o in orders if not o["ready"]),
            "overdue": sum(1 for o in orders if o["overdue"]),
            "planned_cost": sum(o["planned_cost"] for o in orders),
            "shortage_value": sum(c["shortage_value"] for c in components.values()),
        }

    # ------------------------------------------------------------ usage

    def usage(self, start, end, planned=None):
        """Components used and wasted, and products made, by completed orders start..end."""
        done = _with_yield([a for a in self.reports.assemblies(start, end)
                            if a["completed"] and start <= a["completed_date"] <= end], planned)
        boms = self.reports.bills_of_materials()
        components = defaultdict(lambda: {"description": "", "qty": 0, "wastage": 0, "consumed": 0,
                                          "cost": 0, "wastage_cost": 0, "orders": 0, "recipe": 0,
                                          "recipe_used": 0})
        by_day = defaultdict(float)
        for a in done:
            by_day[a["completed_date"]] += a["cost"]
            bom = bom_for(a["product_code"], boms)
            per_unit = {l["product_code"]: l["per_unit"] for l in bom["lines"]} if bom else {}
            for l in a["lines"]:
                c = components[l["product_code"]]
                c["description"] = l["description"]
                c["orders"] += 1
                for k in ("qty", "wastage", "consumed", "cost"):
                    c[k] += l[k]
                c["wastage_cost"] += l["wastage"] * l["unit_cost"]
                if l["product_code"] in per_unit:  # what the recipe called for, for what was made
                    c["recipe"] += per_unit[l["product_code"]] * a["qty"]
                    c["recipe_used"] += l["consumed"]
        for c in components.values():
            c["yield"] = c["recipe"] / c["recipe_used"] if c["recipe_used"] else None
        cost = sum(a["cost"] for a in done)
        wastage_cost = sum(c["wastage_cost"] for c in components.values())
        return {
            "start": start, "end": end, "orders": sorted(done, key=_newest_first, reverse=True),
            "components": sorted(({"product_code": k, **v} for k, v in components.items()),
                                 key=lambda c: -c["cost"]),
            "products": _made_by_product(done, boms),
            "qty_made": sum(a["qty"] for a in done), "cost": cost, "wastage_cost": wastage_cost,
            "wastage_pct": wastage_cost / cost if cost else None, "by_day": dict(by_day),
            "estimated": any(a["estimated"] for a in done), "yield": _overall_yield(done),
        }

    def overview(self, month, planned=None):
        start = month_start(month)
        through = min(month_end(start), self.today)
        return {"month": start, "through": through, "usage": self.usage(start, through, planned),
                "requirements": self.requirements()}

    # ------------------------------------------------------------ helpers

    def _stock(self):
        return {r["product_code"]: r for r in self.reports.stock_on_hand()["rows"]}


def bom_for(product_code, boms):
    """The bill of materials that makes `product_code`: a current one if there is one."""
    matches = [b for b in boms if b["product_code"] == product_code]
    return next((b for b in matches if not b["obsolete"]), matches[0] if matches else None)


def _needs(order, bom):
    """An order's components: its own lines, or its bill of materials when it has none."""
    if order["lines"]:
        return order["lines"]
    if not bom:
        return []
    return [{"product_code": l["product_code"], "description": l["description"],
             "qty": l["qty"] * order["qty"], "wastage": l["wastage"] * order["qty"],
             "consumed": l["per_unit"] * order["qty"], "unit_cost": l["unit_cost"],
             "cost": l["cost"] * order["qty"], "estimated": True} for l in bom["lines"]]


def _buildable(bom, stock):
    """Whole units that available stock could make now (None for an empty recipe)."""
    counts = [max(stock.get(l["product_code"], {}).get("available", 0), 0) / l["per_unit"]
              for l in bom["lines"] if l["per_unit"] > 0]
    return int(min(counts) + 1e-9) if counts else None


def _with_yield(orders, planned=None):
    """Adds "planned_qty" and the finished product's "yield" (made ÷ planned) to completed
    orders whose plan is known."""
    for o in orders:
        plan = (planned or {}).get(o["guid"]) if o["completed"] else None
        o["planned_qty"] = plan
        o["yield"] = o["qty"] / plan if plan else None
    return orders


def _overall_yield(done):
    """The finished product's yield over several orders: made ÷ planned, where planned is known."""
    known = [o for o in done if o.get("planned_qty")]
    planned = sum(o["planned_qty"] for o in known)
    return sum(o["qty"] for o in known) / planned if planned else None


def _made_by_product(done, boms):
    made = defaultdict(lambda: {"description": "", "orders": 0, "qty": 0, "cost": 0, "done": []})
    for a in done:
        m = made[a["product_code"]]
        m["description"] = a["description"]
        m["orders"] += 1
        m["qty"] += a["qty"]
        m["cost"] += a["cost"]
        m["done"].append(a)
    rows = []
    for code, m in made.items():
        bom = bom_for(code, boms)
        orders = m.pop("done")
        unit = m["cost"] / m["qty"] if m["qty"] else 0
        standard = bom["unit_cost"] if bom else None
        rows.append({"product_code": code, **m, "unit_cost": unit, "standard_unit_cost": standard,
                     "variance_pct": unit / standard - 1 if standard else None,
                     "yield": _overall_yield(orders)})
    return sorted(rows, key=lambda r: -r["cost"])


def _newest_first(order):  # sort key; use with reverse=True
    return (order["date"] or dt.date.min, order["number"] or "")
