"""Standard EOD report (issue #9). Fixture numbers are made up; layouts match the real Dashboards."""
import base64
import glob
import json
import os
import re
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import slack_draft
from app import app
from dashboard_image import render_png
from eod_check import INCOMPLETE, REVIEW, VERIFIED, check
from eod_report import SECTIONS, build_report
from inventory import parse_dashboard

TEMPLATE_HEADERS = ["Product", "Initial Stock", "Today's Orders", "Remaining stocks", "daily demand",
                    "Projected Stocks", "Status", "Covered Days", "Order By", "Suggested Order Qty"]
MURAVAI_HEADERS = ["Product", "Opening Stock", "Sold on Date", "Calculated EOD On Hand*", "Daily Demand (30d)",
                   "Days of Cover", "30-Day Projected Balance* (incl. incoming)", "Reorder Status",
                   "Runs Out (with incoming)", "Ship New PO By", "Staged Supply Plan* (after incoming)",
                   "Incoming (Not Arrived)", "60-Day Top-Up (after incoming)",
                   "30-Day Regular PO (incl. case rounding)", "Total to Order (full cases)"]


def dashboard(name, as_of, headers, rows, summary=("1", "0", "0", "100"), status=""):
    values = [[name], ["INVENTORY DASHBOARD"], ["note"], ["As of", as_of, "", "Report Status" if status else "", status],
              [], ["PRODUCTS TRACKED", "", "NEED ORDERING NOW", "", "OUT OF STOCK", "", "UNITS ON HAND"],
              [summary[0], "", summary[1], "", summary[2], "", summary[3]], [], [], ["ACTION LIST"], ["text"], [], [],
              headers, *rows]
    return values


SHEETS = {
    "puravita": dashboard("PuraVita", "28 Sep 2026", TEMPLATE_HEADERS, [
        ["Magnesium Performance Capsules ", "1,000", "300", "700", "250", "20.6", "REORDER NOW", "2.8",
         "28 Sep 2026", "9,000"]], ("1", "1", "0", "700")),
    "fascial-labs": dashboard("Fascial Labs", "28 Sep 2026", TEMPLATE_HEADERS, [
        ["TrueForm Fascial Release Support", "900", "5", "895", "40", "800", "STOCK SUFFICIENT", "22.4",
         "10 Oct 2026", "0"]], ("1", "0", "0", "895")),
    "nuerosmile": dashboard("Neurosmile", "24 Sep 2026", TEMPLATE_HEADERS, [
        ["Nerve Support", "500", "3", "497", "20", "480", "STOCK SUFFICIENT", "24.9", "25 Feb 2027", "0"],
        ["Magnesium Spray", "200", "2", "198", "4", "190", "STOCK SUFFICIENT", "49.5", "10 Apr 2027", "0"],
        [], [], ["TOTAL", "700", "5", "695"]], ("2", "0", "0", "695")),
    "muravai": dashboard("muravai", "28 Sep 2026", MURAVAI_HEADERS, [
        ["Replacement Filters, 3-Pack", "12", "4", "8", "478.6", "0.0", "1", "REORDER NOW", "2026-11-27", "Now",
         "14,400", "26,400", "0", "14,400", "14,400"],
        ["Shower Hose*", "0", "0", "0", "0.9", "0.0", "33.2", "OUT OF STOCK", "2026-12-03", "2026-10-04",
         "24", "60", "0", "24", "24"]], ("2", "1", "1", "8"), status="SOURCE VALUES"),
}
NAMES = {"puravita": "PuraVita", "fascial-labs": "Fascial. Labs", "nuerosmile": "Neurosmile", "muravai": "Muravai"}
ORGS = {"puravita": "PuraVita", "fascial-labs": "Fascial Labs", "nuerosmile": "NeuroSmile", "muravai": "Muravai"}
CHANNELS = {"puravita": "C0AAAAAAAA1", "fascial-labs": "C0BBBBBBBB2", "nuerosmile": "C0CCCCCCCC3",
            "muravai": "C0DDDDDDDD4"}


def row(client, created, items, tracking, order="#1"):
    return {"Tracking Code": tracking, "Created Date": created, "Organization": ORGS[client], "Order Name": order,
            "Tracking Status": "pre-transit", "Voided": "No", "Mission Num": "M1", "Items": items,
            "Origin Address": "Miami, FL, 33166"}


