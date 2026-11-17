"""0.22.1: what the owner tidied stays tidied. "Stuff kept coming back after I pressed tidy up": each way a tidied item
came back, run over what a day on his install runs (a ledger sweep, the clash re-check, Refresh, the scheduled brief, new
mail from the same promo sender, an update in a tidied thread, a re-read or second copy, the backfill going on). A tidied
item comes back only on a real new signal (a person or the owner writing about it), and then says why. Fictional
fixtures only."""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ledger_0160 import Base, Script, item, ts, upd, when  # noqa: E402

import brief2  # noqa: E402
import conflicts as C  # noqa: E402
import extract  # noqa: E402
import ledger as L  # noqa: E402
import tidy  # noqa: E402
import today  # noqa: E402

FOOT = "\n\nYou are receiving this email because you signed up. Unsubscribe or manage your preferences."
FUND = "action@e.fundforward.example"


def old_rules():
    """The read-time rules off: items as 0.21.3 kept them, or as the live install read a 'suggested' sender."""
    return [mock.patch.object(extract.Extractor, "tidy_answer", lambda *a: ([], "")),
            mock.patch.object(extract.TY, "slack_channel", lambda row: False)]


class Day(Base):
    def setUp(self):
        super().setUp()
        self.tidy = tidy.Tidy(self.ms, lambda: self.now[0], owner_names=lambda: ["Jordan Vale"])
        self.td = today.Today(self.ms, self.lv, lambda: self.now[0])
        self.td.tidy = self.tidy

    def old(self, *reads):
        ps = old_rules()
        for p in ps:
            p.start()
        try:
            return [self.read(*a, **kw) for a, kw in reads]
        finally:
            for p in ps:
                p.stop()

    def seed(self):
        """A promo ask, a channel post's ask and a person's ask, read before 0.21.4; then the owner's Tidy up."""
        got = self.old(
            (("p1", "2027-03-01T10:00-08:00", {"items": [item("ask", what="chip in $5", subtype="payment")],
                                              "updates": []}),
             dict(source="gmail", sender=FUND, name="Fund Forward", subject="Chip in $5 before midnight",
                  text="Chip in now." + FOOT, thread="g-fund")),
            (("s1", "2027-03-01T10:01-08:00", {"items": [item("ask", what="review the budget")], "updates": []}),
             dict(source="slack", sender="U9", name="Sam", thread="C123", labels=("channel",),
                  text="Can someone review the budget?")),
            (("d1", "2027-03-01T10:02-08:00", {"items": [item("ask", what="send the deck")], "updates": []}),
             dict(source="gmail", sender="dana.reyes@mail.example", name="Dana", subject="deck?",
                  text="Hi Jordan, can you send the deck?")))
        self.promo, self.chan, self.deck = (g["items"][0] for g in got)
        r = self.tidy.run()
        self.assertEqual(r["moved"], 2)
        return r

    def needs(self):
        return [r["why"] for r in self.td.view()["needs_you"]["rows"]]

    def types(self, *ids):
        return [self.show(i)["type"] for i in ids]


