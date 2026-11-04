"""0.9.0 sorting: Gemma + prompt t083.2 byte-identical to the 0.8.3 screen (hash pinned), the t083.2 addressing cues
computed by Callsheet, the t083.3 Cc/Bcc rule after the model (with the screen's hard to_owner set offline: Gemma's
logged t083.2 answers before and after the rule), the 0.9.x extraction policy (dates only), the throughput caps and
the night backfill resuming across nights. No model is ever called."""
import datetime as dt
import json
import pathlib
import sys
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import index as ix  # noqa: E402
import rules  # noqa: E402
import test_sorting_084 as t84  # noqa: E402
import triage  # noqa: E402

OWNER, TUE, ctx, answer, src = t84.OWNER, t84.TUE, t84.ctx, t84.answer, t84.src
HARD = ROOT / "tests" / "fixtures" / "sorting_hard083.json"
CUES = ("you_copied", "greeting_to", "mentions", "vocative_to", "owner_named")


def built(s, c=None):
    return triage.build(s, c or ctx())


def full(ai):
    return dict({"evidence_s": [], "ask": "none", "facts": [], "confidence": "high", "evidence": []}, **ai)


class PromptT0832(unittest.TestCase):
    def test_byte_identical_to_the_screen_and_pinned(self):
        self.assertEqual(triage.PROMPT_VERSION, "t083.2")
        self.assertEqual(triage.TO_OWNER_RULE, "t083.3")
        text = triage.system_text("Isaac Hammond", "Isaac", ["Zac"])
        # the screen's prompt_hash("t083.2"): system text, user wrapper and version id
        self.assertEqual(triage.screen_hash(text), "ecfcfcc5b3abafa4")
        self.assertEqual(json.loads(HARD.read_text())["prompt_hash"], "ecfcfcc5b3abafa4")
        self.assertEqual(triage.prompt_hash(), "1fa0ae0e24e5d714")      # the template (before names)
        self.assertIn('("Isaac, copying you")', text)                   # the owner's name, from Setup
        self.assertIn("you_copied (true: the owner is only in Cc or Bcc)", text)
        self.assertIn("before, you_copied. from.kind", text)
        self.assertNotIn("<<", triage.system_text())

    def test_gemma_and_shadow_by_default(self):
        d = triage.default_settings()
        self.assertEqual((d["model"], d["mode"]), ("gemma-4-26b-a4b-it-qat-q4_0", "shadow"))
        card, _ = built(src("1", "Sam <sam@known.example>", "Q", "Hi Isaac, can you send it?"))
        body = triage.request_body(d["model"], [dict({"id": "c1"}, **card)], ctx().system(), TUE.date(), "UTC")
        self.assertEqual(body["model"], triage.GEMMA)
        self.assertEqual(body["messages"][0]["content"], triage.system_text("Isaac Hammond", "Isaac", ["Zac"]))


