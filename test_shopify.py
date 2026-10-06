import base64
import json
import unittest
from unittest.mock import patch

import requests
from werkzeug.security import generate_password_hash

from app import app
import shopify_source
import sku_check

SHOPS = {"claritymd": "clarity-test.myshopify.com", "fascial-labs": "fascial-test.myshopify.com",
         "muravai": "muravai-test.myshopify.com", "puravita": "puravita-test.myshopify.com"}
TOKENS = {client: f"shpat_secret_{client}" for client in SHOPS}
READ_SCOPES = ["read_customers", "read_inventory", "read_orders", "read_products"]


def variant(product, sku, title="Default Title", status="ACTIVE", tracked=True):
    return {"product": product, "variant": title, "sku": sku, "product_status": status, "tracked": tracked}


def node(product, sku, title="Default Title", status="ACTIVE", tracked=True):
    return {"title": title, "sku": sku, "product": {"title": product, "status": status},
            "inventoryItem": {"tracked": tracked}}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def scopes_body(scopes):
    return {"data": {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in scopes]}}}


def variants_body(nodes, next_cursor=None):
    return {"data": {"productVariants": {"nodes": nodes, "pageInfo": {
        "hasNextPage": next_cursor is not None, "endCursor": next_cursor}}}}


class FakeShopify:
    """Records every outbound request so tests can prove only reads are sent."""

    def __init__(self, stores):
        self.stores = stores  # shop domain -> {"scopes": [...], "pages": [...]} or a status / exception
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "data": data})
        shop = url.split("//")[1].split("/")[0]
        store = self.stores[shop]
        if isinstance(store, Exception):
            raise store
        if isinstance(store, int):
            return FakeResponse(store)
        if url.endswith("/admin/oauth/access_token"):
            return FakeResponse(200, {"access_token": "short-lived", "scope": ",".join(store["scopes"]), "expires_in": 86399})
        if json["query"] == shopify_source.SCOPES_QUERY:
            return FakeResponse(200, scopes_body(store["scopes"]))
        if "errors" in store:
            return FakeResponse(200, {"errors": store["errors"]})
        after = (json["variables"] or {}).get("after")
        page = 0 if after is None else int(after)
        nodes = store["pages"][page]
        return FakeResponse(200, variants_body(nodes, str(page + 1) if page + 1 < len(store["pages"]) else None))


class ShopifyTestCase(unittest.TestCase):
    def setUp(self):
        shopify_source._cache.clear()
        shopify_source._tokens.clear()
        self.addCleanup(shopify_source._cache.clear)
        self.addCleanup(shopify_source._tokens.clear)

    def env(self, stores):
        return patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(stores)})

    def read(self, fake, client_ids, stores=None):
        stores = stores or {c: {"shop": SHOPS[c], "token": TOKENS[c]} for c in SHOPS}
        with self.env(stores), patch("shopify_source.requests.post", fake.post):
            with self.assertLogs("shopify_source", "WARNING") as logs:
                shopify_source.log.warning("test marker")
                result = {r["id"]: r for r in shopify_source.read_catalogs(client_ids)}
        self.assert_no_secrets(logs.output, stores)
        self.assert_no_secrets([json.dumps(result)], stores)
        self.assert_only_reads(fake)
        return result

    def assert_no_secrets(self, texts, stores):
        for text in texts:
            for store in stores.values():
                for value in store.values():
                    self.assertNotIn(value, text)

    def assert_only_reads(self, fake):
        for call in fake.calls:
            if call["url"].endswith("/admin/oauth/access_token"):
                self.assertEqual(call["data"]["grant_type"], "client_credentials")
                continue
            self.assertIn(call["json"]["query"], shopify_source.READ_QUERIES)
            self.assertNotRegex(call["json"]["query"], r"(?i)\bmutation\b")


