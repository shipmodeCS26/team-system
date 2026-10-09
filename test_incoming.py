import base64
import json
import unittest
from datetime import date
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import incoming
from app import app
from incoming import COLUMNS, parse_incoming

KEYS = list(COLUMNS)
TODAY = date(2026, 9, 29)


def line(po, tracking, product, sku, units, status, treatment, boxes_expected="", boxes_received="",
         units_received="", received_date="", where="", expected_date=""):
    return dict(zip(KEYS, [po, tracking, product, sku, units, status, treatment, boxes_expected, boxes_received,
                           units_received, received_date, where, expected_date]))


def columns(rows):
    """Shape rows like the Sheets API column reads: one list of [cell] per requested column."""
    return {key: [[row[key]] if row[key] else [] for row in rows] for key in KEYS}


ROWS = [
    line("PO22", "YT2620209300105262", "Replacement Filters, 3-Pack", "MUR001", "", "Arrived in warehouse",
         "INCLUDED IN LATEST COUNT"),
    line("PO7", "YT2535900600300512U001", "Nozzle", "MUR002", "", "Arrived in warehouse", "REVIEW — NOT COUNTED"),
    line("PO23", "YT2624709300301957", "Filtered Showerhead", "MUR002", "2,880", "In Transit", "NOT ARRIVED", "48",
         where="In delivery, 88/89 boxes", expected_date="09/29/2026"),
    line("PO23", "YT2624709300301957", "Replacement Filters, 3-Pack", "MUR001", "9,000", "In Transit", "NOT ARRIVED", "15",
         where="In delivery, 88/89 boxes", expected_date="09/29/2026"),
    line("PO24", "YT2624709300301932", "Replacement Filters, 3-Pack", "MUR001", "9000", "Needs transfer to Miami",
         "NOT ARRIVED", "15", where="Delivered Sep 25 to Utah", expected_date="Transfer not booked"),
    line("PO25", "YT2626709300102890", "Replacement Filters, 3-Pack", "MUR001", "8400", "In Transit", "NOT ARRIVED", "14",
         where="At sea"),
    line("PO26", "YT0000000000000001", "Filtered Showerhead", "MUR002", "600", "Arrived in warehouse", "NOT ARRIVED",
         "10", "8", "480", "09/28/2026", expected_date="09/20/2026"),
]


