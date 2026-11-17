"""0.22.x: gaps the real-mail acceptance set found, each on synthetic fixtures (fictional people and shops):
- clashes: the owner's trip of several days against any busy timed entry during it (a meeting, a solo dentist visit); an
  ask whether someone can be there against a tentative or multi-day all-day entry; a free (transparent) entry never
  clashes;
- "tomorrow" sent after midnight: later today from a person, the calendar's tomorrow from an automated sender;
- two orders' deliveries in one shop thread stay two items (one package only by order or tracking number, or by the
  item and its date);
- an appointment mail links to its calendar entry (time overlap and a shared word) instead of clashing with it;
- the replay feeds the extractor what live mail does (display name, account, cc, labels, bulk, sender flags,
  contacts, the owner's RSVP and free/busy on calendar entries);
- a whole day written as times (00:00-23:59, 00:00-24:00, midnight to midnight) is an all-day date or span, and
  doesn't clash with the timed entries in it.
In-process fakes only: no model server, no network."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import TZ, Base, item, ts, when  # noqa: E402

import ledger  # noqa: E402
import replay_ledger as R  # noqa: E402


def event(eid, title, start, end, **kw):
    return dict({"account": "a", "calendar": "c", "event_id": eid, "title": title, "start_ts": ts(start),
                 "end_ts": ts(end), "status": "confirmed"}, **kw)


def day_event(eid, title, d, days=1, **kw):
    start = ts(f"{d}T00:00-08:00")
    return dict({"account": "a", "calendar": "c", "event_id": eid, "title": title, "all_day": True,
                 "start_ts": start, "end_ts": start + 86400 * days, "status": "confirmed"}, **kw)


GUEST = [{"name": "Priya Nair", "address": "priya@studio.example", "response": "accepted", "self": False},
         {"name": "", "address": "me", "response": "needsAction", "self": True}]


class TestTripAgainstAMeeting(Base):
    def stay(self, mid="h1"):
        return self.read(mid, "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", "stay at Maple Inn", when("Check-in Mar 8, check-out Mar 10", "2027-03-08",
                                                          end_date="2027-03-10", all_day=True), subtype="trip")],
            "updates": []}, source="gmail", sender="reservations@mapleinn.example", name="Maple Inn",
            thread="mail-h1")

    def test_a_meeting_during_his_stay_is_a_clash(self):
        self.ms.upsert_events([event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00",
                                     attendees=GUEST)])
        self.stay()
        c = self.lv.conflicts()
        self.assertEqual([(x["kind"], x["confidence"]) for x in c], [("away", "sure")])   # M4: every item sure
        self.assertIn("away", c[0]["why"])

    def test_a_solo_dentist_visit_during_his_trip_is_a_clash(self):
        # owner's rule 2026-11-13: any busy timed entry inside the owner's trip or stay, whatever its attendees
        self.ms.upsert_events([event("e1", "Dentist", "2027-03-09T14:00-08:00", "2027-03-09T15:00-08:00")])
        self.stay()
        c = self.lv.conflicts()
        self.assertEqual([(x["kind"], x["confidence"]) for x in c], [("away", "sure")])   # M4: every item sure
        self.assertEqual([e["title"] for e in c[0]["entries"]], ["Dentist"])   # one grouped clash (0.22.x)

    def test_his_own_timed_blocks_during_it_clash_too(self):
        self.ms.upsert_events([event("e1", "Haircut", "2027-03-08T16:00-08:00", "2027-03-08T16:45-08:00"),
                               event("e2", "Dinner", "2027-03-09T18:30-08:00", "2027-03-09T19:30-08:00")])
        self.stay()
        c = self.lv.conflicts()             # the acceptance rules: ONE grouped clash per trip, listing both
        self.assertEqual([(x["kind"], x["confidence"]) for x in c], [("away", "sure")])   # M4: every item sure
        self.assertEqual([e["title"] for e in c[0]["entries"]], ["Haircut", "Dinner"])

    def test_the_trips_own_travel_entry_does_not(self):
        self.ms.upsert_events([event("e3", "Flight to Tromsø", "2027-03-08T07:00-08:00", "2027-03-08T09:00-08:00",
                                     attendees=GUEST)])
        self.stay()
        self.assertEqual(self.lv.conflicts(), [])

    def test_declined_all_day_and_outside_entries_do_not(self):
        declined = [GUEST[0], {"name": "", "address": "me", "response": "declined", "self": True}]
        self.ms.upsert_events([event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00",
                                     attendees=declined),
                               day_event("e2", "Studio build", "2027-03-09"),
                               event("e3", "Dentist", "2027-03-12T14:00-08:00", "2027-03-12T15:00-08:00")])
        self.stay()
        self.assertEqual(self.lv.conflicts(), [])

    def test_a_free_meeting_or_block_does_not(self):
        self.ms.upsert_events([event("e1", "Design review", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00",
                                     attendees=GUEST, transparency="transparent"),
                               event("e2", "Dentist", "2027-03-09T14:00-08:00", "2027-03-09T15:00-08:00",
                                     transparency="transparent")])
        self.stay()
        self.assertEqual(self.lv.conflicts(), [])

    def test_a_one_day_booking_does_not(self):
        self.ms.upsert_events([event("e1", "Design review", "2027-03-08T10:00-08:00", "2027-03-08T11:00-08:00",
                                     attendees=GUEST)])
        self.read("h1", "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", "day pass at Maple Spa", when("Mar 8", "2027-03-08", all_day=True), subtype="trip")],
            "updates": []}, source="gmail", sender="hello@maplespa.example", name="Maple Spa", thread="mail-h1")
        self.assertEqual(self.lv.conflicts(), [])


class TestAfterMidnightTomorrow(Base):
    """A person's "tomorrow" sent between midnight and 4 am means later today; an automated sender's (a shop, carrier,
    bank or notification) is the calendar's tomorrow (0.22.x)."""
    AT = "2027-03-06T03:10-08:00"

    def delivery(self, row_extra=None, **kw):
        ans = {"items": [item("event", "sender", "package arriving", when("Arriving tomorrow", "2027-03-07",
                                                                          all_day=True), conf="likely",
                              subtype="delivery")], "updates": []}
        self.fake.answers["m1"] = ans
        row = dict(self.msg("m1", self.AT, text="Your order is arriving tomorrow.", source="gmail",
                            thread="mail-m1", **kw), **(row_extra or {}))
        self.ex.read(row)
        return self.show(next(iter(self.items())))

    def plan(self, **kw):
        self.fake.answers["m1"] = {"items": [item(w=when("tomorrow at 10", "2027-03-07", "10:00"))], "updates": []}
        self.ex.read(self.msg("m1", self.AT, text="Can we do tomorrow at 10?", source="gmail", thread="mail-m1", **kw))
        return self.show(next(iter(self.items())))

    def test_a_person_by_mail_means_later_today(self):
        it = self.plan(sender="dana.reyes@example.com", name="Dana Reyes")
        self.assertEqual((it["when"]["start"], it["confidence"]), ("2027-03-06T10:00-08:00", "likely"))
        self.assertTrue(any("after midnight" in c.get("why", "") for c in it["checks"]))

    def test_a_no_reply_shop_means_the_calendars_tomorrow(self):
        it = self.delivery(sender="no-reply@shopwell.example", name="Shopwell")
        self.assertEqual((it["when"]["start_date"], it["when"]["end_date"]), ("2027-03-07", "2027-03-07"))
        self.assertFalse(any("after midnight" in c.get("why", "") for c in it["checks"]))

    def test_a_role_address_or_a_category_tab_too(self):
        it = self.delivery(sender="orders@shopwell.example", name="Shopwell")
        self.assertEqual(it["when"]["start_date"], "2027-03-07")
        self.tearDown()
        self.setUp()
        it = self.delivery(sender="sam.lee@shopwell.example", name="Shopwell", labels=["CATEGORY_UPDATES"])
        self.assertEqual(it["when"]["start_date"], "2027-03-07")       # read narrowly: no person behind it

    def test_the_replays_flags_decide_for_hashed_addresses(self):
        it = self.delivery({"automated_sender": True, "role_sender": True}, sender="p_abc", name="Shopwell")
        self.assertEqual(it["when"]["start_date"], "2027-03-07")
        self.tearDown()
        self.setUp()
        it = self.delivery({"automated_sender": False, "role_sender": False}, sender="p_abc", name="Dana Reyes")
        self.assertEqual(it["when"]["start_date"], "2027-03-06")        # a person: later today

    def test_machine_sender(self):
        from extract import machine_sender
        self.assertTrue(machine_sender({"source": "gmail", "direction": "in", "sender": "p:auto"}))
        self.assertTrue(machine_sender({"source": "gmail", "direction": "in", "sender": "x"}, "narrow"))
        self.assertFalse(machine_sender({"source": "gmail", "direction": "in", "sender": "dana@example.com"}, "full"))
        self.assertFalse(machine_sender({"source": "signal", "direction": "in", "sender": "no-reply"}))
        self.assertFalse(machine_sender({"source": "gmail", "direction": "out", "sender": "me"}))


