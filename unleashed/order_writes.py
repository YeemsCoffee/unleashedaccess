"""Sales and purchase orders made, changed, completed and received from Core.

Unleashed stays the system of record; these write to it through the API, as documented at
apidocs.unleashedsoftware.com (SalesOrders, Purchases):

  Sales orders:    POST /SalesOrders/{guid} (created parked), PUT /SalesOrders/{guid} (header),
                   POST/PUT/DELETE /SalesOrders/{guid}/Lines[/{lineGuid}], POST .../Complete
                   (the stock goes out), DELETE /SalesOrders/{guid}.
  Purchase orders: the same under /PurchaseOrders, with POST .../Receipt receiving each line's
                   ReceiptQuantity (and PendingBatchNumbers) into stock, and .../Complete.

Unleashed leaves the arithmetic to the caller and checks it: LineTotal = round(quantity x
unit price, 2), LineTax = round(LineTotal x tax rate, 2), SubTotal, TaxTotal and Total their
sums. A field left out of a PUT is blanked, so a header change sends the order back as it is
with the changes made. Every change is read back: if Unleashed didn't keep it, OrderError
says so and nothing further is sent (an order is never completed or received unchecked).
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from .client import UnleashedError, body_date, writable

OPEN_SALES = ("Parked", "Placed", "Backordered")
OPEN_PURCHASES = ("Parked", "Placed")
RECEIVED_PURCHASES = ("Receipted", "Costed")
TOLERANCE = 0.0051  # Unleashed may round quantities to a product's decimal places


class OrderError(Exception):
    """Why a change to an order stopped, in words for the person making it."""


def money(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- taxes and currency

def _one(client, endpoint, field, code):
    items = client.get_all(endpoint, {field: code})
    key = "CustomerCode" if endpoint == "Customers" else "SupplierCode"
    found = next((i for i in items if (i.get(key) or "").lower() == (code or "").lower()), None)
    if found is None:
        raise OrderError(f"{code} isn't in Unleashed.")
    return found


def _tax(client, party, revenue):
    """{"code", "rate"}: the customer's (`revenue`) or supplier's tax, none if not taxable."""
    if party.get("Taxable") is False:
        return {"code": "NONE", "rate": 0.0}
    code, rate = party.get("TaxCode"), party.get("TaxRate")
    if code is None and rate is None:
        return {"code": "NONE", "rate": 0.0}
    if rate is None or code is None:
        side = "CanApplyToRevenue" if revenue else "CanApplyToExpenses"
        taxes = [t for t in client.get_all("Taxes") if not t.get("Obsolete") and t.get(side, True)]
        match = next((t for t in taxes if (code and t.get("TaxCode") == code)
                      or (code is None and rate is not None and abs((t.get("TaxRate") or 0) - rate) < 1e-9)), None)
        if match is None:
            raise OrderError(f"The tax for {party.get('CustomerCode') or party.get('SupplierCode')} "
                             f"({code or rate}) isn't in Unleashed's tax list.")
        code, rate = match.get("TaxCode"), match.get("TaxRate") or 0.0
    return {"code": code, "rate": float(rate)}


def _currency(client, party, who):
    """The party's currency code, if it's the account's base currency (or neither says)."""
    code = (party.get("Currency") or {}).get("CurrencyCode")
    companies = client.get("Companies")
    items = companies.get("Items") if isinstance(companies, dict) else companies
    base = ((items or [{}])[0] or {}).get("BaseCurrencyCode")
    if code and base and code != base:
        raise OrderError(f"{who} is billed in {code}, not {base}. Orders in another currency need an "
                         "exchange rate, so make them in Unleashed.")
    return code or base


def _priced(lines, rate, taxable):
    """Lines with LineTotal and LineTax, and the totals: (lines, sub, tax)."""
    out = []
    for l in lines:
        total = money(l["qty"] * l["unit_price"])
        tax = money(total * rate) if taxable(l["product_code"]) else 0.0
        out.append(dict(l, line_total=total, line_tax=tax))
    sub = money(sum(l["line_total"] for l in out))
    tax = money(sum(l["line_tax"] for l in out))
    return out, sub, tax


