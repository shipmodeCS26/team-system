import base64
import json
import unittest
from unittest.mock import patch

import requests
from werkzeug.security import generate_password_hash

from app import app
import ssk_check
import ssk_source

KEYS = {"SSK_API_KEY_MURAVAI": "ssk_secret_muravai", "SSK_API_KEY_FASCIAL_LABS": "ssk_secret_fascial",
        "SSK_API_KEY_PURAVITA": "ssk_secret_puravita", "SSK_API_KEY_NUEROSMILE": "ssk_secret_neuro"}


def level(sku, title, available, committed=0, variant_id=None, aliases=(), **extra):
    return {"id": f"lvl-{sku}-{available}", "availableQuantity": available, "committedQuantity": committed,
            "incomingQuantity": extra.get("incoming", 0), "reservedQuantity": 0,
            "damagedQuantity": extra.get("damaged", 0), "safetyStockQuantity": 0, "qualityControlQuantity": 0,
            "productVariant": {"id": variant_id or f"pv-{sku}", "sku": sku, "title": title,
                               "skuAliases": list(aliases), "price": 10}}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeSsk:
    """Answers per API key; records every request so tests can prove only GETs are sent."""

    def __init__(self, by_key):
        self.by_key = by_key
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        key = headers["Authorization"].removeprefix("Bearer ")
        answer = self.by_key[key]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return FakeResponse(answer)
        page = params["page"]
        rows = answer[page - 1] if page <= len(answer) else []
        return FakeResponse(200, {"object": "list", "data": rows})


class SskTestCase(unittest.TestCase):
    def setUp(self):
        ssk_source._cache.clear()
        self.addCleanup(ssk_source._cache.clear)

    def read(self, fake, client_ids, env=None):
        with patch.dict("os.environ", env if env is not None else KEYS, clear=True), \
                patch("ssk_source.requests.get", fake.get), \
                patch("ssk_source.requests.post", side_effect=AssertionError("POST sent")), \
                patch("ssk_source.requests.request", side_effect=AssertionError("non-GET sent")):
            with self.assertLogs("ssk_source", "WARNING") as logs:
                ssk_source.log.warning("test marker")
                result = {r["id"]: r for r in ssk_source.read_stores(client_ids)}
        for text in logs.output + [json.dumps(result)]:
            for key in KEYS.values():
                self.assertNotIn(key, text)
        for call in fake.calls:
            self.assertTrue(call["url"].startswith(ssk_source.PRODUCTION) or call["url"].startswith(ssk_source.TEST))
            self.assertIn(call["url"].split("/api/v1")[1], ssk_source.READ_PATHS)
        return result


class ReadOnlyGuardTests(SskTestCase):
    def test_non_get_and_unlisted_paths_refused_before_sending(self):
        with patch("ssk_source.requests.get") as get:
            for method, path in (("POST", "/orders"), ("DELETE", "/products"), ("GET", "/inventory/move"),
                                 ("GET", "/shipping-quotes"), ("PUT", "/inventory/levels")):
                with self.assertRaises(ssk_source.ReadOnlyViolation):
                    ssk_source._get("k", path, method=method)
            get.assert_not_called()

    def test_base_url_cannot_point_elsewhere(self):
        with patch.dict("os.environ", {"SSK_API_BASE": "https://evil.example.com/api/v1"}):
            self.assertEqual(ssk_source.base_url(), ssk_source.PRODUCTION)
        with patch.dict("os.environ", {"SSK_API_BASE": ssk_source.TEST + "/"}):
            self.assertEqual(ssk_source.base_url(), ssk_source.TEST)


