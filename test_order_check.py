import base64
import json
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from app import app
import order_check
import shopify_source

SHOP = "muravai-test.myshopify.com"
STORES = {"muravai": {"shop": SHOP, "token": "shpat_secret_muravai"},
          "fascial-labs": {"shop": "fascial-test.myshopify.com", "token": "shpat_secret_fascial"}}
ADDRESS = {"name": "Jane Customer", "address1": "1 Main St", "address2": None, "city": "Miami",
           "provinceCode": "FL", "zip": "33101", "countryCodeV2": "US"}


def order_node(name="#1001", cancelled=None, financial="PAID", items=(("Filtered Showerhead", "", 1),), more=False):
    return {"id": "gid://shopify/Order/1", "name": name, "createdAt": "2026-10-01T10:00:00Z",
            "cancelledAt": cancelled, "displayFinancialStatus": financial, "displayFulfillmentStatus": "FULFILLED",
            "lineItems": {"nodes": [{"name": n, "sku": s, "quantity": q, "currentQuantity": q} for n, s, q in items],
                          "pageInfo": {"hasNextPage": more}}}


class FakeShopify:
    def __init__(self, scopes, nodes, address=ADDRESS, tracking=(("T1", "SUCCESS"),), refuse=()):
        self.scopes, self.nodes, self.calls = scopes, nodes, []
        self.address, self.tracking, self.refuse = address, tracking, refuse

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(json)
        assert "mutation" not in json["query"]
        if json["query"] == shopify_source.SCOPES_QUERY:
            return Resp({"data": {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}}})
        if json["query"] in self.refuse:
            return Resp({"errors": [{"message": "denied", "extensions": {"code": "ACCESS_DENIED"}}]})
        if json["query"] == shopify_source.ORDER_ADDRESS_QUERY:
            return Resp({"data": {"order": {"shippingAddress": self.address}}})
        if json["query"] == shopify_source.ORDER_FULFILLMENTS_QUERY:
            return Resp({"data": {"order": {"fulfillments": [{"status": st, "trackingInfo": [{"number": n}]}
                                                             for n, st in self.tracking]}}})
        assert json["query"] == shopify_source.ORDER_QUERY
        page = int(json["variables"].get("after") or 0)
        more = getattr(self, "pages", 1) > page + 1
        return Resp({"data": {"orders": {"nodes": self.nodes if page == 0 else [],
                                         "pageInfo": {"hasNextPage": more, "endCursor": str(page + 1)}}}})


class Resp:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def read(scopes, nodes, name="#1001", **kw):
    fake = FakeShopify(scopes, nodes, **kw)
    with patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(STORES)}), \
            patch("shopify_source.requests.post", fake.post):
        found = shopify_source.read_order("muravai", name)
        return found["orders"], fake


class ReadOrderTests(unittest.TestCase):
    def test_exact_name_only_and_address_returned(self):
        orders, fake = read(["read_orders", "read_all_orders", "read_customers", "read_products"],
                            [order_node("#1001"), order_node("#10011")])
        self.assertEqual([o["name"] for o in orders], ["#1001"])
        self.assertEqual(orders[0]["address"]["city"], "Miami")
        self.assertTrue(orders[0]["address_visible"])
        self.assertEqual(fake.calls[1]["variables"], {"q": 'name:"#1001"', "after": None})
        self.assertEqual(orders[0]["tracking_numbers"], ["T1"])

    def test_refused_address_keeps_the_order(self):
        orders, _ = read(["read_orders", "read_all_orders", "read_products"], [order_node()],
                         refuse=(shopify_source.ORDER_ADDRESS_QUERY, shopify_source.ORDER_FULFILLMENTS_QUERY))
        self.assertEqual((orders[0]["name"], orders[0]["address"], orders[0]["address_visible"]), ("#1001", None, False))
        self.assertIsNone(orders[0]["tracking_numbers"])

    def test_custom_order_names_are_escaped_not_refused(self):
        orders, fake = read(["read_orders", "read_all_orders", "read_products"], [order_node('SM/10+1 "A"')],
                            name='SM/10+1 "A"')
        self.assertEqual(len(orders), 1)
        self.assertEqual(fake.calls[1]["variables"]["q"], 'name:"SM/10+1 \\"A\\""')
        self.assertEqual(orders[0]["address"]["city"], "Miami")  # field access is Shopify's call, not read_customers

    def test_write_scope_and_missing_order_scope_are_refused(self):
        with self.assertRaises(shopify_source.SourceError) as caught:
            read(["read_products", "read_orders", "write_orders"], [order_node()])
        self.assertEqual(caught.exception.code, "write_scope_granted")
        with self.assertRaises(shopify_source.SourceError) as caught:
            read(["read_products"], [order_node()])
        self.assertEqual(caught.exception.code, "order_scope")

    def test_control_characters_are_not_sent(self):
        with self.assertRaises(shopify_source.InvalidOrderName):
            read(["read_products", "read_orders"], [], name="#10\x0101")

    def test_order_query_is_allowlisted_read(self):
        for query in (shopify_source.ORDER_QUERY, shopify_source.ORDER_ADDRESS_QUERY,
                      shopify_source.ORDER_FULFILLMENTS_QUERY):
            self.assertIn(query, shopify_source.READ_QUERIES)
            self.assertNotRegex(query, shopify_source.WRITE_OPERATION)