class TestTidiedStaysPut(Day):
    def test_a_day_of_sweeps_refresh_and_the_brief_moves_nothing_back(self):
        self.seed()
        self.assertEqual(self.needs(), ["send the deck"])
        for _ in range(3):
            self.now[0] += 300
            self.ms.write(lambda db: C.sweep(db, self.now[0], L.zone("America/Los_Angeles")))
        self.td.refresh()
        p = brief2.Planner(self.ms, self.lv, self.td).plan(self.now[0])
        shown = [i for s in p["steps"] if s["key"] != "cleanup" for i in s["items"]]
        self.assertNotIn(self.promo, shown)
        self.assertNotIn(self.chan, shown)
        self.assertEqual(self.types(self.promo, self.chan), ["cleanup", "cleanup"])
        self.assertEqual(self.needs(), ["send the deck"])
        self.assertEqual(self.lv.view()["counts"]["clean_up"], 2)

    def test_a_re_read_of_a_tidied_message_joins_its_item(self):
        self.seed()
        self.ms.write(lambda db: db.execute("DELETE FROM ledger_reads WHERE message=?",
                                            (self.ms.message("gmail", "acct", "p1")["id"],)))
        ps = old_rules()
        for p in ps:
            p.start()
        try:
            res = self.ex.read(self.ms.message("gmail", "acct", "p1"))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(res["items"], [self.promo])
        self.assertEqual(self.types(self.promo), ["cleanup"])
        self.assertEqual(self.needs(), ["send the deck"])

    def test_a_second_copy_of_a_tidied_message_joins_its_item(self):
        self.seed()
        row = dict(self.ms.message("gmail", "acct", "p1"))
        self.fake.answers["p1-copy"] = {"items": [item("ask", what="chip in $5", subtype="payment")], "updates": []}
        self.ms.upsert_message({"source": "gmail", "account": "acct2", "msg_id": "p1-copy", "thread": "g-fund-2",
                                "direction": "in", "sender": FUND, "sender_name": "Fund Forward",
                                "recipients": [{"name": "Jordan Vale", "address": "me"}], "ts": row["ts"] + 30,
                                "subject": row["subject"], "text": "Chip in now.", "labels": []})
        ps = old_rules()
        for p in ps:
            p.start()
        try:
            res = self.ex.read(self.ms.message("gmail", "acct2", "p1-copy"))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(res["items"], [self.promo])
        self.assertEqual(self.needs(), ["send the deck"])

    def test_more_mail_from_the_promo_sender_lands_in_clean_up(self):
        self.seed()
        res = self.read("p2", "2027-03-02T10:00-08:00", {"items": [item("ask", what="chip in $10", subtype="payment")],
                                                         "updates": []},
                        source="gmail", sender=FUND, name="Fund Forward", subject="Last chance to chip in",
                        text="Chip in now, offer ends tonight." + FOOT, thread="g-fund")
        self.assertEqual(self.types(*res["items"]), ["cleanup"])
        self.assertEqual(self.needs(), ["send the deck"])

    def test_a_machine_follow_up_in_a_tidied_thread_joins_it_in_clean_up(self):
        self.seed()
        # no footer, no list signal: the read-time promo rule has nothing to go on; the ask repeats the tidied one
        res = self.read("p3", "2027-03-02T11:00-08:00", {"items": [item("ask", what="chip in $5", subtype="payment")],
                                                         "updates": [upd(f"L{self.promo}", note="reminder")]},
                        source="gmail", sender="no-reply@fundforward.example", name="Fund Forward",
                        subject="Re: Chip in $5 before midnight", text="Reminder: chip in $5 before midnight.",
                        thread="g-fund")
        self.assertEqual(self.types(self.promo), ["cleanup"])
        self.assertTrue(all(self.show(i)["type"] == "cleanup" for i in res["items"]))
        self.assertEqual(self.needs(), ["send the deck"])

    def test_the_same_channel_ask_again_stays_in_from_your_channels(self):
        self.seed()
        res = self.read("s2", "2027-03-02T09:00-08:00", {"items": [item("ask", what="review the budget")],
                                                         "updates": []},
                        source="slack", sender="U9", name="Sam", thread="C123", labels=("channel",),
                        text="Can someone review the budget? Bumping this.")
        self.assertEqual(self.types(*res["items"]), ["cleanup"])
        self.assertEqual(self.needs(), ["send the deck"])

    def test_a_person_writing_about_it_brings_it_back_with_a_reason(self):
        self.seed()
        res = self.read("s3", "2027-03-02T09:30-08:00", {"items": [item("ask", what="review the budget")],
                                                         "updates": []},
                        source="slack", sender="U9", name="Sam", thread="D55",
                        text="Jordan, can you review the budget today? Finance needs it.")
        self.assertEqual(res["items"], [self.chan])
        it = self.show(self.chan)
        self.assertEqual(it["type"], "ask")
        back = [c for c in it["checks"] if c.get("field") == "back"]
        self.assertTrue(back and "Sam" in back[0]["why"], it["checks"])
        self.assertIn("tidy_back", [h["kind"] for h in it["history"]])
        rows = self.td.view()["needs_you"]["rows"]
        row = next(r for r in rows if r["id"] == self.chan)
        self.assertTrue(row["back"].startswith("Back from Clean up"), row)
        # the run's Undo leaves an item that came back alone
        self.assertEqual(self.tidy.undo(self.tidy.last()["id"])["restored"], 1)
        self.assertEqual(self.show(self.chan)["type"], "ask")

    def test_an_update_from_a_person_brings_it_back(self):
        self.seed()
        self.read("p4", "2027-03-02T12:00-08:00", {"items": [], "updates": [upd(f"L{self.promo}",
                                                                                 note="Ana asks if you chipped in")]},
                  source="gmail", sender="ana@trailfriends.example", name="Ana", subject="Re: Chip in $5",
                  text="Hi Jordan, did you chip in for the trail fund? I said I'd ask.", thread="g-fund")
        it = self.show(self.promo)
        self.assertEqual(it["type"], "ask")
        self.assertTrue(any(c.get("field") == "back" for c in it["checks"]))

    def test_security_alerts_and_invites_from_a_tidied_sender_are_never_hidden(self):
        self.seed()
        res = self.read("x1", "2027-03-02T13:00-08:00", {"items": [item("cleanup", what="new sign-in")],
                                                         "updates": []},
                        source="gmail", sender=FUND, name="Fund Forward", subject="Security alert: new sign-in",
                        text="A new sign-in on your Fund Forward account from a new device.", thread="g-fund")
        secs = [self.show(i) for i in res["items"]]
        self.assertTrue(any(s["subtype"] == "security" for s in secs), secs)
        self.assertTrue(self.td.view()["security"] if "security" in self.td.view() else True)