class SourceTests(SskTestCase):
    def test_each_store_uses_its_own_key_and_fails_alone(self):
        fake = FakeSsk({"ssk_secret_muravai": [[level("MV-SH", "Filtered Showerhead", 5)]],
                        "ssk_secret_fascial": 401, "ssk_secret_puravita": requests.Timeout("slow"),
                        "ssk_secret_neuro": 503})
        result = self.read(fake, ["muravai", "fascial-labs", "puravita", "nuerosmile", "onset"])
        self.assertEqual(result["muravai"]["levels"][0]["available"], 5)
        self.assertEqual(result["fascial-labs"]["error_code"], "access_denied")
        self.assertEqual(result["puravita"]["error_code"], "unavailable")
        self.assertEqual(result["nuerosmile"]["error_code"], "unavailable")
        self.assertEqual(result["onset"]["error_code"], "not_configured")
        # Every configured store was asked with its own key only; onset (no key) was never contacted.
        self.assertEqual({c["headers"]["Authorization"] for c in fake.calls},
                         {"Bearer " + k for k in KEYS.values()})

    def test_bundle_flag_is_read_from_the_api(self):
        row = level("SET", "Premium Shower Hose", 5)
        row["productVariant"]["product"] = {"name": "Premium Shower Hose", "isBundle": True}
        result = self.read(FakeSsk({"ssk_secret_muravai": [[row, level("H", "Shower Hose", 3)]]}), ["muravai"])
        flags = {v["sku"]: v["bundle"] for v in result["muravai"]["levels"]}
        self.assertEqual(flags, {"SET": True, "H": False})

    def test_warehouses_summed_per_variant_and_shared_skus_kept_apart(self):
        rows = [level("A1", "Thing", 3, 1, variant_id="v1"), level("A1", "Thing", 4, 0, variant_id="v1"),
                level("A1", "Other thing", 2, 0, variant_id="v2")]
        result = self.read(FakeSsk({"ssk_secret_muravai": [rows]}), ["muravai"])
        levels = sorted(result["muravai"]["levels"], key=lambda v: v["title"])
        self.assertEqual([(v["title"], v["available"], v["committed"], v["locations"]) for v in levels],
                         [("Other thing", 2, 0, 1), ("Thing", 7, 1, 2)])

    def test_pagination_and_truncation(self):
        pages = [[level(f"S{p}-{i}", "x", 1) for i in range(ssk_source.PAGE_SIZE)] for p in range(3)]
        with patch("ssk_source.MAX_PAGES", 2):
            result = self.read(FakeSsk({"ssk_secret_muravai": pages}), ["muravai"])
        self.assertEqual(len(result["muravai"]["levels"]), 2 * ssk_source.PAGE_SIZE)
        self.assertTrue(result["muravai"]["truncated"])

    def test_has_more_decides_truncation(self):
        full = [[level(f"S{p}-{i}", "x", 1) for i in range(ssk_source.PAGE_SIZE)] for p in range(2)]

        class Paged(FakeSsk):
            def __init__(self, pages, last_has_more):
                super().__init__({"ssk_secret_muravai": pages})
                self.last_has_more = last_has_more

            def get(self, url, params=None, headers=None, timeout=None):
                response = super().get(url, params, headers, timeout)
                response._body["hasMore"] = params["page"] < 2 or self.last_has_more
                return response

        with patch("ssk_source.MAX_PAGES", 2):
            exact = self.read(Paged(full, False), ["muravai"])["muravai"]
            ssk_source._cache.clear()
            more = self.read(Paged(full, True), ["muravai"])["muravai"]
        self.assertEqual(len(exact["levels"]), 2 * ssk_source.PAGE_SIZE)
        self.assertFalse(exact["truncated"])
        self.assertTrue(more["truncated"])

    def test_unexpected_body_is_read_failed_and_not_cached(self):
        answers = {"ssk_secret_muravai": 500}
        fake = FakeSsk(answers)
        self.assertEqual(self.read(fake, ["muravai"])["muravai"]["error_code"], "unavailable")
        answers["ssk_secret_muravai"] = [[level("MV-SH", "Filtered Showerhead", 9)]]
        self.assertEqual(self.read(fake, ["muravai"])["muravai"]["levels"][0]["available"], 9)


def sheet(*rows, as_of="05 Oct 2026"):
    return {"id": "x", "as_of": as_of, "rows": [{"product": p, "remaining": r} for p, r in rows]}


def levels(*items):
    out = []
    for sku, title, available, committed in items:
        out.append({"sku": sku, "title": title, "product": "", "aliases": [], "available": available,
                    "committed": committed, "incoming": 0, "reserved": 0, "damaged": 0,
                    "quality_control": 0, "safety_stock": 0, "locations": 1})
    return out


