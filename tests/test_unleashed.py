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
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from app import create_app
        self.app = create_app({"DATABASE_PATH": self.db_path, "TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

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
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from app import create_app
        self.app = create_app({
            "DATABASE_PATH": self.db_path, "TESTING": True, "UNLEASHED_MODE": "live",
            "UNLEASHED_API_ID": MOCK_API_ID, "UNLEASHED_API_KEY": MOCK_API_KEY,
            "UNLEASHED_TRANSPORT": WsgiTransport(create_mock_app()),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

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
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: [os.path.exists(self.db_path + x) and os.unlink(self.db_path + x)
                                 for x in ("", "-wal", "-shm")])
        from app import create_app
        app = create_app({
            "DATABASE_PATH": self.db_path, "TESTING": True, "UNLEASHED_MODE": "live",
            "UNLEASHED_API_ID": MOCK_API_ID, "UNLEASHED_API_KEY": MOCK_API_KEY,
            "UNLEASHED_TRANSPORT": RewritingTransport(create_mock_app(today=TODAY), rewrite),
        })
        app.extensions["unleashed"]["reports"].today_fn = lambda: TODAY
        return app

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
