"""A fake Google (OAuth token/revoke, Gmail and Calendar read APIs) for tests. Stdlib only.

Point Callsheet at it with the env from `FakeGoogle.env()`. Every request is recorded in
`.requests` as (method, path, query) so tests can check what Callsheet asked for.
"""
import base64
import datetime as dt
import hashlib
import http.server
import json
import secrets
import threading
import urllib.parse

FULL_SCOPE = ("openid https://www.googleapis.com/auth/userinfo.email "
              "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly")
CLIENT_ID = "1234-fakeclient9.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-secret-value-123"
REDIRECT_URI = "http://127.0.0.1:8765/"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def id_token(email):
    head = _b64(json.dumps({"alg": "none"}).encode())
    body = _b64(json.dumps({"email": email, "email_verified": True, "sub": "1"}).encode())
    return f"{head}.{body}.sig"


def event(summary, start, end, **kw):
    """start/end: aware datetimes, or dates for all-day events."""
    def part(v):
        return {"date": v.isoformat()} if not isinstance(v, dt.datetime) else {"dateTime": v.isoformat()}
    ev = {"id": "ev" + secrets.token_hex(4), "status": "confirmed", "summary": summary,
          "start": part(start), "end": part(end)}
    ev.update(kw)
    return ev


def thread(thread_id, sender, subject, snippet, when, to="me@example.com", labels=("INBOX",), earlier=(),
           cc=None, headers=None, body=None, html_body=None):
    """when: aware datetime of the last message. earlier: [(sender, when, labels)] older messages.
    cc / headers ({name: value}) go on the last message only. body / html_body: text of the last
    message (only returned for format=full, like Gmail)."""
    def msg(frm, at, lbls, subj, snip):
        return {"id": "m" + secrets.token_hex(4), "threadId": thread_id, "labelIds": list(lbls), "snippet": snip,
                "internalDate": str(int(at.timestamp() * 1000)),
                "payload": {"headers": [{"name": "From", "value": frm}, {"name": "To", "value": to},
                                        {"name": "Subject", "value": subj},
                                        {"name": "Date", "value": at.strftime("%a, %d %b %Y %H:%M:%S %z")}]}}
    msgs = [msg(f, a, lb, subject, "earlier message") for f, a, lb in earlier]
    last = msg(sender, when, labels, subject, snippet)
    extra = dict(headers or {})
    if cc:
        extra["Cc"] = cc
    last["payload"]["headers"] += [{"name": k, "value": v} for k, v in extra.items()]
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"data": _b64(body.encode())}})
    if html_body is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": _b64(html_body.encode())}})
    if parts:
        last["payload"]["mimeType"] = "multipart/alternative"
        last["payload"]["parts"] = parts
    msgs.append(last)
    return {"id": thread_id, "historyId": "1", "messages": msgs}