class ReadOnlyGuardTests(ShopifyTestCase):
    STORE = {"shop": "muravai-test.myshopify.com", "token": "shpat_x"}

    def test_mutation_is_refused_before_any_request(self):
        with patch("shopify_source.requests.post") as post:
            for document in ("mutation { orderCancel(orderId: \"gid://shopify/Order/1\") { job { id } } }",
                             shopify_source.VARIANTS_QUERY + " mutation",
                             "query { shop { name } }",
                             "subscription { x }"):
                with self.assertRaises(shopify_source.ReadOnlyViolation):
                    shopify_source._graphql(self.STORE, document)
            post.assert_not_called()

    def test_allowlist_contains_only_queries(self):
        for document in shopify_source.READ_QUERIES:
            self.assertTrue(document.lstrip().startswith("query "))
            self.assertNotRegex(document, r"(?i)\b(mutation|subscription)\b")

    def test_token_with_any_write_scope_is_refused(self):
        with self.assertRaises(shopify_source.SourceError) as error:
            shopify_source.check_scopes(READ_SCOPES + ["write_orders"])
        self.assertEqual(error.exception.code, "write_scope_granted")

    def test_missing_required_scope_fails_and_planned_scopes_warn(self):
        with self.assertRaises(shopify_source.SourceError) as error:
            shopify_source.check_scopes(["read_orders"])
        self.assertEqual(error.exception.code, "missing_scope")
        self.assertEqual(shopify_source.check_scopes(["read_products"]),
                         ["read_inventory", "read_orders", "read_customers"])
        self.assertEqual(shopify_source.check_scopes(READ_SCOPES), [])

    def test_write_scope_store_returns_no_data(self):
        fake = FakeShopify({SHOPS["muravai"]: {"scopes": READ_SCOPES + ["write_products"], "pages": [[node("Showerhead", "X")]]}})
        result = self.read(fake, ["muravai"])
        self.assertEqual(result["muravai"]["error_code"], "write_scope_granted")
        self.assertNotIn("variants", result["muravai"])
        # Scope check happens before the catalog is requested.
        self.assertEqual([c["json"]["query"] for c in fake.calls], [shopify_source.SCOPES_QUERY])


class SourceTests(ShopifyTestCase):
    def test_failures_are_per_client_and_categorized(self):
        fake = FakeShopify({
            SHOPS["claritymd"]: {"scopes": READ_SCOPES, "pages": [[node("Clarity Serum", "CLR-1")]]},
            SHOPS["fascial-labs"]: 401,
            SHOPS["muravai"]: 404,
            SHOPS["puravita"]: requests.Timeout("slow"),
        })
        stores = {c: {"shop": SHOPS[c], "token": TOKENS[c]} for c in SHOPS}
        stores["onset"] = {"shop": "https://evil.example.com", "token": "shpat_onset_secret"}
        result = self.read(fake, ["claritymd", "fascial-labs", "muravai", "puravita", "nuerosmile", "onset"], stores)
        self.assertEqual(result["claritymd"]["variants"][0]["sku"], "CLR-1")
        self.assertEqual(result["fascial-labs"]["error_code"], "access_denied")
        self.assertEqual(result["muravai"]["error_code"], "not_found")
        self.assertEqual(result["puravita"]["error_code"], "unavailable")
        self.assertEqual(result["nuerosmile"]["error_code"], "not_configured")
        self.assertEqual(result["onset"]["error_code"], "not_configured")
        # A store is only ever asked about itself.
        self.assertFalse(any("evil.example.com" in c["url"] for c in fake.calls))

    def test_graphql_access_denied_is_missing_scope(self):
        fake = FakeShopify({SHOPS["muravai"]: {"scopes": READ_SCOPES, "errors": [{"extensions": {"code": "ACCESS_DENIED"}}]}})
        self.assertEqual(self.read(fake, ["muravai"])["muravai"]["error_code"], "missing_scope")

    def test_pagination_and_truncation_warning(self):
        pages = [[node(f"P{i}", f"S{i}")] for i in range(3)]
        fake = FakeShopify({SHOPS["muravai"]: {"scopes": READ_SCOPES, "pages": pages}})
        with self.env({"muravai": {"shop": SHOPS["muravai"], "token": "t"}}), \
                patch("shopify_source.requests.post", fake.post), patch("shopify_source.MAX_PAGES", 2):
            result = shopify_source.read_catalogs(["muravai"])[0]
        self.assertEqual([v["sku"] for v in result["variants"]], ["S0", "S1"])
        self.assertTrue(result["truncated"])

    def test_failed_read_is_not_cached_and_success_is(self):
        stores = {SHOPS["muravai"]: 503}
        fake = FakeShopify(stores)
        self.assertEqual(self.read(fake, ["muravai"])["muravai"]["error_code"], "unavailable")
        stores[SHOPS["muravai"]] = {"scopes": READ_SCOPES, "pages": [[node("Showerhead", "MUR-SH")]]}
        self.assertEqual(self.read(fake, ["muravai"])["muravai"]["variants"][0]["sku"], "MUR-SH")
        before = len(fake.calls)
        self.read(fake, ["muravai"])
        self.assertEqual(len(fake.calls), before)

    def test_client_credentials_token_is_fetched_once_and_used(self):
        fake = FakeShopify({SHOPS["muravai"]: {"scopes": READ_SCOPES, "pages": [[node("Showerhead", "MUR-SH")]]}})
        stores = {"muravai": {"shop": SHOPS["muravai"], "client_id": "app-client-id", "client_secret": "app-secret-value"}}
        result = self.read(fake, ["muravai"], stores)
        self.assertEqual(len(result["muravai"]["variants"]), 1)
        token_calls = [c for c in fake.calls if c["url"].endswith("/admin/oauth/access_token")]
        self.assertEqual(len(token_calls), 1)

    def test_rejected_client_credentials_are_access_denied(self):
        fake = FakeShopify({SHOPS["muravai"]: 400})
        stores = {"muravai": {"shop": SHOPS["muravai"], "client_id": "app-client-id", "client_secret": "app-secret-value"}}
        self.assertEqual(self.read(fake, ["muravai"], stores)["muravai"]["error_code"], "access_denied")


