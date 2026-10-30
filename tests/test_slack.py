"""Slack (0.4.0) against the fake Slack in fake_slack.py: read-only allowlist, manifest, connect, the needs-a-reply
rules, Heads up, the ignore list, VIPs, rate limits, suppression and the writer input. Stdlib only, no network."""
import json
import os
import pathlib
import sys
import time
import unittest
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import fake_slack  # noqa: E402
import rules  # noqa: E402
import slack  # noqa: E402
from fake_slack import ME, TOKEN  # noqa: E402
from test_api_google import Base  # noqa: E402

H = 3600


class AllowlistTest(unittest.TestCase):
    def test_read_methods_only(self):
        base = slack.api_base()
        for name in slack.READ_METHODS:
            slack.check_allowed("GET", f"{base}/{name}")
            slack.check_allowed("POST", f"{base}/{name}")
        for bad in ("chat.postMessage", "chat.update", "chat.delete", "reactions.add", "conversations.join",
                    "conversations.mark", "auth.revoke", "files.upload", "users.setPresence", "chat.postMessage/x"):
            with self.assertRaises(slack.NotAllowed, msg=bad):
                slack.check_allowed("POST", f"{base}/{bad}")
        with self.assertRaises(slack.NotAllowed):
            slack.check_allowed("DELETE", f"{base}/auth.test")
        with self.assertRaises(slack.NotAllowed):
            slack.check_allowed("GET", "https://evil.example/api/auth.test")

    def test_refuses_before_network(self):
        with mock.patch.object(slack._opener, "open", side_effect=AssertionError("network used")):
            with self.assertRaises(slack.NotAllowed):
                slack.request("chat.postMessage", TOKEN, {"channel": "C1", "text": "hi"}, "POST")

    def test_source_calls_only_read_methods(self):
        import re
        src = (ROOT / "callsheet" / "app" / "slack.py").read_text()
        called = set(re.findall(r'\.get\("([a-z]+\.[A-Za-z]+)"', src)) | set(re.findall(r'request\("([a-z]+\.[A-Za-z]+)"', src))
        self.assertTrue(called)
        self.assertLessEqual(called, set(slack.READ_METHODS))

    def test_manifest_has_no_write_scopes(self):
        scopes = slack.MANIFEST["oauth_config"]["scopes"]
        self.assertEqual(set(scopes), {"user"})  # no bot scopes, no bot user
        self.assertEqual(set(scopes["user"]), {"channels:history", "channels:read", "groups:history", "groups:read",
                                               "im:history", "im:read", "mpim:history", "mpim:read", "users:read",
                                               "team:read"})
        text = slack.manifest_json() + slack.manifest_yaml()
        for bad in ("write", "chat:", "admin", "bot"):
            self.assertNotIn(bad, text.replace("background_color", ""))
        q = urllib.parse.urlsplit(slack.create_url()).query
        params = dict(urllib.parse.parse_qsl(q))
        self.assertEqual(params["new_app"], "1")
        self.assertEqual(json.loads(params["manifest_json"]), slack.MANIFEST)
        self.assertTrue(slack.create_url().startswith("https://api.slack.com/apps?new_app=1&manifest_json="))


