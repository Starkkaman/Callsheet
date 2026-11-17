"""Scorer 2.4 (the acceptance rules, 2026-11-13): two-annotator scoring with --second-labels.
- Items of the two labellings pair by type (or overlapping types), who and `after`. REQUIRED = both have it (a miss
  counts against recall); OPTIONAL = only one has it (producing it is neither a miss nor a false item; counted for
  information); FALSE = a model item outside the union. Clashes the same way.
- Confidence, section and time are scored on required items only; the block reports the per-field agreement
  between the two labellings and flags a weak test (few required items).
- It works with the oracle, the bad fake and --extractor raw (kept answers re-scored); 2.3's scores are unchanged.
Synthetic fixtures (fictional people); in-process fakes only: no model server, no network."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tools"))

import extract as X  # noqa: E402
import replay_ledger as R  # noqa: E402

TZ = "America/Los_Angeles"
CALL = {"tz": TZ, "phrase": "Thursday at 3", "all_day": False, "approx": False,
        "start": "2026-11-05T15:00-08:00", "end": "2026-11-05T16:00-08:00"}
FRIDAY = {"tz": TZ, "phrase": "Friday", "all_day": True, "approx": False, "start_date": "2026-11-06",
          "end_date": "2026-11-06"}


def case_a():
    """Ana asks for a call on Thursday at 3 (the owner's calendar has an edit session then) and says the gear list is due
    Friday. Labelling A: the ask (sure) and a deadline event; the ask clashes with the edit session."""
    return {"case": "R-T-01", "set": "real", "set_version": "2.2", "replay": "isolated", "owner": "Jordan Vale",
            "patterns": [], "updates": [], "follow_ups": [], "links": [], "no_items": False, "unjudged": [],
            "calendar": [{"id": "cal:1", "title": "Edit session", "all_day": False, "status": "confirmed",
                          "start": "2026-11-05T15:00-08:00", "end": "2026-11-05T16:00-08:00"}],
            "messages": [{"mid": "R-T-01:m1", "source": "signal", "direction": "in", "sender": "p:ana",
                          "to": ["owner"], "chat": "c_ana", "chat_kind": "dm", "ts": "2026-11-02T09:00-08:00",
                          "text": "Can you call me Thursday at 3?"},
                         {"mid": "R-T-01:m2", "source": "signal", "direction": "in", "sender": "p:ana",
                          "to": ["owner"], "chat": "c_ana", "chat_kind": "dm", "ts": "2026-11-02T09:05-08:00",
                          "text": "Also the gear list is due Friday"}],
            "expect": [{"eid": "R-T-01.a", "type": "ask", "who": "p:ana", "what": "call Ana Thursday at 3",
                        "when": CALL, "status": "open", "section": "needs_you", "after": "R-T-01:m1",
                        "confidence": "sure"},
                       {"eid": "R-T-01.e", "type": "event", "subtype": "deadline", "who": "p:ana",
                        "what": "gear list due", "when": FRIDAY, "status": "open", "section": "coming_up",
                        "after": "R-T-01:m2", "confidence": "likely"}],
            "conflicts": [{"a": "R-T-01.a", "b": "cal:1", "after": "R-T-01:m1", "confidence": "sure",
                           "surface_within_s": 300, "kind": "overlap"}]}


def case_quiet():
    """A second case the second annotator didn't label: a thanks, no item."""
    return {"case": "R-T-02", "set": "real", "set_version": "2.2", "replay": "isolated", "owner": "Jordan Vale",
            "patterns": [], "updates": [], "follow_ups": [], "links": [], "no_items": True, "unjudged": [],
            "calendar": [], "conflicts": [], "expect": [],
            "messages": [{"mid": "R-T-02:m1", "source": "signal", "direction": "in", "sender": "p:ben",
                          "to": ["owner"], "chat": "c_ben", "chat_kind": "dm", "ts": "2026-11-03T09:00-08:00",
                          "text": "thanks!"}]}


def labels_b():
    """Labelling B: the same ask (likely, its own eid), a clean-up where A has the event; the same clash."""
    return {"case": "R-T-01", "no_items": False, "updates": [], "follow_ups": [], "links": [],
            "expect": [{"eid": "B-01.x", "type": "ask", "who": "p:ana", "what": "call Ana Thursday 3pm",
                        "when": CALL, "status": "open", "section": "needs_you", "after": "R-T-01:m1",
                        "confidence": "likely"},
                       {"eid": "B-01.y", "type": "cleanup", "who": "p:ana", "what": "gear list note", "when": None,
                        "status": "open", "section": "clean_up", "after": "R-T-01:m2", "confidence": "likely"}],
            "conflicts": [{"a": "B-01.x", "b": "cal:1", "after": "R-T-01:m1", "confidence": "likely",
                           "surface_within_s": 300, "kind": "overlap"}]}


def no_latency(x):
    """A summary part without the wall-clock latencies (p95_s), which differ from run to run."""
    if isinstance(x, dict):
        return {k: no_latency(v) for k, v in x.items() if k != "p95_s"}
    return x


class Run:
    def replay(self, cases, b=None, extra=()):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "cases.jsonl")
            p.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
            argv = ["--cases", str(p), "--quiet"] + list(extra)
            if b is not None:
                q = pathlib.Path(d, "labels-B.jsonl")
                q.write_text("".join(json.dumps(x) + "\n" for x in b), encoding="utf-8")
                argv += ["--second-labels", str(q)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = R.main(argv)
            return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class TestPairing(unittest.TestCase):
    def test_items_pair_by_type_who_and_after(self):
        a, b = case_a(), R.second_case(case_a(), labels_b())
        self.assertEqual(R.pair_items(a, b, R.L.zone(TZ)), [("R-T-01.a", "B-01.x")])

    def test_overlapping_types_pair_and_another_who_or_message_does_not(self):
        a = case_a()
        a["expect"][0] = dict(a["expect"][0], type=None, types=["ask", "event"], section=["needs_you", "coming_up"])
        lb = labels_b()
        self.assertEqual(R.pair_items(a, R.second_case(case_a(), lb), R.L.zone(TZ))[0][1], "B-01.x")
        for change in ({"who": "p:ben"}, {"after": "R-T-01:m2"}, {"type": "commitment"}):
            lb = labels_b()
            lb["expect"][0].update(change)
            self.assertNotIn(("R-T-01.a", "B-01.x"), R.pair_items(case_a(), R.second_case(case_a(), lb),
                                                                 R.L.zone(TZ)))

    def test_clashes_pair_through_the_item_pairing_and_a_pair_meets_a_group(self):
        a, b = case_a(), R.second_case(case_a(), labels_b())
        self.assertEqual(R.pair_clashes(a, b, {"B-01.x": "R-T-01.a"}), [(0, 0)])
        self.assertEqual(R.pair_clashes(a, b, {}), [])                       # B's eid unread: no pair
        b["conflicts"] = [{"a": "B-01.x", "entries": ["cal:1", "cal:2"], "after": "R-T-01:m1"}]
        self.assertEqual(R.pair_clashes(a, b, {"B-01.x": "R-T-01.a"}), [(0, 0)])

    def test_times_agree_when_equal_or_approximately_close(self):
        tz = R.L.zone(TZ)
        self.assertTrue(R.same_time(CALL, dict(CALL, phrase="3 pm Thu"), tz))
        self.assertFalse(R.same_time(CALL, dict(CALL, start="2026-11-05T17:00-08:00"), tz))
        self.assertTrue(R.same_time(CALL, dict(CALL, start="2026-11-05T16:30-08:00", approx=True), tz))
        self.assertTrue(R.same_time(None, {"phrase": "", "tz": TZ}, tz))
        self.assertFalse(R.same_time(FRIDAY, CALL, tz))


class TestBlock(Run, unittest.TestCase):
    def test_the_version(self):
        self.assertEqual(R.SCORER_VERSION, "2.4")

    def test_the_oracle_required_optional_and_agreement(self):
        code, s = self.replay([case_a(), case_quiet()], [labels_b()])
        self.assertEqual((code, s["gate"], s["scorer_version"]), (0, "pass", "2.4"))
        t = s["two_annotator"]
        self.assertEqual((t["labels_b"], t["cases"], t["cases_without_b"], t["b_cases_not_replayed"]),
                         ("labels-B.jsonl", 1, 1, 0))
        self.assertEqual(t["required"], {"items": 1, "clashes": 1, "weak_test": True, "weak_test_below": 30})
        rr = t["required_recall"]
        self.assertEqual((rr["all"], rr["asks"], rr["clashes"]), ({"n": 1, "found": 1, "pct": 100.0},) * 3)
        self.assertEqual((rr["events"]["n"], rr["commitments"]["n"], rr["cleanups"]["n"]), (0, 0, 0))
        # the oracle makes A's items: the A-only event is optional and produced, B's clean-up is not a miss
        self.assertEqual(t["optional"], {"items": 2, "a_only": 1, "b_only": 1, "produced": 1, "produced_a_only": 1,
                                         "produced_b_only": 0, "clashes": 0, "clashes_produced": 0})
        self.assertEqual(t["union_precision"], {"produced": 2, "in_union": 2, "false": 0, "pct": 100.0})
        self.assertEqual(t["false"], {"items": 0, "clashes": 0})
        self.assertEqual(t["required_fields"]["section"], {"n": 1, "ok": 1, "pct": 100.0})
        self.assertEqual(t["required_fields"]["time"], {"n": 1, "ok": 1, "pct": 100.0})
        self.assertEqual(t["required_fields"]["confidence"], {"n": 1, "ok": 1, "pct": 100.0})   # either reading
        ag = t["agreement"]
        self.assertEqual(ag["overall"], {"a": 2, "b": 2, "both": 1, "pct": 33.3})
        self.assertEqual(ag["confidence"], {"n": 1, "agree": 0, "pct": 0.0})
        self.assertEqual((ag["section"]["pct"], ag["time"]["pct"], ag["final_status"]["pct"]), (100.0,) * 3)
        self.assertEqual(ag["no_item"], {"n": 1, "agree": 1, "pct": 100.0})
        self.assertEqual(ag["clashes"], {"a": 1, "b": 1, "both": 1, "pct": 100.0})

    def test_the_bad_fake_misses_required_and_its_extra_item_is_false(self):
        code, s = self.replay([case_a()], [labels_b()], ["--extractor", "bad"])
        t = s["two_annotator"]
        self.assertEqual(t["required_recall"]["asks"], {"n": 1, "found": 1, "pct": 100.0})   # an undated ask
        self.assertEqual(t["required_recall"]["clashes"], {"n": 1, "found": 0, "pct": 0.0})
        self.assertEqual(t["required_fields"]["time"], {"n": 1, "ok": 0, "pct": 0.0})        # no time kept
        self.assertEqual(t["union_precision"], {"produced": 2, "in_union": 1, "false": 1, "pct": 50.0})
        self.assertEqual(t["optional"]["produced"], 0)

    def test_a_time_only_one_annotator_gives_is_not_scored(self):
        b = labels_b()
        b["expect"][0] = dict(b["expect"][0], when=None)          # B's ask has no time: A's time is optional
        code, s = self.replay([case_a()], [b], ["--extractor", "bad"])
        t = s["two_annotator"]
        self.assertEqual(t["required_recall"]["asks"]["found"], 1)
        self.assertEqual(t["required_fields"]["time"], {"n": 0, "ok": 0, "pct": None})

    def test_an_item_only_one_annotator_gives_is_never_false(self):
        b = labels_b()
        b["expect"][1] = dict(b["expect"][1], type="event", subtype="deadline", section="coming_up", when=FRIDAY)
        a = case_a()
        a["expect"] = a["expect"][:1]                    # A has no item for m2: B's event is optional
        code, s = self.replay([a], [b])
        t = s["two_annotator"]
        self.assertEqual((t["optional"]["b_only"], t["optional"]["produced"], t["false"]["items"]), (1, 0, 0))
        self.assertEqual(s["false"].get("false_items", 0), 0)

    def test_without_second_labels_nothing_changes(self):
        code, s = self.replay([case_a()])
        self.assertNotIn("two_annotator", s)
        code2, s2 = self.replay([case_a()], [labels_b()])
        for k in ("gate", "bars", "missed", "metrics", "false", "clash_groups"):
            self.assertEqual(no_latency(s[k]), no_latency(s2[k]), k)

    def test_a_bad_second_labelling_is_a_set_error(self):
        b = labels_b()
        b["expect"][0]["section"] = ["needs_you", "coming_up"]     # two sections for one type
        code, s = self.replay([case_a()], [b])
        self.assertEqual((code, s), (2, None))


class TestRaw(Run, unittest.TestCase):
    def test_kept_answers_re_score_with_the_second_labels(self):
        cases = [case_a(), case_quiet()]
        _code, want = self.replay(copy.deepcopy(cases), [labels_b()])
        with tempfile.TemporaryDirectory() as d:
            raw = pathlib.Path(d, "raw.jsonl")
            with raw.open("w", encoding="utf-8") as f:
                for c in cases:
                    oracle = R.Oracle([c])
                    for m in c["messages"]:
                        f.write(json.dumps({"mid": m["mid"], "tier": "full", "prompt": X.PROMPT_FULL,
                                            "content": json.dumps(oracle({}, {"msg_id": m["mid"]}))}) + "\n")
            code, got = self.replay(copy.deepcopy(cases), [labels_b()], ["--extractor", "raw", "--raw", str(raw)])
        self.assertEqual((code, got["run"]["failed_reads"]), (0, 0))
        self.assertEqual(got["two_annotator"], want["two_annotator"])


if __name__ == "__main__":
    unittest.main()
