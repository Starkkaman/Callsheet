"""0.24.3 calendar-vs-calendar clashes (conflicts.calendar_pairs): two overlapping calendar entries with no ledger item
behind either, under the 0.23 rule (both must need the owner). Fictional people and calendars only."""
import pathlib
import sys
import unittest

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"),
                str(pathlib.Path(__file__).resolve().parent)]
from test_ledger_0160 import FakeSend, ts  # noqa: E402
from test_need_0230 import ME, NeedBase, att, ev, guest  # noqa: E402

import conflicts  # noqa: E402
import need as N  # noqa: E402
import push  # noqa: E402
import today as today_mod  # noqa: E402


class CalPairs(NeedBase):
    def setUp(self):
        super().setUp()
        self.ms.write(lambda db: N.sync_calendars(db, [{"id": "acc", "calendars": [
            {"id": "primary", "summary": "Sam", "primary": True},
            {"id": "family", "summary": "Family calendar"},
            {"id": "school", "summary": "Fernhill School", "use": "headsup"}]}], owner_addrs=(ME["address"],)))

    def sweep(self):
        return self.ms.write(lambda db: conflicts.sweep(db, self.now[0], self.lv.tz()))

    def pairs(self, state="open"):
        return [dict(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM ledger_conflicts WHERE a_kind='event' AND b_kind='event' AND state=? ORDER BY id", (state,))]

    def two(self, a=None, b=None, start2="2027-03-02T10:30-08:00", end2="2027-03-02T11:30-08:00"):
        a = dict(ev("a1", "Budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00"), **(a or {}))
        b = dict(ev("b1", "Dentist", start2, end2), **(b or {}))
        self.put(a, b)
        self.sweep()
        return self.pairs()

    def test_two_entries_of_his_clash(self):
        got = self.two()
        self.assertEqual(len(got), 1)
        c = got[0]
        self.assertEqual((c["verdict"], c["confidence"], c["kind"]), ("clash", "sure", "overlap"))
        self.assertEqual((c["slot_start"], c["slot_end"]), (ts("2027-03-02T10:30-08:00"), ts("2027-03-02T11:00-08:00")))
        self.assertIn("calendar: Budget review overlaps calendar: Dentist", c["why"])
        self.assertIsNone(c["message"])
        self.assertEqual(c["alert"], "")                     # the sweep alone never alerts (Alerter.notify does)
        self.sweep()
        self.assertEqual(len(self.pairs()), 1)               # the same pair is kept, not made again
        view = self.lv.conflicts(state="open", now=self.now[0])
        side_kinds = [[s["kind"] for s in x["sides"]] for x in view if x["id"] == c["id"]]
        self.assertEqual(side_kinds, [["event", "event"]])

    def test_out_declined_free_all_day_optional_cancelled(self):
        for name, b in (("declined", {"attendees": [att("declined"), guest("Ana Ruiz")]}),
                        ("free", {"transparency": "transparent"}),
                        ("all day", {"all_day": 1, "start_ts": ts("2027-03-02T00:00-08:00"),
                                     "end_ts": ts("2027-03-03T00:00-08:00")}),
                        ("optional", {"attendees": [att("needsAction", optional=True), guest("Ana Ruiz")],
                                      "organizer": "ana@work.example"}),
                        ("cancelled", {"status": "cancelled"}),
                        ("fyi calendar", {"calendar": "school"})):
            with self.subTest(name):
                self.ms.write(lambda db: db.execute("DELETE FROM events"))
                self.ms.write(lambda db: db.execute("DELETE FROM ledger_conflicts"))
                self.assertEqual(self.two(b=dict(b, event_id="b-" + name.replace(" ", "-"))), [], name)

    def test_shared_calendar_needs_his_address(self):
        not_him = {"calendar": "family", "attendees": [guest("Lena Ortiz")], "organizer": "lena@work.example"}
        self.assertEqual(self.two(b=not_him), [])
        self.ms.write(lambda db: db.execute("DELETE FROM events"))
        him = {"calendar": "family", "event_id": "b2", "organizer": "lena@work.example",
               "attendees": [dict(guest("Lena Ortiz"), self=True),
                             {"name": "Sam Rowe", "address": ME["address"], "response": "accepted", "self": False}]}
        got = self.two(b=him)
        self.assertEqual([c["verdict"] for c in got], ["clash"])

    def test_same_ical_uid_is_one_event(self):
        self.assertEqual(self.two(a={"ical_uid": "uid-1@x.example"},
                                  b={"calendar": "family", "ical_uid": "uid-1@x.example",
                                     "attendees": [att("accepted")]}), [])

    def test_tentative_and_unanswered_required_are_maybe(self):
        got = self.two(b={"attendees": [att("tentative"), guest("Ana Ruiz")], "organizer": "ana@work.example"})
        self.assertEqual([(c["verdict"], c["confidence"]) for c in got], [("maybe", "likely")])
        self.ms.write(lambda db: db.execute("DELETE FROM events"))
        self.ms.write(lambda db: db.execute("DELETE FROM ledger_conflicts"))
        got = self.two(b={"event_id": "b3", "attendees": [att("needsAction"), guest("Ana Ruiz")],
                          "organizer": "ana@work.example"})
        self.assertEqual([c["verdict"] for c in got], ["maybe"])

    def test_past_overlaps_and_the_grace(self):
        self.now[0] = ts("2027-03-02T11:20-08:00")           # the overlap ended 20 min ago: still shown
        self.assertEqual(len(self.two()), 1)
        self.now[0] = ts("2027-03-02T11:40-08:00")           # 40 min ago: closed "In the past"
        self.sweep()
        self.assertEqual(self.pairs(), [])
        self.assertEqual(self.pairs("resolved")[0]["resolved_why"], conflicts.PAST_WHY)

    def test_entry_with_a_ledger_item_behind_is_left_to_the_item(self):
        a, _b = self.put(ev("a1", "Budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00"),
                         ev("b1", "Dentist", "2027-03-02T10:30-08:00", "2027-03-02T11:30-08:00"))
        self.ms.write(lambda db: db.execute(
            "INSERT INTO ledger_items (type, subtype, who, what, status, confidence, created_at, updated_at, cal_ref) "
            "VALUES ('event','','owner','Budget review','open','sure',?,?,?)", (self.now[0], self.now[0], a["id"])))
        self.sweep()
        self.assertEqual(self.pairs(), [])

    def test_cancelled_later_resolves_and_dismissed_stays(self):
        got = self.two()
        self.put(dict(ev("b1", "Dentist", "2027-03-02T10:30-08:00", "2027-03-02T11:30-08:00"), status="cancelled"))
        self.sweep()
        self.assertEqual(self.pairs(), [])
        self.assertEqual(self.pairs("resolved")[0]["id"], got[0]["id"])
        self.put(dict(ev("b1", "Dentist", "2027-03-02T10:30-08:00", "2027-03-02T11:30-08:00")))
        self.sweep()
        cid = self.pairs()[0]["id"]
        self.ms.write(lambda db: db.execute("UPDATE ledger_conflicts SET state='resolved', resolved_why='dismissed' "
                                            "WHERE id=?", (cid,)))
        self.sweep()
        self.assertEqual(self.pairs(), [])                   # the owner's dismissal holds


def gts(t):
    """Google's `updated` (RFC 3339, UTC) for epoch seconds t."""
    import datetime as dt  # noqa: PLC0415
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class CalPairAlerts(CalPairs):
    """0.24.3 (the owner, 2026-11-17): calendar-pair clashes and Maybe clashes alert like other clashes: first seen
    while ahead, one entry created or changed in the last 6 h (no backfill), quiet hours and rules as usual."""

    def setUp(self):
        super().setUp()
        self.cfg = dict(push.defaults(), targets=["notify.mobile_app_x"], quiet_start="22:00", quiet_end="07:00")
        self.send = FakeSend()
        self.al = conflicts.Alerter(self.ms, self.lv, lambda: self.cfg, self.send,
                                    lambda title, message, tag=None, urgent=False: {"title": title, "message": message,
                                                                                    "tag": tag, "urgent": urgent},
                                    clock=lambda: self.now[0])

    def sweep(self):
        res = super().sweep()
        res["alerts"] = self.al.notify({"conflicts": {"new": res["pairs_new"]}})
        return res

    def fresh(self, ago=600):
        return {"updated": gts(self.now[0] - ago)}

    def test_new_entry_alerts_once(self):
        got = self.two(b=self.fresh())
        self.assertEqual([c["alert"] for c in got], ["sent"])
        kind, p = self.send.sent[0]
        self.assertEqual(kind, "clash")
        self.assertTrue(p["title"].startswith("Clash "), p["title"])
        self.assertIn("your calendar and your calendar", p["message"])
        self.sweep()                                         # a re-run: the kept clash isn't new
        self.assertEqual(len(self.send.sent), 1)

    def test_first_sync_of_an_old_calendar_sends_nothing(self):
        old = {"updated": gts(self.now[0] - 30 * 86400)}
        a = dict(ev("a1", "Budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00"), **old)
        b = dict(ev("b1", "Dentist", "2027-03-02T10:30-08:00", "2027-03-02T11:30-08:00"), **old)
        c = dict(ev("c1", "Standup", "2027-03-04T09:00-08:00", "2027-03-04T09:30-08:00"))   # no `updated` at all
        d = dict(ev("d1", "School run", "2027-03-04T09:15-08:00", "2027-03-04T09:45-08:00"))
        self.put(a, b, c, d)
        res = self.sweep()
        self.assertEqual(len(res["pairs_new"]), 2)           # shown on Today
        self.assertEqual(res["alerts"], [])
        self.assertEqual(self.send.sent, [])
        self.assertEqual([c["alert"] for c in self.pairs()], ["", ""])

    def test_just_over_six_hours_is_backfill(self):
        self.assertEqual([c["alert"] for c in self.two(b=self.fresh(6 * 3600 + 60))], [""])
        self.assertEqual(self.send.sent, [])

    def test_maybe_clash_alerts(self):
        got = self.two(b=dict(self.fresh(), attendees=[att("tentative"), guest("Ana Ruiz")],
                              organizer="ana@work.example"))
        self.assertEqual([(c["verdict"], c["alert"]) for c in got], [("maybe", "sent")])
        self.assertTrue(self.send.sent[0][1]["title"].startswith("Maybe clash"))

    def test_overlap_already_over_never_alerts(self):
        self.now[0] = ts("2027-03-02T11:20-08:00")           # still shown (grace), but over
        got = self.two(b=self.fresh())
        self.assertEqual([c["alert"] for c in got], [""])
        self.assertEqual(self.send.sent, [])

    def test_quiet_hours_hold_then_flush(self):
        self.now[0] = ts("2027-03-01T23:00-08:00")
        self.assertEqual([c["alert"] for c in self.two(b=self.fresh())], ["held"])
        self.assertEqual(self.send.sent, [])
        self.now[0] = ts("2027-03-02T07:30-08:00")
        self.assertEqual([x[2] for x in self.al.flush()], ["sent"])

    def test_clash_alerts_off(self):
        self.cfg["clashes"] = False
        self.assertEqual([c["alert"] for c in self.two(b=self.fresh())], ["off"])
        self.assertEqual(self.send.sent, [])

    def test_today_recheck_alerts(self):
        t = today_mod.Today(self.ms, self.lv, lambda: self.now[0], alerter=self.al)
        self.put(ev("a1", "Budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00"),
                 dict(ev("b1", "Dentist", "2027-03-02T10:30-08:00", "2027-03-02T11:30-08:00"), **self.fresh()))
        res = t.recheck()
        self.assertEqual([x[2] for x in res["alerts"]], ["sent"])


if __name__ == "__main__":
    unittest.main()