class CompareTests(unittest.TestCase):
    def by_sku(self, result):
        return {r["sku"]: r for r in result["skus"]}

    def test_alias_client_matches_by_code_and_reports_both_differences(self):
        result = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 100, 10)),
                                   sheet(("TrueForm Fascial Release Support", "110")))
        row = self.by_sku(result)["FAS001"]
        self.assertEqual((row["sheet_remaining"], row["vs_available"], row["vs_available_committed"]), (110, -10, 0))
        # Only one of the two undecided bases agrees, so it is not reconciled.
        self.assertEqual(row["status"], "REVIEW")
        self.assertIn("Matches on one basis only (available vs on hand is undecided)", row["notes"])
        both = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 110, 0)),
                                 sheet(("TrueForm Fascial Release Support", "110")))
        self.assertEqual(self.by_sku(both)["FAS001"]["status"], "MATCH")

    def test_different_and_unmatched(self):
        result = ssk_check.compare("puravita", levels(("CAP-MAGNESIUM-360", "Magnesium", 50, 0),
                                                       ("GIFT", "Gift card", 0, 0)),
                                   sheet(("Magnesium Performance Capsules", "1,000"), ("Mystery row", "3")))
        row = self.by_sku(result)["PVT001"]
        self.assertEqual((row["vs_available"], row["status"]), (-950, "DIFFERENT"))
        self.assertEqual([v["sku"] for v in result["unmatched_ssk"]], ["GIFT"])
        self.assertEqual(result["unmatched_sheet"], ["Mystery row"])

    def test_muravai_name_matching_and_kit_component(self):
        result = ssk_check.compare("muravai", levels(("MV-F3", "Replacement Filters 3-Pack", 8491, 0),
                                                      ("MV-TEF", "Teflon Tape", 40, 0)),
                                   sheet(("Replacement Filters, 3-Pack", "8,491")))
        rows = self.by_sku(result)
        self.assertEqual(rows["MUR001"]["status"], "MATCH")
        self.assertEqual([c["sku"] for c in result["components"]], ["MV-TEF"])
        self.assertIn("Not found in ShipSidekick", rows["MUR002"]["notes"])
        self.assertIn("No Sheet row matches this SKU", rows["MUR002"]["notes"])

    def test_muravai_real_catalog_kits_are_never_counted_as_single_items(self):
        # ShipSidekick Muravai inventory as seen on 2026-10-06 (Doral warehouse).
        result = ssk_check.compare("muravai", levels(
            ("3 filters", "Replacement Filters (3 Pack)", 7843, 300),
            ("showerhead", "THE FILTERED SHOWERHEAD\u2122", 3543, 121),
            ("shower connector", "Shower connector", 1003, 78),
            ("shower hose", "Shower Hose", 993, 77),
            ("Teflon Tape-360-USA", "1x Teflon Tape", 975, 77),
            ("1x hose and connector", "SHOWER ENHANCEMENT KIT", 2586, 0),
            ("hose and connector set", "Shower Hose & Connector Set", 976, 0),
            ("showerhead + hose and connector", "Complete Shower Set", 976, 0)),
            sheet(("Replacement Filters, 3-Pack", "384"), ("Filtered Showerhead", "1,745"),
                  ("Connector Kit Box", "1,291"), ("Shower Hose*", "0"), ("Bracket / Connector*", "561")))
        rows = self.by_sku(result)
        self.assertEqual(rows["MUR001"]["ssk_skus"], ["3 filters"])
        self.assertEqual(rows["MUR002"]["ssk_skus"], ["showerhead"])
        self.assertEqual(rows["MUR001"]["vs_available_committed"], 8143 - 384)
        not_compared = {c["sku"] for c in result["components"]} | {v["sku"] for v in result["unmatched_ssk"]}
        self.assertEqual(not_compared, {"Teflon Tape-360-USA", "1x hose and connector",
                                        "hose and connector set", "showerhead + hose and connector"})
        # Kit parts split per RULES.md: kits = tape, standalone hose/connector = parts − tape.
        self.assertEqual(rows["MUR003"]["ssk_skus"], ["Teflon Tape-360-USA"])
        self.assertEqual(rows["MUR003"]["ssk"]["available"], 975)
        self.assertEqual(rows["MUR004"]["ssk_skus"], ["shower hose", "Teflon Tape-360-USA"])
        self.assertEqual((rows["MUR004"]["ssk"]["available"], rows["MUR004"]["ssk"]["committed"]), (18, 0))
        self.assertEqual(rows["MUR005"]["ssk"]["available"], 1003 - 975)
        self.assertIn("RULES.md", rows["MUR004"]["basis"])
        self.assertEqual(rows["MUR003"]["sheet_remaining"], 1291)
        self.assertEqual(rows["MUR003"]["vs_available_committed"], 975 + 77 - 1291)
        self.assertEqual((rows["MUR004"]["sheet_remaining"], rows["MUR005"]["sheet_remaining"]), (0, 561))
        self.assertEqual(result["unmatched_sheet"], [])

    def test_bundle_flag_wins_over_name(self):
        items = levels(("PSH", "Premium Shower Hose", 50, 0), ("H", "Shower Hose", 20, 0))
        items[0]["bundle"] = True
        result = ssk_check.compare("muravai", items, sheet(("Shower Hose", "20")))
        self.assertEqual(self.by_sku(result)["MUR004"]["ssk_skus"], ["H"])
        self.assertIn("isBundle", {c["sku"]: c["note"] for c in result["components"]}["PSH"])

    def test_filter_packs_other_than_three_are_not_mur001(self):
        result = ssk_check.compare("muravai", levels(("F6", "Replacement Filters 6 Pack", 10, 0),
                                                      ("F3", "Replacement Filters (3 Pack)", 10, 0)),
                                   sheet(("Replacement Filters, 3-Pack", "10")))
        row = self.by_sku(result)["MUR001"]
        self.assertEqual((row["ssk_skus"], row["status"]), (["F3"], "MATCH"))
        self.assertIn("3-pack", {c["sku"]: c["note"] for c in result["components"]}["F6"])

    def test_kit_split_needs_exactly_one_of_each_part(self):
        result = ssk_check.compare("muravai", levels(("T1", "Teflon Tape", 10, 0), ("T2", "Teflon Tape 2", 5, 0),
                                                      ("H", "Shower Hose", 20, 0), ("C", "Shower Connector", 20, 0)),
                                   sheet(("Shower Hose", "10")))
        rows = self.by_sku(result)
        for sku in ("MUR003", "MUR004", "MUR005"):
            self.assertIsNone(rows[sku]["ssk"])
            self.assertEqual(rows[sku]["status"], "REVIEW")
            self.assertTrue(any("could not be split" in n for n in rows[sku]["notes"]))

    def test_negative_split_is_kept_and_flagged(self):
        result = ssk_check.compare("muravai", levels(("T", "Teflon Tape", 30, 0), ("H", "Shower Hose", 20, 0),
                                                      ("C", "Shower Connector", 40, 0)), sheet(("Shower Hose", "0")))
        row = self.by_sku(result)["MUR004"]
        self.assertEqual(row["ssk"]["available"], -10)
        self.assertEqual(row["status"], "REVIEW")

    def test_negative_incoming_after_split_is_reviewed(self):
        items = levels(("T", "Teflon Tape", 10, 0), ("H", "Shower Hose", 20, 0), ("C", "Shower Connector", 20, 0))
        items[0]["incoming"] = 5
        result = ssk_check.compare("muravai", items, sheet(("Shower Hose", "10")))
        row = self.by_sku(result)["MUR004"]
        self.assertEqual((row["ssk"]["available"], row["ssk"]["incoming"]), (10, -5))
        self.assertEqual(row["status"], "REVIEW")
        self.assertTrue(any("incoming" in n for n in row["notes"]))

    def test_truncated_inventory_is_never_reconciled(self):
        items = levels(("H", "Shower Hose", 20, 0))
        full = ssk_check.compare("muravai", items, sheet(("Shower Hose", "20")))
        self.assertEqual({r["sku"]: r for r in full["skus"]}["MUR004"]["status"], "MATCH")
        cut = ssk_check.compare("muravai", items, sheet(("Shower Hose", "20")), truncated=True)
        for row in cut["skus"]:
            self.assertEqual(row["status"], "REVIEW")
            self.assertIn("ShipSidekick inventory list was cut off; not reconciled", row["notes"])

    def test_sheet_that_may_continue_is_never_reconciled(self):
        partial = dict(sheet(("Shower Hose", "20")), may_continue=True)
        result = ssk_check.compare("muravai", levels(("H", "Shower Hose", 20, 0)), partial)
        row = {r["sku"]: r for r in result["skus"]}["MUR004"]
        self.assertEqual(row["status"], "REVIEW")
        self.assertIn("Sheet may list more products past its last read row; not reconciled", row["notes"])

    def test_negative_unsplit_quantity_is_reviewed(self):
        items = levels(("FASCSUPP-1", "Support", 110, 0))
        items[0]["damaged"] = -3
        result = ssk_check.compare("fascial-labs", items, sheet(("TrueForm Fascial Release Support", "110")))
        row = self.by_sku(result)["FAS001"]
        self.assertEqual(row["status"], "REVIEW")
        self.assertIn("Negative ShipSidekick quantity (damaged)", row["notes"])

    def test_alias_with_pack_size_is_used_after_inconclusive_sku(self):
        items = levels(("FILTERS", "Replacement Filters", 10, 0))
        items[0]["aliases"] = ["3 filters"]
        result = ssk_check.compare("muravai", items, sheet(("Replacement Filters, 3-Pack", "10")))
        self.assertEqual(self.by_sku(result)["MUR001"]["ssk_skus"], ["FILTERS"])
        no_alias = ssk_check.compare("muravai", levels(("FILTERS", "Replacement Filters", 10, 0)), None)
        self.assertIn("3-pack", {c["sku"]: c["note"] for c in no_alias["components"]}["FILTERS"])

    def test_compound_connector_kit_is_not_mur003(self):
        result = ssk_check.compare("muravai", levels(("CK", "Connector Kit Box", 5, 0),
                                                      ("CKS", "Connector Kit + Showerhead", 9, 0)), None)
        self.assertEqual(self.by_sku(result)["MUR003"]["ssk_skus"], ["CK"])
        self.assertIn("CKS", {c["sku"] for c in result["components"]})

    def test_sheet_error_as_of_is_not_reconciled(self):
        for bad in ("#REF!", "not a date"):
            row = next(r for r in ssk_check.compare("muravai", levels(("H-1", "Shower Hose", 20, 0)),
                                                    sheet(("Shower Hose", "20"), as_of=bad))["skus"] if r["sku"] == "MUR004")
            self.assertNotEqual(row["status"], "MATCH")

    def test_punctuated_bundle_names_are_not_mapped(self):
        import muravai_rules
        for name in ("Shower Hose Kit: Chrome", "Bundle: Shower Hose", "Connector Kit Bundle/Set"):
            self.assertIsNone(muravai_rules.shopify_match({"product": name, "variant": "", "sku": ""})["sku"], name)

    def test_sheet_without_as_of_is_not_reconciled(self):
        undated = sheet(("Shower Hose", "20"), as_of="")
        row = self.by_sku(ssk_check.compare("muravai", levels(("H", "Shower Hose", 20, 0)), undated))["MUR004"]
        self.assertEqual(row["status"], "REVIEW")
        self.assertIn("Sheet has no usable as-of date; not reconciled", row["notes"])

    def test_duplicate_skus_listed_once(self):
        items = levels(("A1", "Thing", 1, 0), ("a1", "Other", 1, 0), ("B", "Third", 1, 0))
        self.assertEqual(ssk_check.compare("onset", items, None)["duplicate_skus"], ["A1", "a1"])

    def test_no_tape_means_no_split(self):
        result = ssk_check.compare("muravai", levels(("H", "Shower Hose", 20, 0)), sheet(("Shower Hose", "20")))
        row = self.by_sku(result)["MUR004"]
        self.assertEqual((row["ssk"]["available"], row["basis"], row["status"]), (20, "", "MATCH"))

    def test_fractional_sheet_value_is_not_truncated(self):
        result = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 12, 0)),
                                   sheet(("TrueForm Fascial Release Support", "12.5")))
        row = self.by_sku(result)["FAS001"]
        self.assertIsNone(row["sheet_remaining"])
        self.assertEqual(row["status"], "REVIEW")
        self.assertIn("Sheet value is not a whole number: 12.5", row["notes"])
        whole = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 12, 0)),
                                  sheet(("TrueForm Fascial Release Support", "12.0")))
        self.assertEqual(self.by_sku(whole)["FAS001"]["status"], "MATCH")

    def test_several_ssk_variants_for_one_sku_are_not_added(self):
        result = ssk_check.compare("muravai", levels(("MV-SH1", "Filtered Showerhead", 5, 0),
                                                      ("MV-SH2", "Filtered Showerhead Chrome", 7, 0)),
                                   sheet(("Filtered Showerhead", "12")))
        row = self.by_sku(result)["MUR002"]
        self.assertIsNone(row["ssk"])
        self.assertIsNone(row["vs_available"])
        self.assertEqual(row["status"], "REVIEW")

    def test_sheet_missing_or_non_numeric(self):
        missing = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 1, 0)), None)
        self.assertIn("Sheet not loaded", self.by_sku(missing)["FAS001"]["notes"])
        self.assertTrue(missing["sheet_error"])
        pending = ssk_check.compare("fascial-labs", levels(("FASCSUPP-1", "Support", 1, 0)),
                                    sheet(("TrueForm Fascial Release Support", "PENDING")))
        self.assertEqual(self.by_sku(pending)["FAS001"]["status"], "REVIEW")

    def test_store_without_rules_maps_nothing(self):
        result = ssk_check.compare("onset", levels(("ON-1", "Gel", 3, 0)), None)
        self.assertEqual(result["skus"], [])
        self.assertEqual(len(result["unmatched_ssk"]), 1)

    def test_rules_never_cross_clients(self):
        result = ssk_check.compare("fascial-labs", levels(("CAP-MAGNESIUM-360", "Magnesium", 5, 0)), None)
        self.assertEqual([v["sku"] for v in result["unmatched_ssk"]], ["CAP-MAGNESIUM-360"])


class SskApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}
        self.env = {"SSK_API_ENABLED": "true", "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
                    "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256")}

    def test_disabled_does_not_expose_data(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.client.get("/api/ssk/inventory").status_code, 503)

    def test_enabled_without_sign_in_settings_fails_closed(self):
        with patch.dict("os.environ", {"SSK_API_ENABLED": "true"}, clear=True):
            self.assertEqual(self.client.get("/").status_code, 503)

    def test_requires_sign_in_and_is_client_scoped(self):
        store = {"id": "muravai", "levels": levels(("MV-SH", "Filtered Showerhead", 5, 0)), "truncated": False,
                 "environment": "production", "fetched_at": "2026-10-06T00:00:00+00:00"}
        with patch.dict("os.environ", self.env, clear=True):
            self.assertEqual(self.client.get("/api/ssk/inventory").status_code, 401)
            self.assertEqual(self.client.get("/api/workspace").status_code, 401)
            with patch("app.ssk_source.read_stores", return_value=[store]) as reader:
                body = self.client.get("/api/ssk/inventory?client_id=muravai", headers=self.auth).get_json()
            reader.assert_called_once_with(["muravai"])
            self.assertEqual(body["writes"], "disabled")
            self.assertEqual([c["client_id"] for c in body["clients"]], ["muravai"])
            self.assertIn("Sheet not loaded", {r["sku"]: r for r in body["clients"][0]["skus"]}["MUR002"]["notes"])
            self.assertEqual(self.client.get("/api/ssk/inventory?client_id=nope", headers=self.auth).status_code, 400)

    def test_sheet_and_ssk_are_read_together(self):
        import threading
        both = threading.Barrier(2, timeout=5)

        def slow_sheets(ids):
            both.wait()   # only returns if ShipSidekick is being read at the same time
            return [{"id": "muravai", "as_of": "05 Oct 2026", "rows": [{"product": "Shower Hose", "remaining": "20"}]}]

        def stores(ids):
            both.wait()
            return [{"id": "muravai", "levels": levels(("H", "Shower Hose", 20, 0)), "truncated": False,
                     "environment": "production", "fetched_at": "x"}]
        env = dict(self.env, INVENTORY_SHEETS_ENABLED="true", INVENTORY_SHEETS_JSON="{}",
                   INVENTORY_SERVICE_ACCOUNT_JSON="{}")
        with patch.dict("os.environ", env, clear=True), patch("app.read_dashboards", slow_sheets), \
                patch("app.ssk_source.read_stores", stores):
            body = self.client.get("/api/ssk/inventory?client_id=muravai", headers=self.auth).get_json()
        self.assertEqual({r["sku"]: r for r in body["clients"][0]["skus"]}["MUR004"]["status"], "MATCH")

    def test_failed_store_returns_error_only(self):
        with patch.dict("os.environ", self.env, clear=True), \
                patch("app.ssk_source.read_stores",
                      return_value=[{"id": "muravai", "error_code": "access_denied", "error": "x"}]):
            body = self.client.get("/api/ssk/inventory?client_id=muravai", headers=self.auth).get_json()
        self.assertEqual(body["clients"], [{"client_id": "muravai", "error_code": "access_denied", "error": "x"}])