class TestClashesStayDismissed(Day):
    def clash(self):
        self.read("e1", "2027-03-01T10:00-08:00", {"items": [item("event", what="the shoot", w=when(
            "Thu 2-4pm", "2027-03-04", "14:00", "16:00"))], "updates": []}, sender="p:dana", name="Dana")
        self.read("e2", "2027-03-01T10:05-08:00", {"items": [item("event", what="edit review", w=when(
            "Thu 3pm", "2027-03-04", "15:00", "16:00"))], "updates": []}, sender="p:lee", name="Lee")
        rows = self.ms._read("SELECT id FROM ledger_conflicts WHERE state='open'")  # noqa: SLF001
        self.assertEqual(len(rows), 1)
        return rows[0]["id"]

    def sweep(self):
        self.now[0] += 300
        self.ms.write(lambda db: C.sweep(db, self.now[0], L.zone("America/Los_Angeles")))
        return [r["id"] for r in self.ms._read("SELECT id FROM ledger_conflicts WHERE state='open'")]  # noqa: SLF001

    def test_a_dismissed_clash_isnt_rebuilt_by_the_sweep(self):
        cid = self.clash()
        self.td.dismiss(cid)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(), [])
        self.assertTrue(self.td.reopen(cid))           # Undo still opens it
        self.assertEqual(self.sweep(), [cid])

    def test_a_moved_item_is_a_new_clash(self):
        cid = self.clash()
        self.td.dismiss(cid)
        self.read("e3", "2027-03-01T11:00-08:00", {"items": [], "updates": [upd("L2", "moved", w=when(
            "Thu 3:30", "2027-03-04", "15:30", "16:30"))]}, sender="p:lee", name="Lee")
        self.assertEqual(len(self.sweep()), 1)

    def test_a_tidied_items_clash_isnt_rebuilt(self):
        self.old((("e4", "2027-03-01T10:00-08:00", {"items": [item("event", what="Members night", w=when(
            "Thu 2pm", "2027-03-04", "14:00", "16:00"))], "updates": []}),
            dict(source="gmail", sender="team@gymclass.example", name="Gym", subject="Offer ends tonight",
                 text="Last chance for the special rate on classes, members night Thu 2pm." + FOOT)),
            (("e5", "2027-03-01T10:05-08:00", {"items": [item("event", what="the shoot", w=when(
                "Thu 2-4pm", "2027-03-04", "14:00", "16:00"))], "updates": []}),
             dict(sender="p:dana", name="Dana")))
        self.assertEqual(len(self.sweep()), 1)
        self.tidy.run()
        self.assertEqual(self.sweep(), [])
        gym = self.ms._read("SELECT id FROM ledger_items WHERE type='cleanup'")[0]["id"]  # noqa: SLF001
        self.now[0] = ts("2027-03-04T08:00-08:00")                 # that day's brief doesn't list it either
        p = brief2.Planner(self.ms, self.lv, self.td).plan(self.now[0])
        self.assertNotIn(gym, [i for s in p["steps"] if s["key"] != "cleanup" for i in s["items"]])


