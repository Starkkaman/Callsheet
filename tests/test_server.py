"""Server and ingress-safety checks. Stdlib only: python3 -m unittest discover -s tests"""
import http.client
import json
import pathlib
import re
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import server  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = server.make_server("127.0.0.1", 0, data=cls.tmp.name)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.app.close()
        cls.tmp.cleanup()

    def get(self, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_index(self):
        r, body = self.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")
        v = server.VERSION.encode()
        self.assertIn(b'href="app.css?v=' + v + b'"', body)
        self.assertIn(b'src="app.js?v=' + v + b'"', body)
        self.assertIn(b'<meta name="callsheet-version" content="' + v + b'">', body)
        self.assertNotIn(b"{{", body)
        r2, body2 = self.get("/index.html")
        self.assertEqual((r2.status, body2), (200, body))

    def test_version_matches_config(self):
        cfg = (ROOT / "callsheet" / "config.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version:\s*"([^"]+)"', cfg, re.M).group(1)
        self.assertEqual(server.VERSION, version, "set APP_VERSION in api.py to config.yaml's version")
        r, body = self.get("/api/state")
        self.assertEqual(json.loads(body)["version"], version)

    def test_versioned_assets_and_etag(self):
        for name in ("/", "/app.css", "/app.js"):
            r, body = self.get(name)
            etag = r.getheader("ETag")
            self.assertRegex(etag or "", r'^"[0-9a-f]{32}"$', name)
            self.assertIsNone(r.getheader("Last-Modified"), name)
            if name != "/":  # the query string is ignored when finding the file
                for q in ("?v=" + server.VERSION, "?v=0.0.1", "?v=x&y=1"):
                    rq, bq = self.get(name + q)
                    self.assertEqual((rq.status, bq, rq.getheader("ETag")), (200, body, etag), name + q)
            for inm in (etag, "W/" + etag, '"other", ' + etag, "*"):
                r304, b304 = self.get(name + "?v=" + server.VERSION, headers={"If-None-Match": inm})
                self.assertEqual((r304.status, b304), (304, b""), (name, inm))
                self.assertEqual(r304.getheader("ETag"), etag)
                self.assertEqual(r304.getheader("Cache-Control"), "no-cache")
            r200, b200 = self.get(name, headers={"If-None-Match": '"0000"',
                                                 "If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"})
            self.assertEqual((r200.status, b200), (200, body), name)
            rh, bh = self.get(name, "HEAD")
            self.assertEqual((rh.status, bh, rh.getheader("ETag")), (200, b"", etag), name)
            self.assertEqual(rh.getheader("Content-Length"), str(len(body)))

    def test_fonts_cache_unchanged(self):
        r, _ = self.get("/fonts/jost.ttf?v=1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "public, max-age=604800")

    def test_brief_json(self):
        for path in ("/api/brief", "/api/brief?x=1"):
            r, body = self.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertEqual(r.getheader("Cache-Control"), "no-cache")
            data = json.loads(body)
            self.assertIs(data["sample"], True)
            self.assertEqual(set(data["sections"].values()), {"sample"})
            area_ids = {a["id"] for a in data["areas"]}
            for key in ("schedule", "replies", "projects"):
                for item in data[key]:
                    self.assertIn(item["area"], area_ids, (key, item))

    def test_head(self):
        r, body = self.get("/api/brief", "HEAD")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")

    def test_static_and_fonts(self):
        for path, ctype in (("/app.css", "text/css"), ("/app.js", "text/javascript"),
                            ("/fonts/jost.ttf", "font/ttf"),
                            ("/fonts/ibm-plex-sans-latin-400-600.woff2", "font/woff2")):
            r, _ = self.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(ctype, r.getheader("Content-Type"), path)

    def test_no_listing_traversal_or_unknown_api(self):
        for path in ("/fonts", "/fonts/", "/../server.py", "/%2e%2e/server.py", "/sample_brief.json",
                     "/api/other", "/server.py"):
            r, _ = self.get(path)
            self.assertEqual(r.status, 404, path)

    def test_allowlist(self):
        allow = server.parse_allow(server.DEFAULT_ALLOW)
        self.assertTrue(server.client_allowed(allow, "172.30.32.2"))
        self.assertTrue(server.client_allowed(allow, "127.0.0.1"))
        self.assertTrue(server.client_allowed(allow, "::ffff:172.30.32.2"))
        self.assertFalse(server.client_allowed(allow, "172.30.32.1"))
        self.assertFalse(server.client_allowed(allow, "192.168.1.20"))
        self.assertFalse(server.client_allowed(allow, "not-an-ip"))
        self.assertTrue(server.client_allowed(server.parse_allow("*"), "192.168.1.20"))

    def test_refuses_other_clients(self):
        httpd = server.make_server("127.0.0.1", 0, allow="172.30.32.2", app=self.httpd.app)  # no loopback
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for path in ("/", "/api/brief"):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                c.request("GET", path)
                self.assertEqual(c.getresponse().status, 403, path)
                c.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


class IngressSafetyTest(unittest.TestCase):
    """Ingress serves the app under /api/hassio_ingress/<token>/: every URL must be relative,
    and the app must work offline (no external URLs)."""

    ABSOLUTE = re.compile(r"""(?:href|src|action)\s*=\s*["']/|url\(\s*["']?/|fetch\(\s*["']/""")
    EXTERNAL = re.compile(r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", re.I)

    def test_static_urls(self):
        for name in ("index.html", "app.css", "app.js"):
            text = (APP / "static" / name).read_text(encoding="utf-8")
            self.assertIsNone(self.ABSOLUTE.search(text), name)
            self.assertIsNone(self.EXTERNAL.search(text), name)

    def test_font_files_exist(self):
        css = (APP / "static" / "app.css").read_text(encoding="utf-8")
        for ref in re.findall(r"url\(([^)]+)\)", css):
            self.assertTrue((APP / "static" / ref.strip("'\"")).is_file(), ref)


if __name__ == "__main__":
    unittest.main()