def csv_for(client):
    """A CSV that recounts to exactly each fixture Dashboard's shipped values."""
    if client == "puravita":
        return [row(client, "9/28/26", "300x Puravita Magnesium Performance Capsules (CAP-MAGNESIUM-360)", "T1")]
    if client == "fascial-labs":
        return [row(client, "9/28/26", "5x TrueForm Fascial Release Support (FASCSUPP-1)", "T2")]
    if client == "nuerosmile":
        return [row(client, "9/24/26", "1x Nerve Support (NEURO-120); 1x Magnesium Oil Relief Spray (MAG-SPRAY-360)",
                    "T3", "#10"),
                row(client, "9/24/26", "2x Nerve Support (NEURO-120); 1x Magnesium Oil Relief Spray (MAG-SPRAY-360)",
                    "T4", "#11")]
    return [row(client, "9/28/26", "4x Muravai Replacement Filters, 3-Pack", "T5")]


def report_for(client, rows="default", **kwargs):
    source = parse_dashboard(SHEETS[client])
    result = check(client, source, csv_for(client) if rows == "default" else rows, csv_name="export.csv", **kwargs)
    return build_report(client, NAMES[client], source, result), source


class StructureTests(unittest.TestCase):
    def test_every_client_has_the_same_six_sections_in_order(self):
        for client in SHEETS:
            report, source = report_for(client)
            self.assertEqual([s["title"] for s in report["sections"]], list(SECTIONS), client)
            headings = re.findall(r"^\*\d\. (.+)\*$", report["text"], re.M)
            self.assertEqual(headings, list(SECTIONS), client)
            for line in report["sections"][0]["lines"]:
                self.assertRegex(line, r"^• .+: starting .+ \| shipped .+ \| remaining .+$")
            for line in report["sections"][1]["lines"]:
                self.assertRegex(line, r"^• .+: .+/day \| .+ \| order by .+ \| suggested order .+$")

    def test_every_number_is_the_dashboard_value(self):
        for client in SHEETS:
            report, source = report_for(client)
            text = report["text"]
            self.assertIn(f"as of {source['as_of']}.", text)
            for product in source["rows"]:
                for key in ("starting", "shipped", "remaining", "demand", "order_by", "suggested"):
                    self.assertIn(product[key], text, (client, key))
            self.assertNotIn("TOTAL", text)

    def test_muravai_uses_its_own_columns(self):
        report, _ = report_for("muravai")
        forecast = "\n".join(report["sections"][1]["lines"])
        self.assertIn("runs out 2026-11-27 | order by Now | suggested order 14,400", forecast)
        self.assertIn("• Shower Hose* is out of stock.", report["sections"][3]["lines"])
        self.assertIn("• Confirm a purchase order for Replacement Filters, 3-Pack (suggested 14,400 units).",
                      report["sections"][4]["lines"])


