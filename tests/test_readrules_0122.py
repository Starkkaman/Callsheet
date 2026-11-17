"""The 0.12.2 date and amount rules, re-homed from the retired reader's tests (test_reader_0122.Fixes0122) onto
readrules.py, which the ledger's read (extract.py) uses: cancelled or moved dates, weekday vs date, a weekday said on
that day, EOW / end of month / the 15th, numeric dates by the owner's date order, invite times into the owner's zone,
totals over line items, ranges, signatures. Plus the owner's date-order setting (background.py, meta `reader`) reaching
the ledger's read. Deterministic; nothing calls a network."""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import background  # noqa: E402
import inject  # noqa: E402
import readrules  # noqa: E402
import store  # noqa: E402
import test_ledger_0160 as T  # noqa: E402

MON = dt.date(2026, 11, 2)
LA = zoneinfo.ZoneInfo("America/Los_Angeles")
LON = zoneinfo.ZoneInfo("Europe/London")


def due(body, sent=MON, tz=LON, order=None):
    """The date the message makes due, as the old reader's check did: one date said as new in any sentence and not
    named as old or cancelled anywhere in the message; else None."""
    new, old = set(), set()
    for s in inject.sentences(body):
        n, o = readrules.date_reading(s, sent, tz, order)
        new |= n
        old |= o
    new -= old
    return next(iter(new)).isoformat() if len(new) == 1 else None


def amount_of(body, frag):
    sents = {f"s{i + 1}": x for i, x in enumerate(inject.sentences(body))}
    sid = next((k for k, v in sents.items() if frag in v), None)
    return readrules.pick_amount(sents, sid)