class ParseIncomingTests(unittest.TestCase):
    def setUp(self):
        self.result = parse_incoming(columns(ROWS), TODAY)
        self.by_po = {group["po"]: group for group in self.result["shipments"]}

    def test_groups_open_shipments_and_separates_history(self):
        self.assertEqual(list(self.by_po), ["PO7", "PO23", "PO24", "PO25", "PO26"])
        self.assertEqual([group["po"] for group in self.result["history"]], ["PO22"])
        self.assertEqual(len(self.by_po["PO23"]["lines"]), 2)
        self.assertEqual(self.by_po["PO23"]["lines"][0]["boxes_expected"], "48")

    def test_incoming_totals_use_verified_open_lines_only(self):
        totals = {row["sku"]: row["units"] for row in self.result["incoming_by_sku"]}
        self.assertEqual(totals, {"MUR001": 9000 + 9000 + 8400, "MUR002": 2880 + 600})
        self.assertEqual(self.result["unverified_lines"], 1)

    def test_flags(self):
        self.assertEqual(self.by_po["PO24"]["flags"], ["needs_transfer"])
        self.assertEqual(self.by_po["PO7"]["flags"], ["receipt_not_recorded", "sku_unverified"])
        self.assertEqual(self.by_po["PO26"]["flags"], ["missing_boxes"])
        self.assertEqual(self.by_po["PO23"]["flags"], [])  # expected today, not past
        self.assertEqual(self.by_po["PO25"]["flags"], [])
        self.assertEqual(self.result["history"][0]["flags"], [])  # counted rows are not flagged

    def test_past_expected_only_when_nothing_received(self):
        late = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", "In Transit", "NOT ARRIVED", "1",
                    expected_date="09/20/2026")
        self.assertEqual(parse_incoming(columns([late]), TODAY)["shipments"][0]["flags"], ["past_expected"])

    def test_full_receipt_is_not_flagged_missing(self):
        full = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", "In Transit", "NOT ARRIVED", "4", "4", "10",
                    "09/28/2026")
        self.assertEqual(parse_incoming(columns([full]), TODAY)["shipments"][0]["flags"], [])

    def test_explicit_zero_receipt_counts_as_nothing_received(self):
        late = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", "In Transit", "NOT ARRIVED", "1", "0", "0",
                    expected_date="09/20/2026")
        self.assertEqual(parse_incoming(columns([late]), TODAY)["shipments"][0]["flags"], ["past_expected"])

    def test_mixed_group_keeps_received_lines_in_history(self):
        mixed = [line("PO30", "T30", "Filtered Showerhead", "MUR002", "60", "Arrived in warehouse",
                      "INCLUDED IN LATEST COUNT"),
                 line("PO30", "T30", "Replacement Filters, 3-Pack", "MUR001", "600", "In Transit", "NOT ARRIVED", "1")]
        result = parse_incoming(columns(mixed), TODAY)
        self.assertEqual([l["sku"] for l in result["shipments"][0]["lines"]], ["MUR001"])
        self.assertEqual([l["sku"] for l in result["history"][0]["lines"]], ["MUR002"])
        self.assertEqual(result["incoming_by_sku"], [{"sku": "MUR001", "product": "Replacement Filters, 3-Pack",
                                                      "units": 600}])

    def test_filled_last_row_is_reported_truncated(self):
        many = [line(f"PO{i}", "T", "Filtered Showerhead", "MUR002", "1", "In Transit", "NOT ARRIVED")
                for i in range(incoming.LAST_ROW - 1)]
        self.assertTrue(parse_incoming(columns(many), TODAY)["truncated"])
        self.assertFalse(self.result["truncated"])

    def test_negated_received_status_is_not_a_receipt(self):
        for status in ("Not received", "Not delivered", "Undelivered", "Not yet arrived"):
            row = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", status, "NOT ARRIVED", "1")
            self.assertNotIn("receipt_not_recorded", parse_incoming(columns([row]), TODAY)["shipments"][0]["flags"])
        row = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", "Arrived in warehouse", "NOT ARRIVED", "1")
        self.assertIn("receipt_not_recorded", parse_incoming(columns([row]), TODAY)["shipments"][0]["flags"])

    def test_lines_without_quantity_are_counted_as_left_out(self):
        rows = [line("PO9", "T", "Filtered Showerhead", "MUR002", "", "In Transit", "NOT ARRIVED"),
                line("PO9", "T", "Replacement Filters, 3-Pack", "MUR001", "TBD", "In Transit", "NOT ARRIVED")]
        result = parse_incoming(columns(rows), TODAY)
        self.assertEqual((result["incoming_by_sku"], result["unverified_lines"]), ([], 2))

    def test_completed_transfer_is_not_flagged(self):
        for status, flagged in (("Transferred to Miami", False), ("Transfer complete", False),
                                ("Needs transfer to Miami", True)):
            row = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", status, "NOT ARRIVED", "1")
            self.assertEqual("needs_transfer" in parse_incoming(columns([row]), TODAY)["shipments"][0]["flags"],
                             flagged, status)

    def test_fractional_quantity_is_left_out_not_truncated(self):
        row = line("PO9", "T", "Filtered Showerhead", "MUR002", "10.9", "In Transit", "NOT ARRIVED")
        result = parse_incoming(columns([row]), TODAY)
        self.assertEqual((result["incoming_by_sku"], result["unverified_lines"]), ([], 1))
        self.assertEqual(incoming.whole_units("1,200"), 1200)
        self.assertEqual(incoming.whole_units("12.0"), 12)

    def test_negative_quantity_is_left_out(self):
        self.assertIsNone(incoming.whole_units("-100"))
        row = line("PO9", "T", "Filtered Showerhead", "MUR002", "-100", "In Transit", "NOT ARRIVED")
        self.assertEqual(parse_incoming(columns([row]), TODAY)["incoming_by_sku"], [])

    def test_negated_transfer_still_needs_transfer(self):
        for status in ("Not transferred", "Never transferred", "Not yet transferred"):
            row = line("PO9", "T", "Filtered Showerhead", "MUR002", "10", status, "NOT ARRIVED", "1")
            self.assertIn("needs_transfer", parse_incoming(columns([row]), TODAY)["shipments"][0]["flags"], status)

    def test_rows_without_po_or_tracking_stay_separate(self):
        rows = [line("", "", "Filtered Showerhead", "MUR002", "10", "In Transit", "NOT ARRIVED", where="Utah"),
                line("", "", "Shower Hose", "MUR004", "5", "In Transit", "NOT ARRIVED", where="At sea")]
        groups = parse_incoming(columns(rows), TODAY)["shipments"]
        self.assertEqual([g["po"] for g in groups], ["No PO (Sheet row 2)", "No PO (Sheet row 3)"])

    def test_values_are_not_rewritten(self):
        self.assertEqual(self.by_po["PO23"]["lines"][0]["units"], "2,880")


class FakeReader:
    header = [list(COLUMNS.values()) + ["Box Details"]]

    def __init__(self, sheet_id, credentials):
        self.requested = []

    def batch(self, ranges, optional=False):
        if ranges == ["'Incoming Stocks'!1:1"]:
            return [self.header]
        FakeReader.last_ranges = ranges
        blocks = columns(ROWS)
        return [blocks[key] for key in KEYS]