class CheckTests(unittest.TestCase):
    def test_match_is_verified_and_ready(self):
        for client in SHEETS:
            report, _ = report_for(client)
            self.assertEqual(report["status"], VERIFIED, report["hold_reasons"])
            self.assertTrue(report["ready_to_send"], client)
            self.assertFalse(report["text"].startswith("HOLD"))

    def test_mismatch_is_review_naming_sku_and_gap(self):
        rows = [row("puravita", "9/28/26", "290x Puravita Magnesium Performance Capsules (CAP-MAGNESIUM-360)", "T1")]
        report, _ = report_for("puravita", rows)
        self.assertEqual(report["status"], REVIEW)
        self.assertFalse(report["ready_to_send"])
        self.assertIn("Magnesium Performance Capsules (PVT001): Sheet shows 300 shipped, ShipSidekick recount is 290 (gap +10).",
                      report["hold_reasons"])
        self.assertTrue(report["text"].startswith("HOLD, DO NOT SEND (REVIEW)"))

    def test_missing_csv_is_incomplete_and_held(self):
        report, _ = report_for("fascial-labs", None)
        self.assertEqual(report["status"], INCOMPLETE)
        self.assertFalse(report["ready_to_send"])
        report, _ = report_for("fascial-labs", [row("fascial-labs", "9/27/26", "5x x (FASCSUPP-1)", "T9")])
        self.assertEqual(report["status"], INCOMPLETE)
        self.assertIn("The shipments (export.csv) have no rows dated 28 Sep 2026.", report["hold_reasons"])

    def test_confirmed_no_shipments_passes_only_when_sheet_is_zero(self):
        values = [list(r) for r in SHEETS["fascial-labs"]]
        values[14][2] = "0"
        source = parse_dashboard(values)
        self.assertEqual(check("fascial-labs", source, None, no_shipments_confirmed=True)["status"], VERIFIED)
        self.assertEqual(check("fascial-labs", parse_dashboard(SHEETS["fascial-labs"]), None,
                               no_shipments_confirmed=True)["status"], REVIEW)

    def test_sheet_review_status_or_warning_blocks(self):
        values = [list(r) for r in SHEETS["muravai"]]
        values[3][4] = "REVIEW"
        source = parse_dashboard(values)
        self.assertEqual(check("muravai", source, csv_for("muravai"))["status"], REVIEW)

    def test_neurosmile_two_product_orders_count_one_per_item(self):
        # Sep 24 case: an order with both products used to count its total item count for each product.
        source = parse_dashboard(SHEETS["nuerosmile"])
        result = check("nuerosmile", source, csv_for("nuerosmile"))
        self.assertEqual({c["sku"]: c["csv"] for c in result["checks"]}, {"NEU001": 3, "NEU002": 2})
        self.assertEqual(result["status"], VERIFIED)

    def test_unknown_or_unapproved_items_hold_and_are_named(self):
        cases = {
            "fascial-labs": ("5x TrueForm Fascial Release Support (FASCSUPP-1); "
                             "1x TrueForm Fascial Release Supplement (FASCSUPPx2)", "FASCSUPPx2"),
            "nuerosmile": ("1x Nerve Support (NEURO-120); 1x Pill Carrier (PILL-CARRIER-360)", "PILL-CARRIER-360"),
            "puravita": ("300x Puravita Magnesium Performance Capsules (CAP-MAGNESIUM-360); 1x Gift (GIFT-1)",
                         "GIFT-1"),
            "muravai": ("4x Muravai Replacement Filters, 3-Pack; 1x Mystery Widget", "Mystery Widget"),
        }
        dates = {"nuerosmile": "9/24/26"}
        for client, (items, name) in cases.items():
            report, _ = report_for(client, [row(client, dates.get(client, "9/28/26"), items, "T7")])
            self.assertEqual(report["status"], REVIEW, client)
            self.assertFalse(report["ready_to_send"])
            self.assertTrue(any(name in reason for reason in report["hold_reasons"]), (client, report["hold_reasons"]))

    def test_fascial_bundle_is_held_never_counted(self):
        rows = csv_for("fascial-labs") + [row("fascial-labs", "9/28/26",
                                              "1x Trueform Fascial Release Bundle (FAC3XBDL)", "T8", "#2")]
        report, _ = report_for("fascial-labs", rows)
        self.assertEqual(report["check"]["checks"][0]["csv"], 5)
        self.assertTrue(any("FAC3XBDL" in r and "not approved" in r for r in report["hold_reasons"]))


class IsolationTests(unittest.TestCase):
    IDENTIFIERS = {
        "puravita": ["PuraVita", "Puravita", "PVT0", "Magnesium Performance", "CAP-MAGNESIUM"],
        "fascial-labs": ["Fascial", "FAS0", "TrueForm", "FASCSUPP"],
        "nuerosmile": ["Neurosmile", "NeuroSmile", "NEU0", "Nerve Support", "Magnesium Spray", "NEURO-"],
        "muravai": ["Muravai", "muravai", "MUR0", "Showerhead", "Replacement Filters", "Shower Hose"],
    }

    def test_no_report_or_draft_contains_another_clients_identifiers(self):
        for client in SHEETS:
            report, _ = report_for(client)
            out = json.dumps({"report": report, "draft": slack_draft.draft(client, report, CHANNELS)})
            self.assertIn(CHANNELS[client], out)
            for other, identifiers in self.IDENTIFIERS.items():
                if other == client:
                    continue
                self.assertNotIn(CHANNELS[other], out, (client, other))
                for identifier in identifiers:
                    self.assertNotIn(identifier, out, (client, other, identifier))

    def test_another_clients_csv_rows_block_the_report(self):
        rows = csv_for("puravita") + csv_for("fascial-labs")
        report, _ = report_for("puravita", rows)
        self.assertEqual(report["status"], REVIEW)
        self.assertEqual(report["check"]["checks"][0]["csv"], 300)

    def test_another_clients_dashboard_product_is_not_mapped(self):
        source = parse_dashboard(SHEETS["fascial-labs"])
        result = check("puravita", source, csv_for("puravita"))
        self.assertEqual(result["status"], REVIEW)
        self.assertIn("no SKU mapping for this client", " ".join(result["reasons"]))