class Dates(unittest.TestCase):
    def test_1_cancelled_and_moved_dates(self):
        self.assertIsNone(due("The 12 Nov review is cancelled."))
        self.assertEqual(readrules.date_reading("The 12 Nov review is cancelled.", MON)[1], {dt.date(2026, 11, 12)})
        self.assertIsNone(due("Thursday's call is cancelled; no need to dial in."))
        self.assertIsNone(due("The dinner on Saturday is off. Let's find another date."))
        self.assertIsNone(due("The renewal on 1 Dec won't happen: we cancelled the plan."))
        new, old = readrules.date_reading("The review on 12 Nov is moved to 19 Nov.", MON)
        self.assertEqual((new, old), ({dt.date(2026, 11, 19)}, {dt.date(2026, 11, 12)}))
        self.assertEqual(due("The review on 12 Nov is moved to 19 Nov."), "2026-11-19")
        new, old = readrules.date_reading("We've rescheduled the shoot from Friday to Monday 16 Nov.", MON)
        self.assertNotIn(dt.date(2026, 11, 6), new)
        self.assertEqual(due("Update: the deadline is now 20 Nov (was 13 Nov)."), "2026-11-20")
        # another sentence of the message cancels it: never due anywhere
        self.assertIsNone(due("Lunch is on 12 Nov at noon. Sorry, the 12 Nov lunch is cancelled."))
        # a void invoice in one clause doesn't cancel the corrected one's date; "from 1 Dec" alone isn't old
        self.assertEqual(due("Invoice 2207 is void; a corrected invoice is due 25 Nov."), "2026-11-25")
        self.assertEqual(due("The price goes up to $99 a year from 1 Dec."), "2026-12-01")
        self.assertEqual(due("Let's meet Friday instead."), "2026-11-06")

    def test_2_weekday_and_date_must_agree(self):
        self.assertEqual(due("Kickoff is Tue 10 Nov."), "2026-11-10")
        self.assertIsNone(due("Kickoff is Wed 10 Nov."))       # disagree: nothing
        self.assertEqual(due("Due Friday, 6 Nov."), "2026-11-06")

    def test_3_weekday_said_on_that_day(self):
        self.assertEqual(due("Please pay by Monday."), "2026-11-09")
        self.assertEqual(due("It's Monday today, so please pay by Monday."), "2026-11-02")
        self.assertEqual(due("Can you send the notes by Friday?"), "2026-11-06")

    def test_7_eow_end_of_month_nth_and_numeric(self):
        self.assertEqual(due("Can you send the deck by EOW?"), "2026-11-06")
        self.assertEqual(due("Can you send it by end of the week?"), "2026-11-06")
        self.assertEqual(due("Due EOW.", sent=dt.date(2026, 11, 7)), "2026-11-13")   # a Saturday
        self.assertEqual(due("The report is due end of month."), "2026-11-30")
        self.assertEqual(due("Due by the end of the month.", sent=dt.date(2026, 12, 3)), "2026-12-31")
        self.assertEqual(due("Please pay by the 15th."), "2026-11-15")
        self.assertEqual(due("Please pay by the 1st."), "2026-12-01")
        self.assertEqual(due("Applications close on 30/11."), "2026-11-30")
        self.assertEqual(due("Applications close on 11/30."), "2026-11-30")
        self.assertIsNone(due("Due on 03/04/2027."))                                  # no date order: nothing
        self.assertEqual(due("Due on 03/04/2027.", order="dmy"), "2027-04-03")
        self.assertEqual(due("Due on 03/04/2027.", order="mdy"), "2027-03-04")
        self.assertIsNone(due("Half the team, 1/2 of us, will come.", order="dmy"))
        self.assertIsNone(due("The forms are due in two weeks."))                    # not made deterministic

    def test_8_invite_times_across_midnight(self):
        self.assertEqual(due("Call on Wed 11 Nov at 07:00 CET.", tz=LA), "2026-11-10")
        self.assertEqual(due("Call on Wed 11 Nov at 07:00 CET.", tz=LON), "2026-11-11")
        self.assertEqual(due("Budget review @ Tue 10 Nov 2026 9am - 10am (ET).", tz=LA), "2026-11-10")
        self.assertEqual(due("Join us Friday 13 Nov at 00:30 EST.", tz=LA), "2026-11-12")
        self.assertEqual(due("Our sync is Monday 9 Nov at 4pm PT.", tz=LON), "2026-11-10")
        self.assertEqual(due("The deadline is 23:59 AoE on 20 Nov.", tz=LON), "2026-11-21")
        self.assertEqual(due("Interview: Tue 17 Nov, 2:00 PM Eastern (11:00 AM Pacific).", tz=LA), "2026-11-17")


