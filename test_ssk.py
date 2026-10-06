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
        self.assertEqual(row["status"], "MATCH")

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

    def test_failed_store_returns_error_only(self):
        with patch.dict("os.environ", self.env, clear=True), \
                patch("app.ssk_source.read_stores",
                      return_value=[{"id": "muravai", "error_code": "access_denied", "error": "x"}]):
            body = self.client.get("/api/ssk/inventory?client_id=muravai", headers=self.auth).get_json()
        self.assertEqual(body["clients"], [{"client_id": "muravai", "error_code": "access_denied", "error": "x"}])


if __name__ == "__main__":
    unittest.main()
