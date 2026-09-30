import base64
import json
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import movement
from app import app
from movement import COLUMNS, parse_export

KEYS = list(COLUMNS)


def row(tracking, created, status, org="Muravai", order="#1", carrier="USPS", eta="", voided="No"):
    return dict(zip(KEYS, [tracking, created, org, order, carrier, status, eta, voided]))


def columns(rows, extra=None):
    out = {key: [[r[key]] if r[key] else [] for r in rows] for key in KEYS}
    if extra:
        out["additional"] = [[v] if v else [] for v in extra]
    return out


# Export taken on Sep 29 (latest label that day). as_of = Sep 29 00:00 Miami time.
ROWS = [
    row("NEW", "09/29/2026", "pre-transit"),                 # 0 days: monitoring
    row("W5", "09/23/2026", "pre-transit"),                  # end Sep 23 -> start Sep 29 = 5 days: watch
    row("U7", "09/21/2026", "pre-transit"),                  # 7 days: urgent
    row("C10", "09/18/2026", "pre-transit"),                 # 10 days: critical
    row("TRANSIT", "09/10/2026", "in-transit", eta="2026-09-15"),
    row("OFD", "09/27/2026", "out-for-delivery", eta="2026-09-30"),
    row("DONE", "09/01/2026", "delivered"),
    row("VOID", "09/01/2026", "pre-transit", voided="Yes"),
    row("ERR", "09/20/2026", "error"),
    row("OTHER", "09/01/2026", "pre-transit", org="PuraVita"),
]


class ParseExportTests(unittest.TestCase):
    def setUp(self):
        self.result = parse_export("muravai", columns(ROWS))
        self.by = {s["tracking_number"]: s for s in self.result["shipments"]}

    def test_as_of_is_start_of_latest_label_day(self):
        self.assertEqual(self.result["export_day"], "2026-09-29")
        self.assertTrue(self.result["as_of"].startswith("2026-09-29T00:00:00-04:00"))
        # plain calendar dates for display, so no time zone can shift them a day
        self.assertEqual(self.by["C10"]["label_date"], "2026-09-18")
        self.assertEqual(self.by["C10"]["export_day"], "2026-09-29")

    def test_label_tiers_measured_at_export_not_today(self):
        self.assertEqual((self.by["NEW"]["tier"], self.by["NEW"]["days"]), ("monitoring", 0))
        self.assertEqual((self.by["W5"]["tier"], self.by["W5"]["days"]), ("watch", 5))
        self.assertEqual((self.by["U7"]["tier"], self.by["U7"]["days"]), ("urgent", 7))
        self.assertEqual((self.by["C10"]["tier"], self.by["C10"]["days"]), ("critical", 10))

    def test_in_transit_has_no_guessed_age_but_reports_past_estimate(self):
        transit = self.by["TRANSIT"]
        self.assertEqual(transit["tier"], "data_gap")
        self.assertIsNone(transit["days"])
        self.assertEqual(transit["days_past_estimate"], 14)
        self.assertIn("no carrier scan time", transit["reason"])
        self.assertIsNone(self.by["OFD"]["days_past_estimate"])

    def test_delivered_voided_and_other_clients_excluded(self):
        self.assertNotIn("DONE", self.by)
        self.assertNotIn("VOID", self.by)
        self.assertNotIn("OTHER", self.by)
        self.assertEqual((self.result["delivered"], self.result["cancelled"]), (1, 1))
        self.assertIn("1 row(s) belong to another organization and were left out", self.result["warnings"])

    def test_carrier_error_is_missing_data(self):
        self.assertEqual(self.by["ERR"]["tier"], "data_gap")
        self.assertIn("could not track", self.by["ERR"]["reason"])

    def test_duplicates_use_last_row_and_warn(self):
        rows = [row("DUP", "09/20/2026", "pre-transit"), row("DUP", "09/29/2026", "delivered")]
        result = parse_export("muravai", columns(rows))
        self.assertEqual(result["shipments"], [])
        self.assertEqual(result["delivered"], 1)
        self.assertTrue(any("repeated tracking" in w for w in result["warnings"]))

    def test_multi_package_labels_are_missing_data(self):
        result = parse_export("muravai", columns([row("M", "09/10/2026", "pre-transit")], extra=["X1,X2"]))
        self.assertEqual(result["shipments"][0]["tier"], "data_gap")
        self.assertIsNone(result["shipments"][0]["days"])

    def test_empty_export_is_an_error_not_an_empty_queue(self):
        with self.assertRaises(movement.SourceError):
            parse_export("muravai", columns([]))

    def test_two_digit_year_dates_are_read(self):
        result = parse_export("muravai", columns([row("A", "9/18/26", "pre-transit"), row("B", "09/29/2026", "delivered")]))
        self.assertEqual(result["shipments"][0]["days"], 10)


    def test_puravita_export_spelled_purevita_is_kept(self):
        # Real ShipSidekick exports label this client "PureVita"
        result = parse_export("puravita", columns([row("P1", "09/20/2026", "pre-transit", org="PureVita"),
                                                   row("X", "09/29/2026", "pre-transit", org="Muravai")]))
        self.assertEqual([s["tracking_number"] for s in result["shipments"]], ["P1"])
        self.assertEqual(result["shipments"][0]["tier"], "urgent")  # end Sep 20 -> start Sep 29 = 8 days

    def test_real_tracking_number_is_returned_unchanged(self):
        result = parse_export("muravai", columns([row("420191169261290400221975160442", "09/20/2026", "pre-transit")]))
        self.assertEqual(result["shipments"][0]["tracking_number"], "420191169261290400221975160442")


