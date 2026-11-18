"""0.24.0: the filtering fixes from the owner's secret-shopper audit. Fictional people, mail and calendars only.

Shared calendars (`self` is the calendar, not him), the clash rules, one row per thread, duplicate alerts, direction
(Waiting on others), deliveries never late, Trash closes items, sender reputation, the owner's sender rules with one
Undo, Move back to Needs you, and the one-time Gmail label refresh."""
import json
import pathlib
import sys
import unittest
import zoneinfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import TZ, Base, item, ts, when  # noqa: E402
from test_need_0230 import NeedBase, att, ev, guest  # noqa: E402

import filters as F  # noqa: E402
import google  # noqa: E402
import heads  # noqa: E402
import ledger as L  # noqa: E402
import live  # noqa: E402
import need as N  # noqa: E402
import tidy  # noqa: E402
import today  # noqa: E402

ZONE = zoneinfo.ZoneInfo(TZ)
KIM = {"name": "Kim Rowe", "address": "kim@home.example", "self": True}


class TestSharedCalendar(NeedBase):
    def test_self_on_a_shared_calendar_is_the_calendar(self):
        self.put(ev("f1", "Dentist check-up", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00", cal="family",
                    organizer="lee@work.example", attendees=[dict(KIM, response="accepted"), guest("Lee Park")]))
        n = self.need("f1")
        self.assertNotEqual(n["need"], "yes")                   # not "you said yes": Kim did
        self.assertTrue(n.get("shared"))
        self.assertIn("not as you", n["why"])

    def test_his_own_address_on_a_shared_calendar_is_him(self):
        self.ms.write(lambda db: N.sync_calendars(db, [{"id": "acc", "calendars": [
            {"id": "primary", "summary": "Sam", "primary": True}, {"id": "family", "summary": "Family calendar"}]}],
            owner_addrs=("sam@home.example",)))
        self.put(ev("f2", "Parent evening", "2027-03-02T18:00-08:00", "2027-03-02T19:00-08:00", cal="family",
                    organizer="lee@work.example", attendees=[att("accepted"), guest("Lee Park")]))
        self.assertEqual(self.need("f2")["need"], "yes")

    def test_no_rsvp_ask_for_someone_elses_invite(self):
        self.put(ev("p1", "Design review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00",
                    organizer="ada@work.example", attendees=[att(), guest("Ada Lead")], ical_uid="p1@x"),
                 ev("f3", "Kim's team offsite", "2027-03-03T10:00-08:00", "2027-03-03T11:00-08:00", cal="family",
                    organizer="ada@work.example", attendees=[dict(KIM, response="needsAction"), guest("Ada Lead")],
                    ical_uid="f3@x"))
        out = self.ms.write(lambda db: heads.sync_invites(db, self.now[0], TZ))
        whats = [self.lv.item(i, detail=False)["what"] for i in out["created"]]
        self.assertEqual(whats, ["Design review"])


class TestClashRules(unittest.TestCase):
    @staticmethod
    def s(title, start=100, uid="", org="", invite=False, cancelled=False, event=None):
        return {"title": title, "start": start, "uid": uid, "organizer": org, "invite": invite,
                "cancelled": cancelled, "event": event}

    def test_what_is_not_a_clash(self):
        s = self.s
        self.assertEqual(F.clash_drop(s("Standup", event=5), s("Standup", start=200, event=5)), "the same event")
        self.assertEqual(F.clash_drop(s("Team sync", uid="abc@x"), s("Invitation: Team sync", 300, uid="abc@x")),
                         "the same event")                              # his own invite email for the entry
        self.assertEqual(F.clash_drop(s("Lunch with Lee"), s("Lunch with Lee at Rosa's")), "the same event")
        self.assertEqual(F.clash_drop(s("Yoga", uid="r1_R20270301T170000", invite=True),
                                      s("Pottery", 150, uid="r1", invite=True)), "one series")
        self.assertEqual(F.clash_drop(s("Office hours - Week 3", invite=True, org="svc@x.example"),
                                      s("Office hours - Week 4", 150, invite=True, org="svc@x.example")),
                         "one series or service")
        self.assertEqual(F.clash_drop(s(""), s("Dentist")), "an empty side")
        self.assertEqual(F.clash_drop(s("Dentist", cancelled=True), s("Lunch")), "cancelled")

    def test_a_real_clash_stays(self):
        self.assertEqual(F.clash_drop(self.s("Dentist"), self.s("Board meeting", 150)), "")
        self.assertEqual(F.clash_drop(self.s("Yoga", invite=True, org="a@x.example"),
                                      self.s("Budget review", 150, invite=True, org="b@y.example")), "")

    def test_similar_and_alert_key(self):
        self.assertTrue(F.similar("send the signed lease", "Re: signed lease copy back"))
        self.assertFalse(F.similar("send the signed lease", "book the dentist"))
        t = ts("2027-03-01T09:00-08:00")
        self.assertEqual(F.alert_key("Bank", "New sign-in on 2 devices", t, ZONE),
                         F.alert_key("bank", "New sign-in on 3 devices", t + 600, ZONE))
        self.assertNotEqual(F.alert_key("Bank", "New sign-in", t, ZONE), F.alert_key("Bank", "New sign-in", t + 86400, ZONE))


