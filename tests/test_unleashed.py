"""Tests for the Unleashed client, mock server, reports and dashboard."""
import copy
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unleashed.client import (UnleashedClient, UnleashedError, WsgiTransport, parse_date, sign,  # noqa: E402
                              writable)
from unleashed.mock_server import MOCK_API_ID, MOCK_API_KEY, create_mock_app  # noqa: E402
from unleashed.reports import Reports, add_months, month_end  # noqa: E402
from unleashed.store import LocalStore, snapshot_key  # noqa: E402

TODAY = dt.date(2026, 7, 15)
# Tests finish the first sync before serving and never start the background thread.
SYNC_NOW = {"SYNC_ON_STARTUP": "blocking", "SYNC_INTERVAL_MINUTES": 0}
# Set TEST_DATABASE_URL=postgresql://... to run the app tests against PostgreSQL.
# Every table the app owns in that database is dropped before each test.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
APP_TABLES = ("unleashed_api_log", "ul_records", "ul_documents", "ul_stock_snapshots",
              "ul_sync_runs", "ul_sync_state", "ul_sync_lock", "app_users", "app_settings",
              "prod_steps", "prod_runs", "prod_step_records", "prod_step_entries",
              "access_profiles", "app_user_profiles", "maint_locations", "maint_tasks",
              "maint_completions", "maint_attachments", "maint_equipment", "maint_equipment_moves",
              "maint_counters", "maint_readings", "maint_tasklists", "maint_requests", "maint_orders",
              "maint_order_costs", "maint_files", "prod_step_pictures", "stock_counts", "stock_count_lines")


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

    def test_a_moment_counts_on_its_day_in_the_business_time_zone(self):
        from zoneinfo import ZoneInfo
        from unleashed.client import set_business_timezone
        evening = "/Date(1793674800000)/"  # Nov 3 2026, 03:00 UTC: 7 pm on Nov 2 in Los Angeles
        self.assertEqual(parse_date(evening), dt.date(2026, 11, 3))  # no time zone set: UTC
        set_business_timezone(ZoneInfo("America/Los_Angeles"))
        try:
            self.assertEqual(parse_date(evening), dt.date(2026, 11, 2))
            self.assertEqual(parse_date("/Date(1793664000000)/"), dt.date(2026, 11, 3))  # a plain date stays
        finally:
            set_business_timezone(None)

    def test_a_date_sent_to_unleashed_stays_on_its_day_however_it_reads_the_time(self):
        """Unleashed keeps the Pacific date of the time it's sent, read in UTC, New Zealand or
        Pacific time: midnight and noon both came back as the day before."""
        from zoneinfo import ZoneInfo
        from unleashed.client import body_date
        pacific = ZoneInfo("America/Los_Angeles")
        day = dt.date(2026, 1, 1)
        while day.year == 2026:  # every day, both sides of each daylight-saving change
            sent = dt.datetime.fromisoformat(body_date(day))
            for zone in ("UTC", "Pacific/Auckland", "America/Los_Angeles"):
                kept = sent.replace(tzinfo=ZoneInfo(zone)).astimezone(pacific).date()
                self.assertEqual(kept, day, f"{day} read as {zone}")
            day += dt.timedelta(days=1)

    def test_dates_sent_back_to_unleashed_are_in_the_form_it_accepts(self):
        doc = {"AssembleBy": "/Date(1793059200000)/", "Quantity": 2, "Comments": "/Date( in a note",
               "AssemblyLines": [{"LastModifiedOn": "/Date(1793080800500+1300)/", "Product": None}]}
        self.assertEqual(writable(doc), {  # a date goes back as body_date sends it, so it stays the same day
            "AssembleBy": "2026-10-27T23:00:00", "Quantity": 2, "Comments": "/Date( in a note",
            "AssemblyLines": [{"LastModifiedOn": "2026-10-27T06:00:00", "Product": None}]})
        self.assertEqual(doc["AssembleBy"], "/Date(1793059200000)/")  # a copy; the original is unchanged

    def test_cogs_formula_matches_cost_of_sales(self):
        for month in (TODAY, add_months(TODAY.replace(day=1), -1), add_months(TODAY.replace(day=1), -5)):
            c = self.reports.cogs(month)
            self.assertAlmostEqual(c["cogs"], c["opening"] + c["purchases"] - c["closing"], places=6)
            recon = self.reports.cogs_reconciliation(month)
            self.assertAlmostEqual(c["cogs"], recon["total"], delta=1.0, msg=str(month))
            self.assertGreater(c["shipping"], 0)

    def test_unexplained_cogs_names_its_causes(self):
        month, through = dt.date(2026, 10, 1), dt.date(2026, 10, 31)

        class Stub(Reports):
            def stock_on_hand(self, as_at=None, warehouse=None):
                value = {"WH": 100.0, "CS": 0.0}[warehouse] if as_at == dt.date(2026, 9, 30) else \
                    {"WH": 30.0, "CS": 0.0}[warehouse]
                return {"rows": [{"product_code": "MILK", "description": "Milk", "value": value}]}

            def _completed_sales(self, start, end, warehouses=None):
                return [{"lines": [{"product_code": "MILK", "cost": 50.0}]}]

            def stock_adjustments(self, start, end):
                return {"rows": [{"product_code": "MILK", "warehouse": "WH", "value_change": -5.0}]}

            def _flows(self, first_day, last_day, warehouses=None):
                return []

            def _completed_returns(self, endpoint, start, through, warehouses=None):
                return []

            def transfers(self, start=None, end=None, status=None):
                return []

            def _completed_assemblies(self, start, end):
                return [{"completed": True, "completed_date": dt.date(2026, 10, 9), "number": "ASM-1",
                         "product_code": "LATTE", "cost": 8.0, "supplier_costs": 2.5,
                         "lines": [{"product_code": "MILK", "cost": 8.0}]}]

        received = [
            {"number": "PO-1", "goods": 20.0, "shipping": 2.0, "total": 22.0, "warehouse": "WH",
             "lines": [{"product_code": "MILK", "qty": 10, "received_qty": 6, "unit_price": 2.0, "line_total": 20.0}]},
            {"number": "PO-2", "goods": 9.0, "shipping": 0.0, "total": 9.0, "warehouse": "KTOWN",
             "lines": [{"product_code": "MILK", "qty": 3, "received_qty": 3, "unit_price": 3.0, "line_total": 9.0}]}]
        cogs = {"month": month, "through": through, "prior_end": dt.date(2026, 9, 30), "is_current": False,
                "warehouses": ["WH", "CS"], "received": received}
        why = Stub(None, today_fn=lambda: dt.date(2026, 11, 5)).unexplained(cogs)
        f = why["findings"]
        self.assertEqual(f["partly_received"], [{"number": "PO-1", "value": 8.0}])  # 4 not received, at $2
        self.assertEqual(f["elsewhere"], [{"number": "PO-2", "warehouse": "KTOWN", "value": 9.0}])
        self.assertEqual((f["production_added"], f["production_orders"]), (2.5, [{"number": "ASM-1", "value": 2.5}]))
        milk = next(p for p in why["products"] if p["product_code"] == "MILK")
        # 100 + 31 received − 8 into production − 50 sold − 5 written off = 68 should be there; 30 is.
        self.assertEqual((milk["expected"], milk["closing"], milk["gap"]), (68.0, 30.0, 38.0))

    def test_when_unleashed_cuts_the_day_is_found_and_month_ends_are_exact(self):
        from zoneinfo import ZoneInfo
        import unleashed.client as client_module
        from unleashed.client import set_business_timezone
        from unleashed.reports import stock_cutoff
        pacific, today = ZoneInfo("America/Los_Angeles"), dt.date(2026, 11, 3)
        before = client_module._business_tz
        set_business_timezone(pacific)
        self.addCleanup(set_business_timezone, before)
        # Sales through the day, Pacific time: after midnight, morning, afternoon, late evening.
        sales = []
        day = dt.date(2026, 9, 1)
        while day < today:
            for n, (hour, minute) in enumerate(((1, 30), (9, 0), (15, 0), (21, 30))):
                qty = 3 + (day.toordinal() * 7 + n * 5) % 11  # varies day to day
                at = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=pacific)
                sales.append({"number": f"SO-{len(sales)}", "completed_at": at, "completed_date": day,
                              "order_date": day - dt.timedelta(days=20), "warehouse": None, "customer_code": "STORE",
                              "cost": qty * 2.0, "lines": [{"product_code": "BEANS", "qty": qty, "cost": qty * 2.0}]})
            day += dt.timedelta(days=1)
        now = dt.datetime.combine(today, dt.time(10), tzinfo=pacific)
        sales = [o for o in sales if o["completed_at"] <= now]

        class Account(Reports):
            """An Unleashed account whose stock "as at" a date is at `truth`'s moment."""
            truth = "end"
            settings = {"warehouses": None, "pending": None, "stores": ["STORE"], "stock_as_at": "end"}

            def cogs_settings(self):
                return self.settings

            def stock_on_hand(self, as_at=None, warehouse=None, group=None, product=None):
                cut = (self.client.read_at if self.client else now) if as_at is None else stock_cutoff(self.truth, as_at)
                qty = 100000 - sum(o["lines"][0]["qty"] for o in sales if o["completed_at"] <= cut)
                return {"rows": [{"product_code": "BEANS", "description": "Beans", "qty": qty, "value": qty * 2.0}]}

            def inventory_value(self, as_at=None, warehouse=None):
                return self.stock_on_hand(as_at)["rows"][0]["value"]

            def received_purchases(self, start, end):
                return []

            def _completed_sales(self, start, end, warehouses=None):
                return [o for o in sales if start <= o["completed_date"] <= end]

            def stock_adjustments(self, start, end):
                return {"rows": []}

            def _completed_assemblies(self, start, end):
                return []

            def _completed_returns(self, endpoint, start, through, warehouses=None):
                return []

            def transfers(self, start=None, end=None, status=None):
                return []

            def customers(self):
                return [{"CustomerCode": "STORE", "CustomerName": "Store"}]

        october = dt.date(2026, 10, 1)
        october_sales = sum(o["cost"] for o in sales if o["completed_date"].month == 10)
        november_sales = sum(o["cost"] for o in sales if o["completed_date"].month == 11)
        for truth in ("end", "start", "utc_end", "nz_end", "nz_start"):
            r = Account(None, today_fn=lambda: today)
            r.truth = truth
            r.settings = dict(r.settings, stock_as_at="end")
            timing = r.stock_timing()
            self.assertEqual(timing["verdict"], truth, timing["scores"])
            self.assertAlmostEqual(timing["scores"][truth], 0.0)
            if truth != "end":  # read the wrong way, COGS is off and some of it "not on an order"
                self.assertNotAlmostEqual(r.cogs(october)["cogs"], october_sales, places=2)
                self.assertGreater(timing["scores"]["end"], 0)
            self.assertEqual(timing["residual"], 0)  # read the right way, every day's change is explained
            r.settings = dict(r.settings, stock_as_at=truth)
            for month, expected in ((october, october_sales), (today, november_sales)):
                c = r.cogs(month, daily=True)
                self.assertAlmostEqual(c["cogs"], expected, places=6, msg=(truth, month))  # every sale, once
                self.assertAlmostEqual(c["cogs"], r.cogs_reconciliation(month)["total"], places=6)
                self.assertAlmostEqual(r.cogs_by_store(c)["rest"], 0.0, places=6)  # nothing unexplained
                self.assertAlmostEqual(r.unexplained(c)["gap"], 0.0, places=6)
                self.assertAlmostEqual(c["daily"][-1]["cogs_to_date"], expected, places=6)
                self.assertTrue(all(abs(d["value"]) < 1e-6 for d in r.daily_unexplained(c)["days"]))  # no drift

        class Synced:
            """The stored copy: its stock now was read at 6:30, before the documents (to 10:00)."""
            read_at = dt.datetime.combine(today, dt.time(6, 30), tzinfo=pacific)

            def current_stock_at(self):
                return self.read_at

        r = Account(Synced(), today_fn=lambda: today)
        r.settings = dict(r.settings, stock_as_at="end")
        self.assertLess(r.stock_on_hand()["rows"][0]["qty"], 100000)
        c = r.cogs(today)
        self.assertAlmostEqual(c["cogs"], november_sales, places=6)  # the 9:00 sales today count once
        self.assertAlmostEqual(r.cogs_by_store(c)["rest"], 0.0, places=6)

    def test_orders_placed_long_before_they_are_completed_count_in_the_month_completed(self):
        from unleashed.store import LocalStoreClient
        store = make_app(self, UNLEASHED_MODE="mock", SYNC_ON_STARTUP="off").extensions["unleashed"]["store"]
        when = lambda d: f"/Date({int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)})/"
        store.upsert_documents("SalesOrders", [
            {"Guid": "g1", "OrderNumber": "SO-1", "OrderStatus": "Completed", "OrderDate": when(dt.date(2026, 8, 20)),
             "CompletedDate": when(dt.date(2026, 10, 4)), "Customer": {"CustomerCode": "STORE"},
             "SalesOrderLines": [{"Product": {"ProductCode": "BEANS"}, "OrderQuantity": 5,
                                  "AverageLandedPriceAtTimeOfSale": 2.0}], "SubTotal": 20}])
        r = Reports(LocalStoreClient(store, today_fn=lambda: dt.date(2026, 11, 3)), today_fn=lambda: dt.date(2026, 11, 3))
        self.assertEqual([o["number"] for o in r._completed_sales(dt.date(2026, 10, 1), dt.date(2026, 10, 31))],
                         ["SO-1"])  # placed six weeks before October

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

    def test_stock_on_hand_shows_value_by_product_group(self):
        page = self.client.get("/stock-on-hand").get_data(as_text=True)
        self.assertIn("Value by product group", page)
        self.assertIn(">Dairy</a>", page)
        filtered = self.client.get("/stock-on-hand?group=Dairy").get_data(as_text=True)
        self.assertIn('class="row-selected"', filtered)
        self.assertIn(">Coffee &amp; Tea</a>", filtered)  # every group's value stays, whichever is listed
        csv_text = self.client.get("/stock-on-hand?format=csv&by=group").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("Product Group,Products,Qty,Value,Share"))
        old = self.client.get("/stock-valuation?as_at=2026-07-01")
        self.assertEqual((old.status_code, old.headers["Location"]), (301, "/stock-on-hand?as_at=2026-07-01"))
        self.assertNotIn(">Valuation</a>", page)

    def test_pages_render(self):
        for path in ("/", "/cogs", "/stock-on-hand",
                     "/stock-movement",
                     "/purchases", "/purchases/PO-00000001",
                     "/sales", "/sales/SO-00000001",
                     "/adjustments", "/transactions",
                     "/production", "/production/orders", "/production/orders/ASM-00000001",
                     "/production/requirements", "/production/boms", "/production/boms/BOM-VSC",
                     "/production/usage", "/production?month=2026-01",
                     "/api-log", "/connection"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertNotIn("Couldn't load data", res.get_data(as_text=True), path)

    def test_grouped_tabs(self):
        import re

        def links(path, active=False):
            html = self.client.get(path).get_data(as_text=True)
            cls = r'[^"]*\bactive\b[^"]*' if active else r'[^"]*'
            return re.findall(rf'<a href="[^"]*" class="{cls}">([^<]+)</a>', html)

        overview = links("/")
        self.assertEqual(overview[:5], ["Finance", "Warehouse", "Production", "Maintenance", "Settings"])
        self.assertEqual(overview[5:], ["Overview", "COGS"])  # Finance's pages
        self.assertEqual(links("/", active=True), ["Finance", "Overview"])
        # Stock movement and adjustments are under Transactions, with transfers and stock counts.
        warehouse = ["Stock on hand", "Purchases", "Sales", "Transactions"]
        for path in ("/stock-on-hand", "/transactions", "/stock-movement", "/adjustments", "/transfers",
                     "/stock-counts", "/transactions/history", "/adjustments/new", "/transfers/new"):
            self.assertEqual(links(path)[5:9], warehouse, path)
        for path in ("/stock-movement", "/adjustments", "/transfers", "/stock-counts", "/transactions/history"):
            self.assertEqual(links(path, active=True), ["Warehouse", "Transactions"], path)
        sub = self.client.get("/stock-movement").get_data(as_text=True)
        self.assertIn('class="active" aria-current=page>Stock movement</a>', sub)
        for path in ("/connection", "/api-log"):
            self.assertEqual(links(path)[-3:], ["Connection", "COGS", "API log"], path)  # no Team without accounts
        self.assertEqual(links("/api-log", active=True), ["Settings", "API log"])
        self.assertEqual(links("/purchases/PO-00000001", active=True), ["Warehouse", "Purchases"])
        self.assertEqual(links("/maintenance/", active=True), ["Maintenance", "Overview"])

    def test_csv_export(self):
        res = self.client.get("/stock-on-hand?format=csv")
        self.assertEqual(res.mimetype, "text/csv")
        self.assertTrue(res.get_data(as_text=True).startswith("Product Code,"))
        for path, header in (("/production/orders", "Order,"), ("/production/requirements", "Component,"),
                             ("/production/boms", "Bill,"), ("/production/usage", "Component,")):
            res = self.client.get(path + "?format=csv")
            self.assertEqual(res.mimetype, "text/csv", path)
            self.assertTrue(res.get_data(as_text=True).startswith(header), path)
            self.assertGreater(len(res.get_data(as_text=True).splitlines()), 2, path)

    def test_missing_production_documents_are_404(self):
        self.assertEqual(self.client.get("/production/orders/ASM-NOPE").status_code, 404)
        self.assertEqual(self.client.get("/production/boms/BOM-NOPE").status_code, 404)

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
            self.assertNotIn("Live Unleashed API", html, path)  # the usual case: no badge by the name
            self.assertNotIn("Couldn't load data", html, path)

    def test_live_data_check_passes_on_mock_data(self):
        html = self.client.post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("Live data check", html)
        self.assertNotIn("! Check", html)
        self.assertNotIn("Failed", html)
        self.assertIn("Purchase order cost line", html)
        self.assertIn("PurchaseOrderCosts.BCCost", html)
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

    def test_cost_lines_are_fetched_from_each_orders_costs(self):
        app = self._app(_strip_cost_lines_from_lists)
        reports = app.extensions["unleashed"]["reports"]
        stripped = reports.cogs(TODAY)
        direct = Reports(UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(create_mock_app(today=TODAY)),
                                         mode="mock"), today_fn=lambda: TODAY)
        full = direct.cogs(TODAY)
        self.assertGreater(stripped["shipping"], 0)
        self.assertAlmostEqual(stripped["shipping"], full["shipping"], places=6)
        self.assertAlmostEqual(stripped["cogs"], full["cogs"], places=6)
        html = app.test_client().post("/connection", data={"action": "check"}).get_data(as_text=True)
        self.assertIn("each purchase order&#39;s costs", html)

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


