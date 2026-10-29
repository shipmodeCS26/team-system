"""Golden fixtures for the nightly check (accuracy plan Phase 1, Issue #34 step 4).

Synthetic, sanitized data shaped like a NeuroSmile Oct 27 run (two SKUs): no real client export,
order or customer data. Each test replays one of the Oct 19–28 mistakes and asserts the night's
result; `GOLDEN` is the result with every rule approved and every source clean.
"""
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import client_rules
import nightly_gate
import sheet_writes
from nightly_gate import INCOMPLETE, REVIEW, VERIFIED
from nightly_run import run_client

CLIENT = "nuerosmile"
NOW = datetime(2026, 10, 28, 4, 10, tzinfo=timezone.utc)  # 12:10 AM ET Oct 28: expects the Oct 27 Sheet
READ_AT = "2026-10-28T04:10:00+00:00"


def shipment(n, items, created="10/27/26", org="NeuroSmile", voided="No"):
    return {"Tracking Code": f"TRK{n:04d}", "Created Date": created, "Organization": org,
            "Order Name": f"#{1000 + n}", "Tracking Status": "pre-transit", "Voided": voided,
            "Mission Num": "M1", "Items": items, "Origin Address": "Miami, FL, 33166, US"}


def day_shipments():
    # 3 x Nerve Support, 2 x Magnesium Spray; one two-item order (the whole-order-count trap).
    return [shipment(1, "1x Nerve Support (NEURO-120)"),
            shipment(2, "1x Nerve Support (NEURO-120); 1x Magnesium Spray (MAG-SPRAY-360)"),
            shipment(3, "1x Nerve Support (NEURO-120)"),
            shipment(4, "1x Magnesium Spray (MAG-SPRAY-360)"),
            shipment(5, "1x Nerve Support (NEURO-120)", voided="Yes")]


def dashboard(as_of="27 Oct 2026", nerve=("3", "3,295"), spray=("2", "838")):
    return {"id": CLIENT, "as_of": as_of, "report_status": "", "warnings": [],
            "rows": [{"product": "Nerve Support", "shipped": nerve[0], "remaining": nerve[1]},
                     {"product": "Magnesium Spray", "shipped": spray[0], "remaining": spray[1]}]}


def store(nerve=3295, spray=838, **extra):
    levels = [{"sku": sku, "title": title, "product": "", "aliases": [], "available": qty, "committed": 0,
               "incoming": 0, "reserved": 0, "damaged": 0, "quality_control": 0, "safety_stock": 0,
               "locations": 1}
              for sku, title, qty in (("NEURO-120", "Nerve Support", nerve),
                                      ("MAG-SPRAY-360", "Magnesium Spray", spray))]
    return {"id": CLIENT, "levels": levels, "truncated": False, "environment": "production",
            "scoped_locations": ["Miami"], "fetched_at": READ_AT, **extra}


def night(**overrides):
    args = {"now": NOW, "dashboard": dashboard(), "shipments": day_shipments(), "store": store(),
            "export_complete": True, "cutoff_aligned": True}
    args.update(overrides)
    return run_client(CLIENT, **args)


@contextmanager
def rules_approved():
    """Every open business rule decided, as it would be once Carlos and Gly approve them."""
    with patch.object(client_rules.package(CLIENT), "status", "APPROVED"), \
            patch.dict(nightly_gate.WAREHOUSE_SCOPES, {CLIENT: ("Miami",)}), \
            patch.multiple(nightly_gate, BALANCE_BASIS="available", SHIPMENT_DEDUCTION="created_date"), \
            patch.dict(sheet_writes.CELL_MAPS, {CLIENT: {("EOD Sales", "2026-10-27 Nerve Support"): "C377"}}):
        yield


class GoldenNight(unittest.TestCase):
    def test_golden_clean_night_is_verified(self):
        with rules_approved():
            result = night()
        self.assertEqual(result["status"], VERIFIED, result["incomplete"] + result["review"])
        self.assertEqual(result["eod"]["orders"], 4)  # the voided row is excluded
        self.assertEqual([c["csv"] for c in result["eod"]["checks"]], [3, 2])

    def test_tonight_with_rules_as_they_stand_is_never_verified(self):
        result = night()
        self.assertNotEqual(result["status"], VERIFIED)
        reasons = " ".join(result["review"])
        for open_rule in ("PROPOSED", "warehouse scope", "balance basis", "deduction timing"):
            self.assertIn(open_rule, reasons)


class MistakeReplays(unittest.TestCase):
    """Section 1 rows 1–3 of the plan, each caught by the nightly check."""

    def test_whole_order_count_is_caught(self):
        # Counting the two-item order as one product (Oct 19–21 Onset Dashboard) gives 2 sprays → 1.
        with rules_approved():
            result = night(dashboard=dashboard(spray=("1", "838")))
        self.assertEqual(result["status"], REVIEW)
        self.assertTrue(any("gap -1" in r for r in result["review"]), result["review"])

    def test_wrong_sign_adjustment_is_caught(self):
        # An adjustment typed with the wrong sign leaves the Sheet 2 x 580 away from ShipSidekick.
        with rules_approved():
            result = night(dashboard=dashboard(nerve=("3", "4,455")))
        self.assertEqual(result["status"], REVIEW)
        self.assertTrue(any("NEU001" in r and "-1,160" in r for r in result["review"]), result["review"])

    def test_wrong_row_note_is_caught(self):
        write = {"tab": "EOD Sales", "key": "2026-10-27 Nerve Support", "cell": "C378", "value": "3"}
        with rules_approved():
            result = night(writes=[write], reread={("EOD Sales", "C378"): "3"})
        self.assertEqual(result["status"], REVIEW)
        self.assertEqual(result["wrong_row_writes"], ["EOD Sales C378 (expected C377 for 2026-10-27 Nerve Support)"])

    def test_stale_sheet_is_caught(self):
        # Fascial/Neurosmile Sheets 9–10 days behind: the Dashboard still says Oct 26.
        with rules_approved():
            result = night(dashboard=dashboard(as_of="26 Oct 2026"))
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertTrue(any("stale" in r for r in result["incomplete"]))

    def test_unknown_sku_is_caught(self):
        # A product the rules don't count (TrueSleep 3-pack / D3+K2 were dropped silently).
        rows = day_shipments() + [shipment(6, "1x Pill Carrier (PILL-CARRIER-360)")]
        with rules_approved():
            result = night(shipments=rows)
        self.assertEqual(result["status"], REVIEW)
        self.assertTrue(any("PILL-CARRIER-360" in r for r in result["review"]), result["review"])

    def test_missing_ssk_key_is_incomplete(self):
        with rules_approved():
            result = night(store={"id": CLIENT, "error_code": "not_configured"})
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertIn("ShipSidekick: no ShipSidekick key.", result["incomplete"])

    def test_unreadable_shipments_are_incomplete(self):
        with rules_approved():
            result = night(shipments=None)
        self.assertEqual(result["status"], INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