class SkuCheckTests(unittest.TestCase):
    def by_product(self, result):
        return {(r["product"], r["variant"]): r for r in result["variants"]}

    def test_alias_client_maps_by_shipsidekick_code(self):
        result = sku_check.check("fascial-labs", [
            variant("TrueForm Fascial Release Support", "fascsupp-1"),
            variant("Gift card", "GIFT"),
            variant("Sample", ""),
        ])
        rows = self.by_product(result)
        mapped = rows[("TrueForm Fascial Release Support", "Default Title")]
        self.assertEqual((mapped["status"], mapped["internal_sku"]), ("mapped", "FAS001"))
        self.assertEqual(mapped["flags"], [])
        self.assertEqual(rows[("Gift card", "Default Title")]["status"], "unmapped")
        self.assertIn("needs approval", rows[("Gift card", "Default Title")]["how"])
        self.assertEqual(rows[("Sample", "Default Title")]["flags"], ["blank_sku"])
        self.assertEqual(result["rules"], [{"internal_sku": "FAS001", "label": "TrueForm Fascial Release Support", "state": "found"}])
        self.assertEqual(result["rule_status"], "PROPOSED")
        # Unmapped rows sort first so they are reviewed first.
        self.assertEqual(result["variants"][-1]["status"], "mapped")

    def test_duplicate_and_inactive_and_untracked_flags(self):
        result = sku_check.check("puravita", [
            variant("Magnesium", "CAP-MAGNESIUM-360", "60 count"),
            variant("Magnesium old", "cap-magnesium-360", status="ARCHIVED", tracked=False),
        ])
        for row in result["variants"]:
            self.assertIn("duplicate_sku", row["flags"])
        archived = self.by_product(result)[("Magnesium old", "Default Title")]
        self.assertEqual(archived["flags"], ["duplicate_sku", "inactive_product", "untracked"])

    def test_unlisted_products_count_as_active(self):
        result = sku_check.check("nuerosmile", [variant("Nerve Support", "NEURO-120", status="UNLISTED")])
        self.assertEqual(result["variants"][0]["flags"], [])
        self.assertEqual(result["rules"][0]["state"], "found")

    def test_rule_only_on_archived_product_or_missing(self):
        archived = sku_check.check("nuerosmile", [variant("Nerve Support", "NEURO-120", status="ARCHIVED")])
        self.assertEqual(archived["rules"][0]["state"], "inactive")
        missing = sku_check.check("nuerosmile", [variant("Magnesium Spray", "NEURO-SPRAY")])
        self.assertEqual(missing["rules"][0]["state"], "missing")
        self.assertEqual(missing["summary"]["unmapped"], 1)
        self.assertEqual(missing["summary"]["rules_missing"], 1)

    def test_muravai_matches_by_product_name_like_its_rules(self):
        result = sku_check.check("muravai", [
            variant("Filtered Showerhead", "MV-SH"),
            variant("Replacement Filters", "MV-F3", "3 pack"),
            variant("Shower Hose", "MV-HOSE"),
            variant("Shower Connector Bracket", "MV-CON"),
            variant("Teflon Tape", "MV-TEF"),
            variant("Mystery Bundle", "MV-X"),
            variant("Shower Hose & Connector Set", "MV-SET"),
        ])
        rows = self.by_product(result)
        self.assertEqual(rows[("Filtered Showerhead", "Default Title")]["internal_sku"], "MUR002")
        self.assertEqual(rows[("Replacement Filters", "3 pack")]["internal_sku"], "MUR001")
        self.assertEqual(rows[("Shower Hose", "Default Title")]["internal_sku"], "MUR004")
        self.assertEqual(rows[("Shower Connector Bracket", "Default Title")]["internal_sku"], "MUR005")
        teflon = rows[("Teflon Tape", "Default Title")]
        self.assertEqual((teflon["status"], teflon["internal_sku"]), ("component", "MUR003"))
        bundle = rows[("Mystery Bundle", "Default Title")]
        self.assertEqual((bundle["status"], bundle["internal_sku"]), ("component", None))
        self.assertIn("not matched to one SKU", bundle["how"])
        self.assertEqual(rows[("Shower Hose & Connector Set", "Default Title")]["internal_sku"], None)
        self.assertEqual({r["internal_sku"]: r["state"] for r in result["rules"]},
                         {"MUR001": "found", "MUR002": "found", "MUR003": "found", "MUR004": "found", "MUR005": "found"})
        self.assertEqual(result["rule_status"], "APPROVED")

    def test_client_without_rules_maps_nothing(self):
        result = sku_check.check("onset", [variant("Onset Gel", "ONS-1")])
        self.assertEqual(result["variants"][0]["status"], "no_rules")
        self.assertEqual(result["summary"]["needs_review"], 1)
        self.assertIsNone(result["variants"][0]["internal_sku"])
        self.assertEqual(result["rules"], [])

    def test_rules_never_cross_clients(self):
        # A Muravai product name in another client's store must not map to a Muravai SKU.
        result = sku_check.check("fascial-labs", [variant("Filtered Showerhead", "MV-SH")])
        self.assertEqual(result["variants"][0]["status"], "unmapped")
        # And another client's code in Muravai's store does not map either.
        result = sku_check.check("muravai", [variant("Support", "FASCSUPP-1")])
        self.assertEqual(result["variants"][0]["status"], "unmapped")


