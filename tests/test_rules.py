"""Rules, scheduling and brief composition (pure functions, no network)."""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import rules  # noqa: E402
import store  # noqa: E402
from fake_google import event, thread  # noqa: E402
from scheduler import Scheduler  # noqa: E402

AMS = zoneinfo.ZoneInfo("Europe/Amsterdam")
DAY = dt.date(2026, 10, 28)  # a Wednesday


def at(h, m=0, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=AMS)


def settings(**kw):
    s = store.default_settings()
    s["timezone"] = "Europe/Amsterdam"
    s.update(kw)
    return s


class ScheduleRulesTest(unittest.TestCase):
    def test_first_falls_back_to_first_timed(self):
        rows, _ = rules.build_schedule([(event("Solo", at(8), at(9)), "personal"),
                                        (event("Later", at(10), at(11)), "personal")], AMS, DAY)
        self.assertTrue(rows[0].get("first"))
        self.assertNotIn("first", rows[1])

    def test_note_from_description_and_duplicates(self):
        e = event("Sync", at(10), at(11), iCalUID="u1",
                  description="<p>Read <b>spec</b> &amp; notes</p><p>second line</p>" + "x" * 200)
        dup = dict(e, id="other")
        rows, _ = rules.build_schedule([(e, "a"), (dup, "b")], AMS, DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "Read spec & notes")
        self.assertNotIn("clash", rows[0])
        long = event("L", at(12), at(13), description="y" * 200)
        rows, _ = rules.build_schedule([(long, "a")], AMS, DAY)
        self.assertEqual(len(rows[0]["note"]), 80)

    def test_utc_times_shown_local_and_all_day(self):
        utc = dt.datetime(2026, 10, 28, 8, 0, tzinfo=dt.timezone.utc)
        rows, all_day = rules.build_schedule([
            (event("Call", utc, utc + dt.timedelta(hours=1)), "a"),
            (event("Trip", DAY - dt.timedelta(days=1), DAY + dt.timedelta(days=2)), "a")], AMS, DAY)
        self.assertEqual((rows[0]["time"], rows[0]["end"]), ("09:00", "10:00"))
        self.assertEqual(all_day, ["Trip"])


