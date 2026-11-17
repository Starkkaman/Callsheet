"""Acceptance rules M3 and M4 (2026-11-13) in the clash code:
- M3: the owner's time-off or "out" block of more than a day (a shoot, a job, travel, time off / out of office) works like a
  trip: ONE grouped clash with the busy timed calendar entries and the open invites during it, with the trip's
  exclusions; a friend's block, a one-day block and an ask are not the owner's out blocks;
- M4: a grouped clash is likely unless every item in it is sure (the block, not tentative, and each entry: a
  tentative entry, the owner's maybe, or a likely item standing for the entry makes it likely).
Synthetic fixtures (fictional people and places); in-process fakes only: no model server, no network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, when  # noqa: E402
from test_ledger_mailfix_0220 import GUEST, event  # noqa: E402

import conflicts as C  # noqa: E402
import heads  # noqa: E402
import ledger as L  # noqa: E402

TZ = L.zone("America/Los_Angeles")
PENDING = [GUEST[0], {"name": "", "address": "me", "response": "needsAction", "self": True}]
MAYBE = [GUEST[0], {"name": "", "address": "me", "response": "tentative", "self": True}]
DECLINED = [GUEST[0], {"name": "", "address": "me", "response": "declined", "self": True}]
DAYS = when("Mar 8 to Mar 10", "2027-03-08", end_date="2027-03-10", all_day=True)


class OutBase(Base):
    def entries(self, **kw):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", **dict(
                {"attendees": GUEST}, **kw)),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00"),
            event("x1", "Focus time", "2027-03-09T09:00-08:00", "2027-03-09T10:00-08:00", transparency="transparent"),
            event("x2", "Flight to Tromsø", "2027-03-08T07:00-08:00", "2027-03-08T09:00-08:00"),
            event("x3", "Haircut", "2027-03-12T16:00-08:00", "2027-03-12T16:45-08:00")])

    def mine(self, what="off work, out of office", typ="event", subtype="", conf="sure", w=None, mid="o1"):
        """The owner's own block, in their own outgoing message."""
        self.read(mid, "2027-03-01T09:00-08:00", {"items": [item(typ, "owner", what, w or DAYS, conf=conf,
                                                                 subtype=subtype, status="open")], "updates": []},
                  direction="out", thread="t-dana")
        return max(self.items())

    def ev(self, eid):
        return self.ms._read("SELECT id FROM events WHERE event_id=?", (eid,))[0]["id"]  # noqa: SLF001

    def open_rows(self):
        return self.ms._read("SELECT * FROM ledger_conflicts WHERE state='open'")  # noqa: SLF001


class TestM3OutBlocks(OutBase):
    def test_his_time_off_is_one_grouped_clash_like_a_trip(self):
        self.entries()
        block = self.mine()
        rows = self.open_rows()
        self.assertEqual([(r["a_kind"], r["a_ref"], r["kind"]) for r in rows], [("item", block, "away")])
        self.assertEqual(L.group_entries(rows[0]), [self.ev("e2"), self.ev("e1")])   # free, travel, outside: out

    def test_a_shoot_job_or_travel_of_his_groups_too(self):
        for n, (typ, sub) in enumerate((("commitment", "gig"), ("event", "job"), ("event", "travel"))):
            with self.subTest(sub=sub):
                self.tearDown()
                self.setUp()
                self.entries()
                block = self.mine(what="the studio doc", typ=typ, subtype=sub, mid=f"o{n}")
                rows = self.open_rows()
                self.assertEqual([(r["a_ref"], len(L.group_entries(r))) for r in rows], [(block, 2)])

    def test_a_job_booked_by_mail_is_his(self):
        self.entries()
        self.read("b1", "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", "Brightwater doc shoot", DAYS, subtype="job")], "updates": []}, source="gmail",
            sender="bookings@brightwater.example", name="Brightwater", thread="mail-b1")
        self.assertEqual([len(L.group_entries(r)) for r in self.open_rows()], [2])

    def test_a_friends_shoot_a_one_day_block_or_an_ask_is_not_an_out_block(self):
        self.entries()
        self.read("f1", "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", "Dana's gig in Lisbon", DAYS, subtype="gig")], "updates": []})
        self.mine(w=when("Mar 9", "2027-03-09", all_day=True), mid="o2")
        self.read("f2", "2027-03-01T09:05-08:00", {"items": [item(
            "ask", "sender", "cover the studio job", DAYS, subtype="job")], "updates": []})
        self.assertEqual([r for r in self.open_rows() if r["entries"]], [])

    def test_an_open_invite_during_it_is_an_entry_and_a_no_takes_it_off(self):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=PENDING),
            event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00")])
        self.mine()
        self.ms.write(lambda db: heads.sync_invites(db, self.now[0], TZ))
        rows = self.open_rows()
        self.assertEqual([sorted(L.group_entries(r)) for r in rows], [sorted([self.ev("e1"), self.ev("e2")])])
        gid = rows[0]["id"]
        self.ms.upsert_events([event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00",
                                     attendees=DECLINED)])
        self.ms.write(lambda db: C.regroup_event(db, self.ev("e1"), self.now[0], TZ))   # the owner's RSVP: at once
        self.assertEqual([(r["id"], L.group_entries(r)) for r in self.open_rows()], [(gid, [self.ev("e2")])])

    def test_a_timed_ask_during_it_is_an_away_clash_of_its_own(self):
        self.mine()
        self.read("a1", "2027-03-02T09:00-08:00", {"items": [item(what="call about the edit", w=when(
            "Mar 9 at 2", "2027-03-09", "14:00", "15:00"))],
            "updates": []})
        rows = self.open_rows()
        self.assertEqual([(r["kind"], r["entries"]) for r in rows], [("away", "")])