class TestSomeoneThere(Base):
    def ask(self, mid="m1", what="say whether someone can be there to let the plumber in", text=""):
        return self.read(mid, "2027-03-01T09:00-08:00", {"items": [item(what=what, w=when(
            "Thursday at 2:40 PM", "2027-03-11", "14:40"))], "updates": []}, source="gmail",
            sender="office@maplecourt.example", name="Maple Court Office", thread=f"mail-{mid}",
            text=text or "Can someone be there to let the plumber in Thursday at 2:40 PM? He needs about an hour.")

    def test_a_tentative_all_day_entry_is_a_likely_clash(self):
        self.ms.upsert_events([day_event("e1", "Studio build", "2027-03-11", status="tentative")])
        self.ask()
        c = self.lv.conflicts()
        self.assertEqual([(x["kind"], x["confidence"]) for x in c], [("away", "likely")])
        self.assertIn("calendar holds that day", c[0]["why"])

    def test_his_maybe_and_a_multi_day_entry_count_too(self):
        self.ms.upsert_events([day_event("e1", "Studio build", "2027-03-11", attendees=GUEST[:1] + [
            {"name": "", "address": "me", "response": "tentative", "self": True}]),
            day_event("e2", "Spring market", "2027-03-10", days=3)])
        self.ask()
        self.assertEqual(sorted(x["kind"] for x in self.lv.conflicts()), ["away", "away"])

    def test_free_plain_and_soft_entries_and_other_asks_do_not(self):
        self.ms.upsert_events([day_event("e1", "Studio build", "2027-03-11", status="tentative",
                                         transparency="transparent"),
                               day_event("e2", "Rent due", "2027-03-11"),
                               day_event("e3", "Reminder: recycling", "2027-03-10", days=3)])
        self.ask()
        self.assertEqual(self.lv.conflicts(), [])

    def test_an_ask_for_something_else_does_not(self):
        self.ms.upsert_events([day_event("e1", "Spring market", "2027-03-10", days=3, status="tentative")])
        self.ask(what="send the signed lease", text="Please send the signed lease by Thursday 2:40 PM.")
        self.assertEqual(self.lv.conflicts(), [])


