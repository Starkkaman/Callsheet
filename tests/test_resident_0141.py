"""0.14.1: every background role on ONE resident model (any name), fakes only.

- slot 1 goes to the configured background model (the sorter's model setting), whatever its name; Chat keeps slot 0;
  Gemma stays the default for new installs and nothing compares against it.
- The in-flight budget is 80% of the model's context: the server's GET /models entry (`--ctx-size` or a context in
  its status), else the owner's per-model "Context size", else 32,768. A single sorter, reader or picker request
  always fits the budget.
- Batches size themselves from the model's measured seconds per card (per model) so one takes about 20 s (1 to the
  caps); an unmeasured model starts at FIRST_BATCH. The sorter's status projects the cards an hour and a day.
- Thinking off for every background request (Qwen family included); finish=length is counted.
- Chat says when it shares the background model.
"""
import json
import pathlib
import re
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))

import chat  # noqa: E402
import picker  # noqa: E402
import presets  # noqa: E402
import reader  # noqa: E402
import test_picker_0130 as t130  # noqa: E402
import test_reader_0120 as t120  # noqa: E402
import test_sorting_084 as t84  # noqa: E402
import triage  # noqa: E402
import writer  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"
TUE = t84.TUE
BASE_URL = "http://127.0.0.1:9/v1"
# the measured paces from the owner's server (2026-11-07): Gemma 14 s per 6-card sorter batch; the resident Swift
# (presets.SUGGESTION's model, a Qwen-family model) ~20 s per 2-card sorter batch and ~34 s per 12-card picker batch
GEMMA_CARD_S = 14 / 6
SWIFT = presets.SUGGESTION["model"]
SWIFT_CARD_S = 20 / 2
SWIFT_PICK_S = 34 / 12


def router(model, args=None, status=None, **extra):
    """A router's GET /models answer with one entry for `model`."""
    st = {"value": "loaded"}
    if args is not None:
        st["args"] = args
    st.update(status or {})
    return {"data": [dict({"id": model, "object": "model", "status": st}, **extra)]}


class PacedPost(t84.FakePost):
    """The fake model server at a pace per card (a request of n cards takes n x card_s); `finish` sets the
    finish_reason of every answer."""

    def __init__(self, clock, card_s, finish=None):
        super().__init__(clock)
        self.card_s, self.finish = card_s, finish

    def __call__(self, body, timeout):
        self.secs = self.card_s * len(body["response_format"]["json_schema"]["schema"]["required"])
        res = super().__call__(body, timeout)
        if self.finish:
            res["choices"][0]["finish_reason"] = self.finish
        return res

    def sizes(self):
        return [len(b["response_format"]["json_schema"]["schema"]["required"]) for b, _, _ in self.calls]


class Base(t84.Base):
    def setUp(self):
        super().setUp()
        writer.reset_ctx()
        self.addCleanup(writer.reset_ctx)
        self.store._set_meta("sorting_perf", {})          # unmeasured (t84.Base measures Gemma)
        self.store.update_writer({"provider": "local", "local": {"base_url": BASE_URL, "model": triage.GEMMA}})

    def use(self, model, card_s=None, post=None):
        self.sorter.update_settings({"model": model})
        if card_s:
            t84.paced(self.store, card_s, model)
        if post is not None:
            self.post = self.sorter._post_fn = post  # noqa: SLF001