def _taxable(products, field):
    """Whether a product is taxed (Unleashed's TaxableSales / TaxablePurchase; yes if unsaid)."""
    flags = {p.get("ProductCode"): p.get(field) for p in products}
    return lambda code: flags.get(code) is not False


def _check_lines(lines):
    if not lines:
        raise OrderError("Add at least one product.")
    for l in lines:
        if not l.get("product_code"):
            raise OrderError("Choose a product for each line.")
        if not l["qty"] or l["qty"] <= 0:
            raise OrderError(f"Enter a quantity for {l['product_code']}.")
        if l["unit_price"] is None or l["unit_price"] < 0:
            raise OrderError(f"Enter a price for {l['product_code']} (0 or more).")


def _same_lines(doc_lines, wanted):
    """Whether Unleashed's lines are the ones wanted (product, quantity, price)."""
    def key(code, qty, price):
        return (code, round(float(qty or 0), 3), round(float(price or 0), 3))
    have = sorted(key((l.get("Product") or {}).get("ProductCode"), l.get("OrderQuantity"), l.get("UnitPrice"))
                  for l in doc_lines or [])
    want = sorted(key(l["product_code"], l["qty"], l["unit_price"]) for l in wanted)
    return len(have) == len(want) and all(
        h[0] == w[0] and abs(h[1] - w[1]) <= TOLERANCE and abs(h[2] - w[2]) <= 0.005 for h, w in zip(have, want))


def _status(doc):
    return doc.get("OrderStatus") or ""


def _get(client, endpoint, guid):
    client.clear_cache()
    try:
        doc = client.get(f"{endpoint}/{guid}")
    except UnleashedError as e:
        if e.status_code == 404:
            return None
        raise
    return doc if isinstance(doc, dict) and doc.get("Guid") else None


# ---------------------------------------------------------------- sales orders

def create_sales_order(client, order, products):
    """Make a parked sales order. `order`: {"customer", "warehouse", "order_date",
    "required_date", "reference", "comments", "lines": [{"product_code", "qty",
    "unit_price"}]}. Returns the order as Unleashed holds it."""
    _check_lines(order["lines"])
    customer = _one(client, "Customers", "customerCode", order["customer"])
    tax = _tax(client, customer, revenue=True)
    currency = _currency(client, customer, customer.get("CustomerName") or order["customer"])
    lines, sub, tax_total = _priced(order["lines"], tax["rate"], _taxable(products, "TaxableSales"))
    guid = str(uuid.uuid4())
    body = {
        "Guid": guid, "OrderStatus": "Parked", "OrderDate": body_date(order["order_date"]),
        "RequiredDate": body_date(order["required_date"]) if order.get("required_date") else None,
        "Customer": {"CustomerCode": customer["CustomerCode"]}, "Warehouse": {"WarehouseCode": order["warehouse"]},
        "Currency": {"CurrencyCode": currency}, "ExchangeRate": 1.0,
        "Tax": {"TaxCode": tax["code"], "TaxRate": tax["rate"]}, "TaxRate": tax["rate"],
        "CustomerRef": order.get("reference") or None, "Comments": order.get("comments") or None,
        "SalesOrderLines": [{"Guid": str(uuid.uuid4()), "LineNumber": i, "Product": {"ProductCode": l["product_code"]},
                             "OrderQuantity": l["qty"], "UnitPrice": l["unit_price"], "LineTotal": l["line_total"],
                             "LineTax": l["line_tax"], "DiscountRate": 0}
                            for i, l in enumerate(lines, 1)],
        "SubTotal": sub, "TaxTotal": tax_total, "Total": money(sub + tax_total),
    }
    client.post(f"SalesOrders/{guid}", body)
    made = _get(client, "SalesOrders", guid)
    if made is None:
        raise OrderError("Unleashed took the order, but reading it back failed. It shows under Sales after the "
                         "next sync; check it there before making it again.")
    if not _same_lines(made.get("SalesOrderLines"), order["lines"]):
        raise OrderError(f"Unleashed made {made.get('OrderNumber')} but its lines aren't as entered. Open it and "
                         "check them before completing it.")
    return made


