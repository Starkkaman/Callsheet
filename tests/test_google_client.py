"""The Google client is read-only by construction: GET only to Gmail/Calendar, POST only to token/revoke."""
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import google  # noqa: E402

REAL = {"CALLSHEET_GOOGLE_AUTH_BASE": "", "CALLSHEET_GOOGLE_TOKEN_URL": "", "CALLSHEET_GOOGLE_API_BASE": ""}


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k in REAL:
            os.environ.pop(k, None)

    def test_defaults_are_google(self):
        self.assertEqual(google.token_url(), "https://oauth2.googleapis.com/token")
        self.assertEqual(google.revoke_url(), "https://oauth2.googleapis.com/revoke")
        self.assertEqual(google.api_base(), "https://www.googleapis.com")
        self.assertTrue(google.build_auth_url("id", "s", "c").startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"))

    def test_allowed(self):
        for method, url in (
                ("GET", "https://www.googleapis.com/gmail/v1/users/me/threads?q=x"),
                ("GET", "https://www.googleapis.com/gmail/v1/users/me/threads/abc"),
                ("GET", "https://www.googleapis.com/gmail/v1/users/me/profile"),
                ("GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList"),
                ("GET", "https://www.googleapis.com/calendar/v3/calendars/a%40b.c/events?x=1"),
                ("POST", "https://oauth2.googleapis.com/token"),
                ("POST", "https://oauth2.googleapis.com/revoke")):
            google.check_allowed(method, url)

    def test_refused(self):
        for method, url in (
                ("POST", "https://www.googleapis.com/gmail/v1/users/me/messages/send"),
                ("POST", "https://www.googleapis.com/gmail/v1/users/me/drafts"),
                ("PUT", "https://www.googleapis.com/gmail/v1/users/me/threads/abc"),
                ("PATCH", "https://www.googleapis.com/calendar/v3/calendars/x/events/y"),
                ("DELETE", "https://www.googleapis.com/calendar/v3/calendars/x/events/y"),
                ("POST", "https://www.googleapis.com/calendar/v3/calendars/x/events"),
                ("GET", "https://oauth2.googleapis.com/token"),
                ("POST", "https://oauth2.googleapis.com/token/extra"),
                ("GET", "https://www.googleapis.com/drive/v3/files"),
                ("GET", "https://www.googleapis.com/gmail/v1/users/me/../../../drive/v3/files"),
                ("GET", "https://evil.example/gmail/v1/users/me/threads"),
                ("GET", "http://www.googleapis.com/gmail/v1/users/me/threads"),
                ("GET", "https://www.googleapis.com.evil.example/gmail/v1/users/me/threads")):
            with self.assertRaises(google.NotAllowed, msg=f"{method} {url}"):
                google.check_allowed(method, url)

    def test_request_refuses_before_network(self):
        with mock.patch.object(google._opener, "open", side_effect=AssertionError("network used")):
            with self.assertRaises(google.NotAllowed):
                google.request("POST", "https://www.googleapis.com/gmail/v1/users/me/messages/send", form={})

    def test_env_override_moves_the_allowlist(self):
        os.environ["CALLSHEET_GOOGLE_API_BASE"] = "http://127.0.0.1:9"
        google.check_allowed("GET", "http://127.0.0.1:9/gmail/v1/users/me/threads")
        with self.assertRaises(google.NotAllowed):
            google.check_allowed("GET", "https://www.googleapis.com/gmail/v1/users/me/threads")


class SourceTest(unittest.TestCase):
    NET = re.compile(r"^\s*(?:import|from)\s+(urllib\.request|http\.client|socket|ssl|smtplib)\b", re.M)

    SDK = re.compile(r"^\s*(?:import|from)\s+(anthropic|openai|requests|httpx)\b", re.M)

    def test_only_google_writer_slack_and_ha_open_connections(self):
        for f in sorted(APP.glob("*.py")):
            src = f.read_text(encoding="utf-8")
            hits = self.NET.findall(src)
            if f.name in ("google.py", "writer.py", "slack.py", "ha.py"):  # ha.py: 0.5.1 notify only
                self.assertEqual(hits, ["urllib.request"], f.name)
            else:
                self.assertEqual(hits, [], f.name)
            # the Anthropic SDK is used (lazily) by writer.py only; no other HTTP libraries
            self.assertEqual(self.SDK.findall(src), ["anthropic"] if f.name == "writer.py" else [], f.name)

    def test_api_calls_are_get_only(self):
        src = (APP / "google.py").read_text(encoding="utf-8")
        for call in re.findall(r'request\("(\w+)", ([^,)]+)', src):
            method, target = call
            if "token_url" in target or "revoke_url" in target:
                self.assertEqual(method, "POST", call)
            else:
                self.assertEqual(method, "GET", call)


if __name__ == "__main__":
    unittest.main()
