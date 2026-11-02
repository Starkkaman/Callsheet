"""0.6.1 writer presets per call time against fakes: a fake llama.cpp router that holds one model at a time (swap
delay, "Loading model" 503 while it swaps, a model that never answers in time), the fake Google and the fake
Anthropic API (which must never be called by a preset run). Store validation and migration, api/brief's writing
state, the scheduler's call kind, request bodies, fallback, reload, logs and page checks. Never contacts real
services."""
import json
import os
import pathlib
import re
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import fake_ai  # noqa: E402
import presets  # noqa: E402
import sync  # noqa: E402
import writer  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from store import InputError, NotFound, Store  # noqa: E402
from test_writer import HAVE_SDK, KEY, LOCAL_KEY, WriterBase  # noqa: E402

FAST_MODEL = "gemma-4-26b-a4b-it-qat-q4_0"
SWIFT = presets.SUGGESTION["model"]
APP_JS = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "callsheet" / "app" / "static" / "app.css").read_text(encoding="utf-8")
ANSWER = '{"summary": "Summary by %s.", "replies": {"r1": "Thanks, I will look at it.", "r2": ""}}'


class FakeRouter(fake_ai.FakeLocal):
    """llama-server in router mode: one model loaded at a time. A request for another model swaps (sleeps
    .swap_s) and, when .loading_503 is set, first answers 503 "Loading model" once per swap. Models in .hang
    sleep .hang_s before answering (longer than the request's timeout); models in .broken get HTTP 500."""

    def __init__(self):
        super().__init__()
        self.models = [FAST_MODEL, SWIFT]
        self.loaded = FAST_MODEL
        self.swap_s = 0.0
        self.loading_503 = False
        self.hang, self.hang_s = set(), 3.0
        self.broken = set()
        self.swaps = []
        self.timings = {"prompt_n": 880, "cache_n": 20, "predicted_n": 120, "prompt_ms": 400.0, "predicted_ms": 600.0}

    def answer(self, req):
        if req["method"] != "POST":
            return super().answer(req)
        model = (req["body"] or {}).get("model")
        if model in self.broken:
            return 500, {"error": {"message": "boom"}}, None
        if model != self.loaded:
            if self.loading_503 and not getattr(self, "_told", None) == model:
                self._told = model
                return 503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}, None
            time.sleep(self.swap_s)
            self.swaps.append((self.loaded, model))
            self.loaded = model
            self._told = None
        if model in self.hang:
            time.sleep(self.hang_s)
        self.contents = [ANSWER % model]
        return super().answer(req)


class PresetBase(WriterBase):
    def setUp(self):
        super().setUp()
        self.loc.close()
        self.loc = FakeRouter()
        sl = mock.patch.object(writer, "_sleep", lambda s: None)  # the loading retry waits 10 s in production
        sl.start()
        self.addCleanup(sl.stop)

    def local_on(self):
        return self.ok("PATCH", "/api/writer", {"provider": "local", "local": {
            "base_url": self.loc.base, "model": FAST_MODEL, "api_key": LOCAL_KEY}})

    def careful(self, **kinds):
        r = self.ok("POST", "/api/writer/presets", {"suggestion": "careful"})
        if kinds:
            self.ok("PATCH", "/api/writer", {"call_presets": {k: r["preset_id"] for k in kinds}})
        return r["preset_id"]

    def chats(self):
        return [r["body"] for r in self.loc.chats()]

    def brief(self):
        return self.ok("GET", "/api/brief")