class Cues(unittest.TestCase):
    def test_card_carries_the_cues_in_the_screens_order(self):
        s = src("1", "Tom <tom@new.example>", "Stems", "Hi Priya, can you send the stems by Friday?",
                to="priya@x.example", cc=OWNER)
        card, meta = built(s)
        keys = list(card)
        i = keys.index("instr")
        self.assertEqual(keys[i + 1:i + 6], list(CUES))
        self.assertEqual({k: card[k] for k in CUES}, {"you_copied": True, "greeting_to": "other", "mentions": "none",
                                                     "vocative_to": "none", "owner_named": False})
        self.assertEqual(meta["cues"], {k: card[k] for k in CUES})
        card, _ = built(src("2", "Tom <tom@new.example>", "Hi", "Isaac and Priya, can you both join? +Jordan too.",
                            to="priya@x.example", cc=OWNER))
        self.assertEqual((card["vocative_to"], card["mentions"], card["owner_named"]), ("owner_and_other", "other", True))
        card, _ = built(src("3", "Tom <tom@new.example>", "Hi", "Hi team, the cut is locked. Hammond approved."))
        self.assertEqual((card["you_copied"], card["greeting_to"], card["owner_named"]), (False, "group", True))

    def test_index_mail_is_never_copied(self):
        # the index has no To/Cc headers: its "Cc" is a guess from the people list
        row = {"id": 7, "source": "gmail", "account": OWNER, "source_id": "m1", "kind": "message", "title": "Plan",
               "people": f"Ana <ana@x.example>; Bo <bo@x.example>; {OWNER}", "ts": TUE.timestamp(),
               "body": "Hi Bo, can you check the plan?"}
        card, meta = built(triage.index_source(row, [OWNER]))
        self.assertEqual(card["you"], "cc")
        self.assertFalse(card["you_copied"])
        self.assertEqual(triage.copied_only(meta, full(answer())), (full(answer()), False))

    def test_cues_equal_the_screens_on_its_hard_set(self):
        data = json.loads(HARD.read_text())
        c = hard_ctx(data["ctx"])
        bad = [it["id"] for it in data["items"] if built(it["src"], c)[1]["cues"] != it["cues"]]
        self.assertEqual(bad, [])
        self.assertEqual(len(data["items"]), 171)


def hard_ctx(c):
    return triage.Ctx(c["owner_addrs"], c["owner_first"], c["owner_full"], c["vips"], c["known"],
                      {(k, v): kind for k, v, kind in c["handles"]}, c["first_names"], {},
                      dt.date.fromisoformat(c["today"]), zoneinfo.ZoneInfo(c["tz_name"]), c["tz_name"])


class CopiedOnly(unittest.TestCase):
    """t083.3: owner only in Cc/Bcc, nothing addresses the owner, owner not named -> to_owner no, for_someone_else."""

    def rule(self, s, ai=None, c=None):
        _, meta = built(s, c)
        return triage.copied_only(meta, full(ai or answer()))

    def cc(self, text, frm="Tom <tom@new.example>", to="priya@x.example", **kw):
        return src("1", frm, "Stems", text, to=to, cc=OWNER, **kw)

    def test_fires_on_cc_and_bcc(self):
        out, ch = self.rule(self.cc("Hi Priya, can you send the stems by Friday?"))
        self.assertTrue(ch)
        self.assertEqual((out["kind"], out["to_owner"]), ("for_someone_else", "no"))
        out, ch = self.rule(src("2", "Tom <tom@new.example>", "Stems", "Can you send the stems?", to="priya@x.example"))
        self.assertTrue(ch)                                     # Bcc: the owner isn't listed at all
        out, ch = self.rule(self.cc("The cut is locked."), answer("fyi", "unclear", ()))
        self.assertEqual((out["kind"], out["to_owner"], ch), ("for_someone_else", "no", True))

    def test_kept_when_something_addresses_the_owner(self):
        for text in ("Hi Isaac, can you send the stems?",                # greeting
                     "Priya, Isaac, can you both check this?",           # vocative (the "Isaac," line)
                     "Isaac, can you check this?",
                     "Can you check this @Isaac?",                       # mention
                     "Does anyone have the stems?",                      # a question to the whole group
                     "Hi team, who can take Friday?",
                     "Looping in Isaac for the budget."):                # owner named
            out, ch = self.rule(self.cc(text))
            self.assertFalse(ch, text)
            self.assertEqual(out["to_owner"], "yes", text)

    def test_overrides_keep_the_models_label(self):
        out, ch = self.rule(self.cc("Can you send the stems?", frm="Maya <maya@vip.example>", auth=t84.PASS))
        self.assertFalse(ch)                                    # VIP
        _, ch = self.rule(self.cc("Can you send the stems?"), answer("security", "yes"))
        self.assertFalse(ch)                                    # security (the model's or the rules')
        _, ch = self.rule(self.cc("Your password was changed. If this wasn't you, contact support."))
        self.assertFalse(ch)
        _, ch = self.rule(self.cc("Can you make Friday?", invite=("1",)))
        self.assertFalse(ch)                                    # invite
        _, ch = self.rule(self.cc("Can you send the stems?", to="Isaac Hammond <isaac.h@other.example>"))
        self.assertFalse(ch)                                    # the owner's name in To
        self.assertEqual(triage.copied_only({"cues": {}}, None), (None, False))

    def test_promos_and_updates_keep_their_kind(self):
        out, ch = self.rule(self.cc("Our spring sale starts now."), answer("promo", "yes", ()))
        self.assertEqual((out["kind"], out["to_owner"], ch), ("promo", "no", True))
        out, ch = self.rule(self.cc("Your order shipped."), answer("automated_update", "no", ()))
        self.assertEqual((out["kind"], ch), ("automated_update", False))

    def test_merge_places_and_stores_the_ruled_label(self):
        s = self.cc("Hi Priya, can you send the stems by Friday?")
        card, meta = built(s)
        m = triage.merge({"c1": meta}, {"c1": full(answer())}, ["c1"], TUE.date())["c1"]
        self.assertEqual((m["group"], m["why"]), ("probably_not_for_you", "rules:copied_only"))
        self.assertEqual(m["ai"]["kind"], "for_someone_else")
        self.assertRegex(m["why"], triage.WHY)

    def test_the_sorter_stores_the_ruled_label(self):
        b = t84.Base("run")
        b.setUp()
        try:
            th = [(t84.thread("1", "Tom <tom@new.example>", "Stems", "Hi Priya, can you send the stems?",
                              to="priya@x.example", cc=OWNER), OWNER, "work", "acc1")]
            self.assertEqual(b.sorter.collect_sync(th), 1)
            self.assertEqual(b.sorter.run_pass(), "done")
            lab = b.sorter.labels.get(rules.row_key("mail:t1:1"))
            self.assertEqual((lab["ai_kind"], lab["to_owner"], lab["grp"], lab["why"], lab["prompt"]),
                             ("for_someone_else", "no", "probably_not_for_you", "rules:copied_only", "t083.2"))
        finally:
            b.doCleanups()