# --- 1. the resident model's slot, whatever its name ---------------------------------------------------------------------
class Slot(unittest.TestCase):
    def test_slot_1_for_the_configured_background_model(self):
        for m in (triage.GEMMA, SWIFT, "any-model-name"):
            self.assertEqual(triage.slot_for(m), triage.SLOT)
            self.assertEqual(triage.slot_for(m, background=m), 1)
        self.assertEqual(triage.slot_for(SWIFT, background=triage.GEMMA), 0)
        self.assertEqual(triage.slot_for(triage.GEMMA, background=SWIFT), 0)
        self.assertEqual(chat.LOCAL_DEFAULTS["id_slot"], 0)            # Chat keeps slot 0

    def test_every_background_request_has_slot_1_and_thinking_off_for_qwen(self):
        cards = [{"id": "c1", "s": {"s1": "Hi"}}]
        bodies = [triage.request_body(SWIFT, cards, "x", TUE.date(), "UTC"),
                  reader.request_body(SWIFT, "x", "u", ["c1"], ["s1"]),
                  picker.request_body(SWIFT, "x", "u", ["k1"], ["i1"])]
        self.assertIn("qwen", SWIFT.lower())
        for b in bodies:
            self.assertEqual(b["model"], SWIFT)
            self.assertEqual(b["id_slot"], 1)
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})
            self.assertNotIn("reasoning_budget", b)
            self.assertNotIn("thinking", json.dumps({k: v for k, v in b.items() if k != "messages"}).replace(
                "enable_thinking", ""))

    def test_gemma_is_only_the_default(self):
        self.assertEqual(triage.default_settings()["model"], triage.GEMMA)
        for name in ("triage.py", "reader.py", "picker.py", "chat.py", "writer.py"):
            src = (APP / name).read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"==\s*GEMMA\b|\bGEMMA\s*==|!=\s*GEMMA\b|\bGEMMA\s*!=", src), [], name)
        for name in ("reader.py", "picker.py", "chat.py", "writer.py"):
            self.assertNotIn("GEMMA", (APP / name).read_text(encoding="utf-8"), name)


class SlotRuns(Base):
    def test_a_swift_sorter_uses_slot_1_and_its_own_name(self):
        self.use(SWIFT, SWIFT_CARD_S)
        self.sorter.collect_sync(self.threads(3))
        self.sorter.run_pass()
        self.assertTrue(self.post.calls)
        for b, _, _ in self.post.calls:
            self.assertEqual((b["model"], b["id_slot"]), (SWIFT, 1))
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(triage.background_model(self.store), SWIFT)

    def test_background_model_new_install_and_off(self):
        self.assertEqual(triage.background_model(self.store), triage.GEMMA)
        self.sorter.update_settings({"mode": "off"})
        self.assertIsNone(triage.background_model(self.store))


# --- 2. the context and the budget ----------------------------------------------------------------------------------------
class Context(unittest.TestCase):
    def setUp(self):
        writer.reset_ctx()
        self.addCleanup(writer.reset_ctx)

    def test_budget_is_80_percent(self):
        self.assertEqual(writer.budget_for(65536), 52428)
        self.assertEqual(writer.budget_for(32768), 26214)
        self.assertEqual(writer.CTX_DEFAULT, 32768)
        self.assertEqual(writer.INFLIGHT_MAX, 26214)

    def test_ctx_from_the_router_entry(self):
        cases = [
            ({"status": {"args": ["llama-server", "--ctx-size", "32768", "--kv-unified", "-np", "2"]}}, 32768),
            ({"status": {"args": ["llama-server", "--ctx-size=65536"]}}, 65536),
            ({"status": {"args": ["llama-server", "-c", "16384"]}}, 16384),
            ({"status": {"args": ["llama-server", "-ctx", "8192"]}}, 8192),
            ({"args": "llama-server -c 32768 --jinja"}, 32768),
            ({"status": {"value": "loaded", "n_ctx": 65536}}, 65536),
            ({"meta": {"n_ctx": 32768, "n_ctx_train": 262144}}, 32768),
            ({"meta": {"n_ctx_train": 262144}}, None),                    # the training length isn't the server's
            ({"status": {"args": ["llama-server", "--ctx-size", "0"]}}, None),   # 0: the model's own (unknown)
            ({"status": {"args": ["llama-server", "--ctx-size", "99999999"]}}, None),
            ({"status": {"args": ["llama-server", "--ctx-size"]}}, None),
            ({"status": {"args": ["llama-server", "--ctx-size", "True"]}}, None),
            ({"status": {"n_ctx": True}}, None),
            ({}, None), ("x", None)]
        for entry, want in cases:
            self.assertEqual(writer.ctx_of_entry(entry), want, entry)

    def test_models_ctx_finds_the_model(self):
        res = {"data": [{"id": "other", "status": {"args": ["-c", "8192"]}},
                        {"id": "m1", "aliases": ["alias-1"], "status": {"args": ["--ctx-size", "32768"]}}]}
        self.assertEqual(writer.models_ctx(res, "m1"), 32768)
        self.assertEqual(writer.models_ctx(res, "alias-1"), 32768)
        self.assertEqual(writer.models_ctx(res, "other"), 8192)
        self.assertIsNone(writer.models_ctx(res, "missing"))
        self.assertEqual(writer.models_ctx({"models": [{"name": "m2", "n_ctx": 65536}]}, "m2"), 65536)
        for bad in (None, [], {"data": "x"}, {"data": [1, None]}):
            self.assertIsNone(writer.models_ctx(bad, "m1"))

    def test_server_ctx_is_cached_and_never_raises(self):
        calls, now = [], [0.0]

        def fetch(base, key):
            calls.append(base)
            return router("m1", ["llama-server", "--ctx-size", "32768"])
        self.assertEqual(writer.server_ctx(BASE_URL, None, "m1", fetch, lambda: now[0]), 32768)
        self.assertEqual(writer.server_ctx(BASE_URL + "/", None, "m1", fetch, lambda: now[0]), 32768)
        self.assertEqual(len(calls), 1)
        self.assertEqual(writer.cached_ctx(BASE_URL, "m1"), 32768)
        now[0] = writer.CTX_TTL_S + 1
        writer.server_ctx(BASE_URL, None, "m1", fetch, lambda: now[0])
        self.assertEqual(len(calls), 2)

        def down(base, key):
            calls.append(base)
            raise writer.WriterError("unreachable")
        self.assertIsNone(writer.server_ctx(BASE_URL, None, "m2", down, lambda: now[0]))
        self.assertIsNone(writer.server_ctx(BASE_URL, None, "m2", down, lambda: now[0]))
        self.assertEqual(len(calls), 3)                                # a miss is remembered too
        self.assertIsNone(writer.server_ctx("", None, "m1", fetch))
        self.assertIsNone(writer.cached_ctx(BASE_URL, "never"))


