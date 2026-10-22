"""Tests for the Unleashed client, mock server, reports and dashboard."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unleashed.client import UnleashedClient, UnleashedError, WsgiTransport, parse_date, sign  # noqa: E402
from unleashed.mock_server import MOCK_API_ID, MOCK_API_KEY, create_mock_app  # noqa: E402
from unleashed.reports import Reports, add_months  # noqa: E402

TODAY = dt.date(2026, 7, 15)
# Tests finish the first sync before serving and never start the background thread.
SYNC_NOW = {"SYNC_ON_STARTUP": "blocking", "SYNC_INTERVAL_MINUTES": 0}
# Set TEST_DATABASE_URL=postgresql://... to run the app tests against PostgreSQL.
# Every table the app owns in that database is dropped before each test.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
APP_TABLES = ("unleashed_api_log", "ul_records", "ul_documents", "ul_stock_snapshots",
              "ul_sync_runs", "ul_sync_state", "ul_sync_lock")


def sqlite_database(testcase):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    testcase.addCleanup(lambda: [os.path.exists(path + x) and os.unlink(path + x)
                                 for x in ("", "-wal", "-shm")])
    # DATABASE_URL blanked so a real shared database in the environment is never touched.
    return {"DATABASE_PATH": path, "DATABASE_URL": ""}


def fresh_database(testcase):
    """Config for an empty database: a temp SQLite file, or the wiped TEST_DATABASE_URL."""
    if not TEST_DATABASE_URL:
        return sqlite_database(testcase)
    import psycopg
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        for table in APP_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    return {"DATABASE_URL": TEST_DATABASE_URL}


def make_app(testcase, database=None, **config):
    """create_app on `database` (default: a fresh one); its connection pool closes after the test."""
    from app import create_app
    app = create_app({"TESTING": True, **SYNC_NOW, **(database or fresh_database(testcase)),
                      **config})
    testcase.addCleanup(app.extensions["db"].close)
    return app


class ReportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = create_mock_app(today=TODAY)
        cls.client = UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(cls.mock), mode="mock")
        cls.reports = Reports(cls.client, today_fn=lambda: TODAY)

    def test_signature_is_required(self):
        bad = UnleashedClient(MOCK_API_ID, "wrong-key", WsgiTransport(self.mock))
        with self.assertRaises(UnleashedError) as ctx:
            bad.get("Warehouses")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_signature_matches_known_hmac(self):
        # printf 'pageSize=200' | openssl dgst -sha256 -hmac key -binary | base64
        self.assertEqual(sign("pageSize=200", "key"),
                         "8Dx5rMk44oq5jjkntTrEPm6KKUfn6AN8JBe/sgtsvLQ=")

    def test_paging_combines_all_pages(self):
        everything = self.client.get_all("SalesOrders", page_size=1000)
        paged = self.client.get_all("SalesOrders", page_size=37)
        self.assertGreater(len(everything), 37)
        self.assertEqual([o["OrderNumber"] for o in everything], [o["OrderNumber"] for o in paged])

    def test_parse_date(self):
        self.assertEqual(parse_date("/Date(1784073600000)/"), dt.date(2026, 7, 15))
        self.assertIsNone(parse_date(None))

    def test_cogs_formula_matches_cost_of_sales(self):
        for month in (TODAY, add_months(TODAY.replace(day=1), -1), add_months(TODAY.replace(day=1), -5)):
            c = self.reports.cogs(month)
            self.assertAlmostEqual(c["cogs"], c["opening"] + c["purchases"] - c["closing"], places=6)
            recon = self.reports.cogs_reconciliation(month)
            self.assertAlmostEqual(c["cogs"], recon["total"], delta=1.0, msg=str(month))
            self.assertGreater(c["shipping"], 0)

    def test_current_month_runs_through_today(self):
        c = self.reports.cogs(TODAY, daily=True)
        self.assertTrue(c["is_current"])
        self.assertEqual(c["through"], TODAY)
        self.assertEqual(len(c["daily"]), TODAY.day)
        self.assertAlmostEqual(c["daily"][-1]["cogs_to_date"], c["cogs"], places=6)

    def test_months_chain(self):
        history = self.reports.cogs_history(4)
        for prev, cur in zip(history, history[1:]):
            self.assertAlmostEqual(prev["closing"], cur["opening"], places=4)

    def test_stock_movement_reconciles_to_stock_on_hand(self):
        mv = self.reports.stock_movement("MILK-WHL-GAL", dt.date(2026, 5, 1), TODAY)
        self.assertEqual(mv["closing_qty"], mv["reported_qty"])
        self.assertAlmostEqual(mv["closing_value"], mv["reported_value"], delta=0.5)

    def test_open_documents_do_not_move_stock(self):
        pos = self.reports.purchase_orders(TODAY, TODAY, status="Parked")
        self.assertEqual(len(pos), 1)
        received = self.reports.received_purchases(TODAY.replace(day=1), TODAY)
        self.assertNotIn(pos[0]["number"], [o["number"] for o in received])


class DashboardPageTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock")
        self.client = self.app.test_client()

    def test_pages_render(self):
        for path in ("/", "/cogs", "/stock-on-hand",
                     "/stock-valuation", "/stock-movement",
                     "/purchases", "/purchases/PO-00000001",
                     "/sales", "/sales/SO-00000001",
                     "/adjustments", "/transactions",
                     "/api-log", "/connection"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertNotIn("Couldn't load data", res.get_data(as_text=True), path)

    def test_csv_export(self):
        res = self.client.get("/stock-on-hand?format=csv")
        self.assertEqual(res.mimetype, "text/csv")
        self.assertTrue(res.get_data(as_text=True).startswith("Product Code,"))

    def test_api_calls_are_logged(self):
        self.client.get("/stock-on-hand")
        html = self.client.get("/api-log").get_data(as_text=True)
        self.assertIn("/StockOnHand/1", html)


class LiveModeTests(unittest.TestCase):
    """Runs the app in live mode, with the live client's transport pointed at the mock."""

    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="live", UNLEASHED_API_ID=MOCK_API_ID,
                            UNLEASHED_API_KEY=MOCK_API_KEY,
                            UNLEASHED_TRANSPORT=WsgiTransport(create_mock_app()))
        self.client = self.app.test_client()

    def test_pages_render_in_live_mode(self):
        for path in ("/", "/cogs", "/stock-on-hand", "/purchases", "/sales", "/transactions"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("Live Unleashed API", html, path)
            self.assertNotIn("Couldn't load data", html, path)

    def test_live_data_check_passes_on_mock_data(self):
        html = self.client.post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("Live data check", html)
        self.assertNotIn("! Check", html)
        self.assertNotIn("Failed", html)
        self.assertIn("Purchase order cost line", html)
        self.assertIn("CostLines.Amount", html)
        self.assertIn("Spot-check", html)
        self.assertIn("Deleted: 1 line(s), 1 without", html)
        self.assertIn("Every line without changes is on a deleted or parked adjustment", html)

    def test_wrong_credentials_fail_clearly(self):
        self.app.extensions["unleashed"]["live_client"].api_key = "wrong"
        html = self.client.post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("Failed", html)
        self.assertIn("Forbidden", html)

    def test_unpaged_endpoints_are_called_without_page_number(self):
        self.client.post("/connection", data={"action": "test"})
        html = self.client.get("/api-log").get_data(as_text=True)
        self.assertIn("GET /Warehouses<", html.replace("</td>", "<"))
        self.assertNotIn("/Warehouses/1", html)


class RewritingTransport(WsgiTransport):
    """Mock transport that edits JSON responses, to imitate a real account's quirks."""

    def __init__(self, app, rewrite):
        super().__init__(app)
        self.rewrite = rewrite

    def get(self, path, query_string, headers):
        import json
        status, body = super().get(path, query_string, headers)
        if status == 200:
            body = json.dumps(self.rewrite(path, json.loads(body)))
        return status, body


def _strip_cost_lines_from_lists(path, data):
    import re
    if re.fullmatch(r"/PurchaseOrders/\d+", path):
        for po in data["Items"]:
            po.pop("CostLines", None)
    return data


def _strip_adjustment_changes(path, data):
    if path.startswith("/StockAdjustments"):
        for adj in data["Items"]:
            for line in adj["StockAdjustmentLines"]:
                line.pop("QuantityChange", None)
                line.pop("ValueChange", None)
    return data


class AccountQuirkTests(unittest.TestCase):
    def _app(self, rewrite):
        return make_app(self, UNLEASHED_MODE="live", UNLEASHED_API_ID=MOCK_API_ID,
                        UNLEASHED_API_KEY=MOCK_API_KEY, TODAY=TODAY,
                        UNLEASHED_TRANSPORT=RewritingTransport(create_mock_app(today=TODAY), rewrite))

    def test_cost_lines_missing_from_list_are_fetched_from_detail(self):
        app = self._app(_strip_cost_lines_from_lists)
        reports = app.extensions["unleashed"]["reports"]
        stripped = reports.cogs(TODAY)
        full = ReportsTests.reports.cogs(TODAY)
        self.assertGreater(stripped["shipping"], 0)
        self.assertAlmostEqual(stripped["shipping"], full["shipping"], places=6)
        self.assertAlmostEqual(stripped["cogs"], full["cogs"], places=6)
        html = app.test_client().post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("each purchase order&#39;s detail", html)

    def test_adjustments_without_change_fields(self):
        app = self._app(_strip_adjustment_changes)
        c = app.test_client()
        start = "?start=2025-07-01&end=2026-07-15"
        for path in ("/adjustments" + start, "/transactions" + start,
                     "/stock-movement?product=MILK-WHL-GAL&start=2026-01-01&end=2026-07-15",
                     "/adjustments?format=csv", "/transactions?format=csv", "/cogs"):
            res = c.get(path)
            self.assertEqual(res.status_code, 200, path)
        html = c.get("/adjustments" + start).get_data(as_text=True)
        self.assertIn("without a quantity or value change", html)
        self.assertNotIn("Entered in error", html)  # the deleted adjustment stays out
        check = c.post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("By adjustment status", check)
        self.assertIn("Completed:", check)
        self.assertIn("on active adjustments have no change fields", check)


class StoreSyncTests(unittest.TestCase):
    """The local copy: equivalence with the API, incremental updates, failure handling."""

    def setUp(self):
        self.database = fresh_database(self)  # every app in a test shares this one
        self.mock = create_mock_app(today=TODAY)
        self.ds = self.mock.config["DATASET"]

    def _app(self, **config):
        settings = {"UNLEASHED_MODE": "live", "UNLEASHED_API_ID": MOCK_API_ID,
                    "UNLEASHED_API_KEY": MOCK_API_KEY, "TODAY": TODAY,
                    "UNLEASHED_TRANSPORT": WsgiTransport(self.mock), **config}
        app = make_app(self, database=self.database, **settings)
        self.app_db = app.extensions["db"]
        return app

    def _api_log(self, like="%"):
        """API log rows (endpoint, query) whose "endpoint?query" matches a LIKE-style pattern."""
        import fnmatch
        rows = self.app_db.query("SELECT endpoint, query FROM unleashed_api_log ORDER BY id")
        pattern = like.replace("%", "*")
        return [(r["endpoint"], r["query"]) for r in rows
                if fnmatch.fnmatchcase(f"{r['endpoint']}?{r['query']}", pattern)]

    def test_reports_from_store_match_reports_from_api(self):
        app = self._app()
        stored = app.extensions["unleashed"]["reports"]
        direct = Reports(UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(self.mock)),
                         today_fn=lambda: TODAY)
        month = TODAY.replace(day=1)
        for m in (month, add_months(month, -1), add_months(month, -6)):
            a, b = stored.cogs(m, daily=True), direct.cogs(m, daily=True)
            for k in ("opening", "goods", "shipping", "closing", "cogs"):
                self.assertAlmostEqual(a[k], b[k], places=6, msg=f"{m} {k}")
            self.assertEqual([round(r["cogs_to_date"], 6) for r in a["daily"]],
                             [round(r["cogs_to_date"], 6) for r in b["daily"]])
        start = add_months(month, -2)
        self.assertEqual(stored.purchase_enquiry(start, TODAY)["total"],
                         direct.purchase_enquiry(start, TODAY)["total"])
        self.assertEqual(stored.sales_enquiry(start, TODAY)["revenue"],
                         direct.sales_enquiry(start, TODAY)["revenue"])
        self.assertEqual(len(stored.transactions(start, TODAY)), len(direct.transactions(start, TODAY)))
        mv_a = stored.stock_movement("MILK-WHL-GAL", start, TODAY)
        mv_b = direct.stock_movement("MILK-WHL-GAL", start, TODAY)
        self.assertEqual((mv_a["closing_qty"], mv_a["reported_qty"]),
                         (mv_b["closing_qty"], mv_b["reported_qty"]))

    def test_pages_make_no_api_calls_once_synced(self):
        app = self._app()
        before = len(self._api_log())
        c = app.test_client()
        for path in ("/", "/cogs", "/stock-on-hand", "/stock-valuation", "/stock-movement",
                     "/purchases", "/sales", "/adjustments", "/transactions"):
            self.assertEqual(c.get(path).status_code, 200, path)
        self.assertEqual(len(self._api_log()), before)

    def test_incremental_sync_picks_up_backdated_change(self):
        app = self._app()
        syncer = app.extensions["unleashed"]["syncer"]
        reports = app.extensions["unleashed"]["reports"]
        last_month = add_months(TODAY.replace(day=1), -1)
        po = next(p for p in self.ds.purchase_orders
                  if p["ReceivedDate"] and (TODAY - p["ReceivedDate"]).days > 20
                  and p["ReceivedDate"].replace(day=1) == last_month)
        before = reports.purchase_order(po["OrderNumber"])["shipping"]
        po["Freight"] = round(po["Freight"] + 10, 2)
        po["Modified"] = dt.date.today()  # Unleashed stamps edits with the real clock
        result = syncer.sync()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["full"])
        self.assertIn("modifiedSince=", self._api_log("/PurchaseOrders/%")[-1][1])
        self.assertAlmostEqual(reports.purchase_order(po["OrderNumber"])["shipping"], before + 10)
        # Stock snapshots from that PO's receipt onward were re-fetched.
        refetched = self._api_log(f"%asAtDate={po['ReceivedDate'].isoformat()}%")
        self.assertEqual(len(refetched), 2)

    def test_full_sync_removes_documents_deleted_in_unleashed(self):
        app = self._app()
        syncer = app.extensions["unleashed"]["syncer"]
        reports = app.extensions["unleashed"]["reports"]
        gone = self.ds.sales_orders.pop(-2)
        syncer.sync(full=False)
        self.assertIsNotNone(reports.sales_order(gone["OrderNumber"]))
        syncer.sync(full=True)
        self.assertIsNone(reports.sales_order(gone["OrderNumber"]))

    def test_failed_sync_keeps_previous_data(self):
        app = self._app()
        ext = app.extensions["unleashed"]
        ext["client"].api_key = "wrong"
        self.assertEqual(ext["syncer"].sync()["status"], "failed")
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("Last sync failed", html)
        self.assertIn("COGS spend", html)

    def test_first_sync_page_until_data_arrives(self):
        app = self._app(SYNC_ON_STARTUP="off")
        c = app.test_client()
        self.assertIn("No Unleashed data yet", c.get("/").get_data(as_text=True))
        self.assertEqual(c.get("/connection").status_code, 200)
        c.post("/sync")
        self.assertIn("COGS spend", c.get("/").get_data(as_text=True))

    def test_switching_account_starts_a_fresh_copy(self):
        app = self._app()
        store = app.extensions["unleashed"]["store"]
        self.assertTrue(store.has_data())
        other = "live:https://example.invalid:other-account"
        if store.db.shared:  # other people's data: only replaced when explicitly allowed
            from unleashed.store import SourceMismatch
            with self.assertRaises(SourceMismatch):
                store.ensure_source(other, "live")
            self.assertTrue(store.has_data())
        store.ensure_source(other, "live", allow_reset=True)
        self.assertFalse(store.has_data())
        self.assertEqual(store.document_count("SalesOrders"), 0)

    def test_dates_older_than_the_copy_come_from_the_api(self):
        app = self._app()
        before = len(self._api_log())
        html = app.test_client().get("/sales?start=2024-01-01&end=2024-03-31").get_data(as_text=True)
        self.assertIn("Sales enquiry", html)
        self.assertEqual(len(self._api_log()) - before, 1)
        self.assertIn("startDate=2024-01-01", self._api_log("/SalesOrders/%")[-1][1])

    def test_required_snapshot_dates(self):
        app = self._app(SYNC_ON_STARTUP="off")
        dates = app.extensions["unleashed"]["syncer"].required_snapshot_dates(TODAY)
        self.assertIn(dt.date(2026, 6, 30), dates)          # last month end
        self.assertIn(dt.date(2025, 6, 30), dates)          # 13th month end back
        self.assertIn(dt.date(2026, 6, 1), dates)           # every day of last month
        self.assertIn(dt.date(2026, 7, 14), dates)          # yesterday
        self.assertNotIn(TODAY, dates)                      # today comes from 'current'