if __name__ == "__main__":
    unittest.main()


class ShipmentFieldsTests(unittest.TestCase):
    SHIPMENT = {"id": "8f1d6c9a-0000-4000-8000-000000000001", "trackingCode": "1Z999AA10123456784",
                "trackingStatus": "in_transit", "createdAt": "2026-10-01T15:00:00Z",
                "shipToAddress": {"name": "Jane Doe", "street1": "1 Main St", "city": "Miami", "state": "FL"},
                "carrierAccount": {"carrierCode": "ups", "description": "ShipMode UPS account"},
                "trackingEvents": [{"status": "in_transit", "occurredAt": "2026-10-02T08:00:00Z",
                                    "description": "Arrived at facility in Miami, FL"}],
                "order": {"id": "o1", "orderNumber": "#1001", "customerName": "Jane Doe"}}

    def test_reports_names_and_status_words_but_no_private_values(self):
        fake = FakeSsk({"k": [[self.SHIPMENT]]})
        with patch("ssk_source.requests.get", fake.get), \
                patch("ssk_source.requests.post", side_effect=AssertionError("POST sent")):
            result = ssk_source.shipment_fields("k")
        self.assertEqual(fake.calls[0]["url"], ssk_source.PRODUCTION + "/shipments")
        self.assertEqual(result["sampled"], 1)
        self.assertEqual(result["fields"]["createdAt"], ["date"])
        self.assertEqual(result["fields"]["trackingEvents[].occurredAt"], ["date"])
        self.assertIn("shipToAddress", result["fields"])
        self.assertNotIn("shipToAddress.city", result["fields"])
        self.assertEqual(result["values"]["trackingStatus"], ["in_transit"])
        self.assertEqual(result["values"]["carrierAccount.carrierCode"], ["ups"])
        text = json.dumps(result)
        for private in ("Jane", "Main St", "Miami", "1Z999", "8f1d6c9a", "#1001", "ShipMode UPS", "Arrived"):
            self.assertNotIn(private, text)

    def test_endpoint_requires_sign_in_one_client_and_hides_key(self):
        client = app.test_client()
        auth = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}
        env = {"SSK_API_ENABLED": "true", "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
               "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256"), **KEYS}
        fake = FakeSsk({"ssk_secret_muravai": [[self.SHIPMENT]]})
        with patch.dict("os.environ", env, clear=True), patch("ssk_source.requests.get", fake.get):
            self.assertEqual(client.get("/api/ssk/shipment-fields?client_id=muravai").status_code, 401)
            self.assertEqual(client.get("/api/ssk/shipment-fields?client_id=all", headers=auth).status_code, 400)
            with self.assertLogs(app.logger, "WARNING") as logs:
                response = client.get("/api/ssk/shipment-fields?client_id=muravai", headers=auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["writes"], "disabled")
        self.assertEqual(len(fake.calls), 1)
        for text in logs.output + [response.get_data(as_text=True)]:
            self.assertNotIn("ssk_secret", text)
            self.assertNotIn("Jane", text)


