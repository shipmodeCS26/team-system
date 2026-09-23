import base64
import json
import unittest
from unittest.mock import patch

import requests
from werkzeug.security import generate_password_hash

from app import app
import inventory
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
        self.assertEqual(result["rows"][0]["flags"], ["remaining"])
        self.assertEqual(result["issue_rows"], 0)

    def test_changed_headers_fail_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            parse_dashboard(dashboard(["Product", "Unknown balance"], [["A", "10"]]))


    def test_formula_errors_are_flagged_per_cell(self):
        values = dashboard(["Product", "Initial Stock", "Today's Orders", "Remaining stocks", "daily demand", "Status"],
                           [["Product D", "100", "#NAME?", "#ERROR!", "2", "OK"], ["Product E", "5", "1", "4", "1", "OK"]])
        result = parse_dashboard(values)
        self.assertEqual(result["rows"][0]["flags"], ["remaining", "shipped"])
        self.assertEqual(result["rows"][0]["remaining"], "#ERROR!")
        self.assertEqual(result["rows"][1]["flags"], [])
        self.assertEqual(result["issue_rows"], 1)

    def test_blank_as_of_and_empty_dashboard_warn(self):
        values = dashboard(["Product", "Remaining stocks"], [])
        values[3][1] = ""
        result = parse_dashboard(values)
        self.assertIn("Dashboard as-of date is blank", result["warnings"])
        self.assertIn("No product rows found on the Dashboard", result["warnings"])

    def test_products_reaching_last_row_warn_about_truncation(self):
        values = dashboard(["Product", "Remaining stocks"], [[f"P{i}", "1"] for i in range(25)])
        result = parse_dashboard(values)
        self.assertEqual(len(result["rows"]), 25)
        self.assertTrue(any("past row 39" in warning for warning in result["warnings"]))
        values = dashboard(["Product", "Remaining stocks"], [[f"P{i}", "1"] for i in range(24)] + [["TOTAL", "24"]])
        self.assertFalse(any("past row 39" in warning for warning in parse_dashboard(values)["warnings"]))

    def test_summary_errors_warn(self):
        values = dashboard(["Product", "Remaining stocks"], [["A", "1"]])
        values[6][6] = "#REF!"
        self.assertIn("Dashboard summary contains pending or formula-error values", parse_dashboard(values)["warnings"])


class FakeResponse:
    def __init__(self, status, values=None):
        self.status_code = status
        self._values = values

    def json(self):
        return {"values": self._values}


class SourceIsolationTests(unittest.TestCase):
    """One broken workbook or mapping must not hide or alter another client's values."""
    GOOD = dashboard(["Product", "Remaining stocks"], [["Product A", "90"]])
    IDS = {"claritymd": "a" * 30, "fascial-labs": "b" * 30, "muravai": "c" * 30, "puravita": "d" * 30}

    def setUp(self):
        inventory._cache.clear()
        self.addCleanup(inventory._cache.clear)
        self.env = patch.dict("os.environ", {
            "INVENTORY_SHEETS_JSON": json.dumps(dict(self.IDS, onset="short")),
            "INVENTORY_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account"})})
        self.env.start()
        self.addCleanup(self.env.stop)
        patcher = patch("inventory.service_account.Credentials.from_service_account_info")
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, responses, client_ids):
        def get(session, url, **kwargs):
            sheet = url.split("/spreadsheets/")[1].split("/")[0]
            response = responses[sheet]
            if isinstance(response, Exception):
                raise response
            return response
        with patch("inventory.AuthorizedSession.get", get), patch("inventory.AuthorizedSession.__init__", return_value=None):
            with self.assertLogs("inventory", "WARNING") as logs:
                result = {source["id"]: source for source in inventory.read_dashboards(client_ids)}
        for line in logs.output:
            for sheet in self.IDS.values():
                self.assertNotIn(sheet, line)
        return result

    def test_failures_are_per_client_and_categorized(self):
        responses = {"a" * 30: FakeResponse(200, self.GOOD), "b" * 30: FakeResponse(403),
                     "c" * 30: FakeResponse(404), "d" * 30: FakeResponse(400)}
        result = self.read(responses, ["claritymd", "fascial-labs", "muravai", "puravita", "nuerosmile", "onset"])
        self.assertEqual(list(result), ["claritymd", "fascial-labs", "muravai", "puravita", "nuerosmile", "onset"])
        self.assertEqual(result["claritymd"]["rows"][0]["remaining"], "90")
        self.assertNotIn("error", result["claritymd"])
        self.assertEqual(result["fascial-labs"]["error_code"], "access_denied")
        self.assertEqual(result["muravai"]["error_code"], "not_found")
        self.assertEqual(result["puravita"]["error_code"], "no_dashboard")
        self.assertEqual(result["nuerosmile"]["error_code"], "not_configured")
        self.assertEqual(result["onset"]["error_code"], "not_configured")
        for failed in ("fascial-labs", "muravai", "puravita", "nuerosmile", "onset"):
            self.assertNotIn("rows", result[failed])
            self.assertNotIn("a" * 30, json.dumps(result[failed]))

    def test_layout_change_and_timeouts_do_not_return_values(self):
        responses = {"a" * 30: FakeResponse(200, dashboard(["Item", "Qty"], [["A", "1"]])),
                     "b" * 30: requests.Timeout("slow"), "c" * 30: FakeResponse(503)}
        result = self.read(responses, ["claritymd", "fascial-labs", "muravai"])
        self.assertEqual(result["claritymd"]["error_code"], "layout_changed")
        self.assertEqual(result["fascial-labs"]["error_code"], "unavailable")
        self.assertEqual(result["muravai"]["error_code"], "unavailable")
        self.assertTrue(all("rows" not in source for source in result.values()))

    def test_failed_read_is_not_cached(self):
        responses = {"a" * 30: FakeResponse(403)}
        self.assertEqual(self.read(responses, ["claritymd"])["claritymd"]["error_code"], "access_denied")
        responses["a" * 30] = FakeResponse(200, self.GOOD)
        with patch("inventory.AuthorizedSession.get", lambda session, url, **kwargs: responses["a" * 30]), \
                patch("inventory.AuthorizedSession.__init__", return_value=None):
            result = inventory.read_dashboards(["claritymd"])
        self.assertEqual(result[0]["rows"][0]["remaining"], "90")


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
