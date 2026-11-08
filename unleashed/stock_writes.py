"""Stock adjustments and warehouse stock transfers made from Core.

Unleashed stays the system of record; these write to it through the API, as documented at
apidocs.unleashedsoftware.com:

  StockAdjustments:        POST /StockAdjustments/{guid} only. There is no update, complete
                           or delete, so Core makes adjustments completed: the stock moves
                           straight away, and one made by mistake is undone with another.
                           Each line gives the product's quantity and value in the
                           warehouse after it (NewQuantity, NewActualValue; Unleashed works
                           out the change); a batch-tracked product's batches, each with its
                           quantity after, add up to that quantity.
  WarehouseStockTransfers: POST (parked or placed; Unleashed takes no Guid of ours), PUT
                           /{guid} (header and existing lines), POST/PUT/DELETE
                           /{guid}/Lines[/{lineGuid}], POST /{guid}/Complete (the stock
                           moves), DELETE /{guid} (parked or placed).
  StockCounts:             read only through the API, so stock counts are kept in Core
                           (stock_counts.py) and their differences posted as an adjustment.

Every write is read back: if Unleashed didn't keep it, StockWriteError says so and nothing
further is sent.
"""
import datetime as dt
import uuid

from .client import UnleashedError, body_date
from .order_writes import TOLERANCE, OrderError, _get, money

OPEN_TRANSFERS = ("Parked", "Placed")


class StockWriteError(OrderError):
    """Why an adjustment or transfer stopped, in words for the person making it."""


def adjustment_reasons(client):
    """The reasons Unleashed takes, as listed there (none if it won't say)."""
    try:
        return sorted({r.get("Reason") for r in client.get_all("AdjustmentReasons") if r.get("Reason")},
                      key=str.lower)
    except UnleashedError:
        return []


# ---------------------------------------------------------------- adjustments

def create_adjustment(client, adjustment):
    """Make a completed stock adjustment. `adjustment`: {"warehouse", "reason", "date"
    (default today), "lines": [{"product_code", "new_qty", "new_value", "comments", "batches":
    [{"number", "qty", "expiry"}] for a batch-tracked product}]}. Returns the adjustment as
    Unleashed holds it."""
    lines = adjustment.get("lines") or []
    if not adjustment.get("warehouse"):
        raise StockWriteError("Choose the warehouse.")
    if not (adjustment.get("reason") or "").strip():
        raise StockWriteError("Choose why the stock changed.")
    if not lines:
        raise StockWriteError("Add at least one product.")
    seen = set()
    for l in lines:
        if not l.get("product_code"):
            raise StockWriteError("Choose a product for each line.")
        if l["product_code"] in seen:
            raise StockWriteError(f"{l['product_code']} is on two lines: put it on one.")
        seen.add(l["product_code"])
        if l.get("new_qty") is None or l["new_qty"] < 0:
            raise StockWriteError(f"Enter the quantity of {l['product_code']} there should be (0 or more).")
        if l.get("batches") is not None and abs(sum(b["qty"] for b in l["batches"]) - l["new_qty"]) > TOLERANCE:
            raise StockWriteError(f"{l['product_code']}'s batches should add up to {l['new_qty']:g}.")
    guid = str(uuid.uuid4())
    body = {
        "Guid": guid,
        "Warehouse": {"WarehouseCode": adjustment["warehouse"]},
        "AdjustmentDate": body_date(adjustment.get("date") or dt.date.today()),
        "AdjustmentReason": adjustment["reason"].strip()[:20],
        "Status": "Completed",
        "StockAdjustmentLines": [{
            "Product": {"ProductCode": l["product_code"]},
            "NewQuantity": l["new_qty"],
            "NewActualValue": money(l["new_value"] or 0) if l["new_qty"] else 0,
            "Comments": (l.get("comments") or "").strip()[:200] or None,
            **({"BatchNumbers": [{"Number": b["number"], "Quantity": b["qty"],
                                  **({"ExpiryDate": body_date(b["expiry"])} if b.get("expiry") else {})}
                                 for b in l["batches"] if b["qty"]]} if l.get("batches") is not None else {}),
        } for l in lines],
    }
    try:
        client.post(f"StockAdjustments/{guid}", body)
    except UnleashedError:
        # It may have been made all the same (a timeout after Unleashed kept it): look before saying no.
        if _get(client, "StockAdjustments", guid) is None:
            raise
    made = _get(client, "StockAdjustments", guid)
    if made is None:
        raise StockWriteError("Unleashed didn't keep the adjustment.")
    if (made.get("Status") or "") != "Completed":
        raise StockWriteError(f"Unleashed kept {made.get('AdjustmentNumber')} as {(made.get('Status') or 'no status').lower()}, "
                              "not completed: finish it in Unleashed.")
    have = {(l.get("Product") or {}).get("ProductCode"): l for l in made.get("StockAdjustmentLines") or []}
    for l in lines:
        got = have.get(l["product_code"])
        if got is None or (got.get("NewQuantity") is not None
                           and abs(float(got["NewQuantity"]) - l["new_qty"]) > TOLERANCE):
            raise StockWriteError(f"{made.get('AdjustmentNumber')} was made, but Unleashed doesn't show "
                                  f"{l['product_code']} at {l['new_qty']:g}: check it in Unleashed.")
    return made


