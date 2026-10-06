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


def order_node(name="#1001", cancelled=None, financial="PAID", items=(("Filtered Showerhead", "", 1),)):
    return {"name": name, "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-01T10:00:00Z",
            "cancelledAt": cancelled, "displayFinancialStatus": financial, "displayFulfillmentStatus": "FULFILLED",
            "lineItems": {"nodes": [{"name": n, "sku": s, "quantity": q, "currentQuantity": q} for n, s, q in items]},
            "shippingAddress": ADDRESS}


class FakeShopify:
    def __init__(self, scopes, nodes):
        self.scopes, self.nodes, self.calls = scopes, nodes, []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append(json)
        assert "mutation" not in json["query"]
        if json["query"] == shopify_source.SCOPES_QUERY:
            return Resp({"data": {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}}})
        assert json["query"] == shopify_source.ORDER_QUERY
        return Resp({"data": {"orders": {"nodes": self.nodes}}})


class Resp:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def read(scopes, nodes, name="#1001"):
    fake = FakeShopify(scopes, nodes)
    with patch.dict("os.environ", {"SHOPIFY_STORES_JSON": json.dumps(STORES)}), \
            patch("shopify_source.requests.post", fake.post):
        return shopify_source.read_order("muravai", name), fake


class ReadOrderTests(unittest.TestCase):
    def test_exact_name_only_and_address_returned(self):
        orders, fake = read(["read_orders", "read_customers", "read_products"],
                            [order_node("#1001"), order_node("#10011")])
        self.assertEqual([o["name"] for o in orders], ["#1001"])
        self.assertEqual(orders[0]["address"]["city"], "Miami")
        self.assertTrue(orders[0]["address_visible"])
        self.assertEqual(fake.calls[-1]["variables"], {"q": 'name:"#1001"'})

    def test_write_scope_and_missing_order_scope_are_refused(self):
        with self.assertRaises(shopify_source.SourceError) as caught:
            read(["read_products", "read_orders", "write_orders"], [order_node()])
        self.assertEqual(caught.exception.code, "write_scope_granted")
        with self.assertRaises(shopify_source.SourceError) as caught:
            read(["read_products"], [order_node()])
        self.assertEqual(caught.exception.code, "order_scope")

    def test_odd_order_names_are_not_sent(self):
        with self.assertRaises(ValueError):
            read(["read_products", "read_orders"], [], name='#1" OR name:*')

    def test_order_query_is_allowlisted_read(self):
        self.assertIn(shopify_source.ORDER_QUERY, shopify_source.READ_QUERIES)
        self.assertNotRegex(shopify_source.ORDER_QUERY, shopify_source.WRITE_OPERATION)


class FlagTests(unittest.TestCase):
    SHIPMENT = {"items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]}

    def flags(self, orders, shipment=None, same=1):
        return order_check.check("muravai", shipment or self.SHIPMENT, orders, same)["flags"]

    def order(self, **kw):
        return {"name": "#1001", "cancelled_at": kw.get("cancelled"), "financial": kw.get("financial", "PAID"),
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
        self.assertIn("several_shipments", self.flags([self.order()], same=2))

    def test_removed_lines_are_ignored(self):
        items = [{"name": "Filtered Showerhead", "sku": "", "qty": 1}, {"name": "Shower Hose", "sku": "", "qty": 0}]
        self.assertEqual(self.flags([self.order(items=items)]), [])


class OrderEndpointTests(unittest.TestCase):
    ENV = {"SHOPIFY_ENABLED": "true", "SSK_API_ENABLED": "true", "SHOPIFY_STORES_JSON": json.dumps(STORES),
           "WORKSPACE_USER": "owner", "SECRET_KEY": "s",
           "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw", method="pbkdf2:sha256")}
    AUTH = {"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()}
    STORE = {"id": "muravai", "rows": [
        {"tracking_number": "TRK1", "order_number": "#1001", "items": [{"sku": "", "name": "Filtered Showerhead", "qty": 1}]},
        {"tracking_number": "TRK2", "order_number": "#1001", "items": []}]}

    def test_requires_sign_in_reads_only_that_client_and_never_logs_address(self):
        client = app.test_client()
        fake = FakeShopify(["read_orders", "read_customers", "read_products"], [order_node()])
        with patch.dict("os.environ", self.ENV, clear=True), \
                patch("app.ssk_source.read_shipments", return_value=self.STORE) as shipments, \
                patch("shopify_source.requests.post", fake.post):
            self.assertEqual(client.get("/api/shopify/order?client_id=muravai&tracking=TRK1").status_code, 401)
            with self.assertLogs(level="DEBUG") as logs:
                import logging
                logging.getLogger("test").debug("marker")
                body = client.get("/api/shopify/order?client_id=muravai&tracking=TRK1", headers=self.AUTH).get_json()
            missing = client.get("/api/shopify/order?client_id=muravai&tracking=NOPE", headers=self.AUTH)
        shipments.assert_called_with("muravai", 30)
        self.assertEqual(body["order"]["address"]["address1"], "1 Main St")
        self.assertEqual(body["flags"], ["several_shipments"])
        self.assertEqual(body["writes"], "disabled")
        self.assertEqual(missing.status_code, 404)
        for line in logs.output:
            self.assertNotIn("Main St", line)
            self.assertNotIn("Jane", line)

    def test_disabled_exposes_nothing(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(app.test_client().get("/api/shopify/order?client_id=muravai&tracking=T").status_code, 503)


if __name__ == "__main__":
    unittest.main()