class SorterContext(Base):
    def test_server_then_setting_then_default(self):
        got = {}
        self.sorter._models_fn = lambda base, key: got.setdefault("r", router(triage.GEMMA, ["--ctx-size", "65536"]))
        self.assertEqual(self.sorter.ctx_size(), (65536, "server"))
        self.assertEqual(self.sorter.budget(), 52428)
        writer.reset_ctx()
        self.sorter._models_fn = lambda base, key: router(triage.GEMMA)      # the server says nothing usable
        self.assertEqual(self.sorter.ctx_size(), (32768, "default"))
        self.assertEqual(self.sorter.budget(), 26214)
        v = self.sorter.update_settings({"ctx_size": 65536})
        self.assertEqual(v["settings"]["ctx_size"], 65536)
        self.assertEqual(self.sorter.ctx_size(), (65536, "setting"))
        self.assertEqual(self.sorter.budget(), 52428)

    def test_the_setting_is_per_model(self):
        self.sorter.update_settings({"ctx_size": 65536})
        v = self.sorter.update_settings({"model": SWIFT, "ctx_size": 32768})
        self.assertEqual(v["settings"]["ctx_size"], 32768)
        self.assertEqual(self.sorter.settings()["ctx_sizes"], {triage.GEMMA: 65536, SWIFT: 32768})
        self.assertEqual(self.sorter.ctx_size(), (32768, "setting"))
        v = self.sorter.update_settings({"model": triage.GEMMA})
        self.assertEqual(v["settings"]["ctx_size"], 65536)
        v = self.sorter.update_settings({"ctx_size": None})                    # cleared: the default again
        self.assertIsNone(v["settings"]["ctx_size"])
        self.assertEqual(v["settings"]["ctx_default"], 32768)
        self.assertEqual(self.sorter.ctx_size(), (32768, "default"))
        for bad in (100, 2_000_000, "32768", True, 32768.5):
            with self.assertRaises(triage.InputError, msg=bad):
                self.sorter.update_settings({"ctx_size": bad})
        self.store._set_meta("sorting", dict(self.store._get_meta("sorting"), ctx_sizes={"x y": 1, SWIFT: "big"}))
        self.assertEqual(self.sorter.settings()["ctx_sizes"], {})             # a stored value gone bad is dropped

    def test_no_server_request_with_fakes_or_from_the_page(self):
        self.assertEqual(self.sorter.ctx_size(), (32768, "default"))           # tests' post fake: never asks
        self.sorter._models_fn = mock.Mock(side_effect=AssertionError("no request from the page"))
        self.sorter.status()
        self.sorter.view()
        self.sorter._models_fn.assert_not_called()

    def test_every_role_posts_with_the_models_budget(self):
        self.sorter._post_fn = None  # noqa: SLF001
        self.sorter._models_fn = lambda base, key: router(triage.GEMMA, ["llama-server", "--ctx-size", "32768"])
        rd = reader.Reader(self.store, self.sorter, log=self.logs.append)
        pk = picker.Picker(self.store, self.sorter, None, log=self.logs.append)
        seen = []

        def fake_post(base, key, body, opts, log, clock, notify=None, timing=None, budget=None, stream=None):
            seen.append((base, budget, body["id_slot"]))
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}
        body = {"model": triage.GEMMA, "id_slot": 1, "messages": []}
        with mock.patch.object(writer, "_post", fake_post):
            self.sorter._post(body)  # noqa: SLF001
            rd._post(body, 30)  # noqa: SLF001
            pk._post(body, 30)  # noqa: SLF001
        self.assertEqual(seen, [(BASE_URL, 26214, 1)] * 3)
        self.assertEqual((self.sorter.lengths, rd.lengths, pk.lengths), (1, 1, 1))   # finish=length: counted


