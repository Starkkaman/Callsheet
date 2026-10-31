"""A fake Home Assistant Core API (the Supervisor's /core/api proxy) for tests. Stdlib only.

Point Callsheet at it with the env from `FakeHA.env()` (CALLSHEET_HA_API_BASE and a fake SUPERVISOR_TOKEN).
Every request is recorded in `.requests` as (method, path, json body). GET /services lists `notify` services
from `.services`; POST /services/notify/<name> answers 400 for unknown names (as Home Assistant does), 401 for a
wrong token, and `.fail` (an HTTP status) when set. Sent notifications are kept in `.sent` as (name, body).
"""
import http.server
import json
import threading

TOKEN = "fake-supervisor-token-0123456789abcdef"


class FakeHA:
    def __init__(self):
        self.requests = []
        self.sent = []
        self.services = ["mobile_app_iphone", "mobile_app_pixel_9a", "mobile_app_isaac_ipad", "persistent_notification",
                         "notify"]
        self.fail = None
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                fake._handle(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/core/api"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def env(self):
        return {"CALLSHEET_HA_API_BASE": self.base, "SUPERVISOR_TOKEN": TOKEN}

    def _send(self, h, status, data):
        body = json.dumps(data).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        with self.lock:
            self.requests.append((h.command, h.path, body))
        if h.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(h, 401, {"message": "Unauthorized"})
        if self.fail:
            return self._send(h, self.fail, {"message": "failed"})
        if h.command == "GET" and h.path == "/core/api/services":
            return self._send(h, 200, [
                {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
                {"domain": "notify", "services": {s: {"name": "Send a notification"} for s in self.services}}])
        prefix = "/core/api/services/notify/"
        if h.command == "POST" and h.path.startswith(prefix):
            name = h.path[len(prefix):]
            if name not in self.services:
                return self._send(h, 400, {"message": f"Service notify.{name} not found."})
            with self.lock:
                self.sent.append((name, body))
            return self._send(h, 200, [])
        return self._send(h, 404, {"message": "Not found"})
