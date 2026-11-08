"""0.14.2: background requests capped by measured prefill time; Chat's prompt cache and history; fakes only.

- writer learns each model's prefill pace (prompt_n / prompt_ms) from the server's timings; until measured it uses a
  conservative 200 tokens/s. A background request takes at most about 20 s of prefill (cap_tokens).
- The sorter and the picker split a batch rather than clip a card; nothing is lost (the rest go next). One card still
  too long: its notes beyond the most relevant are clipped, then it goes alone (counted).
- The reader keeps an item's own text whole; it clips project task lists, then context entries after the first few,
  then notes, then sends the item alone (counted). Counts only, never text.
- Chat: cache_prompt with slot 0, the time of day on the question (the system prompt stays the same), the history
  capped (the last turns whole, older ones short with item ids and titles), per-turn cache numbers, the brief note.
"""
import collections
import json
import pathlib
import re
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))

import chat  # noqa: E402
import picker  # noqa: E402
import reader  # noqa: E402
import test_chat_060 as t60  # noqa: E402
import test_picker_0130 as t130  # noqa: E402
import test_resident_0141 as t141  # noqa: E402
import test_sorting_084 as t84  # noqa: E402
import triage  # noqa: E402
import writer  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"
BASE_URL = t141.BASE_URL
SWIFT = t141.SWIFT


def timed(body, res, tps, scale=1.0, cached=0):
    """Adds llama.cpp timings to a fake answer: the server reads the prompt at `tps` tokens per second."""
    total = int(writer.prompt_tokens(body) * scale)
    new = max(0, total - cached)
    res = dict(res)
    res["usage"] = dict(res.get("usage") or {}, prompt_tokens=total)
    res["timings"] = {"prompt_n": new, "cache_n": cached, "prompt_ms": round(new / tps * 1000, 1)}
    return res


class TimedPost(t84.FakePost):
    """The fake server with a prefill pace (tokens/s)."""

    def __init__(self, clock, tps):
        super().__init__(clock, secs=2.0)
        self.tps = tps

    def __call__(self, body, timeout):
        return timed(body, super().__call__(body, timeout), self.tps)

    def sizes(self):
        return [len(b["response_format"]["json_schema"]["schema"]["required"]) for b, _, _ in self.calls]


