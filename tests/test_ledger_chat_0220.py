"""0.22.x: gaps the real Signal set found, on fictional chat fixtures (no real people or messages):
- the owner's own question in their own message is no ask to them: the owner waits for the answer (no time held, no clash).
- one plan in one chat: their other time for the owner's question is their ask (the owner's question still waits, holds no time,
  and the two don't clash); a shoot and an ask right next to it in one group chat are no tight clash (across chats
  they still are);
- a vague evening ("Fri night", "tonight") with no clock time holds the evening (18-21), a likely clash;
- the chat rule names the gaps only a prompt can fix (prompt text; a live run measures it).
In-process fakes only: no model server, no network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, when  # noqa: E402


def event(eid, title, start, end, **kw):
    return dict({"account": "a", "calendar": "c", "event_id": eid, "title": title, "start_ts": ts(start),
                 "end_ts": ts(end), "status": "confirmed"}, **kw)


class TestHisOwnQuestion(Base):
    def test_his_own_question_waits_on_them_and_holds_no_time(self):
        self.ms.upsert_events([event("e1", "Studio session", "2027-03-03T15:00-08:00", "2027-03-03T17:00-08:00")])
        self.read("o1", "2027-03-01T10:00-08:00", {"items": [item(
            "ask", "owner", "are they free for the edit", when("Wed 3pm", "2027-03-03", "15:00"), subtype="availability",
            conf="likely")], "updates": []}, direction="out", text="Free Wed 3pm for the edit?", thread="t-dana")
        it = next(iter(self.items().values()))
        self.assertEqual((it["type"], it["who"], it["status"]), ("ask", "owner", "waiting_on_them"))
        self.assertIn("your own question in your own message", [c["why"] for c in it["checks"]])
        self.assertEqual(self.lv.conflicts(), [])

    def test_an_ask_to_him_in_an_incoming_message_stays_open(self):
        self.read("i1", "2027-03-01T10:00-08:00", {"items": [item(
            "ask", "sender", "free Wed 3pm", when("Wed 3pm", "2027-03-03", "15:00"), subtype="availability")],
            "updates": []}, text="Free Wed 3pm?")
        self.assertEqual([i["status"] for i in self.items().values()], ["open"])


class TestOnePlanInOneChat(Base):
    def test_their_other_time_for_his_question_is_their_ask_his_question_still_waits(self):
        self.ms.upsert_events([event("e1", "Dentist", "2027-03-04T10:00-08:00", "2027-03-04T11:00-08:00")])
        self.read("o1", "2027-03-01T10:00-08:00", {"items": [item(
            "ask", "owner", "coffee Wed 3pm", when("Wed 3pm", "2027-03-03", "15:00"), conf="likely")],
            "updates": []}, direction="out", text="Coffee Wed 3pm?", thread="t-dana")
        mine = next(iter(self.items()))
        self.read("i1", "2027-03-01T10:30-08:00", {"items": [item(
            "ask", "sender", "coffee Thu 10am instead", when("Thu 10am", "2027-03-04", "10:00"), conf="likely")],
            "updates": [{"id": f"L{mine}", "status": "moved", "when": when("Thu 10am", "2027-03-04", "10:00"),
                         "note": "", "tell": "", "due": ""}]}, text="Can't Wed. Thu 10am?", thread="t-dana")
        it = self.items()[mine]
        self.assertEqual(it["status"], "waiting_on_them")       # the owner's question still waits; the time is kept
        self.assertEqual(it["start_ts"], ts("2027-03-04T10:00-08:00"))
        c = self.lv.conflicts()                                  # their ask clashes with the dentist, the owner's doesn't,
        self.assertEqual(len(c), 1)                              # and the owner's and theirs are one plan
        self.assertEqual(sorted((c[0]["a_kind"], c[0]["b_kind"])), ["event", "item"])
        self.assertNotIn(("item", mine), [(c[0]["a_kind"], c[0]["a_ref"]), (c[0]["b_kind"], c[0]["b_ref"])])

    def test_a_shoot_and_an_ask_next_to_it_in_one_group_chat_are_not_a_tight_clash(self):
        self.read("g1", "2027-03-01T10:00-08:00", {"items": [item(
            "event", "sender", "product launch", when("Fri 9-12", "2027-03-05", "09:00", "12:00"), subtype="gig")],
            "updates": []}, text="Shoot Fri 9-12 at the loft", thread="g-crew")
        self.read("g2", "2027-03-01T10:05-08:00", {"items": [item(
            "ask", "sender", "help strike the set", when("Fri 12:30", "2027-03-05", "12:30"), conf="likely")],
            "updates": []}, sender="p:sam", name="Sam", text="Who can help strike at 12:30?", thread="g-crew")
        self.assertEqual(self.lv.conflicts(), [])

    def test_a_tight_squeeze_across_chats_still_clashes(self):
        self.read("g1", "2027-03-01T10:00-08:00", {"items": [item(
            "event", "sender", "product launch", when("Fri 9-12", "2027-03-05", "09:00", "12:00"), subtype="gig")],
            "updates": []}, text="Shoot Fri 9-12 at the loft", thread="g-crew")
        self.read("d1", "2027-03-01T10:05-08:00", {"items": [item(
            "ask", "sender", "lunch Fri 12:30", when("Fri 12:30", "2027-03-05", "12:30"))],
            "updates": []}, sender="p:sam", name="Sam", text="Lunch Fri 12:30?", thread="t-sam")
        self.assertEqual([x["kind"] for x in self.lv.conflicts()], ["tight"])


class TestAVagueEvening(Base):
    def setUp(self):
        super().setUp()
        self.ms.upsert_events([event("e1", "Climbing gym", "2027-03-05T18:30-08:00", "2027-03-05T19:30-08:00")])

    def promise(self, phrase, start):
        self.read("o1", "2027-03-01T10:00-08:00", {"items": [item(
            "commitment", "owner", "help Dana move the couch", when(phrase, "2027-03-05", start), status="accepted")],
            "updates": []}, direction="out", text="Sure, I'll help Fri night", thread="t-dana")

    def test_friday_night_holds_the_evening_a_likely_clash(self):
        self.promise("Fri night", "20:00")
        self.assertEqual([(x["kind"], x["confidence"]) for x in self.lv.conflicts()], [("overlap", "likely")])

    def test_a_clock_time_holds_only_its_hour(self):
        self.promise("Fri night at 8pm", "20:00")
        self.assertEqual(self.lv.conflicts(), [])


class TestTheChatRuleNamesTheRealChatGaps(unittest.TestCase):
    """Prompt text only (the real Signal set's causes no code can fix): plans among others, a shared link, a group ask
    not addressed to the owner, the owner's yes as a commitment and the owner's own days away as an event. Whether the model follows it needs
    a live run."""

    def test_the_rule_names_each_gap(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))
        import extract
        rule = " ".join(extract.CHAT_RULE.format(owner="OWNER", src="Signal").split())
        none = rule[rule.index("No item for"):rule.index("Always an item")]
        always = rule[rule.index("Always an item"):rule.index("No item: ")]
        self.assertIn("plans among others that OWNER isn't part of", none)
        self.assertIn("a link, forward or promo shared in the chat (not a clean-up)", none)
        self.assertIn("A group ask to everyone, or with no @mention of OWNER and no reply to OWNER, is at most likely", none)
        self.assertIn('or their yes to an ask ("sure, I can") = a commitment with its time', always)
        self.assertIn("(that yes also marks the ask done)", always)
        self.assertIn("days away OWNER announces", always)
        self.assertIn("= an all-day event, not an ask", always)

    def test_the_rule_names_what_the_model_still_made_items_of(self):
        """A local model on a set of real chats, after the code filters: items from where someone is or what they did, the owner's
        maybes and what the owner already did, and asks already listed asked again. Prompt text only: a live run measures
        it."""
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))
        import extract
        rule = " ".join(extract.CHAT_RULE.format(owner="OWNER", src="Signal").split())
        either = rule[rule.index("No item either for"):rule.index("Always an item")]
        self.assertIn("where someone is or what they are doing or have done", either)
        self.assertIn('a maybe or a wish with no promise ("we should hang soon")', either)
        self.assertIn("something OWNER already did", either)
        self.assertIn("already in the open items listed (asked again or a reminder: update that item", either)
        nos = rule[rule.index("No item: "):rule.index("Item: ")]
        for ex in ('"omw, 10 min" (where they are)',
                   'OWNER: "dropped it at yours" (only the update: the listed ask done)',
                   'OWNER: "we should get lunch soon" (no promise)', '"any news on the quote?" (a listed ask asked again: '
                   'an update)'):
            self.assertIn(ex, nos)
        yes = rule[rule.index("Item: "):]
        self.assertIn('OWNER: "probably, I\'ll confirm tonight" (their commitment)', yes)   # the owner's maybe with a promise


if __name__ == "__main__":
    unittest.main()