class ShopifyApiTests(unittest.TestCase):
    PASSWORD = "test-password"

    def setUp(self):
        self.client = app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"owner:{self.PASSWORD}".encode()).decode()}
        self.env = {"SHOPIFY_ENABLED": "true", "SHOPIFY_STORES_JSON": json.dumps({"muravai": {"shop": SHOPS["muravai"], "token": "shpat_x"}}),
                    "WORKSPACE_USER": "owner", "SECRET_KEY": "test-secret",
                    "WORKSPACE_PASSWORD_HASH": generate_password_hash(self.PASSWORD, method="pbkdf2:sha256")}

    def test_disabled_does_not_expose_data(self):
        with patch.dict("os.environ", {"SHOPIFY_ENABLED": "false"}):
            self.assertEqual(self.client.get("/api/shopify/sku-check").status_code, 503)

    def test_enabled_without_sign_in_settings_fails_closed(self):
        with patch.dict("os.environ", {"SHOPIFY_ENABLED": "true", "SHOPIFY_STORES_JSON": "{}"}, clear=True):
            self.assertEqual(self.client.get("/").status_code, 503)
            self.assertEqual(self.client.get("/api/workspace").status_code, 503)

    def test_requires_sign_in_and_is_client_scoped(self):
        with patch.dict("os.environ", self.env, clear=True):
            self.assertEqual(self.client.get("/api/shopify/sku-check").status_code, 401)
            # Shopify alone also protects the rest of the workspace.
            self.assertEqual(self.client.get("/api/workspace").status_code, 401)
            catalog = [{"id": "muravai", "variants": [variant("Filtered Showerhead", "MV-SH")], "truncated": False,
                        "missing_scopes": ["read_orders"], "fetched_at": "2026-10-02T00:00:00+00:00"}]
            with patch("app.shopify_source.read_catalogs", return_value=catalog) as reader:
                response = self.client.get("/api/shopify/sku-check?client_id=muravai", headers=self.auth)
            self.assertEqual(response.status_code, 200)
            reader.assert_called_once_with(["muravai"])
            body = response.get_json()
            self.assertEqual(body["writes"], "disabled")
            self.assertEqual([c["client_id"] for c in body["clients"]], ["muravai"])
            self.assertEqual(body["clients"][0]["variants"][0]["internal_sku"], "MUR002")
            self.assertIn("read_orders", body["clients"][0]["warnings"][0])
            self.assertNotIn(SHOPS["muravai"], response.get_data(as_text=True))
            self.assertEqual(self.client.get("/api/shopify/sku-check?client_id=unknown", headers=self.auth).status_code, 400)

    def test_failed_client_returns_error_only(self):
        with patch.dict("os.environ", self.env, clear=True), \
                patch("app.shopify_source.read_catalogs", return_value=[{"id": "muravai", "error_code": "access_denied", "error": "x"}]):
            body = self.client.get("/api/shopify/sku-check?client_id=muravai", headers=self.auth).get_json()
        self.assertEqual(body["clients"], [{"client_id": "muravai", "error_code": "access_denied", "error": "x"}])

    def test_invalid_configuration_fails_closed(self):
        env = dict(self.env, SHOPIFY_STORES_JSON="[not json")
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(self.client.get("/api/shopify/sku-check", headers=self.auth).status_code, 503)


if __name__ == "__main__":
    unittest.main()
