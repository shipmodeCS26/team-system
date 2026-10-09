"""#14: daily Shopify orders vs. EOD shipped vs. Sheet units sold (read-only, display-only)."""
import base64
import copy
import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import app
import daily_orders
import eod
import muravai_rules
import shopify_source
from test_muravai_rules import row

DAY = date(2026, 9, 1)
SEP1 = "2026-09-01T15:00:00Z"  # 11:00 ET on Sep 1


def item(name, qty, sku="", changed=False):
    return {"name": name, "sku": sku, "qty": qty, "changed": changed}


def order(name, items, created=SEP1, cancelled=None, financial="PAID", fulfillment="FULFILLED", test=False,
          truncated=False):
    return {"name": name, "created_at": created, "cancelled_at": cancelled, "test": test, "financial": financial,
            "fulfillment": fulfillment, "items": items, "items_truncated": truncated}


def shopify(*orders, complete=True, oldest=None, all_orders=True):
    return {"orders": list(orders), "complete": complete, "all_orders": all_orders,
            "oldest_read": oldest or (orders[-1]["created_at"] if orders else None), "fetched_at": "x"}


def dashboard(as_of="01 Sep 2026", **shipped):
    names = {"MUR001": "Replacement Filters, 3-pack", "MUR002": "Filtered Showerhead", "MUR003": "Connector Kit Box",
             "MUR004": "Shower Hose", "MUR005": "Bracket / Connector"}
    return {"as_of": as_of, "rows": [{"product": names[sku], "shipped": str(qty)} for sku, qty in shipped.items()]}


def september_first():
    """The RULES.md verified day (221 orders, 681 units), once as ShipSidekick labels and once as
    the same orders in Shopify."""
    labels, orders = [], []
    for n in range(221):
        ssk, shop = [], []
        if n < 82:
            ssk += ["1x 1x Teflon Tape (Teflon Tape-360-USA)", "1x Shower Hose (shower hose)",
                    "1x Shower connector (shower connector)"]
            shop += [item("Teflon Tape", 1, "Teflon Tape-360-USA"), item("Shower Hose", 1), item("Shower connector", 1)]
        elif n < 85:
            ssk.append("1x Shower Hose (shower hose)")
            shop.append(item("Shower Hose", 1))
        elif n < 87:
            ssk.append("1x Shower connector (shower connector)")
            shop.append(item("Shower connector", 1))
        if n < 146:
            ssk.append("1x THE FILTERED SHOWERHEAD™️ (showerhead)")
            shop.append(item("THE FILTERED SHOWERHEAD™️", 1))
        filters = 3 if n < 6 else 2
        ssk.append(f"{filters}x Replacement Filters (3 Pack) (3 filters)")
        shop.append(item("Replacement Filters (3 Pack)", filters, "MUR-FILTER-3"))
        labels.append(row(n, "; ".join(ssk), order=f"#{1000 + n}"))
        orders.append(order(f"#{1000 + n}", shop))
    return labels, orders


SHEET_681 = dashboard(MUR001=448, MUR002=146, MUR003=82, MUR004=3, MUR005=2)


TODAY = date(2026, 9, 5)


def lookup(*found, complete=True, all_orders=True):
    """find_orders() result: `found` is (name, [orders]) pairs."""
    return {"found": {name: {"orders": list(orders), "search_complete": complete} for name, orders in found},
            "all_orders": all_orders}


def compare(orders, labels, sheet=SHEET_681, client="muravai", lookups=None, today=TODAY, **kw):
    return daily_orders.compare(client, DAY, shopify(*orders, **kw), labels, sheet, lookups, today=today)


def kinds(result):
    return {e["type"]: [o["order"] for o in e["orders"]] for e in result["exceptions"] + result["timing_orders"]}