class SourceRoutingTests(unittest.TestCase):
    CREDS = {"type": "service_account"}

    def run_read(self, env):
        calls = []

        def fake(cid, sheet, tab, credentials):
            calls.append((cid, sheet, tab))
            return {"id": cid, "available": True, "shipments": []}
        base = {"INVENTORY_SHEETS_JSON": json.dumps({"muravai": "a" * 44, "puravita": "b" * 44}),
                "INVENTORY_SERVICE_ACCOUNT_JSON": json.dumps(self.CREDS)}
        with patch.dict("os.environ", {**base, **env}, clear=False), patch("movement._read_one", side_effect=fake):
            out = movement.read_movement(["muravai", "puravita", "onset"])
        return calls, out

    def test_shared_workbook_uses_one_tab_per_client(self):
        calls, out = self.run_read({"MOVEMENT_SHEET_ID": "c" * 44})
        self.assertEqual(sorted(calls), [("muravai", "c" * 44, "Muravai"), ("onset", "c" * 44, "Onset"),
                                         ("puravita", "c" * 44, "Pure Vita")])
        self.assertEqual([o["id"] for o in out], ["muravai", "puravita", "onset"])

    def test_without_shared_workbook_each_client_workbook_is_used(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MOVEMENT_SHEET_ID", None)
            calls, out = self.run_read({})
        self.assertEqual(sorted(calls), [("muravai", "a" * 44, "No Movement"), ("puravita", "b" * 44, "No Movement")])
        self.assertEqual(out[2]["error_code"], "not_configured")


class SheetModeApiTests(unittest.TestCase):
    ENV = {"INVENTORY_SHEETS_ENABLED": "true", "SHIPMENTS_SOURCE": "sheets", "INVENTORY_SHEETS_JSON": "{}",
           "INVENTORY_SERVICE_ACCOUNT_JSON": "{}", "WORKSPACE_USER": "owner", "SECRET_KEY": "test-secret",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("test-password", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:test-password").decode()}

    def setUp(self):
        self.client = app.test_client()

    def test_requires_sign_in(self):
        with patch.dict("os.environ", self.ENV):
            self.assertEqual(self.client.get("/api/workspace").status_code, 401)

    def test_ids_unique_and_per_client_errors_kept(self):
        a = parse_export("muravai", columns(ROWS))
        b = dict(a, id="puravita", shipments=[dict(s, client_id="puravita") for s in a["shipments"]])
        failed = {"id": "onset", "error_code": "access_denied", "error": "Access denied."}
        with patch.dict("os.environ", self.ENV), patch("app.read_movement", return_value=[a, b, failed]):
            body = self.client.get("/api/workspace", headers=self.AUTH).get_json()
        self.assertEqual(body["mode"], "sheet")
        ids = [s["id"] for s in body["shipments"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(body["sources"][2]["error_code"], "access_denied")
        self.assertNotIn("shipments", body["sources"][0])

    def test_repeated_requests_return_the_same_queue(self):
        cached = [parse_export("muravai", columns(ROWS))]  # read_movement returns cached objects
        with patch.dict("os.environ", self.ENV), patch("app.read_movement", return_value=cached):
            first = self.client.get("/api/workspace", headers=self.AUTH).get_json()
            second = self.client.get("/api/workspace", headers=self.AUTH).get_json()
        self.assertEqual(len(first["shipments"]), 7)
        self.assertEqual(first["shipments"], second["shipments"])
        self.assertIn("shipments", cached[0])

    def test_no_sample_data_in_sheet_mode(self):
        with patch.dict("os.environ", self.ENV), patch("app.read_movement", return_value=[]):
            body = self.client.get("/api/workspace", headers=self.AUTH).get_json()
        self.assertFalse(any(s["tracking_number"].startswith("DEMO-") for s in body["shipments"]))

    def test_writes_refused_in_sheet_mode(self):
        with patch.dict("os.environ", self.ENV):
            response = self.client.patch("/api/shipments/1", headers=self.AUTH, json={"case_status": "open"})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