# ---------------------------------------------------------------- transfers

def _transfer_status(doc):
    return doc.get("TransferStatus") or ""


def _check_transfer(transfer):
    if not transfer.get("source") or not transfer.get("destination"):
        raise StockWriteError("Choose the warehouse it comes from and the one it goes to.")
    if transfer["source"] == transfer["destination"]:
        raise StockWriteError("It has to go to a different warehouse.")
    lines = transfer.get("lines") or []
    if not lines:
        raise StockWriteError("Add at least one product.")
    for l in lines:
        if not l.get("product_code"):
            raise StockWriteError("Choose a product for each line.")
        if not l.get("qty") or l["qty"] <= 0:
            raise StockWriteError(f"Enter how much {l['product_code']} to move.")


def _same_transfer_lines(doc, wanted):
    def key(code, qty):
        return code, round(float(qty or 0), 3)
    have = sorted(key((l.get("Product") or {}).get("ProductCode"), l.get("TransferQuantity"))
                  for l in doc.get("TransferDetails") or [])
    want = sorted(key(l["product_code"], l["qty"]) for l in wanted)
    return len(have) == len(want) and all(h[0] == w[0] and abs(h[1] - w[1]) <= TOLERANCE for h, w in zip(have, want))


def _warehouses(transfer):
    return {"SourceWarehouse": {"WarehouseCode": transfer["source"]},
            "DestinationWarehouse": {"WarehouseCode": transfer["destination"]}}


def create_transfer(client, transfer, place=False):
    """Make a transfer, parked (or placed). `transfer`: {"source", "destination", "comments",
    "lines": [{"product_code", "qty", "comments"}]}. Returns it as Unleashed holds it."""
    _check_transfer(transfer)
    body = {**_warehouses(transfer), "Comments": (transfer.get("comments") or "").strip()[:1024] or None,
            "TransferStatus": "Placed" if place else "Parked",
            "TransferDetails": [{"Product": {"ProductCode": l["product_code"]}, "TransferQuantity": l["qty"],
                                 "Comments": (l.get("comments") or "").strip()[:1024] or None}
                                for l in transfer["lines"]]}
    made = client.post("WarehouseStockTransfers", body)
    guid = made.get("Guid") if isinstance(made, dict) else None
    done = _get(client, "WarehouseStockTransfers", guid) if guid else None
    if done is None:
        raise StockWriteError("Unleashed didn't say it kept the transfer: look for it in Unleashed before trying again.")
    if not _same_transfer_lines(done, transfer["lines"]):
        raise StockWriteError(f"{done.get('TransferOrderNumber')} was made, but its lines aren't the ones sent: "
                              "check it in Unleashed.")
    return done


