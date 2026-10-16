import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class PanelLoaderTests(unittest.TestCase):
    """Runs the browser-side regression tests (test_panels.js) with Node's built-in runner."""

    def test_panel_loaders(self):
        run = subprocess.run(["node", "--test", "test_panels.js"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