# --- a single request always fits ---------------------------------------------------------------------------------------
class Fits(Base):
    LONG = ("Hi Isaac, the quarterly figures are attached and the summary is below. " * 30)

    def test_sorter_trims_a_batch_to_fit_the_budget(self):
        t84.paced(self.store, 1.0)
        self.sorter.update_settings({"ctx_size": 8192})                       # budget 6,553
        cap = self.sorter.budget()
        self.sorter.collect_sync(self.threads(12, text=self.LONG))
        for _ in range(12):
            if self.sorter.run_pass() in ("empty", "done") and not self.sorter.pending:
                break
        sizes = [len(b["response_format"]["json_schema"]["schema"]["required"]) for b, _, _ in self.post.calls]
        self.assertLess(max(sizes), triage.BATCH)                            # trimmed below the size it picked
        for b, _, _ in self.post.calls:
            n = len(b["response_format"]["json_schema"]["schema"]["required"])
            self.assertTrue(n == 1 or writer.estimate_tokens(b) <= cap, (n, writer.estimate_tokens(b), cap))
        self.assertEqual(sum(sizes), 12)                                     # nothing lost: the rest went next
        self.assertEqual(self.sorter.labels.count(status="ai"), 12)
        self.assertEqual(len(self.sorter.pending), 0)

    def test_a_32k_pool_fits_full_batches(self):
        t84.paced(self.store, 1.0)
        self.sorter.update_settings({"ctx_size": 32768})
        self.sorter.collect_sync(self.threads(12, text=self.LONG))
        self.sorter.run_pass()
        self.assertEqual([len(b["response_format"]["json_schema"]["schema"]["required"]) for b, _, _ in
                          self.post.calls], [6, 6])
        for b, _, _ in self.post.calls:
            self.assertLessEqual(writer.estimate_tokens(b), self.sorter.budget())

    def test_reader_fit_drops_earlier_then_sentences(self):
        sents = {f"s{i}": "This sentence is about the quarterly figures and the plan. " * 6 for i in range(1, 31)}
        payload = {"item": {"from": "Dana", "s": dict(sents), "earlier": [{"who": "you", "when": "x",
                                                                            "text": "y" * 400}] * 2},
                   "context": []}
        tctx = types.SimpleNamespace(today=TUE.date(), tz_name="UTC")
        system = reader.system_text("Isaac Hammond", "Isaac")
        sids = list(sents)
        body, ids = reader.fit(payload, [], system, SWIFT, tctx, [], sids, writer.budget_for(32768))
        self.assertEqual(ids, sids)                                          # fits: unchanged
        self.assertIn('"earlier"', body["messages"][1]["content"])
        cap = writer.estimate_tokens(body) // 2
        body, ids = reader.fit(payload, [], system, SWIFT, tctx, [], sids, cap)
        self.assertLessEqual(writer.estimate_tokens(body), cap)
        self.assertNotIn('"earlier"', body["messages"][1]["content"])
        self.assertLess(len(ids), len(sids))
        self.assertEqual(ids, sids[:len(ids)])                              # the first sentences stay
        due = body["response_format"]["json_schema"]["schema"]["properties"]["due"]["properties"]
        self.assertEqual(due["date_s"]["anyOf"][0]["enum"], ids)            # the schema allows only those sent
        self.assertEqual(len(payload["item"]["s"]), 30)                     # the caller's payload is untouched
        self.assertIn("earlier", payload["item"])