class VerifiedDayTests(unittest.TestCase):
    def test_all_three_columns_agree_at_681(self):
        labels, orders = september_first()
        result = compare(orders, labels)
        self.assertEqual({r["sku"]: (r["shopify_ordered"], r["eod_shipped"], r["sheet_sold"]) for r in result["rows"]},
                         {"MUR001": (448,) * 3, "MUR002": (146,) * 3, "MUR003": (82,) * 3, "MUR004": (3,) * 3,
                          "MUR005": (2,) * 3})
        self.assertEqual((result["totals"]["shopify_ordered"], result["totals"]["eod_shipped"],
                          result["totals"]["sheet_sold"], result["totals"]["unexplained"]), (681, 681, 681, 0))
        self.assertEqual((result["exceptions"], result["timing_orders"]), ([], []))
        self.assertEqual(result["counts"]["orders"], 221)

    def test_injected_missing_orders_appear_as_exceptions(self):
        labels, orders = september_first()
        dropped = orders.pop(10)  # shipped, but not in Shopify
        orders.append(order("#5000", [item("THE FILTERED SHOWERHEAD™️", 1)], fulfillment="UNFULFILLED"))
        self.assertEqual(daily_orders.lookup_names("muravai", DAY, shopify(*orders), labels), [dropped["name"]])
        result = compare(orders, labels, lookups=lookup((dropped["name"], [])))
        self.assertEqual(kinds(result)["not_in_shopify"], [dropped["name"]])
        self.assertEqual(kinds(result)["not_in_ssk"], ["#5000"])
        # #1010's 4 units (kit, showerhead, 2 filter packs) shipped with no Shopify order: unexplained.
        # #5000's showerhead was ordered and not shipped yet: timing.
        self.assertEqual((result["totals"]["eod_minus_shopify"], result["totals"]["timing"],
                          result["totals"]["unexplained"]), (3, -1, 4))

    def test_fulfilled_in_shopify_without_label_is_its_own_exception_not_timing(self):
        result = compare([order("#1", [item("Shower Hose", 1)]), order("#2", [item("Shower Hose", 1)])],
                         [row(2, "1x Shower Hose", order="#2")])
        self.assertEqual(kinds(result), {"fulfilled_not_in_ssk": ["#1"]})
        self.assertEqual((result["totals"]["timing"], result["totals"]["unexplained"]), (0, -1))