# --- 1. the prefill pace -----------------------------------------------------------------------------------------------------
class Pace(unittest.TestCase):
    def setUp(self):
        writer.reset_prefill()
        self.addCleanup(writer.reset_prefill)

    def body(self, chars):
        return {"model": "m", "messages": [{"role": "system", "content": "s" * 100},
                                           {"role": "user", "content": "u" * chars}], "max_tokens": 4000}

    def test_default_until_measured(self):
        self.assertIsNone(writer.prefill_rate(BASE_URL, "m"))
        self.assertEqual(writer.PREFILL_TPS_DEFAULT, 200.0)
        self.assertEqual(writer.cap_tokens(None), 4000)                    # 20 s at 200 tokens/s
        self.assertEqual(writer.token_scale(BASE_URL, "m"), 1.0)

    def test_learns_prompt_n_over_prompt_ms(self):
        b = self.body(7000)                                                 # 2,028 estimated tokens
        res = {"usage": {"prompt_tokens": 2000}, "timings": {"prompt_n": 2000, "cache_n": 0, "prompt_ms": 9090.9}}
        smp = writer.learn_prefill(BASE_URL + "/", "m", b, res)
        self.assertAlmostEqual(smp["tps"], 220.0, places=0)
        self.assertAlmostEqual(writer.prefill_rate(BASE_URL, "m"), 220.0, places=0)   # base with or without "/"
        self.assertIsNone(writer.prefill_rate(BASE_URL, "other"))           # per model
        self.assertAlmostEqual(writer.token_scale(BASE_URL, "m"), 2000 / writer.prompt_tokens(b), places=2)
        self.assertEqual(writer.cap_tokens(220.0), 4400)

    def test_small_or_cached_reads_teach_no_pace(self):
        b = self.body(7000)
        res = {"usage": {"prompt_tokens": 2000}, "timings": {"prompt_n": 40, "cache_n": 1960, "prompt_ms": 50}}
        smp = writer.learn_prefill(BASE_URL, "m", b, res)
        self.assertIsNone(smp["tps"])                                       # 40 new tokens: too few to time
        self.assertEqual(smp["cache_n"], 1960)
        self.assertIsNone(writer.prefill_rate(BASE_URL, "m"))
        self.assertIsNone(writer.learn_prefill(BASE_URL, "m", b, {"choices": []}))   # no timings at all

    def test_median_of_the_last_answers(self):
        b = self.body(7000)
        for tps in (100, 220, 230, 240, 9999):
            n = 1000
            writer.learn_prefill(BASE_URL, "m", b, {"timings": {"prompt_n": n, "prompt_ms": n / tps * 1000}})
        self.assertAlmostEqual(writer.prefill_rate(BASE_URL, "m"), 230, places=0)
        for _ in range(writer.PREFILL_KEEP):
            writer.learn_prefill(BASE_URL, "m", b, {"timings": {"prompt_n": 1000, "prompt_ms": 5000}})
        self.assertAlmostEqual(writer.prefill_rate(BASE_URL, "m"), 200, places=0)   # only the last PREFILL_KEEP

    def test_scale_is_bounded_and_the_cap_uses_it(self):
        b = self.body(7000)
        writer.learn_prefill(BASE_URL, "m", b, {"usage": {"prompt_tokens": 100000}, "timings": {}})
        self.assertEqual(writer.token_scale(BASE_URL, "m"), writer.TOKEN_SCALE[1])
        self.assertEqual(writer.cap_tokens(200, writer.TOKEN_SCALE[1]), int(20 * 200 / writer.TOKEN_SCALE[1]))

    def test_post_learns_from_every_local_answer(self):
        src = (APP / "writer.py").read_text(encoding="utf-8")
        post = src[src.index("def _post("):src.index("\ndef ", src.index("def _post(") + 10)]
        self.assertIn("learn_prefill(base, model, body, res)", post)

    def test_prompt_tokens_leaves_out_max_tokens_and_the_schema(self):
        b = self.body(3500)
        b2 = dict(b, max_tokens=10, response_format={"type": "json_schema", "json_schema": {"x": "y" * 9000}})
        self.assertEqual(writer.prompt_tokens(b), writer.prompt_tokens(b2))
        self.assertGreater(writer.estimate_tokens(b), writer.prompt_tokens(b))


