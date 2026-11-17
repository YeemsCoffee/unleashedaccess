"""Team accounts: each person signs in with their own email and password.

Passwords are stored only as salted hashes (werkzeug's scrypt). Every account
has a session version; bumping it (password change or reset, deactivation)
signs that person out everywhere.
"""
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from unleashed.store import utc_now

MIN_PASSWORD_LENGTH = 10
MAX_FAILED_SIGN_INS = 5
LOCK_SECONDS = 15 * 60
ROLES = ("admin", "member")
SIGN_IN_FAILED = "That email and password don't match an account."
# After MAX_FAILED_SIGN_INS wrong tries, sign-in with that email is refused from that device
# (network address) for LOCK_SECONDS. Counted per email and device, so someone guessing at an
# account locks only themselves out, not its owner; and the same for an email with no account,
# so the message doesn't say which emails have one.
TOO_MANY_TRIES = ("Too many wrong tries from this device. Try again in 15 minutes, or ask an admin to reset "
                  "your password.")
# Checked when an email isn't found, so a wrong email takes as long as a wrong password.
_UNUSED_HASH = generate_password_hash(secrets.token_hex(16))


class AccountError(ValueError):
    """A problem to show the person (bad input, rule broken)."""


def temporary_password():
    return secrets.token_urlsafe(9)  # 12 characters


def check_new_password(password, confirm=None):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
    if confirm is not None and password != confirm:
        raise AccountError("The two new passwords don't match.")