class OrderPageTests(unittest.TestCase):
    """Sales and purchase orders made, changed, completed and received from Core, written to
    (the mock) Unleashed and kept in the shared copy."""

    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock", TODAY=TODAY)
        self.c = self.app.test_client()
        ext = self.app.extensions["unleashed"]
        self.ds = ext["client"].transport.app.config["DATASET"]
        self.reports = ext["reports"]

    def _post(self, path, **data):
        res = self.c.post(path, data=data)
        self.assertIn(res.status_code, (302, 303), res.get_data(as_text=True)[-3000:])
        return res

    def test_a_sales_order_is_made_changed_and_completed_with_its_batches(self):
        form = self.c.get("/sales/new").get_data(as_text=True)
        for text in ("Mock Wholesale Cafe", '<option value="COF-ESP-5LB">COF-ESP-5LB · Espresso Blend 5 lb</option>',
                     '"code": "COF-ESP-5LB"', '"price": 160.0', 'list="order-products"', "+ Add a product"):
            self.assertIn(text, form)
        self.assertIn("Choose the customer", self.c.post("/sales/new", data={"product": "MILK-WHL-GAL", "qty": "1",
                                                                            "price": "1"}).get_data(as_text=True))
        res = self._post("/sales/new", customer="WHSL-01", warehouse="GDN", order_date=TODAY.isoformat(),
                         reference="PO 55", comments="Back door", line_guid=["", "", ""],
                         product=["MILK-WHL-GAL", "CRM-HVY-HG", ""], qty=["2", "3", ""], price=["120", "28", ""])
        number = res.headers["Location"].rsplit("/", 1)[1]
        page = self.c.get(f"/sales/{number}").get_data(as_text=True)
        for text in ("Complete…", "Edit", "Delete", "PO 55", "Back door", "Parked"):
            self.assertIn(text, page)
        so = self.reports.sales_order(number)  # in the shared copy straight away
        self.assertEqual((so["subtotal"], so["tax"], so["total"]), (324.0, 30.78, 354.78))  # 9.5% sales tax

        # Changing it: a quantity and price, a line added, a line removed.
        espresso = next(l for l in so["lines"] if l["product_code"] == "MILK-WHL-GAL")  # the milk
        cream = next(l for l in so["lines"] if l["product_code"] == "CRM-HVY-HG")
        edit = self.c.get(f"/sales/{number}/edit").get_data(as_text=True)
        self.assertIn('name="product" class="line-product" list="order-products" value="MILK-WHL-GAL"', edit)
        self.assertIn("Whole Milk 1 gal</div>", edit)  # its name under the code
        self._post(f"/sales/{number}/edit", warehouse="GDN", order_date=TODAY.isoformat(), reference="PO 56",
                   comments="", line_guid=[espresso["guid"], cream["guid"], ""],
                   product=["MILK-WHL-GAL", "CRM-HVY-HG", "MILK-OAT-QT"], qty=["3", "3", "4"], price=["115", "28", "9.5"])
        so = self.reports.sales_order(number)
        self.assertEqual(sorted((l["product_code"], l["qty"], l["unit_price"]) for l in so["lines"]),
                         sorted([("MILK-WHL-GAL", 3, 115), ("CRM-HVY-HG", 3, 28), ("MILK-OAT-QT", 4, 9.5)]))
        self.assertEqual(so["reference"], "PO 56")
        oat = next(l for l in so["lines"] if l["product_code"] == "MILK-OAT-QT")
        self._post(f"/sales/{number}/edit", warehouse="GDN", order_date=TODAY.isoformat(),
                   line_guid=[espresso["guid"], cream["guid"], oat["guid"]],
                   product=["MILK-WHL-GAL", "CRM-HVY-HG", ""], qty=["3", "3", ""], price=["115", "28", ""])
        self.assertEqual(len(self.reports.sales_order(number)["lines"]), 2)

        # Completing: the batch-tracked cream's batches, first expiring first, then the stock goes out.
        page = self.c.get(f"/sales/{number}/complete").get_data(as_text=True)
        self.assertIn("Batches of CRM-HVY-HG", page)
        self.assertIn('value="CRM-HVY-HG-A"', page)
        bad = self.c.post(f"/sales/{number}/complete", data={f"batch-{cream['guid']}": "CRM-HVY-HG-A",
                                                              f"batch-qty-{cream['guid']}": "2"})
        self.assertIn("add up to 2, not the 3", bad.get_data(as_text=True))
        before = self.ds.stock_as_at("MILK-WHL-GAL")[0]
        self._post(f"/sales/{number}/complete", **{f"batch-{cream['guid']}": "CRM-HVY-HG-A",
                                                   f"batch-qty-{cream['guid']}": "3"})
        so = self.reports.sales_order(number)
        self.assertEqual(so["status"], "Completed")
        self.assertEqual(self.ds.stock_as_at("MILK-WHL-GAL")[0], before - 3)
        self.assertEqual(next(b for b in self.ds.batches if b["Number"] == "CRM-HVY-HG-A")["Quantity"],
                         self.ds.batches[0]["OriginalQty"] - 3)
        page = self.c.get(f"/sales/{number}").get_data(as_text=True)
        self.assertNotIn("Complete…", page)
        self.assertIn("Batch CRM-HVY-HG-A (3)", page)
        self.assertIn("can&#39;t be changed", self.c.get(f"/sales/{number}/edit", follow_redirects=True).get_data(as_text=True))

    def test_not_explained_is_checked_day_by_day_and_adds_up_to_the_month(self):
        cogs = self.reports.cogs(TODAY)
        daily = self.reports.daily_unexplained(cogs)
        self.assertEqual([d["date"] for d in daily["days"]], [TODAY.replace(day=d) for d in range(1, TODAY.day + 1)])
        self.assertAlmostEqual(daily["total"], self.reports.cogs_by_store(cogs)["rest"], places=4)
        self.assertTrue(all(abs(d["value"]) < 0.5 for d in daily["days"]))  # the mock's documents explain it all
        # Two gallons of milk go missing today, on no document: today's row shows it, the others don't move.
        avg = self.ds.average_cost("MILK-WHL-GAL")
        self.ds.move_stock(TODAY, "Unrecorded", "-", "-", "MILK-WHL-GAL", -2, -2 * avg)
        self.app.extensions["unleashed"]["syncer"].sync()
        cogs = self.reports.cogs(TODAY)
        daily = self.reports.daily_unexplained(cogs)
        self.assertAlmostEqual(daily["days"][-1]["value"], 2 * avg, places=2)
        self.assertEqual(daily["days"][-1]["products"][0]["product_code"], "MILK-WHL-GAL")
        self.assertTrue(all(abs(d["value"]) < 0.5 for d in daily["days"][:-1]))
        self.assertAlmostEqual(daily["total"], self.reports.cogs_by_store(cogs)["rest"], places=4)
        page = self.c.get("/cogs/not-on-an-order").get_data(as_text=True)
        for text in ('id="daily"', "✓ matches", "MILK-WHL-GAL", 'class="row-warn"' if 2 * avg >= 25 else "Day by day"):
            self.assertIn(text, page)
        csv_text = self.c.get("/cogs/not-on-an-order?format=csv&by=daily").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("Date,Stock at end of day,Change,Documents explain,Not explained"))
        self.assertIn("Day by day →", self.c.get("/cogs").get_data(as_text=True))
        older = self.reports.cogs(add_months(TODAY.replace(day=1), -3))
        self.assertIsNone(self.reports.daily_unexplained(older))
        self.assertIn("kept for this month and last month",
                      self.c.get(f"/cogs/not-on-an-order?month={older['month']:%Y-%m}").get_data(as_text=True))

    def test_products_are_typed_and_the_next_number_is_shown(self):
        last_so = max(int(n[3:]) for n in self.app.extensions["unleashed"]["store"].document_numbers("SalesOrders")
                      if n.startswith("SO-"))
        form = self.c.get("/sales/new").get_data(as_text=True)
        self.assertIn(f"SO-{last_so + 1:08d}", form)
        # Codes typed in any case are matched; something that isn't a product is refused.
        res = self._post("/sales/new", customer="GDN-POS", warehouse="GDN", order_date=TODAY.isoformat(),
                         line_guid=[""], product=["milk-whl-gal"], qty=["1"], price=["4.2"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(number, f"SO-{last_so + 1:08d}")  # what the form said
        self.assertEqual(self.reports.sales_order(number)["lines"][0]["product_code"], "MILK-WHL-GAL")
        bad = self.c.post("/sales/new", data={"customer": "GDN-POS", "warehouse": "GDN", "order_date": TODAY.isoformat(),
                                              "line_guid": [""], "product": ["Oat milk please"], "qty": ["1"], "price": ["1"]})
        self.assertIn("Oat milk please: not a product in Unleashed", bad.get_data(as_text=True))
        self.assertRegex(self.c.get("/purchases/new").get_data(as_text=True),
                         r'New purchase order <span class="order-number"[^>]*>PO-\d+</span>')

    def test_freight_and_fuel_surcharge_go_on_a_purchase_order_as_charges(self):
        form = self.c.get("/purchases/new").get_data(as_text=True)
        self.assertIn('<option value="FUEL-SURCHARGE" >Fuel surcharge</option>', form)
        self.assertNotIn('<option value="FREIGHT">FREIGHT', form)  # charges aren't offered as products
        res = self._post("/purchases/new", supplier="SUP-DAIRY", warehouse="GDN", order_date=TODAY.isoformat(), place="1",
                         line_guid=[""], product=["MILK-WHL-GAL"], qty=["10"], price=["4.2"],
                         charge_guid=["", ""], charge=["FREIGHT", "FUEL-SURCHARGE"], charge_amount=["18", "3.75"],
                         charge_note=["", "Fuel surcharge Nov"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        po = self.reports.purchase_order(number)
        self.assertEqual((po["goods"], po["shipping"]), (42.0, 21.75))  # charges count with shipping, not stock
        self.assertEqual(sorted(c["description"] for c in po["cost_lines"]), ["Freight", "Fuel surcharge Nov"])
        self.assertIn("Fuel surcharge Nov", self.c.get(f"/purchases/{number}").get_data(as_text=True))
        # Editing keeps them (and can change them); receiving takes them with the goods.
        self.assertIn('value="Fuel surcharge Nov"', self.c.get(f"/purchases/{number}/edit").get_data(as_text=True))
        fuel = next(c for c in po["cost_lines"] if c["product_code"] == "FUEL-SURCHARGE")
        freight = next(c for c in po["cost_lines"] if c["product_code"] == "FREIGHT")
        milk = po["lines"][0]
        self._post(f"/purchases/{number}/edit", warehouse="GDN", order_date=TODAY.isoformat(),
                   line_guid=[milk["guid"]], product=["MILK-WHL-GAL"], qty=["10"], price=["4.2"],
                   charge_guid=[freight["guid"], fuel["guid"]], charge=["FREIGHT", "FUEL-SURCHARGE"],
                   charge_amount=["18", "4.10"], charge_note=["", "Fuel surcharge Nov"])
        self.assertAlmostEqual(self.reports.purchase_order(number)["shipping"], 22.1)
        before = self.ds.stock_as_at("MILK-WHL-GAL")[0]
        self._post(f"/purchases/{number}/receive", complete="1", **{f"qty-{milk['guid']}": "10"})
        self.assertEqual(self.reports.purchase_order(number)["status"], "Complete")
        self.assertEqual(self.ds.stock_as_at("MILK-WHL-GAL")[0], before + 10)
        self.assertNotIn("FUEL-SURCHARGE", [r["product_code"] for r in self.reports.stock_on_hand()["rows"]])
        # Products chosen in Settings → COGS count as charges too.
        self.c.post("/settings/cogs", data={"charges_shown": "1", "charge": ["FREIGHT", "FUEL-SURCHARGE", "JUG-GAL"],
                                            "stock_as_at": "end"})
        self.assertIn("JUG-GAL", self.reports.charge_codes())

    def test_a_parked_sales_order_is_deleted(self):
        res = self._post("/sales/new", customer="GDN-POS", warehouse="GDN", order_date=TODAY.isoformat(),
                         line_guid=[""], product=["MILK-WHL-GAL"], qty=["5"], price=["4.2"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        so = self.reports.sales_order(number)
        self.assertEqual(so["tax"], 0)  # the store isn't charged tax
        self._post(f"/sales/{number}/delete")
        self.assertNotIn(number, [o["number"] for o in self.reports.sales_enquiry(TODAY, TODAY)["orders"]])

    def test_a_purchase_order_is_made_and_received_with_its_batches(self):
        self.assertIn("Mock Dairy Distributors", self.c.get("/purchases/new").get_data(as_text=True))
        res = self._post("/purchases/new", supplier="SUP-DAIRY", warehouse="GDN", order_date=TODAY.isoformat(),
                         delivery_date=(TODAY + dt.timedelta(days=1)).isoformat(), reference="INV-9", place="1",
                         line_guid=["", ""], product=["MILK-WHL-GAL", "CRM-HVY-HG"], qty=["10", "6"], price=["4.2", "9.5"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        po = self.reports.purchase_order(number)
        self.assertEqual((po["status"], po["goods"]), ("Placed", 99.0))
        page = self.c.get(f"/purchases/{number}/receive").get_data(as_text=True)
        self.assertIn("Tracked by batch", page)
        milk = next(l for l in po["lines"] if l["product_code"] == "MILK-WHL-GAL")
        cream = next(l for l in po["lines"] if l["product_code"] == "CRM-HVY-HG")
        self.assertIn("enter the batch number", self.c.post(f"/purchases/{number}/receive", data={
            f"qty-{milk['guid']}": "10", f"qty-{cream['guid']}": "6", "complete": "1"}).get_data(as_text=True))
        self.assertIn("only when everything ordered arrived", self.c.post(f"/purchases/{number}/receive", data={
            f"qty-{milk['guid']}": "8", f"qty-{cream['guid']}": "6", f"batch-{cream['guid']}": "CR-1",
            f"batch-qty-{cream['guid']}": "6", "complete": "1"}).get_data(as_text=True))
        before = self.ds.stock_as_at("MILK-WHL-GAL")[0]
        self._post(f"/purchases/{number}/receive", **{
            f"qty-{milk['guid']}": "10", f"qty-{cream['guid']}": "6", f"batch-{cream['guid']}": "CR-1",
            f"batch-qty-{cream['guid']}": "6", f"batch-expiry-{cream['guid']}": (TODAY + dt.timedelta(days=9)).isoformat(),
            "complete": "1"})
        po = self.reports.purchase_order(number)
        self.assertEqual(po["status"], "Complete")
        self.assertEqual(self.ds.stock_as_at("MILK-WHL-GAL")[0], before + 10)
        batch = next(b for b in self.ds.batches if b["Number"] == "CR-1")
        self.assertEqual((batch["Quantity"], batch["ExpiryDate"]), (6, TODAY + dt.timedelta(days=9)))

    def test_a_purchase_order_received_in_part_is_completed_later(self):
        res = self._post("/purchases/new", supplier="SUP-ROAST", warehouse="GDN", order_date=TODAY.isoformat(),
                         line_guid=[""], product=["COF-ESP-5LB"], qty=["4"], price=["42"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(self.reports.purchase_order(number)["status"], "Parked")  # not placed
        line = self.reports.purchase_order(number)["lines"][0]
        self._post(f"/purchases/{number}/receive", **{f"qty-{line['guid']}": "3"})
        self.assertEqual(self.reports.purchase_order(number)["status"], "Receipted")
        self.assertIn("Complete</button>", self.c.get(f"/purchases/{number}").get_data(as_text=True))
        self._post(f"/purchases/{number}/complete")
        self.assertEqual(self.reports.purchase_order(number)["status"], "Complete")


    def test_a_customer_return_goes_back_into_stock_and_off_cogs(self):
        res = self._post("/sales/new", customer="GDN-POS", warehouse="GDN", order_date=TODAY.isoformat(),
                         line_guid=["", ""], product=["MILK-WHL-GAL", "CRM-HVY-HG"], qty=["3", "2"], price=["13.5", "28"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        so = self.reports.sales_order(number)
        espresso = next(l for l in so["lines"] if l["product_code"] == "MILK-WHL-GAL")
        cream = next(l for l in so["lines"] if l["product_code"] == "CRM-HVY-HG")
        self._post(f"/sales/{number}/complete", **{f"batch-{cream['guid']}": "CRM-HVY-HG-A",
                                                   f"batch-qty-{cream['guid']}": "2"})
        self.assertIn("Credit / return…", self.c.get(f"/sales/{number}").get_data(as_text=True))
        form = self.c.get(f"/sales/{number}/credit").get_data(as_text=True)
        self.assertIn('value="CRM-HVY-HG-A"', form)  # the batch it went out as
        self.assertIn("credit between 0 and the 3", self.c.post(f"/sales/{number}/credit", data={
            f"qty-{espresso['guid']}": "5", "complete": "1"}).get_data(as_text=True))
        before = (self.ds.stock_as_at("MILK-WHL-GAL")[0], self.ds.stock_as_at("CRM-HVY-HG")[0])
        res = self._post(f"/sales/{number}/credit", date=TODAY.isoformat(), reference=number, complete="1", **{
            f"qty-{espresso['guid']}": "1", f"price-{espresso['guid']}": "13.5", f"reason-{espresso['guid']}": "Wrong grind",
            f"back-{espresso['guid']}": "1",
            f"qty-{cream['guid']}": "2", f"price-{cream['guid']}": "28", f"reason-{cream['guid']}": "Spoiled"})
        credit = res.headers["Location"].rsplit("/", 1)[1]
        c = self.reports.credit_note(credit)
        self.assertEqual((c["status"], c["reference"], c["subtotal"]), ("Completed", number, 69.5))
        # The milk went back on the shelf; the spoiled cream was credited only.
        self.assertEqual(self.ds.stock_as_at("MILK-WHL-GAL")[0], before[0] + 1)
        self.assertEqual(self.ds.stock_as_at("CRM-HVY-HG")[0], before[1])
        page = self.c.get(f"/sales/credits/{credit}").get_data(as_text=True)
        for text in ("Wrong grind", "Spoiled", "Completed"):
            self.assertIn(text, page)
        self.assertIn(credit, self.c.get("/sales/credits").get_data(as_text=True))
        # COGS: what came back isn't cost of sales, here or in the store's share.
        recon = self.reports.cogs_reconciliation(TODAY)
        self.assertAlmostEqual(recon["returns_in"], c["cost"])
        self.assertGreater(c["cost"], 0)
        cogs = self.reports.cogs(TODAY)
        self.assertAlmostEqual(cogs["cogs"], recon["total"], delta=1.0)
        self.assertIn("returned by customers", self.c.get("/cogs").get_data(as_text=True))

    def test_a_parked_credit_is_completed_or_deleted(self):
        so = next(o for o in self.reports.sales_enquiry(TODAY - dt.timedelta(days=3), TODAY, status="Completed")["orders"])
        line = so["lines"][0]
        res = self._post(f"/sales/{so['number']}/credit", date=TODAY.isoformat(), **{
            f"qty-{line['guid']}": "1", f"price-{line['guid']}": "1", f"reason-{line['guid']}": "Goodwill"})
        credit = res.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(self.reports.credit_note(credit)["status"], "Parked")
        self._post(f"/sales/credits/{credit}/delete")
        self.assertNotIn(f">{credit}</a>", self.c.get("/sales/credits").get_data(as_text=True))

    def test_stock_sent_back_to_a_supplier_comes_off_purchases(self):
        res = self._post("/purchases/new", supplier="SUP-DAIRY", warehouse="GDN", order_date=TODAY.isoformat(), place="1",
                         line_guid=["", ""], product=["MILK-WHL-GAL", "CRM-HVY-HG"], qty=["10", "4"], price=["4.2", "9.5"])
        number = res.headers["Location"].rsplit("/", 1)[1]
        po = self.reports.purchase_order(number)
        milk = next(l for l in po["lines"] if l["product_code"] == "MILK-WHL-GAL")
        cream = next(l for l in po["lines"] if l["product_code"] == "CRM-HVY-HG")
        self._post(f"/purchases/{number}/receive", complete="1", **{
            f"qty-{milk['guid']}": "10", f"qty-{cream['guid']}": "4", f"batch-{cream['guid']}": "CR-9",
            f"batch-qty-{cream['guid']}": "4"})
        self.assertIn("Return to supplier…", self.c.get(f"/purchases/{number}").get_data(as_text=True))
        form = self.c.get(f"/purchases/{number}/return").get_data(as_text=True)
        self.assertIn("<option>Damaged</option>", form)
        self.assertIn("tracked by batch: choose the batch", self.c.post(f"/purchases/{number}/return", data={
            "qty-CRM-HVY-HG-0": "1", "complete": "1"}).get_data(as_text=True))
        before = (self.ds.stock_as_at("MILK-WHL-GAL")[0], self.ds.stock_as_at("CRM-HVY-HG")[0])
        res = self._post(f"/purchases/{number}/return", date=TODAY.isoformat(), reference="RMA-1", complete="1", **{
            "qty-MILK-WHL-GAL-0": "2", "price-MILK-WHL-GAL-0": "4.2", "reason-MILK-WHL-GAL-0": "Damaged",
            "qty-CRM-HVY-HG-0": "1", "price-CRM-HVY-HG-0": "9.5", "reason-CRM-HVY-HG-0": "Expired",
            "batch-CRM-HVY-HG-0": "CR-9", "batch-qty-CRM-HVY-HG-0": "1"})
        ret = res.headers["Location"].rsplit("/", 1)[1]
        sr = self.reports.supplier_return(ret)
        self.assertEqual((sr["status"], sr["purchase_order"], [l["product_code"] for l in sr["lines"]]),
                         ("Completed", number, ["MILK-WHL-GAL", "CRM-HVY-HG"]))  # only what went back
        self.assertEqual(self.ds.stock_as_at("MILK-WHL-GAL")[0], before[0] - 2)
        self.assertEqual(next(b for b in self.ds.batches if b["Number"] == "CR-9")["Quantity"], 3)
        self.assertIn("RMA-1", self.c.get(f"/purchases/returns/{ret}").get_data(as_text=True))
        self.assertIn(ret, self.c.get("/purchases/returns").get_data(as_text=True))
        cogs = self.reports.cogs(TODAY)
        self.assertAlmostEqual(cogs["returned"], sr["value"])
        self.assertAlmostEqual(cogs["purchases"], cogs["goods"] + cogs["shipping"] - sr["value"])
        self.assertAlmostEqual(cogs["cogs"], self.reports.cogs_reconciliation(TODAY)["total"], delta=1.0)
        self.assertIn("sent back to suppliers", self.c.get("/cogs").get_data(as_text=True))
        self.assertIn(ret, self.c.get("/cogs/not-on-an-order").get_data(as_text=True))


class StockTransactionTests(unittest.TestCase):
    """Stock adjustments, warehouse transfers and stock counts made from Core's Transactions tab,
    written to (the mock) Unleashed and kept in the shared copy."""

    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock", TODAY=TODAY)
        self.c = self.app.test_client()
        ext = self.app.extensions["unleashed"]
        self.ds = ext["client"].transport.app.config["DATASET"]
        self.reports = ext["reports"]

    def _post(self, path, **data):
        res = self.c.post(path, data=data)
        self.assertIn(res.status_code, (302, 303), res.get_data(as_text=True)[-3000:])
        return res

    def _qty(self, code, warehouse=None):
        return self.ds.stock_in(code, warehouse)[0] if warehouse else self.ds.stock_qty(code)

    def test_the_transactions_page_shows_two_days_and_what_to_make(self):
        html = self.c.get("/transactions").get_data(as_text=True)
        for text in ("+ Adjustment", "+ Stock count", "+ Transfer", "Earlier, and filters",
                     f"{(TODAY - dt.timedelta(days=1)):%b} {(TODAY - dt.timedelta(days=1)).day}, {TODAY.year} to"):
            self.assertIn(text, html)
        days = {d for d in re.findall(r'<td class="nowrap">([^<]+)</td>', html)}
        self.assertTrue(days)
        self.assertLessEqual(len(days), 2)
        history = self.c.get("/transactions/history?type=Transfer").get_data(as_text=True)
        self.assertIn("<option selected>Transfer</option>", history)

    def test_an_adjustment_moves_stock_at_average_cost_and_is_listed(self):
        form = self.c.get("/adjustments/new").get_data(as_text=True)
        for text in ('list="stock-products"', '"code": "MILK-WHL-GAL"', "<option>Damaged</option>", "Commissary",
                     "made completed"):
            self.assertIn(text, form)
        self.assertNotIn('"code": "FREIGHT"', form)  # charges hold no stock
        before, avg = self._qty("MILK-WHL-GAL"), self.ds.average_cost("MILK-WHL-GAL")
        bad = self.c.post("/adjustments/new", data={"warehouse": "GDN", "reason": "Damaged", "product": ["nope"],
                                                    "change": ["-1"], "new_qty": [""], "unit_cost": [""], "line_comment": [""]})
        self.assertIn("nope: not a product that holds stock", bad.get_data(as_text=True))
        below = self.c.post("/adjustments/new", data={"warehouse": "GDN", "reason": "Damaged", "product": ["MILK-WHL-GAL"],
                                                      "change": [str(-before - 5)], "new_qty": [""], "unit_cost": [""],
                                                      "line_comment": [""]})
        self.assertIn("be less than 0", below.get_data(as_text=True))
        res = self._post("/adjustments/new", warehouse="GDN", reason="Damaged", product=["milk-whl-gal", ""],
                         change=["-2", ""], new_qty=["", ""], unit_cost=["", ""], line_comment=["Dropped two", ""])
        number = res.headers["Location"].rsplit("/", 1)[1]
        self.assertAlmostEqual(self._qty("MILK-WHL-GAL"), before - 2)
        sa = next(a for a in self.ds.stock_adjustments if a["AdjustmentNumber"] == number)
        self.assertEqual((sa["Status"], sa["AdjustmentReason"], sa["WarehouseCode"]), ("Completed", "Damaged", "GDN"))
        self.assertAlmostEqual(sa["Lines"][0]["ValueChange"], -2 * avg, places=2)
        page = self.c.get(f"/adjustments/{number}").get_data(as_text=True)
        for text in ("Dropped two", "Damaged", "Gardena Store", "Completed", "make another"):
            self.assertIn(text, page)
        self.assertIn(f'>{number}</a>', self.c.get("/transactions").get_data(as_text=True))
        self.assertIn(f'>{number}</a>', self.c.get("/adjustments").get_data(as_text=True))
        # Found stock goes in at the unit cost given.
        self._post("/adjustments/new", warehouse="GDN", reason="Found", product=["MILK-OAT-QT"], change=[""],
                   new_qty=[str(self._qty("MILK-OAT-QT") + 4)], unit_cost=["2.50"], line_comment=[""])
        found = self.ds.stock_adjustments[-1]["Lines"][0]
        self.assertEqual((found["QuantityChange"], round(found["ValueChange"], 2)), (4.0, 10.0))

    def test_a_batch_tracked_adjustment_says_which_batches(self):
        batches = {b["Number"]: b["Quantity"] for b in self.ds.batches
                   if b["ProductCode"] == "VSC-HG" and b["WarehouseCode"] == "GDN" and b["Quantity"] > 0}
        self.assertTrue(batches)
        first, have = sorted(batches.items())[0]
        ask = self.c.post("/adjustments/new", data={"warehouse": "GDN", "reason": "Expired", "product": ["VSC-HG"],
                                                    "change": ["-1"], "new_qty": [""], "unit_cost": [""], "line_comment": [""]})
        html = ask.get_data(as_text=True)
        self.assertIn("tracked by batch", html)
        self.assertIn(f'name="batch-number-VSC-HG" value="{first}"', html)
        rows = sorted(batches.items())
        data = {"warehouse": "GDN", "reason": "Expired", "product": ["VSC-HG"], "change": ["-1"], "new_qty": [""],
                "unit_cost": [""], "line_comment": [""], "batches-shown-VSC-HG": "1",
                "batch-number-VSC-HG": [n for n, _ in rows] + ["NEW-1"],
                "batch-qty-VSC-HG": ["0" if n == first else f"{q:g}" for n, q in rows] + ["2"],
                "batch-expiry-VSC-HG": ["" for _ in rows] + ["2026-08-01"]}
        res = self._post("/adjustments/new", **data)
        self.assertIn("/adjustments/SA-", res.headers["Location"])
        left = {b["Number"]: b["Quantity"] for b in self.ds.batches if b["ProductCode"] == "VSC-HG" and b["WarehouseCode"] == "GDN"}
        self.assertEqual((left[first], left["NEW-1"]), (0.0, 2.0))
        line = self.ds.stock_adjustments[-1]["Lines"][0]
        self.assertAlmostEqual(line["NewQuantity"], sum(batches.values()) - have + 2)

    def test_a_transfer_is_parked_changed_and_completed_between_warehouses(self):
        form = self.c.get("/transfers/new").get_data(as_text=True)
        self.assertIn('<option value="COM" selected>Commissary</option>', form)  # to the other warehouse
        same = self.c.post("/transfers/new", data={"source": "GDN", "destination": "GDN", "product": ["MILK-WHL-GAL"],
                                                   "qty": ["1"], "line_guid": [""], "line_comment": [""], "then": "park"})
        self.assertIn("different warehouse", same.get_data(as_text=True))
        too_much = self.c.post("/transfers/new", data={"source": "GDN", "destination": "COM", "product": ["MILK-WHL-GAL"],
                                                       "qty": ["99999"], "line_guid": [""], "line_comment": [""],
                                                       "then": "complete"})
        self.assertIn("enough of MILK-WHL-GAL (", too_much.get_data(as_text=True))
        self.assertEqual(self.ds.transfers, [])  # nothing made
        res = self._post("/transfers/new", source="GDN", destination="COM", product=["MILK-WHL-GAL", "MILK-OAT-QT"],
                         qty=["4", "2"], line_guid=["", ""], line_comment=["For the kitchen", ""], comments="Monday run",
                         then="park")
        number = res.headers["Location"].rsplit("/", 1)[1]
        page = self.c.get(f"/transfers/{number}").get_data(as_text=True)
        for text in ("Parked", "Gardena Store", "Commissary", "For the kitchen", "Monday run", "Complete: move the stock"):
            self.assertIn(text, page)
        self.assertIn(f">{number}</a>", self.c.get("/transactions").get_data(as_text=True))  # waiting to finish
        self.assertEqual(self._qty("MILK-WHL-GAL", "COM"), 0.0)  # parked moves nothing
        edit = self.c.get(f"/transfers/{number}/edit").get_data(as_text=True)
        guids = re.findall(r'name="line_guid" value="([^"]+)"', edit)
        self.assertEqual(len(guids), 2)
        total, gdn = self._qty("MILK-WHL-GAL"), self._qty("MILK-WHL-GAL", "GDN")
        blank = self.c.post(f"/transfers/{number}/edit", data={"source": "GDN", "destination": "COM",
                                                              "product": ["MILK-WHL-GAL", "CRM-HVY-HG"], "qty": ["5", ""],
                                                              "line_guid": [guids[0], ""], "line_comment": ["", ""],
                                                              "comments": "", "then": "save"})
        self.assertIn("Enter how much CRM-HVY-HG to move", blank.get_data(as_text=True))
        # The oat milk line removed, the milk changed, and completed in one go.
        self._post(f"/transfers/{number}/edit", source="GDN", destination="COM", product=["MILK-WHL-GAL"],
                   qty=["5"], line_guid=[guids[0]], line_comment=[""], comments="", then="complete")
        t = self.ds.transfers[0]
        self.assertEqual((t["TransferStatus"], [(l["ProductCode"], l["Quantity"]) for l in t["Lines"]]),
                         ("Completed", [("MILK-WHL-GAL", 5.0)]))
        self.assertEqual(self._qty("MILK-WHL-GAL", "COM"), 5.0)
        self.assertAlmostEqual(self._qty("MILK-WHL-GAL", "GDN"), gdn - 5)
        self.assertAlmostEqual(self._qty("MILK-WHL-GAL"), total)  # only moved
        recent = self.c.get("/transactions").get_data(as_text=True)
        self.assertEqual(recent.count("Gardena Store → Commissary"), 2)  # out of one, into the other
        stored = self.app.extensions["unleashed"]["store"].query_documents("WarehouseStockTransfers")
        self.assertEqual([d["TransferStatus"] for d in stored], ["Completed"])
        # COGS of every warehouse doesn't change; a COGS of the Commissary alone counts it as bought.
        com = self.reports.cogs(TODAY, warehouses=["COM"])
        self.assertAlmostEqual(com["transferred"], 5 * t["Lines"][0]["Cost"], places=2)
        self.assertAlmostEqual(com["cogs"], com["opening"] + com["purchases"] - com["closing"], places=6)
        self.assertEqual(self.reports.cogs(TODAY)["transferred"], 0.0)
        self.assertIn("Completed", self.c.get(f"/transfers/{number}").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/transfers/{number}/delete").status_code, 302)
        self.assertEqual(self.ds.transfers[0]["TransferStatus"], "Completed")  # a completed one stays

    def test_a_parked_transfer_is_deleted(self):
        res = self._post("/transfers/new", source="GDN", destination="COM", product=["MILK-WHL-GAL"], qty=["1"],
                         line_guid=[""], line_comment=[""], then="place")
        number = res.headers["Location"].rsplit("/", 1)[1]
        self.assertIn("Placed", self.c.get(f"/transfers/{number}").get_data(as_text=True))
        self._post(f"/transfers/{number}/delete")
        self.assertEqual(self.ds.transfers[0]["TransferStatus"], "Deleted")
        self.assertEqual(self.c.get(f"/transfers/{number}").status_code, 404)

    def test_a_stock_count_posts_its_differences_as_one_adjustment(self):
        counts = self.app.extensions["stock_counts"]
        new = self.c.get("/stock-counts/new").get_data(as_text=True)
        self.assertIn("<option>Dairy</option>", new)
        res = self._post("/stock-counts/new", warehouse="GDN", group="Dairy", include_zero="1", name="Cooler")
        count_id = int(res.headers["Location"].rsplit("/", 1)[1])
        c = counts.count(count_id)
        codes = {l["product_code"] for l in c["lines"]}
        self.assertIn("MILK-WHL-GAL", codes)
        self.assertNotIn("COF-ESP-5LB", codes)  # another group
        crm = [l for l in c["lines"] if l["product_code"] == "CRM-HVY-HG"]
        self.assertTrue(crm and all(l["batch_number"] for l in crm))  # a line per batch
        milk = next(l for l in c["lines"] if l["product_code"] == "MILK-WHL-GAL")
        oat = next(l for l in c["lines"] if l["product_code"] == "MILK-OAT-QT")
        page = self.c.get(f"/stock-counts/{count_id}").get_data(as_text=True)
        self.assertIn(f'name="counted-{milk["id"]}"', page)
        self.assertIn("Hide what Unleashed says", page)
        self.assertNotIn(f"{milk['expected_qty']:g}</td>", self.c.get(f"/stock-counts/{count_id}?blind=1").get_data(as_text=True)
                         .split("<tbody>")[1].split(milk["product_code"])[1].split("</tr>")[0])
        # Two people save at once: each only changes the lines they typed in.
        self._post(f"/stock-counts/{count_id}", **{f"counted-{milk['id']}": f"{milk['expected_qty'] - 3:g}", f"orig-{milk['id']}": "",
                                                   f"counted-{oat['id']}": "", f"orig-{oat['id']}": ""})
        self._post(f"/stock-counts/{count_id}", **{f"counted-{milk['id']}": "", f"orig-{milk['id']}": "",
                                                   f"counted-{oat['id']}": f"{oat['expected_qty']:g}", f"orig-{oat['id']}": ""})
        c = counts.count(count_id)
        self.assertEqual(c["counted"], 2)
        self.assertEqual([l["product_code"] for l in c["differences"]], ["MILK-WHL-GAL"])
        # Stock sold while counting stands: the difference moves the stock there now.
        self.ds.move_stock(TODAY, "Sale", "SO-X", "X", "MILK-WHL-GAL", -1, -1 * self.ds.average_cost("MILK-WHL-GAL"))
        before = self._qty("MILK-WHL-GAL", "GDN")
        self._post(f"/stock-counts/{count_id}/post", reason="Stock count")
        c = counts.count(count_id)
        self.assertEqual(c["status"], "posted")
        sa = self.ds.stock_adjustments[-1]
        self.assertEqual((c["adjustment_number"], sa["AdjustmentReason"], len(sa["Lines"])),
                         (sa["AdjustmentNumber"], "Stock count", 1))
        self.assertAlmostEqual(self._qty("MILK-WHL-GAL", "GDN"), before - 3)
        page = self.c.get(f"/stock-counts/{count_id}").get_data(as_text=True)
        self.assertIn(f'>{sa["AdjustmentNumber"]}</a>', page)
        self.assertNotIn('name="counted-', page)  # done: read only
        again = self.c.post(f"/stock-counts/{count_id}/post", data={"reason": "Stock count"}, follow_redirects=True)
        self.assertIn("This count is posted", again.get_data(as_text=True))
        self.assertEqual(self.ds.stock_adjustments[-1]["AdjustmentNumber"], sa["AdjustmentNumber"])

    def test_a_count_that_matches_needs_no_adjustment(self):
        res = self._post("/stock-counts/new", warehouse="GDN", group="Syrups & Sauces", include_zero="", name="")
        count_id = int(res.headers["Location"].rsplit("/", 1)[1])
        c = self.app.extensions["stock_counts"].count(count_id)
        line = c["lines"][0]
        self._post(f"/stock-counts/{count_id}", **{f"counted-{line['id']}": f"{line['expected_qty']:g}", f"orig-{line['id']}": ""})
        n = len(self.ds.stock_adjustments)
        done = self.c.post(f"/stock-counts/{count_id}/post", data={"reason": "Stock count"}, follow_redirects=True)
        self.assertIn("nothing to adjust", done.get_data(as_text=True))
        self.assertEqual(len(self.ds.stock_adjustments), n)
        self.assertEqual(self.app.extensions["stock_counts"].count(count_id)["status"], "posted")


class ProductionTests(unittest.TestCase):
    """Assemblies and bills of materials, read the way the Production pages read them."""

    @classmethod
    def setUpClass(cls):
        from unleashed.production import Production
        cls.mock = create_mock_app(today=TODAY)
        cls.ds = cls.mock.config["DATASET"]
        cls.reports = Reports(UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(cls.mock)),
                              today_fn=lambda: TODAY)
        cls.production = Production(cls.reports)

    def test_assemblies_move_value_between_products_without_changing_the_total(self):
        net = {}
        for m in self.ds.ledger:
            if m["Type"] == "Assembly":
                net[m["Number"]] = net.get(m["Number"], 0) + m["Value"]
        self.assertGreater(len(net), 100)
        self.assertLess(max(abs(v) for v in net.values()), 1e-6)

    def test_stock_movement_reconciles_for_made_products_and_their_components(self):
        for code in ("CB-CONC-GAL", "VSC-HG", "MATCHA-QT", "COF-ESP-5LB", "TEA-MATCHA-1KG", "JUG-GAL"):
            mv = self.reports.stock_movement(code, dt.date(2026, 4, 1), TODAY)
            self.assertAlmostEqual(mv["closing_qty"], mv["reported_qty"], places=6, msg=code)
            self.assertAlmostEqual(mv["closing_value"], mv["reported_value"], delta=0.05, msg=code)
            self.assertIn("Production", {r["type"] for r in mv["rows"]}, code)

    def test_order_variance_against_the_recipe(self):
        orders = [o for o in self.reports.assemblies(dt.date(2026, 5, 1), TODAY) if o["completed"]]
        details = [self.production.order(o["number"]) for o in orders[-40:]]
        exact = next(d for d in details if abs(d["material_variance"]) < 1e-6)  # made to the recipe
        self.assertTrue(all(abs(c["variance"]) < 1e-6 for c in exact["components"]))
        messy = next(d for d in details if d["material_variance"] > 0.001)  # extra wastage
        self.assertAlmostEqual(messy["material_variance"],
                               sum(c["variance"] * c["unit_cost"] for c in messy["components"]), places=6)
        self.assertAlmostEqual(messy["cost"], sum(c["cost"] for c in messy["components"]), places=6)
        self.assertIsNone(self.production.order("ASM-NOPE"))

    def test_requirements_take_stock_in_due_date_order(self):
        req = self.production.requirements()
        self.assertEqual([o["status"] for o in req["orders"]], ["Parked"] * 4)  # the deleted one stays out
        self.assertEqual(req["orders"][0]["qty"], 60)  # overdue, so first in line
        self.assertTrue(req["orders"][0]["overdue"])
        self.assertFalse(req["orders"][0]["ready"])
        matcha = next(c for c in req["components"] if c["product_code"] == "TEA-MATCHA-1KG")
        self.assertAlmostEqual(matcha["required"], 0.042 * (60 + 3), places=6)
        self.assertAlmostEqual(matcha["shortage"], max(matcha["required"] - matcha["available"], 0))
        self.assertEqual(req["components"][0]["product_code"], "TEA-MATCHA-1KG")  # short even after POs
        due_by = self.production.requirements(through=TODAY)
        self.assertEqual(len(due_by["orders"]), 1)

    def test_stock_set_aside_for_an_order_is_not_counted_against_it_twice(self):
        """Unleashed allocates a parked assembly's components, so its Available has already
        taken them out. Received cream covering the order mustn't still show it short."""
        from unleashed.production import Production
        first = Production(AllocationReports(allocated=204000 + 200000))  # Unleashed allocates both
        order = first.order("ASM-1")
        self.assertFalse(order["short"])
        self.assertEqual(order["components"][0]["available"], 358944)
        # The later order gets what's left: 358,944 - 204,000.
        later = first.order("ASM-2")["components"][0]
        self.assertEqual((later["available"], later["short"]), (154944, 45056))
        req = first.requirements()
        self.assertEqual([o["ready"] for o in req["orders"]], [True, False])
        cream = req["components"][0]
        self.assertEqual((cream["required"], cream["available"], cream["shortage"]), (404000, 358944, 45056))
        # Only orders due by the first's date: the later one's stock is still there for it.
        self.assertTrue(first.requirements(through=dt.date(2026, 10, 30))["orders"][0]["ready"])
        # Had Unleashed allocated nothing to assemblies, the answer is the same.
        unallocated = Production(AllocationReports(allocated=0)).requirements()["components"][0]
        self.assertEqual((unallocated["available"], unallocated["shortage"]), (358944, 45056))
        # What sales orders hold stays theirs.
        with_sales = Production(AllocationReports(allocated=404000 + 8944)).requirements()["components"][0]
        self.assertEqual((with_sales["available"], with_sales["shortage"]), (350000, 54000))

    def test_bill_of_materials_cost_and_what_stock_can_make(self):
        bom = self.production.bom("BOM-CB")
        stock = {r["product_code"]: r for r in self.reports.stock_on_hand()["rows"]}
        self.assertAlmostEqual(bom["unit_cost"], sum(l["per_unit"] * stock[l["product_code"]]["avg_cost"]
                                                     for l in bom["lines"]), places=6)
        # Available can be negative (sales orders allocate more than is on hand): makes nothing.
        self.assertEqual(bom["buildable"], int(min(max(stock[l["product_code"]]["available"], 0) / l["per_unit"]
                                                   for l in bom["lines"]) + 1e-9))
        used_by = [b["number"] for b in self.production.boms(component="SYR-VAN-750")]
        self.assertEqual(sorted(used_by), ["BOM-MATCHA", "BOM-VSC"])
        self.assertTrue(next(b for b in self.production.boms() if b["number"] == "BOM-CB-OLD")["obsolete"])

    def test_components_without_cost_are_estimated_at_average_cost(self):
        raw = next(a for a in self.reports.client.get_all("Assemblies")
                   if a["AssemblyStatus"] == "Completed")
        for line in raw["AssemblyLines"]:
            line.pop("UnitCost")
        costs = self.reports.unit_costs()
        order = self.reports._assembly(raw, costs)
        self.assertTrue(order["estimated"])
        line = order["lines"][0]
        self.assertAlmostEqual(line["cost"], line["consumed"] * costs[line["product_code"]], places=6)


class AllocationReports:
    """Heavy cream after a purchase is received: 358,944 on hand, and two parked orders
    needing 204,000 (due first) and 200,000. `allocated` is what Unleashed says it set aside."""
    today = dt.date(2026, 10, 28)

    def __init__(self, allocated):
        self.row = {"product_code": "30000049", "qty": 358944, "allocated": allocated,
                    "available": 358944 - allocated, "on_purchase": 0, "avg_cost": 0.007}
        line = {"product_code": "30000049", "description": "Heavy Cream 40% Clover", "qty": 0,
                "wastage": 0, "consumed": 0, "unit_cost": 0.007, "cost": 0, "estimated": True}
        self.orders = [
            {"guid": f"g{n}", "number": f"ASM-{n}", "status": "Parked", "open": True, "completed": False,
             "product_code": "SWEET-CREAM", "description": "Sweet cream", "qty": need / 1000,
             "date": self.today, "due": due, "completed_date": None, "cost": 0, "unit_cost": 0,
             "estimated": True, "from_warehouse": None, "to_warehouse": None, "comments": None,
             "lines": [dict(line, qty=need, consumed=need)]}
            for n, need, due in ((1, 204000, dt.date(2026, 10, 30)), (2, 200000, dt.date(2026, 11, 6)))]

    def stock_on_hand(self, **_):
        return {"rows": [self.row]}

    def bills_of_materials(self):
        return []

    def assemblies(self, *_, **__):
        return copy.deepcopy(self.orders)

    def assembly(self, number):
        return next((copy.deepcopy(o) for o in self.orders if o["number"] == number), None)


class ProductionRunTests(unittest.TestCase):
    """Recording what's used at each step, and closing the order out to Unleashed."""

    def setUp(self):
        self.mock = create_mock_app(today=TODAY)
        self.ds = self.mock.config["DATASET"]
        # Cold brew: espresso and a jug per gallon.
        self.asm = next(a for a in self.ds.assemblies
                        if a["Status"] == "Parked" and a["ProductCode"] == "CB-CONC-GAL")
        self.url = f"/production/orders/{self.asm['AssemblyNumber']}"

    def _app(self, transport=None):
        return make_app(self, UNLEASHED_MODE="live", UNLEASHED_API_ID=MOCK_API_ID,
                        UNLEASHED_API_KEY=MOCK_API_KEY, TODAY=TODAY,
                        UNLEASHED_TRANSPORT=transport or WsgiTransport(self.mock))

    def _record(self, c, espresso="1.2", jugs="7"):
        """Steps Steep (espresso), Filter (nothing), Bottle (jugs), all saved."""
        c.post("/production/boms/BOM-CB/procedure", data={
            "rows": 4, "name-0": "Steep", "components-0": ["COF-ESP-5LB"], "name-1": "Filter",
            "name-2": "Bottle", "components-2": ["JUG-GAL"], "name-3": ""})
        c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": espresso})
        c.post(self.url + "/steps/1", data={})
        c.post(self.url + "/steps/2", data={"qty:JUG-GAL": jugs})

    def _close(self, c, made="6.5"):
        return c.post(self.url + "/close", data={"made": made, "date": TODAY.isoformat()},
                      follow_redirects=True).get_data(as_text=True)

    def test_recorded_totals_replace_the_recipe_quantities_in_unleashed(self):
        app = self._app()
        c = app.test_client()
        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("1. Production", page)  # no steps set up: one step with everything
        self.assertIn("To complete the order, save", page)
        self.assertIn("To complete the order, first save step 1 (Production)",
                      c.get(self.url + "/close", follow_redirects=True).get_data(as_text=True))

        self._record(c, espresso="1.2")
        page = c.get(self.url).get_data(as_text=True)
        for text in ("1. Steep", "2. Filter", "3. Bottle", "3 of 3 steps saved", "Complete order"):
            self.assertIn(text, page)
        missing = c.post(self.url + "/steps/0", data={}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Enter how much Espresso Blend 5 lb was used", missing)
        confirm = c.get(self.url + "/close").get_data(as_text=True)
        self.assertIn("Complete in Unleashed", confirm)
        self.assertIn("<strong>1.2</strong>", confirm)

        made_before = self.ds.stock_as_at("CB-CONC-GAL")[0]
        espresso_before = self.ds.stock_as_at("COF-ESP-5LB")[0]
        done = self._close(c, made="6.5")
        self.assertIn("is completed in Unleashed", done)
        self.assertEqual(self.asm["Status"], "Completed")
        self.assertEqual(self.asm["Quantity"], 6.5)
        self.assertEqual([(l["ProductCode"], l["Quantity"], l["WastageQuantity"]) for l in self.asm["Lines"]],
                         [("COF-ESP-5LB", 1.2, 0), ("JUG-GAL", 7, 0)])
        self.assertAlmostEqual(self.ds.stock_as_at("CB-CONC-GAL")[0], made_before + 6.5)
        self.assertAlmostEqual(self.ds.stock_as_at("COF-ESP-5LB")[0], espresso_before - 1.2)

        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("Completed by Team", page)
        self.assertNotIn("Save step", page)
        order = app.extensions["unleashed"]["production"].order(self.asm["AssemblyNumber"])
        self.assertTrue(order["completed"])  # the stored copy already shows it
        espresso = next(x for x in order["components"] if x["product_code"] == "COF-ESP-5LB")
        self.assertAlmostEqual(espresso["variance"], 1.2 - 0.36 * 6.5)  # recipe for what was made
        late = c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": "1"}, follow_redirects=True)
        self.assertIn("isn&#39;t open in Unleashed any more", late.get_data(as_text=True))

    def test_nothing_completes_if_unleashed_does_not_keep_the_quantities(self):
        class IgnoresUpdates(WsgiTransport):
            def send(self, method, path, query_string, headers, body=None):
                if method == "PUT":
                    return 200, "{}"
                return super().send(method, path, query_string, headers, body)

        c = self._app(IgnoresUpdates(self.mock)).test_client()
        self._record(c)
        html = self._close(c)
        self.assertIn("didn&#39;t keep the new quantities", html)
        self.assertEqual(self.asm["Status"], "Parked")
        self.assertIn("The last attempt to complete it didn't finish", c.get(self.url).get_data(as_text=True))

    def test_unleashed_refusing_to_complete_leaves_the_order_open_to_retry(self):
        c = self._app().test_client()
        self._record(c, espresso="500")  # more than is in stock
        html = self._close(c)
        self.assertIn("Not completed", html)
        self.assertIn("Not enough stock of COF-ESP-5LB", html)
        self.assertEqual(self.asm["Status"], "Parked")
        c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": "1.3"})  # fix it and try again
        self.assertIn("is completed in Unleashed", self._close(c, made="7"))
        self.assertEqual(self.asm["Lines"][0]["Quantity"], 1.3)

    def test_close_out_retry_after_unleashed_completed(self):
        from unleashed.assembly_writes import CloseOutError, close_out
        client = UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(self.mock))
        quantities = {"COF-ESP-5LB": 1.3, "JUG-GAL": 7}  # what stock allows
        first = close_out(client, self.asm["Guid"], quantities, 7, TODAY)
        self.assertEqual(first["AssemblyStatus"], "Completed")
        again = close_out(client, self.asm["Guid"], quantities, 7, TODAY)  # e.g. the app restarted
        self.assertEqual(again["AssemblyNumber"], self.asm["AssemblyNumber"])
        with self.assertRaises(CloseOutError):
            close_out(client, self.asm["Guid"], {"COF-ESP-5LB": 3, "JUG-GAL": 7}, 7, TODAY)

    def test_start_a_production_order_from_the_erp(self):
        c = self._app().test_client()
        form = c.get("/production/orders/new?product=VSC-HG").get_data(as_text=True)
        self.assertIn('value="VSC-HG" selected', form)
        self.assertNotIn('value="MILK-WHL-GAL"', form)  # only products with a bill of materials
        check = c.get("/production/orders/new?product=VSC-HG&qty=4&assemble_by=2026-07-17&check=1")
        self.assertIn("Components needed", check.get_data(as_text=True))
        self.assertIn("CRM-HVY-HG", check.get_data(as_text=True))
        for data, message in (({"product": "MILK-WHL-GAL", "qty": "1"}, "Choose a product that has a bill"),
                              ({"product": "VSC-HG", "qty": "0"}, "Enter how much to make")):
            self.assertIn(message, c.post("/production/orders/new", data=data).get_data(as_text=True))
        before = len(self.ds.assemblies)

        res = c.post("/production/orders/new", data={"product": "VSC-HG", "qty": "4",
                                                     "assemble_by": "2026-07-17", "comments": "Weekend batch"})
        self.assertEqual(len(self.ds.assemblies), before + 1)
        new = self.ds.assemblies[-1]
        self.assertEqual((new["ProductCode"], new["Quantity"], new["Status"], new["AssembleBy"], new["Comments"]),
                         ("VSC-HG", 4, "Parked", dt.date(2026, 7, 17), "Weekend batch"))
        number = new["AssemblyNumber"]
        self.assertTrue(res.headers["Location"].endswith(f"/production/orders/{number}"))
        page = c.get(res.headers["Location"]).get_data(as_text=True)
        for text in (f"Started {number} in Unleashed", "Operating procedure", "Weekend batch"):
            self.assertIn(text, page)
        self.assertIn(number, c.get("/production/requirements").get_data(as_text=True))  # no sync needed

        c.post(f"/production/orders/{number}/steps/0", data={
            "qty:CRM-HVY-HG": "2", "qty:MILK-WHL-GAL": "1", "qty:SYR-VAN-750": "0.8"})
        done = c.post(f"/production/orders/{number}/close", data={
            "made": "4", "date": TODAY.isoformat(), "batch_number": "VSC-0715",
            "batch_qty": "", "batch_expiry": "2026-07-29"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("is completed in Unleashed", done)
        self.assertEqual(new["Status"], "Completed")
        self.assertEqual(new["Batches"], [{"Number": "VSC-0715", "Quantity": 4, "ExpiryDate": dt.date(2026, 7, 29)}])

    def _batch_order(self, app):
        """A batch-tracked order (sweet cream, 6), its step saved; its URL."""
        asm = self.ds.create_assembly("VSC-HG", 6, comments="Weekend batch")
        app.extensions["unleashed"]["syncer"].sync()
        url = f"/production/orders/{asm['AssemblyNumber']}"
        app.test_client().post(url + "/steps/0", data={
            "qty:CRM-HVY-HG": "3", "qty:MILK-WHL-GAL": "1.5", "qty:SYR-VAN-750": "1.2"})
        return asm, url

    @staticmethod
    def _close_with_batches(c, url, batches, made="6"):
        data = {"made": made, "date": TODAY.isoformat(), "batch_number": [b[0] for b in batches],
                "batch_qty": [b[1] for b in batches], "batch_expiry": [b[2] for b in batches]}
        return c.post(url + "/close", data=data, follow_redirects=True).get_data(as_text=True).replace("&#39;", "'")

    def test_batches_and_expiry_dates_go_to_unleashed_with_the_order(self):
        app = self._app()
        c = app.test_client()
        asm, url = self._batch_order(app)
        confirm = c.get(url + "/close").get_data(as_text=True)
        for text in ("is batch-tracked", "Batches made", 'name="batch_expiry"', "Complete in Unleashed"):
            self.assertIn(text, confirm)

        for batches, message in (
                ([("", "", "")], "Enter the batch number and expiry date"),
                ([("A1", "3", "2026-07-29"), ("A2", "", "2026-07-30")], "Enter how much is in batch A2"),
                ([("A1", "3", "2026-07-29"), ("a1", "3", "2026-07-30")], "Batch a1 is entered twice"),
                ([("A1", "6", "")], "Enter the expiry date of batch A1"),
                ([("A1", "6", "2026-07-01")], "Batch A1 would expire before the day it was made"),
                ([("A1", "3", "2026-07-29"), ("A2", "2", "2026-07-30")], "The batches add up to 5, but 6 was made")):
            html = self._close_with_batches(c, url, batches)
            self.assertIn(message, html)
            self.assertIn('value="A1"' if batches[0][0] else "Batches made", html)  # what was typed is kept
        self.assertEqual(asm["Status"], "Parked")

        # The mock, like Unleashed, keeps a sent time's date in Pacific time: every date stays
        # on its day (midnight used to come back as the day before).
        assemble_by = asm["AssembleBy"]
        page = self._close_with_batches(
            c, url, [("VSC-0715-A", "3.5", "2026-07-29"), ("VSC-0715-B", "2.5", "2026-07-30"), ("", "", "")])
        self.assertIn(f"{asm['AssemblyNumber']} is completed in Unleashed", page)
        self.assertIn("in 2 batches", page)
        self.assertEqual(asm["Status"], "Completed")
        self.assertEqual(asm["Batches"], [
            {"Number": "VSC-0715-A", "Quantity": 3.5, "ExpiryDate": dt.date(2026, 7, 29)},
            {"Number": "VSC-0715-B", "Quantity": 2.5, "ExpiryDate": dt.date(2026, 7, 30)}])
        self.assertEqual((asm["AssemblyDate"], asm["AssembleBy"]), (TODAY, assemble_by))
        self.assertEqual({l["ProductCode"]: l["Quantity"] for l in asm["Lines"]},
                         {"CRM-HVY-HG": 3, "MILK-WHL-GAL": 1.5, "SYR-VAN-750": 1.2})
        self.assertEqual(asm["Comments"], "Weekend batch")
        for text in ("Unleashed has these batches.", "VSC-0715-B", "Jul 30, 2026"):
            self.assertIn(text, page)
        self.assertRegex(page, r"Product yield</dt><dd[^>]*>\s*<span[^>]*>100\.0%")

    def test_batches_unleashed_does_not_keep_leave_the_order_parked(self):
        class DropsBatches(WsgiTransport):
            def send(self, method, path, query_string, headers, body=None):
                if method == "PUT" and body:
                    doc = json.loads(body)
                    doc.pop("PendingBatchNumbers", None)
                    body = json.dumps(doc)
                return super().send(method, path, query_string, headers, body)

        app = self._app(DropsBatches(self.mock))
        c = app.test_client()
        asm, url = self._batch_order(app)
        page = self._close_with_batches(c, url, [("VSC-0715-A", "3.5", "2026-07-29"), ("VSC-0715-B", "2.5", "2026-07-30")])
        self.assertIn("Unleashed didn't keep the batches as entered", page)
        self.assertEqual((asm["Status"], asm["Quantity"]), ("Parked", 6))  # quantities sent, not completed
        self.assertIn('<td class="num">3.12</td>', page)  # the recipe, not what was sent
        self.assertIn("Not in Unleashed yet", page)
        self.assertNotIn("Save step", page)  # sent: the steps are locked
        self.assertNotIn("Complete order", page)

        # Someone enters the batches in Unleashed, one expiry date wrong: Core shows which.
        asm["Batches"] = [{"Number": "VSC-0715-A", "Quantity": 3.5, "ExpiryDate": dt.date(2026, 7, 29)},
                          {"Number": "VSC-0715-B", "Quantity": 2.5, "ExpiryDate": dt.date(2026, 7, 31)}]
        asm["Modified"] = dt.date.today()
        app.extensions["unleashed"]["syncer"].sync()
        page = c.get(url).get_data(as_text=True).replace("&#39;", "'")
        self.assertIn("The batches in Unleashed don't match", page)
        self.assertIn("! 2.5, expires Jul 31, 2026", page)

        asm["Batches"][1]["ExpiryDate"] = dt.date(2026, 7, 30)
        self.ds.complete(asm)  # completed in Unleashed
        app.extensions["unleashed"]["syncer"].sync()
        page = c.get(url).get_data(as_text=True).replace("&#39;", "'")
        self.assertIn("and completed in Unleashed", page)
        self.assertIn("Unleashed has these batches.", page)
        self.assertNotIn("weren't sent", page)
        self.assertRegex(page, r"Product yield</dt><dd[^>]*>\s*<span[^>]*>100\.0%")

    def test_a_new_order_takes_from_one_warehouse_and_makes_into_another(self):
        self.ds.warehouses.append({"Guid": "wh-kit", "WarehouseCode": "KIT",
                                   "WarehouseName": "Gardena Kitchen", "IsDefault": False})
        c = self._app().test_client()
        form = c.get("/production/orders/new?product=VSC-HG").get_data(as_text=True)
        for text in ('name="from_warehouse"', 'name="to_warehouse"', "Gardena Kitchen"):
            self.assertIn(text, form)
        self.assertIn("Choose the warehouses from the list", c.post("/production/orders/new", data={
            "product": "VSC-HG", "qty": "2", "from_warehouse": "NOPE", "to_warehouse": "KIT"}).get_data(as_text=True))
        res = c.post("/production/orders/new", data={"product": "VSC-HG", "qty": "2", "assemble_by": "2026-07-17",
                                                     "from_warehouse": "GDN", "to_warehouse": "KIT"})
        new = self.ds.assemblies[-1]
        self.assertEqual((new["SourceWarehouse"], new["DestinationWarehouse"]), ("GDN", "KIT"))
        page = c.get(res.headers["Location"]).get_data(as_text=True)
        self.assertRegex(page, r"From warehouse</dt><dd[^>]*>Gardena Store<")
        self.assertRegex(page, r"To warehouse</dt><dd[^>]*>Gardena Kitchen<")

    def test_operating_procedure_guides_and_records_each_step(self):
        app = self._app()
        c = app.test_client()
        c.post("/production/boms/BOM-CB/procedure", data={
            "rows": 3, "notes": "Sanitize the brewer first.\nWear gloves.",
            "pos-0": "1", "name-0": "Weigh coffee", "instructions-0": "Weigh the espresso into the bag.",
            "components-0": ["COF-ESP-5LB"], "fields-0": 3,
            "f-0-0-label": "Coffee weight", "f-0-0-type": "number", "f-0-0-unit": "kg",
            "f-0-0-min": "0.5", "f-0-0-max": "0.6", "f-0-0-required": "1",
            "f-0-1-label": "Brewer sanitized", "f-0-1-type": "check", "f-0-1-required": "1",
            "f-0-2-label": "",  # an empty row: no field
            "pos-1": "3", "name-1": "Bottle", "components-1": ["JUG-GAL"], "fields-1": 1,
            "f-1-0-label": "Lot code", "f-1-0-type": "text",
            "pos-2": "2", "name-2": "Steep", "instructions-2": "18 hours in the fridge.", "fields-2": 1,
            "f-2-0-label": "Fridge temperature", "f-2-0-type": "number", "f-2-0-unit": "°C",
            "f-2-0-max": "5"})
        bom_page = c.get("/production/boms/BOM-CB").get_data(as_text=True)
        self.assertIn("Sanitize the brewer first.", bom_page)
        self.assertLess(bom_page.index("Steep"), bom_page.index("Bottle"))  # ordered by step number
        weigh, steep, bottle = app.extensions["production_runs"].procedure_for("CB-CONC-GAL")["steps"]
        weight, sanitized = (f["id"] for f in weigh["fields"])

        page = c.get(self.url).get_data(as_text=True)
        for text in ("Before you start", "Wear gloves.", "1. Weigh coffee", "2. Steep", "3. Bottle",
                     "Weigh the espresso into the bag.", "Coffee weight (kg) *", "Target: 0.5–0.6 kg",
                     "Brewer sanitized", "Target: at most 5 °C"):
            self.assertIn(text, page)

        def step0(**fields):
            return c.post(self.url + "/steps/0", follow_redirects=True, data={
                "qty:COF-ESP-5LB": "0.55", **{f"field:{k}": v for k, v in fields.items()}}).get_data(as_text=True)

        self.assertIn("Confirm “Brewer sanitized”", step0(**{weight: "0.55"}))
        self.assertIn("Enter Coffee weight", step0(**{sanitized: "on"}))
        self.assertIn("Coffee weight needs a number", step0(**{weight: "lots", sanitized: "on"}))
        page = step0(**{weight: "0.7", sanitized: "on"})
        self.assertIn("Saved step 1", page)
        self.assertIn("Outside target (0.5–0.6 kg)", page)
        # Editing the procedure now doesn't change an order already being recorded.
        c.post("/production/boms/BOM-CB/procedure", data={"rows": 1, "name-0": "Weigh beans", "fields-0": 0})
        self.assertIn("1. Weigh coffee", c.get(self.url).get_data(as_text=True))
        self.assertIn("Weigh beans", c.get("/production/boms/BOM-CB").get_data(as_text=True))

        c.post(self.url + "/steps/1", data={f"field:{steep['fields'][0]['id']}": "4"})
        c.post(self.url + "/steps/2", data={"qty:JUG-GAL": "7", f"field:{bottle['fields'][0]['id']}": "CB-1027"})
        confirm = c.get(self.url + "/close").get_data(as_text=True)
        self.assertIn("Coffee weight at Weigh coffee was 0.7 kg (target 0.5–0.6 kg)", confirm)
        self.assertIn("is completed in Unleashed", self._close(c, made="6"))
        record = c.get(self.url).get_data(as_text=True)  # the batch record, read-only
        for text in ("Lot code", "CB-1027", "Yes", "outside 0.5–0.6 kg", "4 °C"):
            self.assertIn(text, record)
        self.assertNotIn('name="field:', record)

    def test_each_material_has_a_yield_and_the_product_its_own(self):
        app = self._app()
        c = app.test_client()
        self._record(c)  # used 1.2 espresso and 7 jugs; the recipe is 0.36 espresso + 1 jug per gallon
        confirm = c.get(self.url + "/close").get_data(as_text=True)
        self.assertIn('data-num="0.36" data-den="1.2"', confirm)  # espresso's yield, as made is typed
        done = self._close(c, made="6.5")  # of 7 planned
        self.assertIn("made 6.5 of 7 planned, a yield of 92.9%", done)  # the finished product's
        order = app.extensions["unleashed"]["production"].order(
            self.asm["AssemblyNumber"], planned=app.extensions["production_runs"].planned_quantities())
        self.assertAlmostEqual(order["yield"], 6.5 / 7)
        by_code = {x["product_code"]: x for x in order["components"]}
        self.assertAlmostEqual(by_code["COF-ESP-5LB"]["yield"], 0.36 * 6.5 / 1.2)  # used less than the recipe
        self.assertAlmostEqual(by_code["JUG-GAL"]["yield"], 6.5 / 7)  # a jug more than 6.5 gallons need
        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("Product yield", page)
        self.assertIn("Material yield", page)

        # Over a period: each material's yield across orders; the product's where the plan is known.
        start, end = TODAY.replace(day=1), TODAY
        usage = app.extensions["unleashed"]["production"].usage(
            start, end, planned=app.extensions["production_runs"].planned_quantities())
        self.assertAlmostEqual(usage["yield"], 6.5 / 7)  # only this order was completed here
        cold_brew = [o for o in usage["orders"] if o["product_code"] == "CB-CONC-GAL"]
        jugs = next(x for x in usage["components"] if x["product_code"] == "JUG-GAL")
        self.assertAlmostEqual(jugs["yield"], sum(o["qty"] for o in cold_brew) / jugs["consumed"])
        self.assertIsNone(next(p for p in usage["products"] if p["product_code"] == "VSC-HG")["yield"])
        span = f"?start={start.isoformat()}&end={end.isoformat()}"
        csv_text = c.get("/production/orders" + span + "&format=csv").get_data(as_text=True)
        self.assertIn("Planned qty,Yield (made / planned)", csv_text)
        self.assertIn(",6.5,7,0.9286,", csv_text)
        usage_csv = c.get("/production/usage" + span + "&format=csv").get_data(as_text=True)
        self.assertIn("Recipe for what was made,Yield (recipe / used)", usage_csv)

    def test_steps_and_fields_can_be_added_while_writing_the_procedure(self):
        app = self._app()
        c = app.test_client()
        page = c.get("/production/boms/BOM-CB/procedure").get_data(as_text=True)
        # The buttons copy these templates, numbered after the last step or field.
        for text in ("+ Add a step", "+ Add a field", 'id="proc-step-template"', 'name="name-__I__"',
                     'name="pos-__I__" value="__N__"', 'id="proc-field-template"', 'name="f-__I__-__J__-label"',
                     'name="rows" value="2"', 'data-max-steps="50"'):
            self.assertIn(text, page)
        # Two empty steps were on the page; the third was added, and a fourth field on the first.
        c.post("/production/boms/BOM-CB/procedure", data={
            "rows": 3, "name-0": "Steep", "fields-0": 4, "f-0-3-label": "Fridge temperature",
            "f-0-3-type": "number", "f-0-3-max": "5", "name-1": "",
            "pos-2": "3", "name-2": "Bottle", "fields-2": 3, "f-2-0-label": "Lot code", "f-2-0-type": "text"})
        steep, bottle = app.extensions["production_runs"].procedure_for("CB-CONC-GAL")["steps"]
        self.assertEqual((steep["name"], [f["label"] for f in steep["fields"]]), ("Steep", ["Fridge temperature"]))
        self.assertEqual((bottle["name"], [f["label"] for f in bottle["fields"]]), ("Bottle", ["Lot code"]))
        # Saved steps come back with two empty steps after them, and the next added is numbered after those.
        self.assertIn('name="rows" value="4"', c.get("/production/boms/BOM-CB/procedure").get_data(as_text=True))

    def test_procedure_steps_show_pictures(self):
        from io import BytesIO
        app = self._app()
        c = app.test_client()
        runs = app.extensions["production_runs"]
        png, jpeg = b"\x89PNG\r\n\x1a\n" + b"\0" * 40, b"\xff\xd8\xff\xe0" + b"\0" * 40
        res = c.post("/production/boms/BOM-CB/procedure", content_type="multipart/form-data", follow_redirects=True,
                     data={"rows": 2, "name-0": "Steep", "components-0": ["COF-ESP-5LB"],
                           "picture-0": [(BytesIO(png), "brewer.png"), (BytesIO(jpeg), "photos/bag.jpg"),
                                         (BytesIO(b"hello"), "notes.txt")],
                           "name-1": "Bottle", "components-1": ["JUG-GAL"]})
        self.assertIn("Left out: notes.txt isn&#39;t a PNG, JPEG, GIF or WebP picture", res.get_data(as_text=True))
        steep, bottle = runs.procedure_for("CB-CONC-GAL")["steps"]
        self.assertEqual((len(steep["pictures"]), bottle["pictures"]), (2, []))
        brewer, bag = steep["pictures"]
        self.assertEqual(runs.picture(bag)["filename"], "bag.jpg")
        got = c.get(f"/production/pictures/{brewer}")
        self.assertEqual((got.status_code, got.mimetype, got.data), (200, "image/png", png))
        self.assertEqual(got.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(c.get("/production/pictures/999999").status_code, 404)
        for page in (c.get(self.url), c.get("/production/boms/BOM-CB"), c.get("/production/boms/BOM-CB/procedure")):
            self.assertIn(f'src="/production/pictures/{bag}"', page.get_data(as_text=True))

        # The order starts recording with both pictures; the procedure then drops one.
        c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": "1.2"})
        c.post("/production/boms/BOM-CB/procedure", data={
            "rows": 1, "name-0": "Steep", "pictures-0": [str(brewer), str(bag)], "remove_picture": str(bag)})
        self.assertEqual(runs.procedure_for("CB-CONC-GAL")["steps"][0]["pictures"], [brewer])
        self.assertIn(f'src="/production/pictures/{bag}"', c.get(self.url).get_data(as_text=True))  # still on it
        # Once no open order shows it, saving the procedure deletes it.
        app.extensions["db"].execute("UPDATE prod_runs SET status = 'closed'")
        c.post("/production/boms/BOM-CB/procedure", data={"rows": 1, "name-0": "Steep", "pictures-0": str(brewer)})
        self.assertIsNone(runs.picture(bag))
        self.assertNotIn(f"/production/pictures/{bag}", c.get(self.url).get_data(as_text=True))
        self.assertIsNotNone(runs.picture(brewer))
        # Another product's picture can't be put on this procedure.
        other = runs.add_picture("VAN-SYR", "x.png", png, "Nathan")
        c.post("/production/boms/BOM-CB/procedure", data={"rows": 1, "name-0": "Steep", "pictures-0": [str(brewer), str(other)]})
        self.assertEqual(runs.procedure_for("CB-CONC-GAL")["steps"][0]["pictures"], [brewer])

    def test_steps_with_nothing_to_record_do_not_hold_up_completing(self):
        c = self._app().test_client()
        c.post("/production/boms/BOM-CB/procedure", data={
            "rows": 3, "name-0": "Steep", "components-0": ["COF-ESP-5LB"], "name-1": "Filter",
            "name-2": "Bottle", "components-2": ["JUG-GAL"]})
        c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": "1.2"})
        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("To complete the order, save", page)
        self.assertIn("step 3 (Bottle)", page)
        self.assertNotIn("step 2 (Filter)", page)  # nothing to record there
        c.post(self.url + "/steps/2", data={"qty:JUG-GAL": "7"})
        self.assertIn("Complete order", c.get(self.url).get_data(as_text=True))
        self.assertIn("is completed in Unleashed", self._close(c, made="7"))

    def test_an_interrupted_completion_can_be_finished(self):
        import dashboard
        app = self._app()
        c = app.test_client()
        self._record(c)
        real = dashboard.close_out
        dashboard.close_out = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection reset"))
        try:
            html = self._close(c)
        finally:
            dashboard.close_out = real
        self.assertIn("Not completed: something unexpected went wrong", html)
        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("RuntimeError: connection reset", page)
        self.assertIn("Complete order", page)  # not stuck
        # A completion that never came back (the app restarted mid-way) is retried after a while.
        app.extensions["db"].execute("UPDATE prod_runs SET status = 'closing', updated_at = '2020-01-01 00:00:00'")
        page = c.get(self.url).get_data(as_text=True)
        self.assertIn("was interrupted before it finished", page)
        self.assertIn("is completed in Unleashed", self._close(c))
        self.assertEqual(self.asm["Status"], "Completed")

    def test_procedures_saved_as_a_plain_list_of_steps_still_load(self):
        from production_runs import parse_procedure
        old = parse_procedure('[{"name": "Mix", "components": ["SYR-VAN-750"]}]')
        self.assertEqual(old, {"notes": "", "steps": [{"name": "Mix", "instructions": "",
                                                      "components": ["SYR-VAN-750"], "fields": [],
                                                      "pictures": []}]})

    def test_close_out_checks_its_inputs(self):
        c = self._app().test_client()
        self._record(c)
        for made, date in (("0", TODAY.isoformat()), ("x", TODAY.isoformat()),
                           ("7", (TODAY + dt.timedelta(days=1)).isoformat())):
            html = c.post(self.url + "/close", data={"made": made, "date": date},
                          follow_redirects=True).get_data(as_text=True)
            self.assertIn("Enter how much was made", html)
        self.assertEqual(self.asm["Status"], "Parked")
        bad = c.post(self.url + "/steps/0", data={"qty:COF-ESP-5LB": "-1"}, follow_redirects=True)
        self.assertIn("can&#39;t be negative", bad.get_data(as_text=True))



class MaintenanceTests(unittest.TestCase):
    """The Maintenance tab: locations, recurring tasks, recording work, files, planning."""

    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock", TODAY=TODAY)
        self.c = self.app.test_client()
        self.store = self.app.extensions["maintenance"]

    def _location(self, name, parent=None, **extra):
        data = {"name": name, **({"parent_id": str(parent)} if parent else {}), **extra}
        return self.c.post("/maintenance/locations", data=data)

    def _location_id(self, name):
        return next(l["id"] for l in self.store.locations() if l["name"] == name)

    def _task(self, name, every, due, **extra):
        res = self.c.post("/maintenance/tasks/new", data={"name": name, "frequency_days": str(every),
                                                          "next_due": due.isoformat(), **extra})
        self.assertEqual(res.status_code, 302, res.get_data(as_text=True))
        return int(res.headers["Location"].rsplit("/", 1)[1])

    def test_locations_form_a_tree(self):
        self._location("Gardena")
        self._location("Kitchen", self._location_id("Gardena"))
        self._location("Espresso machine", self._location_id("Kitchen"), description="La Marzocco")
        self._location("Kitchen")  # the same name is fine somewhere else
        self.assertIn("already a location called kitchen under Gardena",
                      self._location("kitchen", self._location_id("Gardena")).get_data(as_text=True))
        espresso = next(l for l in self.store.locations() if l["name"] == "Espresso machine")
        self.assertEqual((espresso["path"], espresso["depth"]), ("Gardena › Kitchen › Espresso machine", 2))
        gardena = self._location_id("Gardena")
        moved = self._location("Gardena", espresso["id"], id=str(gardena))
        self.assertIn("can&#39;t go under itself", moved.get_data(as_text=True))
        self.assertEqual(self.store.under(gardena), {gardena, self._location_id("Espresso machine"),
                                                     next(l["id"] for l in self.store.locations()
                                                          if l["path"] == "Gardena › Kitchen")})
        page = self.c.get("/maintenance/locations").get_data(as_text=True)
        self.assertIn("La Marzocco", page)
        self.c.post(f"/maintenance/locations/{espresso['id']}/delete")
        self.assertNotIn(espresso["id"], {l["id"] for l in self.store.locations()})
        self.c.post(f"/maintenance/locations/{gardena}/delete")  # and everything under it
        self.assertEqual([l["path"] for l in self.store.locations()], ["Kitchen"])

    def test_tasks_come_due_and_move_on_when_done(self):
        self._location("Gardena")
        self._location("Kitchen", self._location_id("Gardena"))
        kitchen = next(l["id"] for l in self.store.locations() if l["name"] == "Kitchen")
        for data, message in (({"name": "", "frequency_days": "7"}, "Give the task a name"),
                              ({"name": "X", "frequency_days": "0"}, "How often must be at least 1"),
                              ({"name": "X", "frequency_days": "7", "next_due": ""}, "Enter the date it")):
            html = self.c.post("/maintenance/tasks/new", data=data).get_data(as_text=True)
            self.assertIn(message, html.replace("&#39;", "'"))
        descale = self._task("Descale espresso machine", 30, TODAY - dt.timedelta(days=65),
                             location_id=str(kitchen), part_name="Descaler", lead_time_days="10")
        grinder = self._task("Clean grinder burrs", 7, TODAY + dt.timedelta(days=2), location_id=str(kitchen))
        self._task("Check fire extinguishers", 365, TODAY + dt.timedelta(days=100))

        tasks = {t["name"]: t for t in self.store.tasks(TODAY)}
        self.assertEqual((tasks["Descale espresso machine"]["status"], tasks["Descale espresso machine"]["due_label"]),
                         ("overdue", "65 days overdue"))
        self.assertTrue(tasks["Clean grinder burrs"]["due_soon"])
        self.assertEqual(tasks["Descale espresso machine"]["order_by"], TODAY - dt.timedelta(days=75))
        overview = self.c.get("/maintenance/").get_data(as_text=True)
        for text in ("Descale espresso machine", "Parts to order", "Due in 2 days"):
            self.assertIn(text, overview)
        # A location's tasks include those below it; filters narrow them.
        gardena = self._location_id("Gardena")
        self.assertEqual(len(self.store.tasks(TODAY, gardena)), 2)
        here = self.c.get(f"/maintenance/tasks?location={gardena}&status=overdue").get_data(as_text=True)
        self.assertIn("Descale espresso machine", here)
        self.assertNotIn("Clean grinder burrs", here)
        csv = self.c.get("/maintenance/tasks?format=csv").get_data(as_text=True)
        self.assertTrue(csv.startswith("Task,Location,Location code,Equipment,Status"))
        self.assertIn("Gardena › Kitchen", csv)

        # Done 65 days late: it keeps its 30-day schedule, next due after today.
        done = self.c.post(f"/maintenance/tasks/{descale}/complete", data={
            "status": "completed", "duration_minutes": "45", "labor_hours": "0.75", "parts_used": "1 descaler",
            "notes": "Scale was heavy"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("next due Aug 9, 2026", done)  # due May 11, then 30-day steps: Jun 10, Jul 10, Aug 9
        self.assertEqual(self.store.task(descale, TODAY)["status"], "pending")
        self.assertIn("Scale was heavy", done)
        self.assertIn("✓ Completed", done)
        # Skipping needs a reason, and also moves it on.
        self.assertIn("Say why it was skipped", self.c.post(
            f"/maintenance/tasks/{grinder}/complete", data={"status": "skipped"}, follow_redirects=True
        ).get_data(as_text=True).replace("&#39;", "'"))
        self.c.post(f"/maintenance/tasks/{grinder}/complete", data={"status": "skipped", "notes": "Closed"})
        self.assertEqual(self.store.task(grinder, TODAY)["next_due"], TODAY + dt.timedelta(days=9))
        # Work is stamped with the real clock; the test's "today" is fixed earlier.
        history = self.c.get("/maintenance/history?start=2026-01-01&end=2030-12-31").get_data(as_text=True)
        self.assertIn("Skipped", history)
        self.assertIn("Scale was heavy", history)
        self.assertTrue(self.c.get("/maintenance/history?start=2026-01-01&end=2030-12-31&format=csv").get_data(as_text=True)
                        .startswith("Done at,Work,Type,Status,Location"))

        # Editing: in progress, a new due date.
        self.c.post(f"/maintenance/tasks/{grinder}/edit", data={
            "name": "Clean grinder burrs", "frequency_days": "7", "next_due": (TODAY + dt.timedelta(days=1)).isoformat(),
            "location_id": str(kitchen), "in_progress": "1"})
        self.assertEqual(self.store.task(grinder, TODAY)["status"], "in_progress")
        # Without accounts everyone manages everything, so the task can be deleted.
        self.c.post(f"/maintenance/tasks/{grinder}/delete")
        self.assertIsNone(self.store.task(grinder, TODAY))
        self.assertEqual(self.store.completions(task_id=grinder), [])
        left = self.store.tasks(TODAY, kitchen)
        self.assertTrue(left)
        self.assertIn("Deleted Kitchen with", self.c.post(f"/maintenance/locations/{kitchen}/delete",
                                                          follow_redirects=True).get_data(as_text=True))
        self.assertTrue(all(self.store.task(t["id"], TODAY) is None for t in left))

    def test_files_are_kept_with_their_task(self):
        import io
        task = self._task("Service boiler", 180, TODAY)
        pdf = b"%PDF-1.4 boiler SOP" + bytes(range(256))
        self.c.post(f"/maintenance/tasks/{task}/attachments",
                    data={"file": (io.BytesIO(pdf), "Boiler SOP.pdf")}, content_type="multipart/form-data")
        refused = self.c.post(f"/maintenance/tasks/{task}/attachments", follow_redirects=True,
                              data={"file": (io.BytesIO(b"MZ"), "tool.exe")}, content_type="multipart/form-data")
        self.assertIn("Attach a PDF", refused.get_data(as_text=True))
        files = self.store.task(task, TODAY)["attachments"]
        self.assertEqual([(f["filename"], f["size"]) for f in files], [("Boiler SOP.pdf", len(pdf))])
        got = self.c.get(f"/maintenance/attachments/{files[0]['id']}")
        self.assertEqual(got.data, pdf)
        self.assertEqual(got.mimetype, "application/pdf")
        self.assertIn('attachment; filename="Boiler SOP.pdf"', got.headers["Content-Disposition"])
        self.c.post(f"/maintenance/attachments/{files[0]['id']}/delete")
        self.assertEqual(self.store.task(task, TODAY)["attachments"], [])

    def test_calendar_shows_due_dates_the_schedule_and_work_done(self):
        weekly = self._task("Clean grinder burrs", 7, TODAY + dt.timedelta(days=1))
        self._task("Descale espresso machine", 30, TODAY - dt.timedelta(days=3))
        self.c.post(f"/maintenance/tasks/{weekly}/complete", data={"status": "completed"})  # now due Jul 23
        days = self.store.calendar(TODAY.replace(day=1), TODAY, lambda ts: dt.date.fromisoformat(ts[:10]),
                                   "2026-07-01 00:00:00", "2026-08-01 00:00:00")
        kinds = {(d.day, e["kind"]) for d, entries in days.items() for e in entries}
        self.assertIn((12, "overdue"), kinds)  # the descale, due Jul 12
        self.assertIn((23, "due"), kinds)
        self.assertIn((30, "planned"), kinds)  # the grinder a week after
        page = self.c.get("/maintenance/calendar?month=2026-07").get_data(as_text=True)
        for text in ("July 2026", "cal-overdue", "cal-planned", "Clean grinder burrs"):
            self.assertIn(text, page)
        self.assertIn("cal-planned", self.c.get("/maintenance/calendar?month=2026-08").get_data(as_text=True))

    def test_pages_wear_the_yeems_brand(self):
        page = self.c.get("/maintenance/").get_data(as_text=True)
        for text in ('rel="icon" href="/static/favicon.svg?v=', 'aria-label="Yeems coffee"',
                     'class="brand-name">Yeems Core</strong>', "/static/fonts/inter-latin-400-normal.woff2"):
            self.assertIn(text, page)
        for asset in ("favicon.svg", "fonts/inter-latin-400-normal.woff2",
                      "fonts/montserrat-alternates-latin-700-normal.woff2"):
            self.assertEqual(self.c.get(f"/static/{asset}").status_code, 200, asset)
        missing = self.c.get("/maintenance/tasks/999999")  # a branded page, not Flask's plain one
        self.assertEqual(missing.status_code, 404)
        self.assertIn("Page not found", missing.get_data(as_text=True))
        self.assertEqual(self.c.get("/static/nope.css").status_code, 404)

    def test_calendar_days_add_tasks_and_due_dates_can_be_moved(self):
        weekly = self._task("Clean grinder burrs", 7, TODAY + dt.timedelta(days=1))  # due Jul 16
        page = self.c.get("/maintenance/calendar?month=2026-07").get_data(as_text=True)
        self.assertIn('class="brand-name">Yeems Core</strong>', page)
        add = "/maintenance/tasks/new?next_due=2026-07-20&amp;return_to=/maintenance/calendar?month%3D2026-07"
        self.assertIn(f'data-date="2026-07-20" data-new-url="{add}"', page)
        self.assertIn(f'data-move-url="/maintenance/tasks/{weekly}/reschedule" data-due="2026-07-16"', page)
        self.assertEqual(page.count("cal-movable"), 1)  # its due date; not the dates its schedule brings

        # Clicking Jul 20: the new task is due then, and saving goes back to the calendar.
        form = self.c.get("/maintenance/tasks/new?next_due=2026-07-20&return_to=/maintenance/calendar?month=2026-07")
        html = form.get_data(as_text=True)
        self.assertIn('name="next_due" value="2026-07-20"', html)
        self.assertIn('name="return_to" value="/maintenance/calendar?month=2026-07"', html)
        res = self.c.post("/maintenance/tasks/new", data={"name": "Check ice machine", "frequency_days": "30",
                                                          "next_due": "2026-07-20",
                                                          "return_to": "/maintenance/calendar?month=2026-07"})
        self.assertEqual(res.headers["Location"], "/maintenance/calendar?month=2026-07")
        elsewhere = self.c.post("/maintenance/tasks/new", data={"name": "X", "frequency_days": "1", "next_due": "2026-07-20",
                                                                "return_to": "https://example.com/maintenance/"})
        self.assertRegex(elsewhere.headers["Location"], r"^/maintenance/tasks/\d+$")

        # Dragging the grinder from Jul 16 to Jul 18.
        moved = self.c.post(f"/maintenance/tasks/{weekly}/reschedule", data={"from": "2026-07-16", "to": "2026-07-18"})
        self.assertEqual(moved.get_json(), {"ok": True, "message": "Moved Clean grinder burrs to Jul 18, 2026.",
                                            "next_due": "2026-07-18"})
        self.assertEqual(self.store.task(weekly, TODAY)["next_due"], dt.date(2026, 7, 18))
        self.assertIn("Moved Clean grinder burrs to Jul 18, 2026",
                      self.c.get("/maintenance/calendar?month=2026-07").get_data(as_text=True))
        stale = self.c.post(f"/maintenance/tasks/{weekly}/reschedule", data={"from": "2026-07-16", "to": "2026-07-25"})
        self.assertEqual(stale.status_code, 409)
        self.assertIn("changed since this page was opened", stale.get_json()["message"])
        self.assertEqual(self.c.post(f"/maintenance/tasks/{weekly}/reschedule",
                                     data={"from": "2026-07-18", "to": "soon"}).status_code, 400)
        self.assertEqual(self.store.task(weekly, TODAY)["next_due"], dt.date(2026, 7, 18))
        past = self.c.post(f"/maintenance/tasks/{weekly}/reschedule", data={"from": "2026-07-18", "to": "2026-07-10"})
        self.assertIn("That date has passed, so it's overdue.", past.get_json()["message"])
        self.assertEqual(self.store.task(weekly, TODAY)["status"], "overdue")



class MaintenancePlantTests(unittest.TestCase):
    """SAP-style maintenance: location codes and master data, templates, equipment and its
    counters, usage-based tasks, task lists, requests and work orders with their costs."""

    def setUp(self):
        self.app = make_app(self, UNLEASHED_MODE="mock", TODAY=TODAY)
        self.c = self.app.test_client()
        self.store = self.app.extensions["maintenance"]

    def _post(self, path, **data):
        res = self.c.post(path, data=data)
        self.assertIn(res.status_code, (302, 303), res.get_data(as_text=True)[:3000])
        return res

    def _new_id(self, res):
        return int(res.headers["Location"].rstrip("/").rsplit("/", 1)[1].split("#")[0])

    def _location(self, name, parent=None, **fields):
        res = self._post("/maintenance/locations", name=name, parent_id=str(parent or ""), **fields)
        return self._new_id(res)

    def _store_tree(self):
        """Gardena (GAR, site, cost center 4100, A) › Kitchen (GAR-KIT) › Espresso (GAR-KIT-ESP)."""
        gar = self._location("Gardena", code="gar", site="Gardena store", cost_center="4100", criticality="A")
        kit = self._location("Kitchen", gar, code="GAR-KIT", work_center="Maintenance team")
        esp = self._location("Espresso station", kit, code="GAR-KIT-ESP", criticality="B")
        return gar, kit, esp

    def test_location_codes_nest_and_master_data_comes_from_above(self):
        gar, kit, esp = self._store_tree()
        loc = self.store.location(esp)
        self.assertEqual((loc["label"], loc["path"]), ("GAR-KIT-ESP · Espresso station", "Gardena › Kitchen › Espresso station"))
        self.assertEqual((loc["eff_site"], loc["from_site"]), ("Gardena store", "Gardena"))
        self.assertEqual((loc["eff_cost_center"], loc["eff_work_center"]), ("4100", "Maintenance team"))
        self.assertEqual((loc["eff_criticality"], loc["from_criticality"]), ("B", None))  # its own
        bad = self.c.post("/maintenance/locations", data={"name": "Bar", "parent_id": str(gar), "code": "BAR"})
        self.assertIn("Codes under GAR · Gardena start with GAR-.", bad.get_data(as_text=True))
        taken = self.c.post("/maintenance/locations", data={"name": "Other", "code": "gar-kit"})
        self.assertIn("already has the code GAR-KIT", taken.get_data(as_text=True))
        self.assertIn("Codes use letters", self.c.post("/maintenance/locations", data={"name": "X", "code": "A B!"}
                                                      ).get_data(as_text=True))
        # Renaming a code renames the codes under it.
        self._post("/maintenance/locations", id=str(gar), name="Gardena", code="GDN", site="Gardena store",
                   cost_center="4100", criticality="A")
        self.assertEqual([l["code"] for l in self.store.locations()], ["GDN", "GDN-KIT", "GDN-KIT-ESP"])
        page = self.c.get(f"/maintenance/locations/{esp}").get_data(as_text=True)
        for text in ("GDN-KIT-ESP", "from Gardena", "Copy this structure", "Maintenance team"):
            self.assertIn(text, page)
        self.assertEqual([l["code"] for l in self.store.search_locations("kit")], ["GDN-KIT", "GDN-KIT-ESP"])

    def test_a_template_is_copied_to_set_up_a_new_store(self):
        tpl = self._location("Store template", code="TPL", is_template="1", cost_center="4000")
        bar = self._location("Bar", tpl, code="TPL-BAR")
        self._post("/maintenance/tasks/new", name="Clean ice machine", frequency_days="30",
                   next_due="2026-07-01", location_id=str(bar))
        self.assertEqual(self.store.tasks(TODAY), [])  # a template's tasks never come due
        self.assertEqual(len(self.store.tasks(TODAY, bar)), 1)  # but show when looking inside it
        self.assertIn("never come due", self.c.get(f"/maintenance/tasks?location={bar}").get_data(as_text=True))
        res = self._post(f"/maintenance/locations/{tpl}/copy", name="Torrance", code="TOR", with_tasks="1",
                         first_due="2026-07-20")
        tor = self._new_id(res)
        copied = {l["code"]: l for l in self.store.locations() if not l["in_template"]}
        self.assertEqual(sorted(copied), ["TOR", "TOR-BAR"])
        self.assertEqual(copied["TOR-BAR"]["eff_cost_center"], "4000")
        live = self.store.tasks(TODAY)
        self.assertEqual([(t["name"], t["location"], t["next_due"]) for t in live],
                         [("Clean ice machine", "Torrance › Bar", dt.date(2026, 7, 20))])
        clash = self.c.post(f"/maintenance/locations/{tpl}/copy", data={"name": "Again", "code": "TOR"}, follow_redirects=True)
        self.assertIn("already has the code TOR", clash.get_data(as_text=True))
        self.assertEqual(len([l for l in self.store.locations() if l["name"] == "Again"]), 0)  # nothing half made
        self.assertIn("is a template", self.c.post("/maintenance/equipment/new", data={
            "name": "Ice machine", "location_id": str(bar)}).get_data(as_text=True))
        self.assertIsNotNone(tor)

    def test_new_columns_are_added_to_an_earlier_database(self):
        from maintenance_store import migrate
        db = self.app.extensions["db"]
        db.execute("ALTER TABLE maint_tasks DROP COLUMN counter_id")
        db.execute("ALTER TABLE maint_locations DROP COLUMN code")
        migrate(db)
        migrate(db)  # and again: nothing to do
        db.execute("UPDATE maint_tasks SET counter_id = NULL")
        db.execute("UPDATE maint_locations SET code = NULL")

    def test_equipment_moves_and_its_tasks_follow(self):
        gar, kit, esp = self._store_tree()
        bar = self._location("Bar", gar, code="GAR-BAR")
        res = self._post("/maintenance/equipment/new", name="Grinder 1", category="Grinder", manufacturer="Mahlkönig",
                         model="E80", serial_number="SN-123", location_id=str(esp), installed_on="2026-01-10",
                         warranty_until="2027-01-10", purchase_cost="2400")
        grinder = self._new_id(res)
        item = self.store.equipment(grinder)
        self.assertEqual((item["number"], item["status"], item["location"]), ("EQ-00001", "installed", "GAR-KIT-ESP · Espresso station"))
        self.assertEqual(item["criticality_used"], "B")  # from where it is
        self._post("/maintenance/tasks/new", name="Clean burrs", frequency_days="7", next_due="2026-07-16",
                   equipment_id=str(grinder), location_id=str(bar))  # the equipment decides where
        self.assertEqual(self.store.tasks(TODAY, equipment_id=grinder)[0]["location"], "Gardena › Kitchen › Espresso station")
        self._post(f"/maintenance/equipment/{grinder}/move", action="install", location_id=str(bar), on_date="2026-07-01",
                   note="Swapped with grinder 2")
        self.assertEqual(self.store.tasks(TODAY, bar)[0]["name"], "Clean burrs")
        self.assertEqual([m["action"] for m in self.store.equipment(grinder)["moves"]], ["installed", "dismantled", "installed"])
        self.assertIn("Swapped with grinder 2", self.c.get(f"/maintenance/equipment/{grinder}").get_data(as_text=True))
        refused = self.c.post(f"/maintenance/equipment/{grinder}/move", data={"action": "retire"}, follow_redirects=True)
        self.assertIn("has 1 task", refused.get_data(as_text=True))
        self._post(f"/maintenance/equipment/{grinder}/move", action="storage")
        self.assertEqual(self.store.equipment(grinder)["status"], "storage")
        self.assertIsNone(self.store.tasks(TODAY, equipment_id=grinder)[0]["location"])
        listing = self.c.get("/maintenance/equipment?q=e80").get_data(as_text=True)
        self.assertIn("Grinder 1", listing)
        self.assertTrue(self.c.get("/maintenance/equipment?format=csv").get_data(as_text=True).startswith("Number,Asset tag,Name"))
        self.assertIn("has tasks", self.c.post(f"/maintenance/equipment/{grinder}/delete", follow_redirects=True
                                               ).get_data(as_text=True))

    def test_usage_based_tasks_come_due_by_their_counter(self):
        gar, kit, esp = self._store_tree()
        machine = self._new_id(self._post("/maintenance/equipment/new", name="Espresso machine", location_id=str(esp)))
        self._post(f"/maintenance/equipment/{machine}/counters", name="Shots", unit="shots", value="10000")
        counter = self.store.equipment(machine)["counters"][0]["id"]
        self.assertIn("Say how much use", self.c.post("/maintenance/tasks/new", data={
            "name": "Gaskets", "equipment_id": str(machine), "counter_id": str(counter)}).get_data(as_text=True))
        task = self._new_id(self._post("/maintenance/tasks/new", name="Replace group gaskets", equipment_id=str(machine),
                                       counter_id=str(counter), usage_interval="5000"))
        t = self.store.task(task, TODAY)
        self.assertEqual((t["usage"]["due_at"], t["usage"]["left"], t["next_due"]), (15000, 5000, None))
        self.assertEqual(t["due_label"], "At 15,000 shots (now 10,000)")  # no rate yet
        # Readings: 300 shots a day.
        db = self.app.extensions["db"]
        db.execute("UPDATE maint_readings SET read_on = ? WHERE counter_id = ?", ((TODAY - dt.timedelta(days=10)).isoformat(), counter))
        self._post(f"/maintenance/counters/{counter}/readings", value="13000", read_on=TODAY.isoformat())
        lower = self.c.post(f"/maintenance/counters/{counter}/readings", data={"value": "12000"}, follow_redirects=True)
        self.assertIn("lower than the reading before it (13,000 shots)", lower.get_data(as_text=True))
        t = self.store.task(task, TODAY)
        self.assertEqual((t["usage"]["rate"], t["next_due"]), (300, TODAY + dt.timedelta(days=7)))
        self.assertEqual(t["due_label"], "In 2,000 shots (about Jul 22)")
        self.assertFalse(t["due_soon"])  # a week away: just past "the next 7 days"
        self._post(f"/maintenance/counters/{counter}/readings", value="15200", read_on=TODAY.isoformat())
        t = self.store.task(task, TODAY)
        self.assertEqual((t["status"], t["due_label"]), ("overdue", "Due: 15,200 of 15,000 shots"))
        # Recording it with the reading counts the next 5,000 from there.
        done = self.c.post(f"/maintenance/tasks/{task}/complete", data={"status": "completed", "reading": "15300"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("next due at 20,300 shots", done)
        self.assertEqual(self.store.task(task, TODAY)["usage"]["due_at"], 20300)
        # Every 90 days or 20,000 shots, whichever first: done now, both clocks start again.
        both = self._new_id(self._post("/maintenance/tasks/new", name="Service", equipment_id=str(machine),
                                       counter_id=str(counter), usage_interval="20000", frequency_days="90",
                                       next_due="2026-07-01"))
        self.assertEqual(self.store.task(both, TODAY)["status"], "overdue")  # by date
        self._post(f"/maintenance/tasks/{both}/complete", status="completed")
        t = self.store.task(both, TODAY)
        self.assertEqual((t["time_due"], t["usage"]["due_at"]), (TODAY + dt.timedelta(days=90), 35300))
        self.assertFalse(t["movable"] and t["usage"]["projected"] and t["usage"]["projected"] < t["time_due"])

    def test_steps_are_written_on_the_task_and_ticked_off(self):
        task = self._new_id(self._post("/maintenance/tasks/new", name="Burr clean", frequency_days="7",
                                       next_due=TODAY.isoformat(), rows="4",
                                       **{"op-text-0": "Unplug and empty the hopper", "op-minutes-0": "2",
                                          "op-text-1": "Remove and brush the burrs", "op-minutes-1": "10",
                                          "op-text-2": "", "op-text-3": "Reassemble"}))
        t = self.store.task(task, TODAY)
        self.assertEqual(([o["text"] for o in t["steps"]], t["step_minutes"]),
                         (["Unplug and empty the hopper", "Remove and brush the burrs", "Reassemble"], 12))
        page = self.c.get(f"/maintenance/tasks/{task}").get_data(as_text=True)
        self.assertIn('name="op-2"', page)
        self.assertIn("about 12 minutes", page)
        self._post(f"/maintenance/tasks/{task}/complete", status="completed", **{"op-0": "1", "op-2": "1"},
                   parts_cost="12.50", labor_hours="0.25")
        c = self.store.task(task, TODAY)["completions"][0]
        self.assertEqual([s["done"] for s in c["steps"]], [True, False, True])
        self.assertIn("2 of 3 steps", self.c.get(f"/maintenance/tasks/{task}").get_data(as_text=True))
        history = self.store.work_done()
        self.assertEqual((history[0]["cost"], history[0]["labor_hours"]), (12.5, 0.25))

        # Editing shows the steps to change; a new task can start from them.
        edit = self.c.get(f"/maintenance/tasks/{task}/edit").get_data(as_text=True)
        self.assertIn('value="Remove and brush the burrs"', edit)
        self.assertNotIn("Start from another task", edit)  # only itself has steps
        self._post(f"/maintenance/tasks/{task}/edit", name="Burr clean", frequency_days="7", next_due=TODAY.isoformat(),
                   rows="1", **{"op-text-0": "Just brush them"})
        self.assertEqual([o["text"] for o in self.store.task(task, TODAY)["steps"]], ["Just brush them"])
        new = self.c.get("/maintenance/tasks/new").get_data(as_text=True)
        self.assertIn("Start from another task", new)
        self.assertIn("Burr clean (1 step)", new)
        # No steps is fine.
        plain = self._new_id(self._post("/maintenance/tasks/new", name="Mop", frequency_days="1", next_due=TODAY.isoformat()))
        self.assertEqual(self.store.task(plain, TODAY)["steps"], [])

    def test_steps_can_be_added_to_a_task_without_saving_first(self):
        page = self.c.get("/maintenance/tasks/new").get_data(as_text=True)
        for text in ("+ Add a step", 'id="op-template"', 'name="op-text-__I__"', 'aria-label="Step __N__"',
                     'name="rows" value="3"', 'data-max="100"'):
            self.assertIn(text, page)
        self.assertNotIn("Task lists", self.c.get("/maintenance/").get_data(as_text=True))
        self.assertEqual(self.c.get("/maintenance/tasklists").status_code, 301)
        # A fourth step, added on the page; the count sent is capped however large it is.
        res = self._post("/maintenance/tasks/new", name="Descale", frequency_days="90", next_due=TODAY.isoformat(),
                         rows="4", **{"op-text-0": "Drain", "op-text-3": "Refill", "op-minutes-3": "5"})
        self.assertEqual([o["text"] for o in self.store.task(self._new_id(res), TODAY)["steps"]], ["Drain", "Refill"])
        res = self._post("/maintenance/tasks/new", name="Huge", frequency_days="1", next_due=TODAY.isoformat(),
                         rows="1000000000", **{"op-text-0": "One"})
        self.assertEqual(len(self.store.task(self._new_id(res), TODAY)["steps"]), 1)
        # A mistake elsewhere on the form keeps the steps typed.
        bad = self.c.post("/maintenance/tasks/new", data={"name": "", "rows": "1", "op-text-0": "Keep me"})
        self.assertIn('value="Keep me"', bad.get_data(as_text=True))

    def test_task_lists_from_before_become_each_tasks_own_steps(self):
        import json as _json
        from maintenance_store import migrate
        db = self.store.db
        listed = db.query("INSERT INTO maint_tasklists (name, operations, updated_by, updated_at) VALUES"
                          " ('Burr clean', ?, 'x', '2026-01-01') RETURNING id",
                          (_json.dumps([{"text": "Unplug", "minutes": 2}, {"text": "Brush", "minutes": None}]),))[0]["id"]
        a = self._new_id(self._post("/maintenance/tasks/new", name="Grinder 1", frequency_days="7", next_due=TODAY.isoformat()))
        b = self._new_id(self._post("/maintenance/tasks/new", name="Grinder 2", frequency_days="7", next_due=TODAY.isoformat()))
        db.execute("UPDATE maint_tasks SET tasklist_id = ?", (listed,))
        migrate(db)
        migrate(db)  # again, as every start does
        for task in (a, b):
            t = self.store.task(task, TODAY)
            self.assertEqual(([o["text"] for o in t["steps"]], t["step_minutes"], t["tasklist_id"]),
                             (["Unplug", "Brush"], 2, None))

    def test_tasks_have_a_simptww_job_plan_with_pictures(self):
        import io
        gar, kit, esp = self._store_tree()
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
        # A task made before SIMPTWW: everything but its location and schedule is missing.
        old = self._new_id(self._post("/maintenance/tasks/new", name="Mop kitchen", frequency_days="1",
                                      next_due=TODAY.isoformat(), location_id=str(kit)))
        t = self.store.task(old, TODAY)
        self.assertEqual([p["letter"] for p in t["simptww"]], list("SIMPTWW"))
        self.assertEqual([p["key"] for p in t["simptww"] if p["filled"]], ["where"])
        listing = self.c.get("/maintenance/tasks?status=simptww").get_data(as_text=True)
        self.assertIn("Mop kitchen", listing)
        self.assertIn("SIMPTWW: 1 of 7 filled in (missing Safety, Information, Material, People, Tools, When &amp; who)",
                      listing)

        form = self.c.get("/maintenance/tasks/new").get_data(as_text=True)
        for key in ("safety", "info", "material", "people", "tools", "where", "when"):
            self.assertIn(f'name="pictures-{key}" multiple', form)
        self.assertIn('enctype="multipart/form-data"', form)

        res = self.c.post("/maintenance/tasks/new", content_type="multipart/form-data", data={
            "name": "Descale espresso machine", "frequency_days": "90", "next_due": TODAY.isoformat(),
            "location_id": str(esp), "safety": "Lock out at the wall switch. Let it cool 30 min.",
            "rows": "1", "op-text-0": "Run descaler through each group", "materials": "Descaler 500 ml",
            "people_needed": "2", "skills": "Barista lead", "tools": "Blind basket, bucket",
            "where_notes": "Pull the machine forward", "when_notes": "After close",
            "pictures-safety": [(io.BytesIO(png), "lockout.png")],
            "pictures-tools": [(io.BytesIO(png), "tools.png"), (io.BytesIO(b"%PDF-1.4"), "sop.pdf")],
        }, follow_redirects=True)
        page = res.get_data(as_text=True)
        self.assertIn("sop.pdf wasn&#39;t added: Only pictures go in the SIMPTWW", page)
        new = next(x for x in self.store.tasks(TODAY) if x["name"] == "Descale espresso machine")
        task = self.store.task(new["id"], TODAY)
        self.assertEqual(task["simptww_filled"], 7)
        self.assertEqual((task["people_needed"], task["tools"]), (2, "Blind basket, bucket"))
        self.assertEqual([f["filename"] for f in task["pictures"]["safety"]], ["lockout.png"])
        self.assertEqual([f["filename"] for f in task["pictures"]["tools"]], ["tools.png"])
        self.assertEqual(task["attachments"], [])  # section pictures aren't listed under Files
        for text in ("SIMPTWW", "7 of 7 filled in", "⚠ Safety first", "Lock out at the wall switch",
                     "2 people · Barista lead", "Pull the machine forward", "After close",
                     f'src="/maintenance/attachments/{task["pictures"]["safety"][0]["id"]}?view=1"'):
            self.assertIn(text, page)
        self.assertNotIn("Descale espresso machine", self.c.get("/maintenance/tasks?status=simptww").get_data(as_text=True))

        # Editing an existing task: its sheet is filled in, a picture removed and another added.
        edit = self.c.get(f"/maintenance/tasks/{new['id']}/edit").get_data(as_text=True)
        self.assertIn("Blind basket, bucket", edit)
        self.assertIn('name="remove_picture"', edit)
        lockout = task["pictures"]["safety"][0]["id"]
        res = self.c.post(f"/maintenance/tasks/{old}/edit", content_type="multipart/form-data", data={
            "name": "Mop kitchen", "frequency_days": "1", "next_due": TODAY.isoformat(), "location_id": str(kit),
            "safety": "Wet floor sign out", "remove_picture": str(lockout),  # another task's: left alone
            "pictures-where": [(io.BytesIO(png), "kitchen.png")]})
        self.assertEqual(res.status_code, 302)
        t = self.store.task(old, TODAY)
        self.assertEqual([p["key"] for p in t["simptww"] if p["filled"]], ["safety", "where"])
        self.assertEqual([f["filename"] for f in t["pictures"]["where"]], ["kitchen.png"])
        self.assertEqual(len(self.store.task(new["id"], TODAY)["pictures"]["safety"]), 1)
        self.c.post(f"/maintenance/tasks/{new['id']}/edit", data={
            "name": "Descale espresso machine", "frequency_days": "90", "next_due": TODAY.isoformat(),
            "location_id": str(esp), "remove_picture": str(lockout)})
        t = self.store.task(new["id"], TODAY)
        self.assertEqual((t["pictures"]["safety"], t["safety"]), ([], None))  # cleared fields stay cleared
        csv = self.c.get("/maintenance/tasks?format=csv").get_data(as_text=True)
        self.assertIn("SIMPTWW filled in", csv.splitlines()[0])

    def test_work_order_form_picks_location_then_equipment_and_takes_pictures(self):
        import io
        gar, kit, esp = self._store_tree()
        tor = self._location("Torrance", code="TOR")
        grinder = self._new_id(self._post("/maintenance/equipment/new", name="Grinder 2", location_id=str(esp)))
        form = self.c.get("/maintenance/orders/new").get_data(as_text=True)
        self.assertNotIn('name="assigned_to"', form)
        self.assertIn("data-cascade", form)
        self.assertIn(f'data-parent="{kit}"', form)  # the espresso station's option knows what it's under
        self.assertIn(f'data-under="{esp} {kit} {gar}"', form)  # the grinder counts as at all three
        self.assertIn('name="pictures" multiple', form)

        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
        res = self.c.post("/maintenance/orders/new", content_type="multipart/form-data", data={
            "title": "Fix the drain", "location_id": str(kit), "equipment_id": "",
            "pictures": [(io.BytesIO(png), "drain.png"), (io.BytesIO(png), "under-sink.png"),
                         (io.BytesIO(b"nope"), "notes.exe")]}, follow_redirects=True)
        page = res.get_data(as_text=True)
        self.assertIn("notes.exe wasn&#39;t attached", page)
        order = self.store.orders()[0]
        self.assertEqual((order["location_id"], order["equipment_id"]), (kit, None))
        files = self.store.files("order", order["id"])
        self.assertEqual(sorted(f["filename"] for f in files), ["drain.png", "under-sink.png"])
        self.assertIn(f'src="/maintenance/files/{files[0]["id"]}?view=1"', page)
        shown = self.c.get(f"/maintenance/files/{files[0]['id']}?view=1")
        self.assertEqual((shown.mimetype, shown.headers["Content-Disposition"].split(";")[0]), ("image/png", "inline"))
        self.assertEqual(self.c.get(f"/maintenance/files/{files[0]['id']}").headers["Content-Disposition"].split(";")[0],
                         "attachment")
        # A "picture" that isn't one is only ever downloaded.
        self.store.add_file("order", order["id"], "fake.png", b"<script>alert(1)</script>", "x")
        fake = next(f for f in self.store.files("order", order["id"]) if f["filename"] == "fake.png")
        self.assertEqual(self.c.get(f"/maintenance/files/{fake['id']}?view=1").headers["Content-Disposition"]
                         .split(";")[0], "attachment")

        # Picking equipment puts the order where it's installed; whoever it's assigned to stays on edit.
        self.store.db.execute("UPDATE maint_orders SET assigned_to = 7 WHERE id = ?", (order["id"],))
        self._post(f"/maintenance/orders/{order['id']}/edit", title="Fix grinder", location_id=str(tor),
                   equipment_id=str(grinder))
        o = self.store.order(order["id"])
        self.assertEqual((o["location_id"], o["equipment_id"], o["assigned_to"]), (esp, grinder, 7))

    def test_deleting_a_location_takes_its_branch_and_keeps_history(self):
        gar, kit, esp = self._store_tree()
        bar = self._location("Bar", gar, code="GAR-BAR")
        machine = self._new_id(self._post("/maintenance/equipment/new", name="Espresso machine", location_id=str(esp)))
        self._post("/maintenance/tasks/new", name="Descale", frequency_days="30", next_due=TODAY.isoformat(),
                   equipment_id=str(machine))
        self._post("/maintenance/tasks/new", name="Mop kitchen", frequency_days="1", next_due=TODAY.isoformat(),
                   location_id=str(kit))
        req = self._new_id(self._post("/maintenance/requests/new", title="Leak under sink", location_id=str(kit)))
        order = self._new_id(self._post("/maintenance/orders/new", title="Fix sink", location_id=str(esp),
                                        due_date="2026-07-20"))
        page = self.c.get(f"/maintenance/locations/{kit}").get_data(as_text=True)
        self.assertIn("Delete Kitchen and everything under it", page)
        self.assertIn("Moved to storage, not deleted:</strong> 1 piece of equipment", page)
        self.assertIn("now shown under GAR · Gardena", page)
        c = self.store.location_contents(kit)
        self.assertEqual((c["below"], c["tasks"], c["requests"], c["orders"]), (1, 1, 1, 1))

        msg = self.c.post(f"/maintenance/locations/{kit}/delete", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Deleted Kitchen and the 1 location under it with 1 task. Moved 1 piece of equipment to storage.", msg)
        self.assertEqual({l["id"] for l in self.store.locations()}, {gar, bar})
        item = self.store.equipment(machine)
        self.assertEqual((item["status"], item["location_id"]), ("storage", None))
        self.assertEqual([t["name"] for t in self.store.tasks(TODAY, equipment_id=machine)], ["Descale"])
        self.assertEqual(self.store.request(req)["location"], "GAR · Gardena")
        self.assertEqual(self.store.order(order, TODAY)["location"], "GAR · Gardena")
        self.assertIn("a deleted location", self.c.get(f"/maintenance/equipment/{machine}").get_data(as_text=True))

        # At the top level the history is kept with no location.
        self.c.post(f"/maintenance/locations/{gar}/delete")
        self.assertEqual(self.store.locations(), [])
        self.assertIsNone(self.store.request(req)["location"])

    def test_requests_become_work_orders_with_costs(self):
        gar, kit, esp = self._store_tree()
        grinder = self._new_id(self._post("/maintenance/equipment/new", name="Grinder 2", location_id=str(esp),
                                          criticality="A"))
        self.assertIn("Say where it is", self.c.post("/maintenance/requests/new", data={"title": "Noise"}).get_data(as_text=True))
        req = self._new_id(self._post("/maintenance/requests/new", title="Grinder 2 won't start", equipment_id=str(grinder),
                                      breakdown="1", description="Clicks, then nothing"))
        r = self.store.request(req)
        self.assertEqual((r["number"], r["priority"], r["location"], r["status"]),
                         ("R-00001", 1, "GAR-KIT-ESP · Espresso station", "open"))  # machine down: very high
        quiet = self._new_id(self._post("/maintenance/requests/new", title="Drip tray loose", equipment_id=str(grinder)))
        self.assertEqual(self.store.request(quiet)["priority"], 2)  # criticality A: high
        overview = self.c.get("/maintenance/").get_data(as_text=True)
        self.assertIn("Machines down", overview)
        self.assertIn("1 machine down", overview)

        form = self.c.get(f"/maintenance/orders/new?request={req}").get_data(as_text=True)
        self.assertIn('value="Grinder 2 won&#39;t start"', form)
        order = self._new_id(self._post("/maintenance/orders/new", request=str(req), title="Replace grinder 2 motor",
                                        equipment_id=str(grinder), due_date="2026-07-14"))
        self.assertEqual(self.store.request(req)["status"], "ordered")
        self.assertIn(f'href="/maintenance/orders/{order}"', self.c.get("/maintenance/calendar?month=2026-07").get_data(as_text=True))
        o = self.store.order(order, TODAY)
        self.assertEqual((o["number"], o["priority"], o["late"], o["cost_center"]), ("WO-00001", 2, True, "4100"))
        self._post(f"/maintenance/orders/{order}/status", action="start")
        self._post(f"/maintenance/orders/{order}/costs", kind="labor", description="Mo", quantity="2", unit_cost="45")
        self._post(f"/maintenance/orders/{order}/costs", kind="part", description="Motor", quantity="1", unit_cost="310.50")
        self.assertIn("Choose labor", self.c.post(f"/maintenance/orders/{order}/costs", data={
            "kind": "x", "description": "?", "quantity": "1", "unit_cost": "1"}, follow_redirects=True).get_data(as_text=True))
        o = self.store.order(order, TODAY)
        self.assertEqual((o["cost"], o["labor_hours"], o["by_kind"]["Labor"]), (400.5, 2, 90))
        self._post(f"/maintenance/orders/{order}/status", action="complete", findings="Motor burnt out; replaced.")
        r = self.store.request(req)
        self.assertEqual((r["status"], r["resolution"]), ("closed", "Motor burnt out; replaced."))
        locked = self.c.post(f"/maintenance/orders/{order}/costs", data={"kind": "part", "description": "More",
                                                                      "quantity": "1", "unit_cost": "1"}, follow_redirects=True)
        self.assertIn("reopen it to change its costs", locked.get_data(as_text=True))
        history = self.c.get(f"/maintenance/history?start=2026-01-01&end=2030-12-31&location={gar}").get_data(as_text=True)
        for text in ("WO-00001 Replace grinder 2 motor", "$400.50", "4100", "Grinder 2"):
            self.assertIn(text, history)
        self.assertIn("$400.50", self.c.get(f"/maintenance/equipment/{grinder}").get_data(as_text=True))
        # Reopened and cancelled: the request is open again.
        self._post(f"/maintenance/orders/{order}/status", action="reopen")
        self.assertIn("Say why", self.c.post(f"/maintenance/orders/{order}/status", data={"action": "cancel"},
                                             follow_redirects=True).get_data(as_text=True))
        self._post(f"/maintenance/orders/{order}/status", action="cancel", findings="Under warranty: vendor fixing")
        self.assertEqual((self.store.request(req)["status"], self.store.request(req)["order_id"]), ("open", None))
        self._post(f"/maintenance/requests/{quiet}/close", outcome="closed", resolution="Tightened the screw")
        self.assertEqual(self.store.request(quiet)["status"], "closed")
        self.assertTrue(self.c.get("/maintenance/orders?status=all&format=csv").get_data(as_text=True).startswith("Number,Title"))
        cal = self.c.get("/maintenance/calendar?month=2026-07").get_data(as_text=True)
        self.assertNotIn(f'href="/maintenance/orders/{order}"', cal)  # cancelled: off the calendar

    def test_files_go_with_equipment_requests_and_orders(self):
        import io
        gar, kit, esp = self._store_tree()
        machine = self._new_id(self._post("/maintenance/equipment/new", name="Brewer", location_id=str(esp)))
        self.c.post(f"/maintenance/files/equipment/{machine}", data={"file": (io.BytesIO(b"%PDF manual"), "Manual.pdf")},
                    content_type="multipart/form-data")
        files = self.store.equipment(machine)["files"]
        self.assertEqual(files[0]["filename"], "Manual.pdf")
        self.assertEqual(self.c.get(f"/maintenance/files/{files[0]['id']}").data, b"%PDF manual")
        req = self.c.post("/maintenance/requests/new", data={"title": "Leaking", "equipment_id": str(machine),
                                                             "file": (io.BytesIO(b"\x89PNG"), "leak.png")},
                          content_type="multipart/form-data")
        request_id = self._new_id(req)
        self.assertEqual(self.store.request(request_id)["files"][0]["filename"], "leak.png")
        self.c.post(f"/maintenance/files/{files[0]['id']}/delete")
        self.assertEqual(self.store.equipment(machine)["files"], [])


class RefusingTransport(WsgiTransport):
    """Mock transport that answers 403 for some endpoints, like an API user without access."""

    def __init__(self, app, refused):
        super().__init__(app)
        self.refused = refused

    def get(self, path, query_string, headers):
        if path.split("/")[1] in self.refused:
            return 403, '{"description": "Forbidden"}'
        return super().get(path, query_string, headers)


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

    def test_production_from_store_matches_production_from_api(self):
        from unleashed.production import Production
        stored = Production(self._app().extensions["unleashed"]["reports"])
        direct = Production(Reports(UnleashedClient(MOCK_API_ID, MOCK_API_KEY, WsgiTransport(self.mock)),
                                    today_fn=lambda: TODAY))
        a, b = stored.requirements(), direct.requirements()
        self.assertEqual([o["number"] for o in a["orders"]], [o["number"] for o in b["orders"]])
        self.assertAlmostEqual(a["planned_cost"], b["planned_cost"], places=6)
        start = add_months(TODAY.replace(day=1), -2)
        self.assertAlmostEqual(stored.usage(start, TODAY)["cost"], direct.usage(start, TODAY)["cost"], places=6)
        self.assertEqual([x["unit_cost"] for x in stored.boms()], [x["unit_cost"] for x in direct.boms()])

    def test_existing_copy_gets_every_assembly_on_its_next_sync(self):
        """A database copied before production was added gets all assemblies, not just recent edits."""
        app = self._app()
        store, syncer = app.extensions["unleashed"]["store"], app.extensions["unleashed"]["syncer"]
        stored = store.document_count("Assemblies")
        self.assertEqual(stored, len(self.ds.assemblies))
        self.app_db.execute("DELETE FROM ul_documents WHERE kind = 'Assemblies'")
        self.app_db.execute("DELETE FROM ul_sync_state WHERE key = 'window:Assemblies'")
        self.assertFalse(syncer.sync()["full"])
        self.assertEqual(store.document_count("Assemblies"), stored)
        self.assertNotIn("modifiedSince", self._api_log("/Assemblies/1?%")[-1][1])
        syncer.sync()
        self.assertIn("modifiedSince=", self._api_log("/Assemblies/1?%")[-1][1])

    def test_sync_carries_on_without_production_access(self):
        app = self._app(UNLEASHED_TRANSPORT=RefusingTransport(self.mock, {"Assemblies", "BillOfMaterials"}))
        store = app.extensions["unleashed"]["store"]
        self.assertEqual(store.last_run()["status"], "ok")
        self.assertIn("Forbidden", store.get_state("unavailable:Assemblies"))
        c = app.test_client()
        self.assertIn("COGS spend", c.get("/").get_data(as_text=True))
        self.assertIn("return production data", c.get("/production").get_data(as_text=True))
        self.assertIn("Not available", c.get("/connection").get_data(as_text=True))

    def test_pages_make_no_api_calls_once_synced(self):
        app = self._app()
        before = len(self._api_log())
        c = app.test_client()
        for path in ("/", "/cogs", "/stock-on-hand", "/stock-movement",
                     "/purchases", "/sales", "/adjustments", "/transactions", "/production",
                     "/production/orders", "/production/requirements", "/production/boms",
                     "/production/boms/BOM-CB", "/production/usage", "/production/orders/ASM-00000001"):
            self.assertEqual(c.get(path).status_code, 200, path)
        self.assertEqual(len(self._api_log()), before)

    def test_stored_dates_are_worked_out_again_in_the_business_time_zone(self):
        self._app()  # synced, dates read in UTC
        store = LocalStore(self.app_db)
        self.assertEqual(store.get_state("dates_read_as"), "UTC")
        self._app(SYNC_ON_STARTUP="off", TIMEZONE="America/Los_Angeles")
        self.assertEqual(store.get_state("dates_read_as"), "America/Los_Angeles")
        self.assertEqual(store.recompute_dates("America/Los_Angeles"), 0)  # once, not on every start
        self._app(SYNC_ON_STARTUP="off")  # back to UTC for the tests after this one

    def test_the_mock_stock_is_the_end_of_the_day(self):
        app = self._app()
        reports = app.extensions["unleashed"]["reports"]
        self.assertIsNotNone(app.extensions["unleashed"]["store_client"].current_stock_at())  # COGS lines documents up with it
        self.assertEqual(reports.stock_timing()["verdict"], "end")
        page = app.test_client().get("/settings/cogs").get_data(as_text=True)
        self.assertIn("✓ Checked against the last", page)
        self.assertIn('name="stock_as_at" value="end" id="cut-end" checked', page)
        app.test_client().post("/settings/cogs", data={"stock_as_at": "start"})
        self.assertEqual(reports.cogs_settings()["stock_as_at"], "start")
        self.assertIn("Choose it above and save", app.test_client().get("/settings/cogs").get_data(as_text=True))

    def test_orders_completed_or_deleted_in_unleashed_catch_up(self):
        app = self._app()
        ext = app.extensions["unleashed"]
        store, c = ext["store"], app.test_client()

        def status(asm):
            return next((s for _, n, s in store.document_statuses("Assemblies") if n == asm["AssemblyNumber"]), None)

        parked = [a for a in self.ds.assemblies if a["Status"] == "Parked"]
        # Deleted in Unleashed: asking it for what changed can't show that, so every sync
        # re-reads the orders still open here.
        gone = parked[0]
        self.ds.assemblies.remove(gone)
        self.assertEqual(status(gone), "Parked")
        result = ext["syncer"].sync(full=False)
        self.assertEqual(result["counts"]["open production orders"], "1 of 4 updated")
        self.assertIsNone(status(gone))
        self.assertEqual(c.get(f"/production/orders/{gone['AssemblyNumber']}").status_code, 404)

        # Completed in Unleashed without it showing as changed: the order's button checks now.
        done = next(a for a in parked[1:] if self.ds.complete(a) is None)  # one there's stock for
        done["Modified"] = TODAY - dt.timedelta(days=60)
        self.assertIn("Check with Unleashed", c.get(f"/production/orders/{done['AssemblyNumber']}").get_data(as_text=True))
        page = c.post("/production/orders/check", data={"number": done["AssemblyNumber"]},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn(f"{done['AssemblyNumber']} is completed in Unleashed (it was parked here)", page)
        self.assertEqual(status(done), "Completed")
        self.assertNotIn("Check with Unleashed", page)  # done: nothing left to check
        self.assertEqual(store.get_state("recheck_from"), "")  # the sync it started fetched its stock days

        # A check outside a sync marks the days whose stock changed, for the next sync.
        other = next(a for a in parked[1:] if a is not done and self.ds.complete(a) is None)
        other["Modified"] = TODAY - dt.timedelta(days=60)
        self.assertEqual(len(ext["syncer"].refresh_open_assemblies()["changes"]), 1)
        self.assertLessEqual(store.get_state("recheck_from"), TODAY.isoformat())

        page = c.get("/production/orders").get_data(as_text=True)
        self.assertIn("Check open orders with Unleashed", page)
        page = c.post("/production/orders/check", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Checked 1 open order with Unleashed: it is still open there.", page)

    def test_cogs_is_split_by_store(self):
        app = self._app()
        c = app.test_client()
        reports = app.extensions["unleashed"]["reports"]
        page = c.get("/cogs").get_data(as_text=True)
        self.assertIn("No stores chosen yet", page)
        self.assertIn("Settings → COGS", page)  # stores are set up there, for everyone
        self.assertIn('value="GDN-POS"', c.get("/settings/cogs").get_data(as_text=True))
        res = c.post("/settings/cogs", data={"store": ["GDN-POS", "WHSL-01", "NOT-A-CUSTOMER"]})
        self.assertEqual(res.headers["Location"], "/settings/cogs")
        self.assertEqual(reports.cogs_settings()["stores"], ["GDN-POS", "WHSL-01"])

        month = add_months(TODAY.replace(day=1), -1)
        cogs = reports.cogs(month)
        split = reports.cogs_by_store(cogs)
        sales = [o for o in reports.sales_orders(month - dt.timedelta(days=7), month_end(month), status="Completed")
                 if month <= (o["completed_date"] or o["order_date"]) <= month_end(month)]
        for row in split["stores"]:
            mine = [o for o in sales if o["customer_code"] == row["code"]]
            self.assertEqual(row["orders"], len(mine))
            self.assertAlmostEqual(row["cost"], sum(o["cost"] for o in mine), places=6)  # at cost, not price
        self.assertEqual([r["name"] for r in split["stores"]], ["Gardena POS (daily sales)", "Mock Wholesale Cafe"])
        self.assertGreater(split["other_orders"], 0)  # the catering client isn't a store
        self.assertAlmostEqual(split["sent"] + split["other_customers"] + split["written_off"] + split["rest"],
                               cogs["cogs"], places=6)
        self.assertLess(abs(split["rest"]), 1.0)  # the mock's orders and write-offs explain its COGS

        page = c.get(f"/cogs?month={month:%Y-%m}").get_data(as_text=True)
        for text in ("Gardena POS (daily sales)", "Sent to stores", "Other customers", "Stock written off",
                     "Not on an order or write-off", "Not sent to a store"):
            self.assertIn(text, page)
        csv_text = c.get(f"/cogs?month={month:%Y-%m}&format=csv&by=store").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("Store,Customer code,Orders,Cost of what was sent,Share of COGS"))
        self.assertIn("Gardena POS (daily sales),GDN-POS,", csv_text)
        self.assertIn(f"COGS,,,{round(cogs['cogs'], 2)},1", csv_text)

        # What "not on an order or write-off" is made of: the product differences add up to it.
        why = reports.unexplained(cogs)
        self.assertAlmostEqual(why["gap"], split["rest"], places=4)
        # It's on a page of its own, with the purchases counted; the COGS page links to it.
        breakdown = f"/cogs/not-on-an-order?month={month:%Y-%m}"
        self.assertIn(f'href="{breakdown}">What', page)
        for text in ('id="unexplained"', "Purchases counted"):
            self.assertNotIn(text, page)
        own = c.get(breakdown).get_data(as_text=True)
        for text in ('id="unexplained"', "Nothing to explain", 'id="purchases"', "Purchases counted in"):
            self.assertIn(text, own)
        csv_text = c.get(f"{breakdown}&format=csv").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("Product Code,Description,Opening,Received,Production,Sent or sold"))
        old_link = c.get(f"/cogs?month={month:%Y-%m}&format=csv&by=unexplained")  # still works
        self.assertEqual(old_link.headers["Location"], f"{breakdown}&format=csv")

        # Picking one store: what was sent to it, by product and order.
        page = c.get(f"/cogs?month={month:%Y-%m}").get_data(as_text=True)
        self.assertIn('<option value="WHSL-01" >Mock Wholesale Cafe</option>', page)
        self.assertNotIn('value="CATER-01"', page)  # only the stores Settings names
        sent = reports.sent_to(cogs, "WHSL-01")
        mine = [o for o in sales if o["customer_code"] == "WHSL-01"]
        self.assertEqual(len(sent["orders"]), len(mine))
        self.assertAlmostEqual(sent["cost"], sum(o["cost"] for o in mine), places=6)
        self.assertAlmostEqual(sum(p["cost"] for p in sent["products"]), sent["cost"], places=6)
        page = c.get(f"/cogs?month={month:%Y-%m}&store=WHSL-01").get_data(as_text=True)
        self.assertIn(f"Sent to Mock Wholesale Cafe, {month:%B %Y}", page)
        self.assertIn(f"{len(mine)} completed orders", page)
        self.assertIn(mine[0]["number"], page)
        csv_text = c.get(f"/cogs?month={month:%Y-%m}&store=WHSL-01&format=csv&by=sent").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("Product Code,Description,Qty,Cost"))
        self.assertNotIn('id="sent"', c.get(f"/cogs?month={month:%Y-%m}&store=NOPE").get_data(as_text=True))

    def test_cogs_counts_stock_in_the_locations_chosen(self):
        for code, name in (("CS", "Cold Storage"), ("TOR", "Torrance Store")):
            self.ds.warehouses.append({"Guid": f"wh-{code}", "WarehouseCode": code, "WarehouseName": name,
                                       "IsDefault": False})
        app = self._app()
        c = app.test_client()
        ext = app.extensions["unleashed"]
        reports, store = ext["reports"], ext["store"]
        before = reports.cogs(TODAY)
        self.assertIsNone(before["warehouses"])
        # The main warehouse and cold storage (the mock keeps all its stock in the main one):
        # COGS is the same as counting every warehouse, from their own snapshots.
        page = c.post("/settings/cogs", data={"location": ["GDN", "CS", "NOPE"]},
                      follow_redirects=True).get_data(as_text=True)
        self.assertIn("COGS counts stock in Gardena Store and Cold Storage", page)
        self.assertEqual(reports.cogs_settings()["warehouses"], ["GDN", "CS"])  # the sync ran (no scheduler here)
        kept = {r["as_at"] for r in self.app_db.query("SELECT as_at FROM ul_stock_snapshots")}
        for code in ("GDN", "CS"):
            self.assertIn(snapshot_key(TODAY.replace(day=1).isoformat(), code), kept)
        both = reports.cogs(TODAY)
        self.assertAlmostEqual(both["cogs"], before["cogs"], places=4)
        self.assertEqual(set(both["opening_by"]), {"GDN", "CS"})
        self.assertEqual(both["closing_by"]["CS"], 0)
        page = c.get("/cogs").get_data(as_text=True)
        for text in ("in Gardena Store and Cold Storage", "Inventory by location"):
            self.assertIn(text, page)

        cold = reports.cogs(TODAY, warehouses=["CS"])  # stock outside the locations counted is used
        self.assertEqual((cold["opening"], cold["closing"]), (0, 0))
        self.assertAlmostEqual(cold["cogs"], cold["purchases"], places=4)

        # A location added later is counted once its history has downloaded.
        store.set_state("cogs_warehouse", json.dumps(["GDN", "CS", "TOR"]))
        ext["store_client"].clear_cache()
        self.assertEqual(reports.cogs_settings()["pending"], ["GDN", "CS", "TOR"])
        self.assertIn("once their stock", c.get("/cogs").get_data(as_text=True))
        ext["syncer"].sync()
        self.assertEqual(reports.cogs_settings()["warehouses"], ["GDN", "CS", "TOR"])
        # A setting saved before locations were a list (one code) still counts.
        store.set_state("cogs_warehouse", "GDN")
        ext["store_client"].clear_cache()
        self.assertEqual(reports.cogs_settings()["warehouses"], ["GDN"])
        ext["syncer"].sync()
        kept = {r["as_at"] for r in self.app_db.query("SELECT as_at FROM ul_stock_snapshots")}
        self.assertFalse(any(k.endswith(("@CS", "@TOR")) for k in kept))  # the others' are gone
        # Every page still reads from the copy.
        logged = len(self.app_db.query("SELECT id FROM unleashed_api_log"))
        for path in ("/", "/cogs", f"/cogs?month={add_months(TODAY.replace(day=1), -1):%Y-%m}"):
            self.assertEqual(c.get(path).status_code, 200, path)
        self.assertEqual(len(self.app_db.query("SELECT id FROM unleashed_api_log")), logged)

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
        lists = [q for e, q in [row[:2] for row in self._api_log("/PurchaseOrders/%")] if not e.endswith("/Costs")]
        self.assertIn("modifiedSince=", lists[-1])
        # Only the changed order's costs were fetched again.
        self.assertEqual(len(self._api_log(f"/PurchaseOrders/{po['Guid']}/Costs*")), 2)
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

    def _access(self, profile):
        """The Team page's Access value for a profile name, or "admin"."""
        if profile == "admin":
            return "admin"
        return str(next(p["id"] for p in self.app.extensions["profiles"].all() if p["name"] == profile))

    def _add(self, admin, name, email, profile="Standard"):
        import re
        html = admin.post("/team/add", data={"name": name, "email": email, "access": self._access(profile)}
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
        self.assertIn(">Settings<", page)
        self.assertNotIn(">API log<", page)  # inside Settings, not the main tabs
        settings = c.get("/connection").get_data(as_text=True)
        for tab in (">Connection<", ">Team<", ">API log<"):
            self.assertIn(tab, settings)
        self.assertIn(">Team<", c.get("/team").get_data(as_text=True))
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
        for tab in (">Settings<", ">Team<", ">Connection<", ">API log<"):
            self.assertNotIn(tab, page)
        self.assertEqual(bea.get("/team").status_code, 403)
        self.assertEqual(bea.get("/connection").status_code, 403)
        self.assertEqual(bea.get("/api-log").status_code, 403)
        self.assertEqual(bea.post("/connection", data={"action": "check"}).status_code, 403)
        self.assertEqual(bea.post("/team/add", data={"name": "x", "email": "x@y.z"}).status_code, 403)
        self.assertIn("already an account", admin.post(
            "/team/add", data={"name": "Again", "email": "BEA@yeems.test", "access": self._access("Standard")}
        ).get_data(as_text=True))

    def test_members_record_production_steps_but_only_admins_set_them_up(self):
        admin = self._admin_client()
        temp = self._add(admin, "Barista Bea", "bea@yeems.test")
        bea = self.app.test_client()
        self._sign_in(bea, "bea@yeems.test", temp)
        self._choose_password(bea, temp, "bea-own-password")
        self.assertIn("Write the procedure", admin.get("/production/boms/BOM-VSC").get_data(as_text=True))
        self.assertNotIn("Write the procedure", bea.get("/production/boms/BOM-VSC").get_data(as_text=True))
        self.assertEqual(bea.get("/production/boms/BOM-VSC/procedure").status_code, 403)
        self.assertEqual(bea.post("/production/boms/BOM-VSC/procedure", data={"rows": 1, "name-0": "x"}).status_code, 403)
        admin.post("/production/boms/BOM-VSC/procedure", data={
            "rows": 2, "name-0": "Mix", "components-0": ["CRM-HVY-HG", "MILK-WHL-GAL", "SYR-VAN-750"]})
        ds = self.app.extensions["unleashed"]["client"].transport.app.config["DATASET"]
        number = next(a["AssemblyNumber"] for a in ds.assemblies
                      if a["Status"] == "Parked" and a["ProductCode"] == "VSC-HG")
        page = bea.post(f"/production/orders/{number}/steps/0", follow_redirects=True, data={
            "qty:CRM-HVY-HG": "2", "qty:MILK-WHL-GAL": "1", "qty:SYR-VAN-750": "0.8"}).get_data(as_text=True)
        self.assertIn("Saved step 1", page)
        self.assertIn("✓ Barista Bea", page)
        self.assertIn("Complete order", page)

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
                      admin.post(f"/team/{me}", data={"action": "access", "access": self._access("Standard")}
                                 ).get_data(as_text=True))
        from accounts import AccountError
        with self.assertRaises(AccountError):
            self.app.extensions["users"].set_role(me, "member")
        self.assertEqual(admin.get("/").status_code, 200)

    def test_existing_members_keep_their_tabs_when_profiles_arrive(self):
        admin = self._admin_client()
        self._add(admin, "Barista Bea", "bea@yeems.test")
        db = self.app.extensions["db"]
        db.execute("DELETE FROM app_user_profiles")  # as it was before profiles existed
        db.execute("DELETE FROM access_profiles")
        restarted = self._app()
        profiles = restarted.extensions["profiles"]
        self.assertEqual([p["name"] for p in profiles.all()], ["Maintenance technician", "Standard"])
        bea = restarted.extensions["users"].by_email("bea@yeems.test")
        self.assertEqual(profiles.areas_of(bea), {"finance", "warehouse", "production"})
        owner = restarted.extensions["users"].by_email("owner@yeems.test")
        self.assertEqual(profiles.areas_of(owner), {"finance", "warehouse", "production", "maintenance", "maint_requests"})

    def test_profiles_decide_which_tabs_each_person_opens(self):
        admin = self._admin_client()
        page = admin.get("/settings/profiles").get_data(as_text=True)
        for text in ("Standard", "Maintenance technician", "Add a profile"):
            self.assertIn(text, page)
        admin.post("/settings/profiles", data={"name": "  Shift   lead ", "areas": ["warehouse", "maintenance"]})
        self.assertIn("already a profile called Shift lead",
                      admin.post("/settings/profiles", data={"name": "shift LEAD"}).get_data(as_text=True))
        temp = self._add(admin, "Mechanic Mo", "mo@yeems.test", profile="Maintenance technician")
        mo = self.app.test_client()
        self._sign_in(mo, "mo@yeems.test", temp)
        self._choose_password(mo, temp, "mo-own-password1")

        # Maintenance only: the home page opens Maintenance, and nothing from Unleashed shows.
        self.assertEqual(mo.get("/").headers["Location"], "/maintenance/")
        home = mo.get("/maintenance/").get_data(as_text=True)
        self.assertIn(">Maintenance<", home)
        for text in (">Finance<", ">Warehouse<", ">Production<", ">Settings<", "Sync now", "Synced"):
            self.assertNotIn(text, home)
        for path in ("/cogs", "/stock-on-hand", "/transactions", "/production", "/production/orders/new",
                     "/purchases/PO-00000001"):
            res = mo.get(path)
            self.assertEqual(res.status_code, 403, path)
            self.assertIn("No access to", res.get_data(as_text=True))
        self.assertEqual(mo.post("/production/orders/ASM-1/steps/0").status_code, 403)
        self.assertEqual(mo.get("/settings/profiles").status_code, 403)

        # The admin gives Mo the Shift lead profile: Warehouse and Maintenance.
        mo_id = self.app.extensions["users"].by_email("mo@yeems.test")["id"]
        changed = admin.post(f"/team/{mo_id}", data={"action": "access", "access": self._access("Shift lead")})
        self.assertIn("Mechanic Mo now has Shift lead access", changed.get_data(as_text=True))
        self.assertEqual(mo.get("/").headers["Location"], "/stock-on-hand")
        self.assertEqual(mo.get("/stock-on-hand").status_code, 200)
        self.assertEqual(mo.get("/cogs").status_code, 403)
        self.assertIn("Synced", mo.get("/stock-on-hand").get_data(as_text=True))

        # A profile that opens nothing; and profiles in use can't be deleted.
        admin.post("/settings/profiles", data={"name": "Nothing yet"})
        admin.post(f"/team/{mo_id}", data={"action": "access", "access": self._access("Nothing yet")})
        self.assertIn("doesn&#39;t open any tabs yet", mo.get("/", follow_redirects=True).get_data(as_text=True))
        shift_lead = int(self._access("Shift lead"))
        nothing = int(self._access("Nothing yet"))
        self.assertIn("is given to 1 person", admin.post(f"/settings/profiles/{nothing}/delete",
                                                         follow_redirects=True).get_data(as_text=True))
        admin.post(f"/settings/profiles/{shift_lead}/delete")
        self.assertNotIn("Shift lead", [p["name"] for p in self.app.extensions["profiles"].all()])
        # Admin access, then back: the last admin rule still holds.
        admin.post(f"/team/{mo_id}", data={"action": "access", "access": "admin"})
        self.assertIn(">Settings<", mo.get("/connection").get_data(as_text=True))

    def test_every_page_belongs_to_a_tab_or_is_general(self):
        import dashboard
        settings = {"dash.connection", "dash.api_log", "dash.team", "dash.team_add", "dash.team_update",
                    "dash.profiles", "dash.profiles_save", "dash.profiles_delete", "dash.cogs_setup"}
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.split(".")[0] not in ("dash", "maint"):
                continue
            self.assertTrue(dashboard.area_of(rule.endpoint) or rule.endpoint in dashboard._GENERAL_ENDPOINTS
                            or rule.endpoint in settings, f"{rule.endpoint} isn't in a tab")

    def test_maintenance_people_tasks_and_deleting(self):
        admin = self._admin_client()
        temp = self._add(admin, "Mechanic Mo", "mo@yeems.test", profile="Maintenance technician")
        self._add(admin, "Barista Bea", "bea@yeems.test")  # Standard: no Maintenance
        mo = self.app.test_client()
        self._sign_in(mo, "mo@yeems.test", temp)
        self._choose_password(mo, temp, "mo-own-password1")
        users = self.app.extensions["users"]
        mo_id, bea_id = users.by_email("mo@yeems.test")["id"], users.by_email("bea@yeems.test")["id"]

        form = mo.get("/maintenance/tasks/new").get_data(as_text=True)
        self.assertIn(">Mechanic Mo<", form)
        self.assertNotIn(">Barista Bea<", form)  # can't open Maintenance, so can't be assigned
        refused = mo.post("/maintenance/tasks/new", data={"name": "Filter", "frequency_days": "30",
                                                          "next_due": "2026-10-01", "assigned_to": str(bea_id)})
        self.assertIn("someone who can open Maintenance", refused.get_data(as_text=True))
        res = mo.post("/maintenance/tasks/new", data={"name": "Replace water filter", "frequency_days": "30",
                                                      "next_due": "2026-01-01", "assigned_to": str(mo_id)})
        task_id = int(res.headers["Location"].rsplit("/", 1)[1])
        mine = mo.get("/maintenance/").get_data(as_text=True)
        self.assertIn("Assigned to you", mine)
        self.assertIn("Replace water filter", mine)
        workload = mo.get("/maintenance/workload").get_data(as_text=True)
        self.assertIn("Mechanic Mo", workload)
        self.assertNotIn("Barista Bea", workload)
        self.assertEqual(self.app.test_client().get("/maintenance/").status_code, 302)  # signed out
        bea = self.app.test_client()
        bea_temp = admin.post(f"/team/{bea_id}", data={"action": "reset"}).get_data(as_text=True)
        import re
        bea_temp = re.search(r'class="temp-password mono">([^<]+)<', bea_temp).group(1)
        self._sign_in(bea, "bea@yeems.test", bea_temp)
        self._choose_password(bea, bea_temp, "bea-own-password1")
        self.assertEqual(bea.get(f"/maintenance/tasks/{task_id}").status_code, 403)

        # Recording work names the person; only admins delete.
        mo.post(f"/maintenance/tasks/{task_id}/complete", data={"status": "completed", "notes": "Done"})
        self.assertIn("Mechanic Mo", mo.get("/maintenance/history?start=2026-01-01").get_data(as_text=True))
        self.assertIn("Only admins can delete", mo.post(f"/maintenance/tasks/{task_id}/delete",
                                                        follow_redirects=True).get_data(as_text=True))
        self.assertIsNotNone(self.app.extensions["maintenance"].task(task_id, dt.date.today()))
        admin.post(f"/maintenance/tasks/{task_id}/delete")
        self.assertIsNone(self.app.extensions["maintenance"].task(task_id, dt.date.today()))

    def test_cogs_settings_are_for_admins_and_finance_users_filter_locations(self):
        admin = self._admin_client()
        temp = self._add(admin, "David", "david@yeems.test")  # Standard: Finance, Warehouse, Production
        david = self.app.test_client()
        self._sign_in(david, "david@yeems.test", temp)
        self._choose_password(david, temp, "david-own-password")
        page = david.get("/cogs").get_data(as_text=True)
        self.assertIn("By store", page)
        self.assertIn("by an admin, under Settings → COGS", page)
        self.assertNotIn('href="/settings/cogs"', page)
        # The settings change everyone's COGS, so they're behind Settings, for admins.
        self.assertEqual(david.get("/settings/cogs").status_code, 403)
        self.assertEqual(david.post("/settings/cogs", data={"store": ["GDN-POS"]}).status_code, 403)
        self.assertEqual(self.app.extensions["unleashed"]["reports"].cogs_settings()["stores"], [])
        self.assertIn(">COGS<", admin.get("/settings/cogs").get_data(as_text=True))
        admin.post("/settings/cogs", data={"store": ["GDN-POS"]})
        self.assertEqual(self.app.extensions["unleashed"]["reports"].cogs_settings()["stores"], ["GDN-POS"])
        self.assertIn("Gardena POS (daily sales)", david.get("/cogs").get_data(as_text=True))
        # Without Finance, not even the page.
        temp = self._add(admin, "Mechanic Mo", "mo@yeems.test", profile="Maintenance technician")
        mo = self.app.test_client()
        self._sign_in(mo, "mo@yeems.test", temp)
        self._choose_password(mo, temp, "mo-own-password1")
        self.assertEqual(mo.get("/cogs").status_code, 403)

    def test_people_who_only_report_problems(self):
        admin = self._admin_client()
        admin.post("/settings/profiles", data={"name": "Barista", "areas": ["maint_requests"]})
        temp = self._add(admin, "Barista Bea", "bea@yeems.test", profile="Barista")
        bea = self.app.test_client()
        self._sign_in(bea, "bea@yeems.test", temp)
        self._choose_password(bea, temp, "bea-own-password1")
        self.assertEqual(bea.get("/").headers["Location"], "/maintenance/requests")
        page = bea.get("/maintenance/requests").get_data(as_text=True)
        self.assertIn(">Maintenance<", page)
        self.assertIn(">Requests<", page)
        for tab in (">Work orders<", ">Tasks<", ">Equipment<", ">Overview<"):
            self.assertNotIn(tab, page)
        self.assertEqual(bea.get("/maintenance/").headers["Location"], "/maintenance/requests")
        admin.post("/maintenance/locations", data={"name": "Gardena"})
        loc = self.app.extensions["maintenance"].locations()[0]["id"]
        res = bea.post("/maintenance/requests/new", data={"title": "Fridge warm", "location_id": str(loc)})
        req = int(res.headers["Location"].rsplit("/", 1)[1])
        mine = bea.get(f"/maintenance/requests/{req}").get_data(as_text=True)
        self.assertIn("Fridge warm", mine)
        self.assertNotIn("Make a work order", mine)
        for path in ("/maintenance/orders", "/maintenance/tasks", "/maintenance/equipment", f"/maintenance/orders/new?request={req}"):
            self.assertEqual(bea.get(path).status_code, 403, path)
        self.assertEqual(bea.post(f"/maintenance/requests/{req}/close", data={"outcome": "rejected", "resolution": "x"}).status_code, 403)
        import io
        bea.post(f"/maintenance/files/request/{req}", data={"file": (io.BytesIO(b"GIF89a"), "fridge.gif")},
                 content_type="multipart/form-data")
        self.assertEqual(len(self.app.extensions["maintenance"].request(req)["files"]), 1)
        # The maintenance team sees it and deals with it.
        self.assertIn("Make a work order", admin.get(f"/maintenance/requests/{req}").get_data(as_text=True))

    def test_admin_reset_restores_access(self):
        self._admin_client()
        recovered = self._app(ADMIN_PASSWORD="recovery-pass-1", ADMIN_RESET=True)
        c = recovered.test_client()
        self.assertEqual(self._sign_in(c, "owner@yeems.test", "recovery-pass-1").headers["Location"], "/account")
        self._choose_password(c, "recovery-pass-1", "owner-new-password")
        # ADMIN_RESET left set: a restart doesn't undo the password chosen after the reset.
        restarted = self._app(ADMIN_PASSWORD="recovery-pass-1", ADMIN_RESET=True)
        c = restarted.test_client()
        self.assertIn("don&#39;t match", self._sign_in(c, "owner@yeems.test", "recovery-pass-1").get_data(as_text=True))
        self.assertEqual(self._sign_in(c, "owner@yeems.test", "owner-new-password").headers["Location"], "/")
        # A new reset password is a new reset.
        again = self._app(ADMIN_PASSWORD="recovery-pass-2", ADMIN_RESET=True)
        self.assertEqual(self._sign_in(again.test_client(), "owner@yeems.test", "recovery-pass-2").headers["Location"],
                         "/account")

    def test_https_only_when_secure_cookies_are_on(self):
        app = self._app(SESSION_COOKIE_SECURE=True)
        c = app.test_client()
        res = c.get("/cogs?month=2026-01", headers={"X-Forwarded-Proto": "http"})
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res.headers["Location"], "https://localhost/cogs?month=2026-01")
        self.assertEqual(c.post("/login", headers={"X-Forwarded-Proto": "http"}).status_code, 308)
        self.assertEqual(c.get("/healthz").status_code, 200)  # load balancer health checks
        self.assertEqual(c.get("/login", headers={"X-Forwarded-Proto": "https"}).status_code, 200)
        self.assertEqual(self.app.test_client().get("/login", headers={"X-Forwarded-Proto": "http"}).status_code, 200)

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
            'password authentication failed for user "unleashed_admin"': 'user "unleashed_admin"',
            'connection failed: FATAL:  password authentication failed for user "postgres"':
                "without DB_USER the app uses postgres",
            'connection failed: FATAL:  database "unleashedaccess" does not exist': "no database with that name",
            'connection failed: connection to server at "10.0.1.5", port 5432 failed: timeout expired':
                "can't reach the database",
            "couldn't get a connection after 30.00 sec": "can't reach the database",
            'connection failed: connection to server at "172.31.30.106", port 5432 failed: '
            'fe_sendauth: no password supplied': "DB_PASSWORD",
            'connection failed: connection to server at "172.31.30.106", port 5432 failed: FATAL:  '
            'role "unleashed_admn" does not exist': "DB_USER",
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

    @unittest.skipUnless(TEST_DATABASE_URL, "needs TEST_DATABASE_URL (PostgreSQL)")
    def test_missing_database_is_created(self):
        import psycopg
        from urllib.parse import urlsplit, urlunsplit

        from unleashed.db import Database
        name = "ua_autocreate_test"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name}")
        url = urlunsplit(urlsplit(TEST_DATABASE_URL)._replace(path=f"/{name}"))
        try:
            first = Database(url)
            self.assertEqual(first.created_database, name)
            first.close()
            second = Database(url)
            self.assertIsNone(second.created_database)
            second.close()
        finally:
            with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")

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