class PickerFits(t130.Base):
    def setUp(self):
        super().setUp()
        writer.reset_ctx()
        self.addCleanup(writer.reset_ctx)

    def test_picker_trims_a_batch_to_fit_the_budget(self):
        t84.paced(self.store, 0.5, key="picker_perf")                       # 12-card batches by pace
        self.assertEqual(self.picker.batch_size(), picker.BATCH)
        cap = 2500                                                           # a budget smaller than 12 cards
        self.sorter.budget = lambda s=None: cap
        for i in range(12):
            self.feed("Hacker News", f"Home Assistant item {i} " + "word " * 30, "Home Assistant news. " * 40)
        self.assertEqual(self.run_all(), "done")
        sizes = [len(t130.blocks(b)[0]) for b in self.pp.calls]
        self.assertLess(max(sizes), picker.BATCH)
        for b in self.pp.calls:
            self.assertTrue(len(t130.blocks(b)[0]) == 1 or writer.estimate_tokens(b) <= cap)
        self.assertEqual(sum(sizes), 12)
        self.assertEqual(self.picker.pending(), 0)


# --- 3. self-sizing batches -------------------------------------------------------------------------------------------------
class Sizing(unittest.TestCase):
    def test_size_for(self):
        self.assertEqual(triage.TARGET_S, 20)
        self.assertEqual(triage.size_for(None, 6), triage.FIRST_BATCH)
        self.assertEqual(triage.size_for(None, 1), 1)
        self.assertEqual(triage.size_for(GEMMA_CARD_S, triage.BATCH), 6)          # Gemma, by day: the cap
        self.assertEqual(triage.size_for(GEMMA_CARD_S, triage.NIGHT_BATCH), 8)    # ... at night: 8 x 2.3 s ~ 19 s
        self.assertEqual(triage.size_for(SWIFT_CARD_S, triage.BATCH), 2)          # Swift: 2 x 10 s = 20 s
        self.assertEqual(triage.size_for(SWIFT_CARD_S, triage.NIGHT_BATCH), 2)
        self.assertEqual(triage.size_for(SWIFT_PICK_S, picker.BATCH), 7)          # Swift picker: 7 x 2.8 s ~ 20 s
        self.assertEqual(triage.size_for(45.0, 6), 1)                             # never below 1
        self.assertEqual(triage.size_for(0.1, 6), 6)                              # never above the cap
        for card_s in (GEMMA_CARD_S, SWIFT_CARD_S, SWIFT_PICK_S, 7.0, 13.0):
            n = triage.size_for(card_s, 16)
            self.assertLessEqual(n * card_s, triage.TARGET_S)

    def test_perf_is_per_model_and_reads_the_old_list(self):
        store = types.SimpleNamespace(meta={})
        store._get_meta = lambda k, d=None: store.meta.get(k, d)
        store._set_meta = lambda k, v: store.meta.__setitem__(k, v)
        store.meta["sorting_perf"] = {"card_s": [2.0, 2.5, 3.0]}                # before 0.14.1: one list
        self.assertEqual(triage.perf_get(store, "sorting_perf", "m-now"), [2.0, 2.5, 3.0])
        triage.perf_add(store, "sorting_perf", "m-now", 20.0, 2)
        self.assertEqual(store.meta["sorting_perf"], {"models": {"m-now": [2.0, 2.5, 3.0, 10.0]}})
        triage.perf_add(store, "sorting_perf", "m-two", 14.0, 6)
        self.assertEqual(triage.perf_get(store, "sorting_perf", "m-two"), [2.333])
        self.assertEqual(triage.perf_get(store, "sorting_perf", "m-now"), [2.0, 2.5, 3.0, 10.0])
        self.assertEqual(triage.perf_get(store, "sorting_perf", "m-new"), [])
        for i in range(triage.PERF_MODELS + 2):
            triage.perf_add(store, "sorting_perf", f"x{i}", 1.0, 1)
        self.assertEqual(len(store.meta["sorting_perf"]["models"]), triage.PERF_MODELS)
        self.assertIsNone(triage.pace([]))
        self.assertEqual(triage.pace([1.0, 3.0, 2.0]), 2.0)