def update_transfer(client, guid, transfer, place=None):
    """Change an open transfer: its warehouses, comments, status (placed when `place`) and
    lines (`transfer["lines"]` with "guid" for the ones it has: those left out are removed,
    those without one added). Returns it as Unleashed holds it."""
    _check_transfer(transfer)
    current = _get(client, "WarehouseStockTransfers", guid)
    if current is None:
        raise StockWriteError("That transfer isn't in Unleashed any more.")
    if _transfer_status(current) not in OPEN_TRANSFERS:
        raise StockWriteError(f"{current.get('TransferOrderNumber')} is {_transfer_status(current).lower()}, "
                              "so it can't be changed.")
    existing = {l.get("Guid"): l for l in current.get("TransferDetails") or []}
    kept, added = [], []
    for l in transfer["lines"]:
        old = existing.get(l.get("guid"))
        if old is not None and (old.get("Product") or {}).get("ProductCode") == l["product_code"]:
            kept.append((old, l))
        else:
            added.append(l)
    kept_guids = {old.get("Guid") for old, _ in kept}
    status = "Placed" if place else _transfer_status(current) if place is None else "Parked"
    client.put(f"WarehouseStockTransfers/{guid}", {
        "Guid": guid, **_warehouses(transfer), "TransferStatus": status,
        "Comments": (transfer.get("comments") or "").strip()[:1024] or None,
        "TransferDetails": [{"Guid": old.get("Guid"), "TransferQuantity": l["qty"],
                             "Comments": (l.get("comments") or "").strip()[:1024] or None} for old, l in kept]})
    for line_guid in [g for g in existing if g not in kept_guids]:
        client.delete(f"WarehouseStockTransfers/{guid}/Lines/{line_guid}")
    for l in added:
        client.post(f"WarehouseStockTransfers/{guid}/Lines", {
            "Product": {"ProductCode": l["product_code"]}, "TransferQuantity": l["qty"],
            "Comments": (l.get("comments") or "").strip()[:1024] or None})
    done = _get(client, "WarehouseStockTransfers", guid) or {}
    if not _same_transfer_lines(done, transfer["lines"]):
        raise StockWriteError(f"Unleashed didn't keep all the changes to {current.get('TransferOrderNumber')}: "
                              "check it in Unleashed.")
    want_source, want_destination = transfer["source"], transfer["destination"]
    if ((done.get("SourceWarehouse") or {}).get("WarehouseCode"), (done.get("DestinationWarehouse") or {}).get(
            "WarehouseCode")) != (want_source, want_destination):
        raise StockWriteError(f"Unleashed didn't change {current.get('TransferOrderNumber')}'s warehouses.")
    return done


def complete_transfer(client, guid):
    """Complete an open transfer: the stock moves. Returns it as Unleashed holds it."""
    current = _get(client, "WarehouseStockTransfers", guid)
    if current is None:
        raise StockWriteError("That transfer isn't in Unleashed any more.")
    if _transfer_status(current) == "Completed":
        return current
    if _transfer_status(current) not in OPEN_TRANSFERS:
        raise StockWriteError(f"{current.get('TransferOrderNumber')} is {_transfer_status(current).lower()}, "
                              "so it can't be completed.")
    client.post(f"WarehouseStockTransfers/{guid}/Complete", {})
    done = _get(client, "WarehouseStockTransfers", guid) or {}
    if _transfer_status(done) != "Completed":
        raise StockWriteError(f"Unleashed didn't complete {current.get('TransferOrderNumber')} "
                              f"(it's {_transfer_status(done).lower() or 'without a status'}).")
    return done


def delete_transfer(client, guid):
    current = _get(client, "WarehouseStockTransfers", guid)
    if current is None or _transfer_status(current) == "Deleted":
        return current
    if _transfer_status(current) not in OPEN_TRANSFERS:
        raise StockWriteError(f"{current.get('TransferOrderNumber')} is {_transfer_status(current).lower()}, "
                              "so it can't be deleted.")
    client.delete(f"WarehouseStockTransfers/{guid}")
    after = _get(client, "WarehouseStockTransfers", guid)
    if after is not None and _transfer_status(after) != "Deleted":
        raise StockWriteError(f"Unleashed didn't delete {current.get('TransferOrderNumber')}.")
    return after
