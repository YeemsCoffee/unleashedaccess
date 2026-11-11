"""Walk every route of the app as an anonymous visitor and as a limited team member, and say
who can reach what. Prints JSON. Runs on the mock Unleashed server and a throwaway SQLite
file: it never touches a real database or the live Unleashed account.

    python tools/route_audit.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ADMIN = {"ADMIN_EMAIL": "audit-admin@example.test", "ADMIN_PASSWORD": "audit-first-pass",
         "ADMIN_NAME": "Audit admin"}
PUBLIC = {"dash.login", "dash.logout", "dash.healthz", "static"}
# Endpoints expected to answer anonymous visitors without a sign-in.
SAMPLE = {"int": "1", "path": "x", "string": "x", "uuid": "00000000-0000-0000-0000-000000000000"}


def _url(rule):
    url = rule.rule
    for arg in rule.arguments:
        kind = type(rule._converters[arg]).__name__.lower().replace("converter", "")
        url = re.sub(rf"<(?:[^:<>]+:)?{arg}>", SAMPLE.get(kind, "1"), url)
    return url


def _state(res):
    location = res.headers.get("Location", "")
    if res.status_code in (301, 302, 303, 307, 308):
        return "login" if "/login" in location else f"redirect {location}"
    return str(res.status_code)


def main():
    from app import create_app
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app({"TESTING": True, "SYNC_ON_STARTUP": "blocking", "SYNC_INTERVAL_MINUTES": 0,
                      "DATABASE_PATH": path, "DATABASE_URL": "", "UNLEASHED_MODE": "mock", **ADMIN})
    rules = [r for r in app.url_map.iter_rules() if r.endpoint != "static"]
    report = {"routes": len(rules), "anonymous_reachable": [], "anonymous_posts_accepted": [],
              "limited_user_reached": [], "post_without_token_by_admin": [], "headers_missing": [], "limited_user_tested": False}

    anon = app.test_client()
    for rule in rules:
        if rule.endpoint in PUBLIC:
            continue
        url = _url(rule)
        for method in sorted(rule.methods & {"GET", "POST"}):
            res = anon.open(url, method=method, data={} if method == "POST" else None)
            state = _state(res)
            if state != "login":
                report["anonymous_reachable" if method == "GET" else "anonymous_posts_accepted"].append(
                    {"endpoint": rule.endpoint, "method": method, "url": url, "result": state})

    # A person who can only report maintenance problems should reach nothing else.
    admin = app.test_client()
    admin.post("/login", data={"email": ADMIN["ADMIN_EMAIL"], "password": ADMIN["ADMIN_PASSWORD"]})
    admin.post("/account", data={"current_password": ADMIN["ADMIN_PASSWORD"], "new_password": "audit-own-password",
                                 "confirm_password": "audit-own-password"})
    limited_id = app.extensions["profiles"].save(None, "Audit requests only", ["maint_requests"])
    limited = {"id": limited_id, "name": "Audit requests only"}
    if limited:
        html = admin.post("/team/add", data={"name": "Limited", "email": "limited@example.test",
                                              "access": str(limited["id"])}).get_data(as_text=True)
        found = re.search(r'class="temp-password mono">([^<]+)<', html)
        if found:
            low = app.test_client()
            report["limited_user_tested"] = True
            low.post("/login", data={"email": "limited@example.test", "password": found.group(1)})
            low.post("/account", data={"current_password": found.group(1), "new_password": "audit-low-password",
                                       "confirm_password": "audit-low-password"})
            for rule in rules:
                if rule.endpoint in PUBLIC or "GET" not in rule.methods:
                    continue
                res = low.get(_url(rule))
                if res.status_code == 200 and not rule.endpoint.startswith(("maint.request", "maint.attachment")) \
                        and rule.endpoint not in ("dash.account", "dash.no_access", "dash.overview"):
                    report["limited_user_reached"].append({"endpoint": rule.endpoint, "url": _url(rule),
                                                           "profile": limited["name"]})

    # CSRF: does a state-changing POST work with nothing but the session cookie (as another site's form would)?
    for rule in rules:
        if "POST" in rule.methods and rule.endpoint in ("dash.profiles_save", "dash.team_add"):
            res = admin.post(_url(rule), data={"name": "csrf-probe", "email": "csrf@example.test", "access": "admin"},
                             headers={"Origin": "https://evil.example", "Referer": "https://evil.example/"})
            if res.status_code < 400:
                report["post_without_token_by_admin"].append(
                    {"endpoint": rule.endpoint, "status": res.status_code,
                     "note": "accepted a cross-origin POST: no CSRF token or Origin check"})

    page = admin.get("/")
    for header in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options",
                   "Referrer-Policy", "Strict-Transport-Security"):
        if header not in page.headers:
            report["headers_missing"].append(header)
    # The attributes of the sign-in cookie, never its value.
    report["cookie_attributes"] = [h.split(";", 1)[1].strip() if ";" in h else "" for h in page.headers.getlist("Set-Cookie")]
    report["config"] = {k: bool(app.config.get(k)) for k in ("SESSION_COOKIE_SECURE", "SESSION_COOKIE_HTTPONLY")}
    report["config"]["SESSION_COOKIE_SAMESITE"] = app.config.get("SESSION_COOKIE_SAMESITE")
    app.extensions["db"].close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