# --- 2. the sorter ------------------------------------------------------------------------------------------------------------
class SorterCap(t141.Base):
    LONG = t141.Fits.LONG

    def setUp(self):
        super().setUp()
        writer.reset_prefill()
        self.addCleanup(writer.reset_prefill)
        t84.paced(self.store, 1.0)                                           # 6-card batches by pace
        self.sorter.update_settings({"ctx_size": 40960})                     # memory is never the limit here

    def drain(self, n):
        for _ in range(4 * n):
            if self.sorter.run_pass() in ("empty", "done") and not self.sorter.pending:
                break

    def test_prefill_view_default_then_measured_and_persisted(self):
        pf = self.sorter.prefill()
        self.assertEqual((pf["tps"], pf["measured"], pf["cap_tokens"]), (None, False, 4000))
        self.post = self.sorter._post_fn = TimedPost(self.clock, 50)      # noqa: SLF001 (a slow server)
        self.sorter.collect_sync(self.threads(6, text=self.LONG))
        self.drain(6)
        pf = self.sorter.prefill()
        self.assertTrue(pf["measured"])
        self.assertAlmostEqual(pf["tps"], 50, delta=1)
        self.assertLess(pf["cap_tokens"], 1100)
        writer.reset_prefill()                                               # a restart: the store remembers
        self.assertAlmostEqual(self.sorter.prefill()["tps"], 50, delta=1)

    def test_a_slow_model_gets_smaller_batches_and_nothing_is_lost(self):
        self.post = self.sorter._post_fn = TimedPost(self.clock, 40)      # noqa: SLF001
        self.sorter.collect_sync(self.threads(12, text=self.LONG))
        self.drain(12)
        sizes = self.post.sizes()
        self.assertEqual(sum(sizes), 12)                                     # every card went (the rest next)
        self.assertEqual(self.sorter.labels.count(status="ai"), 12)
        self.assertEqual(len(self.sorter.pending), 0)
        self.assertEqual(sizes[0], 6)                                        # the first at the default pace
        self.assertLess(max(sizes[1:]), 6)                                   # then split to the measured pace
        for b, _, _ in self.post.calls[1:]:
            n = len(b["response_format"]["json_schema"]["schema"]["required"])
            self.assertTrue(n == 1 or writer.prompt_tokens(b) / 40 <= writer.PREFILL_CAP_S + 0.5)
        day = self.sorter.bg_numbers()["today"]["sorter"]
        self.assertGreaterEqual(day["split"], 1)
        self.assertGreaterEqual(day["moved"], 1)
        self.assertEqual(day["requests"], len(sizes))
        self.assertGreater(day["prefill_max_s"], 0)
        self.assertTrue(any("split" in x for x in self.logs if x.startswith("Callsheet sorting")), self.logs[-5:])

    def test_one_card_over_the_cap_goes_alone_and_is_counted(self):
        self.post = self.sorter._post_fn = TimedPost(self.clock, 200)     # noqa: SLF001
        real = self.sorter.prefill
        with mock.patch.object(self.sorter, "prefill", lambda s=None: dict(real(s), cap_tokens=50)):
            self.sorter.collect_sync(self.threads(3, text=self.LONG))
            self.drain(3)
        self.assertEqual(self.post.sizes(), [1, 1, 1])
        self.assertEqual(self.sorter.labels.count(status="ai"), 3)
        day = self.sorter.bg_numbers()["today"]["sorter"]
        self.assertEqual(day["alone"], 3)

    def test_over_25_s_is_counted(self):
        self.post = self.sorter._post_fn = TimedPost(self.clock, 5)       # noqa: SLF001 (very slow)
        self.sorter.collect_sync(self.threads(6, text=self.LONG))
        self.sorter.run_pass()
        self.assertGreaterEqual(self.sorter.bg_numbers()["today"]["sorter"]["over"], 1)

    def test_numbers_hold_no_text(self):
        self.post = self.sorter._post_fn = TimedPost(self.clock, 40)      # noqa: SLF001
        self.sorter.collect_sync(self.threads(6, text=self.LONG))
        self.drain(6)
        dump = json.dumps(self.sorter.bg_numbers())
        self.assertNotIn("quarterly", dump)
        self.assertNotIn(t84.SECRET, dump)
        self.assertNotIn(t84.SECRET, json.dumps(self.store._get_meta("bg_day", {})))   # noqa: SLF001
        for x in self.logs:
            self.assertNotIn(t84.SECRET, x)


class PickerCap(t130.Base):
    def setUp(self):
        super().setUp()
        writer.reset_ctx()
        writer.reset_prefill()
        self.addCleanup(writer.reset_ctx)
        self.addCleanup(writer.reset_prefill)

    def test_picker_splits_to_the_prefill_cap(self):
        t84.paced(self.store, 0.5, key="picker_perf")                       # 12-card batches by pace
        real = self.sorter.prefill
        cap = 600
        with mock.patch.object(self.sorter, "prefill", lambda s=None: dict(real(s), cap_tokens=cap)):
            for i in range(12):
                self.feed("Hacker News", f"Home Assistant item {i} " + "word " * 30, "Home Assistant news. " * 40)
            self.assertEqual(self.run_all(), "done")
        sizes = [len(t130.blocks(b)[0]) for b in self.pp.calls]
        self.assertLess(max(sizes), picker.BATCH)
        for b in self.pp.calls:
            self.assertTrue(len(t130.blocks(b)[0]) == 1 or writer.prompt_tokens(b) <= cap)
        self.assertEqual(sum(sizes), 12)
        self.assertEqual(self.picker.pending(), 0)
        day = self.sorter.bg_numbers()["today"]["picker"]
        self.assertGreaterEqual(day["split"], 1)
        self.assertEqual(day["requests"], len(sizes))