class SizingRuns(Base):
    def run_all(self):
        for _ in range(40):
            self.sorter.run_pass()
            if not self.sorter.pending:
                return
            self.clock.advance(30)
        self.fail("the sorter didn't finish")

    def test_swift_by_day_starts_small_and_stays_at_20_s(self):
        post = PacedPost(self.clock, SWIFT_CARD_S)
        self.use(SWIFT, post=post)
        self.sorter.collect_sync(self.threads(10))
        self.run_all()
        self.assertEqual(post.sizes()[0], triage.FIRST_BATCH)                  # unmeasured: a short first batch
        self.assertEqual(set(post.sizes()), {2})
        self.assertLessEqual(max(post.sizes()) * SWIFT_CARD_S, triage.TARGET_S)   # Chat waits <= ~20 s behind one
        self.assertAlmostEqual(triage.pace(self.sorter._perf(SWIFT)), SWIFT_CARD_S)  # noqa: SLF001

    def test_gemma_by_day_grows_to_the_cap(self):
        post = PacedPost(self.clock, GEMMA_CARD_S)
        self.use(triage.GEMMA, post=post)
        self.sorter.collect_sync(self.threads(20))
        self.run_all()
        self.assertEqual(post.sizes(), [2, 6, 6, 6])
        self.assertLessEqual(6 * GEMMA_CARD_S, triage.TARGET_S)

    def test_night_sizes(self):
        self.sorter.update_settings({"backfill": {"on": False}})
        post = PacedPost(self.clock, GEMMA_CARD_S)
        self.use(triage.GEMMA, GEMMA_CARD_S, post)
        self.clock.at(TUE.replace(hour=23))
        self.sorter.collect_sync(self.threads(20))
        self.sorter.run_pass()
        self.assertEqual(post.sizes()[:2], [8, 8])                           # not 12: 12 x 2.3 s would be 28 s
        post.calls.clear()
        self.use(SWIFT, SWIFT_CARD_S, PacedPost(self.clock, SWIFT_CARD_S))
        self.sorter.collect_sync(self.threads(6, start=100))
        self.sorter.run_pass()
        self.assertEqual(set(self.sorter._post_fn.sizes()), {2})  # noqa: SLF001

    def test_a_model_switch_uses_that_models_pace(self):
        t84.paced(self.store, GEMMA_CARD_S, triage.GEMMA)
        t84.paced(self.store, SWIFT_CARD_S, SWIFT)
        s = self.sorter.settings()
        self.assertEqual(self.sorter.batch_size(s), 6)
        self.use(SWIFT)
        self.assertEqual(self.sorter.batch_size(self.sorter.settings()), 2)
        self.use("brand-new-model")
        self.assertEqual(self.sorter.batch_size(self.sorter.settings()), triage.FIRST_BATCH)

    def test_context_full_still_halves(self):
        t84.paced(self.store, 1.0)
        self.sorter._shrink_until = self.clock.now() + 60  # noqa: SLF001
        self.assertEqual(self.sorter.batch_size(self.sorter.settings()), 3)
        self.use(SWIFT, SWIFT_CARD_S)
        self.assertEqual(self.sorter.batch_size(self.sorter.settings()), 1)

    def test_day_caps_still_stop_a_run(self):
        post = PacedPost(self.clock, SWIFT_CARD_S)
        self.use(SWIFT, SWIFT_CARD_S, post)
        self.sorter.update_settings({"day_cards": 50})
        self.sorter.collect_sync(self.threads(60))
        for _ in range(60):
            if self.sorter.run_pass() == "cap":
                break
            self.clock.advance(30)
        self.assertEqual(self.sorter.run_pass(), "cap")
        self.assertLessEqual(self.sorter._day()["cards"], 51)  # noqa: SLF001

    def test_the_night_job_reads_rows_by_the_sized_batch(self):
        import index as ix
        index = ix.Index(self.tmp.name, clock=self.clock.now)
        self.addCleanup(index.close)
        index.upsert([{"source": "gmail", "account": t84.OWNER, "source_id": f"m{i}", "kind": "message",
                       "thread": f"t{i}", "title": f"Note {i}", "people": [f"P{i} <p{i}@x.example>", t84.OWNER],
                       "ts": TUE.timestamp() - 86400 * 3 - i, "body": "Can you check the draft?"} for i in range(10)])
        self.sorter.index = index
        post = PacedPost(self.clock, SWIFT_CARD_S)
        self.use(SWIFT, SWIFT_CARD_S, post)
        self.clock.at(TUE.replace(hour=23))
        self.assertEqual(self.sorter.backfill_step(), "ran")
        self.assertEqual(post.sizes(), [2])
        self.assertEqual(self.store._get_meta("sorting_backfill")["done_n"], 2)


