"""Tests for the scan engine and API. Run with: python -m pytest tests/ -q"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402


class ScanTrackerTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app({
            "DATABASE_PATH": self.db_path,
            "DUPLICATE_SCAN_SECONDS": 0,  # off by default; enabled per-test
            "TESTING": True,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def scan(self, code, **extra):
        return self.client.post("/api/scan", json={"code": code, **extra}).get_json()

    def container_action(self, code, action):
        return self.client.post(
            "/api/container-action", json={"container_code": code, "action": action}
        ).get_json()

    def counts(self):
        data = self.client.get("/api/summary").get_json()
        return {c["display_name"]: c["current_count"] for c in data["counts"]}

    # --- raw ingredients ---------------------------------------------------

    def test_raw_scan_decrements_count(self):
        self.client.post("/api/adjust", json={"item_id": 1, "new_count": 30, "note": "start"})
        res = self.scan("GDN-WHOLE-MILK-JUG")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["new_count"], 29)
        self.assertIn("New count: 29", res["message"])
        self.assertEqual(self.counts()["Whole Milk"], 29)

    def test_raw_scan_is_case_and_whitespace_insensitive(self):
        res = self.scan("  gdn-heavy-cream-jug \n")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.counts()["Heavy Cream"], -1)

    def test_duplicate_scan_window_blocks_double_count(self):
        self.app.config["DUPLICATE_SCAN_SECONDS"] = 10
        first = self.scan("GDN-WHOLE-MILK-JUG")
        second = self.scan("GDN-WHOLE-MILK-JUG")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.counts()["Whole Milk"], -1)

    def test_unknown_code(self):
        res = self.scan("GDN-NOT-A-THING-XYZ")
        self.assertEqual(res["status"], "unknown")

    # --- premade containers ------------------------------------------------

    def test_container_lifecycle(self):
        code = "GDN-VANILLA-CREAM-20260727-001"

        made = self.scan(code)
        self.assertEqual(made["status"], "ok")
        self.assertEqual(made["kind"], "container_made")
        self.assertEqual(self.counts()["Vanilla Cream Batch"], 1)

        rescan = self.scan(code)
        self.assertEqual(rescan["status"], "needs_action")
        self.assertEqual([a["action"] for a in rescan["actions"]],
                         ["IN_USE", "FINISHED", "WASTED"])

        in_use = self.container_action(code, "IN_USE")
        self.assertEqual(in_use["status"], "ok")
        self.assertEqual(self.counts()["Vanilla Cream Batch"], 1)  # unchanged

        rescan2 = self.scan(code)
        self.assertEqual([a["action"] for a in rescan2["actions"]], ["FINISHED", "WASTED"])

        finished = self.container_action(code, "FINISHED")
        self.assertEqual(finished["status"], "ok")
        self.assertEqual(self.counts()["Vanilla Cream Batch"], 0)

        # Terminal: further scans/actions change nothing.
        info = self.scan(code)
        self.assertEqual(info["kind"], "container_info")
        again = self.container_action(code, "WASTED")
        self.assertEqual(again["status"], "error")
        self.assertEqual(self.counts()["Vanilla Cream Batch"], 0)

    def test_wasted_decrements_and_logs_waste(self):
        code = "GDN-MILK-BASE-20260727-002"
        self.scan(code)
        res = self.container_action(code, "WASTED")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.counts()["Milk Base Batch"], 0)
        data = self.client.get("/api/summary").get_json()
        milk_base = [p for p in data["premade"] if p["display_name"] == "Milk Base Batch"][0]
        self.assertEqual((milk_base["made"], milk_base["wasted"]), (1, 1))

    def test_premade_scan_does_not_touch_raw_counts(self):
        before = self.counts()
        self.scan("GDN-MILK-BASE-20260727-009")
        after = self.counts()
        self.assertEqual(before["Whole Milk"], after["Whole Milk"])
        self.assertEqual(before["Heavy Cream"], after["Heavy Cream"])

    def test_generic_premade_code_is_rejected(self):
        res = self.scan("GDN-VANILLA-CREAM")
        self.assertEqual(res["status"], "error")
        self.assertEqual(self.counts()["Vanilla Cream Batch"], 0)

    # --- adjustments -------------------------------------------------------

    def test_adjustment_is_logged(self):
        res = self.client.post(
            "/api/adjust", json={"item_id": 1, "new_count": 24, "note": "weekly recount"}
        ).get_json()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.counts()["Whole Milk"], 24)
        with self.app.test_request_context():
            import db as database
            events = database.get_db().execute(
                "SELECT * FROM scan_events WHERE action = 'ADJUSTMENT'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["quantity_after"], 24)
        self.assertEqual(events[0]["note"], "weekly recount")

    # --- pages -------------------------------------------------------------

    def test_pages_render(self):
        for path in ("/", "/summary", "/correct", "/admin", "/labels?item_id=3&count=2"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)

    def test_label_sheet_generates_unique_sequenced_codes(self):
        res = self.client.get("/labels?item_id=3&count=3&date=20260727")
        html = res.get_data(as_text=True)
        for seq in ("001", "002", "003"):
            self.assertIn(f"GDN-VANILLA-CREAM-20260727-{seq}", html)


if __name__ == "__main__":
    unittest.main()
