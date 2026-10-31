"""Mock Unleashed API server.

Implements the Unleashed endpoints the dashboard uses, with the same request
signing, paging and JSON shapes, backed by MockDataset. Run standalone with
`python mock_unleashed.py`, or mounted in-process by the main app.
"""
import datetime as dt
import hmac
import math

from flask import Flask, jsonify, request

from .client import sign
from .mock_data import BATCH_TRACKED, MockDataset

MOCK_API_ID = "mock-api-id"
MOCK_API_KEY = "mock-api-key"
MAX_PAGE_SIZE = 1000


def create_mock_app(api_id=MOCK_API_ID, api_key=MOCK_API_KEY, today=None):
    app = Flask(__name__)
    ds = MockDataset(today=today)
    app.config["DATASET"] = ds

    @app.before_request
    def _authenticate():
        given_id = request.headers.get("api-auth-id", "")
        given_sig = request.headers.get("api-auth-signature", "")
        expected = sign(request.query_string.decode(), api_key)
        if given_id != api_id or not hmac.compare_digest(given_sig, expected):
            return jsonify({"description": "Forbidden: invalid api-auth-id or api-auth-signature"}), 403

    @app.errorhandler(404)
    def _not_found(e):
        return jsonify({"description": "Resource not found"}), 404

    # ------------------------------------------------------------ reference

    @app.get("/Warehouses")
    @app.get("/Warehouses/<int:page>")
    def warehouses(page=1):
        return _paged([_warehouse(w) for w in ds.warehouses], page)

    @app.get("/ProductGroups")
    @app.get("/ProductGroups/<int:page>")
    def product_groups(page=1):
        groups = sorted({p["ProductGroup"] for p in ds.products.values()})
        return _paged([{"GroupName": g, "Guid": None} for g in groups], page)

    @app.get("/Products")
    @app.get("/Products/<int:page>")
    def products(page=1):
        items = [p for p in ds.products.values() if _match(p["ProductCode"], "productCode")]
        group = request.args.get("productGroup")
        if group:
            items = [p for p in items if p["ProductGroup"] == group]
        return _paged([_product(ds, p) for p in items], page)

    @app.get("/Suppliers")
    @app.get("/Suppliers/<int:page>")
    def suppliers(page=1):
        items = [{"Guid": s["Guid"], "SupplierCode": s["SupplierCode"],
                  "SupplierName": s["SupplierName"], "Currency": {"CurrencyCode": "USD"}}
                 for s in ds.suppliers.values() if _match(s["SupplierCode"], "supplierCode")]
        return _paged(items, page)

    @app.get("/Customers")
    @app.get("/Customers/<int:page>")
    def customers(page=1):
        items = [{"Guid": c["Guid"], "CustomerCode": c["CustomerCode"],
                  "CustomerName": c["CustomerName"], "Currency": {"CurrencyCode": "USD"}}
                 for c in ds.customers.values() if _match(c["CustomerCode"], "customerCode")]
        return _paged(items, page)

    # ------------------------------------------------------------ stock

    @app.get("/StockOnHand")
    @app.get("/StockOnHand/<int:page>")
    def stock_on_hand(page=1):
        as_at = _date_arg("asAtDate")
        wh = request.args.get("warehouseCode")
        if wh and wh != next(w["WarehouseCode"] for w in ds.warehouses if w.get("IsDefault")):
            return _paged([], page)  # all the stock is in the default warehouse
        rows = []
        for p in ds.products.values():
            if not _match(p["ProductCode"], "productCode"):
                continue
            qty, value = ds.stock_as_at(p["ProductCode"], as_at)
            allocated = ds.allocated(p["ProductCode"]) if as_at is None else 0
            last_sale = ds.last_sale_date(p["ProductCode"], as_at)
            ref = as_at or ds.today
            rows.append({
                "Guid": p["Guid"], "ProductGuid": p["Guid"],
                "ProductCode": p["ProductCode"], "ProductDescription": p["ProductDescription"],
                "ProductGroupName": p["ProductGroup"],
                "Warehouse": "Gardena Store", "WarehouseCode": "GDN",
                "QtyOnHand": round(qty, 4), "AllocatedQty": allocated,
                "AvailableQty": round(qty - allocated, 4),
                "OnPurchase": ds.on_order(p["ProductCode"]) if as_at is None else 0,
                "AvgCost": round(value / qty, 4) if qty else 0.0,
                "TotalCost": round(value, 4),
                "DaysSinceLastSale": (ref - last_sale).days if last_sale else None,
                "LastModifiedOn": _ms(ref),
            })
        return _paged(rows, page)

    @app.get("/StockAdjustments")
    @app.get("/StockAdjustments/<int:page>")
    def stock_adjustments(page=1):
        start, end = _date_arg("startDate"), _date_arg("endDate")
        items = [a for a in ds.stock_adjustments
                 if _in_range(a["AdjustmentDate"], start, end)
                 and _modified_since(_modified(a, "AdjustmentDate"))
                 and _match(a["AdjustmentNumber"], "adjustmentNumber")]
        return _paged([_adjustment(ds, a) for a in items], page)

    # ------------------------------------------------------------ orders

    @app.get("/PurchaseOrders")
    @app.get("/PurchaseOrders/<page_or_guid>")
    def purchase_orders(page_or_guid="1"):
        if not page_or_guid.isdigit():
            po = next((p for p in ds.purchase_orders if p["Guid"] == page_or_guid), None)
            return (jsonify(_purchase_order(ds, po)) if po
                    else (jsonify({"description": "Purchase order not found"}), 404))
        start, end = _date_arg("startDate"), _date_arg("endDate")
        statuses = _statuses()
        items = [p for p in ds.purchase_orders
                 if _in_range(p["OrderDate"], start, end)
                 and _modified_since(_modified(p, "CompletedDate", "OrderDate"))
                 and (not statuses or p["OrderStatus"].lower() in statuses)
                 and _match(p["SupplierCode"], "supplierCode", exact=True)
                 and _match(p["OrderNumber"], "orderNumber")]
        return _paged([_purchase_order(ds, p) for p in items], int(page_or_guid))

    @app.get("/SalesOrders")
    @app.get("/SalesOrders/<page_or_guid>")
    def sales_orders(page_or_guid="1"):
        if not page_or_guid.isdigit():
            so = next((s for s in ds.sales_orders if s["Guid"] == page_or_guid), None)
            return (jsonify(_sales_order(ds, so)) if so
                    else (jsonify({"description": "Sales order not found"}), 404))
        start, end = _date_arg("startDate"), _date_arg("endDate")
        statuses = _statuses()
        items = [s for s in ds.sales_orders
                 if _in_range(s["OrderDate"], start, end)
                 and _modified_since(_modified(s, "CompletedDate", "OrderDate"))
                 and (not statuses or s["OrderStatus"].lower() in statuses)
                 and _match(s["CustomerCode"], "customerCode", exact=True)
                 and _match(s["OrderNumber"], "orderNumber")]
        return _paged([_sales_order(ds, s) for s in items], int(page_or_guid))

    # ------------------------------------------------------------ production

    @app.get("/BillOfMaterials")
    @app.get("/BillOfMaterials/<page_or_guid>")
    def bills_of_materials(page_or_guid="1"):
        if not page_or_guid.isdigit():
            bom = next((b for b in ds.boms if b["Guid"] == page_or_guid), None)
            return (jsonify(_bom(ds, bom)) if bom
                    else (jsonify({"description": "Bill of materials not found"}), 404))
        items = [b for b in ds.boms if _match(b["BillNumber"], "billNumber")
                 and _match(b["ProductCode"], "productCode")]
        return _paged([_bom(ds, b) for b in items], int(page_or_guid))

    @app.get("/Assemblies")
    @app.get("/Assemblies/<page_or_guid>")
    def assemblies(page_or_guid="1"):
        if not page_or_guid.isdigit():
            a = next((a for a in ds.assemblies if a["Guid"] == page_or_guid), None)
            return (jsonify(_assembly(ds, a)) if a
                    else (jsonify({"description": "Assembly not found"}), 404))
        start, end = _date_arg("startDate"), _date_arg("endDate")
        statuses = _statuses("assemblyStatus")
        items = [a for a in ds.assemblies
                 if _in_range(a["AssemblyDate"], start, end)
                 and _modified_since(_modified(a, "AssemblyDate"))
                 and (not statuses or a["Status"].lower() in statuses)
                 and _match(a["AssemblyNumber"], "assemblyNumber")
                 and _match(a["ProductCode"], "productCode", exact=True)]
        return _paged([_assembly(ds, a) for a in items], int(page_or_guid))

    @app.post("/Assemblies")
    def create_assembly():
        """A new parked assembly; Unleashed numbers it and fills its lines from the BOM."""
        body = request.get_json(silent=True) or {}
        if _ms_date_in(body):
            return _bad_date(body)
        code = (body.get("Product") or {}).get("ProductCode")
        if not code:
            return jsonify({"description": "Product is required"}), 400
        qty = float(body.get("Quantity") or 0)
        codes = {w["WarehouseCode"] for w in ds.warehouses}
        source, destination = ((body.get(f) or {}).get("WarehouseCode")
                               for f in ("SourceWarehouse", "DestinationWarehouse"))
        for wh in (source, destination):
            if wh and wh not in codes:
                return jsonify({"description": f"Warehouse {wh} not found"}), 400
        a = ds.create_assembly(code, qty, assemble_by=_body_date(body.get("AssembleBy")),
                               comments=body.get("Comments"), source=source, destination=destination)
        if a is None:
            return jsonify({"description": f"Product {code} is not an assembled product"}), 400
        return jsonify(_assembly(ds, a)), 201

    @app.put("/Assemblies/<guid>")
    def update_assembly(guid):
        """Change a parked assembly's quantity, its lines' quantities, and the batches of
        what it makes (PendingBatchNumbers, created when it's completed)."""
        a = next((a for a in ds.assemblies if a["Guid"] == guid), None)
        if a is None:
            return jsonify({"description": "Assembly not found"}), 404
        if a["Status"] != "Parked":
            return jsonify({"description": f"Assembly {a['AssemblyNumber']} is {a['Status']} and can't be changed"}), 400
        body = request.get_json(silent=True) or {}
        if _ms_date_in(body):
            return _bad_date(body)
        pending = None
        if "PendingBatchNumbers" in body:
            if a["ProductCode"] not in BATCH_TRACKED:
                return jsonify({"description": f"Product {a['ProductCode']} is not batch tracked, so "
                                               "PendingBatchNumbers can't be included"}), 400
            pending = []
            for b in body["PendingBatchNumbers"] or []:
                number = (b.get("Number") or "").strip()
                if not number or len(number) > 50 or b.get("Quantity") is None:
                    return jsonify({"description": "Each batch needs a Number (50 characters at most) "
                                                   "and a Quantity"}), 400
                pending.append({"Number": number, "Quantity": float(b["Quantity"]),
                                "ExpiryDate": _body_date(b.get("ExpiryDate"))})
        if body.get("Quantity") is not None:
            a["Quantity"] = float(body["Quantity"])
        for given in body.get("AssemblyLines") or []:
            code = (given.get("Product") or {}).get("ProductCode")
            line = next((l for i, l in enumerate(a["Lines"], 1)
                         if given.get("LineNumber") == i or l["ProductCode"] == code), None)
            if line is None:
                return jsonify({"description": f"No line for {code} on this assembly"}), 400
            for field in ("Quantity", "WastageQuantity"):
                if given.get(field) is not None:
                    line[field] = float(given[field])
        if "Comments" in body:
            if len(body["Comments"] or "") > 1024:
                return jsonify({"description": "Comments must be 1024 characters or fewer"}), 400
            a["Comments"] = body["Comments"]
        if body.get("AssemblyDate"):
            a["AssemblyDate"] = _body_date(body["AssemblyDate"])
        if pending is not None:
            a["Batches"] = pending
        a["Modified"] = dt.date.today()
        return jsonify(_assembly(ds, a))

    @app.post("/Assemblies/<guid>/Complete")
    def complete_assembly(guid):
        a = next((a for a in ds.assemblies if a["Guid"] == guid), None)
        if a is None:
            return jsonify({"description": "Assembly not found"}), 404
        if a["Status"] != "Parked":
            return jsonify({"description": f"Assembly {a['AssemblyNumber']} is {a['Status']}"}), 400
        if a["ProductCode"] in BATCH_TRACKED and abs(
                sum(b["Quantity"] for b in a.get("Batches") or []) - a["Quantity"]) > 1e-9:
            # Assumed: what's made must all be in batches to complete it.
            return jsonify({"description": f"Batch numbers are required for {a['ProductCode']}"}), 400
        short = ds.complete(a)
        if short:
            return jsonify({"description": f"Not enough stock of {short} to complete the assembly"}), 400
        return jsonify(_assembly(ds, a))

    return app