# --- store: migration, validation ---------------------------------------------------------------
class StoreTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    def test_a_060_writer_becomes_the_fast_preset_and_every_call_uses_it(self):
        old = {"provider": "local", "local": {"base_url": "http://10.0.0.2:8080/v1", "model": FAST_MODEL,
                                              "api_key": None, "thinking": False, "reasoning_budget": 1536,
                                              "max_tokens": 6000, "context": 32768, "timeout": 420, "id_slot": 3},
               "fallback_to_claude": True, "configured_by_owner": True}
        self.store._set_meta("writer", old)  # what 0.6.0 stored: no presets, no mapping
        self.store.close()
        self.store = Store(self.tmp.name)
        w = self.store.writer_public()
        self.assertEqual(w["presets"], [{"id": "fast", "name": "Fast", "builtin": True, "model": FAST_MODEL,
                                         "thinking": False, "effort": None, "kwargs": {}, "reasoning_budget": 1536,
                                         "max_tokens": 6000, "timeout": 420, "id_slot": 3}])
        self.assertEqual(w["call_presets"], {"morning": "fast", "midday": "fast", "afternoon": "fast",
                                             "refresh": "fast"})
        self.assertTrue(w["reload_fast"])
        self.assertEqual([k["id"] for k in w["call_kinds"]], ["morning", "midday", "afternoon", "refresh"])
        self.assertEqual(w["preset_suggestion"]["label"], "Careful (Swift, thinking)")
        # the old local view is unchanged (no new keys there)
        self.assertEqual(w["local"]["max_tokens"], 6000)
        self.assertNotIn("effort", w["local"])
        cfg = self.store.writer_config()
        for kind in presets.KIND_IDS + (None, "urgent"):
            self.assertEqual(presets.for_kind(cfg, kind)["id"], "fast")
            self.assertFalse(presets.is_slow(cfg, kind))
        # Fast's options are exactly the 0.6.0 local options: the request body can't change
        self.assertEqual(presets.options(cfg["local"], presets.fast(cfg["local"])), writer.local_options(cfg["local"]))

    def test_add_validate_and_delete(self):
        p = self.store.add_preset({"name": " Careful ", "model": SWIFT, "thinking": True, "reasoning_budget": 2048,
                                   "timeout": 900, "effort": "medium", "kwargs": {"reasoning": "on"}})
        self.assertRegex(p["id"], r"^pre_[0-9a-f]{8}$")
        self.assertEqual((p["name"], p["max_tokens"], p["id_slot"], p["effort"], p["kwargs"]),
                         ("Careful", 4096, None, "medium", {"reasoning": "on"}))
        bad = [({"name": "", "model": "m"}, "name"), ({"name": "fast", "model": "m"}, "already"),
               ({"name": "careful", "model": "m"}, "already"), ({"name": "X", "model": ""}, "model"),
               ({"name": "X"}, "model id"), ({"name": "X", "model": "m", "thinking": "yes"}, "thinking"),
               ({"name": "X", "model": "m", "effort": "max"}, "Effort"),
               ({"name": "X", "model": "m", "kwargs": {"enable_thinking": True}}, "Thinking switch"),
               ({"name": "X", "model": "m", "kwargs": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}}, "at most 4"),
               ({"name": "X", "model": "m", "kwargs": {"a": {"x": 1}}}, "values"),
               ({"name": "X", "model": "m", "kwargs": {"bad key": 1}}, "names"),
               ({"name": "X", "model": "m", "timeout": 29}, "timeout"),
               ({"name": "X", "model": "m", "max_tokens": 40000}, "Max tokens"),
               ({"name": "X", "model": "m", "reasoning_budget": True}, "thinking budget"),
               ({"name": "X", "model": "m", "id_slot": 64}, "slot"),
               ({"name": "X", "model": "m", "temperature": 1}, "can't change"),
               ({"name": "X", "model": "m\n"}, "model")]
        for body, word in bad:
            with self.assertRaises(InputError, msg=body) as cm:
                self.store.add_preset(body)
            self.assertIn(word.lower(), str(cm.exception).lower(), body)
        for i in range(3):
            self.store.add_preset({"name": f"P{i}", "model": "m"})
        with self.assertRaisesRegex(InputError, "at most 4"):
            self.store.add_preset({"name": "P9", "model": "m"})
        # the mapping: known kinds and presets only; the urgent check is not a kind
        self.store.update_writer({"call_presets": {"morning": p["id"]}})
        self.assertEqual(self.store.writer_public()["call_presets"]["morning"], p["id"])
        for m in ({"urgent": "fast"}, {"morning": "pre_00000000"}, ["fast"]):
            with self.assertRaises(InputError):
                self.store.update_writer({"call_presets": m})
        with self.assertRaisesRegex(InputError, "used for Morning brief"):
            self.store.delete_preset(p["id"])
        with self.assertRaisesRegex(InputError, "can't be deleted"):
            self.store.delete_preset("fast")
        with self.assertRaises(NotFound):
            self.store.delete_preset("pre_00000000")
        self.store.update_writer({"call_presets": {"morning": "fast"}})
        self.store.delete_preset(p["id"])
        self.assertNotIn(p["id"], [x["id"] for x in self.store.writer_public()["presets"]])
        with self.assertRaises(InputError):
            self.store.update_writer({"reload_fast": "no"})
        self.store.update_writer({"reload_fast": False})
        self.assertFalse(self.store.writer_public()["reload_fast"])

    def test_fast_edits_are_the_local_settings_and_a_deleted_preset_reads_as_fast(self):
        self.store.update_writer({"local": {"model": FAST_MODEL}})
        self.store.update_preset("fast", {"timeout": 600, "effort": "low", "kwargs": {"x": True}})
        loc = self.store.writer_public()["local"]
        self.assertEqual((loc["timeout"], loc["model"]), (600, FAST_MODEL))
        f = self.store.writer_public()["presets"][0]
        self.assertEqual((f["effort"], f["kwargs"]), ("low", {"x": True}))
        with self.assertRaisesRegex(InputError, "name can't be changed"):
            self.store.update_preset("fast", {"name": "Quick"})
        p = self.store.add_preset({"name": "Careful", "model": SWIFT})
        with self.assertRaisesRegex(InputError, "already"):
            self.store.update_preset(p["id"], {"name": "FAST"})
        self.store.update_preset(p["id"], {"name": "Slow", "thinking": True, "id_slot": 2})
        self.assertEqual(self.store.writer_public()["presets"][1]["name"], "Slow")
        # a mapping to a preset that disappeared outside the API reads as Fast
        w = self.store.writer_config()
        w["call_presets"] = {"morning": "pre_deadbeef"}
        self.store._set_meta("writer", w)
        self.assertEqual(self.store.writer_public()["call_presets"]["morning"], "fast")