class OrderRulesTests(unittest.TestCase):
    def test_cancelled_and_test_orders_excluded(self):
        labels = [row(1, "1x Shower Hose", order="#1")]
        orders = [order("#1", [item("Shower Hose", 1)], cancelled="2026-09-01T16:00:00Z"),
                  order("#2", [item("Shower Hose", 5)], cancelled="2026-09-01T16:00:00Z"),
                  order("#3", [item("Shower Hose", 7)], test=True)]
        result = compare(orders, labels)
        self.assertEqual(result["totals"]["shopify_ordered"], 0)
        self.assertEqual((result["counts"]["cancelled_orders"], result["counts"]["test_orders"]), (2, 1))
        self.assertEqual(kinds(result)["cancelled_but_shipped"], ["#1"])

    def test_earlier_order_cancelled_but_shipped_today_is_flagged_not_timing(self):
        orders = [order("#1", [item("Shower Hose", 1)], created="2026-08-31T15:00:00Z", cancelled="2026-08-31T16:00:00Z")]
        result = compare(orders, [row(1, "1x Shower Hose", order="#1")])
        self.assertEqual(kinds(result), {"cancelled_but_shipped": ["#1"]})
        self.assertEqual(result["totals"]["unexplained"], 1)

    def test_refund_after_shipping_is_flagged_not_subtracted(self):
        labels = [row(1, "2x Shower Hose", order="#1")]
        orders = [order("#1", [item("Shower Hose", 2, changed=True)], financial="PARTIALLY_REFUNDED")]
        result = compare(orders, labels)
        self.assertEqual(result["totals"]["shopify_ordered"], 2)
        self.assertEqual(result["totals"]["unexplained"], 0)
        self.assertIn("refunded_after_shipping", kinds(result))
        self.assertIn("edited", kinds(result))

    def test_rule_warnings_are_never_reconciled(self):
        # Two tapes with one hose and one connector: the Muravai rule flags the kit structure.
        items = [item("Teflon Tape", 2), item("Shower Hose", 1), item("Shower connector", 1)]
        result = compare([order("#1", items)], [row(1, "2x Teflon Tape; 1x Shower Hose; 1x Shower connector", order="#1")])
        self.assertIn("rule_review", kinds(result))
        self.assertNotIn("quantity_mismatch", kinds(result))
        old = order("#2", items, created="2026-08-31T15:00:00Z")
        later = compare([old], [row(2, "2x Teflon Tape; 1x Shower Hose; 1x Shower connector", order="#2")])
        self.assertEqual(kinds(later), {"rule_review": ["#2"]})
        self.assertEqual(later["totals"]["timing"], 0)

    def test_kits_use_the_client_rules(self):
        labels = [row(1, "1x Teflon Tape; 1x Shower Hose; 1x Shower connector", order="#1")]
        orders = [order("#1", [item("Teflon Tape", 1), item("Shower Hose", 1), item("Shower connector", 1)])]
        result = compare(orders, labels)
        by = {r["sku"]: r["shopify_ordered"] for r in result["rows"]}
        self.assertEqual((by["MUR003"], by["MUR004"], by["MUR005"]), (1, 0, 0))
        self.assertEqual(result["exceptions"], [])

    def test_quantity_mismatch(self):
        result = compare([order("#1", [item("Shower Hose", 2)])], [row(1, "1x Shower Hose", order="#1")])
        self.assertEqual(kinds(result)["quantity_mismatch"], ["#1"])
        self.assertEqual(result["exceptions"][0]["orders"][0]["units"], {"MUR004": -1})

    def test_unmapped_item_is_never_compared(self):
        result = compare([order("#1", [item("Mystery gadget", 1)])], [row(1, "1x Shower Hose", order="#1")])
        self.assertIn("unmapped", kinds(result))
        self.assertNotIn("quantity_mismatch", kinds(result))

    def test_order_name_with_or_without_hash_matches(self):
        result = compare([order("#77", [item("Shower Hose", 1)])], [row(1, "1x Shower Hose", order="77")])
        self.assertEqual(result["exceptions"], [])

    def test_alias_client_reads_the_shopify_sku(self):
        labels = [row(1, "2x TrueForm (FASCSUPP-1)", order="#1", org="Fascial Labs")]
        orders = [order("#1", [item("TrueForm Fascial Release Support", 2, "FASCSUPP-1")])]
        result = compare(orders, labels, sheet=None, client="fascial-labs")
        self.assertEqual(result["rows"][0]["shopify_ordered"], 2)
        self.assertEqual(result["rows"][0]["eod_shipped"], 2)
        self.assertEqual(result["exceptions"], [])

    def test_voided_and_other_client_labels_ignored(self):
        labels = [row(1, "1x Shower Hose", order="#1", voided="Yes"), row(2, "1x Shower Hose", order="#2", org="PuraVita")]
        result = compare([], labels)
        self.assertNotIn("not_in_shopify", kinds(result))