class HardSetOffline(unittest.TestCase):
    """The screen's hard to_owner set (171 accuracy items with a hard tag, 3 reps each = 513 answers): Gemma's logged
    t083.2 answers, scored before and after t083.3. Gemma's misses there are promos written like personal notes,
    newsletters from people and the "unclear" group items; none is a Cc/Bcc-only card, so t083.3 changes nothing
    (it never makes a right answer wrong). The numbers are pinned: a change to the cues or the rule shows here."""

    def test_to_owner_before_and_after(self):
        data = json.loads(HARD.read_text())
        c = hard_ctx(data["ctx"])
        n = before = after = fired = applies = 0
        broke = []
        for it in data["items"]:
            _, meta = built(it["src"], c)
            gold = it["gold"]["to_owner"]
            probe, ch = triage.copied_only(meta, full(answer()))
            if ch:
                applies += 1
                self.assertEqual((it["gold"]["kind"], gold), ("for_someone_else", "no"), it["id"])
            for x in it["gemma"]:
                n += 1
                ai = full(x) if x else None
                out, ch = triage.copied_only(meta, ai)
                fired += ch
                ok0 = bool(ai) and ai["to_owner"] == gold
                ok1 = bool(out) and out["to_owner"] == gold
                before += ok0
                after += ok1
                if ok0 and not ok1:
                    broke.append(it["id"])
        self.assertEqual(n, 513)
        self.assertEqual((before, after), (414, 414))          # 0.807 -> 0.807
        self.assertEqual(broke, [])
        self.assertEqual(applies, 7)                            # the rule's cases there: all gold "no"
        self.assertEqual(fired, 0)                              # ... and Gemma already said "no" on each


