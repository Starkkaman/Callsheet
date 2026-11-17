"""Replay scorer 2.1 (the acceptance rules' conditions on scorer 2 and set v1.1): a `section` list is paired with `types`
by position; an ask the owner agreed to resolves (done, or merged into the item it was about) and one left open is a
false item; `auto_sender` must be the sender of one of the case's own automated messages (else a set error)."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tools"))

import replay_ledger as R  # noqa: E402

TZ = "America/Los_Angeles"
AMB = {"types": ["ask", "event"], "section": ["needs_you", "coming_up"]}


def it(typ, section, **kw):
    return dict({"id": 1, "type": typ, "subtype": "", "section": section, "merged_into": None}, **kw)


def case(messages, expect, **kw):
    c = {"case": "S-T-01", "set": "synthetic", "set_version": "1.1", "replay": "isolated", "patterns": [],
         "owner": "Jordan Vale", "owner_tz": TZ, "calendar": [], "messages": messages, "expect": expect,
         "updates": [], "conflicts": [], "follow_ups": [], "links": [], "no_items": False, "note": None,
         "unjudged": []}
    c.update(kw)
    return c


def msg(n, sender, text, direction="in", subject="t"):
    return {"mid": f"S-T-01:m{n}", "source": "mail", "direction": direction, "sender": sender,
            "to": ["owner"] if direction == "in" else ["p:lena"], "chat": "mail:t", "chat_kind": "thread",
            "ts": f"2027-03-0{n}T09:00-08:00", "text": text, "subject": subject}


class TestSectionPairedWithType(unittest.TestCase):
    def test_each_type_only_in_its_own_section(self):
        self.assertTrue(R.section_ok(AMB, it("ask", "needs_you"), 1, None))
        self.assertTrue(R.section_ok(AMB, it("event", "coming_up"), 1, None))
        self.assertFalse(R.section_ok(AMB, it("ask", "coming_up"), 1, None))     # an ask in the event's section
        self.assertFalse(R.section_ok(AMB, it("event", "needs_you"), 1, None))   # an event in the ask's section
        self.assertFalse(R.section_ok(AMB, it("commitment", "needs_you"), 1, None))
        self.assertTrue(R.section_ok({"type": "ask", "section": "needs_you"}, it("ask", "needs_you"), 1, None))

    def test_at_the_end_the_final_item_type_picks_the_section(self):
        e = dict(AMB, final_status="done")
        end = {"items": {1: it("ask", "clean_up", merged_into=2), 2: it("event", "coming_up", id=2)}}
        self.assertTrue(R.section_ok(e, it("ask", "clean_up"), 1, end))
        end["items"][2]["section"] = "needs_you"
        self.assertFalse(R.section_ok(e, it("ask", "clean_up"), 1, end))

    def test_set_error_when_sections_and_types_differ_in_length(self):
        c = case([msg(1, "p:lena", "hi")], [{"eid": "S-T-01:e1", "types": ["ask", "event"],
                                            "section": ["needs_you", "coming_up", "clean_up"], "who": "p:lena",
                                            "after": "S-T-01:m1"}])
        self.assertEqual(len(R.set_errors([c])), 1)


def full(iid, typ, who, mids, status, start, merged_into=None, hist=()):
    ts = R.ts_of(start)
    return {"id": iid, "type": typ, "subtype": "", "who": who, "who_name": who.split(":")[-1].title(),
            "what": "coffee with Lena", "purpose": "", "status": status,
            "section": "needs_you" if typ == "ask" else "coming_up", "start": start, "end": None, "start_ts": ts,
            "end_ts": None, "all_day": False, "start_date": None, "end_date": None, "time": None,
            "date_resolved": True, "candidates": [], "mids": list(mids), "created_mid": mids[0], "cal_ref": None,
            "parent": None, "predecessor": None, "merged_into": merged_into, "never_hide": False, "hist": list(hist)}


class TestMoveAskResolvesIntoTheItem(unittest.TestCase):
    """S-MOVE-01's shape: the owner's yes makes the commitment (e1); Lena asks to push to 11 (e3); "11 works" moves e1 and
    the ask resolves into it (done or merged), never a second open item."""
    T10, T11 = "2027-03-04T10:00-08:00", "2027-03-04T11:00-08:00"

    def case(self):
        w = {"tz": TZ, "phrase": "", "all_day": False, "approx": False}
        return case([msg(1, "p:lena", "Coffee Thursday 10am?"), msg(2, "owner", "Sounds good", "out"),
                     msg(3, "p:lena", "Can we push to 11?"), msg(4, "owner", "11 works", "out")],
                    [{"eid": "S-T-01:e1", "type": "commitment", "who": "owner", "what": "coffee with Lena",
                      "when": dict(w, start=self.T10, end=None), "status": "accepted", "section": "coming_up",
                      "after": "S-T-01:m2", "confidence": "sure"},
                     {"eid": "S-T-01:e3", "type": "ask", "who": "p:lena", "what": "push the coffee to 11",
                      "when": dict(w, start=self.T11, end=None), "status": "open", "section": "needs_you",
                      "after": "S-T-01:m3", "confidence": "sure", "final_status": "done"}],
                    updates=[{"eid": "S-T-01:e3", "after": "S-T-01:m4", "status": "done", "into": "S-T-01:e1"}])

    def score(self, ask_end):
        c = self.case()
        e1 = full(1, "commitment", "owner", ["S-T-01:m2"], "accepted", self.T10)
        ask = full(2, "ask", "p:lena", ["S-T-01:m3"], "open", self.T11)
        snaps = {"S-T-01:m2": {"items": {1: e1}, "conflicts": [], "followups": []},
                 "S-T-01:m3": {"items": {1: e1, 2: ask}, "conflicts": [], "followups": []}}
        end_items = {1: dict(e1, status="accepted", start=self.T11, start_ts=R.ts_of(self.T11),
                             mids=e1["mids"] + ["S-T-01:m4"]), 2: dict(ask, **ask_end)}
        end_items[3] = full(3, "event", "p:sam", ["S-T-01:m1"], "accepted", self.T10)
        snaps["S-T-01:m4"] = end = {"items": end_items, "conflicts": [], "followups": []}
        T = R.Tally()
        match, _ = R.score_case(c, snaps, {}, end, R.L.zone(TZ), T, True)
        upd = T.t["all"]["sure"]
        return (upd["updates.n"], upd["updates.ok"]), dict(T.false), match

    def test_done_or_merged_into_the_item_passes(self):
        for ask_end in ({"status": "done", "hist": [("status", "done", "S-T-01:m4")]},
                        {"merged_into": 1, "hist": [("merged", None, "S-T-01:m4")]}):
            upd, false, match = self.score(ask_end)
            self.assertEqual(upd, (2, 2), ask_end)
            self.assertEqual(false.get("false_items", 0), 0, ask_end)
            self.assertEqual(match["S-T-01:e3"], 2)

    def test_a_leftover_open_move_ask_fails_and_is_a_false_item(self):
        upd, false, _ = self.score({})
        self.assertEqual(upd, (2, 0))                          # the update at "11 works" and the final status
        self.assertEqual((false["false_items"], false["open_after_agreement"]), (1, 1))

    def test_merged_into_another_item_is_not_resolved(self):
        upd, false, _ = self.score({"merged_into": 3})
        self.assertEqual(upd, (2, 0))
        self.assertEqual(false.get("false_items", 0), 0)

    def test_into_must_be_an_asks_done_update_into_another_expectation(self):
        c = self.case()
        self.assertEqual(R.set_errors([c]), [])
        c["updates"][0]["into"] = "S-T-01:e9"
        self.assertEqual(len(R.set_errors([c])), 1)
        c["updates"][0].update(into="S-T-01:e1", status="moved")
        self.assertEqual(len(R.set_errors([c])), 1)


class TestAutoSenderFromTheCaseMessages(unittest.TestCase):
    def auto(self, **kw):
        return case([msg(1, "p:auto", "Your autopay of $70.00 is scheduled for March 12.",
                         subject="Linkwave Internet: autopay"), msg(2, "p:dana", "ok")], [], **kw)

    def test_the_automated_messages_sender_is_fine(self):
        self.assertEqual(R.set_errors([self.auto(auto_sender="Linkwave Internet")]), [])
        self.assertEqual(R.set_errors([self.auto(auto_sender=["linkwave internet"])]), [])
        c = self.auto(auto_sender="Brightgrid Billing")
        c["messages"][0]["sender_name"] = "Brightgrid Billing"
        self.assertEqual(R.set_errors([c]), [])

    def test_any_other_name_is_a_set_error(self):
        for bad in ("Brightgrid Power", "Linkwave Inter", ["Linkwave Internet", "Skyway Air"]):
            self.assertEqual(len(R.set_errors([self.auto(auto_sender=bad)])), 1, bad)
        self.assertTrue(R.set_errors([self.auto(auto_sender="Dana")]))           # a person of the case
        no_auto = case([msg(1, "p:dana", "Linkwave Internet is down again")], [], auto_sender="Linkwave Internet")
        self.assertEqual(len(R.set_errors([no_auto])), 1)                        # no automated message at all

    def test_the_run_stops_on_a_set_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "cases.jsonl")
            p.write_text(json.dumps(self.auto(auto_sender="Brightgrid Power")) + "\n", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = R.main(["--cases", str(p), "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("set error", err.getvalue())
        self.assertEqual(R.SCORER_VERSION, "2.4")


if __name__ == "__main__":
    unittest.main()
