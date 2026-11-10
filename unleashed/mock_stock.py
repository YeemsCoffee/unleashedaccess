"""Stock adjustments and warehouse stock transfers made on the mock Unleashed server.

Follows Unleashed's API reference (apidocs.unleashedsoftware.com, StockAdjustments,
AdjustmentReasons and WarehouseStockTransfers):

    GET    /AdjustmentReasons                        the reasons an adjustment can give
    GET    /StockAdjustments/{guid}                  one adjustment (the list is in mock_server)
    POST   /StockAdjustments[/{guid}]                a new adjustment, Parked or Completed; there is
                                                     no update, complete or delete
    GET    /WarehouseStockTransfers[/{guid or number}]   list (modifiedSince, transferStatus)
    POST   /WarehouseStockTransfers                  a new transfer, Parked or Placed
    PUT    /WarehouseStockTransfers/{guid}           its warehouses, comments, status and lines
    POST   /WarehouseStockTransfers/{guid}/Lines     a line added
    PUT    /WarehouseStockTransfers/{guid}/Lines/{line}
    DELETE /WarehouseStockTransfers/{guid}/Lines/{line}
    POST   /WarehouseStockTransfers/{guid}/Complete  the stock moves
    DELETE /WarehouseStockTransfers/{guid}           a parked or placed transfer

An adjustment line's NewQuantity and NewActualValue are the signed change in the product's
quantity and value in the warehouse, as the reference's examples show ("Found 2 in a box":
2 and 97.271; "Lost 2 somewhere": -2 and -18.884); there are no other change fields. What the
reference doesn't say is assumed: stock can't be taken below zero; stock taken out with no
value given leaves at average cost; a batch-tracked product's BatchNumbers give each batch's
signed change, adding up to NewQuantity. A transfer moves stock at average cost, a
batch-tracked product's batches soonest expiring first; it records when it completed in LastModifiedOn.
"""
import datetime as dt
import uuid

from flask import jsonify, request

from .mock_data import BATCH_TRACKED

ADJUSTMENT_REASONS = ["Stock count", "Damaged", "Expired", "Spoilage", "Samples", "Stolen", "Found", "Adjustment"]
OPEN_TRANSFERS = ("Parked", "Placed")


def _error(text, status=400):
    return jsonify({"description": text}), status


