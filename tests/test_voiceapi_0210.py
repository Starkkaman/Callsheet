"""0.21.0 the voice assistant's read-only API (voiceapi.py, apitokens.py, docs/voice-api.md; approved 2026-11-13).

Every endpoint over the seeded Today store (today_fixtures, Wed 2026-11-11 12:20 in the fixtures' zone), through the real
listener on a loopback port: auth (missing, bad, revoked, scope), the rate limit, ETag/304, unanswered, people's
channels, the brief, the 405s with no state change, the counts-only audit log and the listener being off by default.
No network beyond 127.0.0.1."""
import hashlib
import http.client
import json
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path[:0] = [str(APP), str(ROOT / "tests")]
import api  # noqa: E402
import apitokens  # noqa: E402
import server  # noqa: E402
import today_fixtures as F  # noqa: E402
import voiceapi  # noqa: E402

ALL = list(apitokens.SCOPES)


def seed_people(app):
    """Dana Kim (email only in People, Signal and texts found by name), Marco Bellini (the owner's mail address) and a VIP."""
    db, now = app.store.db, app.clock()
    with app.store.lock:
        for pid, name, vip in (("p_dana", "Dana Kim", 0), ("p_marco", "Marco Bellini", 1), ("p_priya", "Priya Shah", 0)):
            db.execute("INSERT INTO people (id, name, state, vip, created, updated) VALUES (?,?,?,?,?,?)",
                       (pid, name, "confirmed", vip, now, now))
        for pid, kind, value, label in (("p_dana", "email", "dana@kim.example", "dana@kim.example"),
                                        ("p_marco", "email", "marco@bellini.example", "marco@bellini.example"),
                                        ("p_priya", "email", "priya@lumen.example", "priya@lumen.example")):
            db.execute("INSERT INTO person_handles (person, kind, value, label, source, added) VALUES (?,?,?,?,?,?)",
                       (pid, kind, value, label, "mail", now))
    app.people._touch()  # noqa: SLF001


class VoiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.now = [F.ts(F.STAGES["1220"])]
        cls.app = api.App(cls.tmp.name, clock=lambda: cls.now[0])
        cls.app.store.update_settings({"timezone": F.TZ})
        cls.ids = F.seed(cls.app.messages, cls.app.extractor, cls.now, cls.app.today, "1220")
        seed_people(cls.app)
        cls.app.brief2.model = lambda: ""        # the steps' words from Callsheet's own templates (no model)
        cls.app.handle("POST", "talk/start", {})
        cls.app.brief2.thread.join(20)
        cls.app.handle("PATCH", "voice", {"enabled": True})
        cls.app.voice.attach("127.0.0.1", 0)
        cls.port = cls.app.voice.bound_port()

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.now[0] = F.ts(F.STAGES["1220"])
        r = self.app.handle("POST", "voice/tokens", {"name": "Test phone"})
        self.tok, self.tid = r["new"]["secret"], r["new"]["token"]["id"]
        self.addCleanup(self._revoke)

    def _revoke(self):
        try:
            self.app.voice.tokens.revoke(self.tid)
        except LookupError:
            pass

    def req(self, path, method="GET", token=True, headers=None, body=None):
        h = {"Authorization": "Bearer " + (self.tok if token is True else token)} if token else {}
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, (json.loads(raw) if raw else None)

    def ok(self, path, **kw):
        st, hd, body = self.req(path, **kw)
        self.assertEqual(st, 200, body)
        self.assertIs(body["untrusted"], True)
        return body, hd

    # --- off by default ----------------------------------------------------------------------------------------------
    def test_listener_off_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            app = api.App(d)
            try:
                v = app.handle("GET", "voice", None)
                self.assertEqual((v["settings"]["enabled"], v["running"], v["tokens"], v["port"]), (False, False, [], 8100))
                app.voice.attach("127.0.0.1", 0)       # what server.main does: still nothing listening while off
                self.assertIsNone(app.voice.httpd)
                httpd = server.make_server("127.0.0.1", 0, data=d, app=app)   # the Ingress server never starts it
                self.assertIsNone(app.voice.httpd)
                httpd.server_close()
                app.handle("PATCH", "voice", {"enabled": True})
                self.assertIsNotNone(app.voice.httpd)
                app.handle("PATCH", "voice", {"enabled": False})
                self.assertIsNone(app.voice.httpd)
            finally:
                app.close()
        self.assertIn("--voice-port", (APP / "server.py").read_text())

    def test_config_yaml_port_not_exposed(self):
        cfg = (ROOT / "callsheet" / "config.yaml").read_text()
        self.assertRegex(cfg, r"\nports:\n  8100/tcp: null\n")
        self.assertRegex(cfg, r"\nports_description:\n  8100/tcp: \".+\"")

    # --- auth --------------------------------------------------------------------------------------------------------
    def test_auth_missing_bad_revoked_scope(self):
        st, hd, body = self.req("/v1/status", token=False)
        self.assertEqual(st, 401)
        self.assertIn("token", body["error"])
        self.assertIn("bearer", hd["www-authenticate"].lower())
        self.assertEqual(self.req("/v1/status", token="csva_" + "x" * 43)[0], 401)
        self.assertEqual(self.req("/v1/status", headers={"Authorization": "Basic " + self.tok})[0], 401)
        self.assertEqual(self.req("/v1/status", token=self.tok[:-1] + ("A" if self.tok[-1] != "A" else "B"))[0], 401)
        self.assertEqual(self.req("/v1/status")[0], 200)
        self.app.handle("DELETE", "voice/tokens/" + self.tid, None)
        st, _, body = self.req("/v1/status")
        self.assertEqual(st, 401)
        # scope: a token for the calendar only
        r = self.app.handle("POST", "voice/tokens", {"name": "Calendar only", "scopes": ["calendar:read"]})
        cal = r["new"]["secret"]
        self.addCleanup(lambda: self.app.voice.tokens.revoke(r["new"]["token"]["id"]))
        self.assertEqual(self.req("/v1/calendar", token=cal)[0], 200)
        for path, scope in (("/v1/search_messages", "messages:read"), ("/v1/ledger", "ledger:read"),
                            ("/v1/people?q=dana", "people:read"), ("/v1/brief/today", "brief:read"),
                            ("/v1/status", "status:read")):
            st, _, body = self.req(path, token=cal)
            self.assertEqual(st, 403, path)
            self.assertIn(scope, body["error"])

    def test_tokens_stored_hashed_and_shown_once(self):
        r = self.app.handle("POST", "voice/tokens", {"name": "Hash check", "scopes": ["status:read"]})
        secret, tid = r["new"]["secret"], r["new"]["token"]["id"]
        self.addCleanup(lambda: self.app.voice.tokens.revoke(tid))
        self.assertTrue(secret.startswith(apitokens.TOKEN_PREFIX) and len(secret) > 40)
        self.assertTrue(r["new"]["qr_svg"].startswith("<svg"))
        view = json.dumps(self.app.handle("GET", "voice", None))
        self.assertNotIn(secret, view)
        self.assertNotIn("hash", view)
        with self.app.store.lock:
            row = self.app.store.db.execute("SELECT * FROM voice_tokens WHERE id=?", (tid,)).fetchone()
        self.assertNotIn(secret, json.dumps(dict(row)))
        self.assertNotEqual(row["hash"], hashlib.sha256(secret.encode()).hexdigest())   # salted
        self.assertEqual(row["hash"], self.app.voice.tokens._hash(secret))  # noqa: SLF001
        dump = "\n".join(sqlite3.connect(str(pathlib.Path(self.tmp.name) / "callsheet.db")).iterdump())
        self.assertNotIn(secret, dump)
        self.assertIn("compare_digest", (APP / "apitokens.py").read_text())
        for bad in ({"name": ""}, {"name": "x" * 41}, {"name": "a", "scopes": []}, {"name": "a", "scopes": ["send"]},
                    {"name": "a", "extra": 1}):
            with self.assertRaises(api.InputError):
                self.app.handle("POST", "voice/tokens", bad)

    def test_last_used(self):
        self.assertIsNone(next(t for t in self.app.voice.tokens.list() if t["id"] == self.tid)["last_used"])
        self.ok("/v1/status")
        self.assertEqual(next(t for t in self.app.voice.tokens.list() if t["id"] == self.tid)["last_used"], self.now[0])

    # --- network rules -----------------------------------------------------------------------------------------------
    def test_host_client_and_proxy_checks(self):
        for host in ("127.0.0.1:%d" % self.port, "192.168.1.20:8100", "[fd7a:115c:a1e0::1]:8100", "nas",
                     "homeassistant.local:8100", "nas.tail1234.ts.net", "localhost"):
            self.assertEqual(self.req("/v1/status", headers={"Host": host})[0], 200, host)
        for host in ("evil.example.com", "callsheet-api.northwind.example", "nas.tail1234.ts.net.evil.com", "a b"):
            st, _, body = self.req("/v1/status", headers={"Host": host})
            self.assertEqual(st, 403, host)
        self.app.handle("PATCH", "voice", {"hosts": "nas.example.home, other.example.org"})
        self.addCleanup(lambda: self.app.handle("PATCH", "voice", {"hosts": []}))
        self.assertEqual(self.req("/v1/status", headers={"Host": "other.example.org:8100"})[0], 200)
        with self.assertRaises(api.InputError):
            self.app.handle("PATCH", "voice", {"hosts": ["not a host"]})
        st, _, body = self.req("/v1/status", headers={"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(st, 403)
        self.assertIn("Cloudflare", body["error"])
        for ip, want in (("127.0.0.1", True), ("10.0.0.5", True), ("192.168.1.9", True), ("172.30.32.2", True),
                         ("100.101.102.103", True), ("fd7a:115c:a1e0::5", True), ("::ffff:192.168.1.9", True),
                         ("8.8.8.8", False), ("93.184.216.34", False), ("2606:4700::1", False), ("junk", False)):
            self.assertEqual(apitokens.client_allowed(ip), want, ip)

    def test_no_cors(self):
        st, hd, _ = self.req("/v1/status", headers={"Origin": "https://evil.example.com"})
        self.assertEqual(st, 200)
        self.assertFalse([k for k in hd if k.startswith("access-control")])
        self.assertEqual(hd["cache-control"], "no-store")
        self.assertEqual(self.req("/v1/status", method="OPTIONS", headers={
            "Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"})[0], 405)

    # --- rate limit --------------------------------------------------------------------------------------------------
    def test_rate_limit(self):
        for _ in range(apitokens.RATE_N):
            self.assertEqual(self.req("/v1/status")[0], 200)
        st, hd, body = self.req("/v1/status")
        self.assertEqual(st, 429)
        self.assertIn("a minute", body["error"])
        self.assertTrue(1 <= int(hd["retry-after"]) <= 60)
        r = self.app.handle("POST", "voice/tokens", {"name": "Other device"})     # per token
        self.addCleanup(lambda: self.app.voice.tokens.revoke(r["new"]["token"]["id"]))
        self.assertEqual(self.req("/v1/status", token=r["new"]["secret"])[0], 200)
        self.now[0] += 61
        self.assertEqual(self.req("/v1/status")[0], 200)

    # --- endpoints ---------------------------------------------------------------------------------------------------
    def test_search_messages(self):
        body, hd = self.ok("/v1/search_messages?q=shoot")
        self.assertTrue(body["results"])
        r = body["results"][0]
        for k in ("id", "source", "account", "thread_id", "direction", "from", "to", "ts", "subject", "snippet", "ref"):
            self.assertIn(k, r)
        self.assertTrue(r["untrusted"])
        self.assertTrue(r["id"].startswith("m") and r["ref"]["id"] == r["id"])
        self.assertIn("shoot", (r["subject"] + r["snippet"]).lower())
        self.assertTrue(hd["etag"])
        newest, _ = self.ok("/v1/search_messages?limit=3")             # without q: newest first
        ts = [x["ts_unix"] for x in newest["results"]]
        self.assertEqual((len(ts), ts), (3, sorted(ts, reverse=True)))
        sig, _ = self.ok("/v1/search_messages?source=signal&direction=in&limit=50")
        self.assertTrue(sig["results"] and all(x["source"] == "signal" and x["direction"] == "in" for x in sig["results"]))
        dana, _ = self.ok("/v1/search_messages?person=Dana%20Kim&limit=50")
        self.assertTrue(dana["results"])
        self.assertTrue(all("Dana" in json.dumps(x) for x in dana["results"]))
        day, _ = self.ok("/v1/search_messages?since=2026-11-11&until=2026-11-11&limit=50")
        self.assertTrue(day["results"] and all(x["ts"].startswith("2026-11-11") for x in day["results"]))
        cal, _ = self.ok("/v1/search_messages?source=calendar&q=color")
        self.assertEqual([x["source"] for x in cal["results"]], ["calendar"])
        upd, _ = self.ok("/v1/search_messages?updated_since=%d" % (self.now[0] + 1))
        self.assertEqual(upd["results"], [])
        self.assertTrue(all(len(x["snippet"]) <= voiceapi.SNIPPET for x in newest["results"]))
        for bad in ("limit=51", "limit=0", "direction=sideways", "source=fax", "since=yesterday", "unanswered=maybe",
                    "q=" + "a" * 201, "nope=1"):
            st, _, body = self.req("/v1/search_messages?" + bad)
            self.assertEqual(st, 400, bad)
            self.assertTrue(body["error"])

    def test_unanswered(self):
        body, _ = self.ok("/v1/search_messages?unanswered=true&limit=50")
        ids = {r["ref"]["source_id"] for r in body["results"]}
        self.assertTrue(all(r["direction"] == "in" for r in body["results"]))
        self.assertIn("north-1", ids)                   # the W-9 request, never answered
        self.assertNotIn("marco-0", ids)                # Marco's note the owner answered ("Thanks! Glad it landed.")
        ms = self.app.messages
        for r in body["results"]:
            src, acc, th = voiceapi.parse_thread_id(r["thread_id"])
            later = ms._read("SELECT count(*) n FROM messages WHERE source=? AND account=? AND thread=? AND "  # noqa: SLF001
                             "direction='out' AND ts>?", (src, acc, th, r["ts_unix"]))[0]["n"]
            self.assertEqual(later, 0, r["subject"])

    def test_unanswered_wants_reply(self):
        """unanswered=true counts only messages to the owner that want a reply: no Gmail category tabs (promotions,
        social, forums, updates: where bulk and List-Unsubscribe mail lands), no automated or role senders, nothing
        from the owner's own accounts, nothing the ledger read as cleanup or as nothing to keep, no reports, and Slack
        channel posts only when they mention the owner or answer a thread the owner wrote in."""
        ms, now, db = self.app.messages, self.now[0], self.app.store.db
        with self.app.store.lock:
            db.execute("INSERT INTO slack (id, area_id, team_id, team_name, domain, user_id, user_name, token, "
                       "created_at) VALUES ('ws_t', 'a', 'T0T', 'Acme', 'acme', 'U0ME', 'alex', 'x', 0)")
            db.execute("INSERT INTO person_handles (person, kind, value, label, source, added) VALUES "
                       "('me', 'email', 'alex@other.example', 'alex@other.example', 'google', 0)")
        self.app.people._touch()  # noqa: SLF001
        ms.set_meta("slack_users:ws_t", {"at": now, "names": {"U0ME": "Alex Hale", "U0LENA": "Lena Ortiz"}})

        def undo():
            ms.write(lambda d: (d.execute("DELETE FROM messages WHERE msg_id LIKE 'wr-%'"),
                                d.execute("DELETE FROM meta WHERE key='slack_users:ws_t'")))
            with self.app.store.lock:
                db.execute("DELETE FROM slack WHERE id='ws_t'")
                db.execute("DELETE FROM person_handles WHERE person='me' AND value='alex@other.example'")
            self.app.people._touch()  # noqa: SLF001
        self.addCleanup(undo)
        n = [0]

        def put(mid, source, sender, text, labels=(), thread=None, direction="in", account="acct", subject=""):
            n[0] += 1
            ms.upsert_message({"source": source, "account": account, "msg_id": "wr-" + mid,
                               "thread": thread or "wr-t-" + mid, "direction": direction, "sender": sender,
                               "sender_name": sender, "recipients": [], "ts": now - 600 + n[0], "subject": subject,
                               "text": text, "labels": list(labels), "link": ""})
        want = {"wr-ask", "wr-dm", "wr-mention", "wr-inthread", "wr-signal"}
        put("ask", "gmail", "jo@client.example", "Can you send the call sheet?", ["INBOX"], subject="Call sheet?")
        put("promo", "gmail", "jo@shop.example", "40% off lenses", ["INBOX", "CATEGORY_PROMOTIONS"])
        put("update", "gmail", "jo@saas.example", "Your weekly report", ["CATEGORY_UPDATES"])
        put("forum", "gmail", "jo@list.example", "[crew-list] who has a dolly?", ["CATEGORY_FORUMS"])
        put("noreply", "gmail", "no-reply@bank.example", "Your statement is ready", ["INBOX"])
        put("news", "gmail", "newsletter@mag.example", "This week in film", ["INBOX"])
        put("mine", "gmail", "alex@other.example", "Note to self: book the van", ["INBOX"])
        put("report", "reports", "Weekly numbers", "Numbers are up")
        put("chan", "slack", "U0LENA", "Lunch is in the kitchen", ["channel"], thread="C9", account="ws_t")
        put("here", "slack", "U0LENA", "@here standup moved to 10", ["channel"], thread="C9", account="ws_t")
        put("other", "slack", "U0LENA", "@Lena Ortiz, ping @Alexander about it", ["private"], thread="C9",
            account="ws_t")
        put("mention", "slack", "U0LENA", "@Alex Hale can you check the cut?", ["channel"], thread="C9",
            account="ws_t")
        put("root", "slack", "U0ME", "Who has the hard drive?", ["channel"], thread="C9:1.0", direction="out",
            account="ws_t")
        put("inthread", "slack", "U0LENA", "I do, want it today?", ["channel"], thread="C9:1.0", account="ws_t")
        put("chanthread", "slack", "U0LENA", "Anyone for lunch?", ["channel"], thread="C9:2.0", account="ws_t")
        put("dm", "slack", "U0LENA", "Free for a call?", ["dm"], thread="D1", account="ws_t")
        put("signal", "signal", "+15550001111", "Running late, ok?", ["dm"])
        body, _ = self.ok("/v1/search_messages?unanswered=true&limit=50")
        ids = {r["ref"]["source_id"] for r in body["results"]}
        self.assertEqual({i for i in ids if i.startswith("wr-")}, want)
        self.assertIn("north-1", ids)                   # the fixture's W-9 ask stays
        self.assertFalse({i for i in ids if i.startswith("c-")})   # the ledger read these as cleanup (newsletters ...)
        self.assertTrue(all(r["direction"] == "in" for r in body["results"]))
        body, _ = self.ok("/v1/search_messages?unanswered=true&source=slack&limit=50")
        self.assertEqual({r["ref"]["source_id"] for r in body["results"]} & {i for i in want if i.startswith("wr-")},
                         {"wr-dm", "wr-mention", "wr-inthread"})
        body, _ = self.ok("/v1/search_messages?unanswered=true&q=lunch&limit=50")
        self.assertEqual([r for r in body["results"] if r["ref"]["source_id"].startswith("wr-")], [])
        body, _ = self.ok("/v1/search_messages?unanswered=true&limit=2")   # the limit still holds after the filter
        self.assertEqual(len(body["results"]), 2)
        body, _ = self.ok("/v1/search_messages?q=lunch&limit=50")          # without unanswered, nothing is hidden
        self.assertTrue({"wr-chan", "wr-chanthread"} <= {r["ref"]["source_id"] for r in body["results"]})

    def test_etag_304(self):
        for path in ("/v1/search_messages?unanswered=true", "/v1/ledger?kind=needs_you"):
            st, hd, body = self.req(path)
            self.assertEqual(st, 200)
            tag = hd["etag"]
            st, hd2, body2 = self.req(path, headers={"If-None-Match": tag})
            self.assertEqual((st, body2, hd2["etag"]), (304, None, tag))
            self.assertEqual(self.req(path, headers={"If-None-Match": "W/" + tag})[0], 304)
            self.assertEqual(self.req(path, headers={"If-None-Match": '"other"'})[0], 200)
        _, hd, _ = self.req("/v1/search_messages?unanswered=true")
        tag = hd["etag"]
        self.app.messages.upsert_message({"source": "gmail", "account": "acct", "msg_id": "etag-new", "thread": "t-etag",
                                          "direction": "in", "sender": "new@x.example", "sender_name": "New Person",
                                          "recipients": [], "ts": self.now[0] - 5, "subject": "Something new",
                                          "text": "Are you around?", "labels": [], "link": ""})
        st, hd, body = self.req("/v1/search_messages?unanswered=true", headers={"If-None-Match": tag})
        self.assertEqual(st, 200)
        self.assertNotEqual(hd["etag"], tag)
        self.assertEqual(body["results"][0]["subject"], "Something new")
        self.app.messages.write(lambda db: db.execute("DELETE FROM messages WHERE msg_id='etag-new'"))

    def test_thread(self):
        body, _ = self.ok("/v1/search_messages?q=Aster&source=gmail")
        tid = next(r["thread_id"] for r in body["results"] if r["ref"]["source_id"].startswith("marco"))
        t, _ = self.ok("/v1/threads/" + tid)
        self.assertEqual(t["thread_id"], tid)
        ts = [m["ts_unix"] for m in t["messages"]]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual({m["direction"] for m in t["messages"]}, {"in", "out"})
        self.assertTrue(any(p["handle"] == "marco@bellini.example" for p in t["participants"]))
        one, _ = self.ok("/v1/threads/" + tid + "?limit=1")
        self.assertEqual([m["id"] for m in one["messages"]], [t["messages"][-1]["id"]])
        before, _ = self.ok("/v1/threads/" + tid + "?before=%s" % int(t["messages"][-1]["ts_unix"]))
        self.assertEqual([m["id"] for m in before["messages"]], [m["id"] for m in t["messages"][:-1]])
        self.assertEqual(self.req("/v1/threads/" + voiceapi.thread_id("gmail", "acct", "no-such"))[0], 404)
        self.assertEqual(self.req("/v1/threads/tnot-base64!")[0], 404)
        # the text cap
        self.app.messages.upsert_message({"source": "gmail", "account": "acct", "msg_id": "long-1", "thread": "t-long",
                                          "direction": "in", "sender": "a@x.example", "sender_name": "A",
                                          "recipients": [], "ts": self.now[0] - 100, "subject": "Long",
                                          "text": "word " * 3000, "labels": [], "link": ""})
        lt, _ = self.ok("/v1/threads/" + voiceapi.thread_id("gmail", "acct", "t-long"))
        self.assertLessEqual(len(lt["messages"][0]["text"]), voiceapi.THREAD_TEXT)
        self.assertTrue(lt["messages"][0]["text_cut"])
        s, _ = self.ok("/v1/search_messages?q=Long&source=gmail")
        self.assertTrue(all(len(x["snippet"]) <= voiceapi.SNIPPET for x in s["results"]))
        self.app.messages.write(lambda db: db.execute("DELETE FROM messages WHERE msg_id='long-1'"))

    def test_ledger(self):
        allv, hd = self.ok("/v1/ledger")
        kinds = {r["kind"] for r in allv["results"]}
        self.assertTrue({"needs_you", "coming_up", "cleanup", "conflicts"} <= kinds, kinds)
        for kind in voiceapi.LEDGER_KINDS:
            body, _ = self.ok("/v1/ledger?kind=" + kind)
            self.assertTrue(all(r["kind"] == kind for r in body["results"]), kind)
        ny, _ = self.ok("/v1/ledger?kind=needs_you")
        priya = next(r for r in ny["results"] if r["who"] == "Priya Shah")
        for k in ("id", "type", "subtype", "who", "what", "when", "status", "sources", "urgency"):
            self.assertIn(k, priya)
        self.assertTrue(set(priya["when"]) >= {"start", "end", "tz", "all_day", "phrase"})
        self.assertEqual(priya["when"]["start_date"], "2026-12-08")
        self.assertTrue(priya["sources"] and all(s.startswith("m") for s in priya["sources"]))
        fu, _ = self.ok("/v1/ledger?kind=followups")
        self.assertTrue(all(r["type"] == "followup" for r in fu["results"]))
        conf, _ = self.ok("/v1/ledger?kind=conflicts")
        self.assertTrue(conf["results"])
        self.assertTrue(all(len(c["sides"]) == 2 for c in conf["results"]))
        two, _ = self.ok("/v1/ledger?limit=2")
        self.assertEqual(len(two["results"]), 2)
        who, _ = self.ok("/v1/ledger?person=Priya%20Shah")
        self.assertTrue(who["results"] and all("Priya" in json.dumps(r) for r in who["results"]))
        later, _ = self.ok("/v1/ledger?updated_since=%d" % (self.now[0] + 1))
        self.assertEqual(later["results"], [])
        dec, _ = self.ok("/v1/ledger?kind=needs_you&since=2026-12-01&until=2026-12-31")
        self.assertIn(priya["id"], [r["id"] for r in dec["results"]])
        self.assertEqual(self.req("/v1/ledger?kind=everything")[0], 400)
        # one item, with its sources' snippets and the other side of a clash
        one, _ = self.ok("/v1/ledger/" + priya["id"])
        self.assertEqual(one["id"], priya["id"])
        self.assertEqual([m["id"] for m in one["source_messages"]], priya["sources"])
        self.assertTrue(all(len(m["snippet"]) <= voiceapi.SNIPPET for m in one["source_messages"]))
        side = conf["results"][0]["sides"][0]["id"]
        item, _ = self.ok("/v1/ledger/" + side)
        self.assertTrue(any(x.get("type") == "conflict" for x in item["linked"]))
        c, _ = self.ok("/v1/ledger/" + conf["results"][0]["id"])
        self.assertEqual(c["type"], "conflict")
        self.assertTrue(c["linked"] and all("source_messages" in x for x in c["linked"]))
        self.assertEqual(self.req("/v1/ledger/l999999")[0], 404)
        self.assertEqual(self.req("/v1/ledger/c999999")[0], 404)
        self.assertEqual(self.req("/v1/ledger/x1")[0], 404)

    def test_calendar(self):
        body, _ = self.ok("/v1/calendar?from=2026-11-11&to=2026-11-20")
        self.assertTrue(body["results"])
        e = body["results"][0]
        for k in ("id", "calendar", "start", "end", "all_day", "title", "location", "status", "ref"):
            self.assertIn(k, e)
        self.assertTrue(e["id"].startswith("e"))
        today, _ = self.ok("/v1/calendar")                       # default: today and the next six days
        self.assertTrue(today["from"].startswith("2026-11-11T00:00"))
        self.assertEqual(self.req("/v1/calendar?from=2026-11-01&to=2027-01-15")[0], 400)   # > 62 days
        self.assertEqual(self.req("/v1/calendar?from=2026-11-01&to=2027-01-01")[0], 200)   # 62 days
        self.assertEqual(self.req("/v1/calendar?from=2026-11-20&to=2026-11-10")[0], 400)

    def test_people_channels(self):
        body, _ = self.ok("/v1/people?q=dana")
        dana = body["results"][0]
        self.assertEqual((dana["id"], dana["name"]), ("p_dana", "Dana Kim"))
        chans = dana["channels"]
        sig = next(c for c in chans if c["source"] == "signal")
        self.assertEqual(sig["handle"], "+15550142")
        self.assertGreater(sig["count_90d"], 0)
        self.assertTrue(sig["last_used_ts"].startswith("2026-11-"))
        mail = next(c for c in chans if c["source"] == "gmail")
        self.assertEqual((mail["handle"], mail["count_90d"], mail["last_used_ts"]), ("dana@kim.example", 0, None))
        self.assertEqual(chans[0]["source"], "signal")           # the channel the owner really uses first
        for c in chans:
            self.assertEqual(set(c) - {"last_used_unix"}, {"source", "handle", "display", "last_used_ts", "count_90d"})
        by_mail, _ = self.ok("/v1/people?q=marco@bellini.example")
        self.assertEqual([p["id"] for p in by_mail["results"]], ["p_marco"])
        self.assertTrue(by_mail["results"][0]["vip"])
        self.assertGreater(by_mail["results"][0]["channels"][0]["count_90d"], 0)
        by_phone, _ = self.ok("/v1/people?q=%2B1%20555%200142")
        self.assertEqual([p["id"] for p in by_phone["results"]], ["p_dana"])
        sam, _ = self.ok("/v1/people?q=sam")                    # not in People: found by sender, id null
        self.assertEqual((sam["results"][0]["id"], sam["results"][0]["name"]), (None, "Sam Okafor"))
        self.assertEqual({c["source"] for c in sam["results"][0]["channels"]}, {"signal", "sms"})
        everyone, _ = self.ok("/v1/people")
        self.assertEqual(everyone["results"][0]["id"], "p_marco")  # VIPs first
        self.assertEqual(self.req("/v1/people?limit=21")[0], 400)

    def test_brief_today(self):
        body, _ = self.ok("/v1/brief/today")
        self.assertEqual(body["date"], "2026-11-11")
        self.assertTrue(body["steps"])
        for s in body["steps"]:
            self.assertTrue(s["id"] and s["title"])
            self.assertIsInstance(s["visual"], list)
        ready = [s for s in body["steps"] if s["state"] == "ready"]
        self.assertTrue(ready and all(s["text"] for s in ready))
        self.assertTrue(any(s["visual"] for s in ready))
        self.now[0] += 86400                                    # tomorrow: not written yet
        tomorrow, _ = self.ok("/v1/brief/today")
        self.assertEqual((tomorrow["date"], tomorrow["state"], tomorrow["steps"]), ("2026-11-12", "not_written", []))

    def test_status(self):
        body, _ = self.ok("/v1/status")
        self.assertIn("sources", body)
        self.assertIn("freshness", body)
        self.assertEqual(body["store"]["messages"], self.app.messages.stats()["messages"])

    def test_503_when_store_is_down(self):
        ms, self.app.messages = self.app.messages, None
        try:
            st, _, body = self.req("/v1/search_messages")
        finally:
            self.app.messages = ms
        self.assertEqual(st, 503)
        self.assertIn("isn't available", body["error"])

    def test_unknown_path(self):
        self.assertEqual(self.req("/v1/send_message")[0], 404)
        self.assertEqual(self.req("/v1/send_message", token=False)[0], 401)
        self.assertEqual(self.req("/api/state")[0], 404)       # the Ingress API isn't here

    # --- read-only ---------------------------------------------------------------------------------------------------
    def _snapshot(self):
        out = []
        for name in ("callsheet.db", "messages.db"):
            db = sqlite3.connect(str(pathlib.Path(self.tmp.name) / name))
            out.append("\n".join(db.iterdump()))
            db.close()
        return out

    def test_every_other_method_is_405_and_changes_nothing(self):
        self.ok("/v1/status")                                   # last-used written before the snapshot
        before = self._snapshot()
        paths = ["/v1/search_messages", "/v1/threads/" + voiceapi.thread_id("gmail", "acct", "mail-marco"),
                 "/v1/ledger", "/v1/ledger/l1", "/v1/calendar", "/v1/people", "/v1/brief/today", "/v1/status",
                 "/v1/ledger/l1/done", "/v1/send", "/"]
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT", "MKCOL", "PROPFIND"):
            for path in paths:
                for token in (True, False):
                    st, hd, body = self.req(path, method=method, token=token,
                                            body=json.dumps({"status": "done", "text": "hi"}),
                                            headers={"Content-Type": "application/json", "X-Callsheet": "1"})
                    self.assertEqual(st, 405, (method, path))
                    self.assertEqual(hd.get("allow"), "GET")
                    if method != "HEAD":
                        self.assertIn("GET", body["error"])
        self.assertEqual(self._snapshot(), before)
        src = (APP / "voiceapi.py").read_text()
        for word in ("upsert", "set_status", "update_settings", "INSERT", "UPDATE", "DELETE FROM", "ms.write(",
                     "messages.write(", "_set_meta"):
            self.assertNotIn(word, src, word)   # the only writes are apitokens' audit rows and last-used time

    def test_gets_change_nothing_but_the_audit_log(self):
        self.ok("/v1/status")
        strip = lambda dump: [ln for ln in dump.splitlines() if "voice_audit" not in ln and "voice_tokens" not in ln]  # noqa: E731
        before = [strip(d) for d in self._snapshot()]
        tid = voiceapi.thread_id("gmail", "acct", "mail-marco")
        for path in ("/v1/search_messages?q=shoot", "/v1/search_messages?unanswered=true", "/v1/threads/" + tid,
                     "/v1/ledger", "/v1/ledger/l1", "/v1/ledger/c1", "/v1/calendar", "/v1/people?q=dana",
                     "/v1/people?q=sam", "/v1/brief/today", "/v1/status"):
            self.assertIn(self.req(path)[0], (200, 404), path)
        self.assertEqual([strip(d) for d in self._snapshot()], before)

    # --- the audit log -----------------------------------------------------------------------------------------------
    def test_audit_log_has_counts_only(self):
        secret_q = "W-9 invoice"
        tid = voiceapi.thread_id("gmail", "acct", "mail-north")
        self.ok("/v1/search_messages?q=" + secret_q.replace(" ", "%20") + "&person=Northlight")
        self.ok("/v1/threads/" + tid)
        self.ok("/v1/ledger/l1")
        self.req("/v1/status", token=False)
        self.req("/v1/status", token="csva_wrongwrongwrongwrong")
        rows = self.app.voice.tokens.audit_rows(10)
        self.assertEqual([r["endpoint"] for r in rows[:5]],
                         ["/v1/status", "/v1/status", "/v1/ledger/{id}", "/v1/threads/{id}", "/v1/search_messages"])
        self.assertEqual([r["status"] for r in rows[:5]], [401, 401, 200, 200, 200])
        self.assertEqual(rows[2]["count"], 1)
        self.assertGreaterEqual(rows[4]["count"], 1)
        self.assertEqual((rows[0]["name"], rows[2]["name"]), ("", "Test phone"))
        with self.app.store.lock:
            cols = [r[1] for r in self.app.store.db.execute("PRAGMA table_info(voice_audit)")]
            dump = json.dumps([dict(r) for r in self.app.store.db.execute("SELECT * FROM voice_audit")])
        self.assertEqual(cols, ["id", "at", "token", "name", "endpoint", "status", "count"])
        texts = [m["text"] for m in self.app.messages._read("SELECT text FROM messages")]  # noqa: SLF001
        for bad in ["W-9", "invoice", "Northlight", tid, "l1\"", self.tok, "wrongwrong"] + \
                [t[:20] for t in texts if len(t) >= 20]:
            self.assertNotIn(bad, dump, bad)
        # older than 90 days: removed
        self.app.voice.tokens._pruned = 0  # noqa: SLF001
        self.now[0] += 91 * 86400
        self.app.voice.tokens.audit(None, "/v1/status", 401)
        self.assertEqual(len(self.app.voice.tokens.audit_rows(1000)), 1)

    def test_size_cap(self):
        big = {"results": [{"snippet": "x" * 1000} for _ in range(2000)]}
        fit = voiceapi.Voice._fit(big)   # noqa: SLF001
        self.assertTrue(fit["truncated"])
        self.assertLessEqual(len(json.dumps(fit).encode()), voiceapi.MAX_BODY)
        self.assertTrue(fit["results"])

    def test_setup_view_and_page(self):
        v = self.app.handle("GET", "voice", None)
        self.assertEqual(v["scopes"], ALL)
        self.assertEqual((v["rate_per_minute"], v["audit_days"]), (60, 90))
        js = (APP / "static" / "app.js").read_text()
        self.assertIn("Read-only, unless you let a device dismiss, snooze and mark done. Reached over your home network or "
                      "Tailscale; never put it behind Cloudflare.", js)
        self.assertIn("voiceSection()", js)
        self.assertNotRegex(js[js.index("function voiceSection"):js.index("function otherSources")], r"innerHTML")
        with self.assertRaises(api.InputError):
            self.app.handle("PATCH", "voice", {"enabled": "yes"})
        with self.assertRaises(api.InputError):
            self.app.handle("PATCH", "voice", {"port": 9000})
        with self.assertRaises(api.NotFound):
            self.app.handle("DELETE", "voice/tokens/vt_000000000000", None)


if __name__ == "__main__":
    unittest.main()
