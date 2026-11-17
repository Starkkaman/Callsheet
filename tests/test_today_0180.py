"""0.18.0 (rework stage 4): the new Today from the ledger (today.py, api/today*). A seeded ledger (today_fixtures)
through the real extractor with a scripted fake model; no model server, no network."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "callsheet" / "app"), str(ROOT / "tests")]

import api  # noqa: E402
import extract  # noqa: E402
import msgstore  # noqa: E402
import today  # noqa: E402
import today_fixtures as F  # noqa: E402


def text(segs):
    return "".join(t for t, _ in segs)


class Base(unittest.TestCase):
    stage = "1220"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [0.0]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.ex = extract.Extractor(self.ms, F.TZ, F.OWNER, None, clock=lambda: self.now[0])
        self.al = F.FakeAlerter()
        self.td = today.Today(self.ms, self.ex.lv, lambda: self.now[0], alerter=self.al)
        self.ids = F.seed(self.ms, self.ex, self.now, self.td, self.stage)
        self.v = self.td.view()

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()


class TestMorning(Base):
    stage = "0840"

    def test_clashes_soonest_first_with_urgency(self):
        cs = self.v["now_next"]["clashes"]
        self.assertEqual([(c["level"], c["until"], c["compact"]) for c in cs],
                         [("red", "in 9 h", False), ("amber", "in 34 h", False), ("amber", "in 3 days", True)])
        self.assertTrue(cs[0]["lead"])
        self.assertFalse(any(c["lead"] for c in cs[1:]))                  # one filled key: the red one
        self.assertEqual(cs[0]["when"], "Today 6:00 pm")
        self.assertIn("Cascade grade review", text(cs[0]["rows"][0]["text"]))
        self.assertIn("cover Ana's class", text(cs[0]["rows"][1]["text"]))  # what each side is FOR
        self.assertEqual(cs[1]["reply"]["label"], "Reply to Marco")         # the oldest open ask
        self.assertTrue(cs[1]["reply"]["href"].startswith("https://mail.google.com/"))
        self.assertIn("Sam", text(cs[2]["line"]))

    def test_needs_you_oldest_first_with_sources_and_promises(self):
        rows = self.v["needs_you"]["rows"]
        self.assertEqual([r["who"] for r in rows], ["Priya Shah", "Kestrel Rentals", "Lena Ortiz", "Sam Okafor",
                                                    "Theo Park", "Northlight Studios"])
        self.assertEqual([r["icon"] for r in rows], ["mail", "mail", "slack", "text", "signal", "mail"])
        self.assertTrue(rows[1]["mine"])                                    # "You said"
        self.assertEqual(rows[0]["age"], "4 days")
        self.assertNotIn("Marco Bellini", [r["who"] for r in rows])         # a clash side shows in the clash

    def test_coming_up_from_calendar_and_every_message_source(self):
        cu = self.v["coming_up"]
        rows = [r for g in cu["groups"] for r in g["rows"]]
        self.assertEqual([g["label"] for g in cu["groups"]], ["This week", "Next week", "Later"])
        self.assertEqual({r["from"] for r in rows}, {"From text", "From email", "From Slack", "From Signal", ""})
        self.assertEqual(rows[0]["rel"], "in 3 days")
        for r in rows:
            self.assertEqual(r["add"], None if r["on_calendar"] else {"on": False, "why": "needs calendar access"})
        self.assertEqual(len(cu["marks"]), 30)
        self.assertEqual(cu["marks"][0], "td")
        self.assertIn("Dentist · 3:30 pm", [r["what"] for r in rows])       # 12-hour times
        self.assertFalse(self.v["calendar_write"])

    def test_clean_up_count_samples_and_timeline(self):
        c = self.v["clean_up"]
        self.assertEqual((c["total"], len(c["samples"]), c["since"]), (12, 3, "From the last 2 days"))
        tl = self.v["now_next"]["timeline"]
        self.assertEqual(tl[0]["meta"], "On now · until 9:30 am")
        self.assertIn("video", tl[1]["meta"])

    def test_item_view_marks_the_ask_and_says_no_reply_yet(self):
        v = self.td.item_view(self.ids["marco"])
        self.assertEqual(v["stand"][0]["badge"], "No reply yet")
        th = v["threads"][0]
        ask = [m for m in th["messages"] if m["mark"] == "ask"]
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0]["text"][ask[0]["hl"][0]:ask[0]["hl"][1]], "Thursday around 7")
        self.assertEqual(th["end"], "No reply from you yet")
        self.assertEqual(v["when"]["label"], "About Thu Nov 12, 7:00 pm to 8:00 pm")
        self.assertIn("Draft a reply to Marco", v["chat"])

    def test_note_junk_files_and_teaches_remind_tomorrow_hides(self):
        n = self.ids["c-n2"]
        r = self.td.note({"items": [n], "chips": ["junk"], "text": "promos"})
        self.assertEqual(r["filed"], [n])
        self.td.note({"items": [self.ids["theo"]], "chips": ["remind_tomorrow"]})
        v = self.td.view()
        self.assertNotIn(n, v["clean_up"]["ids"])
        self.assertNotIn("Theo Park", [x["who"] for x in v["needs_you"]["rows"]])
        notes = self.td.notes()
        self.assertEqual(notes[1]["said"], "Junk mail; promos")
        self.assertTrue(any("flexible" in x["said"] for x in notes))       # the seeded free-text note
        with self.assertRaises(today.Invalid):
            self.td.note({"items": [n], "chips": ["wipe"]})

    def test_done_and_undo(self):
        p = self.ids["priya"]
        self.assertEqual(self.td.done([p])["done"], [p])
        self.assertNotIn(p, [r["id"] for r in self.td.view()["needs_you"]["rows"]])
        self.assertEqual(self.td.undo([p])["reopened"], [p])
        self.assertIn(p, [r["id"] for r in self.td.view()["needs_you"]["rows"]])


class TestFollowUp(Base):
    stage = "0905"

    def test_red_clash_cleared_and_follow_up_state(self):
        nn = self.v["now_next"]
        self.assertEqual(nn["cleared"], [{"at": "8:55 am", "why": "you wrote to Ana"}])
        c = nn["clashes"][0]
        self.assertEqual(text(c["rows"][0]["text"]), "You told Dana yes: Dana's gallery set, Thu 7:00 pm")
        self.assertTrue(text(c["rows"][1]["text"]).startswith("Marco hasn’t heard from you"))
        self.assertNotIn("remind_at", c["followup"])                       # set at 9:10, after this moment
        self.assertTrue(c["lead"])

    def test_clash_view_shows_both_threads_with_the_ask_and_the_yes(self):
        cid = self.v["now_next"]["clashes"][0]["id"]
        v = self.td.clash_view(cid)
        self.assertEqual([s["badge"] for s in v["stand"]], ["You said yes", "No reply yet"])
        marks = {t["first"]: [m["mark"] for m in t["messages"] if m["mark"]] for t in v["threads"]}
        self.assertEqual(marks, {"Dana": ["ask", "yes"], "Marco": ["ask"]})
        sc = self.td.chat_scope("clash", cid)
        self.assertTrue(sc["project"].startswith("ledger:clash:") and sc["ids"])
        self.assertTrue(all(i >= msgstore.STORE_ID_BASE for i in sc["ids"]))
        got = self.ms.search("lights", ids=sc["ids"])
        self.assertTrue(got and all(r["id"] in sc["ids"] for r in got))
        self.assertEqual(self.ms.search("Lumen", ids=sc["ids"]), [])        # outside the scope

    def test_dismiss(self):
        cid = self.v["now_next"]["clashes"][0]["id"]
        self.td.dismiss(cid)
        self.assertNotIn(cid, [c["id"] for c in self.td.view()["now_next"]["clashes"]])

    def test_replies_has_every_open_ask(self):
        r = self.td.replies()
        self.assertEqual(r["total"], 9)
        self.assertEqual(max(x["bar"] for x in r["rows"]), 1.0)


class TestReminder(Base):
    stage = "1205"

    def test_reminder_went_out_with_names_sources_and_times_only(self):
        fu = self.v["now_next"]["clashes"][0]["followup"]
        self.assertEqual((fu["reminded"], fu["note"]), ("12:00 pm", "Marco still doesn’t know you’re busy then."))
        self.assertEqual(len(self.al.sent), 1)
        sent = json.dumps(self.al.sent)
        self.assertIn("Marco", sent)
        for words in ("rig the lights", "Aster", "Pizza", "client"):
            self.assertNotIn(words, sent)                                   # never message text


class TestCleared(Base):
    stage = "1220"

    def test_follow_up_cleared_by_his_mail(self):
        nn = self.v["now_next"]
        self.assertEqual(nn["cleared"], [{"at": "12:14 pm", "why": "you wrote to Marco"}])
        self.assertEqual([c["when"] for c in nn["clashes"]], ["Sat 10:00 am"])
        self.assertEqual(self.td.tick(), [])


class TestApi(unittest.TestCase):
    def test_routes_and_chat_scope(self):
        with tempfile.TemporaryDirectory() as d:
            now = [F.ts(F.STAGES["0905"])]
            app = api.App(d, clock=lambda: now[0])
            app.store.update_settings({"timezone": F.TZ})
            ids = F.seed(app.messages, app.extractor, now, app.today, "0905")
            v = app.handle("GET", "today", None)
            self.assertEqual(len(v["now_next"]["clashes"]), 2)
            self.assertEqual(app.handle("GET", "ledger", None)["counts"]["conflicts"], 2)
            self.assertEqual(app.handle("GET", f"today/items/{ids['priya']}", None)["kind"], "item")
            with self.assertRaises(api.NotFound):
                app.handle("GET", "today/items/99999", None)
            with self.assertRaises(api.InputError):
                app.handle("POST", "today/done", {"items": ["x"]})
            self.assertEqual(app.handle("POST", "today/remind", {"item": ids["priya"], "at": "tomorrow"})["at"],
                             "tomorrow 9:00 am")
            self.assertEqual(app.handle("GET", "today/cleanup", None)["total"], 12)
            self.assertIn(app.today.tick, app.ledger_runner.hooks)
            with self.assertRaises(api.InputError):
                app.handle("POST", "chat/ask", {"text": "hi", "item_id": "5"})


if __name__ == "__main__":
    unittest.main()