class Capacity(Base):
    def test_gemma_64k(self):
        t84.paced(self.store, GEMMA_CARD_S)
        self.sorter.update_settings({"ctx_size": 65536})
        c = self.sorter.status()["capacity"]
        self.assertEqual((c["batch"], c["night_batch"]), (6, 8))
        self.assertEqual((c["ctx"], c["ctx_from"], c["budget"]), (65536, "setting", 52428))
        self.assertEqual(c["per_hour"], 1540)                                   # 3,600 / 2.33 s
        self.assertEqual(c["per_day"], 1200)                                    # the day's card cap
        self.assertEqual(c["card_s"], 2.33)

    def test_swift_32k(self):
        self.use(SWIFT, SWIFT_CARD_S)
        c = self.sorter.status()["capacity"]
        self.assertEqual((c["model"], c["batch"], c["night_batch"]), (SWIFT, 2, 2))
        self.assertEqual((c["ctx"], c["ctx_from"], c["budget"]), (32768, "default", 26214))
        self.assertEqual(c["per_hour"], 360)
        self.assertEqual(c["per_day"], 360)                                     # 60 model minutes a day / 10 s
        self.sorter._models_fn = lambda base, key: router(SWIFT, ["llama-server", "--ctx-size", "32768"])
        self.sorter.ctx_size()                                                   # the sorter thread asks
        self.assertEqual(self.sorter.status()["capacity"]["ctx_from"], "server")

    def test_unmeasured(self):
        c = self.sorter.status()["capacity"]
        self.assertEqual((c["per_hour"], c["per_day"], c["card_s"]), (None, None, None))
        self.assertEqual(c["batch"], triage.FIRST_BATCH)

    def test_the_page_says_it(self):
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("' emails an hour on this model'", js)
        self.assertIn("'Context size'", js)
        self.assertIn("ctx_size: ctx.value.trim() ? parseInt(ctx.value, 10) : null", js)
        self.assertIn("'sorting.ctx_size'", js)


# --- 4. finish=length is counted ----------------------------------------------------------------------------------------
class Lengths(Base):
    def test_sorter_counts_answers_cut_at_max_tokens(self):
        post = PacedPost(self.clock, 1.0, finish="length")
        self.use(triage.GEMMA, 1.0, post)
        self.sorter.collect_sync(self.threads(8))
        self.sorter.run_pass()
        self.assertEqual(self.sorter._day()["length"], 2)  # noqa: SLF001
        self.assertEqual(self.sorter.status()["capacity"]["lengths"], 2)
        self.assertTrue(any("2 cut at max tokens" in x for x in self.logs))
        self.assertEqual(self.sorter.labels.count(status="ai"), 8)             # handled as before (usable answers)

    def test_a_cut_answer_falls_back_as_before(self):
        post = PacedPost(self.clock, 1.0, finish="length")
        post.reply = lambda ids, body: '{"c1": {"kind": "fy'
        self.use(triage.GEMMA, 1.0, post)
        self.sorter.collect_sync(self.threads(3))
        self.sorter.run_pass()
        self.assertEqual(self.sorter.labels.count(status="fallback"), 3)
        self.assertEqual(self.sorter._day()["length"], 1)  # noqa: SLF001


