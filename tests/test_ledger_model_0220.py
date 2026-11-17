"""0.22.x: what the local model got wrong on the real Signal set, applied in code (fictional chat fixtures, no real people or
messages):
- a group-chat message not addressed to the owner (no @mention, name or quote of the owner's message, not right after the owner's own
  message) makes no new items but never-hidden ones and events; the rest is left out with the
  reason (the read's drops) and named on one Clean up line per group chat; its updates still apply;
- the owner's own chat message has nothing to clean up;
- one answer that says the same thing twice keeps it once (the twin's update moves to the kept item);
- the owner's own chat message's open ask is the owner's question, whoever the model names: it waits on them;
- Signal capture labels a message that @mentions the owner (mentions_me) or quotes the owner's message (quotes_me).
In-process fakes only: no model server, no network."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, upd, when  # noqa: E402

import extract  # noqa: E402
import live  # noqa: E402

AT = "2027-03-01T10:00-08:00"


class Chat(Base):
    def chat(self, mid, answer, text, at=AT, sender="p:sam", name="Sam", labels=("group",), direction="in",
             thread="g:crew", channel="Climbing crew"):
        self.fake.answers[mid] = answer
        self.now[0] = ts(at)
        out = direction == "out"
        self.ms.upsert_message({"source": "signal", "account": "acct", "msg_id": mid, "thread": thread,
                                "direction": direction, "sender": "me" if out else sender,
                                "sender_name": "" if out else name,
                                "recipients": [{"name": channel, "address": "group:" + thread}], "ts": ts(at),
                                "subject": "", "text": text, "labels": list(labels), "channel": channel})
        return self.ex.read(self.ms.message("signal", "acct", mid))

    def drops(self, res):
        return [d for d in res["drops"] if d["what"] == "item"]

    def cleanups(self):
        return [i for i in self.lv.items() if i["type"] == "cleanup"]


class TestAddressed(unittest.TestCase):
    CTX = {"owner_names": ["jordan vale", "jordan"]}

    def row(self, text="", labels=("group",), at=1000.0, rid=9):
        return {"id": rid, "source": "signal", "direction": "in", "text": text, "labels": json.dumps(list(labels)),
                "ts": at}

    def test_mention_quote_and_name(self):
        self.assertEqual(extract.addressed(self.row(labels=("group", "mentions_me")), self.CTX), "mentions you")
        self.assertEqual(extract.addressed(self.row(labels=("group", "quotes_me")), self.CTX),
                         "replies to your message")
        self.assertEqual(extract.addressed(self.row("Jordan, can you bring rope?"), self.CTX), "names you")
        self.assertEqual(extract.addressed(self.row("@Jordan rope?"), self.CTX), "names you")

    def test_not_addressed(self):
        self.assertEqual(extract.addressed(self.row("Who can bring rope?"), self.CTX), "")
        self.assertEqual(extract.addressed(self.row("Jordanna can bring rope"), self.CTX), "")   # a word, not a part

    def test_right_after_his_own_message(self):
        mine = {"id": 1, "direction": "out", "ts": 1000.0 - 600}
        theirs = {"id": 2, "direction": "in", "ts": 1000.0 - 300}
        self.assertEqual(extract.addressed(self.row("Sure, Sat?"), self.CTX, [mine]), "answers your message")
        self.assertEqual(extract.addressed(self.row("Sure, Sat?"), self.CTX, [mine, theirs]), "")   # another came between
        old = dict(mine, ts=1000.0 - extract.REPLY_S - 1)
        self.assertEqual(extract.addressed(self.row("Sure, Sat?"), self.CTX, [old]), "")


class TestGroupNotAddressed(Chat):
    def test_its_asks_and_commitments_are_left_out_and_named_on_the_chats_clean_up_line(self):
        res = self.chat("g1", {"items": [
            item("ask", what="bring the rope Saturday", w=when("Saturday", "2027-03-06", all_day=True)),
            item("commitment", "sender", "drive the van"),
            item("event", what="gym shift", w=when("Sat 9am", "2027-03-06", "09:00"), subtype="job"),
            item("event", what="crag day", w=when("Sun", "2027-03-07", all_day=True))],
            "updates": []}, "Who can bring the rope Saturday? I'll drive the van. Gym shift Sat 9am, crag day Sun")
        kinds = sorted((i["type"], i["subtype"]) for i in self.lv.items())
        self.assertEqual(kinds, [("cleanup", ""), ("event", ""), ("event", "job")])     # events stay, any subtype
        self.assertEqual([d["value"] for d in self.drops(res)], ["ask: bring the rope Saturday",
                                                                 "commitment: drive the van"])
        self.assertTrue(all(d["why"] == extract.GROUP_WHY for d in self.drops(res)))
        line = self.cleanups()[0]
        self.assertEqual(line["what"], "Climbing crew: not addressed to you")
        named = [c for c in self.show(line["id"])["checks"] if c.get("field") == "items"]
        self.assertEqual(named[0]["value"], "ask, commitment")
        self.assertIn('ask "bring the rope Saturday"', named[0]["why"])
        self.assertEqual(self.lv.view()["needs_you"], [])

    def test_one_clean_up_line_per_chat(self):
        for n, at in enumerate(("2027-03-01T10:00-08:00", "2027-03-01T13:00-08:00")):
            self.chat(f"g{n}", {"items": [item("ask", what=f"vote on the crag {n}")], "updates": []},
                      "Vote on the crag?", at=at)
        self.chat("h1", {"items": [item("ask", what="vote on the cake")], "updates": []}, "Vote on the cake?",
                  thread="g:family", channel="Family")
        self.assertEqual(sorted(i["what"] for i in self.cleanups()),
                         ["Climbing crew: not addressed to you", "Family: not addressed to you"])

    def test_its_updates_still_apply(self):
        self.chat("g1", {"items": [item("ask", what="bring the chalk")], "updates": []}, "Jordan, bring the chalk?")
        ask = next(i for i in self.lv.items() if i["type"] == "ask")
        self.chat("g2", {"items": [], "updates": [upd(f"L{ask['id']}", "cancelled")]}, "Never mind the chalk",
                  at="2027-03-01T13:00-08:00")
        self.assertEqual(self.show(ask["id"])["status"], "cancelled")

    def test_an_addressed_group_message_or_a_dm_keeps_its_asks(self):
        self.chat("g1", {"items": [item("ask", what="bring the rope")], "updates": []}, "@Jordan bring the rope?",
                  labels=("group", "mentions_me"))
        self.chat("g2", {"items": [item("ask", what="lend the stove")], "updates": []}, "Can you lend the stove?",
                  labels=("group", "quotes_me"), at="2027-03-01T13:00-08:00")
        self.chat("d1", {"items": [item("ask", what="check the tent poles")], "updates": []}, "Check the tent poles?",
                  labels=("dm",), sender="p:ana", name="Ana", thread="d:ana", channel="Signal with Ana",
                  at="2027-03-01T14:00-08:00")
        self.assertEqual(sorted(i["what"] for i in self.lv.items()), ["bring the rope", "check the tent poles",
                                                                    "lend the stove"])

    def test_right_after_his_own_message_it_answers_him(self):
        self.chat("o1", {"items": [], "updates": []}, "Anyone up for Saturday?", direction="out")
        self.chat("g1", {"items": [item("ask", what="pick a crag for Saturday")], "updates": []},
                  "Yes! Pick the crag?", at="2027-03-01T10:05-08:00")
        self.assertEqual([i["type"] for i in self.lv.items()], ["ask"])


class TestHisOwnChatMessage(Chat):
    def test_nothing_in_it_to_clean_up(self):
        res = self.chat("o1", {"items": [item("cleanup", what="group banter"),
                                         item("commitment", "owner", "bring snacks")], "updates": []},
                        "Ha! I'll bring snacks", direction="out")
        self.assertEqual([i["type"] for i in self.lv.items()], ["commitment"])
        self.assertEqual([d["why"] for d in self.drops(res)], [extract.OWN_CLEANUP_WHY])

    def test_his_own_ask_waits_on_them_whoever_the_model_names(self):
        self.chat("o1", {"items": [item("ask", "Sam", "send the topo")], "updates": []}, "Can you send the topo?",
                  direction="out", labels=("dm",), thread="d:sam", channel="Signal with Sam")
        it = self.lv.items()[0]
        self.assertEqual((it["type"], it["status"]), ("ask", "waiting_on_them"))


class TestTwins(Chat):
    def test_the_same_thing_twice_in_one_answer_is_kept_once(self):
        res = self.chat("d1", {"items": [item("ask", what="send the gear list"),
                                         item("ask", what="send the gear list please")],
                               "updates": [{**upd("N2"), "note": "by Friday"}]},
                        "Send the gear list? Please send the gear list", labels=("dm",), thread="d:sam")
        self.assertEqual([i["what"] for i in self.lv.items()], ["send the gear list"])
        self.assertEqual([d["why"] for d in self.drops(res)], ["the same as item 1 of this answer"])
        self.assertFalse([d for d in res["drops"] if d["what"] == "update"])     # moved to the kept item

    def test_two_times_are_two_things(self):
        self.chat("d1", {"items": [item("ask", what="call Mon", w=when("Mon 9am", "2027-03-08", "09:00")),
                                   item("ask", what="call Mon", w=when("Mon 5pm", "2027-03-08", "17:00"))],
                         "updates": []}, "Call Mon 9am? Or 5pm", labels=("dm",), thread="d:sam")
        self.assertEqual(len(self.lv.items()), 2)


class TestSignalLabels(unittest.TestCase):
    ME = {"aci": "uuid-me", "number": "+15550000000", "name": "Jordan"}

    def env(self, **dm):
        return {"sourceNumber": "+15550001111", "sourceUuid": "uuid-sam", "sourceName": "Sam",
                "dataMessage": dict({"timestamp": 1700000000000, "message": "Rope?",
                                     "groupInfo": {"groupId": "grp1", "groupName": "Climbing crew"}}, **dm)}

    def labels(self, env):
        return live.signal_action(env, self.ME)[1]["labels"]

    def test_mention_and_quote_of_him(self):
        self.assertEqual(self.labels(self.env()), ["group"])
        self.assertEqual(self.labels(self.env(message="￼ rope?", mentions=[
            {"uuid": "uuid-me", "start": 0, "length": 1}])), ["group", "mentions_me"])
        self.assertEqual(self.labels(self.env(mentions=[{"uuid": "uuid-ana", "name": "Ana", "start": 0,
                                                         "length": 1}])), ["group"])
        self.assertEqual(self.labels(self.env(quote={"authorUuid": "uuid-me", "text": "Saturday?"})),
                         ["group", "quotes_me"])
        self.assertEqual(self.labels(self.env(quote={"authorUuid": "uuid-ana"})), ["group"])

    def test_his_own_sent_message_has_neither(self):
        env = {"sourceNumber": "+15550000000", "sourceUuid": "uuid-me", "syncMessage": {"sentMessage": {
            "timestamp": 1700000000000, "message": "Rope?", "quote": {"authorUuid": "uuid-me"},
            "groupInfo": {"groupId": "grp1", "groupName": "Climbing crew"}}}}
        self.assertEqual(self.labels(env), ["group"])


if __name__ == "__main__":
    unittest.main()