class SearchCompletenessTests(unittest.TestCase):
    def test_sixty_day_window_and_too_many_candidates(self):
        fake = FakeShopify(["read_orders", "read_products"], [])
        with patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(STORES)}), \
                patch("shopify_source.requests.post", fake.post):
            found = shopify_source.read_order("muravai", "#1001")
            self.assertEqual((found["complete"], found["search_complete"]), (False, True))
            fake.scopes = ["read_orders", "read_all_orders", "read_products"]
            self.assertTrue(shopify_source.read_order("muravai", "#1001")["complete"])
            fake.nodes, fake.pages = [order_node()], 9
            found = shopify_source.read_order("muravai", "#1001")
        self.assertFalse(found["search_complete"])
        flags = order_check.check("muravai", {"items": []}, found["orders"], search_complete=False)["flags"]
        self.assertEqual(flags, ["search_incomplete"])
        self.assertEqual(order_check.check("muravai", {}, [], complete=False)["flags"], ["not_found_recent"])


class EnrichmentTests(unittest.TestCase):
    def test_address_withheld_without_full_search_and_no_fanout_on_duplicates(self):
        orders, fake = read(["read_orders", "read_products"], [order_node()])
        self.assertTrue(orders[0]["address_withheld"])
        self.assertIsNone(orders[0]["address"])
        self.assertNotIn(shopify_source.ORDER_ADDRESS_QUERY, [c["query"] for c in fake.calls])
        self.assertIn("recent_match_only", order_check.check("muravai", {"items": []}, orders, complete=False)["flags"])
        orders, fake = read(["read_orders", "read_all_orders", "read_products"], [order_node(), order_node()])
        self.assertEqual(len(orders), 2)
        queries = [c["query"] for c in fake.calls]
        self.assertNotIn(shopify_source.ORDER_ADDRESS_QUERY, queries)
        self.assertNotIn(shopify_source.ORDER_FULFILLMENTS_QUERY, queries)