class BodyTest(unittest.TestCase):
    def test_effort_and_kwargs_only_when_set(self):
        base = writer.local_body("m", "p", ["r1"], writer.local_options({}))
        self.assertNotIn("reasoning_effort", base)
        self.assertEqual(base["chat_template_kwargs"], {"enable_thinking": False})
        swift = presets.options({}, dict(presets.SUGGESTION, id="pre_00000001", id_slot=None))
        b = writer.local_body(SWIFT, "p", ["r1"], swift)
        self.assertEqual((b["max_tokens"], b["reasoning_budget_tokens"], b["chat_template_kwargs"]),
                         (6144, 2048, {"enable_thinking": True}))
        self.assertNotIn("reasoning_effort", b)
        self.assertEqual(swift["timeout"], 900)
        o = dict(swift, effort="high", kwargs={"enable_thinking": False, "x": 1})
        b = writer.local_body(SWIFT, "p", ["r1"], o)
        self.assertEqual((b["reasoning_effort"], b["chat_template_kwargs"]), ("high", {"enable_thinking": True, "x": 1}))
        b = writer.local_body(SWIFT, "p", ["r1"], dict(o, thinking=False))
        self.assertNotIn("reasoning_effort", b)  # effort only with thinking on
        self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False, "x": 1})
        self.assertEqual(presets.reload_body(FAST_MODEL, {"id_slot": 1}),
                         {"model": FAST_MODEL, "stream": False, "max_tokens": 8,
                          "chat_template_kwargs": {"enable_thinking": False},
                          "messages": [{"role": "user", "content": "Reply with OK."}], "id_slot": 1})