def update_sales_order(client, guid, order, products):
    """Change an open sales order: its warehouse, dates, reference, comments and lines
    (`order` as for create, with each kept line's "guid"). The customer can't change."""
    _check_lines(order["lines"])
    current = _get(client, "SalesOrders", guid)
    if current is None:
        raise OrderError("That sales order isn't in Unleashed any more.")
    number = current.get("OrderNumber")
    if _status(current) not in OPEN_SALES:
        raise OrderError(f"{number} is {_status(current).lower()}, so it can't be changed.")
    customer = (current.get("Customer") or {}).get("CustomerCode")
    if order.get("customer") and order["customer"] != customer:
        raise OrderError("The customer of an order can't be changed. Delete it and make a new one.")
    rate = (current.get("Tax") or {}).get("TaxRate")
    rate = float(current.get("TaxRate") if rate is None else rate or 0)

    # The header: the order as it is, with the changes (a field left out is blanked).
    body = writable(current)
    body.update({"OrderDate": body_date(order["order_date"]),
                 "RequiredDate": body_date(order["required_date"]) if order.get("required_date") else None,
                 "Warehouse": {"WarehouseCode": order["warehouse"]},
                 "CustomerRef": order.get("reference") or None, "Comments": order.get("comments") or None})
    client.put(f"SalesOrders/{guid}", body)
    _change_lines(client, f"SalesOrders/{guid}", current.get("SalesOrderLines") or [], order["lines"], rate,
                  _taxable(products, "TaxableSales"), sales=True)
    changed = _get(client, "SalesOrders", guid)
    if changed is None or not _same_lines(changed.get("SalesOrderLines"), order["lines"]):
        raise OrderError(f"Unleashed didn't keep all the changes to {number}. Open it and check its lines.")
    return changed


def _change_lines(client, path, current, wanted, rate, taxable, sales):
    """Add, change and remove lines (each wanted line with "guid" when it's a current one):
    new lines first, so an order is never left without one."""
    by_guid = {l.get("Guid"): l for l in current if l.get("Guid")}
    # A line's product can't be changed: a line given another product replaces it.
    wanted = [dict(l, guid=None) if l.get("guid") in by_guid and
              (by_guid[l["guid"]].get("Product") or {}).get("ProductCode") != l["product_code"] else l
              for l in wanted]
    kept = {l["guid"] for l in wanted if l.get("guid")}
    unknown = kept - set(by_guid)
    if unknown:
        raise OrderError("A line changed in Unleashed since this page was opened. Reload it and try again.")
    priced, _, _ = _priced(wanted, rate, taxable)
    next_number = max([l.get("LineNumber") or 0 for l in current] or [0]) + 1
    for l in priced:
        if l.get("guid"):
            continue
        body = {"Guid": str(uuid.uuid4()), "LineNumber": next_number, "Product": {"ProductCode": l["product_code"]},
                "OrderQuantity": l["qty"], "UnitPrice": l["unit_price"], "LineTotal": l["line_total"],
                "LineTax": l["line_tax"], "DiscountRate": 0}
        next_number += 1
        client.post(f"{path}/Lines", body)
    for l in priced:
        old = by_guid.get(l.get("guid"))
        if old is None:
            continue
        if abs((old.get("OrderQuantity") or 0) - l["qty"]) <= TOLERANCE and \
                abs((old.get("UnitPrice") or 0) - l["unit_price"]) <= 0.00005:
            continue
        body = {"OrderQuantity": l["qty"], "UnitPrice": l["unit_price"], "LineTotal": l["line_total"],
                "LineTax": l["line_tax"], "DiscountRate": old.get("DiscountRate") or 0,
                "Comments": old.get("Comments")}
        if not sales:
            body["ReceiptQuantity"] = old.get("ReceiptQuantity")
        client.put(f"{path}/Lines/{l['guid']}", body)
    for line_guid in set(by_guid) - kept:
        client.delete(f"{path}/Lines/{line_guid}")