# ---------------------------------------------------------------- helpers

def _paged(items, page):
    size = min(max(request.args.get("pageSize", default=200, type=int), 1), MAX_PAGE_SIZE)
    pages = max(1, math.ceil(len(items) / size))
    return jsonify({
        "Pagination": {"NumberOfItems": len(items), "PageSize": size,
                       "PageNumber": page, "NumberOfPages": pages},
        "Items": items[(page - 1) * size: page * size],
    })


def _ms(d):
    if d is None:
        return None
    epoch = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()
    return f"/Date({int(epoch * 1000)})/"


def _date_arg(name):
    v = request.args.get(name)
    return dt.date.fromisoformat(v[:10]) if v else None


def _body_date(v):
    """A date sent in a request body: "2026-07-15T00:00:00"."""
    return dt.date.fromisoformat(v[:10]) if v else None


def _ms_date_in(value):
    """The first "/Date(ms)/" in a request body. Unleashed returns dates that way, but
    rejects them in a request."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return next((found for v in value if (found := _ms_date_in(v))), None)
    return value if isinstance(value, str) and value.startswith("/Date(") else None


def _bad_date(body):
    return jsonify({"description": f"{_ms_date_in(body)} is not a valid value for DateTime."}), 400


def _modified(doc, *date_fields):
    """A document's last-modified date: an explicit "Modified", else its latest event."""
    return doc.get("Modified") or next((doc[f] for f in date_fields if doc.get(f)), None)


