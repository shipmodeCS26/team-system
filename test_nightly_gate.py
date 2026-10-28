import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import nightly_gate
import ssk_check
from nightly_gate import INCOMPLETE, REVIEW, VERIFIED, expected_sheet_date, gate

# 2026-10-28 08:00 ET: the Sheet should be current to Oct 27.
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
SCOPES = {"muravai": ("Miami",), "fascial-labs": ("Miami",), "puravita": ("Miami",), "nuerosmile": ("Miami",)}


def eod(status=VERIFIED, report_date="2026-10-27", reasons=(), client_id="muravai", **extra):
    return {"client_id": client_id, "deduction": "created_date", "status": status, "report_date": report_date,
            "reasons": list(reasons), **extra}


def store(client_id="muravai", **extra):
    return {"id": client_id, "levels": [], "truncated": False, "environment": "production",
            "scoped_locations": ["Miami"], **extra}


def row(sku="MUR001", status="MATCH", notes=(), available=0, committed=0):
    return {"sku": sku, "status": status, "notes": list(notes), "vs_available": available,
            "vs_available_committed": committed}


def compare(rows=None, client_id="muravai", **extra):
    base = {"client_id": client_id, "sheet_as_of": "10/27/2026", "skus": rows if rows is not None else [row()],
            "unmatched_ssk": [], "unmatched_sheet": [], "blank_skus": 0, "duplicate_skus": [], "components": []}
    return {**base, **extra}


def clean(client_id="muravai", **overrides):
    args = {"now": NOW, "eod": eod(client_id=client_id), "store": store(client_id), "compare": compare(client_id=client_id),
            "export_complete": True, "cutoff_aligned": True}
    args.update(overrides)
    return gate(client_id, **args)