def register(app, ds, ms, body_date, ms_date_in, product_ref, warehouse_ref, paged, modified_since):
    """Add adjustment and transfer endpoints to the mock `app`, over dataset `ds`."""

    def warehouse_of(ref):
        ref = ref or {}
        code, guid = ref.get("WarehouseCode"), ref.get("Guid")
        return next((w["WarehouseCode"] for w in ds.warehouses
                     if (code and w["WarehouseCode"].lower() == code.lower()) or (guid and w["Guid"] == guid)), None)

    def product_of(ref):
        ref = ref or {}
        code, guid = ref.get("ProductCode"), ref.get("Guid")
        return next((c for c, p in ds.products.items()
                     if (code and c.lower() == code.lower()) or (guid and p["Guid"] == guid)), None)

    # ------------------------------------------------------------ adjustments

    @app.get("/AdjustmentReasons")
    @app.get("/AdjustmentReasons/<int:page>")
    def adjustment_reasons(page=1):
        want = (request.args.get("reason") or "").lower()
        return paged([{"Guid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"reason:{r}")), "Reason": r}
                      for r in ADJUSTMENT_REASONS if r.lower().startswith(want)], page)

    @app.get("/StockAdjustments/<guid>")
    def stock_adjustment(guid):
        a = next((a for a in ds.stock_adjustments if a["Guid"].lower() == guid.lower()), None)
        return jsonify(serialize_adjustment(a)) if a else _error("Stock adjustment not found", 404)

    @app.post("/StockAdjustments")
    @app.post("/StockAdjustments/<guid>")
    def create_adjustment(guid=None):
        body = request.get_json(silent=True) or {}
        if ms_date_in(body):
            return _error("/Date(...)/ is not a valid value for DateTime")
        warehouse = warehouse_of(body.get("Warehouse"))
        if not body.get("Warehouse"):
            return _error("Warehouse is required")
        if warehouse is None:
            return _error("Warehouse not found")
        reason = (body.get("AdjustmentReason") or "").strip()
        if not reason:
            return _error("AdjustmentReason is required")
        status = body.get("Status") or "Parked"
        if status not in ("Parked", "Completed"):
            return _error(f"Status {status} is not valid")
        given = body.get("StockAdjustmentLines") or []
        if not given:
            return _error("At least one StockAdjustmentLine is required")
        guid = body.get("Guid") or guid or str(uuid.uuid4())
        if any(a["Guid"].lower() == guid.lower() for a in ds.stock_adjustments):
            return _error("A stock adjustment with this Guid already exists")
        lines = []
        for l in given:
            code = product_of(l.get("Product"))
            if code is None:
                return _error("Product not found")
            if ds.products[code].get("NonStock"):
                return _error(f"Product {code} is not a stock item")
            if l.get("NewQuantity") is None or l.get("NewActualValue") is None:
                return _error("NewQuantity and NewActualValue are required")
            qty, value = float(l["NewQuantity"]), float(l["NewActualValue"])
            if qty * value < 0:
                return _error(f"{code}: NewActualValue should have the sign of NewQuantity")
            had = ds.stock_in(code, warehouse)[0]
            if status == "Completed" and had + qty < -1e-9:
                return _error(f"Not enough stock of {code} in {warehouse}: {had:g} on hand")
            batches = []
            for b in l.get("BatchNumbers") or []:
                if not (b.get("Number") or "").strip() or b.get("Quantity") is None:
                    return _error("Batch Number and Quantity are required")
                batches.append({"Number": b["Number"].strip(), "Quantity": float(b["Quantity"]),
                                "ExpiryDate": body_date(b.get("ExpiryDate")) if b.get("ExpiryDate") else None})
            if batches and status != "Completed":
                return _error("BatchNumbers can only be assigned to Completed adjustments")
            if status == "Completed" and code in BATCH_TRACKED:
                if abs(sum(b["Quantity"] for b in batches) - qty) > 1e-9:
                    return _error(f"Batch numbers for {code} must add up to its NewQuantity")
                for b in batches:
                    have = ds.find_batch(code, warehouse, b["Number"])
                    if (have["Quantity"] if have else 0.0) + b["Quantity"] < -1e-9:
                        return _error(f"Batch {b['Number']} of {code} doesn't have {-b['Quantity']:g}")
            lines.append({"Guid": str(uuid.uuid4()), "ProductCode": code, "NewQuantity": qty, "NewActualValue": value,
                          "Comments": (l.get("Comments") or None), "BatchNumbers": batches})
        if len({l["ProductCode"] for l in lines}) != len(lines):
            return _error("A product can only be on one line")
        day = ds.today
        number = ds.next_number("SA")
        if status == "Completed":
            for l in lines:
                code, qty = l["ProductCode"], l["NewQuantity"]
                if qty < 0 and not l["NewActualValue"]:  # out at average cost
                    l["NewActualValue"] = round(qty * ds.average_cost(code), 4)
                ds.move_stock(day, "Adjustment", number, reason, code, qty, l["NewActualValue"], warehouse)
                for b in l["BatchNumbers"]:
                    have = ds.find_batch(code, warehouse, b["Number"])
                    if have:
                        have["Quantity"] += b["Quantity"]
                    else:
                        ds.add_batch(code, warehouse, b["Number"], b["Quantity"], b["ExpiryDate"])
        sa = {"Guid": guid, "AdjustmentNumber": number, "AdjustmentDate": day, "AdjustmentReason": reason,
              "Status": status, "WarehouseCode": warehouse, "Lines": lines, "Modified": dt.date.today(),
              "AccountCode": body.get("AccountCode")}
        ds.stock_adjustments.append(sa)
        return jsonify(serialize_adjustment(sa)), 201

    def serialize_adjustment(a):
        return {
            "Guid": a["Guid"], "AdjustmentNumber": a["AdjustmentNumber"],
            "AdjustmentDate": ms(a["AdjustmentDate"]), "AdjustmentReason": a["AdjustmentReason"],
            "Status": a["Status"], "Warehouse": warehouse_ref(ds, a.get("WarehouseCode")),
            "StockAdjustmentLines": [
                {"Guid": l.get("Guid"), "LineNumber": i, "Product": product_ref(ds, l["ProductCode"]),
                 **{k: l[k] for k in ("NewQuantity", "NewActualValue", "Comments") if k in l},
                 "BatchNumbers": [{"Number": b["Number"], "Quantity": b["Quantity"],
                                   "ExpiryDate": ms(b.get("ExpiryDate"))} for b in l.get("BatchNumbers") or []]}
                for i, l in enumerate(a["Lines"], 1)],
            "LastModifiedOn": ms(a.get("Modified") or a["AdjustmentDate"]),
        }

    app.config["MOCK_SERIALIZE_ADJUSTMENT"] = serialize_adjustment

    # ------------------------------------------------------------ transfers

    def find_transfer(key):
        return next((t for t in ds.transfers
                     if t["Guid"].lower() == key.lower() or t["TransferOrderNumber"].lower() == key.lower()), None)

    def serialize_transfer(t):
        return {
            "Guid": t["Guid"], "TransferOrderNumber": t["TransferOrderNumber"],
            "OrderDate": ms(t["OrderDate"]), "DeliveryDate": ms(t.get("DeliveryDate")),
            "TransferStatus": t["TransferStatus"], "Comments": t.get("Comments"),
            "SourceWarehouse": warehouse_ref(ds, t["Source"]),
            "DestinationWarehouse": warehouse_ref(ds, t["Destination"]),
            "TransferDetails": [
                {"Guid": l["Guid"], "LineNumber": i, "Product": product_ref(ds, l["ProductCode"]),
                 "TransferQuantity": l["Quantity"], "Comments": l.get("Comments"),
                 "AverageLandedPriceAtTimeOfTransfer": l.get("Cost"),
                 "SourceWarehouseStockOnHandBeforeTransfer": l.get("SourceBefore"),
                 "DestinateWarehouseStockOnHandBeforeTransfer": l.get("DestinationBefore")}
                for i, l in enumerate(t["Lines"], 1)],
            "CreatedBy": "api", "CreatedOn": ms(t["OrderDate"]),
            "LastModifiedOn": ms(t.get("Modified") or t["OrderDate"]),
        }

    def transfer_line(l, existing=None):
        """A line from a request body: (line, error)."""
        if existing is None:
            code = product_of(l.get("Product"))
            if code is None:
                return None, "Product not found"
            if ds.products[code].get("NonStock"):
                return None, f"Product {code} is not a stock item"
        try:
            qty = float(l.get("TransferQuantity"))
        except (TypeError, ValueError):
            return None, "TransferQuantity is required"
        if qty <= 0:
            return None, "TransferQuantity must be greater than zero"
        line = existing if existing is not None else {"Guid": str(uuid.uuid4()), "ProductCode": code}
        line.update({"Quantity": qty, "Comments": l.get("Comments") or None})
        return line, None

    def warehouses_from(body, t=None):
        source, destination = warehouse_of(body.get("SourceWarehouse")), warehouse_of(body.get("DestinationWarehouse"))
        if not body.get("SourceWarehouse") or not body.get("DestinationWarehouse"):
            return None, None, "SourceWarehouse and DestinationWarehouse are required"
        if source is None or destination is None:
            return None, None, "Warehouse not found"
        if source == destination:
            return None, None, "The source and destination warehouses must be different"
        return source, destination, None

    @app.get("/WarehouseStockTransfers")
    @app.get("/WarehouseStockTransfers/<key>")
    def transfers(key="1"):
        if not key.isdigit():
            t = find_transfer(key)
            return jsonify(serialize_transfer(t)) if t else _error("Warehouse stock transfer not found", 404)
        wanted = [s.strip().lower() for s in (request.args.get("transferStatus") or "").split(",") if s.strip()]
        items = [t for t in ds.transfers
                 if (not wanted or t["TransferStatus"].lower() in wanted)
                 and modified_since(t.get("Modified") or t["OrderDate"])]
        return paged([serialize_transfer(t) for t in items], int(key))

    @app.post("/WarehouseStockTransfers")
    def create_transfer():
        body = request.get_json(silent=True) or {}
        if ms_date_in(body):
            return _error("/Date(...)/ is not a valid value for DateTime")
        if body.get("Guid"):
            return _error("Post / Put does not accept user generated GUID")
        source, destination, problem = warehouses_from(body)
        if problem:
            return _error(problem)
        status = body.get("TransferStatus") or "Parked"
        if status not in OPEN_TRANSFERS:
            return _error(f"TransferStatus {status} is not valid: Parked or Placed")
        given = body.get("TransferDetails") or []
        if not given:
            return _error("TransferDetails is required")
        lines = []
        for l in given:
            if l.get("Guid"):
                return _error("Post / Put does not accept user generated GUID")
            line, problem = transfer_line(l)
            if problem:
                return _error(problem)
            lines.append(line)
        t = {"Guid": str(uuid.uuid4()), "TransferOrderNumber": ds.next_number("TX"), "OrderDate": ds.today,
             "TransferStatus": status, "Comments": body.get("Comments") or None, "Source": source,
             "Destination": destination, "Lines": lines, "Modified": dt.date.today()}
        ds.transfers.append(t)
        return jsonify(serialize_transfer(t)), 201

    def open_transfer(guid):
        t = find_transfer(guid)
        if t is None:
            return None, _error("Warehouse stock transfer not found", 404)
        if t["TransferStatus"] not in OPEN_TRANSFERS:
            return None, _error(f"Transfer {t['TransferOrderNumber']} is {t['TransferStatus']} and can't be changed")
        return t, None

    @app.put("/WarehouseStockTransfers/<guid>")
    def update_transfer(guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        body = request.get_json(silent=True) or {}
        if ms_date_in(body):
            return _error("/Date(...)/ is not a valid value for DateTime")
        if (body.get("Guid") or "").lower() != t["Guid"].lower():
            return _error("Guid is required")
        source, destination, problem = warehouses_from(body)
        if problem:
            return _error(problem)
        status = body.get("TransferStatus") or t["TransferStatus"]
        if status not in OPEN_TRANSFERS:
            return _error(f"TransferStatus {status} is not valid: Parked or Placed")
        if "TransferDetails" not in body:
            return _error("TransferDetails is required")
        for l in body.get("TransferDetails") or []:
            line = next((x for x in t["Lines"] if x["Guid"].lower() == (l.get("Guid") or "").lower()), None)
            if line is None:
                return _error("Lines are added with POST .../Lines; a line here needs its Guid")
            _, problem = transfer_line(l, dict(line))
            if problem:
                return _error(problem)
        for l in body.get("TransferDetails") or []:
            line = next(x for x in t["Lines"] if x["Guid"].lower() == l["Guid"].lower())
            transfer_line(l, line)
        t.update({"Source": source, "Destination": destination, "TransferStatus": status,
                  "Comments": body.get("Comments") or None, "Modified": dt.date.today()})
        return jsonify(serialize_transfer(t))

    @app.post("/WarehouseStockTransfers/<guid>/Lines")
    def add_transfer_line(guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        body = request.get_json(silent=True) or {}
        if body.get("Guid"):
            return _error("Post / Put does not accept user generated GUID")
        line, problem = transfer_line(body)
        if problem:
            return _error(problem)
        t["Lines"].append(line)
        t["Modified"] = dt.date.today()
        return jsonify(serialize_transfer(t)), 201

    @app.put("/WarehouseStockTransfers/<guid>/Lines/<line_guid>")
    def update_transfer_line(guid, line_guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        line = next((x for x in t["Lines"] if x["Guid"].lower() == line_guid.lower()), None)
        if line is None:
            return _error("Line not found", 404)
        _, problem = transfer_line(request.get_json(silent=True) or {}, line)
        if problem:
            return _error(problem)
        t["Modified"] = dt.date.today()
        return jsonify(serialize_transfer(t))

    @app.delete("/WarehouseStockTransfers/<guid>/Lines/<line_guid>")
    def delete_transfer_line(guid, line_guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        before = len(t["Lines"])
        t["Lines"] = [x for x in t["Lines"] if x["Guid"].lower() != line_guid.lower()]
        if len(t["Lines"]) == before:
            return _error("Line not found", 404)
        t["Modified"] = dt.date.today()
        return jsonify(serialize_transfer(t))

    @app.post("/WarehouseStockTransfers/<guid>/Complete")
    def complete_transfer(guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        if not t["Lines"]:
            return _error("The transfer has no lines")
        for l in t["Lines"]:
            code = l["ProductCode"]
            if ds.stock_in(code, t["Source"])[0] + 1e-9 < l["Quantity"]:
                return _error(f"Not enough stock of {code} in {t['Source']} to complete the transfer")
            if code in BATCH_TRACKED and sum(b["Quantity"] for b in ds.batches if b["ProductCode"] == code
                                             and b["WarehouseCode"] == t["Source"]) + 1e-9 < l["Quantity"]:
                return _error(f"Not enough batches of {code} in {t['Source']} to complete the transfer")
        for l in t["Lines"]:
            code = l["ProductCode"]
            l["SourceBefore"] = ds.stock_in(code, t["Source"])[0]
            l["DestinationBefore"] = ds.stock_in(code, t["Destination"])[0]
            l["Cost"] = round(ds.average_cost(code), 4)
            ds.shift_stock(ds.today, code, l["Quantity"], t["Source"], t["Destination"])
            if code in BATCH_TRACKED:  # soonest expiring first
                left = l["Quantity"]
                for b in sorted((b for b in ds.batches if b["ProductCode"] == code and b["WarehouseCode"] == t["Source"]
                                 and b["Quantity"] > 0), key=lambda b: (b["ExpiryDate"] or dt.date.max, b["Number"])):
                    take = min(left, b["Quantity"])
                    b["Quantity"] -= take
                    ds.add_batch(code, t["Destination"], b["Number"], take, b["ExpiryDate"])
                    left -= take
                    if left <= 1e-9:
                        break
        t.update({"TransferStatus": "Completed", "DeliveryDate": ds.today, "Modified": ds.today})
        return jsonify(serialize_transfer(t))

    @app.delete("/WarehouseStockTransfers/<guid>")
    def delete_transfer(guid):
        t, problem = open_transfer(guid)
        if problem:
            return problem
        t.update({"TransferStatus": "Deleted", "Modified": dt.date.today()})
        return jsonify({}), 200