def _modified_since(modified):
    since = _date_arg("modifiedSince")
    return since is None or (modified is not None and modified >= since)


def _in_range(d, start, end):
    return (start is None or d >= start) and (end is None or d <= end)


def _statuses(arg="orderStatus"):
    v = request.args.get(arg)
    return {s.strip().lower() for s in v.split(",")} if v else set()


def _match(value, arg, exact=False):
    want = request.args.get(arg)
    if not want:
        return True
    return value.lower() == want.lower() if exact else want.lower() in value.lower()


def _warehouse(w):
    return {"Guid": w["Guid"], "WarehouseCode": w["WarehouseCode"],
            "WarehouseName": w["WarehouseName"], "IsDefault": w["IsDefault"]}


def _warehouse_ref(ds, code):
    w = next((w for w in ds.warehouses if w["WarehouseCode"] == code), None)
    return {"WarehouseCode": w["WarehouseCode"], "WarehouseName": w["WarehouseName"]} if w else WAREHOUSE_REF


def _product_ref(ds, code):
    p = ds.products[code]
    return {"Guid": p["Guid"], "ProductCode": code, "ProductDescription": p["ProductDescription"]}


def _product(ds, p):
    return {
        "Guid": p["Guid"], "ProductCode": p["ProductCode"],
        "ProductDescription": p["ProductDescription"],
        "ProductGroup": {"GroupName": p["ProductGroup"]},
        "UnitOfMeasure": {"Name": p["UnitOfMeasure"]},
        "DefaultPurchasePrice": p["BaseCost"], "DefaultSellPrice": p["SellPrice"],
        "Supplier": _supplier_ref(ds, p["SupplierCode"]) if p["SupplierCode"] else None,
        "IsSellable": p["SellPrice"] > 0, "IsAssembledProduct": p["SupplierCode"] is None,
        "IsBatchTracked": p["ProductCode"] in BATCH_TRACKED,
        "Obsolete": False,
    }


