import base64
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import app
from inventory import parse_dashboard


def dashboard(headers, data, status=""):
    rows = [[] for _ in range(13)]
    rows[3] = ["As of", "22 Sep 2026", "", "", status]
    rows[6] = ["2", "", "1", "", "0", "", "119"]
    rows.append(headers)
    rows.extend(data)
    return rows


class DashboardAdapterTests(unittest.TestCase):
    def test_standard_dashboard_uses_displayed_values(self):
        values = dashboard(
            ["Product", "Initial Stock", "Today's Orders", "Remaining stocks", "daily demand", "Projected Stocks", "Status", "Covered Days"],
            [["Product A", "100", "10", "90", "2", "88", "REORDER NOW", "45"], ["TOTAL", "100", "10", "90"]],
        )
        result = parse_dashboard(values)
        self.assertEqual(result["as_of"], "22 Sep 2026")
        self.assertEqual(result["summary"]["on_hand"], "119")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["remaining"], "90")
        self.assertEqual(result["rows"][0]["cover"], "45")

    def test_muravai_layout_preserves_review_and_pending(self):
        values = dashboard(
            ["Product", "Opening Stock", "Sold on Date", "Calculated EOD On Hand*", "Daily Demand (30d)", "Days of Cover", "30-Day Projected Balance*", "Reorder Status"],
            [["Product B", "PENDING", "1", "355", "PENDING", "PENDING", "PENDING", "MAPPING REVIEW"]],
            "REVIEW",
        )
        result = parse_dashboard(values)
        self.assertEqual(result["report_status"], "REVIEW")
        self.assertEqual(result["rows"][0]["remaining"], "355")
        self.assertEqual(result["issue_rows"], 1)
        self.assertTrue(result["warnings"])

    def test_conflicting_summary_is_flagged(self):
        values = dashboard(["Product", "Initial Stock", "Today's Orders", "Remaining stocks", "daily demand", "Projected Stocks", "Status"],
                           [["Product C", "100", "10", "-5", "2", "0", "REORDER NOW"]])
        values[6][2] = "0"
        result = parse_dashboard(values)
        self.assertIn("Reorder summary disagrees with product rows", result["warnings"])
        self.assertIn("A displayed remaining balance is negative", result["warnings"])

    def test_changed_headers_fail_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            parse_dashboard(dashboard(["Product", "Unknown balance"], [["A", "10"]]))


class InventoryApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_inventory_disabled_does_not_expose_data(self):
        with patch.dict("os.environ", {"INVENTORY_SHEETS_ENABLED": "false"}):
            self.assertEqual(self.client.get("/api/inventory").status_code, 503)

    def test_incomplete_connection_fails_closed(self):
        with patch.dict("os.environ", {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": ""}, clear=True):
            self.assertEqual(self.client.get("/").status_code, 503)

    def test_enabled_inventory_requires_authentication(self):
        env = {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}",
               "INVENTORY_SERVICE_ACCOUNT_JSON": "{}", "WORKSPACE_USER": "owner",
               "WORKSPACE_PASSWORD_HASH": generate_password_hash("test-password", method="pbkdf2:sha256"), "SECRET_KEY": "test-secret"}
        with patch.dict("os.environ", env):
            self.assertEqual(self.client.get("/api/inventory").status_code, 401)
            auth = base64.b64encode(b"owner:test-password").decode()
            with patch("app.read_dashboards", return_value=[{"id": "claritymd", "rows": []}]) as reader:
                response = self.client.get("/api/inventory?client_id=claritymd", headers={"Authorization": "Basic " + auth})
                self.assertEqual(response.status_code, 200)
                reader.assert_called_once_with(["claritymd"])
            self.assertEqual(self.client.get("/api/inventory?client_id=unknown", headers={"Authorization": "Basic " + auth}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
