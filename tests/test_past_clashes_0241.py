"""0.24.1 (the owner on 0.24.0: "I'm getting inundated with clashes for dates in the past"): a clash is made or kept
open only while its overlap hasn't ended (30 minutes' grace for one happening now); open ones whose time passed resolve
"In the past" at the sweep (history line, no alert, no Today row); no phone alert for a past clash or one a backfill
made; the Day strip, heads-ups, ask cards and RSVP asks leave past ones out. Fictional people; no model server."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import TZ, item, ts, when  # noqa: E402
from test_need_0230 import NeedBase, att, ev, guest  # noqa: E402

import agenda  # noqa: E402
import api  # noqa: E402
import brief2  # noqa: E402
import conflicts  # noqa: E402
import ledger as L  # noqa: E402
import today  # noqa: E402


class PastBase(NeedBase):
    def alerter(self, sent):
        return conflicts.Alerter(self.ms, self.lv, lambda: {"enabled": True, "targets": ["phone"], "quiet_start": "00:00", "quiet_end": "00:00"},
                                 lambda p, t, k=None: (sent.append(p), (1, []))[1],
                                 lambda t, m, tag=None, urgent=False: {"title": t, "message": m},
                                 clock=lambda: self.now[0])

    def sweep(self):
        return self.ms.write(lambda db: conflicts.sweep(db, self.now[0], L.zone(TZ)))

    def call_at(self, eid, start, end):
        self.put(ev(eid, "Call with Jonas Weber", start, end, organizer="jonas@work.example",
                    attendees=[att(), guest("Jonas Weber")]))

    def all_clashes(self):
        return self.lv.conflicts(state=None)


class TestNoPastClash(PastBase):
    def test_a_past_overlap_makes_no_clash(self):
        self.call_at("j1", "2027-02-20T18:00-08:00", "2027-02-20T18:30-08:00")
        self.mine("o1", "rowing practice", when("Sat 6 pm", "2027-02-20", "18:00", "19:30"))
        self.assertEqual(self.all_clashes(), [])
        self.sweep()                                     # the re-run over old items makes none either
        self.assertEqual(self.all_clashes(), [])

    def test_the_same_overlap_ahead_is_a_clash(self):
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        self.assertEqual(self.verdicts(), [("clash", "overlap")])


class TestPassing(PastBase):
    def setUp(self):
        super().setUp()
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.oid = self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        self.cid = self.lv.conflicts()[0]["id"]

    def test_an_open_clash_whose_time_passes_resolves_at_the_sweep_with_no_alert(self):
        sent = []
        al = self.alerter(sent)
        self.ms.write(lambda db: db.execute("UPDATE ledger_conflicts SET alert='held' WHERE id=?", (self.cid,)))
        self.now[0] = ts("2027-03-02T19:05-08:00")
        out = self.sweep()
        self.assertIn(self.cid, out["resolved"])
        self.assertEqual(out["new"], [])
        c = next(c for c in self.all_clashes() if c["id"] == self.cid)
        self.assertEqual((c["state"], c["resolved_why"]), ("resolved", "In the past"))
        hist = self.ms._read("SELECT kind, note FROM ledger_history WHERE item=? AND kind='clash_resolved'",  # noqa: SLF001
                             (self.oid,))
        self.assertEqual(len(hist), 1)
        self.assertIn("In the past", hist[0]["note"])
        self.assertEqual(al.flush(), [])                 # the held alert never goes out
        self.assertEqual(al.notify({"conflicts": {"new": [self.cid]}}), [])
        self.assertEqual(sent, [])
        t = self.td()
        self.assertEqual(t._why_cleared(dict(c), self.now[0]), "")     # no "Cleared …" row on Today
        day0 = L.day_bounds(today.local(self.now[0], L.zone(TZ)).date(), L.zone(TZ))[0]
        self.assertEqual(t._cleared(day0, self.now[0], L.zone(TZ)), [])

    def test_happening_now_within_the_grace_stays(self):
        self.now[0] = ts("2027-03-02T18:50-08:00")       # the overlap ended 20 minutes ago
        self.sweep()
        self.ms.write(lambda db: conflicts.check_item(db, self.oid, self.now[0], L.zone(TZ)))
        self.assertEqual([c["id"] for c in self.lv.conflicts()], [self.cid])
        self.now[0] = ts("2027-03-02T19:01-08:00")       # 31 minutes
        self.ms.write(lambda db: conflicts.check_item(db, self.oid, self.now[0], L.zone(TZ)))
        self.assertEqual(self.lv.conflicts(), [])
        c = self.all_clashes()[0]
        self.assertEqual(c["resolved_why"], "In the past")

    def test_resolve_past_runs_alone_at_start(self):
        self.now[0] = ts("2027-03-03T08:00-08:00")
        got = self.ms.write(lambda db: conflicts.resolve_past(db, self.now[0]))
        self.assertEqual(got, [self.cid])
        self.assertEqual(self.ms.write(lambda db: conflicts.resolve_past(db, self.now[0])), [])

    def test_the_day_strip_leaves_out_a_clash_that_is_over(self):
        t = self.td()
        day = today.local(ts("2027-03-02T12:00-08:00"), L.zone(TZ)).date()
        before = t.day_lanes(day, now=ts("2027-03-02T12:00-08:00"), tz=L.zone(TZ), clashes=[])
        self.assertEqual(len(before["clashes"]), 1)
        after = t.day_lanes(day, now=ts("2027-03-02T19:00-08:00"), tz=L.zone(TZ), clashes=[])
        self.assertEqual(after["clashes"], [])
        self.assertNotIn("clash", after["line"])


class TestAlertsOnlyLive(PastBase):
    def test_a_backfill_rerun_creates_no_alerts(self):
        sent = []
        al = self.alerter(sent)
        self.call_at("j1", "2027-03-10T18:00-08:00", "2027-03-10T18:30-08:00")
        # an old message (read in the catch-up two days after it came) whose clash is still ahead
        self.mine("o1", "rowing practice", when("Wed 6 pm", "2027-03-10", "18:00", "19:30"), at="2027-03-01T08:00-08:00")
        ids = [c["id"] for c in self.lv.conflicts()]
        self.assertEqual(len(ids), 1)
        self.now[0] = ts("2027-03-03T08:00-08:00")
        self.assertEqual(al.notify({"conflicts": {"new": ids}}), [])
        # the sweep's re-run over old items alerts nothing either (and makes nothing new)
        self.assertEqual(self.sweep()["new"], [])
        self.assertEqual(sent, [])

    def test_a_live_clash_still_alerts(self):
        sent = []
        al = self.alerter(sent)
        self.call_at("j1", "2027-03-10T18:00-08:00", "2027-03-10T18:30-08:00")
        self.mine("o1", "rowing practice", when("Wed 6 pm", "2027-03-10", "18:00", "19:30"), at="2027-03-01T12:00-08:00")
        ids = [c["id"] for c in self.lv.conflicts()]
        out = al.notify({"conflicts": {"new": ids}})
        self.assertEqual([o[0] for o in out], ["clash"])
        self.assertEqual(len(sent), 1)

    def test_a_past_clash_never_alerts(self):
        sent = []
        al = self.alerter(sent)
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        ids = [c["id"] for c in self.lv.conflicts()]
        self.now[0] = ts("2027-03-02T18:40-08:00")       # still open (grace) but its time has passed
        self.assertEqual(al.notify({"conflicts": {"new": ids}}), [])
        self.assertEqual(sent, [])


    def test_a_reminder_for_a_clash_that_is_over_is_not_pushed(self):
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        cid = self.lv.conflicts()[0]["id"]
        t = self.td()                                    # makes Today's tables
        self.ms.write(lambda db: db.execute("INSERT INTO today_reminders (conflict, due_ts, state, created_at) "
                                            "VALUES (?,?,?,?)", (cid, ts("2027-03-02T18:35-08:00"), "open", self.now[0])))
        pushed = []
        t._push = lambda title, msg, tag: (pushed.append(title), "sent")[1]
        self.now[0] = ts("2027-03-02T18:40-08:00")       # still open (the grace), but over
        t.tick(now=self.now[0])
        self.assertEqual(pushed, [])
        st = self.ms._read("SELECT state FROM today_reminders WHERE conflict=?", (cid,))[0]["state"]  # noqa: SLF001
        self.assertEqual(st, "cleared")

class TestTripGroup(PastBase):
    def setUp(self):
        super().setUp()
        self.put(ev("t1", "Sprint planning", "2027-03-05T10:00-08:00", "2027-03-05T11:00-08:00",
                    organizer="lead@work.example", attendees=[att(), guest("Ada Lead")]),
                 ev("t2", "Regatta volunteer shift", "2027-03-06T09:00-08:00", "2027-03-06T12:00-08:00",
                    attendees=[att("accepted"), guest("Dana Reyes")]))
        self.trip = self.mine("o1", "trip to Lisbon", when("Fri to Mon", "2027-03-05", end_date="2027-03-08",
                                                           all_day=True), type="event", subtype="trip")
        cs = [c for c in self.lv.conflicts() if c["entries"]]
        self.assertEqual(len(cs), 1)
        self.gid = cs[0]["id"]

    def test_past_entries_leave_the_group(self):
        self.now[0] = ts("2027-03-05T12:00-08:00")       # Sprint planning is over; the regatta shift is ahead
        self.ms.write(lambda db: conflicts.check_item(db, self.trip, self.now[0], L.zone(TZ)))
        cs = [c for c in self.lv.conflicts() if c["entries"]]
        self.assertEqual([e["title"] for e in cs[0]["entries"]], ["Regatta volunteer shift"])

    def test_the_group_with_every_entry_past_resolves(self):
        self.now[0] = ts("2027-03-07T09:00-08:00")       # the trip goes on; both entries are over
        self.ms.write(lambda db: conflicts.check_item(db, self.trip, self.now[0], L.zone(TZ)))
        c = next(c for c in self.all_clashes() if c["id"] == self.gid)
        self.assertEqual((c["state"], c["resolved_why"]), ("resolved", "In the past"))
        self.sweep()
        self.assertEqual(self.lv.conflicts(), [])


class TestMaybe(PastBase):
    def test_a_maybe_clash_in_the_past_is_not_made(self):
        self.call_at("j1", "2027-02-20T18:00-08:00", "2027-02-20T18:30-08:00")
        self.mine("o1", "rowing practice", when("Sat 6 pm", "2027-02-20", "18:00", "19:30"), conf="likely")
        self.assertEqual(self.all_clashes(), [])

    def test_a_maybe_clash_ahead_is_made_and_resolves_once_over(self):
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        oid = self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"), conf="likely")
        self.assertEqual(self.verdicts(), [("maybe", "overlap")])
        self.now[0] = ts("2027-03-02T19:05-08:00")
        self.ms.write(lambda db: conflicts.check_item(db, oid, self.now[0], L.zone(TZ)))
        self.assertEqual(self.lv.conflicts(), [])
        self.assertEqual(self.all_clashes()[0]["resolved_why"], "In the past")

    def test_only_the_candidate_time_ahead_clashes(self):
        self.call_at("j0", "2027-02-26T18:00-08:00", "2027-02-26T18:30-08:00")
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Fri or Tue at 6", options=["2027-02-26 18:00-19:30",
                                                                           "2027-03-02 18:00-19:30"]))
        cs = self.lv.conflicts()
        self.assertEqual(len(cs), 1)
        self.assertEqual((cs[0]["verdict"], cs[0]["candidate"]), ("maybe", 1))
        self.assertEqual(cs[0]["slot_start"], ts("2027-03-02T18:00-08:00"))


class TestScreens(PastBase):
    """Today's Clashes, Talk it through's Choose step and the Agenda's clash marks leave out a clash that is over (even
    while it is still open within the grace)."""

    def setUp(self):
        super().setUp()
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        self.day = today.local(ts("2027-03-02T12:00-08:00"), L.zone(TZ)).date()

    def screens(self, at):
        self.now[0] = ts(at)
        t = self.td()
        v = t.view(now=self.now[0])
        step = brief2.Planner(self.ms, self.lv, t)._clashes(v)
        ag = agenda.Agenda(self.ms, self.lv, t, lambda: self.now[0]).view({"view": "day", "date": self.day.isoformat()})
        return (len(v["now_next"]["clashes"]), step["count"] if step else 0, len(ag["day"]["lanes"]["clashes"]),
                sum(1 for r in ag["day"]["rows"] if r.get("kind") == "clash" or r.get("clash")), ag["header"]["clashes"])

    def test_ahead_it_shows_everywhere(self):
        got = self.screens("2027-03-02T12:00-08:00")
        self.assertEqual(got[:3] + got[4:], (1, 1, 1, 1))
        self.assertGreater(got[3], 0)

    def test_once_over_it_shows_nowhere(self):
        got = self.screens("2027-03-02T18:45-08:00")
        self.assertEqual(len(self.lv.conflicts()), 1)        # still open (the grace), but over
        self.assertEqual(got, (0, 0, 0, 0, 0))


class TestStartup(PastBase):
    def test_the_app_closes_past_clashes_once_at_start(self):
        self.call_at("j1", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        cid = self.lv.conflicts()[0]["id"]
        self.ms.close()
        self.now[0] = ts("2027-03-04T08:00-08:00")
        app = api.App(self.tmp.name, clock=lambda: self.now[0])
        self.addCleanup(lambda: getattr(app.chat, "close", lambda: None)())
        self.ms = app.messages
        c = self.ms._read("SELECT state, resolved_why, alert FROM ledger_conflicts WHERE id=?", (cid,))[0]  # noqa: SLF001
        self.assertEqual((c["state"], c["resolved_why"], c["alert"]), ("resolved", "In the past", ""))


class TestPastAsks(PastBase):
    def test_an_ask_card_for_a_past_event_is_not_shown(self):
        self.put(ev("q1", "Planning session", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00", cal="family",
                    organizer="ada@work.example"),
                 ev("q3", "Coffee chat", "2027-03-02T15:00-08:00", "2027-03-02T16:00-08:00", cal="family",
                    organizer="bo@work.example"))
        self.assertTrue(self.need("q1")["ask"])
        t = self.td()
        early = t.headsup(now=ts("2027-03-02T09:00-08:00"))
        self.assertEqual(sorted(a["title"] for a in early["asks"]), ["Coffee chat", "Planning session"])
        late = t.headsup(now=ts("2027-03-02T12:00-08:00"))
        self.assertEqual([a["title"] for a in late["asks"]], ["Coffee chat"])

    def test_a_headsup_for_a_past_event_is_not_shown(self):
        self.put(ev("g1", "Fernhill early dismissal", "2027-03-02T12:30-08:00", "2027-03-02T13:00-08:00", cal="school"))
        t = self.td()
        self.assertEqual(t.headsup(now=ts("2027-03-02T12:00-08:00"))["count"], 1)
        self.assertEqual(t.headsup(now=ts("2027-03-02T14:00-08:00"))["count"], 0)

    def test_no_rsvp_ask_for_a_past_invite(self):
        iid = self.read("m1", "2027-03-01T09:00-08:00", {"items": [item(
            "ask", "sender", "Kai's launch party", when("Tue 6 pm", "2027-03-02", "18:00", "20:00"), subtype="invite")],
            "updates": []}, sender="kai@work.example", name="Kai", source="gmail", text="Join us Tuesday?")["items"][0]
        it = self.ms.write(lambda db: L.get_item(db, iid))
        self.assertEqual(it["subtype"], "invite")
        self.assertEqual(L.section_of(it, ts("2027-03-02T12:00-08:00"), L.zone(TZ)), "needs_you")
        self.assertEqual(L.section_of(it, ts("2027-03-02T21:00-08:00"), L.zone(TZ)), "none")


if __name__ == "__main__":
    unittest.main()
