import base64
import json
import os
import re
import unittest
from datetime import date, datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash

import app as app_module
from app import app
from incoming import parse_incoming
from inventory import parse_dashboard
import sheet_health
from sheet_health import OFF, OK, UNKNOWN, WARNING, WRONG, client_health, parse_cutoff
from test_incoming import columns, line
from test_inventory import dashboard
from test_ssk import levels

ET = ZoneInfo("America/New_York")
HEADERS = ["Product", "Initial Stock", "Today's Orders", "Remaining stocks", "daily demand", "Status",
           "Incoming (Not Arrived)"]
PRODUCT = "TrueForm Fascial Release Support"  # Fascial Labs' Sheet name for FAS001


def sheet(as_of="21 Oct 2026", remaining="110", demand="2", incoming="0", summary_out="0"):
    values = dashboard(HEADERS, [[PRODUCT, "200", "10", remaining, demand, "OK", incoming]])
    values[3][1] = as_of
    values[6] = ["1", "", "0", "", summary_out, "", "110"]
    return {**parse_dashboard(values), "id": "fascial-labs", "source_url": "https://docs.google.com/x"}


def at(day, hour=18, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)  # Oct 2026: 19 Mon, 21 Wed, 24 Sat


def store(available=110, committed=0):
    return {"id": "fascial-labs", "levels": levels(("FASCSUPP-1", "Support", available, committed)), "truncated": False}