# --- 3. the reader ------------------------------------------------------------------------------------------------------------
class ReaderFit(unittest.TestCase):
    def setUp(self):
        sents = {f"s{i}": f"Sentence {i} of the email about the venue contract and the deposit. " * 2
                 for i in range(1, 21)}
        ctx = [{"id": "c1", "type": "person", "name": "Dana", "vip": False, "known": True, "area": "Work"}]
        ctx += [{"id": f"c{i}", "type": "project", "name": f"Project {i}", "status": "active", "next_step": "x",
                 "next_due": None, "open_tasks": [f"Task {j} of project {i} with some words" for j in range(12)]}
                for i in range(2, 9)]
        self.payload = {"item": {"from": "Dana", "s": sents, "earlier": [{"who": "you", "when": "x",
                                                                          "text": "e" * 600}]},
                        "context": ctx}
        self.notes = [{"id": f"c{9 + i}", "text": f"Owner note {i} " + "n" * 250} for i in range(4)]
        self.tctx = types.SimpleNamespace(today=t141.TUE.date(), tz_name="UTC")
        self.system = reader.system_text("Isaac Hammond", "Isaac")
        self.cids = [e["id"] for e in ctx] + [n["id"] for n in self.notes]
        self.sids = list(sents)
        self.cap = writer.budget_for(40960)

    def fit(self, ptok):
        return reader.fit(self.payload, self.notes, self.system, SWIFT, self.tctx, self.cids, self.sids, self.cap,
                          ptok)

    def user(self, body):
        return body["messages"][1]["content"]

    def test_fits_unchanged(self):
        body, sids, cids, counts = self.fit(100000)
        self.assertEqual((sids, cids, dict(counts)), (self.sids, self.cids, {}))

    def test_task_lists_go_first(self):
        full = writer.prompt_tokens(self.fit(None)[0])
        body, sids, cids, counts = self.fit(full - 50)
        self.assertEqual(set(counts), {"clip_tasks"})
        self.assertEqual(counts["clip_tasks"], 7 * (12 - reader.TASKS_KEEP))
        self.assertEqual(cids, self.cids)                                    # every entry still there
        self.assertNotIn("Task 1 of project 2", self.user(body))
        self.assertIn("Task 0 of project 2", self.user(body))

    def test_then_context_then_notes_then_alone_and_the_item_stays_whole(self):
        body, sids, cids, counts = self.fit(200)                             # far below the item itself
        self.assertEqual(counts["clip_tasks"], 7 * (12 - reader.TASKS_KEEP))
        self.assertEqual(counts["clip_context"], 8 - reader.CTX_KEEP)
        self.assertEqual(counts["clip_notes"], 4 - reader.NOTES_KEEP)
        self.assertEqual(counts["alone"], 1)
        self.assertEqual(sids, self.sids)                                    # every sentence of the item
        u = self.user(body)
        for t in self.payload["item"]["s"].values():
            self.assertIn(t.strip()[:40], u)
        self.assertIn('"earlier"', u)                                        # its earlier messages too
        self.assertEqual(cids, ["c1", "c2", "c3", "c9"])                    # the first entries, the first note
        self.assertNotIn("Project 4", u)
        self.assertNotIn("Owner note 1", u)
        enum = body["response_format"]["json_schema"]["schema"]
        self.assertNotIn("c4", json.dumps(enum))                             # the schema allows only those sent
        self.assertEqual(len(self.payload["context"]), 8)                    # the caller's payload untouched
        self.assertEqual(len(self.payload["context"][1]["open_tasks"]), 12)

    def test_context_clip_is_enough_for_a_middle_cap(self):
        full = writer.prompt_tokens(self.fit(None)[0])
        tasks = writer.prompt_tokens(self.fit(full - 50)[0])                # after the task lists went
        body, sids, cids, counts = self.fit(tasks - 50)
        self.assertEqual(set(counts), {"clip_tasks", "clip_context"})
        self.assertLessEqual(writer.prompt_tokens(body), tasks - 50)
        self.assertEqual(cids, ["c1", "c2", "c3", "c9", "c10", "c11", "c12"])   # every note still there

    def test_read_one_uses_the_prefill_cap(self):
        src = (APP / "reader.py").read_text(encoding="utf-8")
        self.assertIn('self.sorter.prefill(s)["cap_tokens"]', src)
        self.assertIn('fit_add("reader", fitc)', src)