class LookupTests(unittest.TestCase):
    """Orders shipped on the day but placed before the two-day window are looked up by name."""
    LABELS = [row(1, "1x Shower Hose", order="#OLD")]

    def test_found_older_order_is_timing(self):
        old = order("#OLD", [item("Shower Hose", 1)], created="2026-08-20T15:00:00Z")
        result = compare([], self.LABELS, lookups=lookup(("#OLD", [old])))
        self.assertEqual(kinds(result), {"ordered_earlier": ["#OLD"]})
        self.assertEqual(result["totals"]["unexplained"], 0)

    def test_absent_only_when_searched_everywhere(self):
        everywhere = compare([], self.LABELS, lookups=lookup(("#OLD", [])))
        recent = compare([], self.LABELS, lookups=lookup(("#OLD", []), all_orders=False))
        self.assertEqual(everywhere["exceptions"][0]["orders"][0]["detail"], "No Shopify order has this name")
        self.assertIn("last 60 days", recent["exceptions"][0]["orders"][0]["detail"])

    def test_lookup_matches_with_or_without_hash(self):
        old = order("#OLD", [item("Shower Hose", 1)], created="2026-08-20T15:00:00Z")
        result = compare([], [row(1, "1x Shower Hose", order="OLD")], lookups=lookup(("OLD", [old])))
        self.assertEqual(kinds(result), {"ordered_earlier": ["OLD"]})
        self.assertEqual(shopify_source._name_key(" #OLD "), shopify_source._name_key("old"))

    def test_incomplete_lookup_with_a_candidate_is_not_used(self):
        old = order("#OLD", [item("Shower Hose", 1)], created="2026-08-20T15:00:00Z")
        result = compare([], self.LABELS, lookups=lookup(("#OLD", [old]), complete=False))
        self.assertEqual((kinds(result), result["counts"]["not_checked"]), ({}, 1))

    def test_edited_earlier_order_is_never_timing(self):
        old = order("#OLD", [item("Shower Hose", 2, changed=True)], created="2026-08-31T15:00:00Z")
        result = compare([old], [row(1, "1x Shower Hose", order="#OLD", created="8/31/26"),
                                 row(2, "1x Shower Hose", order="#OLD")])
        self.assertEqual(kinds(result), {"edited": ["#OLD"]})
        self.assertEqual((result["totals"]["timing"], result["totals"]["unexplained"]), (0, 1))

    def test_not_looked_up_or_incomplete_is_not_checked(self):
        for lookups in (None, lookup(("#OLD", []), complete=False)):
            result = compare([], self.LABELS, lookups=lookups)
            self.assertNotIn("not_in_shopify", kinds(result))
            self.assertEqual(result["counts"]["not_checked"], 1)

    def test_lookup_is_capped(self):
        labels = [row(n, "1x Shower Hose", order=f"#{n}") for n in range(daily_orders.LOOKUP_LIMIT + 5)]
        self.assertEqual(len(daily_orders.lookup_names("muravai", DAY, shopify(), labels)), daily_orders.LOOKUP_LIMIT)

    def test_earlier_refund_and_test_orders(self):
        refunded = order("#OLD", [item("Shower Hose", 1)], created="2026-08-31T15:00:00Z", financial="REFUNDED")
        self.assertIn("refunded_after_shipping", kinds(compare([refunded], self.LABELS)))
        test = order("#OLD", [item("Shower Hose", 1)], created="2026-08-31T15:00:00Z", test=True)
        result = compare([test], self.LABELS)
        self.assertEqual(kinds(result), {"test_order_shipped": ["#OLD"]})
        self.assertEqual(result["totals"]["unexplained"], 1)

    def test_reship_of_an_already_shipped_order_is_not_timing(self):
        old = order("#OLD", [item("Shower Hose", 1)], created="2026-08-31T15:00:00Z")
        labels = [row(1, "1x Shower Hose", order="#OLD", created="8/31/26"),
                  row(2, "1x Shower Hose", order="#OLD")]
        result = compare([old], labels)
        self.assertEqual(kinds(result), {"quantity_mismatch": ["#OLD"]})
        self.assertEqual((result["totals"]["timing"], result["totals"]["unexplained"]), (0, 1))