def check(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


class DateCheckTests(unittest.TestCase):
    def date_state(self, as_of, now, cutoff=time(17, 0)):
        return sheet_health.date_check(sheet(as_of), now, cutoff)

    def test_cutoff_unset_is_ok_today_and_never_wrong(self):
        self.assertEqual(sheet_health.date_check(sheet("21 Oct 2026"), at(21), None)["state"], OK)
        old = sheet_health.date_check(sheet("10 Oct 2026"), at(21), None)
        self.assertEqual(old["state"], WARNING)
        self.assertIn("cut-off time is not set", old["problems"][0]["reason"])

    def test_stale_after_cutoff_names_shown_and_expected_dates(self):
        result = self.date_state("20 Oct 2026", at(21, 18))
        self.assertEqual(result["state"], WRONG)
        self.assertIn("Tue Oct 20, 2026", result["problems"][0]["reason"])
        self.assertIn("Wed Oct 21, 2026", result["problems"][0]["reason"])
        self.assertEqual(self.date_state("21 Oct 2026", at(21, 18))["state"], OK)

    def test_before_cutoff_previous_workday_is_enough(self):
        self.assertEqual(self.date_state("20 Oct 2026", at(21, 10))["state"], OK)
        self.assertEqual(self.date_state("19 Oct 2026", at(21, 10))["state"], WRONG)
        self.assertEqual(self.date_state("16 Oct 2026", at(19, 10))["state"], OK)  # Monday morning: Friday

    def test_weekend_needs_friday(self):
        self.assertEqual(self.date_state("23 Oct 2026", at(24, 12))["state"], OK)
        self.assertEqual(self.date_state("22 Oct 2026", at(24, 12))["state"], WRONG)

    def test_blank_garbled_and_future_dates_are_wrong(self):
        for text in ("", "#REF!", "soon"):
            self.assertEqual(self.date_state(text, at(21))["state"], WRONG, text)
        self.assertEqual(self.date_state("30 Oct 2026", at(21))["state"], WRONG)

    def test_cutoff_setting(self):
        self.assertEqual(parse_cutoff("17:30"), (time(17, 30), True))
        self.assertEqual(parse_cutoff(""), (None, True))
        self.assertEqual(parse_cutoff("5pm"), (None, False))
        bad = sheet_health.date_check(sheet("20 Oct 2026"), at(21), None, cutoff_valid=False)
        self.assertEqual(bad["state"], UNKNOWN)


class CellAndDashboardTests(unittest.TestCase):
    def test_formula_error_names_product_column_and_row(self):
        result = sheet_health.cell_check(sheet(remaining="#REF!"))
        self.assertEqual(result["state"], WRONG)
        problem = result["problems"][0]
        self.assertEqual(problem["where"], "Dashboard row 15 · Remaining")
        self.assertIn(PRODUCT, problem["reason"])
        self.assertIn("#REF!", problem["reason"])

    def test_pending_is_warning_and_forecast_columns_follow_the_eod_rule(self):
        self.assertEqual(sheet_health.cell_check(sheet(demand="PENDING"))["state"], WARNING)
        self.assertEqual(sheet_health.cell_check(sheet(incoming="#N/A"))["state"], OK)
        self.assertEqual(sheet_health.cell_check(sheet())["state"], OK)

    def test_summary_error_is_wrong(self):
        result = sheet_health.cell_check(sheet(summary_out="#DIV/0!"))
        self.assertEqual(result["state"], WRONG)
        self.assertEqual(result["problems"][0]["where"], "Dashboard summary E7")

    def test_action_list_error_is_wrong(self):
        result = sheet_health.cell_check({**sheet(), "action_list": "#REF!"})
        self.assertEqual((result["state"], result["problems"][0]["where"]), (WRONG, "Dashboard action list (A11)"))

    def test_no_product_rows_is_wrong(self):
        result = sheet_health.dashboard_check({**sheet(), "rows": []})
        self.assertEqual(result["state"], WRONG)

    def test_negative_balance_is_a_warning(self):
        for value in ("-5", "(5)", "(1,200)"):
            result = sheet_health.dashboard_check(sheet(remaining=value))
            self.assertEqual(result["state"], WARNING, value)
            self.assertEqual(result["problems"][0]["where"], "Dashboard row 15 · Remaining")
        self.assertEqual(sheet_health.dashboard_check(sheet(remaining="5"))["state"], OK)

    def test_every_status_that_holds_the_eod_is_a_warning(self):
        for status in ("REVIEW", "INCOMPLETE", "draft"):
            result = sheet_health.dashboard_check({**sheet(), "report_status": status})
            self.assertEqual(result["state"], WARNING, status)
        for status in ("", "SOURCE VALUES", "VERIFIED"):
            self.assertEqual(sheet_health.dashboard_check({**sheet(), "report_status": status})["state"], OK, status)

    def test_report_status_cell_errors(self):
        self.assertEqual(sheet_health.cell_check({**sheet(), "report_status": "#N/A"})["state"], WRONG)
        self.assertEqual(sheet_health.cell_check({**sheet(), "report_status": "PENDING"})["state"], WARNING)


class StockTests(unittest.TestCase):
    def test_match_and_difference(self):
        match = sheet_health.stock_check("fascial-labs", sheet(), store(110), True)
        # Fascial Labs' SKU rules are PROPOSED, so even an exact match stays a Warning.
        self.assertEqual([p["where"] for p in match["problems"]], ["SKU rules"])
        with patch.object(sheet_health.ssk_check.client_rules.package("fascial-labs"), "status", "APPROVED"):
            self.assertEqual(sheet_health.stock_check("fascial-labs", sheet(), store(110), True)["state"], OK)
        result = sheet_health.stock_check("fascial-labs", sheet(), store(90), True)
        self.assertEqual(result["state"], WARNING)  # never Wrong until #16 decides
        reason = next(p["reason"] for p in result["problems"] if p["where"].startswith("SKU FAS001"))
        self.assertIn("SKU FAS001", result["problems"][0]["where"])
        for text in ("Sheet 110", "available 90 (-20)", "available + committed 90 (-20)"):
            self.assertIn(text, reason)

    def test_records_never_compared_are_warnings(self):
        extra = {**store(110), "levels": levels(("FASCSUPP-1", "Support", 110, 0), ("OTHER-9", "Other client", 5, 0),
                                                ("", "No sku", 1, 0))}
        result = sheet_health.stock_check("fascial-labs", sheet(), extra, True)
        self.assertEqual(result["state"], WARNING)
        text = json.dumps(result)
        self.assertIn("OTHER-9", text)
        self.assertIn("have no SKU", text)

    def test_test_environment_is_a_warning(self):
        result = sheet_health.stock_check("fascial-labs", sheet(), {**store(110), "environment": "test"}, True)
        self.assertEqual(result["state"], WARNING)
        self.assertIn("TEST environment", json.dumps(result))

    def test_off_unknown_and_not_current(self):
        self.assertEqual(sheet_health.stock_check("fascial-labs", sheet(), None, True)["state"], OFF)
        self.assertEqual(sheet_health.stock_check("fascial-labs", sheet(), {"error_code": "not_configured"}, True)["state"], OFF)
        self.assertEqual(sheet_health.stock_check("fascial-labs", sheet(), {"error_code": "unavailable"}, True)["state"], UNKNOWN)
        stale = sheet_health.stock_check("fascial-labs", sheet(), store(90), False)
        self.assertEqual(stale["state"], UNKNOWN)
        self.assertIn("not current", stale["problems"][0]["reason"])


class IncomingTests(unittest.TestCase):
    def test_flags_without_echoing_free_text(self):
        rows = [line("PO77", "1Z999SECRET", "Support", "FAS001", "500", "Delivered to Jane Customer, 12 Elm St",
                     "", "5", "", "", "", "Jane Customer garage", "10/01/2026")]
        incoming = {"id": "fascial-labs", **parse_incoming(columns(rows), date(2026, 10, 21))}
        result = sheet_health.incoming_check(incoming)
        self.assertEqual(result["state"], WARNING)
        text = json.dumps(result, ensure_ascii=False)
        for secret in ("Jane", "Elm", "garage", "1Z999SECRET"):
            self.assertNotIn(secret, text)
        self.assertIn("PO PO77 · FAS001", text)
        self.assertIn("Receipt not recorded", text)

    def test_unusable_quantity_is_flagged(self):
        for units in ("", "10.5", "-3", "#N/A"):
            rows = [line("PO5", "", "Support", "FAS001", units, "In transit", "")]
            incoming = {"id": "fascial-labs", **parse_incoming(columns(rows), date(2026, 10, 21))}
            result = sheet_health.incoming_check(incoming)
            self.assertEqual(result["state"], WARNING, units)
            self.assertIn("not a whole number", json.dumps(result))
        rows = [line("PO5", "", "Support", "FAS001", "40", "In transit", "")]
        self.assertEqual(sheet_health.incoming_check({"id": "x", **parse_incoming(columns(rows), date(2026, 10, 21))})["state"], OK)

    def test_error_product_name_is_flagged(self):
        for product in ("#REF!", "PENDING"):
            rows = [line("PO6", "", product, "FAS001", "40", "In transit", "")]
            result = sheet_health.incoming_check({"id": "x", **parse_incoming(columns(rows), date(2026, 10, 21))})
            self.assertEqual(result["state"], WARNING, product)
            self.assertEqual(result["problems"][0]["where"], "PO PO6 · FAS001")

    def test_off_and_unknown(self):
        self.assertEqual(sheet_health.incoming_check({"id": "x", "available": False})["state"], OFF)
        self.assertEqual(sheet_health.incoming_check({"id": "x", "error_code": "unavailable"})["state"], UNKNOWN)


class ClientTests(unittest.TestCase):
    def test_all_clean_is_ok_and_off_sources_are_named(self):
        result = client_health("fascial-labs", sheet(), {"id": "x", "available": False}, None, at(21), None)
        self.assertEqual(result["state"], OK)
        self.assertEqual(result["off"], ["ShipSidekick", "Incoming"])

    def test_failed_sheet_is_unknown_never_ok(self):
        result = client_health("fascial-labs", {"id": "fascial-labs", "error_code": "access_denied"}, None, None, at(21), None)
        self.assertEqual(result["state"], UNKNOWN)

    def test_failed_dashboard_keeps_the_independent_checks(self):
        rows = [line("PO9", "", "Support", "FAS001", "50", "", "", "", "", "", "", "", "10/01/2026")]
        incoming = {"id": "fascial-labs", **parse_incoming(columns(rows), date(2026, 10, 21))}
        result = client_health("fascial-labs", {"id": "fascial-labs", "error_code": "unavailable"}, incoming,
                               store(110), at(21), None)
        keys = {c["key"]: c["state"] for c in result["checks"]}
        self.assertEqual(keys, {"sheet": UNKNOWN, "stock": UNKNOWN, "incoming": WARNING})
        self.assertEqual(result["state"], UNKNOWN)

    def test_unmapped_sheet_is_off(self):
        result = client_health("onset", {"id": "onset", "error_code": "not_configured"}, None, None, at(21), None)
        self.assertEqual((result["state"], result["off"]), (OFF, ["Sheet"]))

    def test_worst_state_wins(self):
        result = client_health("fascial-labs", sheet("20 Oct 2026"), None, store(90), at(21), time(17, 0))
        self.assertEqual(result["state"], WRONG)

    def test_no_network_calls_in_the_checks(self):
        with open(sheet_health.__file__) as handle:
            source = handle.read()
        self.assertIsNone(re.search(r"\b(requests|urllib|urlopen|http\.client|AuthorizedSession|session|SheetReader|read_\w+\()", source))


class EndpointTests(unittest.TestCase):
    ENV = {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}",
           "INVENTORY_SERVICE_ACCOUNT_JSON": "{}", "WORKSPACE_USER": "ops",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw"), "SECRET_KEY": "k"}

    def setUp(self):
        app_module._health.update(at=0.0, result=None)
        self.client = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"ops:pw").decode()}

    def fake_sheets(self, ids, fresh=False):
        return [sheet() if cid == "fascial-labs" else {"id": cid, "error_code": "access_denied", "error": "x"} for cid in ids]

    def test_requires_sign_in(self):
        with patch.dict(os.environ, self.ENV):
            self.assertEqual(self.client.get("/api/sheet-health").status_code, 401)

    def test_off_when_sheets_not_connected(self):
        with patch.dict(os.environ, {"INVENTORY_SHEETS_ENABLED": "false"}):
            body = self.client.get("/api/sheet-health").get_json()
        self.assertEqual((body["enabled"], body["clients"]), (False, []))

    def test_one_failure_never_hides_another_and_client_filter(self):
        with patch.dict(os.environ, self.ENV), patch.object(app_module, "read_dashboards", self.fake_sheets), \
                patch.object(app_module, "read_incoming", lambda ids, today, fresh=False: [{"id": c, "available": False} for c in ids]), \
                patch.object(app_module.ssk_source, "enabled", lambda: False):
            body = self.client.get("/api/sheet-health", headers=self.auth).get_json()
            states = {c["client_id"]: c["state"] for c in body["clients"]}
            self.assertEqual(len(states), 6)
            self.assertNotEqual(states["fascial-labs"], UNKNOWN)
            self.assertEqual(states["muravai"], UNKNOWN)
            one = self.client.get("/api/sheet-health?client_id=muravai", headers=self.auth).get_json()
            self.assertEqual([c["client_id"] for c in one["clients"]], ["muravai"])
            self.assertEqual(body["writes"], "disabled")

    def test_broken_settings_are_unknown_not_500(self):
        def broken(*args):
            raise KeyError("INVENTORY_SHEETS_JSON")
        with patch.dict(os.environ, self.ENV), patch.object(app_module, "read_dashboards", broken), \
                patch.object(app_module, "read_incoming", broken), patch.object(app_module.ssk_source, "enabled", lambda: False):
            response = self.client.get("/api/sheet-health", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(c["state"] == UNKNOWN for c in response.get_json()["clients"]))

    def test_result_is_shared_between_requests(self):
        calls = []
        def counted(ids, fresh=False):
            calls.append(fresh)
            return self.fake_sheets(ids)
        with patch.dict(os.environ, self.ENV), patch.object(app_module, "read_dashboards", counted), \
                patch.object(app_module, "read_incoming", lambda ids, today, fresh=False: [{"id": c, "available": False} for c in ids]), \
                patch.object(app_module.ssk_source, "enabled", lambda: False):
            for _ in range(3):
                self.client.get("/api/sheet-health", headers=self.auth)
        self.assertEqual(calls, [False])

    def test_refresh_skips_the_readers_caches(self):
        seen = []
        def sheets(ids, fresh=False):
            seen.append(("sheets", fresh))
            return self.fake_sheets(ids)
        def stores(ids, fresh=False):
            seen.append(("ssk", fresh))
            return [{"id": c, "error_code": "not_configured"} for c in ids]
        def extra(ids, today, fresh=False):
            seen.append(("incoming", fresh))
            return [{"id": c, "available": False} for c in ids]
        with patch.dict(os.environ, self.ENV), patch.object(app_module, "read_dashboards", sheets), \
                patch.object(app_module, "read_incoming", extra), patch.object(app_module.ssk_source, "enabled", lambda: True), \
                patch.object(app_module.ssk_source, "read_stores", stores):
            self.client.get("/api/sheet-health", headers=self.auth)
            app_module._health["at"] -= 31  # past the Refresh limit
            self.client.get("/api/sheet-health?fresh=1", headers=self.auth)
        self.assertEqual(sorted(seen[3:]), [("incoming", True), ("sheets", True), ("ssk", True)])


if __name__ == "__main__":
    unittest.main()
