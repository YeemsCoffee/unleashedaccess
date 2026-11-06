"""Credit notes and supplier returns on the mock Unleashed server.

Follows Unleashed's API reference (apidocs.unleashedsoftware.com, CreditNotes and
SupplierReturns):

    GET    /CreditNotes[/{guid}]                 list (startDate/endDate/modifiedSince/creditStatus/creditNumber)
    POST   /CreditNotes/FreeCredit               a parked free-entry credit (the only kind the API makes)
    POST   /CreditNotes/{guid}/Complete          complete: lines marked Return go back into stock
    DELETE /CreditNotes/{guid}                   delete a parked free credit

    GET    /SupplierReturns[/{guid}]             list (startDate/endDate/modifiedSince/supplierReturnStatus/...)
    POST   /SupplierReturns                      a parked return of a completed purchase order's lines
    PUT    /SupplierReturns/{guid}               its date, reference, comments, warehouse
    PUT    /SupplierReturns/{guid}/Lines/{line}  a line's return quantity, price, reason
    DELETE /SupplierReturns/{guid}/Lines/{line}
    POST   /SupplierReturns/{guid}/ProductTracking   the batches going back
    POST   /SupplierReturns/{guid}/Complete      complete: the stock goes out
    DELETE /SupplierReturns/{guid}
    GET    /SupplierReturnReasons

What the reference doesn't say is assumed and marked: statuses go Parked -> Completed; a free
credit's returned stock goes back at the product's average cost; a returned line leaves stock
at the average cost (as AverageLandedPriceBefore/AfterReturn show).
"""
import datetime as dt
import uuid
from decimal import ROUND_HALF_UP, Decimal

from flask import jsonify, request

from .mock_data import BATCH_TRACKED

RETURN_REASONS = ["Return", "Damaged", "Wrong item", "Expired"]