def _normalise_email(email):
    email = (email or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise AccountError("Enter a valid email address.")
    return email


class UserStore:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ reading

    def count(self):
        return self.db.query("SELECT COUNT(*) AS n FROM app_users")[0]["n"]

    def get(self, user_id):
        rows = self.db.query("SELECT * FROM app_users WHERE id = ?", (user_id,))
        return rows[0] if rows else None

    def by_email(self, email):
        rows = self.db.query("SELECT * FROM app_users WHERE email = ?",
                             ((email or "").strip().lower(),))
        return rows[0] if rows else None

    def all(self):
        return self.db.query("SELECT * FROM app_users ORDER BY active DESC, lower(name), email")

    def active_admin_count(self):
        return self.db.query("SELECT COUNT(*) AS n FROM app_users"
                             " WHERE role = 'admin' AND active = 1")[0]["n"]

    # ------------------------------------------------------------ signing in

    def authenticate(self, email, password, client=""):
        """(user, None) on success, or (None, message to show). `client`: where the try comes from
        (a network address), which wrong tries are counted against."""
        key, client = (email or "").strip().lower()[:254], (client or "")[:64]
        tries = self._attempts(key, client)
        if tries and tries["locked_until"] and tries["locked_until"] > utc_now():
            check_password_hash(_UNUSED_HASH, password or "")
            return None, TOO_MANY_TRIES
        user = self.by_email(email)
        if user is None or not user["active"]:
            check_password_hash(_UNUSED_HASH, password or "")
            user = None
        elif check_password_hash(user["password_hash"], password or ""):
            self._try_write("DELETE FROM sign_in_attempts WHERE email = ? AND client = ?", (key, client))
            self._try_write("UPDATE app_users SET last_sign_in_at = ? WHERE id = ?", (utc_now(), user["id"]))
            return user, None
        failed = (tries["failed"] if tries else 0) + 1
        locked = failed >= MAX_FAILED_SIGN_INS
        self._try_write("DELETE FROM sign_in_attempts WHERE email = ? AND client = ?", (key, client))
        self._try_write("INSERT INTO sign_in_attempts (email, client, failed, locked_until, updated_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (key, client, 0 if locked else failed, utc_now(LOCK_SECONDS) if locked else None, utc_now()))
        # Tries from a day ago no longer count; clearing them keeps the table small.
        self._try_write("DELETE FROM sign_in_attempts WHERE updated_at < ?", (utc_now(-24 * 3600),))
        return None, TOO_MANY_TRIES if locked else SIGN_IN_FAILED

    def _attempts(self, key, client):
        try:
            rows = self.db.query("SELECT failed, locked_until FROM sign_in_attempts WHERE email = ? AND client = ?",
                                 (key, client))
        except Exception:  # noqa: BLE001 (a viewer on a database from before this table)
            return None
        return rows[0] if rows else None

    def sign_out_everywhere(self, user_id):
        """End every session of this person (sign-out: a copied cookie stops working too)."""
        self._try_write("UPDATE app_users SET session_version = session_version + 1 WHERE id = ?", (user_id,))

    def _try_write(self, sql, args):
        # View-only instances may use a read-only database login; sign-in still works there.
        try:
            self.db.execute(sql, args)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ changing accounts

    def create(self, email, name, password, role="member", must_change=True):
        email = _normalise_email(email)
        name = (name or "").strip() or email.split("@")[0]
        if role not in ROLES:
            raise AccountError("Unknown role.")
        check_new_password(password)
        if self.by_email(email):
            raise AccountError(f"There's already an account for {email}.")
        self.db.execute(
            "INSERT INTO app_users (email, name, password_hash, role, active, must_change_password,"
            " session_version, failed_sign_ins, created_at) VALUES (?, ?, ?, ?, 1, ?, 1, 0, ?)",
            (email, name, generate_password_hash(password), role, 1 if must_change else 0,
             utc_now()))
        return self.by_email(email)

    def set_password(self, user_id, password, must_change=False):
        check_new_password(password)
        self.db.execute(
            "UPDATE app_users SET password_hash = ?, must_change_password = ?,"
            " session_version = session_version + 1, failed_sign_ins = 0, locked_until = NULL"
            " WHERE id = ?", (generate_password_hash(password), 1 if must_change else 0, user_id))
        user = self.get(user_id)
        if user:  # a new password lifts any lock on signing in
            self._try_write("DELETE FROM sign_in_attempts WHERE email = ?", (user["email"],))
        return user

    def reset_password(self, user_id):
        """Give the person a temporary password they must change; returns it."""
        password = temporary_password()
        self.set_password(user_id, password, must_change=True)
        return password

    def set_active(self, user_id, active):
        user = self.get(user_id)
        if not active and user["role"] == "admin" and user["active"] \
                and self.active_admin_count() <= 1:
            raise AccountError("You can't deactivate the last admin.")
        self.db.execute("UPDATE app_users SET active = ?, session_version = session_version + 1"
                        " WHERE id = ?", (1 if active else 0, user_id))

    def set_role(self, user_id, role):
        if role not in ROLES:
            raise AccountError("Unknown role.")
        user = self.get(user_id)
        if role != "admin" and user["role"] == "admin" and user["active"] \
                and self.active_admin_count() <= 1:
            raise AccountError("You can't remove the last admin.")
        self.db.execute("UPDATE app_users SET role = ? WHERE id = ?", (role, user_id))

    def ensure_admin(self, email, password, name=None, reset=False):
        """First admin from settings, or (with reset) restore admin access.

        Returns a short description of what happened, or None if nothing did.
        """
        if self.count() == 0:
            self.create(email, name, password, role="admin", must_change=True)
            return f"created the first admin account, {email.strip().lower()}"
        if not reset:
            return None
        # A reset applies once per email and password: if ADMIN_RESET stays set, a restart
        # must not undo the password chosen after the reset.
        token = f"{email.strip().lower()}\n{password}"
        applied = self.db.query("SELECT value FROM app_settings WHERE key = 'admin_reset_applied'")
        if applied and check_password_hash(applied[0]["value"], token):
            return None
        user = self.by_email(email)
        if user is None:
            self.create(email, name, password, role="admin", must_change=True)
            note = f"created admin account {email.strip().lower()}"
        else:
            self.db.execute("UPDATE app_users SET role = 'admin', active = 1 WHERE id = ?", (user["id"],))
            self.set_password(user["id"], password, must_change=True)
            note = f"restored admin access for {user['email']}"
        for key, value in (("admin_reset_applied", generate_password_hash(token)), ("admin_reset_applied_at", utc_now())):
            self.db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)"
                            " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        return note

    def reset_applied_at(self, email, password):
        """When a reset with exactly this email and password was applied (UTC, "" if it was before
        the time was kept); None if none has been. A reset applies once for each pair, so with the
        same ADMIN_PASSWORD again, ADMIN_RESET changes nothing."""
        try:
            rows = {r["key"]: r["value"] for r in self.db.query(
                "SELECT key, value FROM app_settings WHERE key IN ('admin_reset_applied', 'admin_reset_applied_at')")}
        except Exception:  # noqa: BLE001
            return None
        token = f"{(email or '').strip().lower()}\n{password}"
        if not rows.get("admin_reset_applied") or not check_password_hash(rows["admin_reset_applied"], token):
            return None
        return rows.get("admin_reset_applied_at") or ""


def app_secret(db):
    """The key that signs sign-in cookies: generated once, kept in the database."""
    rows = db.query("SELECT value FROM app_settings WHERE key = 'secret_key'")
    if rows:
        return rows[0]["value"]
    db.execute("INSERT INTO app_settings (key, value) VALUES ('secret_key', ?)"
               " ON CONFLICT (key) DO NOTHING", (secrets.token_hex(32),))
    return db.query("SELECT value FROM app_settings WHERE key = 'secret_key'")[0]["value"]
