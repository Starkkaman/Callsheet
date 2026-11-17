"""0.22.0: follow-ups for someone the owner hasn't told (conflicts): a no to one person that another open ask hangs on
(FUP-03), a calendar entry that still holds a plan the owner agreed to move (FUP-04, with Today's reminder before it), and
a yes in one conversation that someone else asked about in another (FUP-09). In-process fakes only: no model server,
no network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import LA, Base, item, ts, upd, when  # noqa: E402
import today_fixtures as F  # noqa: E402

import conflicts  # noqa: E402
import ledger  # noqa: E402
import today  # noqa: E402

KOFI = ("kofi@studio.example", "Kofi Mensah")
LENA = ("lena@studio.example", "Lena Ortiz")


class Tell(Base):
    def open_fu(self):
        return [(f["item"], f["tell"]) for f in self.lv.followups() if f["state"] == "open"]

    def sweep(self, at):
        self.now[0] = ts(at)
        return self.ms.write(lambda db: conflicts.sweep(db, self.now[0], LA))


class TestDeclineTellsTheOtherSide(Tell):
    """S-FUP-03: Ines asks for Monday; Tess holds Stage B for it; the owner tells Ines they can't."""

    def asks(self, tess_text="Ines mentioned you might do pickups Monday. Should I hold Stage B for you 8-12?"):
        ines = self.read("m1", "2027-03-04T09:00-08:00", {"items": [item(what="rooftop pickup shots", w=when(
            "Monday 3/8", "2027-03-08", all_day=True))], "updates": []}, sender="p:ines", name="Ines", source="sms",
            thread="t-ines", text="Can you do Monday 3/8 for the rooftop pickup shots?")["items"][0]
        tess = self.read("m2", "2027-03-04T10:00-08:00", {"items": [item(what="hold Stage B Monday 8-12?", w=when(
            "Monday 8-12", "2027-03-08", "08:00", "12:00"))], "updates": []}, sender="p:tess", name="Tess",
            source="gmail", thread="t-tess", subject="Stage B hold Monday", text=tess_text)["items"][0]
        return ines, tess

    def no(self, ines, updates=True):
        return self.read("m3", "2027-03-04T12:00-08:00", {"items": [], "updates": [upd(f"L{ines}", "declined")]
                                                          if updates else []},
                         direction="out", to=[("p:ines", "Ines")], source="sms", thread="t-ines",
                         text="Can't Monday, sorry. Tuesday?")

    def test_his_no_to_ines_gives_tess_a_follow_up_until_he_writes_to_her(self):
        ines, tess = self.asks()
        self.no(ines)
        fu = [f for f in self.lv.followups() if f["state"] == "open"]
        self.assertEqual([(f["item"], f["tell"], f["tell_name"]) for f in fu], [(tess, "p:tess", "Tess")])
        self.assertEqual(fu[0]["reason"], "You said no to Ines for Mon Mar 8; Tess still waits to hear about it.")
        self.assertEqual(self.show(tess)["status"], "open")             # her ask stays the owner's to answer
        self.read("m4", "2027-03-04T12:30-08:00", {"items": [], "updates": [upd(f"L{tess}", "declined")]},
                  direction="out", to=[("p:tess", "Tess")], source="gmail", thread="t-tess",
                  text="No need to hold Stage B Monday, thanks.")
        self.assertEqual(self.open_fu(), [])

    def test_the_model_left_the_ask_open_his_cant_in_that_thread_still_counts(self):
        ines, tess = self.asks()
        self.no(ines, updates=False)
        self.assertEqual(self.open_fu(), [(tess, "p:tess")])

    def test_an_unrelated_ask_the_same_day_gets_nothing(self):
        ines, _ = self.asks(tess_text="Can you look at the color notes Monday 8-12?")
        self.no(ines)
        self.assertEqual(self.open_fu(), [])

    def test_a_no_with_nobody_else_waiting_gets_nothing(self):
        ines = self.read("m1", "2027-03-04T09:00-08:00", {"items": [item(what="rooftop pickup shots", w=when(
            "Monday 3/8", "2027-03-08", all_day=True))], "updates": []}, sender="p:ines", name="Ines", source="sms",
            thread="t-ines", text="Can you do Monday 3/8 for the rooftop pickup shots?")["items"][0]
        self.read("m2", "2027-03-04T10:00-08:00", {"items": [item(what="hold Stage B Friday 8-12?", w=when(
            "Friday 8-12", "2027-03-12", "08:00", "12:00"))], "updates": []}, sender="p:tess", name="Tess",
            source="gmail", thread="t-tess", text="Ines mentioned pickups. Should I hold Stage B Friday 8-12?")
        self.no(ines)
        self.assertEqual(self.open_fu(), [])                            # Tess asked about another day


