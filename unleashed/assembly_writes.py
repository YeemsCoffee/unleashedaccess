"""Production orders written to Unleashed: starting one, and closing one out.

Starting an order creates a parked assembly (POST /Assemblies). Unleashed numbers
it and fills its component quantities from the product's bill of materials.

Close-out replaces those quantities with what the team recorded (each component's
total, wastage included, so wastage is sent as 0), sets the quantity made and the
assembly date, then completes the assembly, which moves the stock. A completed
assembly can't be edited or reversed in Unleashed, so the quantities are read back
before completing: if Unleashed didn't keep them, the order is left parked and
nothing moves.
"""
import copy

from .client import parse_date
from .reports import COMPLETED_ASSEMBLY_STATUSES, INACTIVE_ASSEMBLY_STATUSES

# Unleashed may round quantities to a product's decimal places; this much difference is the
# same quantity.
TOLERANCE = 0.0051


class CloseOutError(Exception):
    """Why a change to Unleashed stopped, in words for the person making it."""


def create_assembly(client, product_code, qty, assemble_by, warehouse_code=None, comments=None):
    """Start a production order: a parked assembly making `qty` of `product_code` by
    `assemble_by`. Returns it as Unleashed reports it, number and components included."""
    body = {"Product": {"ProductCode": product_code}, "Quantity": qty,
            "AssembleBy": f"{assemble_by.isoformat()}T00:00:00"}
    if warehouse_code:
        body["SourceWarehouse"] = body["DestinationWarehouse"] = {"WarehouseCode": warehouse_code}
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


def close_out(client, guid, quantities, made, on_date):
    """Send `quantities` ({component code: total used}) and `made` (quantity produced) to the
    parked assembly `guid`, dated `on_date`, and complete it. Returns the completed assembly
    as Unleashed reports it. Raises CloseOutError (or UnleashedError) without completing."""
    client.clear_cache()
    current = client.get(f"Assemblies/{guid}")
    number = current.get("AssemblyNumber") or guid
    status = _status(current)
    if status in COMPLETED_ASSEMBLY_STATUSES:
        if _matches(current, quantities, made):  # a retry after Unleashed already completed it
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

    body = copy.deepcopy(current)
    body["Quantity"] = made
    body["AssemblyDate"] = f"{on_date.isoformat()}T00:00:00"
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
    client.post(f"Assemblies/{guid}/Complete", {})
    done = client.get(f"Assemblies/{guid}")
    if _status(done) not in COMPLETED_ASSEMBLY_STATUSES:
        raise CloseOutError(f"Unleashed took the quantities for {number} but didn't complete it "
                            f"(it's {_status(done) or 'without a status'}). Complete it in Unleashed.")
    return done


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


def _matches(assembly, quantities, made):
    if abs((assembly.get("Quantity") or 0) - made) > TOLERANCE:
        return False
    used = {}
    for l in assembly.get("AssemblyLines") or []:
        code = (l.get("Product") or {}).get("ProductCode")
        used[code] = used.get(code, 0) + (l.get("Quantity") or 0) + (l.get("WastageQuantity") or 0)
    return all(abs(used.get(code, 0) - qty) <= TOLERANCE for code, qty in quantities.items())