class ReadIncomingTests(unittest.TestCase):
    def setUp(self):
        incoming._cache.clear()
        self.addCleanup(incoming._cache.clear)
        env = patch.dict("os.environ", {
            "INVENTORY_SHEETS_JSON": json.dumps({"muravai": "m" * 30, "puravita": "p" * 30, "onset": "bad"}),
            "INVENTORY_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account"})})
        env.start()
        self.addCleanup(env.stop)

    def test_reads_only_listed_columns(self):
        with patch("incoming.SheetReader", FakeReader):
            result = incoming.read_incoming(["muravai"], TODAY)[0]
        self.assertTrue(result["available"])
        self.assertEqual(len(FakeReader.last_ranges), len(COLUMNS))
        self.assertNotIn("N2", " ".join(FakeReader.last_ranges))  # Box Details column is never requested

    def test_cache_is_kept_per_evaluation_date(self):
        # Flags like "past expected date" depend on the day they are judged on, so the EOD report for an
        # earlier as-of date must never reuse (or overwrite) today's cached read, and the reverse.
        from datetime import timedelta
        calls = []

        class Counting(FakeReader):
            def batch(self, ranges, optional=False):
                calls.append(1)
                return super().batch(ranges, optional)
        with patch("incoming.SheetReader", Counting):
            incoming.read_incoming(["muravai"], TODAY)
            first = len(calls)
            incoming.read_incoming(["muravai"], TODAY - timedelta(days=3))
            second = len(calls)
            incoming.read_incoming(["muravai"], TODAY)
        self.assertGreater(second, first)
        self.assertEqual(len(calls), second)
        # Expired per-date entries are dropped when a new read is stored, so they never pile up.
        for key in list(incoming._cache):
            incoming._cache[key] = (incoming._cache[key][0] - incoming.CACHE_SECONDS - 1, incoming._cache[key][1])
        with patch("incoming.SheetReader", Counting):
            incoming.read_incoming(["muravai"], TODAY - timedelta(days=9))
        self.assertEqual(len(incoming._cache), 1)

    def test_missing_tab_changed_header_and_missing_mapping_are_per_client(self):
        class NoTab(FakeReader):
            def batch(self, ranges, optional=False):
                return [[]]

        class Changed(FakeReader):
            header = [["PO (Every Row)", "Status"]]

        readers = {"m" * 30: NoTab, "p" * 30: Changed}
        with patch("incoming.SheetReader", lambda sheet_id, creds: readers[sheet_id](sheet_id, creds)), \
                self.assertLogs("incoming", "WARNING") as logs:
            result = {row["id"]: row for row in incoming.read_incoming(["muravai", "puravita", "onset"], TODAY)}
        self.assertEqual(result["muravai"], {"id": "muravai", "available": False})
        self.assertEqual(result["puravita"]["error_code"], "incoming_layout")
        self.assertEqual(result["onset"]["error_code"], "not_configured")
        self.assertNotIn("p" * 30, " ".join(logs.output))


class IncomingApiTests(unittest.TestCase):
    ENV = {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}", "INVENTORY_SERVICE_ACCOUNT_JSON": "{}",
           "WORKSPACE_USER": "owner", "SECRET_KEY": "test-secret",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("test-password", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:test-password").decode()}

    def setUp(self):
        self.client = app.test_client()

    def test_requires_sign_in_and_one_known_client(self):
        with patch.dict("os.environ", self.ENV):
            self.assertEqual(self.client.get("/api/incoming?client_id=muravai").status_code, 401)
            for bad in ("", "all", "acme"):
                self.assertEqual(self.client.get(f"/api/incoming?client_id={bad}", headers=self.AUTH).status_code, 400)
            with patch("app.read_incoming", return_value=[{"id": "muravai", "available": False}]) as reader:
                response = self.client.get("/api/incoming?client_id=muravai", headers=self.AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(reader.call_args[0][0], ["muravai"])

    def test_disabled_inventory_exposes_nothing(self):
        with patch.dict("os.environ", {"INVENTORY_SHEETS_ENABLED": "false"}):
            self.assertEqual(self.client.get("/api/incoming?client_id=muravai").status_code, 503)

    def test_incoming_does_not_change_dashboard_values(self):
        dashboard = [{"id": "muravai", "rows": [{"product": "Filters", "remaining": "12"}], "summary": {"on_hand": "12"}}]
        with patch.dict("os.environ", self.ENV), patch("app.read_dashboards", return_value=dashboard), \
                patch("app.read_incoming", return_value=[parse_incoming(columns(ROWS), TODAY) | {"id": "muravai"}]):
            self.client.get("/api/incoming?client_id=muravai", headers=self.AUTH)
            response = self.client.get("/api/inventory?client_id=muravai", headers=self.AUTH).get_json()
        self.assertEqual(response["sources"][0]["rows"][0]["remaining"], "12")
        self.assertEqual(response["sources"][0]["summary"]["on_hand"], "12")


if __name__ == "__main__":
    unittest.main()
