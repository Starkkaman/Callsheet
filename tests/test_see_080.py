"""0.8.0 See everything (rules only, no AI): calendar files from email, invites to answer, the calendar-not-read
warning, security heads-ups, Sorted for you, Coming up and What changed. Stdlib only, no network."""
import datetime as dt
import pathlib
import re
import sys
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import gather  # noqa: E402
import horizon  # noqa: E402
import ics  # noqa: E402
import rules  # noqa: E402
import see080_fixtures as fx  # noqa: E402

LA = zoneinfo.ZoneInfo("America/Los_Angeles")
NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=LA)
OWNER = {"me@example.com"}


def parse(text, now=NOW):
    return ics.parse(text, owner=OWNER, default_tz=LA, now=now)


class IcsFormatsTest(unittest.TestCase):
    def test_google_utc_is_the_1130_meeting(self):
        r = parse(fx.GOOGLE)
        self.assertIsNone(r["error"])
        ev = r["events"][0]
        self.assertEqual((ev["title"], ev["method"], ev["status"]), ("Edit review", "REQUEST", "confirmed"))
        self.assertEqual(dt.datetime.fromisoformat(ev["start"]).astimezone(LA).strftime("%H:%M"), "11:30")
        self.assertEqual(ev["organizer"]["addr"], "dana@studio.example")
        self.assertEqual(ev["partstat"], "NEEDS-ACTION")

    def test_outlook_windows_zone_and_html_not_read(self):
        ev = parse(fx.OUTLOOK)["events"][0]
        self.assertEqual(dt.datetime.fromisoformat(ev["start"]).utcoffset(), dt.timedelta(hours=-8))
        self.assertFalse(ev["tz_guess"])
        fx.assert_safe(self, ev)
        self.assertEqual(len(parse(fx.OUTLOOK)["events"]), 1)  # the VALARM is not an event

    def test_zoom_publish_and_defanged_location(self):
        r = parse(fx.ZOOM)
        ev = r["events"][0]
        self.assertEqual(r["method"], "PUBLISH")
        self.assertIn("zoom[.]us", ev["location"])
        self.assertNotIn("https://", ev["location"])

    def test_teams_europe_zone(self):
        ev = parse(fx.TEAMS)["events"][0]
        s = dt.datetime.fromisoformat(ev["start"])
        self.assertEqual(s.astimezone(LA).strftime("%H:%M"), "08:00")  # 17:00 Berlin = 8:00 LA in November
        fx.assert_safe(self, ev)

    def test_la_dst_weekly_keeps_wall_clock(self):
        text = fx.cal(fx.vevent(uid="w1", start="DTSTART;TZID=America/Los_Angeles:20261026T090000",
                                end="DTEND;TZID=America/Los_Angeles:20261026T100000",
                                extra=("RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6",)))
        ev = parse(text, now=dt.datetime(2026, 10, 25, 8, 0, tzinfo=LA))["events"][0]
        starts = [dt.datetime.fromisoformat(s) for s, _ in ev["instances"]]
        self.assertEqual([s.astimezone(LA).hour for s in starts], [9, 9, 9])
        self.assertEqual([s.utcoffset().total_seconds() / 3600 for s in starts], [-7, -8, -8])
        self.assertTrue(ev["repeats"])

    def test_first_instance_after_now(self):
        text = fx.cal(fx.vevent(uid="d1", start="DTSTART;TZID=America/Los_Angeles:20260101T090000",
                                end="DTEND;TZID=America/Los_Angeles:20260101T093000",
                                extra=("RRULE:FREQ=DAILY", "EXDATE;TZID=America/Los_Angeles:20261104T090000")))
        ev = parse(text)["events"][0]
        first = dt.datetime.fromisoformat(ev["instances"][0][0]).astimezone(LA)
        self.assertEqual(first.date(), dt.date(2026, 11, 3))  # today's 9:00 has started: kept until it ends
        self.assertNotIn(dt.date(2026, 11, 4), [dt.datetime.fromisoformat(s).astimezone(LA).date()
                                                for s, _ in ev["instances"]])

    def test_floating_and_all_day(self):
        ev = parse(fx.cal(fx.vevent(uid="f1", start="DTSTART:20261110T100000", end="DTEND:20261110T110000")))
        ev = ev["events"][0]
        self.assertTrue(ev["floating"])
        self.assertEqual(dt.datetime.fromisoformat(ev["start"]).astimezone(LA).hour, 10)
        ad = parse(fx.cal(fx.vevent(uid="a1", start="DTSTART;VALUE=DATE:20261120", end="DTEND;VALUE=DATE:20261121")))
        self.assertTrue(ad["events"][0]["all_day"])

    def test_unknown_zone_is_a_guess(self):
        ev = parse(fx.cal(fx.vevent(uid="z1", start="DTSTART;TZID=Mars/Olympus:20261110T100000",
                                    end="DTEND;TZID=Mars/Olympus:20261110T110000")))["events"][0]
        self.assertTrue(ev["tz_guess"])