class SlackBase(Base):
    def setUp(self):
        super().setUp()
        self.sl = fake_slack.FakeSlack()
        env = mock.patch.dict(os.environ, self.sl.env())
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.sl.close)
        self.sleeps = []
        self.app.syncer.sleep = self.sleeps.append
        self.ok("PATCH", "/api/settings", {"timezone": "UTC", "name": "Sam"})
        self.now = time.time()

    def connect_slack(self, area="personal"):
        return self.ok("POST", "/api/slack/connect", {"area_id": area, "token": TOKEN})

    def sync_brief(self):
        self.ok("POST", "/api/sync")
        return self.ok("GET", "/api/brief")

    def world(self):
        s, now = self.sl, self.now
        for uid, name, email in (("UANA", "Ana Costa", ""), ("UBOB", "Bob", ""), ("UTOM", "Tomás Berg", "tomas@northwind.example"),
                                 ("UMAR", "Marco Silva", ""), ("UKIM", "Kim", ""), ("ULEN", "Lena", "")):
            s.user(uid, name, email)
        s.dm("D1", "UANA")
        s.post("D1", ME, "Morning!", now - 6 * H)
        s.post("D1", "UANA", "Can you check the logo?", now - 4 * H)
        s.dm("D2", "UBOB")
        s.post("D2", "UBOB", "Lunch?", now - 5 * H)
        s.post("D2", ME, "Yes, 12:30.", now - 4 * H)
        s.dm("D3", "UTOM")
        s.post("D3", "UTOM", "ok", now - 600)
        s.dm("D4", "UKIM")
        s.post("D4", "UKIM", "thanks", now - 600)  # no reason yet: not a row
        s.channel("C1", "engineering")
        s.post("C1", "UMAR", f"<@{ME}> is Thursday 18:00 still OK for the deploy window?", now - 1 * H)
        s.post("C1", "ULEN", "<!channel> the office is closed on Monday, " + "please plan around it " * 5, now - 2 * H)
        s.channel("C2", "design")
        s.post("C2", "UKIM", f"<@{ME}> thoughts?", now - 2 * H)
        s.post("C2", ME, "Looks good.", now - 1 * H)
        s.channel("C3", "launch")
        root = s.post("C3", ME, "Draft plan is up.", now - 5 * H)
        s.post("C3", "UANA", "Which version goes to print, dark or light?", now - 3 * H, thread_ts=root)
        s.channel("C4", "random")
        s.post("C4", "UBOB", f"<@{ME}> look at this?", now - 1 * H)
        s.group("G1", "mpdm-sam--ana--bob-1")
        s.post("G1", "UANA", "Can we move the call?", now - 1 * H)
        return root


class ConnectTest(SlackBase):
    def test_manifest_route(self):
        m = self.ok("GET", "/api/slack/manifest")
        self.assertEqual(m["manifest"], slack.MANIFEST)
        self.assertTrue(m["create_url"].startswith("https://api.slack.com/apps?new_app=1&manifest_json="))
        self.assertIn("users:read", m["manifest_yaml"])

    def test_connect_errors_and_token_never_returned(self):
        status, data, _ = self.call("POST", "/api/slack/connect", {"area_id": "personal", "token": "xoxb-bot-token-123456"})
        self.assertEqual((status, data["error"]), (400, "Paste the User OAuth Token. It starts with xoxp-."))
        status, data, _ = self.call("POST", "/api/slack/connect", {"area_id": "personal", "token": "xoxp-9999-wrong-token"})
        self.assertEqual((status, data["error"]), (400, "Slack didn't accept that token. Copy the User OAuth Token again."))
        status, _, _ = self.call("POST", "/api/slack/connect", {"area_id": "nope", "token": TOKEN})
        self.assertEqual(status, 404)
        self.sl.channel("C1", "general")
        ws = self.connect_slack()
        self.assertEqual((ws["team_name"], ws["domain"], ws["user_id"], ws["token_hint"], ws["channels_total"]),
                         ("Northwind", "northwind", ME, "…oken", 1))
        self.assertNotIn("token", ws)
        again = self.connect_slack()
        self.assertEqual(again["id"], ws["id"])
        st = self.call("GET", "/api/state")[2]
        self.assertNotIn(TOKEN, st)
        st = json.loads(st)
        area = st["areas"][0]
        self.assertEqual((len(area["slack"]), area["chat_available"]), (1, True))
        self.assertTrue(all(name in slack.READ_METHODS for _, name, _ in self.sl.requests))

    def test_channels_and_ignore_list(self):
        self.sl.channel("C2", "random")
        self.sl.channel("C1", "general")
        self.sl.channel("C9", "not-mine", member=False)
        self.sl.dm("D1", "UANA")
        ws = self.connect_slack()
        ch = self.ok("GET", f"/api/slack/{ws['id']}/channels")
        self.assertEqual([(c["id"], c["name"], c["ignored"]) for c in ch["channels"]],
                         [("C1", "general", False), ("C2", "random", False)])
        self.assertFalse(ch["stale"])
        ws = self.ok("PATCH", f"/api/slack/{ws['id']}", {"ignored": ["C2"]})
        self.assertEqual(ws["ignored"], ["C2"])
        status, _, _ = self.call("PATCH", f"/api/slack/{ws['id']}", {"ignored": ["C9"]})
        self.assertEqual(status, 400)
        self.sl.tokens.clear()  # Slack unreachable for this token: the saved list, marked stale
        ch = self.ok("GET", f"/api/slack/{ws['id']}/channels")
        self.assertEqual((ch["stale"], [c["ignored"] for c in ch["channels"]]), (True, [False, True]))
        self.assertEqual(self.ok("DELETE", f"/api/slack/{ws['id']}"), {"ok": True})
        self.assertEqual(self.ok("GET", "/api/state")["areas"][0]["slack"], [])


