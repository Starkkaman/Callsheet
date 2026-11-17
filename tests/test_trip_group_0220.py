"""0.22.x: the owner's trip or stay of more than a day against the busy timed calendar entries during it is ONE grouped clash
(the acceptance rules for the owner's rule 2026-11-13: a dentist visit or haircut during a trip is flagged, without
a clash per entry):
- one likely clash per trip that lists each overlapped entry by id, in time order; free, all-day and declined entries
  and the trip's own travel entry stay out;
- an entry that clears (declined in Calendar, its invite declined, cancelled) leaves the list, no separate clash
  opens; when none is left the group resolves; clashes per entry from before the ruling fold into the group;
- Today shows the group as one row with the entries listed when it is open (12-hour times);
- scorer 2.3: a grouped clash is one clash item, matched against `entries`; a missing or extra entry makes that clash
  wrong, never a separate missed or extra clash.
Synthetic fixtures (fictional people and places); in-process fakes only: no model server, no network."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, when  # noqa: E402
from test_ledger_mailfix_0220 import GUEST, day_event, event  # noqa: E402

import agenda  # noqa: E402
import chatstore  # noqa: E402
import conflicts as C  # noqa: E402
import heads  # noqa: E402
import ledger as L  # noqa: E402
import msgstore  # noqa: E402
import replay_ledger as R  # noqa: E402
import replydraft  # noqa: E402
import rsvp  # noqa: E402
import today  # noqa: E402

DECLINED = [GUEST[0], {"name": "", "address": "me", "response": "declined", "self": True}]
PENDING = [GUEST[0], {"name": "", "address": "me", "response": "needsAction", "self": True}]


class TripGroupBase(Base):
    def stay(self, mid="h1", conf="sure"):
        self.read(mid, "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", "stay at Maple Inn", when("Check-in Mar 8, check-out Mar 10", "2027-03-08",
                                                          end_date="2027-03-10", all_day=True), subtype="trip",
            conf=conf)],
            "updates": []}, source="gmail", sender="reservations@mapleinn.example", name="Maple Inn",
            thread="mail-h1")
        return next(i for i in self.items().values() if i["subtype"] == "trip")["id"]

    def three(self, **kw):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=GUEST, **kw),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00"),
            event("e3", "Haircut", "2027-03-10T16:00-08:00", "2027-03-10T16:45-08:00")])

    def ev(self, eid):
        return self.ms._read("SELECT id FROM events WHERE event_id=?", (eid,))[0]["id"]  # noqa: SLF001

    def open_rows(self):
        return self.ms._read("SELECT * FROM ledger_conflicts WHERE state='open'")  # noqa: SLF001

    def sweep(self):
        return self.ms.write(lambda db: C.sweep(db, self.now[0], L.zone("America/Los_Angeles")))


class TestGrouping(TripGroupBase):
    def test_one_likely_clash_lists_each_entry_in_time_order(self):
        self.three()
        trip = self.stay(conf="likely")                          # M4: a likely stay makes the group likely
        rows = self.open_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["a_kind"], r["a_ref"], r["kind"], r["confidence"]), ("item", trip, "away", "likely"))
        self.assertEqual(L.group_entries(r), [self.ev("e2"), self.ev("e1"), self.ev("e3")])
        self.assertEqual(r["b_ref"], self.ev("e2"))                  # the first entry
        c = self.lv.conflicts()[0]
        self.assertEqual([e["title"] for e in c["entries"]], ["Dentist", "Design review", "Haircut"])
        self.assertIn("3 calendar entries", c["why"])

    def test_exclusions_free_all_day_declined_travel_and_outside(self):
        self.ms.upsert_events([
            event("e1", "Dentist", "2027-03-09T14:00-08:00", "2027-03-09T15:00-08:00"),
            event("x1", "Focus time", "2027-03-09T09:00-08:00", "2027-03-09T10:00-08:00", transparency="transparent"),
            day_event("x2", "Studio build", "2027-03-09"),
            event("x3", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=DECLINED),
            event("x4", "Flight to Tromsø", "2027-03-08T07:00-08:00", "2027-03-08T09:00-08:00"),
            event("x5", "Haircut", "2027-03-12T16:00-08:00", "2027-03-12T16:45-08:00"),
            event("x6", "Old plan", "2027-03-09T12:00-08:00", "2027-03-09T13:00-08:00", status="cancelled")])
        self.stay()
        rows = self.open_rows()
        self.assertEqual([L.group_entries(r) for r in rows], [[self.ev("e1")]])

    def test_only_excluded_entries_make_no_clash(self):
        self.ms.upsert_events([
            event("x1", "Focus time", "2027-03-09T09:00-08:00", "2027-03-09T10:00-08:00", transparency="transparent"),
            event("x3", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=DECLINED)])
        self.stay()
        self.assertEqual(self.open_rows(), [])

    def test_an_invite_for_an_entry_during_it_joins_the_group(self):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=PENDING),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00")])
        self.stay()
        self.ms.write(lambda db: heads.sync_invites(db, self.now[0], L.zone("America/Los_Angeles")))
        self.assertTrue(any(i["subtype"] == "invite" for i in self.items().values()))
        rows = self.open_rows()                                  # no clash of the invite's own with the trip
        self.assertEqual([sorted(L.group_entries(r)) for r in rows], [sorted([self.ev("e1"), self.ev("e2")])])


class TestEntriesClear(TripGroupBase):
    def test_declining_one_entry_updates_the_group(self):
        self.three()
        self.stay()
        gid = self.open_rows()[0]["id"]
        self.ms.upsert_events([event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00",
                                     attendees=DECLINED)])
        res = self.sweep()
        self.assertEqual(res["new"], [])                         # nothing reopens as its own clash
        rows = self.open_rows()
        self.assertEqual([(r["id"], L.group_entries(r)) for r in rows], [(gid, [self.ev("e2"), self.ev("e3")])])

    def test_declining_its_invite_updates_the_group_at_once(self):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=PENDING),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00")])
        self.stay()
        tz = L.zone("America/Los_Angeles")
        self.ms.write(lambda db: heads.sync_invites(db, self.now[0], tz))
        gid = self.open_rows()[0]["id"]
        inv = next(i for i in self.items().values() if i["subtype"] == "invite")
        self.ms.write(lambda db: (L.set_status(db, inv["id"], "declined", self.now[0], note="you said no"),
                                  C.check_item(db, inv["id"], self.now[0], tz)))
        rows = self.open_rows()                                  # no sweep: the invite's check re-checks the trip
        self.assertEqual([(r["id"], L.group_entries(r)) for r in rows], [(gid, [self.ev("e2")])])

    def test_moving_one_entry_out_of_the_trip_updates_the_group(self):
        self.three()
        self.stay()
        self.ms.upsert_events([event("e3", "Haircut", "2027-03-12T16:00-08:00", "2027-03-12T16:45-08:00")])
        self.sweep()
        self.assertEqual([L.group_entries(r) for r in self.open_rows()], [[self.ev("e2"), self.ev("e1")]])

    def test_when_none_is_left_the_group_resolves(self):
        self.three()
        self.stay()
        gid = self.open_rows()[0]["id"]
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=DECLINED),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00", status="cancelled"),
            event("e3", "Haircut", "2027-03-10T16:00-08:00", "2027-03-10T16:45-08:00", transparency="transparent")])
        res = self.sweep()
        self.assertEqual((res["new"], self.open_rows()), ([], []))
        r = self.ms._read("SELECT * FROM ledger_conflicts WHERE id=?", (gid,))[0]  # noqa: SLF001
        self.assertEqual((r["state"], r["resolved_why"]), ("resolved", "entries cleared"))

    def test_a_past_trips_group_drops_cancelled_entries_in_the_sweep(self):
        self.three()
        self.stay()
        gid = self.open_rows()[0]["id"]
        self.now[0] = ts("2027-03-20T09:00-08:00")               # the trip is over: no re-check of it
        self.ms.upsert_events([event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00",
                                     status="cancelled")])
        self.sweep()
        r = self.ms._read("SELECT * FROM ledger_conflicts WHERE id=?", (gid,))[0]  # noqa: SLF001
        self.assertEqual((r["state"], L.group_entries(r), r["b_ref"]),
                         ("open", [self.ev("e1"), self.ev("e3")], self.ev("e1")))

    def test_clashes_per_entry_from_before_the_ruling_fold_into_the_group(self):
        self.three()
        trip = self.stay()
        tz = L.zone("America/Los_Angeles")

        def legacy(db):
            db.execute("DELETE FROM ledger_conflicts")
            for e in ("e1", "e2"):
                db.execute("INSERT INTO ledger_conflicts (a_kind, a_ref, b_kind, b_ref, kind, confidence, why, state, "
                           "created_at, updated_at, alert) VALUES ('item',?,'event',?,'away','likely','x','open',?,?,"
                           "'sent')", (trip, self.ev(e), self.now[0], self.now[0]))
            return C.check_item(db, trip, self.now[0], tz)
        new, resolved = self.ms.write(legacy)
        self.assertEqual(new, [])                                # no second alert
        rows = self.open_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["alert"], len(L.group_entries(rows[0]))), ("sent", 3))
        self.assertEqual([r["resolved_why"] for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM ledger_conflicts WHERE state='resolved'")], ["grouped"])
        self.assertEqual(len(resolved), 1)


class TestTodayRow(TripGroupBase):
    def setUp(self):
        super().setUp()
        self.td = today.Today(self.ms, self.lv, lambda: self.now[0])

    def test_one_row_with_its_entries_when_open(self):
        self.three()
        self.stay()
        self.now[0] = ts("2027-03-07T09:00-08:00")
        v = self.td.view()
        cl = v["now_next"]["clashes"]
        self.assertEqual(len(cl), 1)
        g = cl[0]["group"]
        self.assertEqual(g["plain"], "Your stay at Maple Inn overlaps 3 entries")
        self.assertEqual(g["title"], [["Your stay at Maple Inn", True], [" overlaps 3 entries", False]])
        self.assertEqual(cl[0]["line"], g["title"])
        self.assertEqual([e["text"] for e in g["entries"]], [
            [["Dentist", True], [", Tomorrow 2:00 pm to 3:00 pm", False]],
            [["Design review", True], [", Tue 10:00 am to 11:00 am", False]],
            [["Haircut", True], [", Wed 4:00 pm to 4:45 pm", False]]])
        self.assertFalse(cl[0]["compact"])                       # near: open at first
        self.assertIsNone(cl[0]["reply"])

    def test_a_far_group_is_one_closed_row_and_the_clash_view_lists_them(self):
        self.three()
        self.stay()
        v = self.td.view()
        c = v["now_next"]["clashes"][0]
        self.assertTrue(c["compact"])
        d = self.td.clash_view(c["id"])
        self.assertEqual(d["title"], "Your stay at Maple Inn overlaps 3 entries")
        self.assertEqual([s["badge"] for s in d["stand"]][1:], ["On your calendar"] * 3)
        self.assertEqual(len(d["group"]["entries"]), 3)
        self.assertEqual(d["when"]["sub"], "From “Check-in Mar 8, check-out Mar 10” (Maple).")   # no "clear" line

    def test_the_page_draws_the_group_as_one_row(self):
        js = (pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app" / "static" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("c.group ? tdClashGroup(c, sec.length > 0)", js)
        body = js[js.index("function tdClashGroup"):js.index("function tdClashLine")]
        self.assertIn("'aria-expanded'", body)
        self.assertNotIn("innerHTML", body)

    def test_a_cleared_group_says_so_and_a_folded_one_is_quiet(self):
        self.assertEqual(self.td._why_cleared({"resolved_why": "grouped"}, self.now[0]), "")  # noqa: SLF001
        self.assertEqual(self.td._why_cleared({"resolved_why": "entries cleared"}, self.now[0]),  # noqa: SLF001
                         "nothing on your calendar during the trip now")

    def test_the_phone_alert_counts_the_entries(self):
        self.three()
        self.stay()
        c = self.lv.conflicts()[0]
        al = C.Alerter(self.ms, self.lv, lambda: {}, None, lambda t, m, tag, urgent: (t, m))
        title, msg = al.clash_payload(c)
        self.assertTrue(title.startswith("Trip clash"))
        self.assertTrue(msg.endswith("your trip overlaps 3 calendar entries."))


class TestEveryEntryEverywhere(TripGroupBase):
    """The Agenda, the reply draft's facts and Chat's clash list show the trip and EVERY entry of its grouped clash
    (they showed the trip and its first entry only)."""

    def setUp(self):
        super().setUp()
        self.td = today.Today(self.ms, self.lv, lambda: self.now[0])
        self.three()
        self.trip = self.stay()
        self.now[0] = ts("2027-03-07T09:00-08:00")

    def test_the_agenda_day_lists_the_trip_and_every_entry_with_its_time(self):
        ag = agenda.Agenda(self.ms, self.lv, self.td, lambda: self.now[0])
        v = ag.view({"view": "day", "date": "2027-03-08"})
        cl = [r for r in v["day"]["rows"] if r["kind"] == "clash"]
        self.assertEqual(len(cl), 1)
        c = cl[0]
        self.assertEqual(c["group"], "Your stay at Maple Inn overlaps 3 entries")
        self.assertEqual([b["title"] for b in c["sides"]], ["Stay at Maple Inn", "Dentist", "Design review",
                                                             "Haircut"])
        self.assertEqual([b["meta"] for b in c["sides"]][1:], ["Tomorrow 2:00 pm to 3:00 pm",
                                                                "Tue 10:00 am to 11:00 am", "Wed 4:00 pm to 4:45 pm"])
        self.assertEqual(sorted(c["events"]), sorted([self.ev("e1"), self.ev("e2"), self.ev("e3")]))
        days = [r for r in v["day"]["rows"] if r["kind"] != "free"]
        self.assertEqual([r["kind"] for r in days], ["clash"])     # the day's entry shows inside the clash, once

    def test_a_two_sided_clash_in_the_agenda_has_no_group(self):
        self.ms.upsert_events([event("e9", "Call", "2027-03-15T10:00-08:00", "2027-03-15T11:00-08:00")])
        self.read("a9", "2027-03-07T09:00-08:00", {"items": [item(what="sync", w=when(
            "Mon 10:30", "2027-03-15", "10:30", "11:30"))], "updates": []}, sender="p:owen", name="Owen")
        ag = agenda.Agenda(self.ms, self.lv, self.td, lambda: self.now[0])
        c = [r for r in ag.view({"view": "day", "date": "2027-03-15"})["day"]["rows"] if r["kind"] == "clash"][0]
        self.assertEqual((c["group"], len(c["sides"])), ("", 2))

    def test_the_page_says_open_for_a_group(self):
        js = (pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app" / "static" / "app.js").read_text(
            encoding="utf-8")
        body = js[js.index("function agClash"):js.index("function agRows")]
        self.assertIn("r.group ? 'Open' : 'Open both'", body)
        self.assertIn("r.sides.map(", body)

    def test_the_reply_draft_facts_list_every_entry_with_its_time(self):
        rd = replydraft.ReplyDrafts(self.ms, self.td, None, clock=lambda: self.now[0], threads=False)
        facts, _ = rd.facts(self.trip)
        self.assertEqual(len(facts["clashes"]), 1)
        self.assertEqual(facts["clashes"][0]["with"], [
            "Dentist on the calendar, Tomorrow 2:00 pm to 3:00 pm",
            "Design review on the calendar, Tue 10:00 am to 11:00 am",
            "Haircut on the calendar, Wed 4:00 pm to 4:45 pm"])
        self.assertEqual(facts["clashes"][0]["title"], "Your stay at Maple Inn overlaps 3 entries")

    def test_chat_lists_the_trip_and_every_entry(self):
        import chat  # noqa: PLC0415

        class Tools(chatstore.StoreTools):          # Chat's tools (chat.Tools) without the model and the store
            _only, _text = staticmethod(chat.Tools._only), staticmethod(chat.Tools._text)
        t = Tools()
        t.ledger, t.msgstore, t.clock = self.lv, self.ms, lambda: self.now[0]
        t.seen, t.seen_items, t.flagged = {}, set(), set()
        t._when = lambda x: f"at {int(x)}"                     # noqa: SLF001 (Chat's own clock words)
        t._chip = lambda r: {}                                  # noqa: SLF001
        out, label = t._ledger_list({"list": "conflicts"})      # noqa: SLF001
        self.assertEqual(label, "Looked at your clashes")
        c = out["conflicts"][0]
        self.assertEqual(c["entries"], 3)
        self.assertEqual(c["sides"][0]["item"], self.trip)
        evs = c["sides"][1:]
        self.assertEqual([s["title"] for s in evs], ["Dentist", "Design review", "Haircut"])
        self.assertEqual([s["event"] - msgstore.EVENT_ID_BASE for s in evs],
                         [self.ev("e2"), self.ev("e1"), self.ev("e3")])
        self.assertEqual([s["when"] for s in evs], [f"at {int(ts(x))}" for x in (
            "2027-03-08T14:00-08:00", "2027-03-09T10:00-08:00", "2027-03-10T16:00-08:00")])
        self.assertTrue({self.ev("e1"), self.ev("e2"), self.ev("e3")} <= {
            k - msgstore.EVENT_ID_BASE for k in t.seen})        # each entry can be cited


class _Cal:
    """A calendar writer with the grant (calwrite.CalWrite's surface rsvp.Rsvp uses), in process."""
    def __init__(self):
        self.store = type("S", (), {"account": staticmethod(lambda acc: {"email": "me@studio.example"})})()
        self.tokens = type("T", (), {"call": staticmethod(lambda acc, fn, *a: fn(*a))})()

    @staticmethod
    def granted(acc):
        return True


class _Api:
    def __init__(self):
        self.patched = []

    @staticmethod
    def calendar_event_get(cal, eid):
        return {"status": "confirmed", "organizer": {"email": "priya@studio.example"},
                "attendees": [{"email": "priya@studio.example", "responseStatus": "accepted"},
                              {"email": "me@studio.example", "self": True, "responseStatus": "needsAction"}]}

    def calendar_rsvp(self, cal, eid, new, att):
        self.patched.append((eid, [a.get("responseStatus") for a in new]))


class TestRsvpRegroupsAtOnce(TripGroupBase):
    """Answering an invite in Callsheet (rsvp.py) updates the owner's trip's grouped clash at once, not at the next sweep."""

    def setUp(self):
        super().setUp()
        self.tz = L.zone("America/Los_Angeles")
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=PENDING),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00")])
        self.stay()
        self.ms.write(lambda db: heads.sync_invites(db, self.now[0], self.tz))
        self.inv = next(i for i in self.items().values() if i["subtype"] == "invite")["id"]
        self.gid = self.open_rows()[0]["id"]
        self.api = _Api()
        self.rs = rsvp.Rsvp(self.ms, _Cal(), clock=lambda: self.now[0], api=self.api, tz=lambda: self.tz)

    def test_a_no_takes_the_entry_off_the_list_at_once(self):
        self.assertEqual(sorted(L.group_entries(self.open_rows()[0])), sorted([self.ev("e1"), self.ev("e2")]))
        res = self.rs.answer({"item": self.inv, "answer": "no"})
        self.assertEqual((res["word"], self.api.patched), ("No", [("e1", ["accepted", "declined"])]))
        rows = self.open_rows()                                  # no sweep
        self.assertEqual([(r["id"], L.group_entries(r)) for r in rows], [(self.gid, [self.ev("e2")])])

    def test_a_yes_after_a_no_puts_it_back(self):
        self.rs.answer({"item": self.inv, "answer": "no"})
        self.rs.answer({"event": self.ev("e1"), "answer": "yes"})
        rows = self.open_rows()
        self.assertEqual([(r["id"], L.group_entries(r)) for r in rows],
                         [(self.gid, [self.ev("e2"), self.ev("e1")])])

    def test_a_no_on_the_only_entry_clears_the_group(self):
        self.ms.upsert_events([event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00",
                                     status="cancelled")])
        self.sweep()
        self.assertEqual(L.group_entries(self.open_rows()[0]), [self.ev("e1")])
        self.rs.answer({"item": self.inv, "answer": "no"})
        self.assertEqual(self.open_rows(), [])
        r = self.ms._read("SELECT * FROM ledger_conflicts WHERE id=?", (self.gid,))[0]  # noqa: SLF001
        self.assertEqual((r["state"], r["resolved_why"]), ("resolved", "entries cleared"))

    def test_an_entry_outside_any_trip_touches_no_clash(self):
        self.ms.upsert_events([event("e5", "Pitch", "2027-03-15T10:00-08:00", "2027-03-15T11:00-08:00",
                                     attendees=PENDING)])
        before = [dict(r) for r in self.ms._read("SELECT * FROM ledger_conflicts")]  # noqa: SLF001
        self.assertEqual(self.ms.write(lambda db: C.regroup_event(db, self.ev("e5"), self.now[0], self.tz)), ([], []))
        self.assertEqual([dict(r) for r in self.ms._read("SELECT * FROM ledger_conflicts")], before)  # noqa: SLF001

    def test_the_api_passes_the_ledger_zone(self):
        api = (pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app" / "api.py").read_text()
        self.assertIn("rsvp_mod.Rsvp(self.messages, self.calwrite, clock=self.clock, tz=self.ledger.tz)", api)


def _case(conflicts, extra_cal=()):
    """A synthetic S-CLASH-21-like case: a hotel booking Mar 8-10 and two busy entries on Mar 9."""
    return {"case": "S-TG-01", "set": "synthetic", "set_version": "1.4", "replay": "isolated", "owner": "Jordan Vale",
            "owner_tz": "America/Los_Angeles", "patterns": ["trip"], "updates": [], "follow_ups": [], "links": [],
            "no_items": False, "unjudged": [], "conflicts": conflicts,
            "calendar": [{"id": "cal:1", "title": "Design review", "all_day": False, "status": "confirmed",
                          "start": "2027-03-09T10:00-08:00", "end": "2027-03-09T11:00-08:00",
                          "attendees": ["p:priya"], "rsvp": "needsAction"},
                         {"id": "cal:2", "title": "Dinner", "all_day": False, "status": "confirmed",
                          "start": "2027-03-09T18:30-08:00", "end": "2027-03-09T19:30-08:00"}] + list(extra_cal),
            "messages": [{"mid": "S-TG-01:m1", "source": "mail", "direction": "in", "sender": "p:auto",
                          "sender_name": "Maple Inn", "to": ["owner"], "chat": "mail:mapleinn",
                          "chat_kind": "thread", "ts": "2027-03-01T09:00-08:00",
                          "subject": "Your stay at Maple Inn is confirmed",
                          "text": "Check-in: Mon, Mar 8. Check-out: Wed, Mar 10.", "automated_sender": True,
                          "role_sender": True}],
            "expect": [{"eid": "S-TG-01:e1", "type": "event", "subtype": "trip", "who": "p:auto",
                        "what": "stay at Maple Inn",
                        "when": {"tz": "America/Los_Angeles", "phrase": "Check-in: Mon, Mar 8. Check-out: Wed, Mar 10",
                                 "all_day": True, "approx": False, "start_date": "2027-03-08",
                                 "end_date": "2027-03-10"},
                        "status": "open", "section": "coming_up", "after": "S-TG-01:m1", "confidence": "sure"}]}


def _group(entries=None, b=None, conf="sure"):
    x = {"a": "S-TG-01:e1", "after": "S-TG-01:m1", "confidence": conf, "surface_within_s": 300, "kind": "overlap"}
    if entries is not None:
        x["entries"] = entries
    if b is not None:
        x["b"] = b
    return x


class TestScorerGroups(unittest.TestCase):
    def run_case(self, case):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "cases.jsonl")
            p.write_text(json.dumps(case) + "\n", encoding="utf-8")
            det = pathlib.Path(d, "detail.json")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = R.main(["--cases", str(p), "--quiet", "--detail", str(det)])
            return code, json.loads(out.getvalue()), json.loads(det.read_text(encoding="utf-8"))

    def clashes(self, s):
        return s["metrics"]["all"]["sure"]["clashes"]

    def test_the_version(self):
        self.assertEqual(R.SCORER_VERSION, "2.4")

    def test_the_exact_entries_are_one_clash_item(self):
        code, s, fails = self.run_case(_case([_group(["cal:1", "cal:2"])]))
        self.assertEqual((code, s["gate"], fails), (0, "pass", []))
        self.assertEqual(self.clashes(s), {"n": 1, "ok": 1, "pct": 100.0})
        self.assertEqual(s["clash_groups"], {"n": 1, "exact": 1, "partial": 0, "not_flagged": 0,
                                             "missing_entries": 0, "extra_entries": 0})
        self.assertEqual(s["false"].get("extra_clashes", 0), 0)

    def test_an_extra_entry_makes_that_clash_wrong_not_an_extra_clash(self):
        code, s, fails = self.run_case(_case([_group(["cal:1"])]))
        self.assertEqual((code, s["missed"]["clashes"]), (1, 1))
        self.assertEqual(self.clashes(s), {"n": 1, "ok": 0, "pct": 0.0})
        self.assertEqual((s["clash_groups"]["partial"], s["clash_groups"]["extra_entries"]), (1, 1))
        self.assertEqual(s["false"].get("extra_clashes", 0), 0)
        self.assertEqual([(f["metric"], f["why"]) for f in fails],
                         [("clashes", "group entries wrong: missing [], extra ['cal:2']")])

    def test_a_missing_entry_makes_that_clash_wrong_not_a_missed_clash_more(self):
        out = {"id": "cal:3", "title": "Haircut", "all_day": False, "status": "confirmed",
               "start": "2027-03-12T16:00-08:00", "end": "2027-03-12T16:45-08:00"}
        code, s, fails = self.run_case(_case([_group(["cal:1", "cal:2", "cal:3"])], [out]))
        self.assertEqual((self.clashes(s)["n"], self.clashes(s)["ok"]), (1, 0))   # one clash item, wrong
        self.assertEqual((s["clash_groups"]["partial"], s["clash_groups"]["missing_entries"]), (1, 1))
        self.assertEqual(s["false"].get("extra_clashes", 0), 0)
        self.assertEqual(len(fails), 1)

    def test_a_pair_expectation_met_by_a_group_reads_as_its_one_entry(self):
        code, s, fails = self.run_case(_case([_group(b="cal:1", conf="likely")]))
        self.assertEqual(code, 0)                                # likely: no sure bar
        self.assertEqual(s["clash_groups"]["extra_entries"], 1)
        self.assertEqual(s["false"].get("extra_clashes", 0), 0)
        self.assertEqual([f["metric"] for f in fails], ["clashes"])

    def test_no_group_is_not_flagged(self):
        case = _case([_group(["cal:1", "cal:2"])])
        for e in case["calendar"]:
            e["transparency"] = "transparent"
        code, s, fails = self.run_case(case)
        self.assertEqual((code, s["clash_groups"]["not_flagged"]), (1, 1))
        self.assertEqual([f["why"] for f in fails], ["clash not flagged"])

    def test_an_unexpected_group_is_one_extra_clash(self):
        case = _case([])
        code, s, fails = self.run_case(case)
        self.assertEqual(s["false"]["extra_clashes"], 1)         # not one per entry
        self.assertEqual([f["metric"] for f in fails], ["extra_clash"])


if __name__ == "__main__":
    unittest.main()
