"""A small MCP server of read-only security checks for this app (stdio).

Tools: scan_secrets, route_access_audit, audit_dependencies, static_scan, git_history_secrets.
Nothing here writes to the repository, the database or the live Unleashed account; the route
audit runs the app on the mock Unleashed server and a throwaway SQLite file.

Run by Claude Code through the project's .mcp.json. It speaks MCP itself (JSON-RPC, one message
a line on stdin/stdout), so it needs nothing installed beyond the app's own requirements.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The Python that has the app's requirements (Flask...). Set PROJECT_PYTHON to use a virtualenv.
PROJECT_PYTHON = os.environ.get("PROJECT_PYTHON") or sys.executable

INSTRUCTIONS = ("Read-only security checks for the Core (Unleashed Access) Flask app. "
                "Findings never include secret values: only file, line and kind.")
TOOLS = {}


class server:  # the checks register themselves with @server.tool()
    @staticmethod
    def tool():
        def register(fn):
            TOOLS[fn.__name__] = fn
            return fn
        return register


SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "database URL with password": re.compile(r"\b(?:postgres(?:ql)?|mysql)://[^\s:/@]+:[^\s@]{3,}@"),
    "assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|password|passwd|token)\b\s*[:=]\s*[\"'][A-Za-z0-9/+_\-]{16,}[\"']"),
}
PLACEHOLDER = re.compile(r"(?i)example|changeme|placeholder|your[-_ ]|xxxx|test|mock|dummy|sample|<.*>|://user:password@")


def _run(args, timeout=240, cwd=ROOT):
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(args[:3])} took longer than {timeout}s"
    return done, None


def _tracked_files():
    done, err = _run(["git", "ls-files"])
    if err or done.returncode:
        return []
    return [f for f in done.stdout.splitlines() if not f.endswith((".png", ".jpg", ".ico", ".woff", ".woff2", ".zip"))]


@server.tool()
def scan_secrets() -> str:
    """Look through every git-tracked file for credentials (keys, tokens, passwords, database URLs
    with passwords) and check that .env is neither tracked nor in the index. Reports file and
    line only, never the value."""
    findings = []
    for path in _tracked_files():
        try:
            with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as fh:
                for number, line in enumerate(fh, 1):
                    for kind, pattern in SECRET_PATTERNS.items():
                        match = pattern.search(line)
                        if match and not PLACEHOLDER.search(line):
                            findings.append({"file": path, "line": number, "kind": kind,
                                             "in_tests": path.startswith("tests/")})
        except OSError:
            continue
    tracked_env = [f for f in _tracked_files() if os.path.basename(f) in (".env", ".env.local", ".env.production")]
    return json.dumps({"findings": findings, "tracked_env_files": tracked_env,
                       "note": "Values are never shown. A finding outside tests/ should be treated as exposed: "
                               "rotate it, then remove it. One in tests/ is usually a made-up fixture: check."},
                      indent=2)


@server.tool()
def git_history_secrets() -> str:
    """Check git history for .env files ever committed and for credential-looking strings added in
    any commit (file and commit only, never the value)."""
    out = {}
    done, err = _run(["git", "log", "--all", "--oneline", "--diff-filter=A", "--", ".env", "*.env", "*.pem", "*.key"])
    out["env_or_key_files_ever_added"] = err or done.stdout.strip().splitlines()
    hits = []
    for kind, pattern in SECRET_PATTERNS.items():
        if kind == "assigned secret":
            continue
        done, err = _run(["git", "log", "--all", "--oneline", "-G", pattern.pattern, "--"])
        if not err and done.stdout.strip():
            hits.append({"kind": kind, "commits": done.stdout.strip().splitlines()[:10]})
    out["credential_patterns_in_history"] = hits
    return json.dumps(out, indent=2)


@server.tool()
def route_access_audit() -> str:
    """Start the app on the mock server and walk every route: as an anonymous visitor (should all go
    to sign-in), as a team member who can only report maintenance problems (should reach nothing
    else), whether a cross-origin POST is accepted (CSRF), and which security headers are missing."""
    done, err = _run([PROJECT_PYTHON, os.path.join(ROOT, "tools", "route_audit.py")], timeout=300)
    if err:
        return err
    return done.stdout if done.returncode == 0 else f"The audit failed:\n{done.stderr[-3000:]}"


@server.tool()
def audit_dependencies() -> str:
    """Known vulnerabilities in requirements.txt, using pip-audit (pip install pip-audit)."""
    done, err = _run([PROJECT_PYTHON, "-m", "pip_audit", "-r", "requirements.txt", "--progress-spinner", "off"], timeout=300)
    if err or "No module named" in (done.stderr if done else ""):
        return "pip-audit isn't installed for " + PROJECT_PYTHON + ": run `pip install pip-audit`."
    return (done.stdout + done.stderr).strip() or "No known vulnerabilities."


@server.tool()
def static_scan() -> str:
    """Python static analysis with Bandit (pip install bandit), tests left out, medium severity and up.
    Findings are file, line, test id and message."""
    done, err = _run([PROJECT_PYTHON, "-m", "bandit", "-r", ".", "-x", "./tests,./.venv,./venv", "-ll", "-f", "json", "-q"],
                     timeout=300)
    if err or (done and "No module named" in done.stderr):
        return "Bandit isn't installed for " + PROJECT_PYTHON + ": run `pip install bandit`."
    try:
        results = json.loads(done.stdout).get("results", [])
    except ValueError:
        return done.stdout[-3000:] or done.stderr[-3000:]
    return json.dumps([{"file": r["filename"], "line": r["line_number"], "test": r["test_id"],
                        "severity": r["issue_severity"], "message": r["issue_text"]} for r in results], indent=2)


def _answer(message):
    """The reply to one JSON-RPC message, or None for a notification."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                  "serverInfo": {"name": "core-security", "version": "1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "description": " ".join((fn.__doc__ or "").split()),
                             "inputSchema": {"type": "object", "properties": {}},
                             "annotations": {"readOnlyHint": True}} for name, fn in TOOLS.items()]}
    elif method == "tools/call":
        fn = TOOLS.get(params.get("name"))
        if fn is None:
            return {"jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32602, "message": f"No tool named {params.get('name')}"}}
        try:
            result = {"content": [{"type": "text", "text": fn()}]}
        except Exception as exc:  # a failed check is reported, the server keeps running
            result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = _answer(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
