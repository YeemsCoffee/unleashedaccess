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
# Rate-limited / temporarily unavailable responses are retried after these waits.
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_WAITS = (1, 3, 8, None)
# Reference lists Unleashed returns whole, without /{pageNumber} paging.
UNPAGED_ENDPOINTS = {"Warehouses", "ProductGroups", "Currencies", "UnitOfMeasures"}
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


def writable(value):
    """`value` (a document as Unleashed returned it) with every "/Date(ms)/" in the
    "2026-07-15T00:00:00" form: Unleashed rejects its own date format in a request body
    ("... is not a valid value for DateTime")."""
    if isinstance(value, dict):
        return {k: writable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [writable(v) for v in value]
    m = _DATE_RE.fullmatch(value) if isinstance(value, str) else None
    if m:
        moment = dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S")
    return value


class HttpTransport:
    def __init__(self, base_url, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path, query_string, headers):
        return self.send("GET", path, query_string, headers)

    def send(self, method, path, query_string, headers, body=None):
        url = f"{self.base_url}{path}" + (f"?{query_string}" if query_string else "")
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
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
        self.app = wsgi_app

    def get(self, path, query_string, headers):
        resp = self.app.test_client().get(path, query_string=query_string, headers=headers)
        return resp.status_code, resp.get_data(as_text=True)

    def send(self, method, path, query_string, headers, body=None):
        if method == "GET":
            return self.get(path, query_string, headers)
        resp = self.app.test_client().open(path, method=method, query_string=query_string,
                                           headers=headers, data=body)
        return resp.status_code, resp.get_data(as_text=True)


class UnleashedClient:
    def __init__(self, api_id, api_key, transport, log_fn=None, cache_seconds=120,
                 history_cache_seconds=3600, client_type="yeems/unleashedaccess", mode="live"):
        self.api_id = api_id
        self.api_key = api_key
        self.transport = transport
        self.log_fn = log_fn
        self.cache_seconds = cache_seconds
        self.history_cache_seconds = max(history_cache_seconds, cache_seconds)
        self.client_type = client_type
        self.mode = mode
        self._cache = {}

    def get(self, endpoint, params=None, page=None, long_cache=False):
        """One GET. `page` appends /{page} for paged list endpoints; `long_cache`
        keeps the response for the history TTL (records that no longer change)."""
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        # Commas stay literal (orderStatus=Parked,Placed) so the signed string
        # is byte-for-byte what the server reads.
        query_string = urllib.parse.urlencode(params, safe=",")
        path = f"/{endpoint}" + (f"/{page}" if page is not None else "")
        key = (path, query_string)

        long_cache = long_cache or _is_historical(params)
        ttl = self.history_cache_seconds if long_cache else self.cache_seconds
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
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
            for wait in RETRY_WAITS:
                status, body = self.transport.get(path, query_string, headers)
                if status not in RETRY_STATUSES or wait is None:
                    break
                time.sleep(_retry_after(body, wait))
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

    def post(self, endpoint, body=None):
        return self._write("POST", endpoint, body)

    def put(self, endpoint, body):
        return self._write("PUT", endpoint, body)

    def _write(self, method, endpoint, body):
        """One POST/PUT with a JSON body. Unleashed signs the query string, empty here.
        Only a rate limit is retried: anything else may already have taken effect."""
        if not self.api_id or not self.api_key:
            raise UnleashedError("Unleashed API ID/key are not configured "
                                 "(set UNLEASHED_API_ID and UNLEASHED_API_KEY).")
        path = f"/{endpoint}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "api-auth-id": self.api_id,
            "api-auth-signature": sign("", self.api_key),
            "client-type": self.client_type,
        }
        started = time.monotonic()
        status, text, error = None, None, None
        try:
            for wait in RETRY_WAITS:
                status, text = self.transport.send(method, path, "", headers, json.dumps(body or {}))
                if status != 429 or wait is None:
                    break
                time.sleep(_retry_after(text, wait))
            if status not in (200, 201, 204):
                error = _error_message(text) or f"HTTP {status}"
                raise UnleashedError(f"Unleashed {method} {path} failed: {error}", status)
            data = json.loads(text) if text and text.strip() else {}
        except UnleashedError as e:
            error = error or str(e)
            raise
        except ValueError:
            data = {}  # accepted, but the reply isn't JSON
        finally:
            self._log(path, "", status, started, None, error, method=method)
            self._cache.clear()  # whatever changed may be in cached reads
        return data

    def get_all(self, endpoint, params=None, page_size=500, max_pages=200):
        """Follow Pagination across pages and return the combined Items list."""
        if endpoint in UNPAGED_ENDPOINTS:
            return _items(self.get(endpoint, params))
        items, page = [], 1
        while True:
            data = self.get(endpoint, {**(params or {}), "pageSize": page_size}, page=page)
            items.extend(_items(data))
            pages = (data.get("Pagination") or {}).get("NumberOfPages", 1) if isinstance(data, dict) else 1
            if page >= pages:
                break
            if page >= max_pages:
                raise UnleashedError(f"{endpoint}: more than {max_pages * page_size} records; "
                                     "narrow the date range")
            page += 1
        return items

    def clear_cache(self):
        self._cache.clear()

    def _log(self, path, query_string, status, started, body, error, method="GET"):
        if not self.log_fn:
            return
        item_count = None
        if body:
            try:
                item_count = len(json.loads(body).get("Items", []))
            except (ValueError, AttributeError):
                pass
        self.log_fn({
            "method": method,
            "endpoint": path,
            "query": query_string,
            "status_code": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "item_count": item_count,
            "error": error,
            "mode": self.mode,
        })


def _retry_after(body, default):
    try:
        return min(float(json.loads(body).get("RetryAfter", default)), 30)
    except (ValueError, TypeError, AttributeError):
        return default


def _items(data):
    if isinstance(data, list):
        return data
    return data.get("Items") or []


def _is_historical(params):
    """Queries pinned to a past date (as-at or end date) change rarely."""
    today = dt.date.today().isoformat()
    pinned = [str(params[k])[:10] for k in ("asAtDate", "endDate") if params.get(k)]
    return bool(pinned) and all(p < today for p in pinned)


def _error_message(body):
    try:
        data = json.loads(body)
        return data.get("description") or data.get("Description") or data.get("message")
    except (ValueError, TypeError, AttributeError):
        return (body or "")[:200] or None
