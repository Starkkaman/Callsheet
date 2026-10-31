"""0.5.0: dates in mail and chat become "Possible events" (dates.py): the deterministic finder (proposals, ranges,
deadlines; questions, the past and unclear weeks left out), the already-on-the-calendar check, the Add to calendar
link (Google Calendar template URL: encoding, ctz, authuser, all-day), the page fields in 12h/24h, the sync end to
end against the fake Google, and the page code (textContent, the link opened like the Gmail links).
Fakes only; stdlib only."""
import datetime as dt
import pathlib
import re
import sys
import unittest
import urllib.parse
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import dates  # noqa: E402
from fake_google import event, thread  # noqa: E402
from test_writer import WriterBase  # noqa: E402

UTC = dt.timezone.utc
VAN = zoneinfo.ZoneInfo("America/Vancouver")
FRI = dt.date(2026, 10, 30)  # a Friday
APP_JS = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()


def found(text, sent=FRI):
    return [(c["kind"], c["date"].isoformat(), c["start"], c["end"]) for c in dates.find(text, sent)]


class FindTest(unittest.TestCase):
    def test_weekday_and_time(self):
        self.assertEqual(found("Can we meet Tuesday at 2pm?"), [("event", "2026-11-03", "14:00", "15:00")])
        self.assertEqual(found("Lunch on Monday at 12:30 p.m. works for me."),
                         [("event", "2026-11-02", "12:30", "13:30")])
        self.assertEqual(found("Call tomorrow at 9am."), [("event", "2026-10-31", "09:00", "10:00")])

    def test_day_month_and_iso(self):
        self.assertEqual(found("Lunch on 3 Nov at 2pm?"), [("event", "2026-11-03", "14:00", "15:00")])
        self.assertEqual(found("Meeting on 2026-11-12 at 14:00."), [("event", "2026-11-12", "14:00", "15:00")])

    def test_ranges(self):
        self.assertEqual(found("Call tomorrow 9:30-10:15"), [("event", "2026-10-31", "09:30", "10:15")])
        self.assertEqual(found("Meet between 3 and 4pm on Wednesday."), [("event", "2026-11-04", "15:00", "16:00")])
        self.assertEqual(found("Lunch 5 Nov at 11pm"), [("event", "2026-11-05", "23:00", "24:00")])

    def test_proposals_without_a_time_are_all_day(self):
        self.assertEqual(found("Does Monday work for a call?"), [("event", "2026-11-02", None, None)])
        self.assertEqual(found("Coffee Tuesday or Wednesday at 10am?"),
                         [("event", "2026-11-03", None, None), ("event", "2026-11-04", "10:00", "11:00")])

    def test_deadlines(self):
        self.assertEqual(found("Due Friday 6 Nov at 5pm."), [("deadline", "2026-11-06", "16:30", "17:00")])
        self.assertEqual(found("Please send it by Thursday."), [("deadline", "2026-11-05", None, None)])

    def test_questions_the_past_and_unclear_dates_are_left_out(self):
        for text in ("What time works on Thursday?", "When on Friday?", "Can we meet at 3pm?", "Dinner tonight?",
                     "Thanks for the call on Monday.", "We discussed it on Tuesday at 2pm.",
                     "Shall we meet next Friday?", "The office is on the 3rd floor, meet there.",
                     "Sat down to plan the party.", "Here is the report you asked for."):
            with self.subTest(text):
                self.assertEqual(found(text), [])

    def test_at_most_two_per_message(self):
        self.assertEqual(len(found("Meet Monday at 9am. Call Tuesday at 10am. Lunch Wednesday at noon.")), 2)