class TestStaleCalendarEntry(Tell):
    """S-FUP-04: the calendar has the review with Lena and Kofi on Wednesday; the owner agrees with Lena to move it."""

    def setUp(self):
        super().setUp()
        self.event()

    def event(self, start="2027-03-10T15:00-08:00", end="2027-03-10T16:00-08:00", attendees=None):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "rv1", "title": "Review w/ Lena + Kofi",
                                "start_ts": ts(start), "end_ts": ts(end), "status": "confirmed",
                                "attendees": attendees if attendees is not None else [
                                    {"address": LENA[0], "name": LENA[1]}, {"address": KOFI[0], "name": KOFI[1]},
                                    {"address": "me@x.example", "name": "Jordan Vale", "self": True}]}])

    def move(self, reply="Thursday 3pm works. I'll update everyone.", to=(LENA,)):
        ask = self.read("m1", "2027-03-08T09:00-08:00", {"items": [item(what="move the review to Thursday 3pm", w=when(
            "Thursday 3pm", "2027-03-11", "15:00"))], "updates": []}, sender=LENA[0], name=LENA[1], source="gmail",
            thread="t-lena", subject="Review", text="Could we move Wednesday's review to Thursday 3pm?")["items"][0]
        res = self.read("m2", "2027-03-08T09:30-08:00", {"items": [item(
            "commitment", "owner", "review with Lena (moved to Thursday)", when("Thursday 3pm", "2027-03-11", "15:00"),
            "accepted")], "updates": [upd(f"L{ask}", "done")]}, direction="out", to=list(to), source="gmail",
            thread="t-lena", subject="Re: Review", text=reply)
        return res["items"][0]

    def test_kofi_gets_a_follow_up_due_before_the_old_entry(self):
        it = self.move()
        fu = [f for f in self.lv.followups() if f["state"] == "open"]
        self.assertEqual([(f["item"], f["tell"], f["tell_name"]) for f in fu], [(it, KOFI[0], KOFI[1])])
        self.assertEqual(fu[0]["reason"], "The calendar still has Review w/ Lena + Kofi on Wed 3 pm; Kofi hasn't heard "
                                          "about the change.")
        self.assertEqual(fu[0]["due_ts"], ts("2027-03-10T12:00-08:00"))     # 3 hours before Wednesday's entry

    def test_today_reminds_him_before_the_entry_with_names_and_times_only(self):
        self.move()
        al = F.FakeAlerter()
        td = today.Today(self.ms, self.lv, lambda: self.now[0], alerter=al)
        td.tick(ts("2027-03-08T10:00-08:00"))
        self.assertEqual(al.sent, [])
        self.assertEqual(len(td.tick(ts("2027-03-10T12:05-08:00"))), 1)
        self.assertEqual(al.sent[0]["title"], "Kofi hasn’t heard about the change")
        self.assertEqual(al.sent[0]["message"], "The calendar still has it Wed 3:00 pm.")
        self.assertNotIn("Thursday", al.sent[0]["message"])
        td.tick(ts("2027-03-10T12:30-08:00"))
        self.assertEqual(len(al.sent), 1)                                    # once

    def test_moving_the_entry_clears_it_and_its_reminder(self):
        it = self.move()
        al = F.FakeAlerter()
        td = today.Today(self.ms, self.lv, lambda: self.now[0], alerter=al)
        td.tick(ts("2027-03-08T10:00-08:00"))
        self.event("2027-03-11T15:00-08:00", "2027-03-11T16:00-08:00")
        self.sweep("2027-03-08T11:00-08:00")
        self.assertEqual(self.open_fu(), [])
        self.assertIn("follow_up_cleared", {h["kind"] for h in self.show(it)["history"]})
        self.assertEqual(td.view()["now_next"]["cleared"][0]["why"], "the calendar entry was updated")
        td.tick(ts("2027-03-10T12:05-08:00"))
        self.assertEqual(al.sent, [])

    def test_his_message_to_kofi_clears_it(self):
        it = self.move()
        self.read("m3", "2027-03-08T10:00-08:00", {"items": [], "updates": [upd(f"L{it}", note="told Kofi")]},
                  direction="out", to=[KOFI], source="gmail", thread="t-kofi",
                  text="Heads up: the review moved to Thursday 3pm.")
        self.assertEqual(self.open_fu(), [])

    def test_no_follow_up_when_everyone_was_written_to_or_nothing_moved(self):
        self.move(to=(LENA, KOFI))
        self.assertEqual(self.open_fu(), [])
        self.tearDown()
        self.setUp()
        self.read("m1", "2027-03-08T09:00-08:00", {"items": [], "updates": []}, sender=LENA[0], name=LENA[1],
                  source="gmail", thread="t-lena", subject="Review", text="Are we still on for the review Thursday?")
        self.read("m2", "2027-03-08T09:30-08:00", {"items": [item("commitment", "owner", "review with Lena", when(
            "Thursday 3pm", "2027-03-11", "15:00"), "accepted")], "updates": []}, direction="out", to=[LENA],
            source="gmail", thread="t-lena", subject="Re: Review", text="Yes, Thursday 3pm.")
        self.assertEqual(self.open_fu(), [])

    def test_an_entry_with_only_lena_gets_nothing(self):
        self.event(attendees=[{"address": LENA[0], "name": LENA[1]}])
        self.move()
        self.assertEqual(self.open_fu(), [])

    def test_an_old_followups_table_gains_the_event_column(self):
        self.ms.write(lambda db: db.execute("ALTER TABLE ledger_followups DROP COLUMN event"))
        ledger.ensure(self.ms)
        cols = {r[1] for r in self.ms._read("PRAGMA table_info(ledger_followups)")}  # noqa: SLF001
        self.assertIn("event", cols)