class ExtractionPolicy(unittest.TestCase):
    def facts(self, text, fs, **kw):
        _, meta = built(src("1", "Sam <sam@known.example>", "Plan", text, **kw))
        return triage.finalize_facts(meta, full(answer(facts=fs)), TUE.date(), {"isaac"})

    def test_dates_only(self):
        f = self.facts("Can you send the deck by Friday 6 Nov? We decided to go with the blue cover.",
                       [{"type": "deadline", "s": "s1", "date": "2026-11-06", "time": None, "who": "owner"},
                        {"type": "decision", "s": "s2", "date": None, "time": None, "who": "sender"},
                        {"type": "commitment_to_owner", "s": "s2", "date": None, "time": None, "who": "sender"}])
        self.assertEqual([(x["type"], x["status"], x["verified"], x["headsup"]) for x in f],
                         [("deadline", "ok", True, True), ("decision", "held", None, False),
                          ("commitment_to_owner", "held", None, False)])
        used = triage.used_dates(f)
        self.assertEqual([(x["type"], x["s"], x["date"]) for x in used], [("deadline", "s1", "2026-11-06")])
        # a date the parser can't confirm is kept with its sentence id but marked unverified (no heads-up)
        f = self.facts("Let's meet after the show.",
                       [{"type": "date", "s": "s1", "date": "2026-11-09", "time": None, "who": "other"}])
        self.assertEqual((f[0]["verified"], f[0]["headsup"]), (False, False))
        self.assertEqual(len(triage.used_dates(f)), 1)
        row = triage.label_row("a" * 16, "mail", "1", "2026-11-03", 0.0, {"pre_kind": "fyi", "strength": "weak",
                               "evidence": [], "overrides": [], "marks": []}, None,
                               {"group": "for_your_info", "why": "rules_only",
                                "facts": [dict(f[0], status="held")]}, "ai", triage.GEMMA, "t083.2")
        self.assertEqual(row["facts"][0]["status"], "held")


class Throughput(unittest.TestCase):
    def test_caps_fit_the_measured_rate(self):
        lo, hi = triage.MEASURED_CPM
        self.assertEqual((lo, hi), (21, 36))
        self.assertGreaterEqual(triage.DAY_GPU_MIN * lo, triage.DAY_CARDS)     # 1,200 cards fit at 21 a minute
        self.assertTrue(lo <= triage.BATCH / triage.EST_BATCH_S * 60 <= hi)
        self.assertEqual(triage.BACKFILL["nights"], 7)


class NightResumes(t84.Base):
    def setUp(self):
        super().setUp()
        self.index = ix.Index(self.tmp.name, clock=self.clock.now)
        self.addCleanup(self.index.close)
        self.index.upsert([{"source": "gmail", "account": OWNER, "source_id": f"m{i}", "kind": "message",
                            "thread": f"t{i}", "title": f"Note {i}", "people": [f"P{i} <p{i}@x.example>", OWNER],
                            "ts": TUE.timestamp() - 86400 * (5 + i % 20) - i, "body": "Can you check the draft?"}
                           for i in range(40)])
        self.sorter.index = self.index
        self.post.secs = 60.0

    def test_a_night_runs_batches_back_to_back_and_the_next_night_resumes(self):
        self.sorter._last_ix = self.clock.now()       # no incremental index read in this test
        self.clock.at(TUE.replace(hour=2))
        self.sorter._last_ix = self.clock.now()
        self.assertEqual(self.sorter.tick(), "night:ran")
        first = len(self.post.calls)
        self.assertGreater(first, 1)                  # not one batch per 30 s tick
        self.assertLess(first * triage.BATCH, 40)     # ... but within one run's budget
        for d in (1, 2, 3):                           # later nights continue from the cursor
            self.clock.at((TUE + dt.timedelta(days=d)).replace(hour=2))
            self.sorter._last_ix = self.clock.now()
            self.sorter.tick()
        st = self.store._get_meta("sorting_backfill")
        self.assertEqual(st["state"], "done")
        self.assertGreaterEqual(len(st["nights"]), 2)
        self.assertEqual(self.post.cards(), 40)       # every item once
        self.assertEqual(self.sorter.labels.count(status="ai"), 40)


if __name__ == "__main__":
    unittest.main()