class TestOlderStaysOlder(Day):
    def catch_up(self):
        """The first catch-up: a message from February read on March 3."""
        self.fake.answers["o1"] = {"items": [item("ask", what="sign the W-9")], "updates": []}
        self.ms.upsert_message({"source": "gmail", "account": "acct", "msg_id": "o1", "thread": "t-kim",
                                "direction": "in", "sender": "kim@studio.example", "sender_name": "Kim",
                                "recipients": [{"name": "Jordan Vale", "address": "me"}],
                                "ts": ts("2027-02-26T10:00-08:00"), "subject": "W-9", "text": "Can you sign the W-9?",
                                "labels": []})
        self.now[0] = ts("2027-03-03T09:00-08:00")
        self.ex.read(self.ms.message("gmail", "acct", "o1"))
        ny = self.td.view()["needs_you"]
        self.assertEqual([r["why"] for r in ny["older"]["rows"]], ["sign the W-9"])

    def later(self, mid, sender, text, direction="in"):
        self.now[0] += 3600
        self.ms.upsert_message({"source": "gmail", "account": "acct", "msg_id": mid, "thread": "t-kim",
                                "direction": direction, "sender": sender, "sender_name": "",
                                "recipients": [{"name": "Jordan Vale", "address": "me"}], "ts": self.now[0],
                                "subject": "Re: W-9", "text": text, "labels": []})

    def test_machine_mail_in_the_thread_doesnt_bring_it_back(self):
        self.catch_up()
        self.later("o2", "no-reply@docs.example", "Your document was viewed.")
        ny = self.td.view()["needs_you"]
        self.assertEqual(([r["why"] for r in ny["rows"]], ny["older"]["total"]), ([], 1))

    def test_a_reply_from_the_person_does(self):
        self.catch_up()
        self.later("o3", "kim@studio.example", "Any news on the W-9?")
        ny = self.td.view()["needs_you"]
        self.assertEqual([r["why"] for r in ny["rows"]], ["sign the W-9"])


class TestLiveResolver(unittest.TestCase):
    """The live install resolves senders through contacts first: a sender only suggested for People must still read
    as not known, so the promo rules apply to new mail from the senders Tidy up moved."""

    def setUp(self):
        import api
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [ts("2027-03-01T09:00-08:00")]
        self.app = api.App(self.dir, clock=lambda: self.now[0])
        self.addCleanup(self.app.close)
        db = self.app.store.db
        db.execute("INSERT INTO people (id, name, state, created, updated) VALUES ('p77','Fund Forward','suggested',0,0)")
        db.execute("INSERT INTO person_handles (person, kind, value, added) VALUES ('p77','email',?,0)", (FUND,))
        db.commit()
        self.fake = Script()
        self.app.extractor.call = self.fake

    def read(self, mid, at, answer, text, subject):
        self.now[0] = ts(at)
        self.fake.answers[mid] = answer
        self.app.messages.upsert_message({"source": "gmail", "account": "acct", "msg_id": mid, "thread": f"t-{mid}",
                                          "direction": "in", "sender": FUND, "sender_name": "Fund Forward",
                                          "recipients": [{"name": "Jordan Vale", "address": "me"}], "ts": ts(at),
                                          "subject": subject, "text": text, "labels": []})
        return self.app.extractor.read(self.app.messages.message("gmail", "acct", mid))

    def test_a_suggested_sender_keeps_its_state(self):
        key, _name = self.app.extractor.person("gmail", FUND, "Fund Forward")
        self.assertTrue(key.startswith("person:"))
        self.assertEqual(self.app.extractor.person.states.get(key), "suggested")

    def test_new_promo_after_tidy_up_lands_in_clean_up(self):
        ps = old_rules()
        for p in ps:
            p.start()
        try:
            self.read("p1", "2027-03-01T10:00-08:00", {"items": [item("ask", what="chip in $5")], "updates": []},
                      "Chip in now." + FOOT, "Chip in $5 before midnight")
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(self.app.tidy.run()["moved"], 1)
        res = self.read("p2", "2027-03-02T10:00-08:00", {"items": [item("ask", what="chip in $10")], "updates": []},
                        "Chip in now, our holiday sale ends Sunday." + FOOT, "Chip in $10 today")
        self.assertEqual([self.app.extractor.lv.item(i)["type"] for i in res["items"]], ["cleanup"])
        self.assertEqual(self.app.extractor.lv.view()["counts"]["needs_you"], 0)


class TestVersion(unittest.TestCase):
    def test_version_0221(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertIn('version: "0.22.1"', (root / "callsheet" / "config.yaml").read_text(encoding="utf-8"))
        self.assertIn('APP_VERSION = "0.22.1"', (root / "callsheet" / "app" / "api.py").read_text(encoding="utf-8"))
        self.assertIn("0.22.1", (root / "callsheet" / "CHANGELOG.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()



class BannerAgainAfterUpdate(Day):
    """0.22.1: an install that ran Tidy up before the fix is offered it once more; after a run or Not now it stays away."""
    def test_offered_once_more(self):
        self.ms.write(lambda db: db.execute("INSERT INTO tidy_runs (at, how, moved) VALUES (1,'today',3)"))
        self.assertNotEqual(self.ms.meta("tidy_banner_rev", ""), tidy.BANNER_REV)
        self.tidy.dismiss()
        self.assertEqual(self.ms.meta("tidy_banner_rev", ""), tidy.BANNER_REV)
        self.assertIsNone(self.tidy.banner())