class ReaderLengths(t120.Base):
    def test_reader_counts_answers_cut_at_max_tokens(self):
        orig = self.rpost.__call__

        def cut(body, timeout):
            res = orig(body, timeout)
            res["choices"][0]["finish_reason"] = "length"
            return res
        self.reader._post_fn = cut  # noqa: SLF001
        self.read(t120.item("1"))
        self.assertEqual(self.reader.status()["lengths"], 1)
        self.assertTrue(any("1 cut at max tokens" in x for x in self.logs))


class PickerLengths(t130.Base):
    def test_picker_counts_answers_cut_at_max_tokens(self):
        orig = self.pp.__call__

        def cut(body, timeout):
            res = orig(body, timeout)
            res["choices"][0]["finish_reason"] = "length"
            return res
        self.picker._post_fn = cut  # noqa: SLF001
        for i in range(3):
            self.feed("Hacker News", f"Home Assistant item {i}", "Home Assistant. " * 10)
        self.assertEqual(self.run_all(), "done")
        self.assertGreaterEqual(self.picker.status()["lengths"], 1)
        self.assertEqual(self.picker.status()["lengths"], len(self.pp.calls))

    def test_picker_sizes_from_its_own_pace(self):
        self.assertEqual(self.picker.batch_size(), triage.FIRST_BATCH)
        t84.paced(self.store, SWIFT_PICK_S, triage.GEMMA, key="picker_perf")
        self.assertEqual(self.picker.batch_size(), 7)
        self.assertEqual(self.picker.status()["batch"], 7)
        self.sorter.update_settings({"model": SWIFT})
        self.assertEqual(self.picker.batch_size(), triage.FIRST_BATCH)        # another model: its own pace
        orig = self.pp.__call__

        def paced(body, timeout):                                              # PPost's time is per request
            self.pp.secs = SWIFT_PICK_S * len(t130.blocks(body)[0])
            return orig(body, timeout)
        self.picker._post_fn = paced  # noqa: SLF001
        for i in range(10):
            self.feed("Hacker News", f"Home Assistant item {i}", "Home Assistant. " * 10)
        self.assertEqual(self.run_all(), "done")
        self.assertEqual([len(t130.blocks(b)[0]) for b in self.pp.calls], [triage.FIRST_BATCH, 7, 1])
        self.assertAlmostEqual(triage.pace(triage.perf_get(self.store, "picker_perf", SWIFT)), SWIFT_PICK_S,
                               places=2)
        self.assertEqual(self.picker.batch_size(), 7)


# --- 5. Chat shares the background model ------------------------------------------------------------------------------------
class ChatShares(Base):
    def test_shares_when_same_server_and_model(self):
        self.assertTrue(chat.public_settings(self.store)["local"]["shares_background"])   # Gemma, the writer's server
        self.sorter.update_settings({"model": SWIFT})
        self.assertFalse(chat.public_settings(self.store)["local"]["shares_background"])
        self.store.update_writer({"local": {"model": SWIFT}})
        self.assertTrue(chat.shares_background(self.store))
        chat.update_settings(self.store, {"local": {"same_as_writer": False, "base_url": "http://10.0.0.9:8080/v1",
                                                    "model": SWIFT}})
        self.assertFalse(chat.shares_background(self.store))                   # another server
        chat.update_settings(self.store, {"local": {"same_as_writer": True}})
        self.assertTrue(chat.shares_background(self.store))
        chat.update_settings(self.store, {"provider": "claude"})
        self.assertFalse(chat.shares_background(self.store))
        chat.update_settings(self.store, {"provider": "local"})
        self.sorter.update_settings({"mode": "off"})
        self.assertFalse(chat.shares_background(self.store))

    def test_the_page_says_so(self):
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("L.shares_background ? h('p', { class: 'note chshare' }, 'Chat and background work share one "
                      "model: no waiting for a swap.')", js)


if __name__ == "__main__":
    unittest.main()
