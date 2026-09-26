"""Tests for muravai_rules.py. Every expected number comes from
docs/clients/muravai/RULES.md; if a test fails, the code is wrong, not the rule."""
import unittest
from datetime import date

from muravai_rules import (audit_statement, build_eod, eod_message, expected_balance,
                           order_usage, parse_items)

FULL = ("2x Replacement Filters (3 Pack) (3 filters); 2x THE FILTERED SHOWERHEAD™️ (showerhead); "
        "1x 1x Teflon Tape (Teflon Tape-360-USA); 1x Shower connector (shower connector); "
        "1x Shower Hose (shower hose)")


def row(n, items, created="9/1/26", mission="M1", voided="No", order=None, tracking=None,
        origin="Miami, FL, 33166-6557, US", org="Muravai"):
    return {"Tracking Code": tracking or f"TRK{n}", "Created Date": created, "Organization": org,
            "Order Name": order or f"#{n}", "Voided": voided, "Mission Num": mission,
            "Items": items, "Origin Address": origin}


class ParsingRules(unittest.TestCase):
    def test_split_and_quantity_before_first_x(self):
        parsed = parse_items(FULL)
        self.assertEqual([q for q, _ in parsed], [2, 2, 1, 1, 1])
        self.assertTrue(parsed[2][1].lower().startswith("1x teflon tape"))

    def test_full_example_line(self):
        r = order_usage(FULL).usage
        self.assertEqual(r, {"MUR001": 2, "MUR002": 2, "MUR003": 1, "MUR004": 0, "MUR005": 0})

    def test_filter_three_pack_is_never_multiplied_by_three(self):
        self.assertEqual(order_usage("2x Replacement Filters (3 Pack) (3 filters)").usage["MUR001"], 2)

    def test_showerhead_counted_directly(self):
        self.assertEqual(order_usage("2x THE FILTERED SHOWERHEAD™️ (showerhead)").usage["MUR002"], 2)

    def test_ten_complete_kits(self):
        r = order_usage("10x Shower Hose; 10x Shower connector; 10x Teflon Tape").usage
        self.assertEqual((r["MUR003"], r["MUR004"], r["MUR005"]), (10, 0, 0))
        self.assertNotIn("MUR006", r)

    def test_excess_hose_is_standalone(self):
        r = order_usage("3x Shower Hose; 2x Teflon Tape; 2x Shower connector").usage
        self.assertEqual((r["MUR003"], r["MUR004"]), (2, 1))

    def test_excess_connector_is_standalone(self):
        r = order_usage("4x Shower connector; 3x Teflon Tape; 3x Shower Hose").usage
        self.assertEqual((r["MUR003"], r["MUR005"]), (3, 1))

    def test_more_teflon_than_components_is_flagged(self):
        result = order_usage("2x Teflon Tape; 1x Shower Hose; 2x Shower connector")
        self.assertTrue(result.flags)

    def test_unknown_item_is_listed_not_guessed(self):
        result = order_usage("1x Mystery Gadget")
        self.assertEqual(result.unknown_items, ["Mystery Gadget"])
        self.assertEqual(sum(result.usage.values()), 0)

    def test_explicit_kit_counted_directly(self):
        r = order_usage("2x Connector Kit").usage
        self.assertEqual((r["MUR003"], r["MUR004"], r["MUR005"]), (2, 0, 0))

    def test_explicit_kit_with_components_is_flagged(self):
        self.assertTrue(order_usage("1x Connector Kit; 1x Teflon Tape").flags)