class RulesTest(SlackBase):
    def setUp(self):
        super().setUp()
        self.root = self.world()
        self.ok("POST", "/api/vips", {"name": "Tomás Berg", "email": "tomas@northwind.example", "area_id": "personal"})
        ws = self.connect_slack()
        self.ws = self.ok("PATCH", f"/api/slack/{ws['id']}", {"ignored": ["C4"]})

    def rows(self, b):
        return {r["source"] + " " + r["who"]: r for r in b["replies"]}

    def test_rows_reasons_links_and_heads_up(self):
        b = self.sync_brief()
        rows = self.rows(b)
        self.assertEqual(set(rows), {"Slack · DM Ana Costa", "Slack · DM Tomás Berg", "Slack · #engineering Marco Silva",
                                     "Slack · #launch Ana Costa", "Slack · group DM Ana Costa"})
        self.assertEqual(b["replies"][0]["who"], "Tomás Berg")  # VIP first
        self.assertEqual(rows["Slack · DM Tomás Berg"]["reasons"], ["VIP"])
        self.assertEqual(rows["Slack · DM Ana Costa"]["reasons"], ["Direct question", "Waiting 4h"])
        marco = rows["Slack · #engineering Marco Silva"]
        self.assertEqual(marco["reasons"], ["Mentioned you", "Direct question"])
        self.assertEqual(marco["text"], '"@sam is Thursday 18:00 still OK for the deploy window?"')
        self.assertTrue(marco["link"].startswith("https://northwind.slack.com/archives/C1/p"))
        self.assertNotIn(".", marco["link"].rsplit("/p", 1)[1])
        launch = rows["Slack · #launch Ana Costa"]
        self.assertEqual(launch["reasons"], ["In your thread", "Direct question", "Waiting 3h"])
        self.assertIn("?thread_ts=" + self.root + "&cid=C3", launch["link"])
        self.assertEqual(rows["Slack · group DM Ana Costa"]["reasons"], ["Direct question"])
        self.assertTrue(all(r["via"] == "slack" and r["key"] and r["id"].startswith("r") for r in b["replies"]))
        heads = [h for h in b["heads_up"] if h.startswith("@channel")]
        self.assertEqual(len(heads), 1)
        self.assertTrue(heads[0].startswith("@channel in #engineering: the office is closed on Monday"))
        self.assertLessEqual(len(heads[0]), 100)
        self.assertIn("1 Slack workspace", b["sources"])
        # the ignored channel is never read
        self.assertEqual([p for p in self.sl.calls("conversations.history") if p["channel"] == "C4"], [])
        self.assertTrue(all(n in slack.READ_METHODS for _, n, _ in self.sl.requests))
        self.assertTrue(any("Callsheet Slack:" in line for line in self.logs))

    def test_mentions_stay_until_answered(self):
        self.sync_brief()
        b = self.sync_brief()  # nothing new: the mention and the thread are still there
        self.assertIn("Slack · #engineering Marco Silva", self.rows(b))
        self.sl.post("C1", ME, "Yes, 18:00 works.", time.time())
        self.sl.post("C3", ME, "Dark, please.", time.time(), thread_ts=self.root)
        b = self.sync_brief()
        self.assertNotIn("Slack · #engineering Marco Silva", self.rows(b))
        self.assertNotIn("Slack · #launch Ana Costa", self.rows(b))
        self.assertEqual([h for h in b["heads_up"] if h.startswith("@channel")], [])  # not new any more

    def test_vip_by_display_name_without_email(self):
        self.ok("POST", "/api/vips", {"name": "ana costa", "email": "ana@elsewhere.example", "area_id": "personal"})
        b = self.sync_brief()
        self.assertTrue(self.rows(b)["Slack · DM Ana Costa"]["vip"])

    def test_rate_limit_waits_retry_after(self):
        self.sl.limit = {"conversations.history": 1}
        b = self.sync_brief()
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(len(b["replies"]), 5)
        self.sl.limit = {"conversations.history": 99}
        b = self.sync_brief()
        self.assertEqual(self.sleeps, [2.0, 2.0, 2.0])  # 3 tries, then this workspace stops for this sync
        self.assertTrue(any("Slack asked Callsheet to slow down" in h for h in b["heads_up"]))

    def test_request_cap(self):
        with mock.patch.object(slack, "MAX_CALLS", 3):
            b = self.sync_brief()
        self.assertTrue(any("stopped after 3 requests" in h for h in b["heads_up"]))
        self.assertLessEqual(len(self.sl.requests) - 4, 3)  # 4 from connect (auth.test, team.info, list) + 1 list

    def test_revoked_token_needs_reconnect(self):
        self.sl.tokens.clear()
        b = self.sync_brief()
        self.assertIn("Couldn't read Slack Northwind: Slack no longer accepts the token. Connect the workspace again.",
                      b["heads_up"])
        self.assertEqual(self.ok("GET", "/api/state")["areas"][0]["slack"][0]["status"], "needs_reconnect")

    def test_chat_off_reads_nothing(self):
        self.ok("PATCH", "/api/areas/personal", {"reads": {"chat": False}})
        before = len(self.sl.requests)
        b = self.sync_brief()
        self.assertEqual((len(self.sl.requests), b["replies"]), (before, []))

    def test_suppress_sender_and_thread(self):
        b = self.sync_brief()
        ana = self.rows(b)["Slack · DM Ana Costa"]
        status, data, _ = self.call("POST", "/api/suppress", {"row_id": ana["id"], "scope": "domain"})
        self.assertEqual((status, data["error"]), (400, "Slack rows can be hidden for this thread or this sender only."))
        status, _, _ = self.call("POST", "/api/suppress", {"row_id": ana["id"], "scope": "sender", "key": "stale"})
        self.assertEqual(status, 409)
        out = self.ok("POST", "/api/suppress", {"row_id": ana["id"], "scope": "sender", "key": ana["key"]})
        self.assertEqual((out["rule"]["kind"], out["rule"]["value"], out["rule"]["source"]), ("sender", "uana", "slack"))
        b = self.ok("GET", "/api/brief")  # gone at once: the DM, the thread and the group DM are all Ana's
        self.assertEqual(b["hidden"], 3)
        self.assertEqual({r["who"] for r in b["replies"]}, {"Tomás Berg", "Marco Silva"})
        b = self.sync_brief()
        self.assertEqual((b["hidden"], len(b["replies"])), (3, 2))
        tom = self.rows(b)["Slack · DM Tomás Berg"]
        self.ok("POST", "/api/suppress", {"row_id": tom["id"], "scope": "sender"})
        self.assertIn("Slack · DM Tomás Berg", self.rows(self.sync_brief()))  # VIPs win over sender rules
        self.ok("POST", "/api/suppress", {"row_id": tom["id"], "scope": "thread"})
        self.assertNotIn("Slack · DM Tomás Berg", self.rows(self.sync_brief()))  # a thread rule always applies
        st = self.ok("GET", "/api/state")
        self.assertEqual([r["kind"] for r in st["suppress"]], ["sender", "sender", "thread"])
        self.ok("DELETE", f"/api/suppress/{st['suppress'][0]['id']}")
        self.assertIn("Slack · DM Ana Costa", self.rows(self.sync_brief()))


class SlackTextTest(unittest.TestCase):
    def test_markup_and_links(self):
        users = {"U1": {"name": "Sam"}}
        self.assertEqual(rules.slack_text("hi <@U1> in <#C1|gen>, <!here> see <https://x.example|the doc> &amp; "
                                          "<https://y.example>", users),
                         "hi @Sam in #gen, @here see the doc & https://y.example")
        self.assertEqual(rules.slack_link("acme", "C1", "1700000000.000100"),
                         "https://acme.slack.com/archives/C1/p1700000000000100")
        self.assertEqual(rules.slack_link("acme", "C1", "2.5", "1.5"),
                         "https://acme.slack.com/archives/C1/p25?thread_ts=1.5&cid=C1")


if __name__ == "__main__":
    unittest.main()