def _supplier_ref(ds, code):
    s = ds.suppliers[code]
    return {"Guid": s["Guid"], "SupplierCode": code, "SupplierName": s["SupplierName"]}


def _customer_ref(ds, code):
    c = ds.customers[code]
    return {"Guid": c["Guid"], "CustomerCode": code, "CustomerName": c["CustomerName"]}


WAREHOUSE_REF = {"WarehouseCode": "GDN", "WarehouseName": "Gardena Store"}


def _purchase_order(ds, po):
    lines = []
    for i, l in enumerate(po["Lines"], 1):
        lines.append({
            "LineNumber": i, "Product": _product_ref(ds, l["ProductCode"]),
            "OrderQuantity": l["OrderQuantity"], "ReceiptQuantity": l.get("ReceiptQuantity"),
            "UnitPrice": l["UnitPrice"], "LineTotal": l["LineTotal"], "LineTax": 0.0,
            "BCUnitPrice": l["UnitPrice"], "BCSubTotal": l["LineTotal"],
            "DueDate": _ms(po["RequiredDate"]),
        })
    freight = po["Freight"]
    return {
        "Guid": po["Guid"], "OrderNumber": po["OrderNumber"], "OrderStatus": po["OrderStatus"],
        "OrderDate": _ms(po["OrderDate"]), "RequiredDate": _ms(po["RequiredDate"]),
        "ReceivedDate": _ms(po["ReceivedDate"]), "CompletedDate": _ms(po["CompletedDate"]),
        "Supplier": _supplier_ref(ds, po["SupplierCode"]), "Warehouse": WAREHOUSE_REF,
        "Currency": {"CurrencyCode": "USD"}, "ExchangeRate": 1.0,
        "Tax": {"TaxCode": "NONE"}, "TaxRate": 0.0,
        "SubTotal": po["SubTotal"], "TaxTotal": 0.0, "Total": round(po["SubTotal"] + freight, 2),
        "BCSubTotal": po["SubTotal"], "BCTaxTotal": 0.0, "BCTotal": round(po["SubTotal"] + freight, 2),
        "CostLines": [{"LineNumber": 1, "Description": "Freight",
                       "Supplier": _supplier_ref(ds, po["SupplierCode"]),
                       "Currency": {"CurrencyCode": "USD"}, "Amount": freight}],
        "PurchaseOrderLines": lines,
        "LastModifiedOn": _ms(_modified(po, "CompletedDate", "OrderDate")),
    }


