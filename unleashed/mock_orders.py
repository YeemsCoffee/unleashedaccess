"""Sales and purchase order writes on the mock Unleashed server.

Follows Unleashed's API reference (apidocs.unleashedsoftware.com, SalesOrders and Purchases):

    POST   /SalesOrders/{guid}                    create (OrderStatus Parked, or Completed)
    PUT    /SalesOrders/{guid}                    change the header of an open order
    POST   /SalesOrders/{guid}/Lines              add a line
    PUT    /SalesOrders/{guid}/Lines/{lineGuid}   change a line (quantity, price, batches)
    DELETE /SalesOrders/{guid}/Lines/{lineGuid}   remove a line
    POST   /SalesOrders/{guid}/Complete           complete: the stock goes out
    DELETE /SalesOrders/{guid}                    delete a parked order

and the same for /PurchaseOrders, with POST /PurchaseOrders/{guid}/Receipt receiving each
line's ReceiptQuantity (and its PendingBatchNumbers) into stock, and /Complete finishing it.

Unleashed leaves the arithmetic to the caller and checks it: LineTotal = round(qty x price,
2), LineTax = round(LineTotal x tax rate, 2), SubTotal and TaxTotal their sums. The mock
checks the same. What the reference doesn't say (which statuses can be changed, what a
receipt does to a line received in part) is assumed as the web app behaves, and marked.
"""
import datetime as dt
import uuid
from decimal import ROUND_HALF_UP, Decimal

from flask import jsonify, request

OPEN_SO = ("Parked", "Placed", "Backordered")
OPEN_PO = ("Parked", "Placed")
TAXES = [
    {"TaxCode": "NONE", "Description": "No Tax", "TaxRate": 0.0, "CanApplyToExpenses": True,
     "CanApplyToRevenue": True, "Obsolete": False},
    {"TaxCode": "CA-SALES", "Description": "California sales tax", "TaxRate": 0.095,
     "CanApplyToExpenses": False, "CanApplyToRevenue": True, "Obsolete": False},
]