def found(mid, text, ts=1793700000, auth="pass", acc="me@example.com", inline=True, part=1, sent=False,
          frm="dana@studio.example"):
    out = []
    for ev in parse(text)["events"]:
        out.append({"ev": ev, "part": part, "inline": inline, "acc_email": acc, "msg_id": mid, "thread_id": "t" + mid,
                    "ts": ts, "auth": auth, "from_addr": frm, "sent": sent})
    return out


class MergeTest(unittest.TestCase):
    def test_cancel_and_update_by_sequence(self):
        req = found("m1", fx.GOOGLE)
        upd = found("m2", fx.cal(fx.vevent(seq=2, start="DTSTART:20261104T210000Z", end="DTEND:20261104T220000Z")),
                    ts=1793700100)
        m = gather.merge_invites(req + upd, now=NOW)
        self.assertEqual(len(m), 1)
        self.assertIn("updated", m[0]["marks"])
        self.assertTrue(m[0]["ev"]["start"].startswith("2026-11-04T21:00"))
        cancel = found("m3", fx.cal(fx.vevent(seq=3), method="CANCEL"), ts=1793700200)
        m = gather.merge_invites(req + upd + cancel, now=NOW)
        row = gather.email_invite_rows(m, OWNER, {"me@example.com": "work"})[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("cancelled", row["marks"])
        self.assertIsNone(row["link"])
        old_cancel = found("m4", fx.cal(fx.vevent(seq=0), method="CANCEL"), ts=1793600000)
        m = gather.merge_invites(upd + old_cancel, now=NOW)  # an older SEQUENCE can't cancel a newer one
        self.assertEqual(m[0]["ev"]["status"], "confirmed")

    def test_rows_are_unverified_with_answer_in_gmail(self):
        rows = gather.email_invite_rows(gather.merge_invites(found("m1", fx.GOOGLE), now=NOW), OWNER,
                                        {"me@example.com": "work"})
        r = rows[0]
        self.assertEqual((r["kind"], r["area"], r["sender"]), ("email", "work", "pass"))
        self.assertIn("unverified", r["marks"])
        self.assertTrue(r["link"].startswith("https://mail.google.com/mail/u/?authuser=me@example.com"))
        fx.assert_safe(self, r)

    def test_dmarc_fail_has_no_answer_button(self):
        hd = [("Authentication-Results", fx.AUTH_FAIL)]
        self.assertEqual(rules.auth_level(hd), "fail")
        self.assertEqual(rules.auth_level([("Authentication-Results", "evil.example; dmarc=pass")]), "none")
        self.assertEqual(rules.auth_level([("Authentication-Results", fx.AUTH_PASS)]), "pass")
        msg = fx.message("m9", [(fx.GOOGLE, {})], auth=fx.AUTH_FAIL)
        f = gather.message_invites(msg, "me@example.com", OWNER, LA, NOW)
        r = gather.email_invite_rows(gather.merge_invites(f, now=NOW), OWNER, {})[0]
        self.assertIn("sender_fail", r["marks"])
        self.assertIsNone(r["link"])
        self.assertTrue(r["mail_link"])
        page = gather.invites_for_page([r], LA, "12h", NOW.date())[0]
        self.assertIn(["sender_fail", gather.MARKS["sender_fail"]], page["marks"])

    def test_calendar_version_wins(self):
        self.assertEqual(gather.merge_invites(found("m1", fx.GOOGLE), {"abc123@google.com"}, NOW), [])

    def test_attachment_fetch_only_small_calendar_parts(self):
        calls = []
        msg = fx.message("m5", [(None, {"attachmentId": "A1", "size": 900, "filename": "invite.ics",
                                        "mime": "application/ics"}),
                                (None, {"attachmentId": "A2", "size": 10 * 1024 * 1024, "filename": "big.ics",
                                        "mime": "application/ics"})])
        f = gather.message_invites(msg, "me@example.com", OWNER, LA, NOW,
                                   fetch=lambda aid: calls.append(aid) or fx.b64(fx.ZOOM))
        self.assertEqual(calls, ["A1"])
        self.assertEqual([x["ev"]["title"] for x in f], ["Zoom catch-up"])


class CalendarInvitesTest(unittest.TestCase):
    def ev(self, status, uid="u1", start="2026-11-05T10:00:00-08:00"):
        return {"id": uid, "iCalUID": uid, "summary": "Planning", "status": "confirmed",
                "start": {"dateTime": start}, "end": {"dateTime": start.replace("10:00", "11:00")},
                "organizer": {"email": "x@corp.example", "displayName": "X"},
                "attendees": [{"email": "me@example.com", "self": True, "responseStatus": status}],
                "htmlLink": "https://www.google.com/calendar/event?eid=abc"}

    def test_needs_action_only(self):
        rows = gather.calendar_invite_rows([(self.ev("needsAction"), "work"), (self.ev("accepted", "u2"), "work"),
                                            (self.ev("needsAction", "u3", "2026-11-01T10:00:00-07:00"), "work")],
                                           LA, NOW)
        self.assertEqual([r["title"] for r in rows], ["Planning"])
        self.assertEqual(rows[0]["marks"], ["not_answered"])
        self.assertEqual(rows[0]["link"], "https://www.google.com/calendar/event?eid=abc")
        bad = self.ev("needsAction")
        bad["htmlLink"] = "https://evil.example/calendar"
        self.assertIsNone(gather.calendar_invite_rows([(bad, "work")], LA, NOW)[0]["link"])


class GapTest(unittest.TestCase):
    def test_mail_read_calendar_not(self):
        areas = {"work": {"reads": {"calendar": True, "mail": True}}, "home": {"reads": {"calendar": False,
                                                                                         "mail": True}}}
        accs = [{"email": "a@x.example", "area_id": "work", "services": {"calendar": False, "mail": True},
                 "calendars": []},
                {"email": "b@x.example", "area_id": "home", "services": {"calendar": True, "mail": True},
                 "calendars": [{"id": "b", "selected": True}]},
                {"email": "c@x.example", "area_id": "work", "services": {"calendar": True, "mail": True},
                 "calendars": [{"id": "c", "selected": True}]},
                {"email": "d@x.example", "area_id": "work", "services": {"calendar": True, "mail": False},
                 "calendars": []}]
        gaps = gather.calendar_gaps(accs, areas)
        self.assertEqual([g["email"] for g in gaps], ["a@x.example", "b@x.example"])
        self.assertIn("calendar isn't read", gather.gap_text(gaps[0]))


def mail(tid, sender, subject, labels=("INBOX",), at=1793700000, auth=fx.AUTH_PASS, snippet=""):
    hd = [{"name": "Authentication-Results", "value": auth}] if auth else []
    hd += [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
           {"name": "To", "value": "me@example.com"}]
    return {"id": tid, "messages": [{"id": tid + "m", "internalDate": str(at * 1000), "labelIds": list(labels),
                                     "snippet": snippet, "payload": {"headers": hd}}]}


class SecurityAndSortedTest(unittest.TestCase):
    def items(self, threads):
        return [gather.mail_item(t, "me@example.com", "work")[1] for t in threads]

    def test_security_always_surfaces_and_counts_are_exact(self):
        threads = [mail("p1", "Shop <deals@shop.example>", "50% off", ("INBOX", "CATEGORY_PROMOTIONS")),
                   mail("p2", "PayPal <service@paypal.com>", "Your PayPal verification code",
                        ("INBOX", "CATEGORY_PROMOTIONS")),
                   mail("u1", "GitHub <noreply@github.com>", "CI passed", ("INBOX", "CATEGORY_UPDATES")),
                   mail("o1", "Friend <f@pals.example>", "Photos")]
        out = gather.sort_mail(self.items(threads), 1793700100)
        self.assertEqual([s["who"] for s in out["security"]], ["PayPal"])
        self.assertIn("checked", out["security"][0]["marks"])
        n = {k: v["n"] for k, v in out["sorted"].items()}
        self.assertEqual(n, {"promos": 1, "updates": 1, "other": 1})
        self.assertEqual(sum(n.values()) + out["security_n"], len(threads))  # nothing hidden
        page = gather.sorted_for_page(out["sorted"], LA, "12h", NOW.date())
        self.assertEqual([(b["name"], b["n"], b["more"]) for b in page],
                         [("Promos", 1, 0), ("Updates", 1, 0), ("No reply needed", 1, 0)])

    def test_unread_promos_still_counted(self):
        extra = [{"who": "Not read yet", "subject": "", "at": 1793700000, "area": "w", "link": None}] * 3
        out = gather.sort_mail([], 1793700100, extra)
        self.assertEqual(out["sorted"]["promos"]["n"], 3)

    def test_look_alike_is_a_lure_never_the_bank(self):
        for frm in ("PayPal <service@paypa1.com>", "Chase <alerts@chаse.com>", "PayPal <x@xn--pypal-4ve.com>"):
            it = self.items([mail("l1", frm, "Unusual sign-in: verify your account")])[0]
            out = gather.sort_mail([it], 1793700100)
            self.assertEqual(len(out["security"]), 1, frm)
            s = out["security"][0]
            self.assertIn("lure", s["marks"], frm)
            self.assertNotIn("checked", s["marks"], frm)
            self.assertIsNone(s["brand"], frm)
            text = gather.security_for_page([s], LA, "12h", NOW.date())[0]["marks"]
            self.assertTrue(any(m[0] == "lure" and m[1].startswith("Not from") for m in text))

    def test_genuine_domain_failing_dmarc_is_not_checked(self):
        it = self.items([mail("g1", "Chase <alerts@chase.com>", "Payment declined", auth=fx.AUTH_FAIL)])[0]
        s = gather.sort_mail([it], 1793700100)["security"][0]
        self.assertIn("sender_fail", s["marks"])
        self.assertNotIn("checked", s["marks"])

    def test_vip_display_name_spoof(self):
        vips = {"priya@corp.example": "Priya Nair"}
        t = mail("v1", "Priya Nair <priya.nair@freemail.example>", "Quick favour?",
                 snippet="Can you send the wire today?")
        row = rules.reply_row(t, "me@example.com", "work", 1793700100, 3, vips)
        if row is not None:
            self.assertNotIn("VIP", row.get("reasons") or [])
            self.assertEqual(row.get("uses_name"), "Priya Nair")
        real = mail("v2", "Priya Nair <priya@corp.example>", "Lunch?", snippet="Lunch tomorrow?")
        self.assertIn("VIP", rules.reply_row(real, "me@example.com", "work", 1793700100, 3, vips)["reasons"])
        failed = mail("v3", "Priya Nair <priya@corp.example>", "Lunch?", auth=fx.AUTH_FAIL, snippet="Lunch?")
        r3 = rules.reply_row(failed, "me@example.com", "work", 1793700100, 3, vips)
        self.assertNotIn("VIP", (r3 or {}).get("reasons") or [])


class HorizonTest(unittest.TestCase):
    today = dt.date(2026, 11, 3)

    def cand(self, title, days, **kw):
        d = self.today + dt.timedelta(days=days)
        return dict({"key": "ev:" + title, "title": title, "start": d.isoformat(),
                     "end": (d + dt.timedelta(days=1)).isoformat(), "all_day": True, "days": 1}, **kw)

    def test_timing_table(self):
        evs = [self.cand("Flight to NYC", 14), self.cand("Flight to LA", 20), self.cand("Wedding", 30),
               self.cand("Car insurance renewal", 3), self.cand("Sam", 7, birthday=True),
               self.cand("Dentist", 2), self.cand("Grant deadline", 7)]
        c = horizon.items(evs, [], [], [], self.today, LA)
        b = horizon.build(c, {}, self.today)
        heads = {h["title"] for h in b["heads"]}
        self.assertEqual(heads, {"Flight to NYC", "Wedding", "Car insurance renewal", "Sam", "Grant deadline"})
        self.assertEqual([m["title"] for m in b["months"]], ["Flight to LA"])
        self.assertNotIn("Dentist", {x["title"] for x in b["heads"] + b["weeks"] + b["months"]})

    def test_got_it_quiet_until_next_lead_and_week_before(self):
        c = horizon.items([self.cand("Flight to NYC", 14)], [], [], [], self.today, LA)
        marks = horizon.apply_mark({}, "ev:Flight to NYC", "got", c, self.today)
        self.assertEqual(horizon.build(c, marks, self.today)["n"]["heads"], 0)
        later = self.today + dt.timedelta(days=12)  # 2 days before: the next lead point
        c2 = horizon.items([self.cand("Flight to NYC", 14)], [], [], [], self.today, LA)
        self.assertEqual(horizon.build(c2, marks, later)["n"]["heads"], 1)
        c3 = horizon.items([self.cand("Dentist", 7)], [], [], [], self.today, LA)
        m3 = horizon.apply_mark({}, "ev:Dentist", "week", c3, self.today)
        self.assertEqual(horizon.build(c3, m3, self.today)["n"]["heads"], 1)
        m4 = horizon.apply_mark({}, "ev:Flight to NYC", "notbig", c, self.today)
        b4 = horizon.build(c, m4, self.today)
        self.assertEqual((b4["n"]["heads"], b4["n"]["weeks"]), (0, 1))  # Not big: out of heads ups, still listed
        with self.assertRaises(ValueError):
            horizon.apply_mark({}, "ev:Flight to NYC", "send", c, self.today)

    def test_invites_daily_and_unverified_never_timed(self):
        inv = [{"key": "cal:1", "kind": "calendar", "status": "needs_action", "title": "Planning",
                "start": "2026-11-05T10:00:00-08:00"},
               {"key": "inv:1", "kind": "email", "status": "confirmed", "title": "Edit review",
                "start": "2026-11-04T11:30:00-08:00"}]
        c = horizon.items([], inv, [], [], self.today, LA)
        b = horizon.build(c, {}, self.today)
        self.assertEqual([h["title"] for h in b["heads"]], ["Planning"])
        self.assertEqual([w["kind"] for w in b["weeks"]], ["unverified"])
        m = horizon.apply_mark({}, "cal:1", "got", c, self.today)
        self.assertEqual(horizon.build(c, m, self.today)["n"]["heads"], 0)
        self.assertEqual(horizon.build(c, m, self.today + dt.timedelta(days=1))["n"]["heads"], 1)  # daily


class WhatChangedTest(unittest.TestCase):
    def test_new_moved_cancelled(self):
        def ev(i, title, start):
            return {"id": i, "summary": title, "start": {"dateTime": start}, "end": {"dateTime": start}}
        base = gather.snapshot({"c": [ev("1", "A", "2026-11-05T10:00:00-08:00"),
                                      ev("2", "B", "2026-11-06T10:00:00-08:00")]}, LA, NOW - dt.timedelta(days=1))
        cur = gather.snapshot({"c": [ev("1", "A", "2026-11-05T12:00:00-08:00"),
                                     ev("3", "C", "2026-11-07T10:00:00-08:00")],
                               "unread": [ev("9", "Z", "2026-11-07T10:00:00-08:00")]}, LA, NOW)
        ch = gather.changes(base, cur, LA, NOW)
        self.assertEqual([(c["kind"], c["title"]) for c in ch], [("moved", "A"), ("cancelled", "B"), ("new", "C")])
        page = gather.changes_for_page(ch, LA, "12h", NOW.date())
        self.assertTrue(page[0]["text"].startswith("A moved from"))
        self.assertEqual(gather.changes(None, cur, LA, NOW), [])


class PageTest(unittest.TestCase):
    def test_page_renders_text_only(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        self.assertNotRegex(js, r"\.innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=")

    def test_writer_input_has_no_080_fields(self):
        src = (ROOT / "callsheet" / "app" / "sync.py").read_text()
        body = src[src.index("def _writer_input"):]
        body = body[:body.index("\n    def ", 10)]
        for word in ("invites", "security", "sorted", "coming_up", "changed", "upcoming"):
            self.assertNotRegex(body, rf"[\"']{word}[\"']")


if __name__ == "__main__":
    unittest.main()