class TestCrossThreadPromise(Tell):
    """S-FUP-09: Theo asks on Signal whether the owner's on the Brightwater doc; the owner confirms it with Dana by email."""

    def theo(self, text="Are you booked on the Brightwater doc Mar 24-25? Want to know if I should hold for you as my DP"):
        return self.read("m1", "2027-03-06T10:00-08:00", {"items": [item(
            what="are you on the Brightwater doc? (he will hold for you)")], "updates": []}, sender="p:theo",
            name="Theo", source="signal", thread="t-theo", text=text)["items"][0]

    def confirm(self, to=("p:dana", "Dana"), thread="t-dana"):
        return self.read("m2", "2027-03-06T16:00-08:00", {"items": [item(
            "commitment", "owner", "Brightwater doc shoot", when("Confirming Mar 24-25", "2027-03-24", end_date="2027-03-25",
                                                                 all_day=True), "accepted")], "updates": []},
            direction="out", to=[to], source="gmail", thread=thread, subject="Re: Booking",
            text="Confirming Mar 24-25 at $850/day.")["items"][0]

    def test_his_yes_to_dana_gives_theo_a_follow_up_until_he_writes_to_him(self):
        ask = self.theo()
        self.confirm()
        fu = [f for f in self.lv.followups() if f["state"] == "open"]
        self.assertEqual([(f["item"], f["tell"]) for f in fu], [(ask, "p:theo")])
        self.assertTrue(fu[0]["reason"].startswith("You confirmed Brightwater doc shoot ("), fu[0]["reason"])
        self.assertTrue(fu[0]["reason"].endswith(") with Dana; Theo asked and hasn't heard."), fu[0]["reason"])
        self.read("m3", "2027-03-06T17:00-08:00", {"items": [], "updates": [upd(f"L{ask}", "done")]},
                  direction="out", to=[("p:theo", "Theo")], source="signal", thread="t-theo",
                  text="Yes, I'm on it, hold the 24th-25th.")
        self.assertEqual(self.open_fu(), [])

    def test_other_days_or_his_answer_in_the_same_conversation_get_nothing(self):
        self.theo(text="Are you booked on the Brightwater doc Mar 30-31? Want to know if I should hold for you")
        self.confirm()
        self.assertEqual(self.open_fu(), [])
        self.tearDown()
        self.setUp()
        self.theo()
        self.confirm(to=("p:theo", "Theo"), thread="t-theo")
        self.assertEqual(self.open_fu(), [])

    def test_an_unrelated_ask_on_those_days_gets_nothing(self):
        self.read("m1", "2027-03-06T10:00-08:00", {"items": [item(what="send the invoice for Mar 24")], "updates": []},
                  sender="p:theo", name="Theo", source="signal", thread="t-theo",
                  text="Can you send me the invoice for Mar 24?")
        self.confirm()
        self.assertEqual(self.open_fu(), [])


if __name__ == "__main__":
    unittest.main()