def _money(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _error(text, status=400):
    return jsonify({"description": text}), status


def register(app, ds, ms, body_date, ms_date_in, product_ref, customer_ref, supplier_ref, warehouse_ref):
    """Add credit note and supplier return endpoints to the mock `app`, over dataset `ds`."""
    ds.credit_notes = getattr(ds, "credit_notes", [])
    ds.supplier_returns = getattr(ds, "supplier_returns", [])

    def find(items, guid):
        return next((d for d in items if d["Guid"].lower() == guid.lower()), None)

    def body():
        b = request.get_json(silent=True) or {}
        bad = ms_date_in(b)
        return b, (_error(f"{bad} is not a valid value for DateTime.") if bad else None)

    def date_arg(name):
        v = request.args.get(name)
        return dt.date.fromisoformat(v[:10]) if v else None

    def listed(items, date_field, status_arg, number_field, number_arg, party_field, party_arg):
        start, end, since = date_arg("startDate"), date_arg("endDate"), date_arg("modifiedSince")
        status = {s.strip().lower() for s in (request.args.get(status_arg) or "").split(",") if s.strip()}
        number, party = request.args.get(number_arg), request.args.get(party_arg)
        return [d for d in items
                if (start is None or d[date_field] >= start) and (end is None or d[date_field] <= end)
                and (since is None or d["Modified"] >= since)
                and (not status or d["Status"].lower() in status)
                and (not number or d[number_field] == number)
                and (not party or d[party_field].lower().startswith(party.lower()))]

    def page(items):
        return jsonify({"Pagination": {"NumberOfItems": len(items), "PageSize": max(len(items), 1),
                                       "PageNumber": 1, "NumberOfPages": 1}, "Items": items})

    # ------------------------------------------------------------ credit notes

    def credit_json(cn):
        lines = []
        for i, l in enumerate(cn["Lines"], 1):
            total = _money(l["CreditQuantity"] * l["CreditPrice"])
            batches = [{"Number": b["Number"], "Quantity": b["Quantity"], "ExpiryDate": ms(b.get("ExpiryDate"))}
                       for b in l.get("Batches") or []]
            lines.append({
                "Guid": l["Guid"], "LineNumber": i, "Product": product_ref(ds, l["ProductCode"]),
                "CreditQuantity": l["CreditQuantity"], "CreditPrice": l["CreditPrice"], "LineTotal": total,
                "BCLineTotal": total, "LineTax": _money(total * cn["TaxRate"]), "Reason": l["Reason"],
                "Return": l["Return"], "Comments": l.get("Comments"),
                "AverageLandedPriceAtTimeOfSale": l.get("AverageLandedPriceAtTimeOfSale"),
                ("BatchNumbers" if cn["Status"] == "Completed" else "PendingBatchNumbers"): batches,
            })
        sub = _money(sum(l["LineTotal"] for l in lines))
        tax = _money(sum(l["LineTax"] for l in lines))
        return {"Guid": cn["Guid"], "CreditNumber": cn["CreditNumber"], "CreditDate": ms(cn["CreditDate"]),
                "CreditStatus": cn["Status"], "Status": cn["Status"], "CreditType": "FreeCredit",
                "Customer": customer_ref(ds, cn["CustomerCode"]), "Warehouse": warehouse_ref(ds, cn["WarehouseCode"]),
                "Reference": cn.get("Reference"), "Comments": cn.get("Comments"),
                "Currency": {"CurrencyCode": "USD"}, "ExchangeRate": 1.0, "TaxRate": cn["TaxRate"],
                "SubTotal": sub, "TaxTotal": tax, "Total": _money(sub + tax), "CreditLines": lines,
                "LastModifiedOn": ms(cn["Modified"])}

    @app.get("/CreditNotes")
    @app.get("/CreditNotes/<page_or_guid>")
    def credit_notes(page_or_guid="1"):
        if not page_or_guid.isdigit():
            cn = find(ds.credit_notes, page_or_guid)
            return jsonify(credit_json(cn)) if cn else _error("Credit note not found", 404)
        return page([credit_json(c) for c in listed(ds.credit_notes, "CreditDate", "creditStatus", "CreditNumber",
                                                     "creditNumber", "CustomerCode", "customerCode")])

    @app.post("/CreditNotes/FreeCredit")
    def create_credit():
        b, bad = body()
        if bad:
            return bad
        customer = (b.get("Customer") or {}).get("CustomerCode")
        if customer not in ds.customers:
            return _error(f"Customer {customer} not found")
        warehouse = (b.get("Warehouse") or {}).get("WarehouseCode")
        if not any(w["WarehouseCode"] == warehouse for w in ds.warehouses):
            return _error("Warehouse is required")
        if not b.get("CreditDate") or b.get("ExchangeRate") is None:
            return _error("CreditDate and ExchangeRate are required")
        if not b.get("CreditLines"):
            return _error("At least one credit line is required")
        cn = {"Guid": str(uuid.uuid4()), "CreditNumber": ds.next_number("CN"), "CreditDate": body_date(b["CreditDate"]),
              "Status": "Parked", "CustomerCode": customer, "WarehouseCode": warehouse,
              "Reference": b.get("Reference"), "Comments": b.get("Comments") or b.get("Comment"),
              "TaxRate": 0.0 if customer == "GDN-POS" else 0.095, "Lines": [], "Modified": dt.date.today()}
        for given in b["CreditLines"]:
            code = (given.get("Product") or {}).get("ProductCode")
            if code not in ds.products:
                return _error(f"Product {code} not found")
            for field in ("CreditQuantity", "CreditPrice", "Reason", "Return"):
                if given.get(field) is None:
                    return _error(f"{field} is required on each credit line")
            batches = []
            for x in (given.get("BatchNumbers") or given.get("PendingBatchNumbers") or []):
                if not x.get("Number") or x.get("Quantity") is None:
                    return _error("Each batch needs a Number and a Quantity")
                batches.append({"Number": x["Number"], "Quantity": float(x["Quantity"]),
                                "ExpiryDate": body_date(x.get("ExpiryDate")) if x.get("ExpiryDate") else None})
            cn["Lines"].append({"Guid": str(uuid.uuid4()), "ProductCode": code,
                                "CreditQuantity": float(given["CreditQuantity"]),
                                "CreditPrice": float(given["CreditPrice"]), "Reason": given["Reason"],
                                "Return": bool(given["Return"]), "Comments": given.get("Comments"), "Batches": batches})
        ds.credit_notes.append(cn)
        return jsonify(credit_json(cn)), 201

    @app.post("/CreditNotes/<guid>/Complete")
    def complete_credit(guid):
        cn = find(ds.credit_notes, guid)
        if cn is None:
            return _error("Credit note not found", 404)
        if cn["Status"] != "Parked":
            return _error(f"Credit note {cn['CreditNumber']} is {cn['Status']}")
        for l in cn["Lines"]:
            if l["Return"] and l["ProductCode"] in BATCH_TRACKED and \
                    abs(sum(b["Quantity"] for b in l["Batches"]) - l["CreditQuantity"]) > 1e-9:
                return _error(f"Batch numbers are required for {l['ProductCode']}")
        for l in cn["Lines"]:
            code = l["ProductCode"]
            avg = ds.average_cost(code)
            l["AverageLandedPriceAtTimeOfSale"] = round(avg, 4)
            if l["Return"]:  # assumed: back at the average cost
                ds.move_stock(ds.today, "Credit", cn["CreditNumber"], cn["CustomerCode"], code,
                              l["CreditQuantity"], l["CreditQuantity"] * avg)
                for b in l["Batches"]:
                    ds.add_batch(code, cn["WarehouseCode"], b["Number"], b["Quantity"], b.get("ExpiryDate"))
        cn["Status"], cn["Modified"] = "Completed", dt.date.today()
        return jsonify(credit_json(cn))

    @app.delete("/CreditNotes/<guid>")
    def delete_credit(guid):
        cn = find(ds.credit_notes, guid)
        if cn is None:
            return _error("Credit note not found", 404)
        if cn["Status"] != "Parked":
            return _error(f"Credit note {cn['CreditNumber']} is {cn['Status']}; only a parked one can be deleted")
        cn["Status"], cn["Modified"] = "Deleted", dt.date.today()
        return "", 204

    # ------------------------------------------------------------ supplier returns

    def return_json(sr):
        lines = []
        for i, l in enumerate(sr["Lines"], 1):
            lines.append({
                "Guid": l["Guid"], "LineNumber": i, "Product": product_ref(ds, l["ProductCode"]),
                "OrderQuantity": l["OrderQuantity"], "OrderPrice": l["OrderPrice"],
                "ReturnQuantity": l["ReturnQuantity"], "PurchaseUnitReturnQuantity": l["ReturnQuantity"],
                "ReturnPrice": l["ReturnPrice"], "ReturnCost": l["ReturnCost"], "ReturnReason": l["ReturnReason"],
                "ReturnToSupplier": l["ReturnToSupplier"], "Comments": l.get("Comments"), "LineTax": 0.0,
                "AverageLandedPriceBeforeReturn": l.get("AvgBefore"), "AverageLandedPriceAfterReturn": l.get("AvgAfter"),
                "StockOnHandBeforeReturn": l.get("SohBefore"),
            })
        sub = _money(sum(l["ReturnQuantity"] * l["ReturnPrice"] for l in sr["Lines"]))
        out = {"Guid": sr["Guid"], "SupplierReturnNumber": sr["SupplierReturnNumber"], "Status": sr["Status"],
               "ReturnDate": ms(sr["ReturnDate"]), "Supplier": supplier_ref(ds, sr["SupplierCode"]),
               "PurchaseOrder": {"OrderNumber": sr["PurchaseOrderNumber"], "Guid": sr["PurchaseOrderGuid"]},
               "Warehouse": warehouse_ref(ds, sr["WarehouseCode"]), "SupplierRef": sr.get("SupplierRef"),
               "Comments": sr.get("Comments"), "Currency": {"CurrencyCode": "USD"}, "ExchangeRate": 1.0,
               "SubTotal": sub, "TaxTotal": 0.0, "Total": sub, "SupplierReturnLines": lines,
               "SupplierReturnCosts": [], "LastModifiedOn": ms(sr["Modified"])}
        if request.args.get("serialBatch", "").lower() == "true":
            out["ProductBatches"] = [{"Product": product_ref(ds, code), "Batches": [
                {"BatchNumber": b["Number"], "Quantity": b["Quantity"]} for b in batches]}
                for code, batches in sr["Batches"].items() if batches]
        return out

    @app.get("/SupplierReturnReasons")
    def return_reasons():
        return page([{"Reason": r} for r in RETURN_REASONS])

    @app.get("/SupplierReturns")
    @app.get("/SupplierReturns/<page_or_guid>")
    def supplier_returns(page_or_guid="1"):
        if not page_or_guid.isdigit():
            sr = find(ds.supplier_returns, page_or_guid)
            return jsonify(return_json(sr)) if sr else _error("Supplier return not found", 404)
        items = listed(ds.supplier_returns, "ReturnDate", "supplierReturnStatus", "SupplierReturnNumber",
                       "supplierReturnNumber", "SupplierCode", "supplierCode")
        po = request.args.get("purchaseOrderNumber")
        return page([return_json(s) for s in items if not po or s["PurchaseOrderNumber"] == po])

    @app.post("/SupplierReturns")
    @app.post("/SupplierReturns/")
    def create_return():
        b, bad = body()
        if bad:
            return bad
        given = b.get("PurchaseOrder") or {}
        po = next((p for p in ds.purchase_orders if p["OrderNumber"] == given.get("OrderNumber")
                   or (given.get("Guid") and p["Guid"].lower() == given["Guid"].lower())), None)
        if po is None:
            return _error("Purchase order not found")
        if po["OrderStatus"] != "Complete":
            return _error(f"Purchase order {po['OrderNumber']} must have the Complete status")
        sr = {"Guid": str(uuid.uuid4()), "SupplierReturnNumber": ds.next_number("SR"), "Status": "Parked",
              "ReturnDate": ds.today, "SupplierCode": po["SupplierCode"], "PurchaseOrderNumber": po["OrderNumber"],
              "PurchaseOrderGuid": po["Guid"], "WarehouseCode": po.get("WarehouseCode") or "GDN",
              "Modified": dt.date.today(), "Batches": {},
              "Lines": [{"Guid": str(uuid.uuid4()), "ProductCode": l["ProductCode"], "OrderQuantity": l["OrderQuantity"],
                         "OrderPrice": l["UnitPrice"], "ReturnQuantity": 0.0, "ReturnPrice": l["UnitPrice"],
                         "ReturnCost": l["UnitPrice"], "ReturnReason": "Return", "ReturnToSupplier": True}
                        for l in ds.lines_with_guids(po)]}
        ds.supplier_returns.append(sr)
        return jsonify(return_json(sr)), 201

    def parked(guid):
        sr = find(ds.supplier_returns, guid)
        if sr is None:
            return None, _error("Supplier return not found", 404)
        if sr["Status"] != "Parked":
            return None, _error(f"Supplier return {sr['SupplierReturnNumber']} is {sr['Status']}")
        return sr, None

    @app.put("/SupplierReturns/<guid>")
    def update_return(guid):
        sr, error = parked(guid)
        if error:
            return error
        b, bad = body()
        if bad:
            return bad
        warehouse = (b.get("Warehouse") or {}).get("WarehouseCode")
        if warehouse and not any(w["WarehouseCode"] == warehouse for w in ds.warehouses):
            return _error("Warehouse not found")
        sr.update({"Comments": b.get("Comments"), "SupplierRef": b.get("SupplierRef"),
                   "ReturnDate": body_date(b.get("ReturnDate")) or sr["ReturnDate"],
                   "WarehouseCode": warehouse or sr["WarehouseCode"], "Modified": dt.date.today()})
        return jsonify(return_json(sr))

    @app.put("/SupplierReturns/<guid>/Lines/<line_guid>")
    def update_return_line(guid, line_guid):
        sr, error = parked(guid)
        if error:
            return error
        line = find(sr["Lines"], line_guid)
        if line is None:
            return _error("Supplier return line not found", 404)
        b, bad = body()
        if bad:
            return bad
        qty = b.get("PurchaseUnitReturnQuantity")
        if qty is not None and (float(qty) < 0 or float(qty) > line["OrderQuantity"] + 1e-9):
            return _error(f"Return quantity for {line['ProductCode']} can't be more than was ordered")
        reason = b.get("ReturnReason") or "Return"
        if reason not in RETURN_REASONS:
            return _error(f"{reason} is not a supplier return reason")
        # A blank editable field overwrites the old value (Unleashed's reference).
        line.update({"ReturnQuantity": float(qty or 0), "ReturnPrice": float(b.get("ReturnPrice") or 0),
                     "ReturnCost": float(b.get("ReturnCost") or 0), "ReturnReason": reason,
                     "ReturnToSupplier": bool(b.get("ReturnToSupplier")), "Comments": b.get("Comments")})
        sr["Modified"] = dt.date.today()
        return jsonify(return_json(sr))

    @app.delete("/SupplierReturns/<guid>/Lines/<line_guid>")
    def delete_return_line(guid, line_guid):
        sr, error = parked(guid)
        if error:
            return error
        line = find(sr["Lines"], line_guid)
        if line is None:
            return _error("Supplier return line not found", 404)
        sr["Lines"].remove(line)
        sr["Modified"] = dt.date.today()
        return "", 204

    @app.post("/SupplierReturns/<guid>/ProductTracking")
    @app.put("/SupplierReturns/<guid>/ProductTracking")
    def return_batches(guid):
        sr, error = parked(guid)
        if error:
            return error
        b, bad = body()
        if bad:
            return bad
        if request.method == "PUT":
            sr["Batches"] = {}
        for pb in b.get("ProductBatches") or []:
            code = (pb.get("Product") or {}).get("ProductCode")
            if not any(l["ProductCode"] == code for l in sr["Lines"]):
                return _error(f"{code} isn't on this supplier return")
            for x in pb.get("Batches") or []:
                if not x.get("BatchNumber") or x.get("Quantity") is None:
                    return _error("Each batch needs a BatchNumber and a Quantity")
                sr["Batches"].setdefault(code, []).append({"Number": x["BatchNumber"], "Quantity": float(x["Quantity"])})
        sr["Modified"] = dt.date.today()
        return jsonify(return_json(sr))

    @app.post("/SupplierReturns/<guid>/Complete")
    def complete_return(guid):
        sr, error = parked(guid)
        if error:
            return error
        going = [l for l in sr["Lines"] if l["ReturnQuantity"] > 0]
        if not going:
            return _error("Nothing to return: every line's return quantity is 0")
        for l in going:
            code = l["ProductCode"]
            if ds.stock_qty(code) + 1e-9 < l["ReturnQuantity"]:
                return _error(f"Not enough stock of {code} to return")
            if code in BATCH_TRACKED:
                batches = sr["Batches"].get(code) or []
                if abs(sum(b["Quantity"] for b in batches) - l["ReturnQuantity"]) > 1e-9:
                    return _error(f"Batch numbers are required for {code}")
                for b in batches:
                    have = ds.find_batch(code, sr["WarehouseCode"], b["Number"])
                    if have is None or have["Quantity"] + 1e-9 < b["Quantity"]:
                        return _error(f"Batch {b['Number']} of {code} doesn't have {b['Quantity']:g}")
        for l in going:
            code = l["ProductCode"]
            before, avg = ds.stock_qty(code), ds.average_cost(code)
            l["SohBefore"], l["AvgBefore"] = before, round(avg, 4)
            ds.move_stock(ds.today, "Supplier return", sr["SupplierReturnNumber"], sr["SupplierCode"], code,
                          -l["ReturnQuantity"], -l["ReturnQuantity"] * avg)
            l["AvgAfter"] = round(ds.average_cost(code), 4)
            for b in sr["Batches"].get(code) or []:
                ds.find_batch(code, sr["WarehouseCode"], b["Number"])["Quantity"] -= b["Quantity"]
        sr["Status"], sr["Modified"] = "Completed", dt.date.today()
        return jsonify(return_json(sr))

    @app.delete("/SupplierReturns/<guid>")
    def delete_return(guid):
        sr, error = parked(guid)
        if error:
            return error
        sr["Status"], sr["Modified"] = "Deleted", dt.date.today()
        return "", 204
