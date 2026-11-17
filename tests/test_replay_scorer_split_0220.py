"""Replay scorer 2.1, report only (0.22.0): the false items split into no-item cases (noise) and cases with
expectations (real threads), by the read tier of the message that made them; the extra clashes split into those with a
false item on one side and those between real items; the read tiers per case kind and the expected items found per
tier (sure and likely apart), with each narrow-read case's routing. No scored number or bar changes."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tools"))

import extract as X  # noqa: E402
import replay_ledger as R  # noqa: E402
import writer  # noqa: E402

TZ = "America/Los_Angeles"


def item(iid, mid, section="needs_you", merged_into=None, typ="ask"):
    return {"id": iid, "type": typ, "created_mid": mid, "merged_into": merged_into, "cal_ref": None,
            "section": section}


def conflict(cid, a, b, mid="R:m1"):
    return {"id": cid, "sides": [a, b], "kind": "overlap", "created_mid": mid}


class TestSplit(unittest.TestCase):
    NOISE = {"case": "N", "no_items": True, "messages": [{"mid": "N:m1"}], "expect": [], "links": []}
    REAL = {"case": "R", "no_items": False, "messages": [{"mid": "R:m1"}, {"mid": "R:m2"}], "links": [],
            "expect": [{"eid": "R:e1", "type": "ask", "after": "R:m1", "confidence": "sure"},
                       {"eid": "R:e2", "type": "event", "after": "R:m2", "confidence": "likely"}]}
    TIERS = {"N:m1": ("narrow", "bulk"), "R:m1": ("full", ""), "R:m2": ("narrow", "automated, updates")}

    def run_split(self):
        end = {"items": {1: item(1, "R:m1"), 2: item(2, "N:m1"), 3: item(3, "R:m2", "coming_up", typ="event"),
                         4: item(4, "R:m1"), 5: item(5, "R:m2", "none"), 6: item(6, "R:m1", merged_into=1)},
               "conflicts": [conflict(1, ("item", 1), ("item", 2)), conflict(2, ("item", 1), ("event", "ev1")),
                             conflict(3, ("item", 1), ("item", 99)), conflict(4, ("item", 1), ("item", 3)),
                             conflict(5, ("item", 6), ("item", 3))],
               "followups": []}
        T = R.Tally()
        T.leftover = {4}                   # an ask left open after the owner's agreement (score_case counted it already)
        R.false_items([self.NOISE, self.REAL], {}, end, {1, 4}, R.L.zone(TZ), T, {4}, self.TIERS)
        return T, end

    def test_false_items_by_case_kind_and_read_tier(self):
        T, _ = self.run_split()
        self.assertEqual((T.false["false_items"], T.false["in_no_items_cases"]), (2, 1))   # scored as before
        split, _ = R.split_summary(T)
        self.assertEqual(split["false_items"], {"noise_cases": 1, "real_threads": 2,     # item 3 and the leftover 4
                                                "by_read_tier": {"full": 1, "narrow": 2}})
        reads = {f["ref"]: f["read"] for f in T.fails if f["metric"] == "false_item"}
        self.assertEqual(reads, {"item 2": "narrow", "item 3": "narrow"})

    def test_extra_clashes_with_a_false_item_or_between_real_items(self):
        T, _ = self.run_split()
        self.assertEqual(T.false["extra_clashes"], 4)                                   # conflict 4 was expected
        split, _ = R.split_summary(T)
        # 1: real vs false; 2: real vs a calendar entry; 3: a side not in the ledger; 5: merged into 1 vs false 3
        self.assertEqual(split["extra_clashes"], {"with_false_item": 2, "between_real_items": 1, "other": 1})

    def test_read_tiers_per_case_kind_and_recall_per_tier(self):
        T, end = self.run_split()
        snaps = {"R:m1": end, "R:m2": end, "N:m1": end}
        R.tier_report([self.NOISE, self.REAL], self.TIERS, {"R:e1": 1, "R:e2": None}, snaps, T)
        split, tiers = R.split_summary(T)
        self.assertEqual(split["reads_by_case_kind"], {"noise": {"narrow": 1}, "real": {"full": 1, "narrow": 1}})
        self.assertEqual(tiers["recall"], {
            "full": {"sure": {"items": {"n": 1, "found": 1, "pct": 100.0}, "asks": {"n": 1, "found": 1, "pct": 100.0}}},
            "narrow": {"likely": {"items": {"n": 1, "found": 0, "pct": 0.0}}}})
        self.assertEqual(tiers["narrow_cases"], {
            "N": {"kind": "noise", "reads": {"narrow": 1}, "why": ["bulk"], "expected": 0, "found": 0,
                  "expected_from_narrow": 0},
            "R": {"kind": "real", "reads": {"full": 1, "narrow": 1}, "why": ["automated, updates"], "expected": 2,
                  "found": 1, "expected_from_narrow": 1}})

    def test_an_unread_message_counts_as_unread_and_an_unjudged_case_apart(self):
        T = R.Tally()
        unj = {"case": "U", "no_items": None, "messages": [{"mid": "U:m1"}], "expect": []}
        R.tier_report([unj], {}, {}, {}, T)
        split, tiers = R.split_summary(T)
        self.assertEqual(split["reads_by_case_kind"], {"unjudged": {"unread": 1}})
        self.assertEqual(tiers["narrow_cases"], {})

    def test_the_summary_adds_the_split_and_keeps_the_scores(self):
        T, _ = self.run_split()
        s = R.summarize(T, 2, "real", {"stopped": None}, False)
        self.assertEqual(s["scorer_version"], "2.4")
        self.assertEqual(s["false"]["false_items"], 2)
        self.assertEqual(s["false"]["per_20_cases"], 20.0)
        self.assertFalse(s["bars"]["false_items_per_20"])
        self.assertEqual(s["false"]["split"]["false_items"]["noise_cases"], 1)
        self.assertIn("narrow_cases", s["tiers"])


class TestReplayRouting(unittest.TestCase):
    def test_the_sets_automated_sender_and_mail_signals(self):
        ctx = {"sender": ("p:auto", ""), "owner_names": {"jordan vale", "jordan"}}
        row = R.msg_row({"mid": "m", "source": "mail", "direction": "in", "sender": "p:auto", "to": ["owner"],
                         "ts": "2027-03-01T06:00-08:00", "text": "x", "chat_kind": "thread",
                         "labels": ["CATEGORY_PROMOTIONS"], "bulk": True}, "Jordan Vale", {})
        self.assertEqual((row["bulk"], row["labels"]), (True, ["CATEGORY_PROMOTIONS"]))   # Gmail: labelIds only
        self.assertEqual(R.replay_tier(row, ctx), ("narrow", "bulk, automated, promotions"))
        plain = R.msg_row({"mid": "m", "source": "mail", "direction": "in", "sender": "p:auto", "to": ["owner"],
                           "ts": "2027-03-01T06:00-08:00", "text": "x"}, "Jordan Vale", {})
        self.assertEqual(R.replay_tier(plain, ctx), ("narrow", "automated"))
        slack = dict(plain, source="slack")
        self.assertEqual(R.replay_tier(slack, ctx), ("full", ""))                       # not mail: never narrow
        person = dict(plain, sender="p:lena")
        self.assertEqual(R.replay_tier(person, {"sender": ("p:lena", ""), "owner_names": set()}), ("full", ""))

    def test_a_kept_answer_is_used_only_for_its_own_prompt(self):
        # 0.22.x: both tiers read with the full prompt, so a full answer re-scores a read of either tier; an answer kept
        # from the retired narrow prompt (a narrow line without "prompt") is never used
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "raw.jsonl")
            keep = R.raw_keeper(lambda body, info: '{"items": [], "updates": []}', raw)
            keep({}, {"msg_id": "a", "tier": "narrow", "prompt": "full"})
            keep({}, {"msg_id": "b", "tier": "full"})
            with open(raw, "a", encoding="utf-8") as f:
                f.write(json.dumps({"mid": "c", "content": "{}"}) + "\n")                 # before the tiers: full
                f.write(json.dumps({"mid": "n", "tier": "narrow", "content": "{}"}) + "\n")   # the narrow prompt
            with open(raw, encoding="utf-8") as f:
                lines = [json.loads(x) for x in f]
            self.assertEqual([x.get("prompt") for x in lines], ["full", "full", None, None])
            self.assertEqual(R.kept_prompt(lines[3]), "narrow")
            call = R.raw_caller(raw)
            for mid in "abc":
                for tier in ("full", "narrow"):
                    self.assertIn(call({}, {"msg_id": mid, "tier": tier, "prompt": "full"}),
                                  ('{"items": [], "updates": []}', "{}"))
            for tier in ("full", "narrow"):
                with self.assertRaises(writer.WriterError):
                    call({}, {"msg_id": "n", "tier": tier, "prompt": "full"})
            with self.assertRaises(writer.WriterError):
                call({}, {"msg_id": "zz", "tier": "full", "prompt": "full"})


class TestEndToEnd(unittest.TestCase):
    """A tiny set through main(): a newsletter (bulk, Promotions, the set's automated sender), a person's ask and a
    no-item chat. The oracle passes with the routing reported; the bad fake's false items are split."""
    W = {"tz": TZ, "phrase": "Thursday 10am", "all_day": False, "approx": False}

    def cases(self):
        base = {"set": "synthetic", "set_version": "1.1", "replay": "isolated", "patterns": [], "owner": "Jordan Vale",
                "owner_tz": TZ, "calendar": [], "updates": [], "conflicts": [], "follow_ups": [], "links": [],
                "note": None, "unjudged": []}
        news = dict(base, case="S-T-01", no_items=False, messages=[
            {"mid": "S-T-01:m1", "source": "mail", "direction": "in", "sender": "p:auto", "to": ["owner"],
             "chat": "mail:auto", "chat_kind": "thread", "ts": "2027-03-01T06:00-08:00", "subject": "Frame Weekly",
             "text": "This week: 5 lighting setups", "bulk": True, "labels": ["CATEGORY_PROMOTIONS"]}],
            expect=[{"eid": "S-T-01:e1", "type": "cleanup", "who": "p:auto", "what": "newsletter", "when": None,
                     "status": "open", "section": "clean_up", "after": "S-T-01:m1", "confidence": "sure"}])
        ask = dict(base, case="S-T-02", no_items=False, messages=[
            {"mid": "S-T-02:m1", "source": "mail", "direction": "in", "sender": "p:lena", "to": ["owner"],
             "chat": "mail:lena", "chat_kind": "thread", "ts": "2027-03-01T09:00-08:00", "subject": "coffee",
             "text": "Coffee Thursday 10am?"}],
            expect=[{"eid": "S-T-02:e1", "type": "ask", "who": "p:lena", "what": "coffee Thursday",
                     "when": dict(self.W, start="2027-03-04T10:00-08:00", end=None), "status": "open",
                     "section": "needs_you", "after": "S-T-02:m1", "confidence": "sure"}])
        chat = dict(base, case="S-T-03", no_items=True, messages=[
            {"mid": "S-T-03:m1", "source": "signal", "direction": "in", "sender": "p:sam", "to": ["owner"],
             "chat": "signal:sam", "chat_kind": "dm", "ts": "2027-03-01T10:00-08:00", "text": "lol same"}],
            expect=[])
        return [news, ask, chat]

    def run_main(self, extractor):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "cases.jsonl"
            p.write_text("".join(json.dumps(c) + "\n" for c in self.cases()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = R.main(["--cases", str(p), "--extractor", extractor, "--quiet"])
            return code, json.loads(out.getvalue())

    def test_the_oracle_passes_and_reports_the_routing(self):
        code, s = self.run_main("oracle")
        self.assertEqual((code, s["gate"], s["scorer_version"]), (0, "pass", "2.4"))
        self.assertEqual(s["false"]["split"]["false_items"], {"noise_cases": 0, "real_threads": 0, "by_read_tier": {}})
        self.assertEqual(s["false"]["split"]["reads_by_case_kind"], {"noise": {"full": 1},
                                                                    "real": {"full": 1, "narrow": 1}})
        self.assertEqual(s["tiers"]["narrow_cases"], {"S-T-01": {
            "kind": "real", "reads": {"narrow": 1}, "why": ["bulk, automated, promotions"], "expected": 1, "found": 1,
            "expected_from_narrow": 1}})
        self.assertEqual(s["tiers"]["recall"]["narrow"]["sure"]["items"], {"n": 1, "found": 1, "pct": 100.0})
        self.assertEqual(s["tiers"]["recall"]["full"]["sure"]["asks"], {"n": 1, "found": 1, "pct": 100.0})

    def test_the_bad_fakes_false_items_are_split(self):
        code, s = self.run_main("bad")
        self.assertEqual(code, 1)
        sp = s["false"]["split"]["false_items"]
        self.assertEqual(sp["noise_cases"], 1)                    # "lol same" became an ask
        self.assertEqual(sp["noise_cases"] + sp["real_threads"], s["false"]["false_items"])
        self.assertEqual(sum(sp["by_read_tier"].values()), s["false"]["false_items"])
        # 0.22.x: the newsletter's ask is left out by the narrow read in code and the mail listed in Clean up instead
        self.assertIsNone(sp["by_read_tier"].get("narrow"))


if __name__ == "__main__":
    unittest.main()