# --- API: presets and the model check ----------------------------------------------------------------
class ApiTest(PresetBase):
    def test_one_click_suggestion_checked_against_the_server(self):
        self.local_on()
        r = self.ok("POST", "/api/writer/presets", {"suggestion": "careful"})
        p = next(x for x in r["presets"] if x["id"] == r["preset_id"])
        self.assertEqual({k: p[k] for k in ("name", "model", "thinking", "reasoning_budget", "max_tokens", "timeout")},
                         {"name": "Careful", "model": SWIFT, "thinking": True, "reasoning_budget": 2048,
                          "max_tokens": 4096, "timeout": 900})
        self.assertIsNone(r["model_check"])
        self.assertEqual(r["call_presets"]["morning"], "fast")  # adding a preset maps nothing
        self.assertEqual([q["path"] for q in self.loc.requests if q["method"] == "GET"], ["/v1/models"])
        # the server doesn't list it: refused, nothing saved
        st, body, _ = self.call("POST", "/api/writer/presets", {"suggestion": "careful", "name": "Careful 2",
                                                                "model": "Swift-1.5-typo"})
        self.assertEqual(st, 400)
        self.assertIn("doesn't list Swift-1.5-typo", body["error"])
        self.assertEqual(len(self.ok("GET", "/api/writer")["presets"]), 2)
        st, body, _ = self.call("PATCH", f"/api/writer/presets/{r['preset_id']}", {"model": "nope"})
        self.assertEqual(st, 400)
        self.assertEqual(self.ok("GET", "/api/writer/models"), {"models": [FAST_MODEL, SWIFT], "error": None})
        st, _, _ = self.call("POST", "/api/writer/presets", {"suggestion": "other"})
        self.assertEqual(st, 400)
        self.assertEqual(self.call("PATCH", "/api/writer/presets/x1", {})[0], 404)

    def test_unreachable_server_saves_with_a_sentence(self):
        self.ok("PATCH", "/api/writer", {"local": {"base_url": "http://127.0.0.1:9/v1", "model": FAST_MODEL}})
        r = self.ok("POST", "/api/writer/presets", {"suggestion": "careful"})
        self.assertTrue(r["model_check"].startswith("Couldn't check the model on the server:"))
        self.assertEqual(self.ok("GET", "/api/writer/models")["models"], [])
        self.ok("DELETE", f"/api/writer/presets/{r['preset_id']}")