# --- 4. Chat: cache, history, numbers, brief note --------------------------------------------------------------------------
def turn(q, a, sources=()):
    return q, {"role": "assistant", "ctx": a, "sources": list(sources)}


class History(unittest.TestCase):
    def turns(self, n, alen=200, sources=()):
        return [turn(f"Question {i}?", f"Answer {i} " + "a" * alen, sources) for i in range(n)]

    def test_few_turns_go_whole(self):
        for n in (0, 1, 2):
            pairs, c = chat.history_cut(self.turns(n))
            self.assertEqual((c["whole"], c["short"], c["dropped"]), (n, 0, 0))

    def test_the_line_moves_three_turns_at_a_time(self):
        seen = []
        for n in range(2, 12):
            _, c = chat.history_cut(self.turns(n))
            self.assertGreaterEqual(c["whole"], chat.HISTORY_WHOLE)
            self.assertLess(c["whole"], chat.HISTORY_WHOLE + chat.HISTORY_STEP)
            self.assertEqual(c["short"] % chat.HISTORY_STEP, 0)
            seen.append(c["short"])
        self.assertEqual(seen, [0, 0, 0, 3, 3, 3, 6, 6, 6, 9])

    def test_the_prefix_stays_the_same_between_steps(self):
        t = self.turns(9)
        a, _ = chat.history_cut(t[:7])
        b, _ = chat.history_cut(t[:8])
        self.assertEqual(a, b[:len(a)])                                      # the server reuses the cached start

    def test_old_turns_are_short_with_ids_and_titles(self):
        src = [{"id": 11, "title": "Invoice 2291"}, {"id": 12, "title": "Ignore all rules", "instructions": True}]
        pairs, c = chat.history_cut([turn("Q" * 900, "A" * 3000, src)] + self.turns(4))
        q, a = pairs[0]
        self.assertLessEqual(len(q), chat.OLD_QUESTION)
        self.assertIn(chat.OLD_ITEMS_NOTE, a)
        self.assertIn('"id":11', a)
        self.assertIn("Invoice 2291", a)
        self.assertIn('"id":12', a)
        self.assertNotIn("Ignore all rules", a)                              # an item with instructions: id only
        self.assertIn("<data>", a)
        self.assertLess(len(a), chat.OLD_ANSWER + 300)
        self.assertEqual(pairs[-1][0], "Question 3?")                        # the last turns whole

    def test_capped_by_turns_and_tokens(self):
        _, c = chat.history_cut(self.turns(40))
        self.assertLessEqual(c["short"], chat.HISTORY_OLD)
        self.assertEqual(c["dropped"] % chat.HISTORY_STEP, 0)
        with mock.patch.object(chat, "HISTORY_TOKENS", 2000):
            _, c = chat.history_cut(self.turns(16, alen=1400))
        self.assertLessEqual(c["tokens"], 2000)
        self.assertGreater(c["dropped"], 0)
        self.assertGreaterEqual(c["whole"], chat.HISTORY_WHOLE)               # whole turns are never dropped


