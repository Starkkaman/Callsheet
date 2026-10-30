"""0.4.1: the wait text on reply rows. Each stored reply row carries `wait` = facts.wait_text() of the row's facts
(calendar days in the owner's time zone to the brief's date), with or without the writer, stored with the brief,
no message text; the page shows it with a middle dot in place of the age. Fakes only; stdlib only."""
import datetime as dt
import json
import pathlib
import re
import sys
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import facts  # noqa: E402
import rules  # noqa: E402
from fake_google import thread  # noqa: E402
from test_writer import WriterBase  # noqa: E402

UTC = dt.timezone.utc


class RowWaitTest(unittest.TestCase):
    def test_same_rule_as_the_writer_facts(self):
        tz = zoneinfo.ZoneInfo("America/Los_Angeles")
        day = dt.date(2026, 10, 30)
        at = lambda d, h: dt.datetime(2026, 10, d, h, tzinfo=tz).timestamp()  # noqa: E731
        f = {"first": at(19, 9), "last": at(22, 23), "count": 3}
        self.assertEqual(facts.row_wait(f, day, tz), "waiting 11 days, last nudge 8 days ago")
        self.assertEqual(facts.row_wait(f, day, tz),
                         facts.build([{"id": "r1", "who": "A", "text": "x", "facts": f}], [], day, tz)
                         ["replies"]["r1"]["wait"])
        self.assertEqual(facts.row_wait({"first": at(27, 9), "last": at(27, 9)}, day, tz), "waiting 3 days")
        self.assertEqual(facts.row_wait({"first": at(30, 1)}, day, tz), "waiting since today")
        # the owner's time zone decides the day: 23:30 in Los Angeles on the 29th is already the 30th in UTC
        late = {"first": at(29, 23) + 1800}
        self.assertEqual(facts.row_wait(late, day, tz), "waiting 1 day")
        self.assertEqual(facts.row_wait(late, day, UTC), "waiting since today")
        for f in (None, {}, {"first": None, "count": 0}):
            self.assertIsNone(facts.row_wait(f, day, tz))

    def test_slack_rows_facts(self):
        now = dt.datetime(2026, 10, 30, 15, 0, tzinfo=UTC).timestamp()
        ts = lambda d: f"{now - d * 86400:.6f}"  # noqa: E731
        data = {"me": "U1", "users": {"U2": {"name": "Ana"}}, "convs": {"D1": {"kind": "dm"}},
                "dm": {"D1": [{"user": "U1", "ts": ts(12), "text": "Hi"}, {"user": "U2", "ts": ts(6), "text": "Any news?"},
                              {"user": "U2", "ts": ts(2), "text": "Ping"}]}}
        rows, _ = rules.slack_rows(data, {"team_id": "T1"}, "work", now, {})
        self.assertEqual(facts.row_wait(rows[0]["_facts"], dt.date(2026, 10, 30), UTC),
                         "waiting 6 days, last nudge 2 days ago")


class SyncWaitTest(WriterBase):
    def seed_waits(self):
        now = dt.datetime.now(UTC)
        d = lambda n: now - dt.timedelta(days=n)  # noqa: E731
        self.seed(threads=[
            thread("t1", "Priya N <priya@acme.example>", "Invoice 1042", "Any news on this?", d(8),
                   earlier=[("Priya N <priya@acme.example>", d(11), ("INBOX",))], body="Any news on this?"),
            thread("t2", "Bob <bob@x.example>", "Venue", "Can you confirm the venue?", d(3), body="Venue?"),
        ], events={"me@example.com": []})

    def waits(self, b):
        return {r["who"]: r.get("wait") for r in b["replies"]}

    def test_rows_carry_wait_without_a_writer(self):
        self.seed_waits()
        b = self.sync_brief()
        want = {"Priya N": "waiting 11 days, last nudge 8 days ago", "Bob": "waiting 3 days"}
        self.assertEqual(self.waits(b), want)
        stored = self.app.store.cached_brief()
        self.assertEqual(self.waits(stored), want)
        # only the wording: no facts or message text on stored rows, nothing new in brief_refs
        self.assertNotIn("_facts", json.dumps(stored))
        self.assertNotIn("Any news", json.dumps(stored["replies"]))
        self.assertNotIn("waiting", json.dumps(self.app.store.brief_refs()))

    def test_with_the_writer_the_same_and_the_payload_unchanged(self):
        self.seed_waits()
        self.ok("PATCH", "/api/writer", {"provider": "local", "local": {"base_url": self.loc.base, "model": "m"}})
        b = self.sync_brief()
        self.assertEqual(self.waits(b), {"Priya N": "waiting 11 days, last nudge 8 days ago", "Bob": "waiting 3 days"})
        payload = self.loc.chats()[-1]["body"]["messages"][1]["content"]
        # the writer gets the wording only in its facts block, as before (once per row), not on the data rows
        self.assertEqual(payload.count("waiting 11 days, last nudge 8 days ago"), 1)


class PageTest(unittest.TestCase):
    def test_app_js_renders_the_wait_as_text_in_place_of_the_age(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        self.assertIn("return typeof r.wait === 'string' && r.wait ? r.wait.split(', ').join(' · ') : '';", js)
        self.assertIn("function ageOf(r) { return waitOf(r) || r.age || ''; }", js)
        # the list row's age span and the detail panel's meta line, both through h() (text nodes)
        self.assertIn("h('span', { class: 'age', title: waitOf(r) || null }, ageOf(r))", js)
        self.assertIn("ageOf(r) ? h('span', null, ageOf(r)) : null", js)
        self.assertEqual(len(re.findall(r"\br\.age\b", js)), 1)  # only inside ageOf()
        self.assertIsNone(re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js))


if __name__ == "__main__":
    unittest.main()