class TimingTests(unittest.TestCase):
    def test_day_is_eastern_midnight_to_midnight(self):
        late = order("#1", [item("Shower Hose", 1)], created="2026-09-02T03:30:00Z")  # 23:30 ET Sep 1
        early = order("#2", [item("Shower Hose", 1)], created="2026-09-01T03:30:00Z")  # 23:30 ET Aug 31
        result = compare([late, early], [row(1, "1x Shower Hose", order="#1"),
                                         row(2, "1x Shower Hose", order="#2", created="8/31/26")])
        self.assertEqual(result["totals"]["shopify_ordered"], 1)
        self.assertEqual(result["exceptions"], [])
        self.assertEqual(daily_orders.window(DAY)[0].isoformat(), "2026-08-30T00:00:00-04:00")

    def test_shipped_next_day_and_ordered_day_before_are_timing(self):
        orders = [order("#1", [item("Shower Hose", 2)]),
                  order("#2", [item("THE FILTERED SHOWERHEAD", 1)], created="2026-08-31T15:00:00Z")]
        labels = [row(1, "2x Shower Hose", order="#1", created="9/2/26"), row(2, "1x THE FILTERED SHOWERHEAD", order="#2")]
        result = compare(orders, labels)
        by = {r["sku"]: r for r in result["rows"]}
        self.assertEqual((by["MUR004"]["eod_minus_shopify"], by["MUR004"]["timing"], by["MUR004"]["unexplained"]), (-2, -2, 0))
        self.assertEqual((by["MUR002"]["eod_minus_shopify"], by["MUR002"]["timing"], by["MUR002"]["unexplained"]), (1, 1, 0))
        self.assertEqual(kinds(result), {"shipped_later": ["#1"], "ordered_earlier": ["#2"]})
        self.assertEqual(result["exceptions"], [])

    def test_unshipped_remainder_of_a_partly_shipped_order_is_timing(self):
        orders = [order("#1", [item("Shower Hose", 10)], fulfillment="PARTIALLY_FULFILLED")]
        result = compare(orders, [row(1, "1x Shower Hose", order="#1")])
        self.assertEqual(kinds(result), {"partly_shipped": ["#1"]})
        self.assertEqual((result["totals"]["timing"], result["totals"]["unexplained"]), (-9, 0))
        fulfilled = compare([order("#1", [item("Shower Hose", 10)])], [row(1, "1x Shower Hose", order="#1")])
        self.assertEqual(kinds(fulfilled), {"quantity_mismatch": ["#1"]})

    def test_incomplete_shopify_day_blanks_the_column_and_timing(self):
        result = compare([order("#1", [item("Shower Hose", 1)], fulfillment="UNFULFILLED")], [],
                         complete=False, oldest=SEP1)
        self.assertIsNone(result["totals"]["shopify_ordered"])
        self.assertIsNone(result["rows"][3]["timing"])
        self.assertTrue(any("incomplete" in n for n in result["notes"]))

    def test_older_window_unread_means_not_in_shopify_is_not_asserted(self):
        result = compare([order("#1", [item("Shower Hose", 1)])], [row(9, "1x Shower Hose", order="#9")],
                         complete=False, oldest="2026-08-31T12:00:00Z")
        self.assertEqual(result["rows"][3]["shopify_ordered"], 1)
        self.assertNotIn("not_in_shopify", kinds(result))
        self.assertEqual(result["counts"]["not_checked"], 1)


class ShopifyLimitsTests(unittest.TestCase):
    def test_old_day_without_read_all_orders_is_not_compared(self):
        result = compare([], [row(1, "1x Shower Hose", order="#1")], all_orders=False, today=date(2026, 11, 15))
        self.assertIsNone(result["totals"]["shopify_ordered"])
        self.assertNotIn("not_in_shopify", kinds(result))
        self.assertTrue(any("read_all_orders" in n for n in result["notes"]))

    def test_truncated_order_hides_shopify_totals(self):
        result = compare([order("#1", [item("Shower Hose", 1)], truncated=True)], [row(1, "1x Shower Hose", order="#1")])
        self.assertIsNone(result["totals"]["shopify_ordered"])
        self.assertIsNone(result["totals"]["timing"])
        self.assertIn("items_truncated", kinds(result))

    def test_digital_only_orders_need_no_label(self):
        gift = order("#1", [], fulfillment="UNFULFILLED")
        gift["digital_only"] = True
        result = compare([gift], [])
        self.assertEqual((result["exceptions"], result["counts"]["digital_orders"], result["counts"]["orders"]), ([], 1, 0))

    def test_internal_spaces_in_names_are_kept(self):
        self.assertEqual(daily_orders.order_key(" #SM 10 "), "sm 10")
        self.assertNotEqual(daily_orders.order_key("SM 10"), daily_orders.order_key("SM10"))
        self.assertEqual(daily_orders.order_key("#1001"), daily_orders.order_key("1001"))