class TestDeliveries(Base):
    def mail(self, mid, at, what, d, text, thread="mail-shop"):
        return self.read(mid, at, {"items": [item("event", "sender", what, when("Arriving " + d, d, all_day=True),
                                                  conf="likely", subtype="delivery")], "updates": []},
                         source="gmail", sender="ship-confirm@shop.example", name="Shopwell", thread=thread, text=text)

    def test_two_orders_in_one_thread_stay_two_items(self):
        a = self.mail("d1", "2027-03-01T09:00-08:00", "Shopwell delivery: ceramic mugs", "2027-03-02",
                      "Your package with ceramic mugs arrives tomorrow.")
        b = self.mail("d2", "2027-03-01T15:00-08:00", "Shopwell delivery: desk lamp", "2027-03-02",
                      "Your package with a desk lamp arrives tomorrow.")
        self.assertNotEqual(a["items"][0], b["items"][0])
        self.assertEqual(len([i for i in self.lv.items() if i["subtype"] == "delivery"]), 2)

    def test_the_same_order_number_is_one_package(self):
        a = self.mail("d1", "2027-03-01T09:00-08:00", "Shopwell order shipped", "2027-03-04",
                      "Order #SW-20417 has shipped.")
        b = self.mail("d2", "2027-03-03T09:00-08:00", "Shopwell package out for delivery", "2027-03-03",
                      "Order #SW-20417 is out for delivery.", thread="mail-shop-2")
        self.assertEqual(a["items"][0], b["items"][0])

    def test_different_order_numbers_never_merge_even_for_the_same_item(self):
        a = self.mail("d1", "2027-03-01T09:00-08:00", "Shopwell delivery: ceramic mugs", "2027-03-02",
                      "Order #SW-20417: ceramic mugs arrive tomorrow.")
        b = self.mail("d2", "2027-03-01T10:00-08:00", "Shopwell delivery: ceramic mugs", "2027-03-02",
                      "Order #SW-20488: ceramic mugs arrive tomorrow.")
        self.assertNotEqual(a["items"][0], b["items"][0])

    def test_the_same_item_on_meeting_dates_is_one_package(self):
        a = self.mail("d1", "2027-03-01T09:00-08:00", "Shopwell order: ceramic mugs", "2027-03-04",
                      "Thanks for your order of ceramic mugs.")
        b = self.mail("d2", "2027-03-02T09:00-08:00", "Shopwell shipped: ceramic mugs", "2027-03-03",
                      "Your ceramic mugs have shipped.", thread="mail-shop-2")
        self.assertEqual(a["items"][0], b["items"][0])

    def test_order_refs(self):
        self.assertEqual(ledger.order_refs("Order #SW-20417 shipped; tracking number 1Z12345E0291980793"),
                         {"sw-20417", "1z12345e0291980793"})
        self.assertEqual(ledger.order_refs("Your package is on the way"), set())


