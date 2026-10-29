"""API and Google flow against the fake Google in fake_google.py. Stdlib only, no network."""
import datetime as dt
import http.client
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import fake_google  # noqa: E402
import server  # noqa: E402
from fake_google import CLIENT_ID, CLIENT_SECRET, event, thread  # noqa: E402

UTC = dt.timezone.utc


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = fake_google.FakeGoogle()
        env = mock.patch.dict(os.environ, self.fake.env())
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, data=self.tmp.name)
        self.app = self.httpd.app
        self.logs = []
        self.app.syncer.log = self.logs.append
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.fake.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, header=True, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        headers = {"X-Callsheet": "1"} if header else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, (json.loads(text) if text else None), text

    def ok(self, method, path, body=None):
        status, data, text = self.call(method, path, body)
        self.assertEqual(status, 200, text)
        return data

    def connect(self, email, area_id="personal"):
        if not self.app.store.google_client():
            self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        if email not in self.fake.users:
            self.fake.add_user(email)
        flow = self.ok("POST", "/api/accounts/start", {"area_id": area_id})
        pasted = self.fake.approve(flow["auth_url"], email)
        return self.ok("POST", "/api/accounts/finish", {"flow_id": flow["flow_id"], "pasted": pasted})


class StateAndSettingsTest(Base):
    def test_defaults(self):
        st = self.ok("GET", "/api/state")
        self.assertEqual(st["google_client"], {"configured": False, "client_id_hint": None})
        self.assertEqual(len(st["areas"]), 1)
        p = st["areas"][0]
        self.assertEqual((p["id"], p["name"], p["colour"], p["fixed"]), ("personal", "Personal", 1, True))
        self.assertEqual(p["reads"], {"calendar": True, "mail": True, "chat": True, "tasks": True,
                                      "writer": True})
        self.assertEqual(p["accounts"], [])
        s = st["settings"]
        self.assertEqual(s["workdays"], ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(s["reply_age_days"], 2)
        self.assertEqual([(c["id"], c["time"], c["enabled"]) for c in s["call_times"]],
                         [("morning", "06:30", True), ("midday", "12:30", True), ("afternoon", "17:00", True)])
        self.assertEqual((s["skip_checkins_days_off"], s["look"], s["mode"]), (True, "bold", "auto"))
        self.assertEqual(st["last_sync"], {"at": None, "ok": None, "errors": []})
        self.assertIs(st["setup_done"], False)
        self.assertEqual(st["vips"], [])

    def test_db_file_is_private(self):
        mode = stat.S_IMODE(os.stat(pathlib.Path(self.tmp.name) / "callsheet.db").st_mode)
        self.assertEqual(mode, 0o600)

    def test_csrf_header_size_and_json(self):
        status, data, _ = self.call("PATCH", "/api/settings", {"look": "clean"}, header=False)
        self.assertEqual(status, 403)
        self.assertIn("error", data)
        status, data, _ = self.call("PATCH", "/api/settings", raw=b"{not json")
        self.assertEqual(status, 400)
        status, data, _ = self.call("POST", "/api/vips", raw=b"{" + b" " * (64 * 1024) + b"}")
        self.assertEqual(status, 413)
        status, data, _ = self.call("POST", "/api/nothing", {})
        self.assertEqual(status, 404)
        status, data, _ = self.call("GET", "/api/settings")
        self.assertEqual(status, 405)

    def test_settings_patch(self):
        s = self.ok("PATCH", "/api/settings", {"timezone": "Europe/Amsterdam", "workdays": ["fri", "mon"],
                                               "call_times": [{"id": "midday", "time": "13:15", "enabled": False}],
                                               "look": "easy", "mode": "dark", "name": "Sam", "setup_done": True})
        self.assertEqual(s["workdays"], ["mon", "fri"])
        self.assertEqual(s["call_times"][1], {"id": "midday", "label": "Midday check-in", "time": "13:15",
                                              "enabled": False})
        st = self.ok("GET", "/api/state")
        self.assertEqual(st["settings"]["timezone"], "Europe/Amsterdam")
        self.assertIs(st["setup_done"], True)
        for bad in ({"timezone": "Mars/Base"}, {"look": "neon"}, {"call_times": [{"id": "x"}]},
                    {"call_times": [{"id": "morning", "time": "25:00"}]}, {"reply_age_days": "2"}, {"bogus": 1}):
            status, data, _ = self.call("PATCH", "/api/settings", bad)
            self.assertEqual(status, 400, bad)
            self.assertIsInstance(data["error"], str)

    def test_areas_and_vips(self):
        a = self.ok("POST", "/api/areas", {"name": "CO. A", "colour": 2})
        self.assertEqual((a["id"], a["colour"], a["fixed"], a["accounts"]), ("co-a", 2, False, []))
        a = self.ok("PATCH", "/api/areas/co-a", {"name": "Acme", "reads": {"chat": False}})
        self.assertEqual((a["name"], a["reads"]["chat"], a["reads"]["mail"]), ("Acme", False, True))
        self.assertEqual(self.call("POST", "/api/areas", {"name": "X", "colour": 5})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/areas/personal")[0], 400)
        self.assertEqual(self.call("PATCH", "/api/areas/nope", {"name": "Y"})[0], 404)
        v = self.ok("POST", "/api/vips", {"name": "Priya", "email": "Priya@Acme.example", "area_id": "co-a"})
        self.assertEqual(v["email"], "priya@acme.example")
        self.assertEqual(self.call("POST", "/api/vips", {"name": "Q", "email": "nope", "area_id": "co-a"})[0], 400)
        self.ok("DELETE", "/api/areas/co-a")
        st = self.ok("GET", "/api/state")
        self.assertEqual([x["id"] for x in st["areas"]], ["personal"])
        self.assertEqual(st["vips"][0]["area_id"], "personal")  # VIPs move to Personal
        self.ok("DELETE", "/api/vips/" + v["id"])
        self.assertEqual(self.call("DELETE", "/api/vips/" + v["id"])[0], 404)

    def test_google_client_never_returned(self):
        status, _, _ = self.call("POST", "/api/google-client", {"client_id": "abc", "client_secret": "x"})
        self.assertEqual(status, 400)
        c = self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.assertEqual(c, {"configured": True, "client_id_hint": "…lient9"})
        _, _, text = self.call("GET", "/api/state")
        self.assertNotIn(CLIENT_SECRET, text)
        self.assertNotIn(CLIENT_ID, text)
        self.assertEqual(self.ok("DELETE", "/api/google-client")["configured"], False)

    def test_brief_is_sample_without_accounts(self):
        b = self.ok("GET", "/api/brief")
        self.assertIs(b["sample"], True)
        self.assertEqual(b["sections"], {"schedule": "sample", "replies": "sample", "headsup": "sample",
                                         "projects": "sample"})
        self.assertEqual(b, dict(server.api.load_sample()))


class FlowTest(Base):
    def test_start_needs_client_and_area(self):
        self.assertEqual(self.call("POST", "/api/accounts/start", {"area_id": "personal"})[0], 400)
        self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.assertEqual(self.call("POST", "/api/accounts/start", {"area_id": "nope"})[0], 404)

    def test_auth_url(self):
        self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        flow = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        import urllib.parse
        u = urllib.parse.urlsplit(flow["auth_url"])
        self.assertEqual(u.path, "/auth/o/oauth2/v2/auth")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8765/")
        self.assertEqual(q["scope"], "openid email https://www.googleapis.com/auth/gmail.readonly "
                                     "https://www.googleapis.com/auth/calendar.readonly")
        for k, v in (("response_type", "code"), ("access_type", "offline"), ("prompt", "consent"),
                     ("include_granted_scopes", "true"), ("code_challenge_method", "S256"),
                     ("client_id", CLIENT_ID)):
            self.assertEqual(q[k], v, k)
        self.assertTrue(q["state"] and q["code_challenge"])

    def test_finish_full_url_and_reconnect(self):
        acc = self.connect("me@example.com")
        self.assertEqual(acc["email"], "me@example.com")
        self.assertEqual(acc["status"], "connected")
        self.assertEqual(acc["services"], {"calendar": True, "mail": True})
        self.assertEqual([(c["primary"], c["selected"]) for c in acc["calendars"]], [(True, True), (False, False)])
        # reconnecting the same email updates it instead of adding a second account
        again = self.connect("me@example.com")
        self.assertEqual(again["id"], acc["id"])
        st = self.ok("GET", "/api/state")
        self.assertEqual(len(st["areas"][0]["accounts"]), 1)
        self.assertIs(st["setup_done"], True)
        _, _, text = self.call("GET", "/api/state")
        for secret in list(self.fake.refresh_tokens) + list(self.fake.access_tokens) + [CLIENT_SECRET]:
            self.assertNotIn(secret, text)

    def test_finish_bare_code(self):
        self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user("me@example.com")
        flow = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        pasted = self.fake.approve(flow["auth_url"], "me@example.com")
        import urllib.parse
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)["code"][0]
        acc = self.ok("POST", "/api/accounts/finish", {"flow_id": flow["flow_id"], "pasted": "  " + code + "\n"})
        self.assertEqual(acc["email"], "me@example.com")

    def test_finish_errors(self):
        self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user("me@example.com")
        status, data, _ = self.call("POST", "/api/accounts/finish", {"flow_id": "nope", "pasted": "x"})
        self.assertEqual(status, 400)
        self.assertIn("Start again", data["error"])
        # state mismatch
        f1 = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        f2 = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        pasted2 = self.fake.approve(f2["auth_url"], "me@example.com")
        status, data, _ = self.call("POST", "/api/accounts/finish", {"flow_id": f1["flow_id"], "pasted": pasted2})
        self.assertEqual(status, 400)
        self.assertIn("different sign-in", data["error"])
        # missing scopes
        f3 = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        p3 = self.fake.approve(f3["auth_url"], "me@example.com",
                               scope="openid https://www.googleapis.com/auth/gmail.readonly")
        status, data, _ = self.call("POST", "/api/accounts/finish", {"flow_id": f3["flow_id"], "pasted": p3})
        self.assertEqual(data["error"], "Tick both Gmail and Calendar on Google's consent screen.")
        # code already used -> invalid_grant
        self.ok("POST", "/api/accounts/finish", {"flow_id": f2["flow_id"], "pasted": pasted2})
        f4 = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        p4 = self.fake.approve(f4["auth_url"], "me@example.com")
        self.fake.codes[p4.split("code=")[1].split("&")[0].replace("%2F", "/")]["used"] = True
        status, data, _ = self.call("POST", "/api/accounts/finish", {"flow_id": f4["flow_id"], "pasted": p4})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "That sign-in code was already used or expired. Start again.")
        # cancelled on Google's page
        f5 = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        status, data, _ = self.call("POST", "/api/accounts/finish", {
            "flow_id": f5["flow_id"], "pasted": "http://127.0.0.1:8765/?error=access_denied&state=x"})
        self.assertIn("cancelled", data["error"])

    def test_flow_expires(self):
        self.ok("POST", "/api/google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user("me@example.com")
        flow = self.ok("POST", "/api/accounts/start", {"area_id": "personal"})
        pasted = self.fake.approve(flow["auth_url"], "me@example.com")
        self.app.flows.clock = lambda: 10 ** 12
        status, data, _ = self.call("POST", "/api/accounts/finish", {"flow_id": flow["flow_id"], "pasted": pasted})
        self.assertEqual(status, 400)
        self.assertIn("expired", data["error"])


class SyncTest(Base):
    def seed(self, email):
        today = dt.datetime.now(UTC).date()
        at = lambda h, m=0: dt.datetime.combine(today, dt.time(h, m), tzinfo=UTC)  # noqa: E731
        now = dt.datetime.now(UTC)
        events = {email: [
            event("Stand-up", at(9, 30), at(9, 45), attendees=[{"email": email, "self": True,
                                                                "responseStatus": "accepted"},
                                                               {"email": "a@x.example"}]),
            event("Roadmap review", at(11), at(12), location="Room 4",
                  attendees=[{"email": email, "self": True, "responseStatus": "needsAction"}]),
            event("Vendor call", at(11, 30), at(12, 15)),
            event("Focus", at(11, 40), at(11, 50), transparency="transparent"),
            event("Old plan", at(13), at(14), status="cancelled"),
            event("Declined thing", at(15), at(16), attendees=[{"email": email, "self": True,
                                                                "responseStatus": "declined"}]),
            event("Holiday", today, today + dt.timedelta(days=1)),
        ]}
        threads = [
            thread("t1", "Priya N <priya@acme.example>", "Budget", "Can you sign off by Thursday?",
                   now - dt.timedelta(days=3), headers={"Message-ID": "<CAB+x/1=@mail.acme.example>"}),
            thread("t2", "Bob <bob@x.example>", "Lunch", "See you there", now - dt.timedelta(hours=5)),
            thread("t3", f"Me <{email}>", "Re: Plan", "Sure?", now - dt.timedelta(days=4)),
            thread("t4", "Boss <boss@acme.example>", "FYI", "Notes attached", now - dt.timedelta(hours=2, minutes=5)),
            thread("t5", "Shop <news@shop.example>", "Last chance?", "Sale ends today", now - dt.timedelta(days=5),
                   headers={"List-Unsubscribe": "<mailto:u@shop.example>"}),
            thread("t6", "Bank <no-reply@bank.example>", "Statement", "Pay by Friday?", now - dt.timedelta(days=4)),
            thread("t7", "Team <lead@acme.example>", "Plan?", "Thoughts?", now - dt.timedelta(days=4),
                   to="all@acme.example"),
        ]
        self.fake.add_user(email, events=events, threads=threads)

    def test_full_flow_live_brief_and_get_only(self):
        self.ok("PATCH", "/api/settings", {"timezone": "UTC"})
        self.ok("POST", "/api/vips", {"name": "Boss", "email": "boss@acme.example", "area_id": "personal"})
        self.seed("me@example.com")
        self.connect("me@example.com")
        last = self.ok("POST", "/api/sync")
        self.assertEqual((last["ok"], last["errors"]), (True, []))
        b = self.ok("GET", "/api/brief")
        self.assertIs(b["sample"], False)
        self.assertEqual(b["sections"], {"schedule": "live", "replies": "live", "headsup": "live",
                                         "projects": "sample"})
        self.assertEqual(b["areas"], [{"id": "personal", "name": "Personal", "colour": 1}])
        self.assertRegex(b["written_at"], r"^\d\d:\d\d$")
        self.assertRegex(b["next_checkin"], r"^\d\d:\d\d$")
        self.assertEqual(b["date"], dt.datetime.now(UTC).date().isoformat())
        rows = {r["title"]: r for r in b["schedule"]}
        self.assertEqual(list(rows), ["Stand-up", "Roadmap review", "Vendor call"])
        self.assertTrue(rows["Stand-up"]["first"])
        self.assertEqual(rows["Roadmap review"]["flag"]["text"], "Not accepted yet · Overlaps 11:30 Vendor call")
        self.assertEqual(rows["Roadmap review"]["note"], "Room 4")
        self.assertEqual(rows["Vendor call"]["flag"]["text"], "Overlaps 11:00 Roadmap review")
        self.assertTrue(rows["Vendor call"]["clash"])
        self.assertEqual(b["heads_up"], ["All day: Holiday"])
        who = [r["who"] for r in b["replies"]]
        self.assertEqual(who, ["Boss", "Priya N"])  # VIP first; not Bob, own, bulk, no-reply or unaddressed
        priya = b["replies"][1]
        self.assertEqual(priya["reasons"], ["Direct question", "Mentions a deadline", "Waiting 3 days"])
        self.assertEqual((priya["age"], priya["source"], priya["area"]), ("3d", "Mail", "personal"))
        self.assertTrue(priya["link"].startswith("https://mail.google.com/mail/u/?authuser=me@example.com"))
        self.assertTrue(b["replies"][0]["link"].startswith("https://mail.google.com/mail/u/?authuser=me@example.com"))
        self.assertEqual(b["replies"][0]["reasons"], ["VIP"])
        self.assertEqual(b["replies"][0]["age"], "2h")
        # Gmail/Calendar were only read (GET); POSTs only to token/revoke
        reqs = self.fake.requests
        self.assertTrue(self.fake.api_requests())
        for method, path, _ in reqs:
            if path.startswith(("/gmail/", "/calendar/")):
                self.assertEqual(method, "GET", path)
            else:
                self.assertIn((method, path), {("POST", "/token"), ("POST", "/revoke")})
        lists = [q for m, p, q in reqs if p == "/gmail/v1/users/me/threads"]
        self.assertEqual(lists[0]["q"], ["in:inbox -category:promotions -category:social -category:forums "
                                         "newer_than:14d"])
        self.assertEqual(lists[0]["maxResults"], ["50"])
        gets = [q for m, p, q in reqs if p.startswith("/gmail/v1/users/me/threads/")]
        self.assertEqual(gets[0]["format"], ["metadata"])
        self.assertEqual(gets[0]["metadataHeaders"], ["From", "To", "Cc", "Subject", "Date", "List-Id",
                                                      "List-Unsubscribe", "Precedence", "Auto-Submitted",
                                                      "Reply-To"])
        ev = [q for m, p, q in reqs if p.endswith("/events")]
        self.assertEqual(len(ev), 1)  # only the selected (primary) calendar
        self.assertEqual((ev[0]["singleEvents"], ev[0]["orderBy"]), (["true"], ["startTime"]))
        # one log line, counts only
        self.assertEqual(len(self.logs), 1)
        for word in ("me@example.com", "Priya", "Budget", "Stand-up", "ya29", "1//"):
            self.assertNotIn(word, self.logs[0])
        st = self.ok("GET", "/api/state")
        self.assertEqual(st["last_sync"]["ok"], True)

    def test_one_account_failing_does_not_stop_others(self):
        self.ok("PATCH", "/api/settings", {"timezone": "UTC"})
        self.ok("POST", "/api/areas", {"name": "Work", "colour": 2})
        self.seed("me@example.com")
        self.fake.add_user("work@acme.example")
        self.connect("me@example.com")
        self.connect("work@acme.example", "work")
        self.fake.users["work@acme.example"]["fail"] = 500
        last = self.ok("POST", "/api/sync")
        self.assertIs(last["ok"], False)
        self.assertEqual(len(last["errors"]), 1)
        self.assertTrue(last["errors"][0].startswith("Couldn't read work@acme.example: "))
        b = self.ok("GET", "/api/brief")
        self.assertTrue(b["schedule"])
        self.assertIn(last["errors"][0], b["heads_up"])
        st = self.ok("GET", "/api/state")
        work = st["areas"][1]["accounts"][0]
        self.assertEqual(work["status"], "error")
        for secret in list(self.fake.refresh_tokens) + list(self.fake.access_tokens):
            self.assertNotIn(secret, json.dumps(st) + json.dumps(b))

    def test_refresh_on_401_and_invalid_grant(self):
        self.seed("me@example.com")
        acc = self.connect("me@example.com")
        self.fake.expire_access_tokens()  # Google forgets the access token -> 401 -> refresh -> retry
        self.assertIs(self.ok("POST", "/api/sync")["ok"], True)
        self.assertTrue(any(p == "/token" for _, p, _ in self.fake.requests[-20:]))
        self.fake.refresh_tokens.clear()  # refresh token revoked at Google
        self.fake.expire_access_tokens()
        last = self.ok("POST", "/api/sync")
        self.assertIs(last["ok"], False)
        st = self.ok("GET", "/api/state")
        a = st["areas"][0]["accounts"][0]
        self.assertEqual(a["status"], "needs_reconnect")
        b = self.ok("GET", "/api/brief")
        self.assertTrue(any("me@example.com" in h for h in b["heads_up"]))
        # reconnecting fixes it
        again = self.connect("me@example.com")
        self.assertEqual((again["id"], again["status"]), (acc["id"], "connected"))

    def test_account_patch_and_delete_revokes(self):
        self.seed("me@example.com")
        acc = self.connect("me@example.com")
        a = self.ok("PATCH", f"/api/accounts/{acc['id']}", {"services": {"mail": False},
                                                            "calendars": ["family@group.calendar.google.com"]})
        self.assertEqual(a["services"], {"calendar": True, "mail": False})
        self.assertEqual([c["selected"] for c in a["calendars"]], [False, True])
        self.assertEqual(self.call("PATCH", f"/api/accounts/{acc['id']}", {"calendars": ["nope"]})[0], 400)
        self.ok("POST", "/api/sync")
        paths = [p for _, p, _ in self.fake.requests]
        self.assertNotIn("/gmail/v1/users/me/threads", paths)
        rt = next(iter(self.fake.refresh_tokens))
        self.ok("DELETE", f"/api/accounts/{acc['id']}")
        self.assertIn(rt, self.fake.revoked)
        self.assertIs(self.ok("GET", "/api/brief")["sample"], True)

    def test_delete_area_revokes_accounts(self):
        self.ok("POST", "/api/areas", {"name": "Work", "colour": 2})
        self.fake.add_user("work@acme.example")
        self.connect("work@acme.example", "work")
        rt = next(iter(self.fake.refresh_tokens))
        self.ok("DELETE", "/api/areas/work")
        self.assertIn(rt, self.fake.revoked)
        self.assertFalse(self.app.store.has_accounts())

    def test_sync_without_accounts(self):
        status, data, _ = self.call("POST", "/api/sync")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