class ReplyRulesTest(unittest.TestCase):
    NOW = at(12).timestamp()

    def row(self, t, vips=None, age_days=2, me="me@example.com"):
        return rules.reply_row(t, me, "personal", self.NOW, age_days, vips or {})

    def test_reasons(self):
        now = at(12)
        self.assertIsNone(self.row(thread("a", "X <x@e.x>", "Hi", "thanks", now - dt.timedelta(hours=1))))
        r = self.row(thread("b", "x@e.x", "Report", "due EOD please", now - dt.timedelta(hours=1)))
        self.assertEqual((r["reasons"], r["who"]), (["Mentions a deadline"], "x@e.x"))
        r = self.row(thread("c", "X <x@e.x>", "", "Old &amp; quiet", now - dt.timedelta(days=5)))
        self.assertEqual((r["reasons"], r["text"], r["age"]), (["Waiting 5 days"], '"Old & quiet"', "5d"))
        self.assertIsNone(self.row(thread("d", "X <x@e.x>", "Hi", "ok", now - dt.timedelta(days=5)), age_days=0))
        for bad in ("Sending you the due diligence", "by the way", "by month end"):
            r = self.row(thread("e", "X <x@e.x>", "Hi", bad, now - dt.timedelta(hours=1)))
            self.assertEqual(r and r["reasons"], ["Mentions a deadline"] if "due " in bad else None, bad)

    def test_bulk_and_automated_dropped(self):
        old = at(12) - dt.timedelta(days=5)
        cases = [({"List-Id": "<news.e.x>"}, "x@e.x"), ({"List-Unsubscribe": "<mailto:u@e.x>"}, "x@e.x"),
                 ({"Precedence": "bulk"}, "x@e.x"), ({"Precedence": " List "}, "x@e.x"),
                 ({"Precedence": "junk"}, "x@e.x"), ({"Auto-Submitted": "auto-replied"}, "x@e.x"),
                 ({"Auto-Submitted": "auto-generated"}, "x@e.x")]
        cases += [({}, f"{local}@e.x") for local in (
            "noreply", "no-reply", "NoReply", "donotreply", "do-not-reply", "notification", "notifications",
            "mailer-daemon", "postmaster", "alert", "alerts", "bounce", "bounces+123", "billing.noreply")]
        for headers, sender in cases:
            t = thread("a", f"Robot <{sender}>", "Due by Friday?", "Can you?", old, headers=headers)
            self.assertIsNone(self.row(t), (headers, sender))
            # a VIP is always kept
            r = self.row(t, vips={sender.lower(): "Robot"})
            self.assertEqual(r["reasons"][0], "VIP", (headers, sender))
        # not automated: Auto-Submitted "no", ordinary senders that merely contain the words
        for headers, sender in (({"Auto-Submitted": "no"}, "x@e.x"), ({"Precedence": "first-class"}, "x@e.x"),
                                ({"Reply-To": "noreply@e.x"}, "x@e.x"), ({}, "alertson@e.x"),
                                ({}, "renotify@e.x")):
            t = thread("b", f"P <{sender}>", "Hi", "plain", old, headers=headers)
            self.assertEqual(self.row(t)["reasons"], ["Waiting 5 days"], (headers, sender))

    def test_must_be_addressed(self):
        old = at(12) - dt.timedelta(days=5)
        # not in To or Cc (e.g. a Bcc or group alias): waiting, deadline and question don't count
        t = thread("a", "P <p@e.x>", "Due by Friday?", "Can you?", old, to="team@e.x")
        self.assertIsNone(self.row(t))
        # in Cc only: waiting and deadline count, a question doesn't
        t = thread("b", "P <p@e.x>", "Due by Friday?", "Can you?", old, to="team@e.x",
                   cc="Other <o@e.x>, Me <ME@example.com>")
        self.assertEqual(self.row(t)["reasons"], ["Mentions a deadline", "Waiting 5 days"])
        # in To among others: everything counts
        t = thread("c", "P <p@e.x>", "Due by Friday?", "Can you?", old, to="o@e.x, Me <me@example.com>")
        self.assertEqual(self.row(t)["reasons"], ["Direct question", "Mentions a deadline", "Waiting 5 days"])
        # VIP counts even when not addressed
        t = thread("d", "Boss <boss@e.x>", "FYI", "notes", at(11), to="all@e.x")
        self.assertEqual(self.row(t, vips={"boss@e.x": "Boss"})["reasons"], ["VIP"])

    def test_own_last_message_and_drafts(self):
        now = at(12)
        t = thread("a", "Me <ME@example.com>", "Q?", "ok?", now - dt.timedelta(days=3))
        self.assertIsNone(self.row(t))
        t = thread("b", "X <x@e.x>", "Q?", "draft", now, labels=("DRAFT",),
                   earlier=[("Y <y@e.x>", now - dt.timedelta(days=1), ("INBOX",))])
        r = self.row(t)
        self.assertEqual(r["who"], "Y")
        t = thread("c", "Someone <s@e.x>", "Q?", "sent from alias", now, labels=("SENT",))
        self.assertIsNone(self.row(t))

    def test_gmail_link(self):
        # Gmail's own view-link format: hex API ids written in decimal (checked against a real viewUrl)
        self.assertEqual(rules.gmail_link("issaacc.h@gmail.com", "1a1559dc6547b99f", "1a155d21e6e7b99f"),
                         "https://mail.google.com/mail/u/?authuser=issaacc.h@gmail.com"
                         "#all/thread-f:1879507222623926687|msg-f:1879510819686300063")
        self.assertEqual(rules.gmail_link("me+work@example.com", "1a1559dc6547b99f"),
                         "https://mail.google.com/mail/u/?authuser=me%2Bwork@example.com"
                         "#all/thread-f:1879507222623926687")
        # ids that aren't hex never produce a broken link: account only, or thread only
        self.assertEqual(rules.gmail_link("a@b.c", "t9"), "https://mail.google.com/mail/u/?authuser=a@b.c")
        self.assertEqual(rules.gmail_link("a@b.c", "ff", "mX"),
                         "https://mail.google.com/mail/u/?authuser=a@b.c#all/thread-f:255")
        now = at(12)
        t = thread("1a1559dc6547b99f", "X <x@e.x>", "Q?", "ok?", now - dt.timedelta(hours=1))
        self.assertTrue(self.row(t)["link"].startswith(
            "https://mail.google.com/mail/u/?authuser=me@example.com#all/thread-f:1879507222623926687"))

    def test_sort_vip_then_age(self):
        now = at(12)
        rows = [self.row(thread(i, f"{i} <{i}@e.x>", "?", "?", now - dt.timedelta(hours=h)),
                         vips={"v@e.x": "V"}) for i, h in (("a", 1), ("b", 50), ("v", 2))]
        self.assertEqual([r["who"] for r in rules.sort_replies(rows)], ["v", "b", "a"])
        self.assertNotIn("_age_s", rows[0])


