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
              "ul_sync_runs", "ul_sync_state", "ul_sync_lock", "app_users", "app_settings")


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
    app = create_app({"TESTING": True, **SYNC_NOW, "ALLOW_NO_SIGN_IN": True,
                      **(database or fresh_database(testcase)), **config})
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


class AwsSettingsTests(unittest.TestCase):
    def test_database_url_from_aws_style_settings(self):
        from urllib.parse import unquote, urlsplit

        from unleashed.db import database_url_from_env
        self.assertEqual(database_url_from_env({}), "")
        self.assertEqual(database_url_from_env({"DATABASE_URL": " postgresql://u:p@h/d "}),
                         "postgresql://u:p@h/d")
        generated = "a#b?c@d/e:f%g h"  # RDS-generated passwords contain symbols like these
        url = database_url_from_env({"DB_HOST": "mydb.abc.us-west-2.rds.amazonaws.com",
                                     "DB_USER": "dash", "DB_PASSWORD": generated,
                                     "DB_NAME": "unleashedaccess"})
        parts = urlsplit(url)
        self.assertEqual(unquote(parts.password), generated)
        self.assertEqual((parts.hostname, parts.port, parts.path, parts.query),
                         ("mydb.abc.us-west-2.rds.amazonaws.com", 5432, "/unleashedaccess",
                          "sslmode=require"))
        eb = database_url_from_env({"RDS_HOSTNAME": "eb-db", "RDS_PORT": "5433",
                                    "RDS_DB_NAME": "ebdb", "RDS_USERNAME": "ebuser",
                                    "RDS_PASSWORD": "pw"})
        self.assertEqual(eb, "postgresql://ebuser:pw@eb-db:5433/ebdb?sslmode=require")
        secret = ('{"username": "admin", "password": "s3cr#t", "host": "sm-host", '
                  '"port": 5432, "dbname": "dash"}')
        self.assertEqual(database_url_from_env({"DB_SECRET": secret}),
                         "postgresql://admin:s3cr%23t@sm-host:5432/dash?sslmode=require")
        # RDS-managed master secrets hold only the login; the host comes from DB_HOST.
        self.assertEqual(
            database_url_from_env({"DB_SECRET": '{"username": "admin", "password": "x"}',
                                   "DB_HOST": "h", "DB_NAME": "dash"}),
            "postgresql://admin:x@h:5432/dash?sslmode=require")

    def test_health_check_needs_no_sign_in_or_data(self):
        app = make_app(self, UNLEASHED_MODE="mock", SYNC_ON_STARTUP="off",
                       DASHBOARD_PASSWORD="secret")
        res = app.test_client().get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok"})

    def test_sync_once_command(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db = sqlite_database(self)["DATABASE_PATH"]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("UNLEASHED_", "DATABASE_URL", "DB_", "RDS_"))}
        # No .env and blank database settings: never touch a real (shared) database.
        env.update(DATABASE_PATH=db, UNLEASHED_MODE="mock", DATABASE_URL="", DB_HOST="",
                   RDS_HOSTNAME="", DB_SECRET="", ENV_FILE=os.path.join(root, "no-such.env"))
        run = subprocess.run([sys.executable, "sync_once.py"], cwd=root, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Sync ok (full)", run.stdout)
        again = subprocess.run([sys.executable, "sync_once.py"], cwd=root, env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertIn("Sync ok (incremental)", again.stdout)


class TeamAccountTests(unittest.TestCase):
    ADMIN = {"ADMIN_EMAIL": "Owner@Yeems.test", "ADMIN_PASSWORD": "first-temp-pass",
             "ADMIN_NAME": "Owner"}

    def setUp(self):
        self.database = fresh_database(self)
        self.app = self._app()

    def _app(self, **config):
        return make_app(self, database=self.database, UNLEASHED_MODE="mock",
                        **{**self.ADMIN, **config})

    def _sign_in(self, client, email, password):
        return client.post("/login", data={"email": email, "password": password})

    def _choose_password(self, client, current, new):
        return client.post("/account", data={"current_password": current, "new_password": new,
                                             "confirm_password": new})

    def _admin_client(self):
        c = self.app.test_client()
        self._sign_in(c, "owner@yeems.test", "first-temp-pass")
        self._choose_password(c, "first-temp-pass", "owner-own-password")
        return c

    def _add(self, admin, name, email, role="member"):
        import re
        html = admin.post("/team/add", data={"name": name, "email": email, "role": role}
                          ).get_data(as_text=True)
        match = re.search(r'class="temp-password mono">([^<]+)<', html)
        self.assertIsNotNone(match, html)
        return match.group(1)

    def test_first_admin_must_choose_a_password(self):
        c = self.app.test_client()
        self.assertIn("/login", c.get("/").headers["Location"])
        res = self._sign_in(c, "OWNER@yeems.test", "first-temp-pass")
        self.assertEqual(res.headers["Location"], "/account")
        self.assertIn("/account", c.get("/cogs").headers["Location"])
        bad = self._choose_password(c, "wrong", "owner-own-password").get_data(as_text=True)
        self.assertIn("current password isn", bad)
        short = self._choose_password(c, "first-temp-pass", "short").get_data(as_text=True)
        self.assertIn("at least 10 characters", short)
        self.assertEqual(self._choose_password(c, "first-temp-pass", "owner-own-password").status_code, 302)
        page = c.get("/").get_data(as_text=True)
        self.assertIn("COGS spend", page)
        self.assertIn(">Owner<", page)
        self.assertIn(">Team<", page)
        # Restarting with the same settings doesn't reset the admin's password.
        restarted = self._app()
        c2 = restarted.test_client()
        self.assertIn("don&#39;t match", self._sign_in(c2, "owner@yeems.test", "first-temp-pass").get_data(as_text=True))
        self.assertEqual(self._sign_in(c2, "owner@yeems.test", "owner-own-password").headers["Location"], "/")

    def test_member_sets_own_password_and_has_no_admin_pages(self):
        admin = self._admin_client()
        temp = self._add(admin, "Barista Bea", "bea@yeems.test")
        bea = self.app.test_client()
        self.assertEqual(self._sign_in(bea, "bea@yeems.test", temp).headers["Location"], "/account")
        self._choose_password(bea, temp, "bea-own-password")
        page = bea.get("/").get_data(as_text=True)
        self.assertIn("COGS spend", page)
        self.assertNotIn(">Team<", page)
        self.assertEqual(bea.get("/team").status_code, 403)
        self.assertEqual(bea.post("/connection", data={"action": "check"}).status_code, 403)
        self.assertEqual(bea.post("/team/add", data={"name": "x", "email": "x@y.z"}).status_code, 403)
        self.assertIn("already an account", admin.post(
            "/team/add", data={"name": "Again", "email": "BEA@yeems.test"}).get_data(as_text=True))

    def test_wrong_passwords_lock_the_account(self):
        c = self.app.test_client()
        unknown = self._sign_in(c, "nobody@yeems.test", "whatever-pass").get_data(as_text=True)
        self.assertIn("don&#39;t match", unknown)
        for _ in range(4):
            self.assertIn("don&#39;t match",
                          self._sign_in(c, "owner@yeems.test", "wrong-pass").get_data(as_text=True))
        self.assertIn("Too many wrong passwords",
                      self._sign_in(c, "owner@yeems.test", "wrong-pass").get_data(as_text=True))
        locked = self._sign_in(c, "owner@yeems.test", "first-temp-pass")
        self.assertIn("Too many wrong passwords", locked.get_data(as_text=True))

    def test_deactivating_or_resetting_signs_the_person_out(self):
        admin = self._admin_client()
        temp = self._add(admin, "Barista Bea", "bea@yeems.test")
        bea_id = self.app.extensions["users"].by_email("bea@yeems.test")["id"]
        bea = self.app.test_client()
        self._sign_in(bea, "bea@yeems.test", temp)
        self._choose_password(bea, temp, "bea-own-password")
        self.assertEqual(bea.get("/").status_code, 200)

        admin.post(f"/team/{bea_id}", data={"action": "deactivate"})
        self.assertIn("/login", bea.get("/").headers["Location"])
        self.assertIn("don&#39;t match", self._sign_in(bea, "bea@yeems.test", "bea-own-password").get_data(as_text=True))
        admin.post(f"/team/{bea_id}", data={"action": "activate"})
        self._sign_in(bea, "bea@yeems.test", "bea-own-password")
        self.assertEqual(bea.get("/").status_code, 200)

        import re
        html = admin.post(f"/team/{bea_id}", data={"action": "reset"}).get_data(as_text=True)
        new_temp = re.search(r'class="temp-password mono">([^<]+)<', html).group(1)
        self.assertIn("/login", bea.get("/").headers["Location"])
        self.assertEqual(self._sign_in(bea, "bea@yeems.test", new_temp).headers["Location"], "/account")

    def test_the_last_admin_stays_an_admin(self):
        admin = self._admin_client()
        me = self.app.extensions["users"].by_email("owner@yeems.test")["id"]
        self.assertIn("can&#39;t deactivate your own account",
                      admin.post(f"/team/{me}", data={"action": "deactivate"}).get_data(as_text=True))
        self.assertIn("Ask another admin",
                      admin.post(f"/team/{me}", data={"action": "role", "role": "member"}).get_data(as_text=True))
        from accounts import AccountError
        with self.assertRaises(AccountError):
            self.app.extensions["users"].set_role(me, "member")
        self.assertEqual(admin.get("/").status_code, 200)

    def test_admin_reset_restores_access(self):
        self._admin_client()
        recovered = self._app(ADMIN_PASSWORD="recovery-pass-1", ADMIN_RESET=True)
        c = recovered.test_client()
        self.assertEqual(self._sign_in(c, "owner@yeems.test", "recovery-pass-1").headers["Location"], "/account")

    def test_sign_in_key_is_generated_once_and_shared(self):
        other = self._app()
        self.assertEqual(self.app.config["SECRET_KEY"], other.config["SECRET_KEY"])
        self.assertEqual(len(self.app.config["SECRET_KEY"]), 64)
        admin = self._admin_client()
        # A cookie signed by one server works on the other (several servers, one database).
        cookie = admin.get_cookie("session")
        c2 = other.test_client()
        c2.set_cookie("session", cookie.value)
        self.assertEqual(c2.get("/").status_code, 200)


class StartupProblemTests(unittest.TestCase):
    """A broken setting shows an explanation page instead of a bare 502."""

    def test_real_database_errors_are_explained(self):
        from startup import explain
        cases = {
            'connection failed: connection to server at "10.0.1.5", port 5432 failed: FATAL:  '
            'password authentication failed for user "unleashed_admin"': "username or password",
            'connection failed: FATAL:  database "unleashedaccess" does not exist': "no database with that name",
            'connection failed: connection to server at "10.0.1.5", port 5432 failed: timeout expired':
                "can't reach the database",
            "couldn't get a connection after 30.00 sec": "can't reach the database",
            'connection failed: could not translate host name "https://x" to address': "DB_HOST",
            "ADMIN_EMAIL / ADMIN_PASSWORD settings: Passwords need at least 10 characters.":
                "first admin account",
            "ADMIN_EMAIL / ADMIN_PASSWORD settings: Enter a valid email address.": "first admin account",
        }
        for message, expected in cases.items():
            headline, todo = explain(RuntimeError(message))
            self.assertIn(expected, headline + todo, message)
        self.assertIn("valid email", explain(RuntimeError(
            "ADMIN_EMAIL / ADMIN_PASSWORD settings: Enter a valid email address."))[1])

    def test_guard_explains_then_recovers(self):
        from werkzeug.test import Client

        from startup import StartupGuard
        attempts = {"n": 0}
        good = make_app(self, UNLEASHED_MODE="mock")

        def factory():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("couldn't get a connection after 30.00 sec")
            return good

        guard = StartupGuard(factory, retry_seconds=0)
        guard._last_try = float("inf")  # hold the failure for the first two requests
        c = Client(guard)
        page = c.get("/")
        self.assertEqual(page.status_code, 503)
        self.assertIn("can&#x27;t reach the database", page.get_data(as_text=True))
        self.assertEqual(c.get("/healthz").status_code, 503)
        guard._last_try = 0  # the retry interval has passed
        self.assertEqual(c.get("/healthz").status_code, 200)
        self.assertEqual(attempts["n"], 2)

    def test_too_short_admin_password_is_explained(self):
        from werkzeug.test import Client

        from startup import StartupGuard
        db = fresh_database(self)
        guard = StartupGuard(lambda: make_app(self, database=db, UNLEASHED_MODE="mock",
                                              ADMIN_EMAIL="a@b.test", ADMIN_PASSWORD="short"),
                             retry_seconds=999)
        body = Client(guard).get("/").get_data(as_text=True)
        self.assertIn("ADMIN_PASSWORD needs at least 10 characters", body)

    def test_real_data_needs_sign_in(self):
        mock = create_mock_app(today=TODAY)
        live = dict(UNLEASHED_MODE="live", UNLEASHED_API_ID=MOCK_API_ID,
                    UNLEASHED_API_KEY=MOCK_API_KEY, UNLEASHED_TRANSPORT=WsgiTransport(mock))
        app = make_app(self, ALLOW_NO_SIGN_IN=False, **live)
        c = app.test_client()
        for path in ("/", "/sales", "/purchases/PO-00000001", "/connection", "/api-log"):
            res = c.get(path)
            self.assertEqual(res.status_code, 403, path)
            self.assertIn("Sign-in isn't set up", res.get_data(as_text=True), path)
        self.assertEqual(c.get("/healthz").status_code, 200)
        mocked = make_app(self, ALLOW_NO_SIGN_IN=False, UNLEASHED_MODE="mock")
        self.assertEqual(mocked.test_client().get("/").status_code, 200)  # simulated data is fine


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


class SchemaTests(unittest.TestCase):
    def test_test_cleanup_covers_every_table(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "schema.sql"), encoding="utf-8") as f:
            tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", f.read()))
        self.assertEqual(tables, set(APP_TABLES))


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
