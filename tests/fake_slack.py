"""A fake Slack Web API for tests (read methods only). Stdlib only.

Point Callsheet at it with the env from `FakeSlack.env()`. Every request is recorded in `.requests` as
(http method, api method, params). Messages are kept per channel; thread replies carry thread_ts. `limit`
{api method: n} answers the next n calls of that method with HTTP 429 and Retry-After `retry_after`.
"""
import http.server
import json
import threading
import urllib.parse

TOKEN = "xoxp-1111-2222-3333-fakeusertoken"
ME = "UME"


class FakeSlack:
    def __init__(self):
        self.requests = []
        self.tokens = {TOKEN}
        self.team = {"id": "T1", "name": "Northwind", "domain": "northwind"}
        self.users = {ME: {"name": "sam", "email": "sam@northwind.example"}}
        self.convs = []       # conversations.list objects
        self.messages = {}    # channel -> [msg], any order; replies have thread_ts != ts
        self.limit = {}
        self.retry_after = "2"
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                parts = urllib.parse.urlsplit(self.path)
                name = parts.path.rsplit("/", 1)[-1]
                params = dict(urllib.parse.parse_qsl(parts.query))
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    params.update(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                with fake.lock:
                    fake.requests.append((method, name, params))
                    if fake.limit.get(name):
                        fake.limit[name] -= 1
                        self.send_response(429)
                        self.send_header("Retry-After", fake.retry_after)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    auth = self.headers.get("Authorization", "")
                    if auth.removeprefix("Bearer ") not in fake.tokens:
                        out = {"ok": False, "error": "invalid_auth"}
                    else:
                        out = fake.answer(name, params)
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/api"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def env(self):
        return {"CALLSHEET_SLACK_API_BASE": self.base}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- data helpers ---------------------------------------------------------
    def channel(self, cid, name, private=False, member=True):
        self.convs.append({"id": cid, "name": name, "is_channel": not private, "is_group": private,
                           "is_private": private, "is_member": member, "created": 1})
        self.messages.setdefault(cid, [])

    def dm(self, cid, user, priority=0.5):
        self.convs.append({"id": cid, "is_im": True, "user": user, "priority": priority, "created": 1})
        self.messages.setdefault(cid, [])

    def group(self, cid, name):
        self.convs.append({"id": cid, "name": name, "is_mpim": True, "is_group": True, "is_private": True,
                           "created": 1})
        self.messages.setdefault(cid, [])

    def user(self, uid, name, email=""):
        self.users[uid] = {"name": name, "email": email}

    def post(self, channel, user, text, ts, thread_ts=None, **kw):
        m = {"type": "message", "user": user, "text": text, "ts": f"{ts:.6f}"}
        if thread_ts:
            m["thread_ts"] = thread_ts
        m.update(kw)
        self.messages[channel].append(m)
        return m["ts"]

    def calls(self, name):
        return [p for _, n, p in self.requests if n == name]

    # --- answers ----------------------------------------------------------------
    def _parents(self, channel):
        """Top-level messages as conversations.history returns them (thread parents with reply info)."""
        msgs = self.messages.get(channel, [])
        out = []
        for m in msgs:
            if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                continue
            m = dict(m)
            replies = [r for r in msgs if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"]]
            if replies:
                m["thread_ts"] = m["ts"]
                m["reply_count"] = len(replies)
                m["reply_users"] = sorted({r["user"] for r in replies})
                m["latest_reply"] = max(r["ts"] for r in replies)
            out.append(m)
        return out

    def answer(self, name, p):
        if name == "auth.test":
            return {"ok": True, "url": f"https://{self.team['domain']}.slack.com/", "team": self.team["name"],
                    "user": self.users[ME]["name"], "team_id": self.team["id"], "user_id": ME}
        if name == "team.info":
            return {"ok": True, "team": dict(self.team)}
        if name == "users.info":
            u = self.users.get(p.get("user"))
            if not u:
                return {"ok": False, "error": "user_not_found"}
            prof = {"display_name": u["name"], "real_name": u["name"]}
            if u.get("email"):
                prof["email"] = u["email"]
            return {"ok": True, "user": {"id": p["user"], "name": u["name"].lower(), "profile": prof}}
        if name == "conversations.list":
            return {"ok": True, "channels": self.convs, "response_metadata": {"next_cursor": ""}}
        if name == "conversations.history":
            if p.get("channel") not in self.messages:
                return {"ok": False, "error": "channel_not_found"}
            msgs = [m for m in self._parents(p["channel"]) if float(m["ts"]) >= float(p.get("oldest") or 0)]
            msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
            return {"ok": True, "messages": msgs[: int(p.get("limit") or 100)], "has_more": False}
        if name == "conversations.replies":
            ch, ts = p.get("channel"), p.get("ts")
            parents = {m["ts"]: m for m in self._parents(ch)} if ch in self.messages else {}
            if ts not in parents:
                return {"ok": False, "error": "thread_not_found"}
            replies = [m for m in self.messages[ch] if m.get("thread_ts") == ts and m["ts"] != ts]
            return {"ok": True, "messages": [parents[ts]] + sorted(replies, key=lambda m: float(m["ts"]))}
        if name == "conversations.info":
            return {"ok": True, "channel": next((c for c in self.convs if c["id"] == p.get("channel")), {})}
        return {"ok": False, "error": "unknown_method"}