@patch.multiple(nightly_gate, BALANCE_BASIS="available", SHIPMENT_DEDUCTION="created_date")
@patch.dict(nightly_gate.WAREHOUSE_SCOPES, SCOPES, clear=True)
class TruthTable(unittest.TestCase):
    def assertStatus(self, result, status, text):
        self.assertEqual(result["status"], status, result)
        reasons = result["incomplete"] if status == INCOMPLETE else result["review"]
        self.assertTrue(any(text in reason for reason in reasons), reasons)

    def test_verified_only_when_everything_passes(self):
        result = clean()
        self.assertEqual(result["status"], VERIFIED, result)
        self.assertEqual(result["incomplete"] + result["review"], [])

    # INCOMPLETE row
    def test_missing_credentials(self):
        self.assertStatus(clean(store={"id": "muravai", "error_code": "not_configured"}), INCOMPLETE,
                          "no ShipSidekick key")

    def test_ssk_source_error(self):
        self.assertStatus(clean(store={"id": "muravai", "error_code": "read_failed", "error": "SSK request failed"}),
                          INCOMPLETE, "SSK request failed")

    def test_ssk_not_read(self):
        self.assertStatus(clean(store=None), INCOMPLETE, "not read")

    def test_truncated_stock(self):
        self.assertStatus(clean(store=store(truncated=True)), INCOMPLETE, "truncated")

    def test_export_without_completeness_evidence(self):
        self.assertStatus(clean(export_complete=False), INCOMPLETE, "export is complete")

    def test_confirmed_no_shipment_day_needs_no_export(self):
        self.assertEqual(clean(eod=eod(no_shipments_confirmed=True), export_complete=False)["status"], VERIFIED)

    def test_test_environment_never_verified(self):
        self.assertStatus(clean(store=store(environment="test")), INCOMPLETE, "test environment")

    def test_stock_not_aligned_to_cutoff(self):
        self.assertStatus(clean(cutoff_aligned=False), INCOMPLETE, "cutoff")

    def test_missing_rule_package(self):
        self.assertStatus(clean("unknown-client"), INCOMPLETE, "No rule package")

    def test_stale_sheet(self):
        self.assertStatus(clean(eod=eod(report_date="2026-10-26")), INCOMPLETE, "stale")

    def test_blank_sheet_date(self):
        self.assertStatus(clean(eod=eod(report_date="")), INCOMPLETE, "blank or unreadable")

    def test_no_data_for_the_date(self):
        self.assertStatus(clean(eod=eod(INCOMPLETE, reasons=["No ShipSidekick shipments were provided for this date."])),
                          INCOMPLETE, "No ShipSidekick shipments")

    def test_eod_not_run(self):
        self.assertStatus(clean(eod=None), INCOMPLETE, "did not run")

    def test_compare_not_run(self):
        self.assertStatus(clean(compare=None), INCOMPLETE, "comparison did not run")

    # REVIEW row
    def test_eod_review(self):
        self.assertStatus(clean(eod=eod(REVIEW, reasons=["Filters (MUR001): gap +5."])), REVIEW, "gap +5")

    def test_ssk_row_different(self):
        self.assertStatus(clean(compare=compare([row(status="DIFFERENT", available=5, committed=5)])),
                          REVIEW, "MUR001: differs")

    def test_ssk_row_review(self):
        self.assertStatus(clean(compare=compare([row("MUR003", "REVIEW", ["Not found"], None, None)])),
                          REVIEW, "Not found")

    def test_unmatched_ssk_sku(self):
        self.assertStatus(clean(compare=compare(unmatched_ssk=[{"sku": "Teflon tape", "title": "Teflon tape"}])),
                          REVIEW, "not in the rules")

    def test_unmatched_sheet_product(self):
        self.assertStatus(clean(compare=compare(unmatched_sheet=["Mystery Item"])), REVIEW, "Mystery Item")

    def test_blank_sku(self):
        self.assertStatus(clean(compare=compare(blank_skus=2)), REVIEW, "have no SKU")

    def test_duplicate_sku(self):
        self.assertStatus(clean(compare=compare(duplicate_skus=["MUR001"])), REVIEW, "more than once")

    def test_future_sheet_date(self):
        self.assertStatus(clean(eod=eod(report_date="2026-10-28"), compare=compare(sheet_as_of="10/28/2026")),
                          REVIEW, "future")

    def test_wrong_row_write(self):
        self.assertStatus(clean(wrong_row_writes=["Incoming Stocks G329 (expected G328)"]), REVIEW, "G329")

    def test_no_warehouse_scope(self):
        with patch.dict(nightly_gate.WAREHOUSE_SCOPES, {}, clear=True):
            self.assertStatus(clean(), REVIEW, "warehouse scope")

    def test_no_balance_basis_even_when_both_bases_match(self):
        # committed = 0 makes both differences zero; that is still not an approved basis.
        with patch.object(nightly_gate, "BALANCE_BASIS", None):
            self.assertStatus(clean(), REVIEW, "balance basis")

    def test_no_shipment_deduction_timing(self):
        with patch.object(nightly_gate, "SHIPMENT_DEDUCTION", None):
            self.assertStatus(clean(), REVIEW, "deduction timing")

    def test_nothing_verified_with_rules_as_they_stand(self):
        with patch.multiple(nightly_gate, BALANCE_BASIS=None, SHIPMENT_DEDUCTION=None), \
                patch.dict(nightly_gate.WAREHOUSE_SCOPES, {}, clear=True):
            for client_id in ("muravai", "fascial-labs", "puravita", "nuerosmile"):
                with self.subTest(client_id):
                    self.assertNotEqual(clean(client_id)["status"], VERIFIED)

    def test_foreign_eod_rejected(self):
        self.assertStatus(clean(eod=eod(client_id="fascial-labs")), INCOMPLETE, "belongs to 'fascial-labs'")

    def test_compare_from_another_sheet_date(self):
        self.assertStatus(clean(compare=compare(sheet_as_of="10/26/2026")), INCOMPLETE, "Sheet dated")
        self.assertStatus(clean(compare=compare(sheet_as_of="")), INCOMPLETE, "Sheet dated")

    def test_deduction_rule_must_match_the_recount(self):
        with patch.object(nightly_gate, "SHIPMENT_DEDUCTION", "shipped"):
            self.assertStatus(clean(), REVIEW, "counts shipments by 'created_date'")

    def test_foreign_store_rejected(self):
        self.assertStatus(clean(store=store("fascial-labs")), INCOMPLETE, "belongs to 'fascial-labs'")

    def test_foreign_compare_rejected(self):
        self.assertStatus(clean(compare=compare(client_id="fascial-labs")), INCOMPLETE, "belongs to 'fascial-labs'")

    def test_stock_not_restricted_to_scope(self):
        # Today's inventory_levels sums every location, so an unscoped total can't pass.
        self.assertStatus(clean(store=store(scoped_locations=None)), REVIEW, "approved warehouse locations")
        self.assertStatus(clean(store=store(scoped_locations=["Miami", "Utah"])), REVIEW, "approved warehouse locations")

    def test_unresolved_component_held(self):
        item = {"sku": "BUNDLE-1", "title": "Bundle", "note": "Bundle in ShipSidekick (isBundle): not matched to one SKU"}
        self.assertStatus(clean(compare=compare(components=[item])), REVIEW, "BUNDLE-1")

    def test_component_used_by_kit_split_is_fine(self):
        item = {"sku": "shower hose", "title": "Shower Hose", "note": ssk_check.KIT_USED}
        self.assertEqual(clean(compare=compare(components=[item]))["status"], VERIFIED)

    def test_approved_basis_decides_one_basis_rows(self):
        held = row(status="REVIEW", notes=["Matches on one basis only (available vs on hand is undecided)"],
                   available=0, committed=12)
        with patch.object(nightly_gate, "BALANCE_BASIS", "available"):
            self.assertEqual(clean(compare=compare([held]))["status"], VERIFIED)
        with patch.object(nightly_gate, "BALANCE_BASIS", "available+committed"):
            self.assertStatus(clean(compare=compare([held])), REVIEW, "+12")

    def test_approved_basis_keeps_other_notes(self):
        held = row(status="REVIEW", notes=["Sheet not loaded"], available=None, committed=None)
        self.assertStatus(clean(compare=compare([held])), REVIEW, "Sheet not loaded")

    def test_unknown_basis_value(self):
        with patch.object(nightly_gate, "BALANCE_BASIS", "on hand"):
            self.assertStatus(clean(), REVIEW, "not recognised")

    def test_proposed_packages_never_verified(self):
        # Fascial, PuraVita and NeuroSmile as they stand today.
        for client_id in ("fascial-labs", "puravita", "nuerosmile"):
            with self.subTest(client_id):
                self.assertStatus(clean(client_id), REVIEW, "PROPOSED")

    def test_unknown_eod_status_is_incomplete(self):
        self.assertStatus(clean(eod=eod("SOMETHING")), INCOMPLETE, "SOMETHING")

    # Precedence
    def test_incomplete_wins_over_review(self):
        result = clean(store=store(environment="test"), compare=compare(duplicate_skus=["MUR001"]))
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertTrue(result["review"])

    def test_review_wins_over_verified(self):
        self.assertEqual(clean(wrong_row_writes=["X"])["status"], REVIEW)


class ExpectedDate(unittest.TestCase):
    def test_just_before_and_after_midnight_et(self):
        # 23:59 ET Oct 28 (03:59 UTC Oct 29) still expects Oct 27; 00:01 ET Oct 29 expects Oct 28.
        self.assertEqual(expected_sheet_date(datetime(2026, 10, 29, 3, 59, tzinfo=timezone.utc)).isoformat(),
                         "2026-10-27")
        self.assertEqual(expected_sheet_date(datetime(2026, 10, 29, 4, 1, tzinfo=timezone.utc)).isoformat(),
                         "2026-10-28")

    def test_dst_change(self):
        # Nov 1 2026: ET moves to UTC-5. 04:30 UTC Nov 2 is 23:30 ET Nov 1.
        self.assertEqual(expected_sheet_date(datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)).isoformat(),
                         "2026-10-31")

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            expected_sheet_date(datetime(2026, 10, 28, 8, 0))


if __name__ == "__main__":
    unittest.main()
