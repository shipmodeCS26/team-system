import os
import unittest

import app as server


class KnowledgebaseTests(unittest.TestCase):
    def setUp(self):
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def test_index_lists_guides_and_sidebar_link(self):
        page = self.client.get("/knowledgebase")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Knowledgebase", page.data)
        self.assertIn(b"/knowledgebase/claude-slack-ssk", page.data)

    def test_guide_page_renders(self):
        page = self.client.get("/knowledgebase/claude-slack-ssk")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"ShipSidekick stock vs. Sheet", page.data)
        self.assertNotIn(b"<script>", page.data)  # CSP allows only external scripts
        self.assertNotIn(b"style=", page.data)    # CSP allows only external styles

    def test_unknown_guide_is_404(self):
        self.assertEqual(self.client.get("/knowledgebase/nope").status_code, 404)
        self.assertEqual(self.client.get("/knowledgebase/..%2Fworkspace").status_code, 404)

    def test_workspace_has_knowledgebase_link(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'href="/knowledgebase"', page.data)


if __name__ == "__main__":
    unittest.main()