class TestAppointmentMail(Base):
    def visit(self, mid, start, what="video visit with Dr. Lin", subject="Your visit is confirmed", name="Cedar Clinic"):
        return self.read(mid, "2027-03-01T09:00-08:00", {"items": [item(
            "event", "sender", what, when("Thu 1:00 PM", start[:10], start[11:16]), subtype="appointment")],
            "updates": []}, source="gmail", sender="no-reply@cedarclinic.example", name=name, thread=f"mail-{mid}",
            subject=subject)

    def test_the_entry_that_blocks_more_time_is_the_same_visit(self):
        self.ms.upsert_events([event("e1", "Dr. Lin", "2027-03-04T12:30-08:00", "2027-03-04T13:30-08:00")])
        res = self.visit("a1", "2027-03-04T13:00")
        it = self.show(res["items"][0])
        self.assertIsNotNone(it["cal_ref"])
        self.assertEqual(self.lv.conflicts(), [])

    def test_a_word_from_the_subject_or_the_provider_is_enough(self):
        self.ms.upsert_events([event("e1", "Cedar telehealth", "2027-03-04T10:00-08:00", "2027-03-04T11:00-08:00")])
        res = self.visit("a1", "2027-03-04T10:00", what="appointment", subject="Reminder: upcoming telehealth visit")
        self.assertIsNotNone(self.show(res["items"][0])["cal_ref"])

    def test_no_shared_word_or_no_overlap_stays_apart_and_clashes(self):
        self.ms.upsert_events([event("e1", "Budget sync", "2027-03-04T12:30-08:00", "2027-03-04T13:30-08:00"),
                               event("e2", "Dr. Lin", "2027-03-05T13:00-08:00", "2027-03-05T14:00-08:00")])
        res = self.visit("a1", "2027-03-04T13:00")
        self.assertIsNone(self.show(res["items"][0])["cal_ref"])
        self.assertEqual(len(self.lv.conflicts()), 1)

    def test_a_weekday_or_time_word_is_not_a_shared_word(self):
        self.ms.upsert_events([event("e1", "Thu standup", "2027-03-04T13:00-08:00", "2027-03-04T13:30-08:00")])
        res = self.visit("a1", "2027-03-04T13:00", subject="Your visit Thu 1 PM")
        self.assertIsNone(self.show(res["items"][0])["cal_ref"])