def complete_sales_order(client, guid, batches=None):
    """Complete an open sales order, so its stock goes out. `batches`: {line guid: [{"number",
    "qty"}]} for batch-tracked products, set on the lines first and read back. Returns the
    order as Unleashed holds it."""
    current = _get(client, "SalesOrders", guid)
    if current is None:
        raise OrderError("That sales order isn't in Unleashed any more.")
    number = current.get("OrderNumber")
    if _status(current) == "Completed":
        return current
    if _status(current) not in OPEN_SALES:
        raise OrderError(f"{number} is {_status(current).lower()}, so it can't be completed.")
    lines = {l.get("Guid"): l for l in current.get("SalesOrderLines") or []}
    for line_guid, chosen in (batches or {}).items():
        line = lines.get(line_guid)
        if line is None:
            raise OrderError(f"A line of {number} changed in Unleashed since this page was opened. Reload it.")
        code = (line.get("Product") or {}).get("ProductCode")
        if abs(sum(b["qty"] for b in chosen) - (line.get("OrderQuantity") or 0)) > TOLERANCE:
            raise OrderError(f"The batches for {code} add up to {sum(b['qty'] for b in chosen):g}, "
                             f"not the {line.get('OrderQuantity'):g} on the order.")
        client.put(f"SalesOrders/{guid}/Lines/{line_guid}", {
            "OrderQuantity": line.get("OrderQuantity"), "UnitPrice": line.get("UnitPrice"),
            "DiscountRate": line.get("DiscountRate") or 0, "Comments": line.get("Comments"),
            "BatchNumbers": [{"Number": b["number"], "Quantity": b["qty"]} for b in chosen]})
    if batches:
        ready = _get(client, "SalesOrders", guid) or {}
        for l in ready.get("SalesOrderLines") or []:
            chosen = (batches or {}).get(l.get("Guid"))
            if chosen and "BatchNumbers" in l and sorted(
                    (b.get("Number"), round(float(b.get("Quantity") or 0), 3)) for b in l.get("BatchNumbers") or []) != \
                    sorted((b["number"], round(b["qty"], 3)) for b in chosen):
                raise OrderError(f"Unleashed didn't keep the batches for {(l.get('Product') or {}).get('ProductCode')} "
                                 f"on {number}, so it wasn't completed. Check them and try again.")
    client.post(f"SalesOrders/{guid}/Complete", {})
    done = _get(client, "SalesOrders", guid) or {}
    if _status(done) != "Completed":
        raise OrderError(f"Unleashed didn't complete {number} (it's {_status(done).lower() or 'without a status'}). "
                         "Nothing left stock.")
    return done


def delete_sales_order(client, guid):
    current = _get(client, "SalesOrders", guid)
    if current is None or _status(current) == "Deleted":
        return
    if _status(current) != "Parked":
        raise OrderError(f"{current.get('OrderNumber')} is {_status(current).lower()}. Only a parked order can be "
                         "deleted.")
    client.delete(f"SalesOrders/{guid}")
    after = _get(client, "SalesOrders", guid)
    if after is not None and _status(after) != "Deleted":
        raise OrderError(f"Unleashed didn't delete {current.get('OrderNumber')}.")


# ---------------------------------------------------------------- purchase orders