class SharedDatabaseTests(unittest.TestCase):
    """Several app instances on one database: viewers, the sync lock, never wiping shared data."""

    def setUp(self):
        self.database = fresh_database(self)
        self.mock = create_mock_app(today=TODAY)

    def _syncing_app(self, **config):
        return make_app(self, database=self.database, UNLEASHED_MODE="live",
                        UNLEASHED_API_ID=MOCK_API_ID, UNLEASHED_API_KEY=MOCK_API_KEY,
                        UNLEASHED_TRANSPORT=WsgiTransport(self.mock), TODAY=TODAY, **config)

    def _viewer_app(self):
        return make_app(self, database=self.database, UNLEASHED_MODE="viewer",
                        UNLEASHED_API_ID="", UNLEASHED_API_KEY="", TODAY=TODAY)

    def _api_calls(self, app):
        return app.extensions["db"].query("SELECT COUNT(*) AS n FROM unleashed_api_log")[0]["n"]

    def test_viewer_reads_the_shared_copy_without_an_api_key(self):
        syncing = self._syncing_app()
        viewer = self._viewer_app()
        self.assertIsNone(viewer.extensions["unleashed"]["syncer"])
        before = self._api_calls(viewer)
        c = viewer.test_client()
        for path in ("/", "/cogs", "/purchases", "/sales", "/stock-on-hand", "/transactions"):
            html = c.get(path).get_data(as_text=True)
            self.assertIn("view only", html, path)
            self.assertNotIn("Couldn't load data", html, path)
            self.assertNotIn(">Sync now<", html, path)
        self.assertEqual(self._api_calls(viewer), before)
        self.assertAlmostEqual(viewer.extensions["unleashed"]["reports"].cogs(TODAY)["cogs"],
                               syncing.extensions["unleashed"]["reports"].cogs(TODAY)["cogs"])

    def test_viewer_needs_the_syncing_instance_to_set_up_the_database(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._viewer_app()
        self.assertIn("Start the instance that has the Unleashed API key first", str(ctx.exception))

    def test_viewer_waits_for_first_sync(self):
        self._syncing_app(SYNC_ON_STARTUP="off")  # creates the tables, hasn't synced yet
        viewer = self._viewer_app()
        html = viewer.test_client().get("/").get_data(as_text=True)
        self.assertIn("Waiting for the first copy", html)

    def test_only_one_instance_syncs_at_a_time(self):
        first = self._syncing_app()
        second = self._syncing_app(SYNC_ON_STARTUP="off")
        store = first.extensions["unleashed"]["store"]
        self.assertTrue(store.acquire_lock("other-machine", 600))
        self.assertEqual(second.extensions["unleashed"]["syncer"].sync()["status"], "busy")
        self.assertTrue(second.extensions["unleashed"]["syncer"].running)
        store.release_lock("other-machine")
        self.assertEqual(second.extensions["unleashed"]["syncer"].sync()["status"], "ok")

    def test_a_stalled_sync_lock_expires(self):
        app = self._syncing_app()
        store = app.extensions["unleashed"]["store"]
        self.assertTrue(store.acquire_lock("crashed-machine", -1))  # already expired
        self.assertEqual(app.extensions["unleashed"]["syncer"].sync()["status"], "ok")

    def test_shared_database_is_not_wiped_by_another_setup(self):
        from unittest import mock as umock

        from unleashed.db import Database
        self._syncing_app()
        with umock.patch.object(Database, "shared", new_callable=umock.PropertyMock,
                                return_value=True):
            other = make_app(self, database=self.database, UNLEASHED_MODE="mock")
            ext = other.extensions["unleashed"]
            self.assertIn("different Unleashed account", ext["source_error"])
            self.assertIsNone(ext["syncer"])
            self.assertGreater(ext["store"].document_count("SalesOrders"), 0)
            self.assertIn("different Unleashed account",
                          other.test_client().get("/").get_data(as_text=True))

            replaced = make_app(self, database=self.database, UNLEASHED_MODE="mock",
                                ALLOW_DATABASE_RESET=True)
            self.assertIsNone(replaced.extensions["unleashed"]["source_error"])
            self.assertEqual(replaced.extensions["unleashed"]["store"].get_state("source"), "mock")

    @unittest.skipUnless(TEST_DATABASE_URL, "needs TEST_DATABASE_URL (PostgreSQL)")
    def test_shared_database_without_key_defaults_to_viewer(self):
        self._syncing_app(SYNC_ON_STARTUP="off")
        app = make_app(self, database=self.database, UNLEASHED_MODE="", UNLEASHED_API_ID="",
                       UNLEASHED_API_KEY="")
        self.assertEqual(app.config["UNLEASHED_MODE"], "viewer")


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock", DASHBOARD_PASSWORD="latte-art")
        self.client = self.app.test_client()

    def test_dashboard_requires_the_password(self):
        res = self.client.get("/cogs")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login", res.headers["Location"])
        bad = self.client.post("/login", data={"password": "nope"}).get_data(as_text=True)
        self.assertIn("isn&#39;t right", bad)
        self.assertNotIn("COGS spend", bad)
        ok = self.client.post("/login?next=/cogs", data={"password": "latte-art"})
        self.assertEqual(ok.headers["Location"], "/cogs")
        self.assertIn("Cost of goods sold", self.client.get("/cogs").get_data(as_text=True))
        self.client.post("/logout")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_login_only_redirects_within_the_site(self):
        res = self.client.post("/login?next=//evil.example/x", data={"password": "latte-art"})
        self.assertEqual(res.headers["Location"], "/")


class TimezoneTests(unittest.TestCase):
    def test_today_follows_the_business_time_zone(self):
        def today_in(zone):
            app = make_app(self, UNLEASHED_MODE="mock", SYNC_ON_STARTUP="off", TIMEZONE=zone)
            return app.extensions["unleashed"]["reports"].today
        # UTC+14 is always a day or two ahead of UTC-11.
        self.assertGreater(today_in("Pacific/Kiritimati"), today_in("Pacific/Pago_Pago"))


class RetryTests(unittest.TestCase):
    def test_rate_limited_request_is_retried(self):
        import unleashed.client as client_mod

        class FlakyTransport:
            calls = 0

            def get(self, path, query_string, headers):
                FlakyTransport.calls += 1
                if FlakyTransport.calls == 1:
                    return 429, '{"description": "Too many requests"}'
                return 200, '{"Items": [{"WarehouseCode": "GDN"}]}'

        saved = client_mod.RETRY_WAITS
        client_mod.RETRY_WAITS = (0, 0, None)
        try:
            c = UnleashedClient("id", "key", FlakyTransport())
            self.assertEqual(c.get_all("Warehouses"), [{"WarehouseCode": "GDN"}])
            self.assertEqual(FlakyTransport.calls, 2)
        finally:
            client_mod.RETRY_WAITS = saved


class FieldFallbackTests(unittest.TestCase):
    def setUp(self):
        self.reports = Reports(client=None, today_fn=lambda: TODAY)

    def test_null_base_currency_fields_fall_back(self):
        po = self.reports._po({
            "OrderNumber": "PO-1", "OrderStatus": "Complete",
            "ReceivedDate": None, "CompletedDate": "/Date(1784073600000)/",
            "PurchaseOrderLines": [
                {"Product": {"ProductCode": "MILK"}, "OrderQuantity": 2, "BCSubTotal": None,
                 "LineTotal": 10.0, "BCUnitPrice": None, "UnitPrice": 5.0},
                {"Product": {"ProductCode": "FREIGHT"}, "OrderQuantity": 1, "LineTotal": 7.5},
            ],
        })
        self.assertEqual(po["goods"], 10.0)
        self.assertEqual(po["shipping"], 7.5)
        self.assertEqual(po["lines"][0]["unit_price"], 5.0)
        self.assertEqual(po["received_date"], TODAY)

    def test_cost_line_amount_field_priority(self):
        from unleashed.reports import cost_line_amount, cost_line_keys
        po = {"OrderNumber": "PO-2", "PurchaseOrderLines": [], "CostCentre": None,
              "CostLines": [{"Description": "Freight", "BCAmount": None, "Amount": 12.5},
                            {"Description": "Fuel levy", "BCSubTotal": 3.0, "Total": 3.3}]}
        self.assertEqual(cost_line_keys(po), ["CostLines"])
        self.assertEqual(cost_line_amount(po["CostLines"][0]), ("Amount", 12.5))
        self.assertEqual(cost_line_amount(po["CostLines"][1]), ("BCSubTotal", 3.0))
        self.assertEqual(self.reports._po(po)["shipping"], 15.5)

    def test_history_queries_are_detected(self):
        from unleashed.client import _is_historical
        self.assertTrue(_is_historical({"asAtDate": "2020-01-31"}))
        self.assertFalse(_is_historical({"asAtDate": "2999-01-01"}))
        self.assertFalse(_is_historical({"startDate": "2020-01-01"}))


class StartupConfigTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("UNLEASHED_API_ID", "UNLEASHED_API_KEY", "UNLEASHED_MODE")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _mode(self):
        return make_app(self, database=sqlite_database(self),
                        SYNC_ON_STARTUP="off").config["UNLEASHED_MODE"]

    def test_mode_defaults_to_live_only_with_credentials(self):
        self.assertEqual(self._mode(), "mock")
        os.environ.update(UNLEASHED_API_ID="id", UNLEASHED_API_KEY="key")
        self.assertEqual(self._mode(), "live")
        os.environ["UNLEASHED_MODE"] = " Mock "
        self.assertEqual(self._mode(), "mock")

    def test_env_file_saved_with_bom_is_read(self):
        from envfile import load_env_file
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("UNLEASHED_API_ID=abc-123\nUNLEASHED_MODE=live\n")
        try:
            load_env_file(path)
            self.assertEqual(os.environ.get("UNLEASHED_API_ID"), "abc-123")
            self.assertEqual(os.environ.get("UNLEASHED_MODE"), "live")
        finally:
            os.unlink(path)


class PortabilityTests(unittest.TestCase):
    def test_no_platform_specific_strftime_flags(self):
        # "%-d" / "%#d" only work on some platforms (e.g. "%-d" raises on Windows).
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "venv", "__pycache__")]
            for name in filenames:
                if name.endswith((".py", ".html")):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as f:
                        if re.search(r"%[-#][a-zA-Z]", f.read()):
                            offenders.append(os.path.relpath(path, root))
        offenders = [o for o in offenders if not o.endswith("test_unleashed.py")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
