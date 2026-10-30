#!/usr/bin/env python3
"""Callsheet web server (stdlib only).

Serves the Daily Brief page from static/ and the JSON API under api/ (see api.py and
docs/step2-google.md). Home Assistant Ingress strips its /api/hassio_ingress/<token> prefix before
forwarding, so every path here is served the same way whatever the prefix; the page itself only
uses relative URLs.

This layer stays thin: client allowlist, the X-Callsheet header on mutating requests, a 64 KB JSON
body limit, and JSON errors. Nothing here sends mail or messages; the only outbound traffic is the
read-only Google client in google.py.
"""
import argparse
import hashlib
import html
import http.server
import ipaddress
import json
import os
import pathlib
import sys
import traceback
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import api  # noqa: E402
from store import Conflict, InputError, NotFound, StoreError  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
SAMPLE = api.SAMPLE
MAX_BODY = 64 * 1024
MUTATING = ("POST", "PATCH", "DELETE")
VERSION = api.APP_VERSION

# The page and its two assets: served with a strong ETag (content hash) and Cache-Control: no-cache.
# index.html is a template: {{version}} becomes the app version, so the page asks for app.css?v=<version>
# and app.js?v=<version> (a new URL after every update, even if a browser kept the old file) and carries
# <meta name="callsheet-version"> for the page's own version check (app.js compares it with api/state).
PAGE_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}

# The Supervisor's ingress proxy. Everything else is refused (HA ingress docs).
# Loopback stays allowed for local testing and in-container health checks.
DEFAULT_ALLOW = "172.30.32.2,127.0.0.1,::1"

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def data_dir():
    return os.environ.get("CALLSHEET_DATA", "/data")


def parse_allow(spec):
    """'*' allows everyone; otherwise a comma list of IPs. Returns None for '*'."""
    if spec.strip() == "*":
        return None
    return {ipaddress.ip_address(p.strip()) for p in spec.split(",") if p.strip()}


def client_allowed(allow, host):
    if allow is None:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip in allow


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "Callsheet"
    sys_version = ""
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, **TYPES}
    allow = parse_allow(DEFAULT_ALLOW)
    app = None  # api.App, set by make_server

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    # --- routing -------------------------------------------------------
    def _route(self, head):
        if not client_allowed(self.allow, self.client_address[0]):
            self.send_error(403, "Forbidden")
            return None
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            return self._api("GET", path, head)
        if path != "/" and pathlib.Path(self.translate_path(path)).is_dir():
            # no directory listings, and no redirect to an absolute "dir/" URL (breaks under ingress)
            self.send_error(404, "Not found")
            return None
        return False  # fall through to static files

    def do_GET(self):
        if self._route(head=False) is False and not self._page_file(head=False):
            super().do_GET()

    def do_HEAD(self):
        if self._route(head=True) is False and not self._page_file(head=True):
            super().do_HEAD()

    def _page_file(self, head):
        """index.html, app.css, app.js (the query string, e.g. ?v=0.3.4, is ignored when finding the file).
        Returns False for any other path."""
        name = PAGE_FILES.get(urllib.parse.urlsplit(self.path).path)
        if name is None:
            return False
        try:
            body = (STATIC / name).read_bytes()
        except OSError:
            self.send_error(404, "Not found")
            return True
        if name == "index.html":
            body = body.replace(b"{{version}}", html.escape(VERSION).encode())
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if self._etag_matches(etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return True
        self.send_response(200)
        self.send_header("Content-Type", TYPES[pathlib.PurePath(name).suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.end_headers()
        if not head:
            self.wfile.write(body)
        return True

    def _etag_matches(self, etag):
        """If-None-Match uses the weak comparison (RFC 9110 13.1.2): W/ is ignored."""
        inm = self.headers.get("If-None-Match")
        if not inm:
            return False
        tags = [t.strip() for t in inm.split(",")]
        return "*" in tags or etag in (t[2:] if t.startswith("W/") else t for t in tags)

    def _mutate(self):
        if not client_allowed(self.allow, self.client_address[0]):
            self.send_error(403, "Forbidden")
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._send_json({"error": "There's nothing here."}, False, 404)
        if self.headers.get("X-Callsheet") != "1":
            return self._send_json({"error": "This request is missing the X-Callsheet header."}, False, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._send_json({"error": "That request is too large."}, False, 413)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                return self._send_json({"error": "That request isn't valid JSON."}, False, 400)
        elif self.command != "DELETE":
            body = {}
        return self._api(self.command, path, False, body)

    do_POST = do_PATCH = do_DELETE = _mutate

    def _api(self, method, path, head, body=None):
        sub = path[len("/api/"):].rstrip("/")
        try:
            data = self.app.handle(method, sub, body)
        except InputError as e:
            return self._send_json({"error": str(e)}, head, 400)
        except NotFound as e:
            return self._send_json({"error": str(e)}, head, 404)
        except Conflict as e:
            return self._send_json({"error": str(e)}, head, 409)
        except api.MethodNotAllowed:
            return self._send_json({"error": "That action isn't possible here."}, head, 405)
        except Exception:
            # Tracebacks carry no local values; our exceptions never hold tokens.
            sys.stderr.write(traceback.format_exc())
            return self._send_json({"error": "Something went wrong inside Callsheet. Check the app log."},
                                   head, 500)
        if isinstance(data, api.Download):
            return self._send_file(data, head)
        return self._send_json(data, head)

    # --- responses -----------------------------------------------------
    def _send_file(self, dl, head):
        """An attachment download (the writer evaluation export). The filename is built by api.py from a
        checked run id, so it needs no escaping."""
        self.send_response(200)
        self.send_header("Content-Type", dl.content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{dl.filename}"')
        self.send_header("Content-Length", str(len(dl.body)))
        self.end_headers()
        if not head:
            self.wfile.write(dl.body)
        return True

    def _send_json(self, data, head, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", TYPES[".json"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)
        return True

    def end_headers(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/fonts/"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:  # index, app.css/js and api: always revalidate
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def make_server(host="0.0.0.0", port=8099, allow=DEFAULT_ALLOW, data=None, app=None):
    """data: data directory (default env CALLSHEET_DATA or /data). The scheduler is not started here."""
    app = app or api.App(data or data_dir())
    handler = type("CallsheetHandler", (Handler,), {"allow": parse_allow(allow), "app": app})
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    httpd.app = app
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="Callsheet web server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--allow", default=DEFAULT_ALLOW,
                    help="comma-separated client IPs, or * for any (default: %(default)s)")
    ap.add_argument("--no-scheduler", action="store_true", help="don't sync at call times (testing)")
    args = ap.parse_args(argv)
    os.umask(0o077)  # the data dir holds secrets: new files are private
    try:
        httpd = make_server(args.host, args.port, args.allow)
    except StoreError as e:
        # Never start on a fresh, empty database in place of one that didn't open: the file is untouched.
        print(f"Callsheet can't start: {e} The database was left unchanged. "
              "Restore a backup or report this.", file=sys.stderr, flush=True)
        sys.exit(1)
    if not args.no_scheduler:
        httpd.app.scheduler.start()
    print(f"Callsheet listening on {args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        httpd.app.close()


if __name__ == "__main__":
    main()