class SheetColumnTests(unittest.TestCase):
    def test_other_day_dashboard_is_not_compared(self):
        result = compare([], [], sheet=dashboard("02 Sep 2026", MUR001=1))
        self.assertIsNone(result["totals"]["sheet_sold"])

    def test_pending_cell_is_never_zero(self):
        sheet = dashboard(MUR001=448)
        sheet["rows"].append({"product": "Shower Hose", "shipped": "#REF!"})
        result = compare([], [], sheet=sheet)
        by = {r["sku"]: r["sheet_sold"] for r in result["rows"]}
        self.assertEqual((by["MUR001"], by["MUR004"], by["MUR002"]), (448, None, 0))

    def test_dashboard_cut_off_is_not_compared(self):
        sheet = dashboard(MUR001=448)
        sheet["may_continue"] = True
        result = compare([], [], sheet=sheet)
        self.assertIsNone(result["totals"]["sheet_sold"])
        self.assertTrue(any("may continue" in n for n in result["notes"]))

    def test_as_of_formats(self):
        for text in ("01 Sep 2026", "Tue, 01 Sep 2026", "9/1/2026", "2026-09-01", "Sep 1, 2026"):
            self.assertEqual(daily_orders.sheet_day(text), DAY, text)


class NeverChangesEodTests(unittest.TestCase):
    def test_inputs_and_eod_are_unchanged(self):
        labels, orders = september_first()
        before_rows, before_orders = copy.deepcopy(labels), copy.deepcopy(orders)
        before = eod.build_eod(labels, muravai_rules.RULES)[DAY].usage
        compare(orders[:-5], labels)
        self.assertEqual((labels, orders), (before_rows, before_orders))
        self.assertEqual(eod.build_eod(labels, muravai_rules.RULES)[DAY].usage, before)

    def test_no_rules_no_numbers(self):
        self.assertEqual(daily_orders.compare("claritymd", DAY, shopify(), [], None)["error_code"], "no_rules")


