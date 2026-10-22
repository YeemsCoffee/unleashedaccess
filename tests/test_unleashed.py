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
        for path in ("/unleashed/", "/unleashed/cogs", "/unleashed/stock-on-hand",
                     "/unleashed/stock-valuation", "/unleashed/stock-movement",
                     "/unleashed/purchases", "/unleashed/purchases/PO-00000001",
                     "/unleashed/sales", "/unleashed/sales/SO-00000001",
                     "/unleashed/adjustments", "/unleashed/transactions",
                     "/unleashed/api-log", "/unleashed/connection"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertNotIn("Couldn't load data", res.get_data(as_text=True), path)

    def test_csv_export(self):
        res = self.client.get("/unleashed/stock-on-hand?format=csv")
        self.assertEqual(res.mimetype, "text/csv")
        self.assertTrue(res.get_data(as_text=True).startswith("Product Code,"))

    def test_api_calls_are_logged(self):
        self.client.get("/unleashed/stock-on-hand")
        html = self.client.get("/unleashed/api-log").get_data(as_text=True)
        self.assertIn("/StockOnHand/1", html)


if __name__ == "__main__":
    unittest.main()
