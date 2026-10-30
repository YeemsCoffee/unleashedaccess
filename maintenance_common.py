"""Shared by the maintenance modules: the error shown to people, form parsing, files."""
import re

DUE_SOON_DAYS = 7
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_OPERATIONS = 100  # steps in a task list
# What can be attached, and the type each is served as.
FILE_TYPES = {
    "pdf": "application/pdf", "txt": "text/plain", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "heic": "image/heic", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# How critical a location or a piece of equipment is to the business (SAP's ABC indicator).
CRITICALITIES = {"A": "A: stops the business", "B": "B: slows it down", "C": "C: can wait"}
PRIORITIES = {1: "Very high", 2: "High", 3: "Medium", 4: "Low"}
# The priority a new request starts with, from the criticality of what it's about.
PRIORITY_FOR_CRITICALITY = {"A": 2, "B": 3, "C": 4}
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]*$")


class MaintenanceError(ValueError):
    """A problem to show the person (missing name, location in use)."""


def text(form, key, limit):
    """A single-line text field, spaces tidied; None when empty."""
    return " ".join((form.get(key) or "").split())[:limit] or None


def long_text(form, key, limit=5000):
    return (form.get(key) or "").strip()[:limit] or None


def whole(value, label, minimum=None, required=False):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        if required:
            raise MaintenanceError(f"{label} is needed.")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MaintenanceError(f"{label} must be a whole number.") from None
    if minimum is not None and number < minimum:
        raise MaintenanceError(f"{label} must be at least {minimum}.")
    return number


def number(value, label, maximum=10000, required=False, minimum=0):
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        if required:
            raise MaintenanceError(f"{label} is needed.")
        return None
    try:
        result = float(str(value).replace(",", ""))
    except ValueError:
        raise MaintenanceError(f"{label} must be a number.") from None
    if not minimum <= result <= maximum:
        raise MaintenanceError(f"{label} must be between {minimum:,g} and {maximum:,g}.")
    return result


def date(value, label, required=False):
    import datetime as dt
    value = (value or "").strip()
    if not value:
        if required:
            raise MaintenanceError(f"Enter {label}.")
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise MaintenanceError(f"Enter {label} as a date.") from None


def checked_file(filename, data):
    """(clean file name, content type) for an upload, or MaintenanceError."""
    filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not filename:
        raise MaintenanceError("Choose a file to attach.")
    if extension not in FILE_TYPES:
        raise MaintenanceError("Attach a PDF, Word or Excel file, a picture (PNG, JPG, GIF, HEIC) or a text file.")
    if not data:
        raise MaintenanceError(f"{filename} is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise MaintenanceError(f"{filename} is too big: files can be 16 MB at most.")
    return filename[:200], FILE_TYPES[extension]


def fmt_number(value):
    """1,234,567 or 2.5: no exponents, no needless decimals."""
    if value is None or value == "":
        return ""
    try:
        value = float(str(value).replace(",", ""))
    except ValueError:
        return str(value)
    if value.is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def code(value):
    """A location code as entered: upper case, no spaces; None when empty."""
    value = "".join((value or "").split()).upper()
    if not value:
        return None
    if len(value) > 40 or not CODE_RE.match(value):
        raise MaintenanceError("Codes use letters, numbers and - . _ / (40 at most), e.g. GAR-KIT-ESP.")
    return value