class TestWholeDayAsTimes(Base):
    """Some models write a bill's due date as 00:00-23:59 on that day instead of all_day (the real-mail set)."""
    def due(self, w, mid="b1", what="Water bill due", subtype="bill"):
        res = self.read(mid, "2027-03-01T09:00-08:00", {"items": [item("event", "sender", what, w,
                                                                       subtype=subtype)], "updates": []},
                        source="gmail", sender="billing@water.example", name="Citywater", text="Your bill is due.")
        return self.show(res["items"][0])

    def test_midnight_to_2359_is_all_day(self):
        it = self.due(when("due March 9", "2027-03-09", "00:00", "23:59", "2027-03-09"))
        self.assertTrue(it["when"]["all_day"])
        self.assertEqual((it["when"]["start_date"], it["when"]["end_date"]), ("2027-03-09", "2027-03-09"))
        self.assertIn("a whole day written as times; read as all-day", {c["why"] for c in it["checks"]})
        self.assertEqual(it["confidence"], "sure")

    def test_without_end_date_24_00_and_midnight_to_midnight(self):
        for i, w in enumerate((when("due March 9", "2027-03-09", "00:00", "23:59"),
                               when("due March 9", "2027-03-09", "00:00", "24:00"),
                               when("due March 9", "2027-03-09", "00:00", "00:00", "2027-03-10"))):
            it = self.due(w, mid=f"b{i}")
            self.assertTrue(it["when"]["all_day"], i)
            self.assertEqual((it["when"]["start_date"], it["when"]["end_date"]), ("2027-03-09", "2027-03-09"), i)

    def test_a_span_of_days(self):
        it = self.due(when("March 9 to 11", "2027-03-09", "00:00", "23:59", "2027-03-11"))
        self.assertEqual((it["when"]["all_day"], it["when"]["start_date"], it["when"]["end_date"]),
                         (True, "2027-03-09", "2027-03-11"))
        it = self.due(when("March 9 to 11", "2027-03-09", "00:00", "00:00", "2027-03-12"), mid="b2")
        self.assertEqual((it["when"]["start_date"], it["when"]["end_date"]), ("2027-03-09", "2027-03-11"))

    def test_real_midnight_times_stay_timed(self):
        for i, w in enumerate((when("at midnight", "2027-03-09", "00:00"),
                               when("midnight to 1", "2027-03-09", "00:00", "01:00"),
                               when("11 pm to midnight", "2027-03-09", "23:00", "23:59"))):
            it = self.due(w, mid=f"t{i}")
            self.assertFalse(it["when"]["all_day"], i)
            self.assertIsNotNone(it["when"]["start"], i)

    def test_no_clash_with_a_meeting_that_day(self):
        self.ms.upsert_events([event("e1", "Budget sync", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00")])
        self.due(when("on March 9", "2027-03-09", "00:00", "23:59", "2027-03-09"), what="Street fair", subtype="")
        self.assertEqual(self.lv.conflicts(), [])


class TestReplayFeedsWhatLiveDoes(unittest.TestCase):
    def mail(self, **kw):
        return dict({"mid": "m", "source": "mail", "direction": "in", "sender": "p_abc", "to": ["owner"],
                     "ts": "2027-03-01T06:00-08:00", "text": "x", "chat": "c_1", "chat_kind": "thread"}, **kw)

    def test_display_name_account_cc_labels_and_bulk(self):
        row = R.msg_row(self.mail(sender_name="Maple Court Office", account="acct1", cc=["p_def"],
                                  labels=["INBOX"], gmail_category="updates", bulk=None,
                                  list_unsubscribe="<mailto:x>"), "Jordan Vale", {})
        self.assertEqual((row["sender_name"], row["account"]), ("Maple Court Office", "acct1"))
        self.assertEqual([(r["address"], r["kind"]) for r in row["recipients"]], [("me", "to"), ("p_def", "cc")])
        self.assertEqual(row["labels"], ["INBOX", "CATEGORY_UPDATES"])     # Gmail's labelIds, no chat kind
        self.assertTrue(row["bulk"])                                       # from the List-Unsubscribe header
        plain = R.msg_row(self.mail(), "Jordan Vale", {})
        self.assertEqual((plain["sender_name"], plain["account"], plain["labels"], plain["bulk"]),
                         ("p_abc", "replay", [], False))
        sig = R.msg_row(self.mail(source="signal", chat_kind="dm"), "Jordan Vale", {})
        self.assertEqual(sig["labels"], ["dm"])

    def test_sender_flags_route_like_live(self):
        ctx = {"sender": ("p_abc", "Maple Court Office"), "owner_names": {"jordan vale", "jordan"}}
        text = "Hi Jordan, can someone be there for the plumber Thursday?"
        base = R.msg_row(self.mail(text=text, labels=["CATEGORY_UPDATES"]), "Jordan Vale", {})
        person = dict(base, **R.sender_flags(self.mail(automated_sender=False, role_sender=False)))
        self.assertEqual(R.replay_tier(person, ctx)[0], "full")             # asks the owner by name through a mailing tool
        role = dict(base, **R.sender_flags(self.mail(automated_sender=False, role_sender=True)))
        self.assertEqual(R.replay_tier(role, ctx), ("narrow", "updates"))   # office@: a role address
        auto = dict(base, **R.sender_flags(self.mail(automated_sender=True, role_sender=True)))
        self.assertEqual(R.replay_tier(auto, ctx), ("narrow", "automated, updates"))
        old = dict(R.msg_row(self.mail(sender="p:auto"), "Jordan Vale", {}), **R.sender_flags(self.mail(sender="p:auto")))
        self.assertEqual(R.replay_tier(old, {"sender": ("p:auto", ""), "owner_names": set()}), ("narrow", "automated"))

    def test_contacts_take_the_known_person_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "contacts.json"
            p.write_text(json.dumps({"known": ["p_abc"]}), encoding="utf-8")
            contacts = R.load_contacts(p)
            self.assertEqual(R.load_contacts(pathlib.Path(d) / "none.json"), {})
        row = dict(R.msg_row(self.mail(labels=["CATEGORY_PROMOTIONS"]), "Jordan Vale", {}),
                   **R.sender_flags(self.mail(automated_sender=False, role_sender=False)))
        ctx = {"sender": ("p_abc", ""), "owner_names": set()}
        self.assertEqual(R.replay_tier(row, ctx), ("narrow", "promotions"))
        self.assertEqual(R.replay_tier(row, ctx, contacts), ("full", "a known person, promotions"))

    def test_calendar_rows_keep_rsvp_and_free_busy(self):
        rows = R.cal_rows([{"id": "cal:1", "title": "Studio build", "all_day": True, "start": "2027-03-09",
                            "end": "2027-03-12", "rsvp": "needsAction", "status": "tentative"},
                           {"id": "cal:2", "title": "Focus", "all_day": False, "start": "2027-03-09T10:00-08:00",
                            "end": "2027-03-09T11:00-08:00", "transparency": "transparent",
                            "responseStatus": "tentative"}], ledger.zone(TZ))
        self.assertEqual((rows[0]["status"], rows[0]["attendees"][-1]["response"], rows[0]["attendees"][-1]["self"]),
                         ("tentative", "needsAction", True))
        self.assertEqual((rows[0]["start"], rows[0]["end"]), ("2027-03-09", "2027-03-11"))
        self.assertEqual((rows[1]["transparency"], rows[1]["attendees"][-1]["response"]), ("transparent", "tentative"))


if __name__ == "__main__":
    unittest.main()