def shipment(sid, status="in-transit", details=(), label="2026-09-20T10:00:00Z", **extra):
    return {"id": sid, "trackingCode": f"TRK{sid}", "createdAt": label, "updatedAt": "2026-10-06T01:00:00Z",
            "order": {"id": "o" + sid, "name": "#" + sid},
            "returnAddress": {"name": "ShipMode Warehouse", "street1": "9 Dock Rd"},
            "shippingRate": {"shipToAddress": {"name": "Jane Customer", "street1": "1 Main St", "city": "Miami"}},
            "carrierAccount": {"carrierCode": "cirro-ecommerce"},
            "packages": [{"shippingLabel": {"createdAt": label, "pdfUrl": "https://labels.example/x.pdf"},
                          "lineItems": [{"price": 30, "productVariant": {"sku": "MUR002"}}]}],
            "tracker": {"status": status, "carrierCode": "cirro-ecommerce",
                        "trackingUrl": "https://track.example/" + sid,
                        "trackingDetails": [{"status": s, "createdAt": at, "message": m, "source": "carrier",
                                             "trackingLocation": {"city": "Doral"}} for s, at, m in details]},
            "voidStatus": None, **extra}


class ShipmentMappingTests(unittest.TestCase):
    NOW = ssk_source.datetime(2026, 10, 6, 12, tzinfo=ssk_source.timezone.utc)

    def classify(self, raw):
        from tracking import classify
        import ssk_shipments
        return classify(dict(ssk_shipments.to_row(raw, "muravai", self.NOW), id=1), self.NOW)

    def test_stalled_after_physical_scan_uses_scan_time(self):
        row = self.classify(shipment("1", "in-transit", [
            ("pre-transit", "2026-09-20T10:00:00Z", "Shipping label created"),
            ("in-transit", "2026-09-25T10:00:00Z", "Arrived at carrier facility")]))
        self.assertEqual(row["carrier_status"], "in_transit")
        self.assertEqual(row["last_movement_at"], "2026-09-25T10:00:00+00:00")
        self.assertEqual((row["tier"], row["days"]), ("critical", 11))
        self.assertEqual(row["anchor_source"], "Last carrier movement")

    def test_label_only_falls_back_to_label_date(self):
        row = self.classify(shipment("2", "pre-transit", [("pre-transit", "2026-09-30T10:00:00Z", "Label created")],
                                     label="2026-09-30T09:00:00Z"))
        self.assertEqual(row["carrier_status"], "pre_transit")
        self.assertIsNone(row["last_movement_at"])
        self.assertTrue(row["never_scanned"])
        self.assertEqual((row["tier"], row["anchor_source"]), ("watch", "Label created"))

    def test_electronic_in_transit_event_is_not_movement(self):
        row = self.classify(shipment("3", "in-transit", [("in-transit", "2026-10-05T10:00:00Z",
                                                           "Shipment information sent to carrier")]))
        self.assertIsNone(row["last_movement_at"])
        self.assertEqual(row["tier"], "data_gap")

    def test_unknown_status_and_void_labels(self):
        self.assertEqual(self.classify(shipment("4", "weird"))["carrier_status"], "unknown")
        self.assertEqual(self.classify(shipment("5", voidStatus="voided"))["tier"], "cancelled")

    def test_no_addresses_items_or_label_files_in_row(self):
        import ssk_shipments
        text = json.dumps(ssk_shipments.to_row(shipment("6", "in-transit", [
            ("in-transit", "2026-10-01T10:00:00Z", "Arrived at carrier facility")]), "muravai", self.NOW))
        for private in ("Jane", "Main St", "Dock Rd", "Warehouse", "Doral", "labels.example", "MUR002", "Miami"):
            self.assertNotIn(private, text)
        self.assertIn("https://track.example/6", text)


