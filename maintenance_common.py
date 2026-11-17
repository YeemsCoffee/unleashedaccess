"""Shared by the maintenance modules: the error shown to people, form parsing, files."""
import re

DUE_SOON_DAYS = 7
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_OPERATIONS = 100  # steps on a task
MAX_HAZARDS = 50  # hazards on a task, each with what's done about it
# What can be attached, and the type each is served as.
FILE_TYPES = {
    "pdf": "application/pdf", "txt": "text/plain", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "heic": "image/heic", "doc": "application/msword",
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
# Where a task is in its life: active tasks come due; inactive ones are paused (they never come
# due, and can be switched back on); obsolete ones are retired, kept for their history.
TASK_STATES = {"active": "Active", "inactive": "Inactive", "obsolete": "Obsolete"}
# A task's job plan, SIMPTWW: what to know before starting. (key, letter, heading, what it
# covers); pictures attached to a task can be filed under one of the keys.
SIMPTWW = (
    ("safety", "S", "Safety", "Each hazard and what's done about it: PPE, lockout/tagout, isolation"),
    ("info", "I", "Information", "Steps, instructions and the documents to follow"),
    ("material", "M", "Material", "The part to order, consumables, chemicals"),
    ("people", "P", "People", "How many people it takes"),
    ("tools", "T", "Tools", "Tools and equipment to bring"),
    ("where", "W", "Where", "The location or equipment, and how to get to it"),
    ("when", "W", "When", "How often it's due, by time or by usage, and the best time to do it"),
)
SIMPTWW_KEYS = tuple(s[0] for s in SIMPTWW)


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
        raise MaintenanceError("Attach a PDF, Word or Excel file, a picture (PNG, JPG, GIF, WebP, HEIC) or a text file.")
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


def form_steps(form):
    """A task's steps from the form's op-text-N / op-minutes-N rows ("rows" of them, the
    page adds rows without saving): [{"text", "minutes"}], empty rows left out."""
    steps = []
    for i in range(min(whole(form.get("rows"), "Rows") or 0, MAX_OPERATIONS)):
        step = text(form, f"op-text-{i}", 300)
        if step:
            steps.append({"text": step, "minutes": whole(form.get(f"op-minutes-{i}"), f"Minutes for “{step}”",
                                                          minimum=0)})
    return steps


def form_hazards(form):
    """A task's hazards from the form's hazard-N / mitigation-N rows ("hazard_rows" of them):
    [{"hazard", "mitigation"}], empty rows left out. A mitigation needs its hazard named."""
    hazards = []
    for i in range(min(whole(form.get("hazard_rows"), "Rows") or 0, MAX_HAZARDS)):
        hazard, mitigation = text(form, f"hazard-{i}", 300), text(form, f"mitigation-{i}", 500)
        if mitigation and not hazard:
            raise MaintenanceError(f"Say what the hazard is for “{mitigation[:60]}”.")
        if hazard:
            hazards.append({"hazard": hazard, "mitigation": mitigation or ""})
    return hazards


def schedule_choice(form):
    """How a task's form says it's scheduled: "time" (every so many days) or "usage" (every so
    much on a counter). It's one or the other. A form that doesn't say (schedule_by) is read
    from what's filled in, and refused when both are."""
    chosen = (form.get("schedule_by") or "").strip()
    if chosen in ("time", "usage"):
        return chosen
    by_time, by_usage = bool(str(form.get("frequency_days") or "").strip()), bool(str(form.get("counter_id") or "").strip())
    if by_time and by_usage:
        raise MaintenanceError("A task is due by time or by usage, not both: choose one.")
    return "usage" if by_usage else "time"