class DraftOnlyTests(unittest.TestCase):
    def test_draft_never_sends_and_needs_a_mapped_channel(self):
        report, _ = report_for("puravita")
        result = slack_draft.draft("puravita", report, CHANNELS)
        self.assertEqual(result["channel"], CHANNELS["puravita"])
        self.assertFalse(result["send"])
        self.assertTrue(result["ready_to_send"])
        unmapped = slack_draft.draft("puravita", report, {})
        self.assertIsNone(unmapped["channel"])
        self.assertFalse(unmapped["ready_to_send"])
        self.assertEqual(slack_draft.channel_for("puravita", {"puravita": "not-a-channel"}), None)

    def test_no_code_path_can_send_to_slack(self):
        here = os.path.dirname(os.path.abspath(__file__))
        pattern = re.compile(r"chat\.postMessage|chat_postMessage|hooks\.slack\.com|slack_sdk|slack\.com/api", re.I)
        for path in glob.glob(os.path.join(here, "*.py")) + glob.glob(os.path.join(here, "static", "*.js")):
            if os.path.basename(path) == "test_eod_report.py":
                continue
            with open(path, encoding="utf-8") as handle:
                self.assertIsNone(pattern.search(handle.read()), path)
        self.assertFalse([name for name in dir(slack_draft) if "send" in name.lower() or "post" in name.lower()])


class ImageTests(unittest.TestCase):
    def test_image_is_a_png_of_the_same_read(self):
        report, source = report_for("nuerosmile")
        png = render_png("Neurosmile", source, report["status"])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(report["as_of"], source["as_of"])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="test")
        self.env = patch.dict(os.environ, {
            "INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}", "INVENTORY_SERVICE_ACCOUNT_JSON": "{}",
            "WORKSPACE_USER": "gly", "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw"), "SECRET_KEY": "test",
            "CLIENT_CHANNELS_JSON": json.dumps(CHANNELS)})
        self.env.start()
        self.client = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"gly:pw").decode()}

    def tearDown(self):
        self.env.stop()

    def post(self, body, csrf="token"):
        with self.client.session_transaction() as session:
            session["csrf"] = "token"
        return self.client.post("/api/eod-report", json=body, headers={**self.auth, "X-CSRF-Token": csrf})

    def test_requires_sign_in_and_csrf(self):
        self.assertEqual(self.client.post("/api/eod-report", json={"client_id": "puravita"}).status_code, 401)
        self.assertEqual(self.post({"client_id": "puravita"}, csrf="wrong").status_code, 403)
        self.assertEqual(self.post({"client_id": "all"}).status_code, 400)

    @patch("app.read_incoming", return_value=[{"id": "puravita", "available": False}])
    @patch("app.read_dashboards")
    def test_one_read_feeds_text_image_and_draft(self, dashboards, _incoming):
        dashboards.return_value = [parse_dashboard(SHEETS["puravita"]) | {"id": "puravita"}]
        csv_text = ("Tracking Code,Created Date,Organization,Order Name,Tracking Status,Voided,Mission Num,Items,"
                    "Origin Address\nT1,9/28/26,PuraVita,#1,pre-transit,No,M1,"
                    "300x Puravita Magnesium Performance Capsules (CAP-MAGNESIUM-360),\"Miami, FL, 33166\"\n")
        response = self.post({"client_id": "puravita", "csv": csv_text, "csv_name": "export.csv"})
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        dashboards.assert_called_once_with(["puravita"])
        self.assertEqual(data["report"]["status"], VERIFIED)
        self.assertEqual(data["draft"]["channel"], CHANNELS["puravita"])
        self.assertFalse(data["draft"]["send"])
        self.assertTrue(data["image"].startswith("data:image/png;base64,"))
        self.assertEqual(data["writes"], "disabled")

    @patch("app.read_incoming", return_value=[{"id": "puravita", "available": False}])
    @patch("app.read_dashboards")
    def test_reads_the_clients_own_daily_sales_tab_by_default(self, dashboards, _incoming):
        dashboards.return_value = [parse_dashboard(SHEETS["puravita"]) | {"id": "puravita"}]
        with patch("app.daily_sales_rows", return_value=(csv_for("puravita"), None)) as sales:
            data = self.post({"client_id": "puravita"}).get_json()
        sales.assert_called_once_with("puravita")
        self.assertEqual(data["report"]["status"], VERIFIED)
        self.assertEqual(data["report"]["check"]["csv"], "Daily Sales tab")

    @patch("app.read_incoming", return_value=[{"id": "puravita", "available": False}])
    @patch("app.read_dashboards")
    def test_unreadable_daily_sales_is_incomplete_with_the_reason(self, dashboards, _incoming):
        dashboards.return_value = [parse_dashboard(SHEETS["puravita"]) | {"id": "puravita"}]
        with patch("app.daily_sales_rows", return_value=(None, "The Daily Sales tab could not be read: x")):
            data = self.post({"client_id": "puravita"}).get_json()
        self.assertEqual(data["report"]["status"], INCOMPLETE)
        self.assertFalse(data["report"]["ready_to_send"])
        self.assertIn("The Daily Sales tab could not be read: x", data["report"]["hold_reasons"])


if __name__ == "__main__":
    unittest.main()