class TestOneOfEach(Base):
    def test_duplicate_security_alerts_are_one_card(self):
        td = today.Today(self.ms, self.lv, lambda: self.now[0])
        t = ts("2027-03-01T07:00-08:00")
        sec = [{"id": i, "from": [], "who_name": "Northbank", "who": "alerts@northbank.example", "created_at": t + i,
                "what": "New sign-in to your account"} for i in (1, 2)]
        sec.append({"id": 3, "from": [], "who_name": "Mailhost", "who": "no-reply@mailhost.example", "created_at": t,
                    "what": "Password changed"})
        rows = td._security({"security": sec}, self.now[0], ZONE)  # noqa: SLF001
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["same"], [1])                                  # the twin is kept, said once

    def test_one_row_per_thread(self):
        kw = dict(source="gmail", sender="dana@mail.example", name="Dana", thread="T1")
        a = self.read("m1", "2027-03-01T09:00-08:00", {"items": [item("ask", "Dana", "send the signed lease")],
                                                       "updates": []}, **kw)["items"][0]
        b = self.read("m2", "2027-03-01T10:00-08:00", {"items": [item("ask", "Dana", "book the dentist")],
                                                       "updates": []}, **dict(kw, thread="T2"))["items"][0]
        c = self.read("m3", "2027-03-01T10:05-08:00", {"items": [item("ask", "Dana", "water the plants")],
                                                       "updates": []}, **dict(kw, thread="T3"))["items"][0]
        # rows read before the read-time merge: a second ask from the same thread about the same thing
        self.ms.write(lambda db: (db.execute("UPDATE ledger_items SET what='signed lease copy back' WHERE id=?", (b,)),
                                  db.execute("UPDATE messages SET thread='T1' WHERE msg_id IN ('m2', 'm3')")))
        got = self.ms.write(lambda db: F.thread_merges(db, self.now[0]))
        self.assertEqual(got, [(a, b)])
        open_ids = {i["id"] for i in self.lv.items() if i["merged_into"] is None and i["status"] == "open"}
        self.assertEqual(open_ids, {a, c})                                      # c: another ask in the thread
        self.assertIn("merged", [h["kind"] for h in self.lv.item(b)["history"]])

    def test_trash_closes_its_items_but_never_an_alert(self):
        kw = dict(source="gmail", sender="shop@deals.example", name="Deals", thread="T9", labels=("INBOX",))
        a = self.read("t1", "2027-03-01T09:00-08:00", {"items": [item("ask", "Deals", "claim your voucher")],
                                                       "updates": []}, **kw)["items"][0]
        s = self.read("t2", "2027-03-01T09:01-08:00", {"items": [item("ask", "Deals", "confirm the new sign-in",
                                                                      subtype="security")], "updates": []},
                      **dict(kw, thread="T10"))["items"][0]
        self.ms.write(lambda db: db.execute("UPDATE messages SET labels=? WHERE source='gmail'",
                                            (json.dumps(["TRASH"]),)))
        closed = self.ms.write(lambda db: F.close_trashed(db, self.now[0]))
        self.assertEqual(closed, [a])
        self.assertEqual(self.lv.item(a, detail=False)["status"], "done")
        self.assertEqual(self.lv.item(s, detail=False)["status"], "open")