class CallTimesTest(unittest.TestCase):
    def test_next_call(self):
        s = settings()
        self.assertEqual(rules.next_call(s, at(7))[0], at(12, 30))
        self.assertEqual(rules.next_call(s, at(12, 30))[0], at(17))
        nxt, call = rules.next_call(s, at(18))
        self.assertEqual((nxt, call["id"]), (at(6, 30, DAY + dt.timedelta(days=1)), "morning"))

    def test_days_off_skip_checkins_but_not_brief(self):
        sat = dt.date(2026, 10, 31)
        s = settings()
        self.assertEqual(rules.next_call(s, at(7, 0, sat))[0], at(6, 30, sat + dt.timedelta(days=1)))
        s = settings(skip_checkins_days_off=False)
        self.assertEqual(rules.next_call(s, at(7, 0, sat))[0], at(12, 30, sat))

    def test_none_enabled(self):
        s = settings()
        for c in s["call_times"]:
            c["enabled"] = False
        self.assertEqual(rules.next_call(s, at(7)), (None, None))

    def test_kind(self):
        s = settings()
        self.assertEqual(rules.kind_for(s, at(5)), "Morning brief")
        self.assertEqual(rules.kind_for(s, at(12, 30)), "Midday check-in")
        self.assertEqual(rules.kind_for(s, at(20)), "Afternoon check-in")

    def test_scheduler_due_and_startup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = store.Store(tmp.name)
        self.addCleanup(st.close)
        st.update_settings({"timezone": "Europe/Amsterdam"})
        sch = Scheduler(st, syncer=None, clock=lambda: at(12, 31).timestamp())
        self.assertTrue(sch.due(at(12, 29).timestamp(), at(12, 30).timestamp()))
        self.assertFalse(sch.due(at(12, 30).timestamp(), at(12, 31).timestamp()))
        self.assertFalse(sch.startup_due())  # no accounts
        st.save_account("personal", "me@example.com", "rt", "at", 0, 0)
        self.assertTrue(sch.startup_due())  # never synced
        st.set_last_sync({"at": at(12, 10).isoformat(), "ok": True, "errors": []})
        self.assertFalse(sch.startup_due())
        st.set_last_sync({"at": at(11, 0).isoformat(), "ok": True, "errors": []})
        self.assertTrue(sch.startup_due())


class ComposeTest(unittest.TestCase):
    def test_live_without_cache(self):
        b = rules.compose_brief({"projects": [1]}, settings(), [{"id": "personal", "name": "P", "colour": 1,
                                                                 "reads": {}, "fixed": True}], None, True, at(7))
        self.assertEqual((b["sample"], b["sections"]["schedule"], b["sections"]["projects"]), (False, "live", "sample"))
        self.assertEqual((b["schedule"], b["replies"], b["projects"]), ([], [], [1]))
        self.assertEqual((b["next_checkin"], b["date_long"], b["date_short"]), ("12:30", "Wednesday 28 Oct", "Wed 28 Oct"))
        self.assertEqual(b["areas"], [{"id": "personal", "name": "P", "colour": 1}])


if __name__ == "__main__":
    unittest.main()
