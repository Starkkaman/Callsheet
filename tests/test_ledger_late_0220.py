"""0.22.0: past-due bills and passed deadlines are never hidden (the ledger's reading, sections, the chase follow-up
for money owed to the owner, and Today's Needs you). In-process fakes only: no model server, no network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, upd, when  # noqa: E402

import extract  # noqa: E402
import ledger  # noqa: E402
import today  # noqa: E402

BILL = "Past due: your phone bill of $62.40 was due March 3. Pay now to avoid a late fee."
INVOICE = "Invoice #1043 to Cornerstone Coffee ($2,400) is 15 days past due."


def money(it, cited):
    return dict(it, amount_from=cited, stage="")


class Late(Base):
    def today(self):
        return today.Today(self.ms, self.lv, lambda: self.now[0])

    def bill(self, answer_item, at="2027-03-08T08:00-08:00"):
        return self.read("b1", at, {"items": [answer_item], "updates": []}, text=BILL, subject="Payment past due",
                         source="gmail", sender="billing@phoneco.example", name="PhoneCo")

    def invoice(self, answer_item):
        return self.read("i1", "2027-03-10T09:00-08:00", {"items": [answer_item], "updates": []}, text=INVOICE,
                         subject="Invoice past due", source="gmail", sender="ar@books.example", name="Books",
                         thread="t-books")


class TestPastDueBill(Late):
    def test_a_past_due_bill_read_as_an_event_is_an_ask_never_hidden(self):
        res = self.bill(money(item("event", what="phone bill", subtype="bill", w=when("was due March 3", "2027-03-03",
                                                                                      all_day=True)), BILL))
        it = self.show(res["items"][0])
        self.assertEqual((it["type"], it["subtype"], it["status"], it["section"]), ("ask", "bill", "open", "needs_you"))
        self.assertTrue(it["never_hide"])
        self.assertEqual(it["who"], "billing@phoneco.example")
        self.assertIn("past_due", {c["field"] for c in it["checks"]})
        self.assertEqual(it["late"]["days"], 5)
        self.assertEqual(it["late"]["word"], "5 days late")
        self.assertEqual(it["amount"], "$62.40")
        self.assertEqual(it["when"]["start_date"], "2027-03-03")    # the date is kept as written

    def test_a_past_due_bill_filed_as_a_reminder_or_his_own_to_do_is_an_ask(self):
        a = self.show(self.bill(item("cleanup", what="payment reminder"))["items"][0])
        self.assertEqual((a["type"], a["subtype"], a["section"]), ("ask", "bill", "needs_you"))
        self.tearDown()
        self.setUp()
        b = self.show(self.bill(item("commitment", "owner", "pay the phone bill", when(
            "was due March 3", "2027-03-03", all_day=True), "accepted", subtype="bill"))["items"][0])
        self.assertEqual((b["type"], b["who"], b["status"], b["section"]), ("ask", "billing@phoneco.example", "open",
                                                                            "needs_you"))

    def test_two_readings_of_one_past_due_bill_are_one_item(self):
        res = self.read("b1", "2027-03-08T08:00-08:00", {"items": [
            money(item("event", what="phone bill", subtype="bill", w=when("was due March 3", "2027-03-03",
                                                                         all_day=True)), BILL),
            money(item(what="pay phone bill", subtype="bill"), BILL)], "updates": []}, text=BILL, source="gmail",
            sender="billing@phoneco.example", name="PhoneCo")
        self.assertEqual(len(res["items"]), 1)
        self.assertIn("the same past-due bill as item 1", {d["why"] for d in res["drops"]})

    def test_a_bill_due_later_stays_an_event(self):
        res = self.read("b1", "2027-03-01T08:00-08:00", {"items": [item("event", what="power bill $84.12", subtype="bill",
                                                                        w=when("due March 15", "2027-03-15"))],
                                                         "updates": []},
                        text="Your Brightgrid Power bill of $84.12 is due March 15.", source="gmail", sender="b@x.example")
        it = self.show(res["items"][0])
        self.assertEqual((it["type"], it["section"], it["late"]), ("event", "coming_up", None))
        self.assertNotIn("past_due", {c["field"] for c in it["checks"]})

    def test_was_due_on_the_third_is_this_month(self):
        it = self.show(self.read("b1", "2027-03-08T08:00-08:00", {"items": [item(what="phone bill", subtype="bill", w=when(
            "was due on the 3rd"))], "updates": []}, text="Reminder: your bill was due on the 3rd.")["items"][0])
        self.assertEqual(it["when"]["start_date"], "2027-03-03")
        c = []
        self.assertEqual(extract._check_date(ledger.dt.date(2027, 3, 3), "was due on the 3rd",  # noqa: SLF001
                                             ledger.dt.datetime(2027, 3, 8, 8, tzinfo=ledger.zone("UTC")),
                                             ledger.zone("UTC"), c), (ledger.dt.date(2027, 3, 3), False))

    def test_today_shows_it_first_urgent_with_amount_and_days_late(self):
        self.read("a1", "2027-03-07T10:00-08:00", {"items": [item(what="send the call sheet")], "updates": []},
                  sender="p:dana", name="Dana")
        self.bill(money(item("event", what="phone bill", subtype="bill", w=when("was due March 3", "2027-03-03",
                                                                                all_day=True)), BILL))
        self.now[0] = ts("2027-03-09T09:00-08:00")
        rows = self.today().view()["needs_you"]["rows"]
        self.assertEqual([r["why"] for r in rows], ["phone bill", "send the call sheet"])
        self.assertEqual((rows[0]["late"], rows[0]["amount"], rows[0].get("urgent")), ("6 days late", "$62.40", True))
        self.assertEqual((rows[1]["late"], rows[1]["amount"]), ("", ""))


class TestPassedDeadlines(Late):
    def test_a_passed_deadline_shows_late_and_a_past_plan_does_not(self):
        due = self.read("c1", "2027-03-01T10:00-08:00", {"items": [item(
            "commitment", "owner", "send Kofi the cut", when("by Friday", "2027-03-05"), "accepted")], "updates": []},
            direction="out", to=[("p:kofi", "Kofi")], text="I'll send the cut by Friday")["items"][0]
        party = self.read("c2", "2027-03-01T11:00-08:00", {"items": [item("event", what="Ana's party", subtype="party",
                                                                          w=when("Thursday 7pm", "2027-03-04", "19:00"))],
                                                           "updates": []}, sender="p:ana", name="Ana")["items"][0]
        self.now[0] = ts("2027-03-08T09:00-08:00")
        v = self.lv.view()
        self.assertEqual([r["id"] for r in v["needs_you"]], [due])
        self.assertEqual(v["needs_you"][0]["late"]["word"], "3 days late")
        self.assertNotIn(party, [r["id"] for k in ("now_next", "coming_up", "needs_you") for r in v[k]])
        rows = self.today().view()["needs_you"]["rows"]
        self.assertEqual([(r["id"], r["late"], r["mine"]) for r in rows], [(due, "3 days late", True)])

    def test_late_words(self):
        self.assertEqual([ledger.late_word(x) for x in ({"days": 0}, {"days": 1}, {"days": 15}, {"days": None}, None)],
                         ["Late", "1 day late", "15 days late", "Past due", ""])

    def test_autopay_and_done_are_not_late(self):
        res = self.read("p1", "2027-03-01T08:00-08:00", {"items": [item("event", what="internet autopay $70",
                                                                        subtype="bill", status="autopay",
                                                                        w=when("March 3", "2027-03-03"))],
                                                         "updates": []}, text="Your autopay of $70.00 is set for Mar 3.")
        self.now[0] = ts("2027-03-08T09:00-08:00")
        it = self.show(res["items"][0])
        self.assertEqual((it["late"], it["section"]), (None, "none"))


class TestReceivable(Late):
    def test_past_due_invoice_is_chased_until_paid(self):
        res = self.invoice(money(item("event", what="invoice #1043 past due", subtype="receivable",
                                      w=when("15 days past due")), INVOICE))
        it = self.show(res["items"][0])
        self.assertEqual((it["type"], it["subtype"], it["section"], it["owed"]), ("ask", "receivable", "needs_you",
                                                                                   "to_owner"))
        self.assertEqual(it["late"]["word"], "15 days late")
        fu = self.lv.followups()
        self.assertEqual([(f["item"], f["tell"], f["state"]) for f in fu], [(it["id"], "", "open")])
        self.assertIn("chase it", fu[0]["reason"])
        loose = self.today().view()["now_next"]["followups"]
        self.assertEqual(len(loose), 1)
        self.assertIn("15 days late: chase it", loose[0]["line"])
        # the owner's nudge on another thread names the invoice: waiting on them; the chase stays until it's paid
        self.read("i2", "2027-03-10T11:00-08:00", {"items": [], "updates": []}, direction="out",
                  to=[("p:lena", "Lena")], source="gmail", thread="t-lena",
                  text="Hi Lena, friendly nudge on invoice #1043. Let me know if you need it resent.")
        self.assertEqual(self.show(it["id"])["status"], "waiting_on_them")
        self.assertEqual(len(self.lv.followups()), 1)
        self.now[0] = ts("2027-03-11T09:00-08:00")
        self.assertIn("16 days late", self.today().view()["now_next"]["followups"][0]["line"])
        # a message that says it isn't paid changes nothing; "payment received" closes it and clears the chase
        self.read("i3", "2027-03-11T10:00-08:00", {"items": [], "updates": []}, source="gmail", sender="ar@books.example",
                  name="Books", thread="t-books2", text="Reminder: invoice #1043 has not been paid.")
        self.assertEqual(len(self.lv.followups()), 1)
        self.read("i4", "2027-03-12T10:00-08:00", {"items": [], "updates": []}, source="gmail", sender="ar@books.example",
                  name="Books", thread="t-books3", text="Payment received for invoice #1043. Thank you!")
        self.assertEqual(self.show(it["id"])["status"], "done")
        self.assertEqual(self.lv.followups(), [])

    def test_paid_names_another_invoice_leaves_it_open(self):
        it = self.invoice(money(item(what="chase invoice #1043", subtype="receivable", w=when("15 days past due")),
                                INVOICE))["items"][0]
        self.read("i2", "2027-03-11T10:00-08:00", {"items": [], "updates": []}, source="gmail", sender="ar@books.example",
                  name="Books", thread="t-books2", text="Invoice #2001 has been paid in full.")
        self.assertEqual(self.show(it)["status"], "open")
        self.assertEqual(len(self.lv.followups()), 1)

    def test_the_models_done_clears_the_chase(self):
        it = self.invoice(money(item(what="chase invoice #1043", subtype="receivable", w=when("15 days past due")),
                                INVOICE))["items"][0]
        self.read("i2", "2027-03-11T10:00-08:00", {"items": [], "updates": [upd(f"L{it}", "done", note="paid")]},
                  source="gmail", sender="p:lena", name="Lena", thread="t-lena", text="Sent the $2,400 today!")
        self.assertEqual(self.lv.followups(), [])


if __name__ == "__main__":
    unittest.main()