class TestDirection(unittest.TestCase):
    @staticmethod
    def it(**kw):
        return dict({"type": "ask", "subtype": "", "status": "open", "stage": "", "what": "", "approx": False,
                     "who": "sender", "phrase": "", "owed": "by_owner"}, **kw)

    def test_deliveries_and_returns_are_never_late(self):
        self.assertFalse(L.has_deadline(self.it(subtype="delivery", what="your parcel arrives")))
        self.assertFalse(L.has_deadline(self.it(subtype="return", what="return the jacket", phrase="by Friday")))
        self.assertFalse(L.has_deadline(self.it(stage="shipped", what="order on its way")))
        self.assertTrue(L.has_deadline(self.it(subtype="bill", what="pay the water bill")))

    def test_what_waits_on_others(self):
        self.assertEqual(L.waiting_why(self.it(owed="to_owner"), {"from": []}), "they owe you this")
        self.assertEqual(L.waiting_why(self.it(status="waiting_on_them", owed=""), {"from": []}), "they owe you this")
        out = {"from": [{"role": "created", "direction": "out"}]}
        # his own promises are his, with a time or without one: Needs you, never Waiting on others
        self.assertEqual(L.waiting_why(self.it(type="commitment", who=L.OWNER), out), "")
        self.assertEqual(L.waiting_why(self.it(type="commitment", who=L.OWNER, owed="to_owner"), out), "")
        self.assertEqual(L.waiting_why(self.it(type="commitment", who=L.OWNER, start_ts=1.0), out), "")
        self.assertEqual(L.waiting_why(self.it(type="commitment", who="sender", owed="to_owner"), out),
                         "they owe you this")                     # someone else's promise to him waits on them
        self.assertEqual(L.waiting_why(self.it(), out), "")       # an ask of him quoted in his own sent mail is his
        self.assertEqual(L.waiting_why(self.it(), {"from": [{"role": "created", "direction": "in"}]}), "")


class TestSenders(Base):
    def setUp(self):
        super().setUp()
        F.forget()

    def mail(self, mid, at, sender, what, thread, text="", subtype=""):
        return self.read(mid, at, {"items": [item("ask", "x", what, subtype=subtype)], "updates": []}, source="gmail",
                         sender=sender, name=sender.split("@")[0].title(), thread=thread, subject=what,
                         text=text, labels=("INBOX",))["items"][0]

    def test_reputation(self):
        for i in range(4):
            self.mail(f"n{i}", f"2027-03-0{i + 1}T09:00-08:00", "offers@news.travelco.example", f"getaway {i}",
                      f"N{i}", text="Deals inside. Unsubscribe here.")
            self.mail(f"d{i}", f"2027-03-0{i + 1}T10:00-08:00", "dana@mail.example", f"question {i}", f"D{i}",
                      text="Unsubscribe me from the club list, can you?")
            self.msg(f"o{i}", f"2027-03-0{i + 1}T11:00-08:00", "sure", direction="out", source="gmail",
                     thread=f"D{i}", to=[("dana@mail.example", "Dana")])
        rep = F.Reputation.load(self.ms, fresh=True)
        self.assertEqual(rep.bulk("offers@news.travelco.example"), "a list sender you never answer")
        self.assertEqual(rep.bulk("dana@mail.example"), "")                     # he answers her

    def test_sender_rule_one_undo_and_move_back(self):
        a = self.mail("p1", "2027-03-01T09:00-08:00", "hello@shop.example", "pick a gift for Dana", "P1")
        b = self.mail("p2", "2027-03-01T09:05-08:00", "hello@shop.example", "decide on the colour", "P2")
        s = self.mail("p3", "2027-03-01T09:10-08:00", "hello@shop.example", "new sign-in", "P3", subtype="security")
        td = today.Today(self.ms, self.lv, lambda: self.now[0])
        td.tidy = tidy.Tidy(self.ms, lambda: self.now[0], owner_names=lambda: ["Jordan Vale"])
        with self.assertRaises(today.Invalid):
            td.sender_rule(s, "never")                                          # alerts always show
        r = td.sender_rule(a, "never")
        self.assertEqual(r["label"], "Never from this sender: Hello")
        self.assertEqual(self.lv.item(a, detail=False)["status"], "done")
        self.assertEqual(self.lv.item(b, detail=False)["type"], "cleanup")
        self.assertEqual(self.lv.item(s, detail=False)["type"], "ask")         # never hidden
        td.sender_rule_undo(r["rule"])
        self.assertEqual(self.lv.item(a, detail=False)["status"], "open")
        self.assertEqual(self.lv.item(b, detail=False)["type"], "ask")
        self.assertEqual(F.rules_of(self.ms), {})
        r = td.sender_rule(a, "promo")
        self.assertEqual(td.move_back(b)["item"], b)                            # "Move back to Needs you"
        self.assertEqual(self.lv.item(b, detail=False)["type"], "ask")


class FakeLabels:
    def __init__(self):
        self.calls = []

    def message_labels(self, tok, mid):
        self.calls.append(mid)
        if mid == "gone":
            raise google.GoogleError("Requested entity was not found.", status=404)
        hd = [{"name": "List-Unsubscribe", "value": "<mailto:out@list.example>"}] if mid == "promo" else []
        return {"id": mid, "labelIds": ["CATEGORY_PROMOTIONS", "INBOX"] if mid == "promo" else ["INBOX"],
                "payload": {"headers": hd}}


class FakeTokens:
    def run(self, aid, fn, *a, **kw):
        return fn(aid, *a, **kw)


