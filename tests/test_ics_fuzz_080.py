"""0.8.0 ICS fuzz suite (the test lead's F1-F8, offline). Bar: no crash, every case inside its time and memory caps,
no real (Calendar API) event altered, every output escaped or defanged. Set CALLSHEET_FUZZ_REPORT=1 to print the
time and peak memory of every case."""
import base64
import datetime as dt
import os
import pathlib
import sys
import time
import tracemalloc
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import gather  # noqa: E402
import ics  # noqa: E402
import rules  # noqa: E402
import see080_fixtures as fx  # noqa: E402
from test_api_google import Base  # noqa: E402
from fake_google import event  # noqa: E402

LA = zoneinfo.ZoneInfo("America/Los_Angeles")
NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=LA)
OWNER = {"me@example.com"}
TIME_CAP_S = 2.0
MEM_CAP_B = 64 * 1024 * 1024
RESULTS = []


def ve(**kw):
    return fx.vevent(**kw)


class Fuzz(unittest.TestCase):
    def run_case(self, name, fn):
        tracemalloc.start()
        t = time.perf_counter()
        try:
            out = fn()
        finally:
            took = time.perf_counter() - t
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        RESULTS.append((name, took, peak))
        self.assertLess(took, TIME_CAP_S, name)
        self.assertLess(peak, MEM_CAP_B, name)
        return out

    def parse(self, name, text, now=NOW):
        r = self.run_case(name, lambda: ics.parse(text, owner=OWNER, default_tz=LA, now=now))
        for ev in r["events"]:
            fx.assert_safe(self, ev)
        return r

    def invites(self, name, msg, fetch=None, cal_uids=()):
        def go():
            f = gather.message_invites(msg, "me@example.com", OWNER, LA, NOW, fetch)
            return gather.email_invite_rows(gather.merge_invites(f, cal_uids, NOW), OWNER, {})
        rows = self.run_case(name, go)
        for r in rows:
            fx.assert_safe(self, r)
        return rows

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("CALLSHEET_FUZZ_REPORT") and RESULTS:
            for name, took, peak in RESULTS:
                sys.stderr.write(f"\nFUZZ {name:<34} {took * 1000:8.1f} ms {peak / 1024:9.0f} KiB")
            RESULTS.clear()

    # F1 RRULE explosion
    def test_f1_rrule_explosion(self):
        for name, rule, start in (
                ("F1 SECONDLY no COUNT", "RRULE:FREQ=SECONDLY", "DTSTART:20261104T193000Z"),
                ("F1 MINUTELY COUNT=10^6", "RRULE:FREQ=MINUTELY;COUNT=1000000", "DTSTART:20261104T193000Z"),
                ("F1 DAILY COUNT=10^6 from 1990", "RRULE:FREQ=DAILY;COUNT=1000000", "DTSTART:19900101T090000Z"),
                ("F1 WEEKLY 7 days to 2099", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;UNTIL=20991231T000000Z",
                 "DTSTART:19900101T090000Z"),
                ("F1 MONTHLY INTERVAL=1 no end", "RRULE:FREQ=MONTHLY;INTERVAL=1", "DTSTART:19900131T090000Z"),
                ("F1 YEARLY BYSETPOS unsupported", "RRULE:FREQ=YEARLY;BYSETPOS=-1;BYDAY=MO", "DTSTART:20261104T193000Z")):
            r = self.parse(name, fx.cal(ve(uid="r1", start=start, end=start.replace("DTSTART", "DTEND"),
                                           extra=(rule,))))
            for ev in r["events"]:
                self.assertLessEqual(len(ev["instances"]), ics.MAX_INSTANCES)
        many = "\r\n".join(ve(uid=f"x{i}", extra=("RRULE:FREQ=SECONDLY;COUNT=1000000",)) for i in range(60))
        r = self.parse("F1 60 SECONDLY events", fx.cal(many))
        self.assertLessEqual(len(r["events"]), ics.MAX_EVENTS)

    # F2 UID collision with a real calendar event
    def test_f2_uid_collision_unit(self):
        cancel = fx.message("c1", [(fx.cal(ve(uid="real-1", seq=9), method="CANCEL"), {"method": "CANCEL"})])
        moved = fx.message("c2", [(fx.cal(ve(uid="real-1", seq=9, start="DTSTART:20261104T010000Z",
                                             end="DTEND:20261104T020000Z")), {})])
        for name, m in (("F2 CANCEL of a real UID", cancel), ("F2 moved real UID", moved)):
            self.assertEqual(self.invites(name, m, cal_uids={"real-1"}), [])

    # F3 organiser / attendee spoofed as the owner
    def test_f3_owner_spoof(self):
        text = fx.cal(ve(uid="s1", organizer="ORGANIZER;CN=Me:mailto:me@example.com"))
        for name, auth in (("F3 owner organiser, dmarc fail", fx.AUTH_FAIL), ("F3 owner organiser, no auth", None)):
            rows = self.invites(name, fx.message("o" + name[-4:], [(text, {})], sender="Me <me@example.com>",
                                                  auth=auth))
            self.assertIn("uses_your_address", rows[0]["marks"])
            self.assertIn("unverified", rows[0]["marks"])
            self.assertNotEqual(rows[0]["kind"], "calendar")

    # F4 time zones and dates
    def test_f4_zones_and_dates(self):
        r = self.parse("F4 garbage TZID", fx.cal(ve(uid="t1", start="DTSTART;TZID=\x00../../etc/passwd:20261110T100000",
                                                    end="DTEND;TZID=Nope/Nope:20261110T110000")))
        self.assertTrue(all(ev["tz_guess"] for ev in r["events"]))
        bomb = "\r\n".join("BEGIN:VTIMEZONE\r\nTZID:Z%d\r\n" % i + "BEGIN:STANDARD\r\nTZOFFSETTO:+1400\r\n"
                           "END:STANDARD\r\n" * 20 + "END:VTIMEZONE" for i in range(200))
        r = self.parse("F4 VTIMEZONE bomb", fx.cal(ve(uid="t2", start="DTSTART;TZID=Z199:20261110T100000",
                                                      end="DTEND;TZID=Z199:20261110T110000"), extra=bomb))
        self.assertLessEqual(len(r["events"]), 1)
        for name, s in (("F4 DTSTART year 1601", "DTSTART:16010101T000000Z"),
                        ("F4 DTSTART year 9999", "DTSTART:99991231T235959Z"),
                        ("F4 DTSTART bogus", "DTSTART:20261399T256199Z")):
            r = self.parse(name, fx.cal(ve(uid="t3", start=s, end="DTEND:20261110T110000Z")))
            self.assertEqual(r["events"], [], name)
        r = self.parse("F4 DTEND before DTSTART", fx.cal(ve(uid="t4", start="DTSTART:20261110T110000Z",
                                                             end="DTEND:20261110T090000Z")))
        ev = r["events"][0]
        self.assertIn("end_before_start", ev["flags"])
        self.assertGreaterEqual(ev["end"], ev["start"])

    # F5 bidi, zero-width, homoglyphs
    def test_f5_invisible_and_homoglyphs(self):
        text = fx.cal(ve(uid="h1", summary="Board ‮meeting‬​ with Pаypal",
                         organizer="ORGANIZER;CN=⁦Dana⁩ Lеe:mailto:dana@studio.example"))
        r = self.parse("F5 bidi + zero-width + homoglyph", text)
        ev = r["events"][0]
        self.assertTrue(ev["title_marked"])
        self.assertTrue(ev["organizer"]["marked"])
        rows = self.invites("F5 rows", fx.message("h1", [(text, {})]))
        self.assertIn("look_alike", rows[0]["marks"])

    # F6 sizes, encodings, HTML, data: URIs, alarms
    def test_f6_sizes_and_encodings(self):
        big = fx.cal(ve(uid="b1", extra=("X-JUNK:" + "A" * (10 * 1024 * 1024),)))
        self.assertEqual(self.parse("F6 10 MB file", big)["error"], "too_big")
        many = fx.cal(*[ve(uid=f"m{i}", summary=f"E{i}") for i in range(10000)])
        r = self.parse("F6 10k VEVENTs", many)
        self.assertLessEqual(len(r["events"]), ics.MAX_EVENTS)
        small_many = fx.cal(*["BEGIN:VEVENT\r\nUID:u%d\r\nDTSTART:20261110T100000Z\r\nSUMMARY:x\r\nEND:VEVENT" % i
                              for i in range(2000)])
        r = self.parse("F6 2k small VEVENTs", small_many)
        self.assertLessEqual(len(r["events"]), ics.MAX_EVENTS)
        att = "\r\n".join(f"ATTENDEE;CN=P{i}:mailto:p{i}@x.example" for i in range(1000))
        self.parse("F6 1k ATTENDEEs", fx.cal(ve(uid="a1", extra=(att,))))
        folded = "SUMMARY:" + "\r\n ".join(["ab"] * 30000)
        r = self.parse("F6 deep folding", fx.cal(ve(uid="f1", summary="x", extra=(folded,))))
        for ev in r["events"]:
            self.assertLessEqual(len(ev["title"]), ics.MAX_TITLE)
        nasty = fx.cal(ve(uid="n1", summary="Caf\xe9 <b onerror=x>", extra=(
            "X-ALT-DESC;FMTTYPE=text/html:<script>alert(1)</script><img src=https://t.example/p.gif>",
            "ATTACH;VALUE=URI:data:text/html;base64,PHNjcmlwdD4=", "ATTACH:https://t.example/a.ics",
            "URL:javascript:alert(1)", "LOCATION:https://evil.example/login",
            "BEGIN:VALARM", "ACTION:AUDIO", "ATTACH:https://t.example/ring.mp3", "TRIGGER:-PT5M", "END:VALARM")))
        cp1252 = nasty.encode("cp1252", "replace")
        fetched = []
        msg = fx.message("e1", [(None, {"attachmentId": "Q1", "size": 4000, "mime": "application/ics",
                                        "charset": "windows-1252", "filename": "a.ics"}),
                                (None, {"attachmentId": "Q2", "size": 12 * 1024 * 1024, "filename": "huge.ics"})])
        rows = self.invites("F6 cp1252 + HTML + data: + VALARM", msg,
                            fetch=lambda aid: fetched.append(aid) or fx.b64(cp1252))
        self.assertEqual(fetched, ["Q1"])  # no fetch over the cap, never a URL
        self.assertEqual(rows[0]["title"], "Café <b onerror=x>")  # kept as literal text; the page uses textContent
        qp = nasty.replace("Caf\xe9", "Caf=E9").encode("latin-1")
        msg = fx.message("e2", [(None, {"cte": "quoted-printable", "charset": "iso-8859-1"})])
        msg["payload"]["parts"][1]["body"] = {"size": len(qp), "data": base64.urlsafe_b64encode(qp).decode()}
        self.invites("F6 quoted-printable part", msg)
        b64part = base64.b64encode(nasty.encode()).decode()
        msg = fx.message("e3", [(None, {"cte": "base64"})])
        msg["payload"]["parts"][1]["body"] = {"size": len(b64part), "data": fx.b64(b64part)}
        self.invites("F6 base64-in-base64 part", msg)

    # F7 conflicting parts in one mail
    def test_f7_conflicting_parts(self):
        a = fx.cal(ve(uid="k1", summary="Lunch"))
        b = fx.cal(ve(uid="k1", summary="Lunch moved", start="DTSTART:20261105T193000Z",
                      end="DTEND:20261105T201500Z"))
        msg = fx.message("k1", [(b, {"attachmentId": "", "filename": "invite.ics", "mime": "application/ics"}),
                                (a, {})])
        rows = self.invites("F7 two parts disagree", msg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Lunch")  # the inline text/calendar part wins
        self.assertIn("disagree", rows[0]["marks"])
        many = fx.message("k2", [(fx.cal(ve(uid="k2", summary=f"V{i}")), {}) for i in range(80)])
        self.assertLessEqual(len(self.invites("F7 80 parts", many)), 1)

    # F8 METHOD variants
    def test_f8_methods(self):
        pub = self.invites("F8 PUBLISH", fx.message("p1", [(fx.cal(ve(uid="p1"), method="PUBLISH"), {})]))
        self.assertIn("event_file", pub[0]["marks"])
        for m in ("REFRESH", "COUNTER", "REPLY", "DECLINECOUNTER"):
            self.assertEqual(self.invites("F8 " + m, fx.message("q" + m, [(fx.cal(ve(uid="q1"), method=m),
                                                                            {"method": m})])), [], m)
        none = self.invites("F8 no METHOD", fx.message("p2", [(fx.cal(ve(uid="p2"), method=None), {"method": ""})]))
        self.assertTrue(none and "unverified" not in none[0]["marks"] or not none)
        weird = self.invites("F8 METHOD:X-EVIL", fx.message("p3", [(fx.cal(ve(uid="p3"), method="X-EVIL"),
                                                                    {"method": "X-EVIL"})]))
        for r in weird:
            self.assertIsNone(r.get("calendar"))

    # security and VIP surfacing
    def test_security_lures_and_vip_spoofs(self):
        def alert(addr, subject, auth=fx.AUTH_PASS):
            hd = {"from": addr, "subject": subject}
            return rules.security_alert(hd, addr.split("<")[-1].strip(">"), subject, "", rules.auth_level(
                [("Authentication-Results", auth)]))
        for addr in ("alerts@chase-secure.example", "alerts@chаse.com", "service@paypa1.com",
                     "service@xn--pypal-4ve.com", "no-reply@accounts.goog1e.com", "x@rnicrosoft.com"):
            a = self.run_case("SEC look-alike " + addr[:18], lambda: alert(addr, "Verify your account: new sign-in"))
            self.assertIsNotNone(a, addr)
            self.assertNotIn("checked", a["marks"], addr)
            if addr != "alerts@chase-secure.example":
                self.assertIn("lure", a["marks"], addr)
            self.assertIsNone(a["brand"], addr)
        ok = alert("alerts@chase.com", "Your verification code")
        self.assertIn("checked", ok["marks"])
        self.assertEqual(rules.vip_name_of("Priya  NAIR", {"p@corp.example": "Priya Nair"}), "Priya Nair")


class UidCollisionSyncTest(Base):
    """F2 end to end: an email cancel or move of a real calendar event's UID changes nothing that is shown."""
    def test_real_event_is_never_altered(self):
        # the app's day is the machine's local date when no time zone is set (TZ may be anything): 01:00 local
        # today, so start + 6 h is still today whatever time and zone the suite runs in
        now = dt.datetime.now().astimezone().replace(second=0, microsecond=0)
        start = now.replace(hour=1, minute=0).astimezone(zoneinfo.ZoneInfo("UTC"))
        email = "me@example.com"
        user = self.fake.add_user(email, events={email: [event("Real meeting", start, start + dt.timedelta(hours=1),
                                                               iCalUID="real-1@google.com")]})
        stamp = lambda d: d.strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
        cancel = fx.cal(ve(uid="real-1@google.com", seq=99, summary="Real meeting",
                           start="DTSTART:" + stamp(start), end="DTEND:" + stamp(start + dt.timedelta(hours=1))),
                        method="CANCEL")
        moved = fx.cal(ve(uid="real-1@google.com", seq=100, summary="Real meeting",
                          start="DTSTART:" + stamp(start + dt.timedelta(hours=5)),
                          end="DTEND:" + stamp(start + dt.timedelta(hours=6))))
        email_only = fx.cal(ve(uid="mail-only@google.com", summary="Edit review",
                               start="DTSTART:" + stamp(start + dt.timedelta(days=1)),
                               end="DTEND:" + stamp(start + dt.timedelta(days=1, hours=1))))
        ts = int(now.timestamp() * 1000)
        user["messages"] = [fx.message("m1", [(cancel, {"method": "CANCEL"})], ts_ms=ts),
                            fx.message("m2", [(moved, {})], ts_ms=ts),
                            fx.message("m3", [(None, {"attachmentId": "AT1", "size": 900, "mime": "application/ics",
                                                      "filename": "invite.ics"})], ts_ms=ts,
                                       sender="Google Calendar <calendar-notification@google.com>")]
        user["attachments"] = {"AT1": fx.b64(email_only)}
        self.connect(email)
        self.ok("POST", "/api/sync")
        b = self.ok("GET", "/api/brief")
        self.assertEqual([r["title"] for r in b["schedule"] if r.get("title") == "Real meeting"], ["Real meeting"])
        self.assertEqual([i["title"] for i in b.get("invites") or []], ["Edit review"])
        inv = b["invites"][0]
        self.assertEqual(inv["kind"], "email")
        self.assertIn("unverified", [m[0] for m in inv["marks"]])
        self.assertFalse(inv.get("clash"))
        self.assertNotIn("Edit review", [r.get("title") for r in b["schedule"]])  # never booked, never a clash
        for method, path, _ in self.fake.requests:
            if path.startswith(("/gmail/", "/calendar/")):
                self.assertEqual(method, "GET", path)
        self.assertEqual(len(self.logs), 1)
        self.assertNotIn("Edit review", self.logs[0])


if __name__ == "__main__":
    unittest.main()
