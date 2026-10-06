import base64
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import app
from daily_update import build_update
from incoming import parse_incoming
from test_incoming import ROWS, TODAY, columns, line

SOURCE = {"id": "muravai", "as_of": "28 Sep 2026", "report_status": "SOURCE VALUES", "warnings": [], "rows": [
    {"product": "Replacement Filters, 3-Pack", "remaining": "12", "cover": "0.0", "demand": "481.8", "status": "REORDER NOW"},
    {"product": "Filtered Showerhead", "remaining": "2,354", "cover": "10.9", "demand": "215.1", "status": "REORDER NOW"},
    {"product": "Connector Kit Box", "remaining": "1,656", "cover": "13.4", "demand": "123.9", "status": "REORDER NOW"},
    {"product": "Shower Hose", "remaining": "0", "cover": "0.0", "demand": "1.1", "status": "OUT OF STOCK"},
]}


class BuildUpdateTests(unittest.TestCase):
    def test_every_product_is_stated_separately_with_sheet_values(self):
        text = build_update("Muravai", SOURCE)["text"]
        self.assertIn("Hi @channel! Here is the Muravai Inventory Update as of 28 Sep 2026.", text)
        self.assertIn("• Replacement Filters, 3-Pack: 12 units, less than 1 day of cover (REORDER NOW)", text)
        self.assertIn("• Filtered Showerhead: 2,354 units, 10.9 days of cover (REORDER NOW)", text)
        self.assertIn("• Connector Kit Box: 1,656 units, 13.4 days of cover (REORDER NOW)", text)
        self.assertNotIn("days left", text)

    def test_earliest_stockout_and_out_of_stock(self):
        text = build_update("Muravai", SOURCE)["text"]
        self.assertIn("Out of stock now: Shower Hose.", text)
        self.assertIn("Earliest to run out: Replacement Filters, 3-Pack (less than 1 day of cover at the Sheet's "
                      "current daily demand of 481.8/day). This is an estimate.", text)

    def test_review_and_warnings_produce_draft_prefix(self):
        result = build_update("Muravai", dict(SOURCE, report_status="REVIEW",
                                              warnings=["1 row(s) contain pending or formula-error values"]))
        self.assertTrue(result["draft"])
        self.assertTrue(result["text"].startswith("DRAFT (source under review): Report marked REVIEW; "
                                                  "1 row(s) contain pending or formula-error values."))
        self.assertFalse(build_update("Muravai", SOURCE)["draft"])

    def test_pending_values_are_restated_not_computed(self):
        row = {"product": "Bundle", "remaining": "#REF!", "cover": "PENDING", "status": "MAPPING REVIEW"}
        text = build_update("Muravai", dict(SOURCE, rows=[row]))["text"]
        self.assertIn("• Bundle: remaining: #REF!, days of cover: PENDING (MAPPING REVIEW)", text)
        self.assertNotIn("Earliest to run out", text)

    def test_incoming_is_listed_separately_with_flags(self):
        result = build_update("Muravai", SOURCE, parse_incoming(columns(ROWS), TODAY) | {"id": "muravai"})
        text = result["text"]
        self.assertEqual(result["held_back"], ["PO7"])
        incoming = text.split("Incoming, not yet in stock:")[1]
        self.assertIn("• PO24: Replacement Filters, 3-Pack 9000. Delivered Sep 25 to Utah; Expected in Miami: "
                      "Transfer not booked. Needs attention: Needs transfer.", incoming)
        self.assertIn("• PO23: Filtered Showerhead 2,880; Replacement Filters, 3-Pack 9,000.", incoming)
        self.assertNotIn("PO22", text)  # received history is not incoming
        self.assertNotIn("PO7", text)  # unverified SKU: internal review, not client-facing
        self.assertIn("• Replacement Filters, 3-Pack: 12 units", text.split("Incoming")[0])  # on-hand unchanged


    def test_partly_held_back_po_is_reported(self):
        rows = [line("PO40", "T40", "Filtered Showerhead", "MUR002", "60", "In Transit", "NOT ARRIVED"),
                line("PO40", "T40", "Nozzle", "REVIEW", "5", "In Transit", "REVIEW — NOT COUNTED")]
        result = build_update("Muravai", SOURCE, parse_incoming(columns(rows), TODAY) | {"id": "muravai"})
        self.assertEqual(result["held_back"], ["PO40"])
        self.assertIn("• PO40: Filtered Showerhead 60", result["text"])
        self.assertNotIn("Nozzle", result["text"])
        self.assertNotIn("SKU not verified", result["text"])  # flags of held-back lines stay internal


    def test_blank_product_name_uses_sku(self):
        rows = [line("PO41", "T41", "", "MUR002", "60", "In Transit", "NOT ARRIVED")]
        result = build_update("Muravai", SOURCE, parse_incoming(columns(rows), TODAY) | {"id": "muravai"})
        self.assertEqual(result["held_back"], [])
        self.assertIn("• PO41: MUR002 60", result["text"])


class DailyUpdateApiTests(unittest.TestCase):
    ENV = {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}", "INVENTORY_SERVICE_ACCOUNT_JSON": "{}",
           "WORKSPACE_USER": "owner", "SECRET_KEY": "test-secret",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("test-password", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:test-password").decode()}

    def setUp(self):
        self.client = app.test_client()

    def test_requires_sign_in_and_one_client(self):
        with patch.dict("os.environ", self.ENV):
            self.assertEqual(self.client.get("/api/daily-update?client_id=muravai").status_code, 401)
            for bad in ("", "all", "acme"):
                self.assertEqual(self.client.get(f"/api/daily-update?client_id={bad}", headers=self.AUTH).status_code, 400)

    def test_failed_source_gives_no_text(self):
        failed = [{"id": "muravai", "error_code": "access_denied", "error": "Access denied."}]
        with patch.dict("os.environ", self.ENV), patch("app.read_dashboards", return_value=failed), \
                patch("app.read_incoming") as incoming:
            response = self.client.get("/api/daily-update?client_id=muravai", headers=self.AUTH)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("text", response.get_json())
        incoming.assert_not_called()

    def test_text_for_selected_client_only(self):
        with patch.dict("os.environ", self.ENV), patch("app.read_dashboards", return_value=[SOURCE]) as dashboards, \
                patch("app.read_incoming", return_value=[{"id": "muravai", "error_code": "not_configured",
                                                          "error": "No workbook"}]) as incoming:
            body = self.client.get("/api/daily-update?client_id=muravai", headers=self.AUTH).get_json()
        dashboards.assert_called_once_with(["muravai"])
        self.assertEqual(incoming.call_args[0][0], ["muravai"])
        self.assertIn("Muravai Inventory Update", body["text"])
        self.assertNotIn("Incoming, not yet in stock", body["text"])
        self.assertEqual(body["incoming_error"], "No workbook")
        self.assertIn("sheet_read_at", body)  # the dialog names which Sheet read the text came from

    def test_missing_incoming_tab_is_reported(self):
        with patch.dict("os.environ", self.ENV), patch("app.read_dashboards", return_value=[SOURCE]), \
                patch("app.read_incoming", return_value=[{"id": "muravai", "available": False}]):
            body = self.client.get("/api/daily-update?client_id=muravai", headers=self.AUTH).get_json()
        self.assertIn("no Incoming Stocks tab", body["incoming_error"])


if __name__ == "__main__":
    unittest.main()
