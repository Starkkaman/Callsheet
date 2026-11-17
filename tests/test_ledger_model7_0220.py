"""0.22.x, the local model round 7: cut answers kept up to the cut, the owner's own chat messages, quoted words, clashes that can't
be, the brief's repair try and the replay's exact raw re-score. Fictional fixtures only."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "callsheet", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, HERE)

import conflicts as C  # noqa: E402
import extract  # noqa: E402
import writer  # noqa: E402
from test_brief_0210 import Base, answer_for  # noqa: E402

ITEM = {"type": "commitment", "subtype": "", "who": "owner", "quote": "sure, Sat works", "what": "help Dana move",
        "for": "", "when": {"phrase": "Sat", "date": "2026-11-21", "end_date": "", "start": "", "end": "",
                            "all_day": True, "approx": False, "tz": "", "options": []},
        "status": "accepted", "says": "", "confidence": "sure", "never_hide": False, "amount_from": "", "stage": ""}


def answer(items, updates):
    return json.dumps({"items": items, "updates": updates}, indent=2)


class TestCutAnswers(unittest.TestCase):
    def test_whole_items_and_a_cut_update_with_its_id_and_status_are_kept(self):
        full = answer([ITEM, dict(ITEM, type="event")], [{"id": "L1", "status": "done", "when": ITEM["when"],
                                                          "note": "", "tell": "Dana", "due": "", "amount_from": "",
                                                          "stage": ""}])
        cut = full[:full.index('"when"', full.index('"updates"')) + 12]
        got = extract.parse(cut)
        self.assertEqual(len(got["items"]), 2)
        self.assertEqual(got["updates"], [{"id": "L1", "status": "done"}])
        self.assertEqual(got["cut"], extract.CUT_WHY)

    def test_a_cut_item_is_never_kept_and_nothing_whole_still_fails(self):
        full = answer([ITEM, ITEM], [])
        got = extract.parse(full[:full.index('"status"', full.index('"type"', full.index('"type"') + 1))])
        self.assertEqual(len(got["items"]), 1)
        with self.assertRaises(writer.ModelGarbage):
            extract.parse(full[:60])
        with self.assertRaises(writer.ModelGarbage):
            extract.parse("not json at all")

    def test_a_cut_move_needs_its_time(self):
        full = answer([ITEM], [{"id": "L2", "status": "moved", "when": ITEM["when"], "note": "", "tell": "",
                                "due": "", "amount_from": "", "stage": ""}])
        got = extract.parse(full[:full.index('"phrase"', full.index('"updates"'))])
        self.assertEqual((len(got["items"]), got["updates"]), (1, []))

    def test_room_for_two_items_and_an_update(self):
        self.assertGreaterEqual(extract.MAX_TOKENS, 1500)


def parsed_of(*its, updates=()):
    return {"items": [dict(it, checks=[]) for it in its], "updates": [dict(u) for u in updates], "drops": []}


class TestHisOwnChatMessage(unittest.TestCase):
    def test_no_ask_to_him_nor_what_he_already_did(self):
        p = parsed_of({"type": "ask", "status": "tentative", "what": "a", "start_ts": None, "start_date": ""},
                      {"type": "ask", "status": "waiting_on_them", "what": "b", "start_ts": None, "start_date": ""},
                      {"type": "event", "status": "done", "what": "c", "start_ts": None, "start_date": ""},
                      {"type": "commitment", "status": "accepted", "what": "d", "start_ts": 100, "start_date": ""},
                      {"type": "cleanup", "status": "open", "what": "e", "start_ts": None, "start_date": ""},
                      updates=[{"id": 3, "new": None, "status": "done"}])
        left = extract.own_answer(p, {"text": "x"})
        self.assertEqual([it["what"] for it in p["items"]], ["b", "d"])
        self.assertEqual([it["what"] for it in left], ["a", "c"])       # named on the chat's Clean up line
        self.assertEqual(len(p["updates"]), 1)                          # the owner's updates always apply
        self.assertEqual({d["why"] for d in p["drops"]}, {extract.OWN_CLEANUP_WHY, extract.OWN_WHY})

    def test_an_event_at_his_commitments_time_is_the_same_plan(self):
        p = parsed_of({"type": "commitment", "status": "accepted", "what": "dinner", "start_ts": 500, "start_date": ""},
                      {"type": "event", "status": "open", "what": "dinner at 7", "start_ts": 500, "start_date": ""},
                      {"type": "event", "status": "open", "what": "gig", "start_ts": 900, "start_date": ""},
                      updates=[{"id": None, "new": 1, "status": "accepted"}])
        extract.own_answer(p, {"text": "x"})
        self.assertEqual([it["what"] for it in p["items"]], ["dinner", "gig"])
        self.assertEqual(p["updates"][0]["new"], 0)                     # moved to the owner's commitment
        self.assertIn("the same plan as item 1", p["drops"][0]["why"])


class TestQuotes(unittest.TestCase):
    ctx = {"subject": "Re: Saturday", "text": "Sure — Sat works!\n\nSee you   at the  “usual” place"}

    def test_found_after_case_space_and_punctuation(self):
        for q in ("sure, sat works", "SEE YOU AT THE USUAL PLACE", "Re: Saturday", "sure... usual place"):
            self.assertTrue(extract.quote_found(q, self.ctx), q)
        for q in ("", "Sunday works", "see you at the cafe"):
            self.assertFalse(extract.quote_found(q, self.ctx), q)

    def test_an_item_whose_quote_isnt_there_is_left_out_with_why(self):
        p = parsed_of({"type": "event", "what": "a", "_quote": True}, {"type": "event", "what": "b", "_quote": False},
                      {"type": "event", "what": "c"})
        left = extract.quote_answer(p)
        self.assertEqual(([it["what"] for it in p["items"]], [it["what"] for it in left]), (["a", "c"], ["b"]))
        self.assertEqual(p["drops"][0]["why"], extract.QUOTE_WHY)
        self.assertFalse(any("_quote" in it for it in p["items"] + left))

    def test_the_schema_asks_for_it(self):
        self.assertIn("quote", extract.SCHEMA["properties"]["items"]["items"]["required"])
        self.assertIn("quote = the words", extract.SYSTEM)


class FakeDb:
    def __init__(self, source, direction):
        self.row = {"source": source, "direction": direction}

    def execute(self, sql, args=()):
        row = self.row

        class Cur:
            def fetchone(self):
                return row
        return Cur()


class TestClashSides(unittest.TestCase):
    def it(self, **k):
        base = {"id": 1, "type": "event", "subtype": "", "status": "open", "confidence": "sure", "all_day": 0,
                "start_ts": 100, "start_date": "", "candidates": [], "merged_into": None}
        base.update(k)
        return base

    def test_a_security_alert_holds_no_time(self):
        self.assertFalse(C.blocks(self.it(type="ask", subtype="security")))
        self.assertTrue(C.blocks(self.it(type="ask", subtype="invite")))

    def test_his_own_chat_message_clashes_only_with_a_commitment_or_days_away(self):
        own = FakeDb("signal", "out")
        self.assertFalse(C.chat_clashes(own, self.it(type="event")))
        self.assertFalse(C.chat_clashes(own, self.it(type="ask", status="tentative")))
        self.assertTrue(C.chat_clashes(own, self.it(type="commitment", status="accepted")))
        self.assertTrue(C.chat_clashes(own, self.it(type="event", all_day=1, start_date="2026-11-21")))
        self.assertTrue(C.chat_clashes(FakeDb("signal", "in"), self.it(type="event", confidence="likely")))
        self.assertTrue(C.chat_clashes(FakeDb("gmail", "out"), self.it(type="event")))


class TestBriefRepair(Base):
    def test_one_more_try_after_an_unusable_answer(self):
        good = answer_for("Two things need you today.")
        n = []

        def first_long(body):
            n.append(1)
            if len(n) == 1:
                return answer_for(" ".join(["word"] * 200))(body)
            return good(body)
        b = self.brief(first_long)
        out = b.write_step(b.plan()["steps"][0], 1, "m")
        self.assertEqual((out["by"], out["repaired"]), ("model", "too long"))
        self.assertEqual(len(self.calls), 2)
        said = self.calls[1]["messages"]
        self.assertEqual([m["role"] for m in said[-2:]], ["assistant", "user"])
        self.assertIn("can't be used (too long)", said[-1]["content"])

    def test_two_unusable_answers_are_the_template(self):
        b = self.brief({"choices": [{"message": {"content": "{oops"}}]})
        out = b.write_step(b.plan()["steps"][0], 1, "m")
        self.assertEqual((out["by"], out["why"], len(self.calls)), ("template", "not JSON", 2))


class TestExactRawRescore(unittest.TestCase):
    def test_ids_of_the_run_become_this_replays(self):
        import replay_ledger as R
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        raw = os.path.join(d.name, "raw.jsonl")
        ids = os.path.join(d.name, "ids.json")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(json.dumps({"mid": "c:m3", "content": '{"items": [], "updates": [{"id": "L7", "status": "done"},'
                                                          ' {"id": "L9", "status": "done"}]}'}) + "\n")
        with open(ids, "w", encoding="utf-8") as f:
            json.dump({"groups": {"c:m1": {"7": ["c:m2", "ask", "lend the ladder"], "9": ["c:m2", "event", "x"]}},
                       "group_of": {"c:m1": "c:m1", "c:m2": "c:m1", "c:m3": "c:m1"}}, f)
        call = R.raw_caller(raw, ids)
        call.bind_snap("c:m1", {"items": {4: {"id": 4, "created_mid": "c:m2", "type": "ask",
                                              "what": "lend the ladder"}}})
        got = json.loads(call({}, {"msg_id": "c:m3"}))
        self.assertEqual([u["id"] for u in got["updates"]], ["L4", "L0"])   # the event was left out here


if __name__ == "__main__":
    unittest.main()