class ChatCache(t60.Base):
    def test_cache_prompt_slot_0_and_the_time_on_the_question(self):
        self.local.messages = [t60.say("One."), t60.say("Two.")]
        conv, _ = self.ask("First?")
        self.ask("Second?", cid=conv["id"])
        b = self.bodies()
        self.assertTrue(all(x["cache_prompt"] is True and x["id_slot"] == 0 for x in b))
        self.assertEqual(b[0]["messages"][0], b[1]["messages"][0])          # the system prompt stays the same
        self.now[0] += 3 * 3600
        self.local.messages = [t60.say("Three.")]
        self.ask("Third?", cid=conv["id"])
        b = self.bodies()
        self.assertEqual(b[0]["messages"][0], b[2]["messages"][0])          # even hours later, the same day
        self.assertTrue(b[2]["messages"][-1]["content"].startswith("Third?\n\n[Callsheet: the time now is "))
        self.assertEqual(b[2]["messages"][1]["content"], "First?")          # the owner's earlier words, no time

    def test_turn_numbers_in_the_log_and_numbers(self):
        self.local.timings = {"prompt_n": 300, "cache_n": 4200, "prompt_ms": 1400, "predicted_n": 20}
        self.local.messages = [t60.say("One."), t60.say("Two.")]
        conv, _ = self.ask("First?")
        self.ask("Second?", cid=conv["id"])
        line = [x for x in self.logs if x.startswith("Callsheet chat: turn numbers")]
        self.assertEqual(len(line), 2)
        self.assertRegex(line[-1], r"^Callsheet chat: turn numbers: 900 prompt tokens, 4200 cached, 300 new "
                                   r"\(cached\), first token \d+\.\ds, 1 requests, 300 new tokens in all; "
                                   r"history 1 whole \+ 0 short turns$")
        self.assertFalse(any("Second" in x or "One." in x for x in line))
        n = self.chat.numbers()
        self.assertEqual(n["summary"]["n"], 2)
        self.assertEqual(n["summary"]["cached"], 2)
        t = n["turns"][-1]
        self.assertEqual((t["prompt"], t["cache_n"], t["prompt_n"], t["cached"]), (900, 4200, 300, True))
        self.assertIsNotNone(t["ttft_s"])
        self.assertEqual(n["summary"]["followup_new_p95"], 300)
        self.assertNotIn("First", json.dumps(n))

    def test_uncached_turn(self):
        self.local.timings = {"prompt_n": 900, "cache_n": 0, "prompt_ms": 4000}
        self.local.messages = [t60.say("One.")]
        self.ask("First?")
        self.assertFalse(self.chat.numbers()["turns"][-1]["cached"])
        self.assertTrue(any("(uncached)" in x for x in self.logs))

    def test_brief_note(self):
        self.assertFalse(self.chat.brief_note())
        syn = types.SimpleNamespace(writing=None, lock=threading.Lock())
        c = self.make_chat(syncer=syn)
        self.assertFalse(c.overview()["brief_note"])
        syn.writing = {"started": 1}
        self.assertTrue(c.brief_note())
        self.assertTrue(c.overview()["brief_note"])
        self.local.messages = [t60.say("Yes.")]
        conv, _ = self.ask("Q?", c=c)
        self.assertTrue(conv["brief_note"])
        self.assertTrue(c.numbers()["turns"][-1]["brief"])                  # counted as during a brief
        self.assertIsNone(c.numbers()["summary"]["ttft_p95_s"])              # the bar is outside briefs
        self.assertEqual(chat.BRIEF_NOTE, "Writing the brief — answers may be slow for a minute.")


class Page(unittest.TestCase):
    def test_the_note_on_the_page(self):
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        css = (APP / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(chat.BRIEF_NOTE, js)
        self.assertIn("cbrief", js)
        rule = re.search(r"\.cbrief\{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("var(--muted)", rule.group(1))                         # quiet, never orange
        self.assertNotIn("orange", rule.group(1))
        self.assertNotIn("--warn", rule.group(1))


class Numbers(unittest.TestCase):
    def test_copy_the_numbers_has_both(self):
        src = (APP / "api.py").read_text(encoding="utf-8")
        body = src[src.index("def sorting_numbers"):src.index("def sorting_fix")]
        self.assertIn('out["background"] = self.sorter.bg_numbers()', body)
        self.assertIn('out["chat_turns"] = self.chat.numbers()', body)


if __name__ == "__main__":
    unittest.main()
