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
