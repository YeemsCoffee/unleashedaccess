"""Unleashed REST API client.

Speaks the Unleashed protocol: every request carries `api-auth-id` and an
`api-auth-signature` (base64 HMAC-SHA256 of the query string, keyed with the
API key); list endpoints are paged as /{Endpoint}/{pageNumber}?pageSize=N and
answer {"Pagination": {...}, "Items": [...]}; dates come back as
"/Date(<epoch ms>)/".

The same client talks to the real API (HttpTransport) or to the bundled mock
server in-process (WsgiTransport) — the dashboard never knows which.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://api.unleashedsoftware.com"
_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")


class UnleashedError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def sign(query_string, api_key):
    digest = hmac.new(api_key.encode(), query_string.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def parse_date(value):
    """'/Date(1690848000000)/' -> datetime.date (UTC). Passes through None."""
    if not value:
        return None
    m = _DATE_RE.match(value)
    if not m:
        return dt.date.fromisoformat(value[:10])
    return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc).date()


def format_date(d):
    return d.isoformat() if d else None


class HttpTransport:
    def __init__(self, base_url, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path, query_string, headers):
        url = f"{self.base_url}{path}" + (f"?{query_string}" if query_string else "")
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise UnleashedError(f"Could not reach Unleashed at {self.base_url}: {e.reason}")


class WsgiTransport:
    """Calls a WSGI app (the mock server) in-process, through the full HTTP stack."""

    def __init__(self, wsgi_app):
        self.client = wsgi_app.test_client()

    def get(self, path, query_string, headers):
        resp = self.client.get(path, query_string=query_string, headers=headers)
        return resp.status_code, resp.get_data(as_text=True)


class UnleashedClient:
    def __init__(self, api_id, api_key, transport, log_fn=None, cache_seconds=120,
                 client_type="yeems/gardena-dashboard", mode="live"):
        self.api_id = api_id
        self.api_key = api_key
        self.transport = transport
        self.log_fn = log_fn
        self.cache_seconds = cache_seconds
        self.client_type = client_type
        self.mode = mode
        self._cache = {}

    def get(self, endpoint, params=None, page=None):
        """One GET. `page` appends /{page} for paged list endpoints."""
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        query_string = urllib.parse.urlencode(params)
        path = f"/{endpoint}" + (f"/{page}" if page is not None else "")
        key = (path, query_string)

        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        if not self.api_id or not self.api_key:
            raise UnleashedError("Unleashed API ID/key are not configured "
                                 "(set UNLEASHED_API_ID and UNLEASHED_API_KEY).")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "api-auth-id": self.api_id,
            "api-auth-signature": sign(query_string, self.api_key),
            "client-type": self.client_type,
        }
        started = time.monotonic()
        status, body, error = None, None, None
        try:
            status, body = self.transport.get(path, query_string, headers)
            if status != 200:
                error = _error_message(body) or f"HTTP {status}"
                raise UnleashedError(f"Unleashed {path} failed: {error}", status)
            data = json.loads(body)
        except UnleashedError as e:
            error = error or str(e)
            raise
        except ValueError:
            error = "Response was not valid JSON"
            raise UnleashedError(f"Unleashed {path}: {error}", status)
        finally:
            self._log(path, query_string, status, started, body if error is None else None, error)

        self._cache[key] = (time.monotonic(), data)
        return data

    def get_all(self, endpoint, params=None, page_size=500, max_pages=100):
        """Follow Pagination across pages and return the combined Items list."""
        items, page = [], 1
        while page <= max_pages:
            data = self.get(endpoint, {**(params or {}), "pageSize": page_size}, page=page)
            items.extend(data.get("Items", []))
            pages = data.get("Pagination", {}).get("NumberOfPages", 1)
            if page >= pages:
                break
            page += 1
        return items

    def clear_cache(self):
        self._cache.clear()

    def _log(self, path, query_string, status, started, body, error):
        if not self.log_fn:
            return
        item_count = None
        if body:
            try:
                item_count = len(json.loads(body).get("Items", []))
            except (ValueError, AttributeError):
                pass
        self.log_fn({
            "method": "GET",
            "endpoint": path,
            "query": query_string,
            "status_code": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "item_count": item_count,
            "error": error,
            "mode": self.mode,
        })


def _error_message(body):
    try:
        data = json.loads(body)
        return data.get("description") or data.get("Description") or data.get("message")
    except (ValueError, TypeError, AttributeError):
        return (body or "")[:200] or None