class Amounts(unittest.TestCase):
    def test_4_line_items_subtotals_and_totals(self):
        body = "Design: $1,800\nHosting: $200\nTotal due: $2,000 by 30 Nov"
        self.assertEqual(amount_of(body, "Design"), (None, "amount_not_total"))
        self.assertEqual(amount_of(body, "Total"), ("$2,000", "shown"))
        self.assertEqual(amount_of("Subtotal: £850.00", "Subtotal"), (None, "amount_line_item"))
        for line in ("Hours: 12 x $85", "Cleaning x2 at €75", "Tickets are $45 each.", "Per-seat price $80, 12 seats.",
                     "Item 1: Venue hire 2,000 EUR", "Line item: design $1,800"):
            self.assertEqual(amount_of(line, line[:5]), (None, "amount_line_item"), line)
        self.assertEqual(amount_of("You saved €12!", "saved"), (None, "amount_not_due"))
        self.assertEqual(amount_of("The total is $2,000. The deposit of $500 is due Friday.", "total"),
                         (None, "amount_ambiguous"))
        self.assertEqual(amount_of("Total including VAT: £1,020.", "Total"), ("£1,020", "shown"))
        self.assertEqual(amount_of("The invoice for $1,200 is due Friday.", "invoice"), ("$1,200", "shown"))

    def test_5_signatures_without_the_dashes(self):
        sig = readrules.strip_signature
        self.assertEqual(sig("Can you pay by Friday?\n\nBest,\nMarcus Webb\nFinance Lead\nTel +1 555 0100\n"
                             "Invoices over $5,000 need a PO."), "Can you pay by Friday?")
        self.assertEqual(sig("Your order ships Friday.\n\nAcme Ltd · 12 Mill Road · Free delivery over £40"),
                         "Your order ships Friday.")
        self.assertEqual(sig("Paid, thanks.\n\nAccounts Team\nLumen Ltd\nVAT No. GB123456789"), "Paid, thanks.")
        self.assertEqual(sig("Please review it.\n\n--\nDana\nPS: our day rate is $200."), "Please review it.")
        self.assertEqual(sig("New quote: CA$1,800.\n\nFrom: Dana <d@x.example>\nSent: Monday\nSubject: Re\n\nOld CA$2,100."),
                         "New quote: CA$1,800.")
        for keep in ("Hi,\nThanks.\nThe invoice is attached and due Friday.", "Please pay Lumen Ltd £400 by Friday.",
                     "Thanks! Paid.", "The fee is $300.\n\nCall me on 020 7946 0000 if that's a problem."):
            self.assertEqual(sig(keep), keep)

    def test_6_ranges_show_nothing(self):
        for body in ("The budget is $1–2k for the edit.", "Somewhere around 2-3k for the venue, budget wise.",
                     "The quote is 5–7k EUR for the week.", "We can pay between 500 and 800 dollars.",
                     "Expect between $500 and $800 for the repair.", "The fee is $1,000-$1,500.", "Costs run £40 to £60."):
            self.assertEqual(amount_of(body, body[:6]), (None, "amount_range"), body)
        self.assertEqual(amount_of("The budget is up to $2,000.", "budget"), ("$2,000", "shown"))
        self.assertFalse(readrules.money_range("Open Mon–Fri 9–5"))
        self.assertFalse(readrules.money_range("Call 555-0100 about the 10-12 Nov trip."))


class DateOrderSetting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(self.tmp.name)
        self.bg = background.Background(self.store)

    def test_saved_in_the_0122_key_and_shown_in_the_background_block(self):
        self.assertIsNone(self.bg.settings()["date_order"])
        v = self.bg.update_settings({"date_order": "dmy"})
        self.assertEqual(v["settings"]["date_order"], "dmy")
        self.assertEqual(background.date_order(self.store), "dmy")
        self.assertEqual(self.store._get_meta("reader", None)["date_order"], "dmy")  # noqa: SLF001
        self.bg.update_settings({"model": "example-model"})                 # another change keeps it
        self.assertEqual(self.bg.settings()["date_order"], "dmy")
        self.bg.update_settings({"date_order": None})
        self.assertIsNone(self.bg.settings()["date_order"])
        with self.assertRaises(background.InputError):
            self.bg.update_settings({"date_order": "ymd"})

    def test_a_0122_saved_choice_still_holds(self):
        self.store._set_meta("reader", {"on": True, "date_order": "mdy"})  # noqa: SLF001
        self.assertEqual(self.bg.settings()["date_order"], "mdy")


class LedgerReadUsesTheOrder(T.Base):
    """extract.Extractor passes the owner's order to the date rules: a date the model left out is taken from the
    phrase only when the order makes 03/04/2027 one date."""

    def _read(self, order):
        self.ex.date_order = lambda: order
        res = self.read("m-" + str(order), "2027-03-01T09:00-08:00",
                        {"items": [T.item(w=T.when("due on 03/04/2027", "", "17:00"))], "updates": []},
                        text="The forms are due on 03/04/2027 at 5pm.")
        return self.show(res["items"][0])["when"]

    def test_dmy(self):
        self.assertEqual((self._read("dmy")["start"] or "")[:10], "2027-04-03")

    def test_mdy(self):
        self.assertEqual((self._read("mdy")["start"] or "")[:10], "2027-03-04")

    def test_no_order_leaves_it_open(self):
        w = self._read(None)
        self.assertNotIn((w["start"] or "")[:10], ("2027-04-03", "2027-03-04"))


if __name__ == "__main__":
    unittest.main()