def create_purchase_order(client, order, products, place=True):
    """Make a purchase order, placed (or parked). `order`: {"supplier", "warehouse",
    "order_date", "delivery_date", "reference", "comments", "lines": [{"product_code",
    "qty", "unit_price"}]}. Returns the order as Unleashed holds it."""
    _check_lines(order["lines"])
    supplier = _one(client, "Suppliers", "supplierCode", order["supplier"])
    tax = _tax(client, supplier, revenue=False)
    currency = _currency(client, supplier, supplier.get("SupplierName") or order["supplier"])
    lines, sub, tax_total = _priced(order["lines"], tax["rate"], _taxable(products, "TaxablePurchase"))
    guid = str(uuid.uuid4())
    body = {
        "Guid": guid, "OrderStatus": "Placed" if place else "Parked", "OrderDate": body_date(order["order_date"]),
        "DeliveryDate": body_date(order["delivery_date"]) if order.get("delivery_date") else None,
        "Supplier": {"SupplierCode": supplier["SupplierCode"]}, "Warehouse": {"WarehouseCode": order["warehouse"]},
        "Currency": {"CurrencyCode": currency}, "ExchangeRate": 1.0,
        "Tax": {"TaxCode": tax["code"], "TaxRate": tax["rate"]}, "TaxCode": tax["code"], "TaxRate": tax["rate"],
        "SupplierRef": order.get("reference") or None, "Comments": order.get("comments") or None,
        "PurchaseOrderLines": [{"Guid": str(uuid.uuid4()), "LineNumber": i, "Product": {"ProductCode": l["product_code"]},
                                "OrderQuantity": l["qty"], "UnitPrice": l["unit_price"], "LineTotal": l["line_total"],
                                "LineTax": l["line_tax"], "DiscountRate": 0}
                               for i, l in enumerate(lines, 1)],
        "SubTotal": sub, "TaxTotal": tax_total, "Total": money(sub + tax_total),
    }
    client.post(f"PurchaseOrders/{guid}", body)
    made = _get(client, "PurchaseOrders", guid)
    if made is None:
        raise OrderError("Unleashed took the purchase order, but reading it back failed. It shows under Purchases "
                         "after the next sync; check it there before making it again.")
    if not _same_lines(made.get("PurchaseOrderLines"), order["lines"]):
        raise OrderError(f"Unleashed made {made.get('OrderNumber')} but its lines aren't as entered. Open it and "
                         "check them before receiving it.")
    return made


def update_purchase_order(client, guid, order, products, place=None):
    """Change an open purchase order (as update_sales_order). `place`: True places a parked
    one, None leaves its status."""
    _check_lines(order["lines"])
    current = _get(client, "PurchaseOrders", guid)
    if current is None:
        raise OrderError("That purchase order isn't in Unleashed any more.")
    number = current.get("OrderNumber")
    if _status(current) not in OPEN_PURCHASES:
        raise OrderError(f"{number} is {_status(current).lower()}, so it can't be changed.")
    supplier = (current.get("Supplier") or {}).get("SupplierCode")
    if order.get("supplier") and order["supplier"] != supplier:
        raise OrderError("The supplier of a purchase order can't be changed. Delete it and make a new one.")
    rate = (current.get("Tax") or {}).get("TaxRate")
    rate = float(current.get("TaxRate") if rate is None else rate or 0)
    body = writable(current)
    body.update({"OrderDate": body_date(order["order_date"]),
                 "DeliveryDate": body_date(order["delivery_date"]) if order.get("delivery_date") else None,
                 "Warehouse": {"WarehouseCode": order["warehouse"]},
                 "SupplierRef": order.get("reference") or None, "Comments": order.get("comments") or None})
    if place:
        body["OrderStatus"] = "Placed"
    client.put(f"PurchaseOrders/{guid}", body)
    _change_lines(client, f"PurchaseOrders/{guid}", current.get("PurchaseOrderLines") or [], order["lines"], rate,
                  _taxable(products, "TaxablePurchase"), sales=False)
    changed = _get(client, "PurchaseOrders", guid)
    if changed is None or not _same_lines(changed.get("PurchaseOrderLines"), order["lines"]):
        raise OrderError(f"Unleashed didn't keep all the changes to {number}. Open it and check its lines.")
    return changed