# --- the run flow ------------------------------------------------------------------------------------
class RunTest(PresetBase):
    def test_default_mapping_sends_exactly_the_fast_request(self):
        self.seed()
        self.local_on()
        self.app.syncer.run_now(kind="morning")
        bodies = self.chats()
        self.assertEqual(len(bodies), 1)  # no reload request, no other model
        self.assertEqual(bodies[0]["model"], FAST_MODEL)
        self.assertEqual((bodies[0]["max_tokens"], bodies[0]["chat_template_kwargs"]), (4096, {"enable_thinking": False}))
        for k in ("reasoning_effort", "reasoning_budget_tokens", "id_slot"):
            self.assertNotIn(k, bodies[0])
        self.assertEqual(self.brief()["summary"]["text"], f"Summary by {FAST_MODEL}.")
        self.assertIn("Callsheet writer: preset fast for morning, model change unknown", "\n".join(self.logs))
        self.assertIsNone(self.app.syncer.writing)

    def test_careful_morning_swaps_then_loads_fast_again(self):
        self.seed()
        self.local_on()
        pid = self.careful(morning=1)
        self.loc.swap_s = 0.3
        with mock.patch.object(sync, "guard_claims", wraps=sync.guard_claims) as guard:
            self.app.syncer.run_now(kind="morning")
        guard.assert_called_once()  # 0.5.3's post-check runs on the careful output too
        bodies = self.chats()
        self.assertEqual([b["model"] for b in bodies], [SWIFT, FAST_MODEL])
        b = bodies[0]
        self.assertEqual((b["max_tokens"], b["reasoning_budget_tokens"], b["chat_template_kwargs"]),
                         (6144, 2048, {"enable_thinking": True}))
        self.assertEqual(b["messages"][0]["content"].count("Safety rules (fixed"), 1)  # the same fixed prompt
        self.assertEqual(bodies[1]["max_tokens"], 8)
        self.assertEqual(self.loc.swaps, [(FAST_MODEL, SWIFT), (SWIFT, FAST_MODEL)])
        br = self.brief()
        self.assertEqual((br["summary"]["text"], br["summary"]["by"]), (f"Summary by {SWIFT}.", "Local · " + SWIFT))
        self.assertNotIn("writing", br)
        logs = "\n".join(self.logs)
        self.assertIn(f"Callsheet writer: preset {pid} for morning, model change unknown", logs)
        self.assertRegex(logs, r"swap and queue about \d+\.\ds before the server worked 1\.0s")
        self.assertRegex(logs, r"fast model loaded again, \d+\.\ds")
        for word in ("Priya", "Budget", "sign off", "Lunch", "Thanks"):
            self.assertNotIn(word, logs)  # counts only
        # the next check-in uses Fast, which is loaded: no swap, model change "no"
        self.app.syncer.run_now(kind="midday")
        self.assertEqual(self.chats()[-1]["model"], FAST_MODEL)
        self.assertIn("preset fast for midday, model change no", "\n".join(self.logs))
        self.assertEqual(len(self.loc.swaps), 2)

    def test_loading_503_while_swapping_is_retried(self):
        self.seed()
        self.local_on()
        self.careful(morning=1)
        self.loc.loading_503 = True
        self.app.syncer.run_now(kind="morning")
        models = [b["model"] for b in self.chats()]
        self.assertEqual(models, [SWIFT, SWIFT, FAST_MODEL, FAST_MODEL])  # 503, retry, reload 503, reload retry
        self.assertEqual(self.brief()["summary"]["by"], "Local · " + SWIFT)
        self.assertIn("was loading the model; trying again", "\n".join(self.logs))

    def _fast_timeout(self, careful_timeout=1):
        real = presets.options
        return mock.patch.object(presets, "options", lambda loc, p: dict(
            real(loc, p), timeout=careful_timeout if p["id"] != "fast" else 30))

    def test_timeout_falls_back_once_to_fast_never_to_claude(self):
        self.seed()
        self.local_on()
        self.app.store.update_writer({"claude": {"api_key": KEY}, "fallback_to_claude": True})
        self.careful(morning=1)
        self.loc.hang.add(SWIFT)
        with self._fast_timeout(), mock.patch.object(sync, "guard_claims", wraps=sync.guard_claims) as guard:
            self.app.syncer.run_now(kind="morning")
        guard.assert_called_once()
        br = self.brief()
        self.assertEqual(br["summary"]["by"], "Local · " + FAST_MODEL)
        self.assertIn(presets.FELL_BACK_TIMEOUT, br["heads_up"])
        self.assertEqual([b["model"] for b in self.chats()], [SWIFT, FAST_MODEL])  # Fast is loaded: no reload
        self.assertEqual(self.ant.requests, [])
        self.assertIn("fell back to fast", "\n".join(self.logs))
        self.assertTrue(self.ok("GET", "/api/state")["last_writer"]["ok"])

    def test_failure_falls_back_and_both_failing_says_so(self):
        self.seed()
        self.local_on()
        self.app.store.update_writer({"claude": {"api_key": KEY}, "fallback_to_claude": True})
        self.careful(morning=1)
        self.loc.broken.add(SWIFT)
        self.app.syncer.run_now(kind="morning")
        self.assertIn(presets.FELL_BACK, self.brief()["heads_up"])
        self.loc.broken.add(FAST_MODEL)
        self.app.syncer.run_now(kind="morning")
        br = self.brief()
        line = next(x for x in br["heads_up"] if x.startswith("The AI writer didn't run"))
        self.assertIn("The fast model didn't run either", line)
        self.assertNotIn("summary", br)
        self.assertEqual(self.ant.requests, [])  # a careful run never falls back to the cloud

    def test_fast_failure_keeps_the_owners_claude_fallback_setting(self):
        self.seed()
        self.local_on()
        self.app.store.update_writer({"claude": {"api_key": KEY}, "fallback_to_claude": True})
        self.loc.broken.add(FAST_MODEL)
        self.app.syncer.run_now(kind="midday")
        if HAVE_SDK:
            self.assertEqual(self.brief()["summary"]["by"].startswith("Claude"), True)
        else:  # without the SDK Claude was tried and isn't available in this build
            self.assertTrue(any("Claude didn't run either" in x for x in self.brief()["heads_up"]))

    def test_reload_off(self):
        self.seed()
        self.local_on()
        self.careful(morning=1)
        self.ok("PATCH", "/api/writer", {"reload_fast": False})
        self.app.syncer.run_now(kind="morning")
        self.assertEqual([b["model"] for b in self.chats()], [SWIFT])


