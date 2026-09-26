"""Calculated inventory: client isolation, ledger math, approved-only inputs,
customer-data minimization, and the read-only endpoint."""
import base64
import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

import client_rules
import eod
import ledger
import ledger_sources
from app import app


def sale(n, items, org, created="9/10/26", tracking=None, voided="No"):
    return {"Tracking Code": tracking or f"T{n}", "Created Date": created, "Organization": org,
            "Order Name": f"#{n}", "Tracking Status": "pre-transit", "Voided": voided,
            "Mission Num": "M1", "Items": items, "Origin Address": "Miami, FL, 33166, US"}


class ClientRuleTests(unittest.TestCase):
    def test_alias_counts_csv_quantity_directly(self):
        rules = client_rules.package("puravita")
        usage = rules.order_usage("3x Puravita Magnesium Performance Capsules (CAP-MAGNESIUM-360)").usage
        self.assertEqual(usage, {"PVT001": 3})

    def test_fascial_code_maps_to_internal_sku(self):
        rules = client_rules.package("fascial-labs")
        self.assertEqual(rules.order_usage("1x TrueForm Fascial Release Support (FASCSUPP-1)").usage,
                         {"FAS001": 1})

    def test_unknown_code_is_listed_never_guessed(self):
        result = client_rules.package("nuerosmile").order_usage("1x Neurosmile Magnesium Spray (SPRAY-9)")
        self.assertEqual(result.usage, {"NEU001": 0})
        self.assertEqual(len(result.unknown_items), 1)

    def test_unknown_client_gets_no_rules(self):
        self.assertIsNone(client_rules.package("claritymd"))

    def test_packages_never_share_skus(self):
        seen = {}
        for client_id, rules in client_rules.PACKAGES.items():
            for sku in rules.skus:
                self.assertNotIn(sku, seen, f"{sku} is in both {seen.get(sku)} and {client_id}")
                seen[sku] = client_id

    def test_other_clients_rows_never_count(self):
        rows = [sale(1, "1x TrueForm (FASCSUPP-1)", "Fascial Labs"),
                sale(2, "5x Replacement Filters (3 Pack)", "Muravai")]
        report = eod.build_eod(rows, client_rules.package("fascial-labs"))[date(2026, 9, 10)]
        self.assertEqual((report.orders, report.usage), (1, {"FAS001": 1}))
        self.assertTrue(report.needs_review)


class _Approved:
    skus = ("A1",)
    labels = {"A1": "Product A"}
    status = "APPROVED"


def reports(day_usage, review=False):
    out = {}
    for day, qty in day_usage.items():
        r = eod.DayReport(day=day, skus=("A1",))
        r.usage["A1"] = qty
        if review:
            r.flags.append("check")
        out[day] = r
    return out


class LedgerTests(unittest.TestCase):
    def base(self, timing=ledger.AFTER, qty=1000):
        return {"A1": ledger.Baseline("A1", qty, date(2026, 9, 3), timing, "Carlos", "count")}

    def calc(self, baselines, usage, movements=(), rules=_Approved()):
        return ledger.calculate("x", rules, reports(usage), baselines, list(movements))["skus"][0]

    def test_closing_count_excludes_that_days_orders(self):
        row = self.calc(self.base(ledger.AFTER), {date(2026, 9, 3): 50, date(2026, 9, 4): 20})
        self.assertEqual((row.usage, row.calculated, row.status), (20, 980, "VERIFIED"))

    def test_opening_count_includes_that_days_orders(self):
        row = self.calc(self.base(ledger.BEFORE), {date(2026, 9, 3): 50, date(2026, 9, 4): 20})
        self.assertEqual(row.calculated, 930)

    def test_no_approved_count_means_no_number(self):
        row = self.calc({}, {date(2026, 9, 4): 20})
        self.assertIsNone(row.calculated)
        self.assertEqual(row.status, "INCOMPLETE")

    def test_unclear_count_timing_is_not_guessed(self):
        row = self.calc(self.base("unclear: SPOT CHECK"), {date(2026, 9, 4): 20})
        self.assertIsNone(row.calculated)

    def test_negative_balance_is_kept_and_flagged(self):
        row = self.calc(self.base(qty=10), {date(2026, 9, 4): 13})
        self.assertEqual((row.calculated, row.status), (-3, "REVIEW"))

    def test_receipts_before_the_count_are_not_added_again(self):
        moves = [ledger.Movement("A1", 500, date(2026, 9, 1), "RECEIPT", "Carlos"),
                 ledger.Movement("A1", 200, date(2026, 9, 5), "RECEIPT", "Carlos")]
        row = self.calc(self.base(), {date(2026, 9, 4): 20}, moves)
        self.assertEqual((row.receipts, row.calculated), (200, 1180))

    def test_eod_review_blocks_verified(self):
        result = ledger.calculate("x", _Approved(), reports({date(2026, 9, 4): 5}, review=True),
                                  self.base(), [])["skus"][0]
        self.assertEqual(result.status, "REVIEW")

    def test_proposed_rules_never_verified(self):
        class Proposed(_Approved):
            status = "PROPOSED"
        self.assertEqual(self.calc(self.base(), {date(2026, 9, 4): 1}, rules=Proposed()).status, "REVIEW")

    def test_baseline_sku_without_mapping_is_flagged(self):
        baselines = {**self.base(), "B2": ledger.Baseline("B2", 5, date(2026, 9, 3), ledger.AFTER, "C", "c")}
        rows = ledger.calculate("x", _Approved(), reports({}), baselines, [])["skus"]
        self.assertEqual(rows[1].status, "REVIEW")


