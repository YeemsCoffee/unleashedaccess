"""Production orders written to Unleashed: starting one, and closing one out.

Starting an order creates a parked assembly (POST /Assemblies). Unleashed numbers
it and fills its component quantities from the product's bill of materials.

Close-out replaces those quantities with what the team recorded (each component's
total, wastage included, so wastage is sent as 0), sets the quantity made and the
assembly date, then completes the assembly, which moves the stock. A completed
assembly can't be edited or reversed in Unleashed, so the quantities are read back
before completing: if Unleashed didn't keep them, the order is left parked and
nothing moves.

For a batch-tracked product, the batches made (number, quantity, expiry date) go in
the same update as PendingBatchNumbers, which completing turns into batches. They're
read back too: if Unleashed didn't keep them as sent, the order is left parked for
them to be checked and the order completed in Unleashed.
"""
from .client import parse_date, writable
from .reports import COMPLETED_ASSEMBLY_STATUSES, INACTIVE_ASSEMBLY_STATUSES

# Unleashed may round quantities to a product's decimal places; this much difference is the
# same quantity.
TOLERANCE = 0.0051
# Batch and serial numbers for what's made. Sent only when given: Unleashed requires a
# batch-tracked (or serialized) product when they're included.
TRACKING_FIELDS = ("PendingBatchNumbers", "PendingSerialNumbers", "BatchNumbers", "SerialNumbers")


class CloseOutError(Exception):
    """Why a change to Unleashed stopped, in words for the person making it."""


def create_assembly(client, product_code, qty, assemble_by, from_warehouse=None, to_warehouse=None,
                    comments=None):
    """Start a production order: a parked assembly making `qty` of `product_code` by
    `assemble_by`, taking components from `from_warehouse` and putting what's made into
    `to_warehouse` (warehouse codes). Returns it as Unleashed reports it, number and
    components included."""
    body = {"Product": {"ProductCode": product_code}, "Quantity": qty,
            "AssembleBy": f"{assemble_by.isoformat()}T00:00:00"}
    if from_warehouse:
        body["SourceWarehouse"] = {"WarehouseCode": from_warehouse}
    if to_warehouse:
        body["DestinationWarehouse"] = {"WarehouseCode": to_warehouse}
    if comments:
        body["Comments"] = comments
    created = client.post("Assemblies", body)
    if isinstance(created, dict) and created.get("Items"):
        created = created["Items"][0]
    guid = created.get("Guid") if isinstance(created, dict) else None
    if not guid:
        raise CloseOutError("Unleashed accepted the order but didn't say which one it is. It will "
                            "show under Production orders after the next sync.")
    order = client.get(f"Assemblies/{guid}")
    if not order.get("AssemblyNumber"):
        raise CloseOutError("Unleashed created the order, but reading it back failed. It will show "
                            "under Production orders after the next sync.")
    return order


