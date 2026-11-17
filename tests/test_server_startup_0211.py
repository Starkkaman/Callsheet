"""0.21.1: every attribute main() starts on the app exists (0.21.0 crashed at start on the retired index scanner)."""
import pathlib
import re
import sys
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent / "callsheet" / "app"
sys.path.insert(0, str(APP))
import server  # noqa: E402


class StartupAttributes(unittest.TestCase):
    def test_main_uses_only_existing_app_attributes(self):
        names = set(re.findall(r"httpd\.app\.([a-z_]+)", (APP / "server.py").read_text()))
        self.assertTrue(names)
        with tempfile.TemporaryDirectory() as d:
            httpd = server.make_server("127.0.0.1", 0, data=d)
            try:
                missing = sorted(n for n in names if not hasattr(httpd.app, n))
                self.assertEqual(missing, [])
            finally:
                httpd.server_close()
                httpd.app.close()


if __name__ == "__main__":
    unittest.main()