class FulfillmentTests(unittest.TestCase):
    def test_cancelled_fulfillments_are_not_parcels(self):
        orders, _ = read(["read_orders", "read_all_orders", "read_products"], [order_node()],
                         tracking=(("T1", "CANCELLED"), ("T2", "SUCCESS")))
        self.assertEqual(orders[0]["fulfillment_count"], 1)

    def test_partly_fulfilled_order_and_uncountable_lines_are_unverified(self):
        shipment = {"items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]}
        order = {"name": "#1", "financial": "PAID", "fulfillment": "PARTIALLY_FULFILLED", "fulfillment_count": 1,
                 "items": [{"name": "Filtered Showerhead", "sku": "", "qty": 1}, {"name": "Shower Hose", "sku": "", "qty": 1}]}
        flags = order_check.check("muravai", shipment, [order])["flags"]
        self.assertEqual(flags, ["items_unverified"])
        import ssk_shipments
        raw = {"id": "x", "packages": [{"lineItems": [{"quantity": 1, "productVariant": {"sku": "A"}},
                                                      {"quantity": None, "productVariant": {"sku": "B"}}]}]}
        self.assertTrue(ssk_shipments.to_row(raw, "muravai")["items_truncated"])


class UnknownSplitTests(unittest.TestCase):
    SHIPMENT = {"items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]}
    ORDER = {"name": "#1", "financial": "PAID", "items": [{"name": "Filtered Showerhead", "sku": "", "qty": 2}]}

    def test_unread_or_full_fulfillment_list_is_unverified(self):
        for extra in ({"fulfillment_count": None}, {"fulfillment_count": 1, "fulfillments_truncated": True}):
            flags = order_check.check("muravai", self.SHIPMENT, [dict(self.ORDER, **extra)])["flags"]
            self.assertEqual(flags, ["items_unverified"], extra)

    def test_many_cancelled_attempts_are_one_parcel(self):
        orders, fake = read(["read_orders", "read_all_orders", "read_products"], [order_node()],
                            tracking=tuple((f"C{i}", "CANCELLED") for i in range(19)) + (("OK", "SUCCESS"),))
        self.assertEqual(orders[0]["fulfillment_count"], 1)
        self.assertNotIn("first:", shopify_source.ORDER_FULFILLMENTS_QUERY.split("trackingInfo")[0])

    def test_refunded_after_shipping_is_unverified_not_differ(self):
        order = dict(self.ORDER, fulfillment_count=1,
                     items=[{"name": "Filtered Showerhead", "sku": "", "qty": 0, "changed": True}])
        flags = order_check.check("muravai", self.SHIPMENT, [order])["flags"]
        self.assertIn("items_unverified", flags)
        self.assertNotIn("items_differ", flags)

    def test_non_shipping_lines_are_ignored(self):
        node = order_node(items=(("Filtered Showerhead", "", 1),))
        node["lineItems"]["nodes"].append({"name": "Gift card", "sku": "GC", "quantity": 1, "currentQuantity": 1,
                                           "requiresShipping": False})
        orders, _ = read(["read_orders", "read_all_orders", "read_products"], [node])
        self.assertEqual([i["name"] for i in orders[0]["items"]], ["Filtered Showerhead"])


class FlagTests(unittest.TestCase):
    SHIPMENT = {"items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]}

    def flags(self, orders, shipment=None, same=1):
        return order_check.check("muravai", shipment or self.SHIPMENT, orders, same)["flags"]

    def order(self, **kw):
        return {"name": "#1001", "cancelled_at": kw.get("cancelled"), "financial": kw.get("financial", "PAID"),
                "fulfillment_count": 1,
                "items": kw.get("items", [{"name": "Filtered Showerhead", "sku": "", "qty": 1}])}

    def test_matching_order_has_no_flags(self):
        self.assertEqual(self.flags([self.order()]), [])

    def test_each_flag(self):
        self.assertEqual(self.flags([]), ["not_found"])
        self.assertEqual(self.flags([self.order(), self.order()]), ["several_orders"])
        self.assertIn("cancelled", self.flags([self.order(cancelled="2026-10-02T00:00:00Z")]))
        self.assertIn("refunded", self.flags([self.order(financial="REFUNDED")]))
        self.assertIn("partially_refunded", self.flags([self.order(financial="PARTIALLY_REFUNDED")]))
        self.assertIn("items_differ", self.flags([self.order(items=[{"name": "Filtered Showerhead", "sku": "", "qty": 2}])]))
        self.assertIn("items_unverified", self.flags([self.order(items=[{"name": "Mystery item", "sku": "", "qty": 1}])]))
        split = self.flags([self.order()], same=2)
        self.assertIn("several_shipments", split)
        self.assertIn("items_unverified", split)  # a split package never yields "items differ"
        self.assertNotIn("items_differ", self.flags([self.order(items=[{"name": "Filtered Showerhead", "sku": "", "qty": 2}])], same=2))
        self.assertIn("several_shipments", self.flags([dict(self.order(), tracking_numbers=["A", "B"])]))
        self.assertIn("items_unverified", self.flags([dict(self.order(), items_truncated=True)]))
        untracked = self.flags([dict(self.order(), tracking_numbers=[], fulfillment_count=2)])
        self.assertIn("several_shipments", untracked)
        self.assertNotIn("items_differ", untracked)
        self.assertEqual(order_check.check("muravai", self.SHIPMENT, [], unlinked=True)["flags"], ["unlinked"])

    def test_removed_lines_are_ignored(self):
        items = [{"name": "Filtered Showerhead", "sku": "", "qty": 1}, {"name": "Shower Hose", "sku": "", "qty": 0}]
        self.assertEqual(self.flags([self.order(items=items)]), [])


class OrderEndpointTests(unittest.TestCase):
    ENV = {"SHOPIFY_ENABLED": "true", "SSK_API_ENABLED": "true", "SHOPIFY_STORES_JSON": json.dumps(STORES),
           "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}
    STORE = {"id": "muravai", "rows": [
        {"ssk_id": "s1", "tracking_number": "TRK1", "order_number": "#1001",
         "items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]},
        {"ssk_id": "s2", "tracking_number": "TRK2", "order_number": "#1001", "items": []},
        {"ssk_id": "s3", "tracking_number": "TRK3", "order_number": None, "items": []}]}

    def test_requires_sign_in_reads_only_that_client_and_never_logs_address(self):
        client = app.test_client()
        fake = FakeShopify(["read_orders", "read_all_orders", "read_products"], [order_node()])
        with patch.dict("os.environ", self.ENV, clear=True), \
                patch("app.ssk_source.read_shipments", return_value=self.STORE) as shipments, \
                patch("shopify_source.requests.post", fake.post):
            self.assertEqual(client.get("/api/shopify/order?client_id=muravai&shipment=s1").status_code, 401)
            with self.assertLogs(level="DEBUG") as logs:
                import logging
                logging.getLogger("test").debug("marker")
                body = client.get("/api/shopify/order?client_id=muravai&shipment=s1", headers=self.AUTH).get_json()
            missing = client.get("/api/shopify/order?client_id=muravai&shipment=NOPE", headers=self.AUTH)
            unlinked = client.get("/api/shopify/order?client_id=muravai&shipment=s3", headers=self.AUTH).get_json()
            workspace_clients = __import__("app").shopify_order_clients()
        self.assertEqual(unlinked["flags"], ["unlinked"])
        self.assertEqual(workspace_clients, ["fascial-labs", "muravai"])
        shipments.assert_called_with("muravai", 30)
        self.assertEqual(body["order"]["address"]["address1"], "1 Main St")
        self.assertEqual(body["flags"], ["items_unverified", "several_shipments"])
        self.assertEqual(body["writes"], "disabled")
        self.assertEqual(missing.status_code, 404)
        for line in logs.output:
            self.assertNotIn("Main St", line)
            self.assertNotIn("Jane", line)

    def test_shopify_failure_is_reported_not_mislabelled(self):
        client = app.test_client()
        with patch.dict("os.environ", self.ENV, clear=True), \
                patch("app.ssk_source.read_shipments", return_value=self.STORE), \
                patch("app.shopify_source.read_order", side_effect=shopify_source.SourceError("unavailable")):
            response = client.get("/api/shopify/order?client_id=muravai&shipment=s1", headers=self.AUTH)
        self.assertEqual(response.status_code, 502)
        self.assertIn("did not respond", response.get_json()["error"])

    def test_broken_store_config_gives_503(self):
        client = app.test_client()
        with patch.dict("os.environ", {**self.ENV, "SHOPIFY_STORES_JSON": "{not json"}, clear=True), \
                patch("app.ssk_source.read_shipments", return_value=self.STORE):
            response = client.get("/api/shopify/order?client_id=muravai&shipment=s1", headers=self.AUTH)
        self.assertEqual(response.status_code, 503)

    def test_disabled_exposes_nothing(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(app.test_client().get("/api/shopify/order?client_id=muravai&shipment=s1").status_code, 503)


if __name__ == "__main__":
    unittest.main()