def close_out(client, guid, quantities, made, on_date, batches=None):
    """Send `quantities` ({component code: total used}) and `made` (quantity produced) to the
    parked assembly `guid`, dated `on_date`, and complete it. Returns the completed assembly
    as Unleashed reports it. Raises CloseOutError (or UnleashedError) without completing.

    `batches` ([{"number", "qty", "expiry"}]) are the batches made, for a batch-tracked
    product. If Unleashed takes the quantities but doesn't keep the batches as sent, the
    assembly is returned still parked (see is_completed)."""
    client.clear_cache()
    current = client.get(f"Assemblies/{guid}")
    number = current.get("AssemblyNumber") or guid
    status = _status(current)
    if status in COMPLETED_ASSEMBLY_STATUSES:
        if _matches(current, quantities, made, batches):  # a retry after Unleashed completed it
            return current
        raise CloseOutError(f"{number} is already completed in Unleashed, with other quantities. "
                            "Nothing was sent.")
    if status in INACTIVE_ASSEMBLY_STATUSES:
        raise CloseOutError(f"{number} was deleted in Unleashed. Nothing was sent.")
    lines = current.get("AssemblyLines") or []
    on_order = {(l.get("Product") or {}).get("ProductCode") for l in lines}
    missing = sorted(code for code in quantities if code not in on_order)
    if missing:
        raise CloseOutError(f"{number} in Unleashed has no line for {', '.join(missing)}, so its "
                            "quantity can't be sent. Nothing was sent.")

    # Every field goes back as it is (a field left blank in a PUT is cleared), dated in a
    # form Unleashed accepts.
    body = writable(current)
    body["Quantity"] = made
    body["AssemblyDate"] = f"{on_date.isoformat()}T00:00:00"
    for field in TRACKING_FIELDS:
        if not body.get(field):
            body.pop(field, None)
    if batches:
        body["PendingBatchNumbers"] = [{"Number": b["number"], "Quantity": b["qty"],
                                        "ExpiryDate": f"{b['expiry']}T00:00:00"} for b in batches]
    seen = set()
    for line in body.get("AssemblyLines") or []:
        code = (line.get("Product") or {}).get("ProductCode")
        line["Quantity"] = round(quantities.get(code, 0), 4) if code not in seen else 0
        line["WastageQuantity"] = 0
        seen.add(code)
    client.put(f"Assemblies/{guid}", body)

    updated = client.get(f"Assemblies/{guid}")
    if not _matches(updated, quantities, made):
        raise CloseOutError(f"Unleashed didn't keep the new quantities for {number}, so it was left "
                            "parked and not completed. Check it in Unleashed, then try again.")
    if batches and not _same_batches(updated.get("PendingBatchNumbers"), batches):
        return updated  # left parked: its batches are checked, and it's completed, in Unleashed
    client.post(f"Assemblies/{guid}/Complete", {})
    done = client.get(f"Assemblies/{guid}")
    if _status(done) not in COMPLETED_ASSEMBLY_STATUSES:
        raise CloseOutError(f"Unleashed took the quantities for {number} but didn't complete it "
                            f"(it's {_status(done) or 'without a status'}). Complete it in Unleashed.")
    return done


def is_completed(assembly):
    return _status(assembly) in COMPLETED_ASSEMBLY_STATUSES


def sent_summary(done):
    """What Unleashed holds for a completed assembly, for the record."""
    return {
        "made": done.get("Quantity"), "date": str(parse_date(done.get("AssemblyDate")) or ""),
        "components": {(l.get("Product") or {}).get("ProductCode"):
                       (l.get("Quantity") or 0) + (l.get("WastageQuantity") or 0)
                       for l in done.get("AssemblyLines") or []},
    }


def _status(assembly):
    return (assembly.get("AssemblyStatus") or assembly.get("Status") or "").lower()


def _matches(assembly, quantities, made, batches=None):
    if abs((assembly.get("Quantity") or 0) - made) > TOLERANCE:
        return False
    if batches and not _same_batches(assembly.get("BatchNumbers"), batches):
        return False
    used = {}
    for l in assembly.get("AssemblyLines") or []:
        code = (l.get("Product") or {}).get("ProductCode")
        used[code] = used.get(code, 0) + (l.get("Quantity") or 0) + (l.get("WastageQuantity") or 0)
    return all(abs(used.get(code, 0) - qty) <= TOLERANCE for code, qty in quantities.items())


def _same_batches(held, batches):
    """Whether Unleashed's batches (`held`, as it lists them) are `batches`: the same
    numbers, quantities and expiry dates."""
    held = {(b.get("Number") or "").lower(): b for b in held or []}
    return len(held) == len(batches) and all(
        (h := held.get(b["number"].lower())) is not None
        and abs((h.get("Quantity") or 0) - b["qty"]) <= TOLERANCE
        and str(parse_date(h.get("ExpiryDate")) or "") == b["expiry"]
        for b in batches)