class CalendarTest(unittest.TestCase):
    def rows(self, text, **kw):
        return [dict({"id": "r1", "key": "k1", "who": "Priya Nair", "source": "Mail", "via": "mail", "area": "work",
                      "subject": "Re: Budget review", "text": text, "sent": FRI, "account": "priya.owner@example.com",
                      "link": "https://mail.google.com/mail/u/0/#inbox/abc"}, **kw)]

    def test_already_on_the_calendar_is_skipped(self):
        rows = self.rows("Can we meet Tuesday at 2pm?")
        at = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=UTC)  # noqa: E731
        busy = dates.calendar_items([(event("Dentist", at(3, 13, 30), at(3, 14, 30)), "work")], UTC, FRI)
        self.assertEqual(dates.possible_events(rows, busy, FRI), [])
        named = dates.calendar_items([(event("Budget review", dt.date(2026, 11, 3), dt.date(2026, 11, 4)), "work")],
                                     UTC, FRI)
        self.assertEqual(dates.possible_events(rows, named, FRI), [])
        free = dates.calendar_items([(event("Dentist", at(3, 15), at(3, 16)), "work"),
                                     (event("Budget review", at(4, 14), at(4, 15)), "work"),
                                     (event("Cancelled", at(3, 14), at(3, 15), status="cancelled"), "work"),
                                     (event("Free", at(3, 14), at(3, 15), transparency="transparent"), "work")],
                                    UTC, FRI)
        got = dates.possible_events(rows, free, FRI)
        self.assertEqual([(p["id"], p["title"], p["date"], p["start_hhmm"], p["end_hhmm"]) for p in got],
                         [("p1", "Budget review", "2026-11-03", "14:00", "15:00")])
        self.assertTrue(got[0]["key"])
        self.assertEqual(got[0]["row"], "r1")

    def test_titles_and_order(self):
        rows = self.rows("Lunch Wednesday at noon?", via="slack", source="Slack", subject="", account=None,
                         link=None) + self.rows("Please send it by Monday.", id="r2", key="k2", subject="Report")
        got = dates.possible_events(rows, [], FRI)
        self.assertEqual([(p["title"], p["kind"], p["date"]) for p in got],
                         [("Due: Report", "deadline", "2026-11-02"), ("Lunch with Priya Nair", "event", "2026-11-04")])
        self.assertEqual(dates.possible_events(self.rows("Due Thursday.", via="slack", subject=""), [], FRI)[0]["title"],
                         "Deadline from Priya Nair")

    def test_past_and_far_dates_are_left_out(self):
        self.assertEqual(dates.possible_events(self.rows("Meeting on 2026-10-20 at 10:00."), [], FRI), [])
        self.assertEqual(dates.possible_events(self.rows("Meeting on 2027-06-01 at 10:00."), [], FRI), [])

    def url(self, p, tz="America/Vancouver", account="priya.owner@example.com"):
        u = urllib.parse.urlsplit(dates.calendar_url(p, tz, account))
        return u, dict(urllib.parse.parse_qsl(u.query))

    def test_calendar_url(self):
        p = dates.possible_events(self.rows("Can we meet Tuesday at 2pm? Bring the Q&A notes #7 + 50%."), [], FRI)[0]
        u, q = self.url(p)
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "calendar.google.com", "/calendar/render"))
        self.assertEqual(q["action"], "TEMPLATE")
        self.assertEqual(q["text"], "Budget review")
        self.assertEqual(q["dates"], "20261103T140000/20261103T150000")
        self.assertEqual(q["ctz"], "America/Vancouver")
        self.assertEqual(q["authuser"], "priya.owner@example.com")
        self.assertIn("From Priya Nair (email): \"Tuesday at 2pm\"", q["details"])
        self.assertIn("https://mail.google.com/mail/u/0/#inbox/abc", q["details"])
        raw = dates.calendar_url(p, "America/Vancouver", "priya.owner@example.com")
        self.assertIn("authuser=priya.owner@example.com", raw)  # "@" kept readable, like the Gmail links
        self.assertIn("ctz=America/Vancouver", raw)
        self.assertNotRegex(raw, r"[ \"<>]")
        self.assertNotIn("#", raw.split("?", 1)[1])

    def test_calendar_url_tricky_text_round_trips(self):
        p = {"title": "Q&A #7 + 50% = \"done\"?", "date": "2026-11-03", "start_hhmm": "23:00", "end_hhmm": "24:00",
             "who": "Bo", "via": "slack", "phrase": "a=b&c", "link": "javascript:alert(1)"}
        u, q = self.url(p, "UTC", None)
        self.assertEqual(q["text"], p["title"])
        self.assertEqual(q["dates"], "20261103T230000/20261104T000000")
        self.assertNotIn("authuser", q)
        self.assertIn("From Bo (Slack): \"a=b&c\"", q["details"])
        self.assertNotIn("javascript", q["details"])

    def test_all_day(self):
        p = dates.possible_events(self.rows("Does Monday work for a call?"), [], FRI)[0]
        self.assertEqual(self.url(p)[1]["dates"], "20261102/20261103")

    def test_for_page_12h_24h_today(self):
        items = dates.possible_events(
            self.rows("Call today at 2pm. Lunch Monday at 12:30.")
            + self.rows("Due Friday 6 Nov at 5pm. Also send it by Thursday.", id="r2", key="k2", subject="Report"),
            [], FRI)
        p12 = dates.for_page(items, "UTC", "12h", FRI)
        self.assertEqual([p["when"] for p in p12], ["Today · 2 pm–3 pm", "Mon 2 Nov · 12:30 pm–1:30 pm",
                                                    "Thu 5 Nov · due", "Fri 6 Nov · due 5 pm"])
        self.assertEqual([p["today"] for p in p12], [True, False, False, False])
        p24 = dates.for_page(items, "UTC", "24h", FRI)
        self.assertEqual(p24[0]["when"], "Today · 14:00–15:00")
        self.assertEqual(p24[3]["when"], "Fri 6 Nov · due 17:00")
        for p in p12:
            self.assertNotIn("account", p)
            self.assertNotIn("link", p)
            self.assertTrue(p["url"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE&"))
        later = dates.for_page(items, "UTC", "12h", FRI + dt.timedelta(days=3))
        self.assertEqual([p["when"] for p in later], ["Today · 12:30 pm–1:30 pm", "Thu 5 Nov · due",
                                                      "Fri 6 Nov · due 5 pm"])  # past dates are gone
        self.assertEqual(dates.for_page([{"date": "bad"}, {}], "UTC", "12h", FRI), [])


class SyncTest(WriterBase):
    def test_brief_lists_possible_events_not_on_the_calendar(self):
        now = dt.datetime.now(UTC)
        today = now.date()
        d3 = today + dt.timedelta(days=3)
        d4 = today + dt.timedelta(days=4)
        day = lambda d: f"{d.day} {d:%b}"  # noqa: E731
        busy = event("Dentist", dt.datetime.combine(d3, dt.time(14, 30), tzinfo=UTC),
                     dt.datetime.combine(d3, dt.time(15, 30), tzinfo=UTC))
        self.seed(threads=[
            thread("t1", "Priya N <priya@acme.example>", "Budget", f"Can we meet on {day(d3)} at 2pm?",
                   now - dt.timedelta(hours=3), body=f"Can we meet on {day(d3)} at 2pm?"),
            thread("t2", "Bob <bob@x.example>", "Lunch plan", f"Lunch on {day(d4)} at 1pm?",
                   now - dt.timedelta(hours=2), body=f"Lunch on {day(d4)} at 1pm?"),
        ], events={"me@example.com": [busy]})
        self.ok("PATCH", "/api/settings", {"time_format": "24h"})
        b = self.sync_brief()
        self.assertNotIn("Dentist", [e.get("title") for e in b["schedule"]])  # only today's events are the schedule
        ps = b["possible"]
        self.assertEqual([(p["title"], p["date"], p["start_hhmm"]) for p in ps], [("Lunch plan", d4.isoformat(), "13:00")])
        p = ps[0]
        self.assertTrue(p["when"].endswith("13:00–14:00"), p["when"])
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(p["url"]).query))
        self.assertEqual((q["authuser"], q["ctz"]), ("me@example.com", "UTC"))
        self.assertEqual(p["row"], next(r["id"] for r in b["replies"] if r["who"] == "Bob"))
        self.assertNotIn("account", p)
        # a note on the possible event marks it by its "pe:" ref
        n = self.ok("POST", "/api/memory/notes", {"kind": "pe", "id": p["id"], "ref": "pe:" + p["key"],
                                                   "text": "Bob's lunches run long."})
        self.assertEqual(n["from"], "possible event")
        self.assertIn("pe:" + p["key"], self.ok("GET", "/api/brief")["notes"])

    def test_sample_brief_has_no_possible_events(self):
        b = self.ok("GET", "/api/brief")
        self.assertTrue(b.get("sample"))
        self.assertNotIn("possible", b)


class PageTest(unittest.TestCase):
    def fn(self, name):
        m = re.search(r"\n  function " + name + r"\(.*?\n  }\n", APP_JS, re.S)
        self.assertIsNotNone(m, name)
        return m.group(0)

    def test_possible_events_render_as_text_with_a_checked_link(self):
        card = self.fn("peCard")
        self.assertIn("safeHttps(p.url)", card)
        self.assertIn("target: '_blank', rel: 'noopener noreferrer'", card)
        self.assertNotIn("calendar.google", APP_JS)
        for name in ("peCard", "possibleSection", "noteItem", "notePop", "noteAction", "memoryBlock"):
            body = self.fn(name)
            self.assertNotRegex(body, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", name)

    def test_note_and_memory_calls_use_relative_urls(self):
        for path in ("'api/memory/notes'", "'api/memory'", "'api/memory/reset'"):
            self.assertIn(path, APP_JS)
        self.assertNotIn("'/api/memory", APP_JS)


if __name__ == "__main__":
    unittest.main()
