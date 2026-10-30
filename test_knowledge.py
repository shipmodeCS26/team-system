import unittest
from unittest.mock import patch

from app import app


class KnowledgeBasePageTests(unittest.TestCase):
    """The Knowledge Base tab sits under Invoices and is served only through the protected home page."""

    def setUp(self):
        self.client = app.test_client()

    def test_knowledge_base_tab_follows_invoices(self):
        with patch.dict("os.environ", {}, clear=True):
            html = self.client.get("/").get_data(as_text=True)
        invoices = html.index('data-section="invoices"')
        knowledge = html.index('data-section="knowledge"')
        self.assertLess(invoices, knowledge)
        self.assertIn('<section id="knowledge-section" hidden>', html)
        for heading in ("Why email for ShipMode", "ShipMode's email flows", "Benchmarks", "Glossary"):
            self.assertIn(heading, html)

    def test_knowledge_base_stays_behind_sign_in(self):
        with patch.dict("os.environ", {"INVENTORY_SHEETS_ENABLED": "true", "INVENTORY_SHEETS_JSON": ""}, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("knowledge-section", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