def money(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _error(text, status=400):
    return jsonify({"description": text}), status


def register(app, ds, serialize_so, serialize_po, body_date, ms_date_in):
    """Add the order write endpoints to the mock `app`, over dataset `ds`."""

    def find(items, guid):
        return next((d for d in items if d["Guid"].lower() == guid.lower()), None)

    def body():
        b = request.get_json(silent=True) or {}
        bad = ms_date_in(b)
        return b, (_error(f"{bad} is not a valid value for DateTime.") if bad else None)

    def tax_of(b):
        """(code, rate) from an order's Tax / TaxCode / TaxRate."""
        given = b.get("Tax") or {}
        code = b.get("TaxCode") or given.get("TaxCode")
        known = next((t for t in TAXES if t["TaxCode"] == code), None)
        if code and not known:
            return None
        rate = given.get("TaxRate", b.get("TaxRate"))
        rate = known["TaxRate"] if rate is None and known else rate
        return (code or "NONE", float(rate or 0))

    def warehouse_of(b):
        code = (b.get("Warehouse") or {}).get("WarehouseCode")
        if code is None:
            return next(w["WarehouseCode"] for w in ds.warehouses if w["IsDefault"])
        return code if any(w["WarehouseCode"] == code for w in ds.warehouses) else None

    def check_line(given, rate):
        """(line, error): a line as given, with Unleashed's arithmetic checked."""
        code = (given.get("Product") or {}).get("ProductCode")
        if code not in ds.products:
            return None, f"Product {code} not found"
        qty, price = given.get("OrderQuantity"), given.get("UnitPrice")
        if qty is None or price is None:
            return None, f"OrderQuantity and UnitPrice are required ({code})"
        qty, price = float(qty), float(price)
        total = money(qty * price)
        if given.get("LineTotal") is not None and abs(float(given["LineTotal"]) - total) > 0.001:
            return None, f"LineTotal for {code} should be {total:.2f}"
        tax = money(total * rate) if given.get("LineTax") is None else float(given["LineTax"])
        if abs(tax - money(total * rate)) > 0.001 and tax != 0:
            return None, f"LineTax for {code} should be {money(total * rate):.2f}"
        return {"Guid": given.get("Guid") or str(uuid.uuid4()), "ProductCode": code, "OrderQuantity": qty,
                "UnitPrice": price, "LineTotal": total, "LineTax": tax,
                "Comments": given.get("Comments")}, None

    def totals(doc, b=None):
        sub = money(sum(l["LineTotal"] for l in doc["Lines"]))
        tax = money(sum(l.get("LineTax") or 0 for l in doc["Lines"]))
        if b is not None:
            for field, want in (("SubTotal", sub), ("TaxTotal", tax), ("Total", money(sub + tax))):
                if b.get(field) is not None and abs(float(b[field]) - want) > 0.001:
                    return f"{field} should be {want:.2f}"
        doc["SubTotal"], doc["TaxTotal"] = sub, tax
        return None

    def batches_in(given, field="BatchNumbers"):
        out = []
        for x in given.get(field) or []:
            number = (x.get("Number") or "").strip()
            if not number or x.get("Quantity") is None:
                return None
            out.append({"Number": number, "Quantity": float(x["Quantity"]),
                        "ExpiryDate": body_date(x.get("ExpiryDate")) if x.get("ExpiryDate") else None})
        return out

    # ------------------------------------------------------------ reference

    @app.get("/Taxes")
    @app.get("/Taxes/<int:page>")
    def taxes(page=1):
        return jsonify({"Pagination": {"NumberOfItems": len(TAXES), "PageSize": 200, "PageNumber": 1,
                                       "NumberOfPages": 1}, "Items": TAXES})

    @app.get("/Companies")
    def companies():
        return jsonify({"Items": [{"CompanyName": "Mock Café", "BaseCurrencyCode": "USD", "DefaultTaxRate": 0.0}]})

    @app.get("/BatchNumbers")
    @app.get("/BatchNumbers/<int:page>")
    def batch_numbers(page=1):
        code, warehouse = request.args.get("ProductCode"), request.args.get("WarehouseCode")
        available = request.args.get("available", "true").lower() != "false"
        items = [{"Number": b["Number"], "ProductCode": b["ProductCode"], "WarehouseCode": b["WarehouseCode"],
                  "Quantity": b["Quantity"], "OriginalQty": b["OriginalQty"], "Guid": b["Guid"],
                  "ExpiryDate": serialize_date(b["ExpiryDate"]),
                  "Status": "Available" if b["Quantity"] > 0 else "Used"}
                 for b in ds.batches
                 if (not code or b["ProductCode"].lower() == code.lower())
                 and (not warehouse or b["WarehouseCode"] == warehouse)
                 and (not available or b["Quantity"] > 0)]
        return jsonify({"Pagination": {"NumberOfItems": len(items), "PageSize": max(len(items), 1),
                                       "PageNumber": 1, "NumberOfPages": 1}, "Items": items})

    # ------------------------------------------------------------ sales orders

    @app.post("/SalesOrders")
    @app.post("/SalesOrders/<guid>")
    def create_sales_order(guid=None):
        b, bad = body()
        if bad:
            return bad
        if guid and find(ds.sales_orders, guid):
            return _error("A sales order with this Guid already exists")
        customer = (b.get("Customer") or {}).get("CustomerCode")
        if customer not in ds.customers:
            return _error(f"Customer {customer} not found")
        warehouse, tax = warehouse_of(b), tax_of(b)
        if warehouse is None:
            return _error("Warehouse not found")
        if tax is None:
            return _error("Tax not found")
        if b.get("ExchangeRate") is None:
            return _error("ExchangeRate is required")
        status = b.get("OrderStatus")
        if status not in ("Parked", "Completed"):
            return _error("OrderStatus must be Parked or Completed")
        if not b.get("SalesOrderLines"):
            return _error("At least one sales order line is required")
        so = {"Guid": guid or b.get("Guid") or str(uuid.uuid4()), "OrderNumber": b.get("OrderNumber") or ds.next_number("SO"),
              "OrderDate": body_date(b.get("OrderDate")) or ds.today, "RequiredDate": body_date(b.get("RequiredDate")),
              "CompletedDate": None, "OrderStatus": "Parked", "CustomerCode": customer, "WarehouseCode": warehouse,
              "Tax": {"TaxCode": tax[0], "TaxRate": tax[1]}, "Comments": b.get("Comments"),
              "CustomerRef": b.get("CustomerRef"), "Lines": [], "Modified": dt.date.today()}
        for given in b["SalesOrderLines"]:
            line, error = check_line(given, tax[1])
            if error:
                return _error(error)
            line["BatchNumbers"] = batches_in(given) or []
            so["Lines"].append(line)
        error = totals(so, b)
        if error:
            return _error(error)
        ds.sales_orders.append(so)
        if status == "Completed" and ds.sell(so):  # couldn't complete: stays parked (API FAQ)
            pass
        return jsonify(serialize_so(ds, so)), 201

    @app.put("/SalesOrders/<guid>")
    def update_sales_order(guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] not in OPEN_SO:  # assumed: as in the web app
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']} and can't be changed")
        b, bad = body()
        if bad:
            return bad
        if b.get("OrderStatus") not in OPEN_SO:
            return _error("OrderStatus is required: Parked or Placed")
        tax, warehouse = tax_of(b), warehouse_of(b)
        if tax is None or warehouse is None:
            return _error("Tax and Warehouse must exist")
        # A blank editable field overwrites the old value (Unleashed's reference).
        so.update({"OrderStatus": b["OrderStatus"], "OrderDate": body_date(b.get("OrderDate")) or so["OrderDate"],
                   "RequiredDate": body_date(b.get("RequiredDate")), "Comments": b.get("Comments"),
                   "CustomerRef": b.get("CustomerRef"), "WarehouseCode": warehouse,
                   "Tax": {"TaxCode": tax[0], "TaxRate": tax[1]}, "Modified": dt.date.today()})
        totals(so)
        return jsonify(serialize_so(ds, so))

    @app.post("/SalesOrders/<guid>/Lines")
    def add_sales_line(guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] not in OPEN_SO:
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']} and can't be changed")
        b, bad = body()
        if bad:
            return bad
        if not b.get("Guid"):
            return _error("Guid is required")
        line, error = check_line(b, so["Tax"]["TaxRate"])
        if error:
            return _error(error)
        line["BatchNumbers"] = batches_in(b) or []
        so["Lines"].append(line)
        totals(so)
        so["Modified"] = dt.date.today()
        return jsonify(serialize_so(ds, so)), 201

    @app.put("/SalesOrders/<guid>/Lines/<line_guid>")
    def update_sales_line(guid, line_guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] not in OPEN_SO:
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']} and can't be changed")
        line = find(ds.lines_with_guids(so), line_guid)
        if line is None:
            return _error("Sales order line not found", 404)
        b, bad = body()
        if bad:
            return bad
        given = dict(b, Product={"ProductCode": line["ProductCode"]})
        given.setdefault("UnitPrice", line["UnitPrice"])
        changed, error = check_line(given, so["Tax"]["TaxRate"])
        if error:
            return _error(error)
        batches = batches_in(b)
        if batches is None:
            return _error("Each batch needs a Number and a Quantity")
        line.update({k: changed[k] for k in ("OrderQuantity", "UnitPrice", "LineTotal", "LineTax", "Comments")})
        if "BatchNumbers" in b:
            line["BatchNumbers"] = batches
        totals(so)
        so["Modified"] = dt.date.today()
        return jsonify(serialize_so(ds, so))

    @app.delete("/SalesOrders/<guid>/Lines/<line_guid>")
    def delete_sales_line(guid, line_guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] not in OPEN_SO:
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']} and can't be changed")
        line = find(ds.lines_with_guids(so), line_guid)
        if line is None:
            return _error("Sales order line not found", 404)
        if len(so["Lines"]) == 1:
            return _error("A sales order needs at least one line")
        so["Lines"].remove(line)
        totals(so)
        so["Modified"] = dt.date.today()
        return "", 204

    @app.post("/SalesOrders/<guid>/Complete")
    def complete_sales_order(guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] not in OPEN_SO:
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']}")
        problem = ds.sell(so)
        if problem:
            return _error(problem)
        return jsonify(serialize_so(ds, so))

    @app.delete("/SalesOrders/<guid>")
    def delete_sales_order(guid):
        so = find(ds.sales_orders, guid)
        if so is None:
            return _error("Sales order not found", 404)
        if so["OrderStatus"] != "Parked":  # assumed: only parked orders can be deleted
            return _error(f"Sales order {so['OrderNumber']} is {so['OrderStatus']}; only parked orders can be deleted")
        so["OrderStatus"], so["Modified"] = "Deleted", dt.date.today()
        return "", 204

    # ------------------------------------------------------------ purchase orders

    @app.post("/PurchaseOrders")
    @app.post("/PurchaseOrders/<guid>")
    def create_purchase_order(guid=None):
        b, bad = body()
        if bad:
            return bad
        supplier = (b.get("Supplier") or {}).get("SupplierCode")
        if supplier not in ds.suppliers:
            return _error(f"Supplier {supplier} not found")
        status = b.get("OrderStatus")
        if status not in ("Parked", "Placed", "Complete"):
            return _error("OrderStatus must be Parked, Placed or Complete")
        warehouse, tax = warehouse_of(b), tax_of(b)
        if warehouse is None or tax is None:
            return _error("Warehouse and Tax must exist")
        lines = b.get("PurchaseOrderLines") or []
        if status != "Parked" and not lines:
            return _error("At least one purchase order line is required")
        po = {"Guid": guid or b.get("Guid") or str(uuid.uuid4()), "OrderNumber": b.get("OrderNumber") or ds.next_number("PO"),
              "OrderDate": body_date(b.get("OrderDate")) or ds.today, "RequiredDate": body_date(b.get("DeliveryDate")),
              "ReceivedDate": None, "CompletedDate": None, "OrderStatus": status, "SupplierCode": supplier,
              "WarehouseCode": warehouse, "Tax": {"TaxCode": tax[0], "TaxRate": tax[1]}, "Comments": b.get("Comments"),
              "SupplierRef": b.get("SupplierRef"), "Freight": 0.0, "Lines": [], "Modified": dt.date.today()}
        for given in lines:
            if given.get("LineNumber") is None:
                return _error("LineNumber is required")
            line, error = check_line(given, tax[1])
            if error:
                return _error(error)
            line["ReceiptQuantity"] = float(given["ReceiptQuantity"]) if given.get("ReceiptQuantity") is not None else None
            line["PendingBatchNumbers"] = batches_in(given, "PendingBatchNumbers") or []
            po["Lines"].append(line)
        error = totals(po, b)
        if error:
            return _error(error)
        ds.purchase_orders.append(po)
        if status == "Complete":
            ds.receive(po, complete=True)
        return jsonify(serialize_po(ds, po)), 201

    @app.put("/PurchaseOrders/<guid>")
    def update_purchase_order(guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:  # assumed: as in the web app
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']} and can't be changed")
        b, bad = body()
        if bad:
            return bad
        if b.get("OrderStatus") not in OPEN_PO + ("Costed",):
            return _error("OrderStatus is required")
        tax, warehouse = tax_of(b), warehouse_of(b)
        if tax is None or warehouse is None:
            return _error("Tax and Warehouse must exist")
        po.update({"OrderStatus": b["OrderStatus"], "OrderDate": body_date(b.get("OrderDate")) or po["OrderDate"],
                   "RequiredDate": body_date(b.get("DeliveryDate")), "Comments": b.get("Comments"),
                   "SupplierRef": b.get("SupplierRef"), "WarehouseCode": warehouse,
                   "Tax": {"TaxCode": tax[0], "TaxRate": tax[1]}, "Modified": dt.date.today()})
        totals(po)
        return jsonify(serialize_po(ds, po))

    @app.post("/PurchaseOrders/<guid>/Lines")
    def add_purchase_line(guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']} and can't be changed")
        b, bad = body()
        if bad:
            return bad
        if b.get("LineNumber") is None:
            return _error("LineNumber is required")
        line, error = check_line(b, po["Tax"]["TaxRate"])
        if error:
            return _error(error)
        line["ReceiptQuantity"] = None
        line["PendingBatchNumbers"] = batches_in(b, "PendingBatchNumbers") or []
        po["Lines"].append(line)
        totals(po)
        po["Modified"] = dt.date.today()
        return jsonify(serialize_po(ds, po)), 201

    @app.put("/PurchaseOrders/<guid>/Lines/<line_guid>")
    def update_purchase_line(guid, line_guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']} and can't be changed")
        line = find(ds.lines_with_guids(po), line_guid)
        if line is None:
            return _error("Purchase order line not found", 404)
        b, bad = body()
        if bad:
            return bad
        for field in ("OrderQuantity", "LineTotal", "LineTax"):
            if b.get(field) is None:
                return _error(f"{field} is required")
        given = dict(b, Product={"ProductCode": line["ProductCode"]})
        given.setdefault("UnitPrice", line["UnitPrice"])
        changed, error = check_line(given, po["Tax"]["TaxRate"])
        if error:
            return _error(error)
        pending = batches_in(b, "PendingBatchNumbers")
        if pending is None:
            return _error("Each batch needs a Number and a Quantity")
        line.update({k: changed[k] for k in ("OrderQuantity", "UnitPrice", "LineTotal", "LineTax", "Comments")})
        line["ReceiptQuantity"] = float(b["ReceiptQuantity"]) if b.get("ReceiptQuantity") is not None else None
        if "PendingBatchNumbers" in b:
            line["PendingBatchNumbers"] = pending
        totals(po)
        po["Modified"] = dt.date.today()
        return jsonify(serialize_po(ds, po))

    @app.delete("/PurchaseOrders/<guid>/Lines/<line_guid>")
    def delete_purchase_line(guid, line_guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']} and can't be changed")
        line = find(ds.lines_with_guids(po), line_guid)
        if line is None:
            return _error("Purchase order line not found", 404)
        po["Lines"].remove(line)
        totals(po)
        po["Modified"] = dt.date.today()
        return "", 204

    @app.post("/PurchaseOrders/<guid>/Receipt")
    def receipt_purchase_order(guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']}")
        problem = ds.receive(po)
        return _error(problem) if problem else jsonify(serialize_po(ds, po))

    @app.post("/PurchaseOrders/<guid>/Complete")
    def complete_purchase_order(guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO + ("Receipted", "Costed"):
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']}")
        problem = ds.receive(po, complete=True)
        return _error(problem) if problem else jsonify(serialize_po(ds, po))

    @app.delete("/PurchaseOrders/<guid>")
    def delete_purchase_order(guid):
        po = find(ds.purchase_orders, guid)
        if po is None:
            return _error("Purchase order not found", 404)
        if po["OrderStatus"] not in OPEN_PO:
            return _error(f"Purchase order {po['OrderNumber']} is {po['OrderStatus']}; it can't be deleted")
        po["OrderStatus"], po["Modified"] = "Deleted", dt.date.today()
        return "", 204


def serialize_date(d):
    if d is None:
        return None
    epoch = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()
    return f"/Date({int(epoch * 1000)})/"