class FakeStore:
    def accounts(self):
        return [{"id": "acct", "email": "jordan@home.example", "services": {}}]


class TestLabelRefresh(Base):
    def test_one_pass_fills_labels_and_bulk_and_notes_gone_mail(self):
        for mid in ("promo", "plain", "gone"):
            self.msg(mid, "2027-03-01T09:00-08:00", "hi", source="gmail", sender="x@list.example", thread=mid)
        self.msg("new", "2027-03-01T09:00-08:00", "hi", source="gmail", sender="x@list.example", labels=("INBOX",))
        api, seen = FakeLabels(), []
        r = live.GmailReader(FakeStore(), self.ms, FakeTokens(), api, lambda: self.now[0], sleep=lambda s: None)
        r.on_refreshed = lambda aid, ids: seen.append((aid, sorted(ids)))
        cur = r.labels_pass("acct")
        self.assertEqual(sorted(api.calls), ["gone", "plain", "promo"])         # only the unlabelled ones
        self.assertTrue(cur["done"])
        self.assertEqual((cur["total"], cur["read"]), (3, 3))
        m = self.ms.message("gmail", "acct", "promo")
        self.assertIn("CATEGORY_PROMOTIONS", json.loads(m["labels"]))
        self.assertEqual(m["bulk"], 1)
        self.assertIsNotNone(self.ms.message("gmail", "acct", "gone")["deleted_at"])   # recorded, never removed
        self.assertEqual(seen, [("acct", ["gone", "plain", "promo"])])
        self.assertEqual(r.labels_pass("acct")["done"], True)
        self.assertEqual(len(api.calls), 3)                                     # once


class TestValidation(Base):
    """0.24.0 validation on the owner's data (fictional stand-ins here): his promises stay his, what others promised
    him waits on them instead of leaving Today, a real bill is never a promotion on sender history alone, and list mail
    that books nothing is never a clash side."""

    def test_his_promise_with_a_guessed_time_stays_in_needs_you(self):
        a = self.read("w1", "2027-03-01T09:00-08:00", {"items": [item(
            "commitment", "owner", "send the mockups once the parts come", when("once the parts come", "2027-03-02",
                                                                              all_day=True, approx=True),
            "accepted")], "updates": []}, direction="out")["items"][0]
        b = self.read("w2", "2027-03-01T09:05-08:00", {"items": [item(
            "commitment", "sender", "Dana sends the signed form", when("Tuesday", "2027-03-02", "10:00", "10:30"),
            "accepted")], "updates": []})["items"][0]
        self.now[0] = ts("2027-03-04T09:00-08:00")                         # both times have passed
        v = self.lv.view()
        self.assertIn(a, [r["id"] for r in v["needs_you"]])               # his promise: Needs you
        self.assertNotIn(a, [r["id"] for r in v["waiting"]])
        w = {r["id"]: r for r in v["waiting"]}
        self.assertEqual(w[b]["waiting_why"], "they owe you this")         # hers to him: Waiting on others
        self.assertNotIn(b, [r["id"] for r in v["needs_you"]])

    def test_a_real_bill_is_never_a_promotion_on_history_alone(self):
        class Rep:
            rule = staticmethod(lambda a: "")
            bulk = staticmethod(lambda a: "a list sender you never answer")
        known = tidy.Known(rep=Rep())
        row = {"source": "gmail", "direction": "in", "sender": "alerts@servicing.cardbank.example",
               "subject": "Card account update", "labels": [], "recipients": [],
               "text": "Your minimum payment of $35.00 was due Mar 1 and is now past due. Unsubscribe from alerts."}
        bill = {"type": "event", "subtype": "bill", "what": "Card payment past due", "amount": "$35.00"}
        self.assertIsNone(tidy.judge_mail(bill, row, known))
        self.assertEqual(tidy.judge_mail(dict(bill, subtype="decision"), row, known)[0], "promo")
        promo = dict(row, subject="Save $50 now on your next card")         # a real promo cue still counts
        self.assertEqual(tidy.judge_mail(bill, promo, known)[0], "promo")

    def test_list_mail_that_books_nothing_is_no_clash_side(self):
        row = {"source": "gmail", "direction": "in", "sender": "news@mail.brokerco.example", "subject": "Spot a scam?",
               "labels": [], "recipients": [], "text": "Join our webinar Monday 9 am. Unsubscribe."}
        self.assertTrue(today.Today._list_not_booking(row, None))
        self.assertFalse(today.Today._list_not_booking(dict(row, subject="Your appointment is confirmed"), None))
        self.assertFalse(today.Today._list_not_booking(dict(row, sender="pat@brokerco.example",
                                                            text="See you Monday at 9."), None))   # no list signal


if __name__ == "__main__":
    unittest.main()