class TestM4GroupConfidence(OutBase):
    def conf(self):
        return [r["confidence"] for r in self.open_rows() if r["entries"]]

    def test_sure_when_every_item_is_sure(self):
        self.entries()
        self.mine()
        self.assertEqual(self.conf(), ["sure"])

    def test_a_likely_block_makes_it_likely(self):
        self.entries()
        self.mine(conf="likely")
        self.assertEqual(self.conf(), ["likely"])

    def test_a_tentative_entry_or_his_maybe_makes_it_likely(self):
        for kw in ({"status": "tentative"}, {"attendees": MAYBE}):
            with self.subTest(kw=kw):
                self.tearDown()
                self.setUp()
                self.entries(**kw)
                self.mine()
                self.assertEqual(self.conf(), ["likely"])

    def test_a_likely_mail_repeating_a_sure_entry_keeps_it_sure(self):
        self.entries()
        self.mine()
        self.read("m1", "2027-03-02T09:00-08:00", {"items": [item(
            "event", "sender", "Design review", when("Mar 9 at 10", "2027-03-09", "10:00", "11:00"), conf="likely")],
            "updates": []}, source="gmail", sender="priya@studio.example", name="Priya Nair", thread="mail-m1")
        mail = max(self.items())
        self.ms.write(lambda db: db.execute("UPDATE ledger_items SET cal_ref=? WHERE id=?", (self.ev("e1"), mail)))
        self.ms.write(lambda db: C.check_item(db, mail, self.now[0], TZ))
        self.assertEqual(self.conf(), ["sure"])               # the calendar entry itself is sure: it stays sure

    def test_an_invite_item_that_isnt_sure_makes_it_likely(self):
        self.ms.upsert_events([
            event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", attendees=PENDING)])
        block = self.mine()
        self.ms.write(lambda db: heads.sync_invites(db, self.now[0], TZ))
        self.assertEqual(self.conf(), ["sure"])               # an open invite is sure (R1)
        self.ms.write(lambda db: (db.execute("UPDATE ledger_items SET confidence='likely' WHERE subtype='invite'"),
                                  C.check_item(db, block, self.now[0], TZ)))
        self.assertEqual(self.conf(), ["likely"])

    def test_it_follows_the_entries_when_they_change(self):
        self.entries()
        self.mine()
        self.assertEqual(self.conf(), ["sure"])
        self.ms.upsert_events([event("e2", "Dentist", "2027-03-08T14:00-08:00", "2027-03-08T15:00-08:00",
                                     status="tentative")])
        self.ms.write(lambda db: C.regroup_event(db, self.ev("e2"), self.now[0], TZ))
        self.assertEqual(self.conf(), ["likely"])


if __name__ == "__main__":
    import unittest
    unittest.main()