class ShopifyReadTests(unittest.TestCase):
    STORES = {"muravai": {"shop": "muravai-test.myshopify.com", "token": "shpat_x"}}

    def fake(self, scopes, pages, throttle=0):
        calls = []

        class Resp:
            status_code = 200

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        state = {"throttle": throttle}

        def post(url, json=None, headers=None, timeout=None, data=None):
            calls.append(json)
            if json["query"] == shopify_source.SCOPES_QUERY:
                return Resp({"data": {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in scopes]}}})
            assert json["query"] in (shopify_source.DAY_ORDERS_QUERY, shopify_source.ORDER_QUERY)
            if state["throttle"]:
                state["throttle"] -= 1
                return Resp({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]})
            page = int(json["variables"].get("after") or 0)
            return Resp({"data": {"orders": {"nodes": pages[page], "pageInfo": {
                "hasNextPage": page + 1 < len(pages), "endCursor": str(page + 1)}}}})
        return post, calls

    def node(self, name, created=SEP1):
        return {"name": name, "createdAt": created, "cancelledAt": None, "test": False, "displayFinancialStatus": "PAID",
                "displayFulfillmentStatus": "FULFILLED", "lineItems": {"nodes": [
                    {"name": "Shower Hose", "sku": "H", "quantity": 2, "currentQuantity": 1, "requiresShipping": True},
                    {"name": "Gift card", "sku": "G", "quantity": 1, "currentQuantity": 1, "requiresShipping": False}],
                    "pageInfo": {"hasNextPage": False}}}

    def read(self, scopes, pages, throttle=0):
        post, calls = self.fake(scopes, pages, throttle)
        waits = []
        start, end = daily_orders.window(DAY)
        with patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(self.STORES)}), \
                patch("shopify_source.requests.post", post):
            result = shopify_source.read_day_orders("muravai", start, end, sleep=waits.append)
        return result, calls, waits

    def test_reads_window_newest_first_without_customer_fields(self):
        result, calls, _ = self.read(["read_orders", "read_products"], [[self.node("#2")], [self.node("#1")]])
        self.assertEqual([o["name"] for o in result["orders"]], ["#2", "#1"])
        self.assertTrue(result["complete"])
        self.assertFalse(result["all_orders"])
        self.assertEqual(result["orders"][0]["items"], [{"name": "Shower Hose", "sku": "H", "qty": 2, "changed": True}])
        self.assertEqual(calls[1]["variables"]["q"],
                         "created_at:>='2026-08-30T04:00:00Z' created_at:<'2026-09-02T04:00:00Z'")
        for field in ("customer", "shippingAddress", "billingAddress", "email", "phone", "note"):
            self.assertNotIn(field, shopify_source.DAY_ORDERS_QUERY)

    def test_digital_only_order_is_marked(self):
        gift = self.node("#3")
        gift["lineItems"]["nodes"] = gift["lineItems"]["nodes"][1:]  # gift card only
        result, _, _ = self.read(["read_orders", "read_products"], [[gift, self.node("#2")]])
        self.assertEqual([o["digital_only"] for o in result["orders"]], [True, False])
        self.assertEqual(result["orders"][0]["items"], [])

    def test_find_orders_matches_exact_names_only(self):
        post, calls = self.fake(["read_orders", "read_all_orders", "read_products"],
                                [[self.node("#OLD"), self.node("#OLD-2")]])
        with patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(self.STORES)}), \
                patch("shopify_source.requests.post", post):
            result = shopify_source.find_orders("muravai", ["#OLD", "bad\x00name"], sleep=lambda s: None)
        self.assertTrue(result["all_orders"])
        self.assertEqual([o["name"] for o in result["found"]["#OLD"]["orders"]], ["#OLD"])
        self.assertTrue(result["found"]["#OLD"]["search_complete"])
        self.assertNotIn("bad\x00name", result["found"])
        self.assertEqual([c["query"] for c in calls], [shopify_source.SCOPES_QUERY, shopify_source.ORDER_QUERY])

    def test_write_scope_and_missing_read_orders_are_refused(self):
        for scopes, code in ((["read_orders", "read_products", "write_orders"], "write_scope_granted"),
                             (["read_products"], "order_scope")):
            with self.assertRaises(shopify_source.SourceError) as error:
                self.read(scopes, [[]])
            self.assertEqual(error.exception.code, code)

    def test_throttled_reads_wait_and_retry(self):
        result, _, waits = self.read(["read_orders", "read_products"], [[self.node("#1")]], throttle=2)
        self.assertEqual((len(result["orders"]), waits), (1, [2, 4]))


class JobTests(unittest.TestCase):
    def setUp(self):
        daily_orders._jobs.clear()

    def test_started_once_and_reused(self):
        started = []

        class Thread:
            def __init__(self, target, args, daemon):
                started.append(args)

            def start(self):
                pass

        first = daily_orders.status("muravai", DAY, date(2026, 9, 5), start=Thread)
        again = daily_orders.status("muravai", DAY, date(2026, 9, 5), start=Thread)
        self.assertEqual((first["status"], again["status"], len(started)), ("running", "running", 1))

    def test_concurrent_reads_are_capped(self):
        class Thread:
            def __init__(self, target, args, daemon):
                pass

            def start(self):
                pass

        for n in range(daily_orders.MAX_RUNNING):
            daily_orders.status("muravai", date(2026, 9, n + 1), date(2026, 9, 30), start=Thread)
        busy = daily_orders.status("muravai", date(2026, 9, 20), date(2026, 9, 30), start=Thread)
        self.assertEqual(busy["status"], "failed")
        self.assertEqual(daily_orders.status("muravai", DAY, date(2026, 9, 30), start=Thread)["status"], "running")

    def test_failure_is_reported_without_order_data_in_logs(self):
        job = {"status": "running"}
        with patch("daily_orders._read_and_compare", side_effect=shopify_source.SourceError("order_scope")), \
                self.assertLogs(level="WARNING") as logs:
            daily_orders._run(("muravai", DAY), job)
        self.assertEqual(job["status"], "failed")
        self.assertIn("read_orders", job["error"])
        self.assertTrue(all("#" not in line for line in logs.output))


