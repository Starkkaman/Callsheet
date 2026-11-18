"""0.24.0 (B2) the secret shopper's Find, done reports, the leftover promotions and Waiting on others.

Find returns people, open items and messages (api/find), each opening its own view; a message from the owner that
reports a thing finished ("sent the invoice") closes the promise or ask it finishes in that thread, with the reason
and Undo, instead of opening a new promise; a confirmed People address that sends list mail is no longer protected,
and a "rate us" survey from machine mail is Clean up; Waiting on others drops automated senders, says a thing once and
moves what has waited 30 quiet days to Older. Fictional people and mail only; no model, no network."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, when  # noqa: E402

import api  # noqa: E402
import ledger as L  # noqa: E402
import msgstore  # noqa: E402
import people as PP  # noqa: E402
import tidy  # noqa: E402
import today  # noqa: E402
from store import Store  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"


class TestFindItems(Base):
    def test_items_by_person_and_sender_today_first_said_once(self):
        kw = dict(source="gmail", sender="ana@portolagarden.example", name="Portola Valley Garden", thread="G1",
                  subject="Plot renewal")
        a = self.read("f1", "2027-03-01T09:00-08:00", {"items": [item("ask", "Portola Valley Garden",
                                                                      "renew the garden plot")], "updates": []},
                      **kw)["items"][0]
        self.read("f2", "2027-03-01T09:05-08:00", {"items": [item("ask", "Portola Valley Garden",
                                                                  "renew the garden plot")], "updates": []},
                  **dict(kw, thread="G2"))
        w = self.read("f3", "2027-03-01T10:00-08:00", {"items": [item("ask", "owner", "the plot map",
                                                                      status="waiting_on_them")], "updates": []},
                      **dict(kw, thread="G3"))["items"][0]
        gone = self.read("f4", "2027-03-01T11:00-08:00", {"items": [item("ask", "Portola Valley Garden",
                                                                         "water the beds")],
                                                          "updates": []}, **dict(kw, thread="G4"))["items"][0]
        self.read("f5", "2027-03-01T12:00-08:00", {"items": [item("ask", "Kai", "lunch")], "updates": []},
                  source="gmail", sender="kai@friends.example", name="Kai Moss", thread="K1")
        td = today.Today(self.ms, self.lv, lambda: self.now[0])
        td.done([gone])
        got = td.find("portola")
        ids = [r["id"] for r in got]
        self.assertEqual(ids[0], a)                                  # Needs you first
        self.assertIn(w, ids)                                        # found by the sender of its mail
        self.assertNotIn(gone, ids)                                  # closed items aren't offered
        self.assertEqual(len([r for r in got if "renew the garden plot" in r["label"]]), 1)   # said once
        self.assertTrue(got[0]["sub"].startswith("Needs you"))
        self.assertTrue(next(r for r in got if r["id"] == w)["sub"].startswith("Waiting on others"))
        self.assertEqual(td.find("portola plot")[0]["id"], a)       # every word counts
        self.assertEqual(td.find("x"), [])                           # one letter finds nothing
        self.assertEqual(td.find("schuyler"), [])


class TestFindPeople(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.pp = PP.People(self.store, None, clock=lambda: 1.8e9)
        db = self.store.db
        rows = [("p00000000001", "Schuyler Van Loon", "confirmed", 0, "schuyler@work.example", "Schuyler Van Loon"),
                ("p00000000002", "S. Van Loon", "suggested", 0, "svl@home.example", "Sky"),
                ("p00000000003", "Portola Valley Garden", "suggested", 0, "ana@portolagarden.example",
                 "Portola Valley Garden"),
                ("p00000000004", "Old Merge", "merged", 0, "old@x.example", "Schuyler")]
        with self.store.lock:
            for pid, name, state, vip, val, lab in rows:
                db.execute("INSERT INTO people (id, name, aliases, state, vip, area, merged_into, created, updated) "
                           "VALUES (?, ?, '[]', ?, ?, '', '', 0, 0)", (pid, name, state, vip))
                db.execute("INSERT INTO person_handles (person, kind, value, label, verified, source, n, extra, "
                           "added) VALUES (?, 'email', ?, ?, 'inferred', 'mail', 3, '{}', 0)", (pid, val, lab))
            db.commit()

    def test_by_name_and_handle_never_merged_rows(self):
        got = self.pp.find("Schuyler")
        self.assertEqual([p["id"] for p in got], ["p00000000001"])   # the merged row isn't offered
        self.assertTrue(got[0]["confirmed"])
        self.assertEqual([p["name"] for p in self.pp.find("portola")], ["Portola Valley Garden"])
        self.assertEqual({p["id"] for p in self.pp.find("van loon")}, {"p00000000001", "p00000000002"})
        self.assertEqual(self.pp.find("s"), [])


class _Ix:
    def __init__(self, rows):
        self.rows = rows

    def search(self, q, **kw):
        return self.rows


class TestFindApi(Base):
    def test_people_items_and_messages_each_open_their_view(self):
        a = self.read("g1", "2027-03-01T09:00-08:00", {"items": [item("ask", "Portola Valley Garden",
                                                                      "renew the garden plot")], "updates": []},
                      source="gmail", sender="ana@portolagarden.example", name="Portola Valley Garden", thread="G1",
                      subject="Plot renewal")["items"][0]
        self.msg("g2", "2027-03-01T10:00-08:00", "News from the garden", source="gmail",
                 sender="ana@portolagarden.example", name="Portola Valley Garden", thread="G9", subject="Newsletter")
        mids = {r["msg_id"]: r["id"] for r in self.ms._read("SELECT id, msg_id FROM messages")}  # noqa: SLF001
        base = msgstore.STORE_ID_BASE
        rows = [{"id": base + mids["g1"], "title": "Plot renewal", "people": "Portola Valley Garden "
                 "<ana@portolagarden.example>", "date": "2027-03-01T17:00:00Z", "source": "gmail"},
                {"id": base + mids["g1"], "title": "Plot renewal", "people": "Portola Valley Garden "
                 "<ana@portolagarden.example>", "date": "2027-03-01T17:00:00Z", "source": "gmail"},
                {"id": base + mids["g2"], "title": "Newsletter", "people": "Portola Valley Garden "
                 "<ana@portolagarden.example>", "date": "2027-03-01T18:00:00Z", "source": "gmail"},
                {"id": base + mids["g2"] + 5, "title": "Other", "people": "", "date": "", "source": "gmail",
                 "relaxed": True}]

        class FakePeople:
            def find(self, q):
                return [{"id": "p00000000003", "name": "Portola Valley Garden", "vip": False, "confirmed": False}]

        app = type("FakeApp", (), {})()
        app.people, app.today, app.index, app.messages = FakePeople(), today.Today(
            self.ms, self.lv, lambda: self.now[0]), _Ix(rows), self.ms
        out = api.App.find(app, {"q": "  Portola  "})
        self.assertEqual(out["q"], "Portola")
        self.assertEqual(out["people"][0]["name"], "Portola Valley Garden")
        self.assertEqual(out["items"][0]["id"], a)
        self.assertEqual([m["title"] for m in out["messages"]], ["Plot renewal", "Newsletter"])  # once; no relaxed
        self.assertEqual(out["messages"][0]["item"], a)              # a message opens the item it made
        self.assertIsNone(out["messages"][1]["item"])                # else the message itself
        self.assertEqual(out["messages"][0]["who"], "Portola Valley Garden")
        self.assertEqual(api.App.find(app, {"q": "P"}), {"q": "P", "people": [], "items": [], "messages": []})

    def test_the_page_asks_api_find_and_puts_ask_last(self):
        js = (APP / "static" / "app.js").read_text()
        self.assertIn("api('GET', 'api/find?q=' + encodeURIComponent(k))", js)
        for g in ("'People'", "'On your list'", "'Messages'"):
            self.assertIn("item(" + g, js)
        body = js[js.index("function faItems(q)"):js.index("function faAsk(text)")]
        self.assertGreater(body.index("item('Ask'"), body.index("item('Messages'"))
        self.assertIn('("GET", r"find", self.find)', (APP / "api.py").read_text())


class TestDoneReports(Base):
    def ask_and_report(self, report, ask_what="send the signed lease", thread="L1", out_thread=None, w=None):
        a = self.read("d1", "2027-03-01T09:00-08:00", {"items": [item("ask", "Dana", ask_what)], "updates": []},
                      source="gmail", sender="dana@friends.example", name="Dana", thread=thread)["items"][0]
        res = self.read("d2", "2027-03-01T11:00-08:00", {"items": [item("commitment", "owner", report, w=w)],
                                                         "updates": []},
                        source="gmail", direction="out", thread=out_thread or thread,
                        to=[("dana@friends.example", "Dana")])
        return a, res

    def test_sent_closes_the_ask_in_that_thread_with_a_reason_and_undo(self):
        a, res = self.ask_and_report("sent the signed lease")
        self.assertEqual(res["created"], [])                          # no new open promise
        self.assertIn(a, res["closed"])
        it = self.lv.item(a)
        self.assertEqual(it["status"], "done")
        h = [x for x in it["history"] if x["kind"] == "done_report"]
        self.assertEqual(len(h), 1)
        self.assertIn("sent the signed lease", h[0]["note"])
        self.assertTrue(any("closed item" in d["why"] for d in res["drops"]))
        td = today.Today(self.ms, self.lv, lambda: self.now[0])
        rep = td.view()["now_next"]["reported"]
        self.assertEqual([r["id"] for r in rep], [a])
        self.assertTrue(rep[0]["why"].startswith("You wrote that it's done"))
        self.assertTrue(td.item_view(a)["closed_why"].startswith("You wrote that it's done"))
        self.assertEqual(td.undo([a])["reopened"], [a])               # Undo puts it back
        self.assertEqual(self.lv.item(a, detail=False)["status"], "open")
        self.assertEqual(td.view()["now_next"]["reported"], [])

    def test_done_closes_the_one_promise_of_the_thread(self):
        p = self.read("d0", "2027-03-01T08:00-08:00", {"items": [item("commitment", "owner", "fix the gate latch",
                                                                      status="accepted")], "updates": []},
                      source="gmail", direction="out", thread="F1", to=[("dana@friends.example", "Dana")])["items"][0]
        res = self.read("d3", "2027-03-02T08:00-08:00", {"items": [item("commitment", "owner", "all done")],
                                                         "updates": []},
                        source="gmail", direction="out", thread="F1", to=[("dana@friends.example", "Dana")])
        self.assertEqual(res["created"], [])
        self.assertEqual(self.lv.item(p, detail=False)["status"], "done")

    def test_not_a_report(self):
        for what in ("will send the signed lease", "send the signed lease by Friday", "sent the lease?",
                     "haven't sent the lease yet"):
            self.assertEqual(L.done_report({"type": "commitment", "status": "open", "what": what}), "", what)
        self.assertEqual(L.done_report({"type": "commitment", "status": "open", "what": "I've paid the deposit"}),
                         "pay")
        self.assertEqual(L.done_report({"type": "commitment", "status": "open", "what": "the deck is done"}), "do")
        at = ts("2027-03-01T11:00-08:00")
        plan = {"type": "commitment", "status": "accepted", "what": "booked dinner for Friday",
                "start_ts": at + 3 * 86400, "end_ts": at + 3 * 86400 + 7200}
        self.assertEqual(L.done_report(plan, at), "")                 # a plan still to come stays a plan

    def test_a_plan_with_a_time_is_still_made(self):
        a, res = self.ask_and_report("booked the table", ask_what="book a table",
                                     w=when("Fri 7 pm", "2027-03-05", "19:00", "21:00"))
        self.assertEqual(len(res["created"]), 1)
        self.assertEqual(self.lv.item(a, detail=False)["status"], "open")

    def test_another_thread_or_another_thing_closes_nothing(self):
        a, res = self.ask_and_report("sent the photos", ask_what="book the dentist")
        self.assertEqual(self.lv.item(a, detail=False)["status"], "open")
        self.assertEqual(res["created"], [])                          # still not an open promise
        self.assertTrue(any(d["why"] == "a report that it's done, not a promise" for d in res["drops"]))
        b, _res = self.ask_and_report("sent the signed lease", thread="L7", out_thread="L8")
        self.assertEqual(self.lv.item(b, detail=False)["status"], "open")


def mail(sender, subject="Your order", text="Hi there.", labels=(), bulk=0):
    return {"source": "gmail", "direction": "in", "sender": sender, "subject": subject, "text": text,
            "labels": json.dumps(list(labels)), "bulk": bulk, "account": "a", "thread": "t-" + subject}


class TestLeftovers(unittest.TestCase):
    def test_a_confirmed_list_address_is_a_list(self):
        k = tidy.Known(confirmed={"news@paws.shop.example", "kim@friends.example"})
        promo = mail("news@paws.shop.example", "Your pup will love these", labels=("CATEGORY_PROMOTIONS",), bulk=1)
        self.assertFalse(k.protects(promo))
        self.assertEqual(tidy.judge_mail({"type": "ask", "subtype": "decision", "what": "pick a treat"}, promo,
                                         k)[0], "promo")
        self.assertTrue(k.protects(mail("kim@friends.example", "Dinner?")))
        esp = mail("club@shared1.ccsend.com", "Spring events", text="Unsubscribe here.")
        self.assertFalse(tidy.Known(confirmed={"club@shared1.ccsend.com"}).protects(esp))
        wrote = tidy.Known(confirmed={"news@paws.shop.example"}, wrote_to={"news@paws.shop.example"})
        self.assertTrue(wrote.protects(promo))                          # he wrote to it: his call

    def test_a_survey_from_machine_mail_but_never_a_bill_or_alert(self):
        k = tidy.Known()
        s = mail("support@help.desk.example", "How did we do? Rate your support request")
        self.assertEqual(tidy.judge_mail({"type": "ask", "subtype": "reply", "what": "rate the support"}, s, k)[0],
                         "promo")
        bill = mail("billing@power.example", "Rate your experience: your bill is ready",
                    text="Amount due $84.10 by Mar 9.")
        self.assertIsNone(tidy.judge_mail({"type": "event", "subtype": "bill", "what": "pay the power bill",
                                           "amount": "$84.10"}, bill, k))
        alert = mail("no-reply@bank.example", "New sign-in: rate your experience")
        self.assertIsNone(tidy.judge_mail({"type": "ask", "subtype": "security", "what": "check the sign-in"},
                                          alert, k))
        person = mail("dana@friends.example", "How did we do at the fair?")
        self.assertIsNone(tidy.judge_mail({"type": "ask", "subtype": "reply", "what": "tell Dana"}, person, k))


class TestWaiting(Base):
    def owed(self, mid, at, sender, name, what, thread, labels=(), text=""):
        due = when("by the next day", at[:10], "17:00")                    # a time that has passed: waiting on them
        return self.read(mid, at, {"items": [item("commitment", name, what, w=due, status="accepted")],
                                   "updates": []},
                         source="gmail", sender=sender, name=name, thread=thread, labels=labels, text=text)["items"][0]

    def test_one_row_no_machines_and_quiet_ones_go_to_older(self):
        a = self.owed("w1", "2027-03-01T09:00-08:00", "sam@friends.example", "Sam", "send the quote", "S1")
        b = self.owed("w2", "2027-03-01T09:30-08:00", "sam@work.example", "Sam", "send the quote", "S9")
        m = self.owed("w3", "2027-03-01T10:00-08:00", "no-reply@refunds.shop.example", "Shop",
                      "refund the order", "R1")
        q = self.owed("w4", "2027-01-10T10:00-08:00", "lee@friends.example", "Lee", "return the ladder", "Q1")
        self.now[0] = ts("2027-03-02T09:00-08:00")
        td = today.Today(self.ms, self.lv, lambda: self.now[0])
        lv = self.lv.view(now=self.now[0])
        self.assertTrue({a, b, q} <= {it["id"] for it in lv["waiting"]})
        mine = [it for it in lv["waiting"] if it["id"] in {a, b, q}] + [self.lv.item(m, detail=False)]
        rows, older = td._waiting(mine, self.now[0])  # noqa: SLF001
        self.assertEqual([r["id"] for r in rows], [a])
        self.assertEqual((rows[0]["more"], rows[0]["ids"]), (1, [a, b]))   # one row for the conversation
        self.assertEqual([r["id"] for r in older], [q])                 # 30 quiet days: Older, still open?
        self.assertIn("30 days", older[0]["old_why"])
        self.assertNotIn(m, [r["id"] for r in rows + older])            # a no-reply sender owes him nothing
        v = td.view()["needs_you"]
        self.assertIn(q, v["older"]["ids"])
        self.assertNotIn(q, [r["id"] for r in v["waiting"]["rows"]])


if __name__ == "__main__":
    unittest.main()
