"""Access profiles: which tabs each kind of account opens.

Admins open everything, Settings included. Every other account has a profile, given
by an admin on the Team page, naming the tabs it opens: Finance, Warehouse,
Production, Maintenance. Admins create and change profiles under Settings → Access
profiles. Without team accounts (one shared password, or none) everyone opens
everything.
"""
import json

from accounts import AccountError
from unleashed.store import utc_now

AREAS = (("finance", "Finance"), ("warehouse", "Warehouse"), ("production", "Production"),
         ("maintenance", "Maintenance"))
AREA_KEYS = tuple(key for key, _ in AREAS)
# Tabs that show data from Unleashed (and its sync status).
UNLEASHED_AREAS = ("finance", "warehouse", "production")
# Made the first time this runs, so the people already on the team keep the tabs they had.
STANDARD = "Standard"
STARTER_PROFILES = ((STANDARD, UNLEASHED_AREAS), ("Maintenance technician", ("maintenance",)))


class Profiles:
    def __init__(self, db):
        self.db = db

    def all(self):
        """Every profile, with "people": how many accounts have it."""
        rows = self.db.query(
            "SELECT p.*, (SELECT COUNT(*) FROM app_user_profiles u WHERE u.profile_id = p.id) AS people"
            " FROM access_profiles p ORDER BY lower(p.name)")
        return [_profile(r) for r in rows]

    def get(self, profile_id):
        rows = self.db.query("SELECT * FROM access_profiles WHERE id = ?", (profile_id,))
        return _profile(rows[0]) if rows else None

    def save(self, profile_id, name, areas):
        """Create a profile (`profile_id` None) or change one. Returns its id."""
        name = " ".join((name or "").split())
        if not name:
            raise AccountError("Give the profile a name.")
        if len(name) > 60:
            raise AccountError("Profile names can be 60 characters at most.")
        same = [p for p in self.all() if p["name"].lower() == name.lower() and p["id"] != profile_id]
        if same:
            raise AccountError(f"There's already a profile called {same[0]['name']}.")
        areas = [a for a in AREA_KEYS if a in set(areas or ())]
        if profile_id is None:
            self.db.execute("INSERT INTO access_profiles (name, areas, created_at) VALUES (?, ?, ?)",
                            (name, json.dumps(areas), utc_now()))
            return self.db.query("SELECT id FROM access_profiles WHERE name = ?", (name,))[0]["id"]
        if self.get(profile_id) is None:
            raise AccountError("That profile doesn't exist any more.")
        self.db.execute("UPDATE access_profiles SET name = ?, areas = ? WHERE id = ?",
                        (name, json.dumps(areas), profile_id))
        return profile_id

    def delete(self, profile_id):
        profile = next((p for p in self.all() if p["id"] == profile_id), None)
        if profile is None:
            return
        if profile["people"]:
            raise AccountError(f"{profile['name']} is given to {profile['people']} "
                               f"{'person' if profile['people'] == 1 else 'people'}. Give them "
                               "another profile first.")
        self.db.execute("DELETE FROM access_profiles WHERE id = ?", (profile_id,))

    # ------------------------------------------------------------ people

    def of_users(self):
        """{user id: profile} for everyone who has one."""
        profiles = {p["id"]: p for p in self.all()}
        return {r["user_id"]: profiles[r["profile_id"]]
                for r in self.db.query("SELECT user_id, profile_id FROM app_user_profiles")
                if r["profile_id"] in profiles}

    def of_user(self, user_id):
        rows = self.db.query(
            "SELECT p.* FROM access_profiles p JOIN app_user_profiles u ON u.profile_id = p.id"
            " WHERE u.user_id = ?", (user_id,))
        return _profile(rows[0]) if rows else None

    def assign(self, user_id, profile_id):
        if self.get(profile_id) is None:
            raise AccountError("Choose a profile.")
        self.db.execute(
            "INSERT INTO app_user_profiles (user_id, profile_id) VALUES (?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET profile_id = excluded.profile_id",
            (user_id, profile_id))

    def areas_of(self, user):
        """The tabs `user` (an app_users row) opens."""
        if user["role"] == "admin":
            return set(AREA_KEYS)
        profile = self.of_user(user["id"])
        return set(profile["areas"]) if profile else set()

    def seed(self):
        """The first time: the starter profiles, and Standard (what every account could see
        before profiles) for the members already on the team."""
        if self.db.query("SELECT COUNT(*) AS n FROM access_profiles")[0]["n"]:
            return
        for name, areas in STARTER_PROFILES:
            self.db.execute("INSERT INTO access_profiles (name, areas, created_at) VALUES (?, ?, ?)"
                            " ON CONFLICT (name) DO NOTHING", (name, json.dumps(list(areas)), utc_now()))
        standard = self.db.query("SELECT id FROM access_profiles WHERE name = ?", (STANDARD,))[0]["id"]
        self.db.execute("INSERT INTO app_user_profiles (user_id, profile_id)"
                        " SELECT id, ? FROM app_users WHERE role <> 'admin'"
                        " ON CONFLICT (user_id) DO NOTHING", (standard,))


def _profile(row):
    areas = json.loads(row["areas"])
    return {"id": row["id"], "name": row["name"], "areas": [a for a in AREA_KEYS if a in areas],
            "people": row.get("people", 0)}