class SourceInputTests(unittest.TestCase):
    def test_latest_approved_count_wins_and_timing_is_read(self):
        counts = [{"Count Date": "09/01/2026", "SKU": "MUR001", "Count Quantity": "10,785",
                   "Count Type": "PHYSICAL OPENING COUNT", "Approved By": "Carlos", "Approval Status": "APPROVED"},
                  {"Count Date": "09/03/2026", "SKU": "MUR001", "Count Quantity": "9,423",
                   "Count Type": "PHYSICAL CLOSING COUNT", "Approved By": "Carlos", "Approval Status": "APPROVED"}]
        with patch.dict("os.environ", {}, clear=True):
            baselines, _ = ledger_sources.build_inputs("muravai", counts, [], [])
        self.assertEqual((baselines["MUR001"].quantity, baselines["MUR001"].timing), (9423, ledger.AFTER))

    def test_unapproved_rows_are_ignored(self):
        self.assertFalse(ledger_sources._is_approved({"Approval Status": "DRAFT", "Approved By": "x"}))
        self.assertFalse(ledger_sources._is_approved({"Approval Status": "APPROVED", "Approved By": ""}))

    def test_private_baselines_need_an_approver(self):
        env = {"INVENTORY_BASELINES_JSON": json.dumps({"fascial-labs": {
            "FAS001": {"quantity": "10,990", "date": "2026-09-03", "timing": "after_processing",
                       "approved_by": "Carlos"},
            "FAS002": {"quantity": "5", "date": "2026-09-03", "timing": "after_processing"}}})}
        with patch.dict("os.environ", env):
            result = ledger_sources.private_baselines("fascial-labs")
        self.assertEqual(list(result), ["FAS001"])
        self.assertEqual(result["FAS001"].quantity, 10990)

    def test_column_letters(self):
        self.assertEqual([ledger_sources.column_letter(i) for i in (0, 25, 26, 30)], ["A", "Z", "AA", "AE"])

    def test_customer_names_and_addresses_are_never_requested(self):
        header = ["Tracking Code", "Created Date", "Organization", "Order Name", "Order Alias", "Ship To Name",
                  "Ship To Company", "Ship To Street", "Ship To City", "Ship To State", "Ship To Postal Code",
                  "Ship To Country", "Carrier", "Service", "Rate", "Currency", "Weight lbs", "Zone",
                  "Tracking Status", "Est Delivery Date", "Voided", "Customs Type", "Mission Num", "Num Items",
                  "Items", "Item Lots", "Item Expirations", "Num Packages", "Packaging", "Origin Address"]
        reader = ledger_sources.SheetReader.__new__(ledger_sources.SheetReader)
        requested = []

        def fake_batch(ranges, optional=False):
            requested.extend(ranges)
            if ranges == ["'Daily Sales'!1:1"]:
                return [[header]]
            return [[["v"]] for _ in ranges]

        reader.batch = fake_batch
        rows = reader.daily_sales()
        self.assertEqual(set(rows[0]), set(eod.REQUIRED_COLUMNS))
        for letter in "FGHIJK":  # Ship To Name .. Ship To Postal Code
            self.assertFalse(any(r.startswith(f"'Daily Sales'!{letter}2") for r in requested), letter)

    def test_missing_required_column_fails_closed(self):
        reader = ledger_sources.SheetReader.__new__(ledger_sources.SheetReader)
        reader.batch = MagicMock(return_value=[[["Tracking Code", "Created Date"]]])
        with self.assertRaises(ledger_sources.SourceError):
            reader.daily_sales()


class CalculatedApiTests(unittest.TestCase):
    env = {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": "{}",
           "INVENTORY_SERVICE_ACCOUNT_JSON": "{}", "WORKSPACE_USER": "owner",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256"),
           "SECRET_KEY": "s"}

    def setUp(self):
        self.client = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}

    def test_switched_off_by_default(self):
        with patch.dict("os.environ", self.env):
            self.assertEqual(self.client.get("/api/inventory/calculated", headers=self.auth).status_code, 503)

    def test_requires_sign_in_and_reports_writes_disabled(self):
        with patch.dict("os.environ", {**self.env, "INVENTORY_LEDGER_ENABLED": "true"}):
            self.assertEqual(self.client.get("/api/inventory/calculated").status_code, 401)
            with patch("app.calculate_clients", return_value=[{"client_id": "muravai"}]) as calc:
                response = self.client.get("/api/inventory/calculated?client_id=muravai", headers=self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["writes"], "disabled")
            calc.assert_called_once_with(["muravai"])
            self.assertEqual(self.client.get("/api/inventory/calculated?client_id=zzz",
                                             headers=self.auth).status_code, 400)


if __name__ == "__main__":
    unittest.main()