class WritingStateTest(PresetBase):
    def test_brief_says_writing_while_the_careful_model_works(self):
        self.seed()
        self.local_on()
        self.careful(morning=1)
        self.loc.swap_s = 1.5
        t = threading.Thread(target=self.app.syncer.run_now, kwargs={"kind": "morning"})
        t.start()
        deadline = time.time() + 5
        while self.app.syncer.writing is None and time.time() < deadline:
            time.sleep(0.02)
        br = self.brief()
        self.assertEqual(br["writing"]["text"], "Writing the summary…")
        self.assertEqual(br["writing"]["preset"], "Careful")
        self.assertEqual(br["writing"]["poll_s"], 10)
        self.assertNotIn("summary", br)
        self.assertEqual([r["id"] for r in br["replies"]], ["r1", "r2"])  # the rules brief is there already
        t0 = time.time()
        self.assertEqual(self.call("POST", "/api/sync")[0], 200)  # a Refresh meanwhile returns at once
        self.assertLess(time.time() - t0, 1.0)
        t.join(10)
        br = self.brief()
        self.assertNotIn("writing", br)
        self.assertEqual(br["summary"]["by"], "Local · " + SWIFT)
        self.assertEqual(len([b for b in self.chats() if b["model"] == SWIFT]), 1)

    def test_refresh_mapped_to_careful_answers_before_the_writer(self):
        self.seed()
        self.local_on()
        self.careful(refresh=1)
        self.loc.swap_s = 1.0
        t0 = time.time()
        self.ok("POST", "/api/sync")
        self.assertLess(time.time() - t0, 1.0)
        self.assertIn("writing", self.brief())
        deadline = time.time() + 10
        while "writing" in self.brief() and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.brief()["summary"]["by"], "Local · " + SWIFT)
        while self.app.syncer.lock.locked() and time.time() < deadline:  # the Fast reload runs after the brief
            time.sleep(0.05)
        self.assertFalse(self.app.syncer.lock.locked())
        self.assertEqual(self.chats()[-1]["max_tokens"], 8)

    def test_writer_off_has_no_writing_state(self):
        self.seed()
        self.ok("POST", "/api/sync")
        self.assertNotIn("writing", self.brief())
        self.assertIsNone(self.app.syncer.writing)
        self.assertFalse(self.app.syncer.lock.locked())


class SchedulerKindTest(unittest.TestCase):
    def test_scheduled_sync_passes_its_call_time_and_startup_is_a_refresh(self):
        import datetime as dt
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        self.addCleanup(store.close)
        store.update_settings({"timezone": "UTC"})
        utc = dt.timezone.utc
        at = lambda h, m: dt.datetime(2026, 11, 3, h, m, tzinfo=utc).timestamp()  # noqa: E731 (a Tuesday)
        kinds = []

        class S:
            def run_now(self, kind=None):
                kinds.append(kind)

        sch = Scheduler(store, S(), clock=lambda: 0)
        self.assertEqual(sch.due_call(at(6, 29), at(6, 30)), "morning")
        self.assertEqual(sch.due_call(at(12, 29), at(12, 31)), "midday")
        self.assertEqual(sch.due_call(at(16, 0), at(17, 0)), "afternoon")
        self.assertIsNone(sch.due_call(at(7, 0), at(7, 1)))
        self.assertTrue(sch.due(at(6, 29), at(6, 30)))
        with mock.patch.object(store, "has_sources", return_value=True):
            sch._sync(scheduled=False)
            sch._sync(scheduled=True, kind="morning")
        self.assertEqual(kinds, [None, "morning"])


class PageTest(unittest.TestCase):
    def test_setup_table_and_writing_line(self):
        for s in ("api/writer/presets", "api/writer/models", "call_presets", "reload_fast", "suggestion: 'careful'",
                  "Never uses the writer", "After a careful run, load the Fast model again", "Writing the summary…",
                  "pollWriting", "B.writing"):
            self.assertIn(s, APP_JS)
        self.assertNotRegex(APP_JS, r"api\('[A-Z]+', '/")  # relative URLs only
        block = APP_JS[APP_JS.index("function presetsBlock"):APP_JS.index("function testWriter")]
        self.assertNotIn("innerHTML", block)
        self.assertIn(".prs{", APP_CSS)
        self.assertIn(".bv .writing{", APP_CSS)


if __name__ == "__main__":
    unittest.main()