def _sales_order(ds, so):
    lines = []
    for i, l in enumerate(so["Lines"], 1):
        lines.append({
            "LineNumber": i, "Product": _product_ref(ds, l["ProductCode"]),
            "OrderQuantity": l["OrderQuantity"], "UnitPrice": l["UnitPrice"],
            "LineTotal": l["LineTotal"], "LineTax": 0.0,
            "BCUnitPrice": l["UnitPrice"], "BCLineTotal": l["LineTotal"],
            "AverageLandedPriceAtTimeOfSale": l["AverageLandedPriceAtTimeOfSale"],
        })
    return {
        "Guid": so["Guid"], "OrderNumber": so["OrderNumber"], "OrderStatus": so["OrderStatus"],
        "OrderDate": _ms(so["OrderDate"]), "RequiredDate": _ms(so["RequiredDate"]),
        "CompletedDate": _ms(so["CompletedDate"]),
        "Customer": _customer_ref(ds, so["CustomerCode"]), "Warehouse": WAREHOUSE_REF,
        "Currency": {"CurrencyCode": "USD"}, "ExchangeRate": 1.0,
        "SubTotal": so["SubTotal"], "TaxTotal": 0.0, "Total": so["SubTotal"],
        "BCSubTotal": so["SubTotal"], "BCTaxTotal": 0.0, "BCTotal": so["SubTotal"],
        "SalesOrderLines": lines,
        "LastModifiedOn": _ms(_modified(so, "CompletedDate", "OrderDate")),
    }