def receive_purchase_order(client, guid, receipts, complete=False):
    """Receive a purchase order's delivery into stock. `receipts`: {line guid: {"qty": total
    received, "batches": [{"number", "qty", "expiry"}]}} (batches for batch-tracked
    products). The quantities are set on the lines and read back before the receipt is
    posted. `complete`: also complete it (once every line is received in full)."""
    current = _get(client, "PurchaseOrders", guid)
    if current is None:
        raise OrderError("That purchase order isn't in Unleashed any more.")
    number = current.get("OrderNumber")
    if _status(current) not in OPEN_PURCHASES:
        raise OrderError(f"{number} is {_status(current).lower()}, so it can't be received.")
    lines = {l.get("Guid"): l for l in current.get("PurchaseOrderLines") or []}
    if not any(r["qty"] > 0 for r in receipts.values()):
        raise OrderError("Enter how much of at least one product arrived.")
    if _status(current) == "Parked":  # a parked order is placed before it's received
        body = writable(current)
        body["OrderStatus"] = "Placed"
        client.put(f"PurchaseOrders/{guid}", body)
    for line_guid, r in receipts.items():
        line = lines.get(line_guid)
        if line is None:
            raise OrderError(f"A line of {number} changed in Unleashed since this page was opened. Reload it.")
        code = (line.get("Product") or {}).get("ProductCode")
        if r["qty"] < 0 or r["qty"] > (line.get("OrderQuantity") or 0) + TOLERANCE:
            raise OrderError(f"{code}: receive between 0 and the {line.get('OrderQuantity'):g} ordered. "
                             "To receive more, change the order first.")
        if r.get("batches") and abs(sum(b["qty"] for b in r["batches"]) - r["qty"]) > TOLERANCE:
            raise OrderError(f"The batches for {code} add up to {sum(b['qty'] for b in r['batches']):g}, "
                             f"not the {r['qty']:g} received.")
        body = {"OrderQuantity": line.get("OrderQuantity"), "UnitPrice": line.get("UnitPrice"),
                "LineTotal": line.get("LineTotal"), "LineTax": line.get("LineTax") or 0,
                "DiscountRate": line.get("DiscountRate") or 0, "Comments": line.get("Comments"),
                "ReceiptQuantity": r["qty"]}
        if r.get("batches"):
            body["PendingBatchNumbers"] = [{"Number": b["number"], "Quantity": b["qty"],
                                            "ExpiryDate": body_date(b["expiry"]) if b.get("expiry") else None}
                                           for b in r["batches"]]
        client.put(f"PurchaseOrders/{guid}/Lines/{line_guid}", body)
    ready = _get(client, "PurchaseOrders", guid) or {}
    for l in ready.get("PurchaseOrderLines") or []:
        r = receipts.get(l.get("Guid"))
        if r and abs((l.get("ReceiptQuantity") or 0) - r["qty"]) > TOLERANCE:
            raise OrderError(f"Unleashed didn't keep the quantity received for "
                             f"{(l.get('Product') or {}).get('ProductCode')} on {number}, so nothing was received. "
                             "Check it and try again.")
    client.post(f"PurchaseOrders/{guid}/Receipt", {})
    received = _get(client, "PurchaseOrders", guid) or {}
    if _status(received) not in RECEIVED_PURCHASES + ("Complete",):
        raise OrderError(f"Unleashed didn't receive {number} (it's {_status(received).lower() or 'without a status'}).")
    if complete:
        return complete_purchase_order(client, guid)
    return received


def complete_purchase_order(client, guid):
    current = _get(client, "PurchaseOrders", guid)
    if current is None:
        raise OrderError("That purchase order isn't in Unleashed any more.")
    if _status(current) == "Complete":
        return current
    if _status(current) not in RECEIVED_PURCHASES:
        raise OrderError(f"{current.get('OrderNumber')} is {_status(current).lower()}: receive it before completing it.")
    client.post(f"PurchaseOrders/{guid}/Complete", {})
    done = _get(client, "PurchaseOrders", guid) or {}
    if _status(done) != "Complete":
        raise OrderError(f"Unleashed didn't complete {current.get('OrderNumber')} "
                         f"(it's {_status(done).lower() or 'without a status'}).")
    return done


def delete_purchase_order(client, guid):
    current = _get(client, "PurchaseOrders", guid)
    if current is None or _status(current) == "Deleted":
        return
    if _status(current) not in OPEN_PURCHASES:
        raise OrderError(f"{current.get('OrderNumber')} is {_status(current).lower()}, so it can't be deleted.")
    client.delete(f"PurchaseOrders/{guid}")
    after = _get(client, "PurchaseOrders", guid)
    if after is not None and _status(after) != "Deleted":
        raise OrderError(f"Unleashed didn't delete {current.get('OrderNumber')}.")