class FakeGoogle:
    def __init__(self):
        self.requests = []
        self.codes = {}
        self.refresh_tokens = {}  # rt -> email
        self.access_tokens = {}   # at -> email
        self.revoked = []
        self.users = {}
        self.scope = FULL_SCOPE
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                fake._handle(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def env(self):
        return {"CALLSHEET_GOOGLE_AUTH_BASE": self.base + "/auth",
                "CALLSHEET_GOOGLE_TOKEN_URL": self.base + "/token",
                "CALLSHEET_GOOGLE_API_BASE": self.base}

    def add_user(self, email, calendars=None, events=None, threads=None):
        self.users[email] = {
            "calendars": calendars if calendars is not None else [
                {"id": email, "summary": email, "primary": True},
                {"id": "family@group.calendar.google.com", "summary": "Family"}],
            "events": events or {}, "threads": threads or [], "fail": None}
        return self.users[email]

    def approve(self, auth_url, email, scope=None):
        """What Google does when the user approves: returns the loopback URL the browser lands on."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
        code = "4/0A" + secrets.token_urlsafe(16)
        self.codes[code] = {"email": email, "challenge": q["code_challenge"][0],
                            "method": q["code_challenge_method"][0], "redirect_uri": q["redirect_uri"][0],
                            "client_id": q["client_id"][0], "used": False}
        qs = urllib.parse.urlencode({"state": q["state"][0], "code": code, "scope": scope or self.scope})
        return q["redirect_uri"][0] + "?" + qs

    def expire_access_tokens(self):
        self.access_tokens.clear()

    def api_requests(self):
        return [r for r in self.requests if r[1].startswith(("/gmail/", "/calendar/"))]

    # --- handling ---------------------------------------------------------
    def _send(self, h, status, data):
        body = json.dumps(data).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h):
        parts = urllib.parse.urlsplit(h.path)
        query = urllib.parse.parse_qs(parts.query)
        length = int(h.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(h.rfile.read(length).decode()) if length else {}
        form = {k: v[0] for k, v in form.items()}
        with self.lock:
            self.requests.append((h.command, parts.path, query))
        path = parts.path
        if h.command == "POST" and path == "/token":
            return self._token(h, form)
        if h.command == "POST" and path == "/revoke":
            self.revoked.append(form.get("token"))
            self.refresh_tokens.pop(form.get("token"), None)
            return self._send(h, 200, {})
        if h.command != "GET":
            return self._send(h, 405, {"error": {"code": 405, "status": "METHOD_NOT_ALLOWED"}})
        auth = h.headers.get("Authorization", "")
        email = self.access_tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if not email:
            return self._send(h, 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        user = self.users[email]
        if user["fail"]:
            return self._send(h, user["fail"], {"error": {"code": user["fail"], "status": "INTERNAL"}})
        if path == "/gmail/v1/users/me/profile":
            return self._send(h, 200, {"emailAddress": email})
        if path == "/gmail/v1/users/me/threads":
            return self._send(h, 200, {"threads": [{"id": t["id"], "snippet": ""} for t in user["threads"]]})
        if path.startswith("/gmail/v1/users/me/threads/"):
            tid = urllib.parse.unquote(path.rsplit("/", 1)[1])
            for t in user["threads"]:
                if t["id"] == tid:
                    if query.get("format") != ["full"]:  # metadata: headers only, no bodies
                        t = json.loads(json.dumps(t))
                        for m in t["messages"]:
                            m["payload"].pop("parts", None)
                    return self._send(h, 200, t)
            return self._send(h, 404, {"error": {"code": 404}})
        if path == "/calendar/v3/users/me/calendarList":
            return self._send(h, 200, {"items": user["calendars"]})
        if path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            cal = urllib.parse.unquote(path[len("/calendar/v3/calendars/"):-len("/events")])
            return self._send(h, 200, {"items": user["events"].get(cal, [])})
        return self._send(h, 404, {"error": {"code": 404}})

    def _token(self, h, form):
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return self._send(h, 401, {"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            c = self.codes.get(form.get("code"))
            verifier = form.get("code_verifier", "")
            if (not c or c["used"] or c["redirect_uri"] != form.get("redirect_uri")
                    or c["method"] != "S256" or _b64(hashlib.sha256(verifier.encode()).digest()) != c["challenge"]):
                return self._send(h, 400, {"error": "invalid_grant", "error_description": "Bad Request"})
            c["used"] = True
            rt, at = "1//rt-" + secrets.token_urlsafe(12), "ya29.at-" + secrets.token_urlsafe(12)
            self.refresh_tokens[rt] = c["email"]
            self.access_tokens[at] = c["email"]
            return self._send(h, 200, {"access_token": at, "expires_in": 3599, "refresh_token": rt,
                                       "scope": self.scope, "token_type": "Bearer",
                                       "id_token": id_token(c["email"])})
        if form.get("grant_type") == "refresh_token":
            email = self.refresh_tokens.get(form.get("refresh_token"))
            if not email:
                return self._send(h, 400, {"error": "invalid_grant", "error_description": "Token has been expired"})
            at = "ya29.at-" + secrets.token_urlsafe(12)
            self.access_tokens[at] = email
            return self._send(h, 200, {"access_token": at, "expires_in": 3599, "scope": self.scope,
                                       "token_type": "Bearer"})
        return self._send(h, 400, {"error": "unsupported_grant_type"})