def _avg_cost(ds, code):
    qty, value = ds.stock_as_at(code)
    return value / qty if qty else ds.products[code]["BaseCost"]


def _bom(ds, b):
    lines = []
    for i, l in enumerate(b["Lines"], 1):
        lines.append({
            "Guid": f"{b['Guid'][:-4]}{i:04d}", "LineNumber": i,
            "Product": _product_ref(ds, l["ProductCode"]),
            "Quantity": l["Quantity"], "WastageQuantity": l["WastageQuantity"],
            "TotalLineCost": round(l["Quantity"] * _avg_cost(ds, l["ProductCode"]), 4),
        })
    return {
        "Guid": b["Guid"], "BillNumber": b["BillNumber"],
        "Product": _product_ref(ds, b["ProductCode"]),
        "CanAutoAssemble": False, "CanAutoDisassemble": False, "Obsolete": b["Obsolete"],
        "BillOfMaterialsLines": lines, "LastModifiedOn": _ms(ds.start),
    }


def _assembly(ds, a):
    lines = []
    for i, l in enumerate(a["Lines"], 1):
        lines.append({
            "Guid": f"{a['Guid'][:-4]}{i:04d}", "LineNumber": i,
            "Product": _product_ref(ds, l["ProductCode"]),
            "Quantity": l["Quantity"], "WastageQuantity": l["WastageQuantity"],
            **({"UnitCost": l["UnitCost"]} if "UnitCost" in l else {}),
        })
    return {
        "Guid": a["Guid"], "AssemblyNumber": a["AssemblyNumber"],
        "Product": _product_ref(ds, a["ProductCode"]), "Quantity": a["Quantity"],
        "AssemblyDate": _ms(a["AssemblyDate"]), "AssembleBy": _ms(a["AssembleBy"]),
        "AssemblyStatus": a["Status"],
        "SourceWarehouse": _warehouse_ref(ds, a.get("SourceWarehouse")),
        "DestinationWarehouse": _warehouse_ref(ds, a.get("DestinationWarehouse")),
        "Comments": a.get("Comments"),
        # The finished product's batches, entered in the web app: pending until it's completed.
        ("BatchNumbers" if a["Status"] == "Completed" else "PendingBatchNumbers"): [
            {"Number": b["Number"], "Quantity": b["Quantity"], "ExpiryDate": _ms(b["ExpiryDate"])}
            for b in a.get("Batches") or []],
        "AssemblyLines": lines, "LastModifiedOn": _ms(_modified(a, "AssemblyDate")),
    }


def _adjustment(ds, a):
    return {
        "Guid": a["Guid"], "AdjustmentNumber": a["AdjustmentNumber"],
        "AdjustmentDate": _ms(a["AdjustmentDate"]), "AdjustmentReason": a["AdjustmentReason"],
        "Status": a["Status"], "Warehouse": WAREHOUSE_REF,
        "StockAdjustmentLines": [
            {"Product": _product_ref(ds, l["ProductCode"]),
             **{k: l[k] for k in ("QuantityChange", "ValueChange", "NewQuantity",
                                  "NewActualValue", "Comments") if k in l}}
            for l in a["Lines"]
        ],
    }
