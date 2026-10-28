#!/usr/bin/env python3
"""Callsheet web server (stdlib only).

Serves the Daily Brief page from static/ and the brief data at api/brief.
Home Assistant Ingress strips its /api/hassio_ingress/<token> prefix before
forwarding, so every path here is served the same way whatever the prefix;
the page itself only uses relative URLs.

Only GET and HEAD. Nothing here sends anything anywhere.
"""
import argparse
import http.server
import ipaddress
import json
import pathlib
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
SAMPLE = HERE / "sample_brief.json"

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


def load_brief():
    """The brief to show. Step 1: always the bundled sample."""
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    # --- routing -------------------------------------------------------
    def _route(self, head):
        if not client_allowed(self.allow, self.client_address[0]):
            self.send_error(403, "Forbidden")
            return None
        path = urllib.parse.urlsplit(self.path).path
        if path.rstrip("/") == "/api/brief":
            return self._send_json(load_brief(), head)
        if path.startswith("/api/") or (path != "/" and pathlib.Path(self.translate_path(path)).is_dir()):
            # no directory listings, and no redirect to an absolute "dir/" URL (breaks under ingress)
            self.send_error(404, "Not found")
            return None
        return False  # fall through to static files

    def do_GET(self):
        if self._route(head=False) is False:
            super().do_GET()

    def do_HEAD(self):
        if self._route(head=True) is False:
            super().do_HEAD()

    # --- responses -----------------------------------------------------
    def _send_json(self, data, head):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
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


def make_server(host="0.0.0.0", port=8099, allow=DEFAULT_ALLOW):
    handler = type("CallsheetHandler", (Handler,), {"allow": parse_allow(allow)})
    return http.server.ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Callsheet web server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--allow", default=DEFAULT_ALLOW,
                    help="comma-separated client IPs, or * for any (default: %(default)s)")
    args = ap.parse_args(argv)
    httpd = make_server(args.host, args.port, args.allow)
    print(f"Callsheet listening on {args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