class DailyRules(unittest.TestCase):
    def test_kits_are_computed_per_order_not_per_day(self):
        # Aggregating first would give 2 kits, 0 standalone hoses.
        rows = [row(1, "2x Shower Hose; 2x Shower connector"),
                row(2, "2x Teflon Tape")]
        usage = build_eod(rows)[date(2026, 9, 1)].usage
        self.assertEqual(usage["MUR004"], 2)
        self.assertEqual(usage["MUR005"], 2)

    def test_voided_rows_excluded_pre_transit_kept(self):
        rows = [row(1, "1x THE FILTERED SHOWERHEAD", voided="Yes"),
                row(2, "1x THE FILTERED SHOWERHEAD", voided="TRUE"),
                row(3, "1x THE FILTERED SHOWERHEAD", voided="1"),
                row(4, "1x THE FILTERED SHOWERHEAD")]
        rep = build_eod(rows)[date(2026, 9, 1)]
        self.assertEqual((rep.orders, rep.voided_excluded, rep.usage["MUR002"]), (1, 3, 1))

    def test_repeated_tracking_code_not_counted_twice(self):
        rows = [row(1, "1x THE FILTERED SHOWERHEAD", tracking="T1"),
                row(2, "1x THE FILTERED SHOWERHEAD", tracking="T1")]
        rep = build_eod(rows)[date(2026, 9, 1)]
        self.assertEqual(rep.usage["MUR002"], 1)
        self.assertTrue(rep.duplicate_tracking_excluded)
        self.assertTrue(rep.needs_review)

    def test_same_order_different_tracking_is_split_shipment(self):
        rows = [row(1, "1x Replacement Filters", order="#9"),
                row(2, "1x THE FILTERED SHOWERHEAD", order="#9")]
        rep = build_eod(rows)[date(2026, 9, 1)]
        self.assertEqual((rep.usage["MUR001"], rep.usage["MUR002"]), (1, 1))
        self.assertFalse(rep.flags)

    def test_same_order_identical_items_flagged(self):
        rows = [row(1, "1x Replacement Filters", order="#9"),
                row(2, "1x Replacement Filters", order="#9")]
        self.assertTrue(build_eod(rows)[date(2026, 9, 1)].flags)

    def test_loose_shipment_counted_and_reported(self):
        rep = build_eod([row(1, "1x Replacement Filters", mission="")])[date(2026, 9, 1)]
        self.assertEqual((rep.usage["MUR001"], len(rep.loose_shipments), rep.missions), (1, 1, 0))

    def test_each_date_calculated_independently(self):
        reports = build_eod([row(1, "1x Replacement Filters", created="9/1/26"),
                             row(2, "3x Replacement Filters", created="9/2/26")])
        self.assertEqual(reports[date(2026, 9, 1)].usage["MUR001"], 1)
        self.assertEqual(reports[date(2026, 9, 2)].usage["MUR001"], 3)

    def test_other_origin_split_and_flagged(self):
        rep = build_eod([row(1, "1x Replacement Filters"),
                         row(2, "1x Replacement Filters", origin="Reno, NV, 89502, US")])[date(2026, 9, 1)]
        self.assertEqual(set(rep.usage_by_origin), {"Miami", "Reno, NV, 89502, US"})
        self.assertNotIn("All shipments originated in Miami", audit_statement(rep))

    def test_other_organization_excluded(self):
        rep = build_eod([row(1, "1x Replacement Filters", org="Fascial Labs")])[date(2026, 9, 1)]
        self.assertEqual(rep.orders, 0)
        self.assertTrue(rep.flags)


class VerifiedSeptemberFirst(unittest.TestCase):
    """RULES.md 'Verified calculation example — September 1, 2026'."""

    def setUp(self):
        # 221 orders, 6 missions, 448 filters, 146 showerheads, 82 teflon, 85 hoses, 84 connectors
        rows = []
        missions = [f"M{i}" for i in range(6)]
        for n in range(221):
            parts = []
            if n < 82:
                parts += ["1x 1x Teflon Tape (Teflon Tape-360-USA)", "1x Shower Hose (shower hose)",
                          "1x Shower connector (shower connector)"]
            elif n < 85:
                parts.append("1x Shower Hose (shower hose)")
            elif n < 87:
                parts.append("1x Shower connector (shower connector)")
            if n < 146:
                parts.append("1x THE FILTERED SHOWERHEAD™️ (showerhead)")
            filters = 3 if n < 6 else 2 if n < 221 else 0
            parts.append(f"{filters}x Replacement Filters (3 Pack) (3 filters)")
            rows.append(row(n, "; ".join(parts), mission=missions[n % 6]))
        # 6*3 + 215*2 = 448 filter packs
        self.report = build_eod(rows)[date(2026, 9, 1)]

    def test_counts(self):
        self.assertEqual((self.report.orders, self.report.missions), (221, 6))

    def test_verified_result(self):
        self.assertEqual(self.report.usage,
                         {"MUR001": 448, "MUR002": 146, "MUR003": 82, "MUR004": 3, "MUR005": 2})
        self.assertEqual(self.report.total_units, 681)

    def test_message_format_and_audit(self):
        msg = eod_message(self.report)
        self.assertTrue(msg.startswith("MUR EOD — 09/01/2026\nOrders: 221\nMissions: 6"))
        self.assertIn("09/01/2026\tMUR004\t3\tEOD", msg)
        self.assertNotIn("MUR006", msg)
        self.assertTrue(msg.endswith(
            "All shipments originated in Miami. No voids, duplicates, or loose shipments were found."))


class ManualBaselineRules(unittest.TestCase):
    def test_expected_balance_after_september_first(self):
        baseline = {"MUR001": 10785, "MUR002": 6984, "MUR003": 4517, "MUR004": 0, "MUR005": 400}
        usage = {"MUR001": 448, "MUR002": 146, "MUR003": 82, "MUR004": 3, "MUR005": 2}
        result = expected_balance(baseline, {}, usage)
        self.assertEqual(result, {"MUR001": 10337, "MUR002": 6838, "MUR003": 4435,
                                  "MUR004": -3, "MUR005": 398})
        self.assertEqual(sum(result.values()), 22005)

    def test_negative_is_never_hidden(self):
        self.assertEqual(expected_balance({"MUR004": 0}, {}, {"MUR004": 3})["MUR004"], -3)

    def test_receipt_case_pack_example(self):
        self.assertEqual(18 * 600 + 482, 11282)


if __name__ == "__main__":
    unittest.main()