class ShipmentReaderTests(unittest.TestCase):
    def setUp(self):
        ssk_source._shipment_cache.clear()
        self.addCleanup(ssk_source._shipment_cache.clear)

    def fake(self, by_status, reject=()):
        calls = []

        def get(url, params=None, headers=None, timeout=None):
            calls.append(params)
            state = params["trackingStatus"]
            if state in reject:
                return FakeResponse(400)
            return FakeResponse(200, {"data": by_status.get(state, []) if params["page"] == 1 else [], "hasMore": False})
        return get, calls

    def test_reads_only_open_statuses_get_only(self):
        get, calls = self.fake({"in-transit": [shipment("1", "in-transit")], "pre-transit": [shipment("2", "pre-transit")]},
                               reject=("error",))
        with patch.dict("os.environ", KEYS, clear=True), patch("ssk_source.requests.get", get), \
                patch("ssk_source.requests.post", side_effect=AssertionError("POST sent")):
            result = ssk_source.read_shipment_stores(["muravai", "onset"], 30)
        self.assertEqual(sorted(r["tracking_number"] for r in result[0]["rows"]), ["TRK1", "TRK2"])
        self.assertEqual(result[1]["error_code"], "not_configured")
        self.assertNotIn("delivered", {c["trackingStatus"] for c in calls})
        self.assertEqual(result[0]["skipped_statuses"], ["error"])
        self.assertEqual(result[0]["environment"], "production")
        self.assertTrue(all(c["dateRange[from]"] for c in calls))

    def test_ignored_filter_refuses_partial_queue(self):
        get, _ = self.fake({"in-transit": [shipment("1", "delivered")]})
        with patch.dict("os.environ", KEYS, clear=True), patch("ssk_source.requests.get", get):
            result = ssk_source.read_shipment_stores(["muravai"], 30)
        self.assertEqual(result[0]["error_code"], "filter_ignored")

    def test_workspace_uses_ssk_when_enabled(self):
        client = app.test_client()
        auth = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}
        env = {"SSK_API_ENABLED": "true", "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
               "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256"), **KEYS}
        get, _ = self.fake({"in-transit": [shipment("1", "in-transit")]})
        with patch.dict("os.environ", env, clear=True), patch("ssk_source.requests.get", get):
            self.assertEqual(client.get("/api/workspace").status_code, 401)
            body = client.get("/api/workspace", headers=auth).get_json()
        self.assertEqual(body["mode"], "ssk")
        sources = {src["client_id"]: src for src in body["sources"]}
        self.assertEqual(sources["onset"]["error_code"], "not_configured")
        self.assertEqual((sources["muravai"]["skipped_statuses"], sources["muravai"]["environment"]), ([], "production"))
        self.assertEqual(body["writes"], "disabled")
        muravai = [r for r in body["shipments"] if r["client_id"] == "muravai"]
        self.assertEqual([r["tracking_number"] for r in muravai], ["TRK1"])
        self.assertNotIn("Jane", json.dumps(body))
        self.assertNotIn("DEMO-", json.dumps(body))