class EndpointTests(unittest.TestCase):
    ENV = {"SHOPIFY_ENABLED": "true", "INVENTORY_SHEETS_ENABLED": "true",
           "SHOPIFY_STORES_JSON": json.dumps({"muravai": {"shop": "muravai-test.myshopify.com", "token": "shpat_x"}}),
           "INVENTORY_SHEETS_JSON": json.dumps({"muravai": "m" * 30, "claritymd": "c" * 30}),
           "INVENTORY_SERVICE_ACCOUNT_JSON": "{}",
           "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}

    def get(self, query, env=None, auth=True):
        with patch.dict("os.environ", env if env is not None else self.ENV, clear=True), \
                patch("app.daily_orders.status", return_value={"status": "running", "orders_read": 0}) as status:
            response = app.test_client().get("/api/shopify/daily-orders?" + query, headers=self.AUTH if auth else {})
        return response, status

    def test_requires_sign_in(self):
        response, status = self.get("client_id=muravai&date=2026-09-01", auth=False)
        self.assertEqual(response.status_code, 401)
        status.assert_not_called()

    def test_runs_for_that_client_only(self):
        response, status = self.get("client_id=muravai&date=2026-09-01")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(status.call_args[0][:2], ("muravai", DAY))

    def test_bad_inputs(self):
        future = (datetime.now().date().replace(year=datetime.now().year + 1)).isoformat()
        for query in ("client_id=nobody&date=2026-09-01", "client_id=muravai&date=09/01/2026",
                      "client_id=muravai&date=" + future, "client_id=muravai"):
            self.assertEqual(self.get(query)[0].status_code, 400, query)
        response, status = self.get("client_id=fascial-labs&date=2026-09-01")  # no store mapped
        self.assertEqual(response.status_code, 503)
        status.assert_not_called()

    def test_workspace_lists_clients_with_a_store(self):
        with patch.dict("os.environ", self.ENV, clear=True):
            body = app.test_client().get("/api/workspace", headers=self.AUTH).get_json()
        self.assertEqual(body["shopify_orders"], ["muravai"])
        self.assertEqual(body["daily_orders"], ["muravai"])
        no_sheet = {**self.ENV, "INVENTORY_SHEETS_JSON": json.dumps({"puravita": "p" * 30})}
        with patch.dict("os.environ", no_sheet, clear=True):
            body = app.test_client().get("/api/workspace", headers=self.AUTH).get_json()
        self.assertEqual((body["shopify_orders"], body["daily_orders"]), (["muravai"], []))

    def test_client_without_rules_never_reads_sources(self):
        stores = {c: {"shop": f"{c}-test.myshopify.com", "token": "shpat_x"} for c in ("muravai", "claritymd")}
        env = {**self.ENV, "SHOPIFY_STORES_JSON": json.dumps(stores)}
        response, status = self.get("client_id=claritymd&date=2026-09-01", env=env)
        self.assertEqual(response.status_code, 409)
        status.assert_not_called()
        with patch.dict("os.environ", env, clear=True):
            body = app.test_client().get("/api/workspace", headers=self.AUTH).get_json()
        self.assertEqual((sorted(body["shopify_orders"]), body["daily_orders"]), (["claritymd", "muravai"], ["muravai"]))

    def test_disabled_exposes_nothing(self):
        self.assertEqual(self.get("client_id=muravai&date=2026-09-01", env={})[0].status_code, 503)
        env = {k: v for k, v in self.ENV.items() if k != "INVENTORY_SHEETS_ENABLED"}
        self.assertEqual(self.get("client_id=muravai&date=2026-09-01", env=env)[0].status_code, 503)


if __name__ == "__main__":
    unittest.main()
