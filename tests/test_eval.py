"""Writer evaluation (0.5.0 contenders) against fakes only: fake Google, a fake OpenAI-compatible server that
answers per model id (and can answer 503 "Loading model") and, for Claude contenders, the fake Anthropic API.
Never contacts the real llama-server or any real API."""
import datetime as dt
import http.client
import json
import os
import stat
import time
import unittest
from unittest import mock

import test_writer  # noqa: F401  (sets sys.path for the app and the fakes)
import evalscore  # noqa: E402
import evaluate  # noqa: E402
import fake_ai  # noqa: E402
import writer  # noqa: E402
from test_writer import HAVE_SDK, KEY, LOCAL_KEY, WriterBase  # noqa: E402

UTC = dt.timezone.utc
G, Q = evaluate.GEMMA_MODEL, evaluate.QWEN_MODEL
G0_OUT = {"summary": "Gemma summary.", "replies": {"r1": "I'll look at the budget by [day].", "r2": "Lunch works."}}
QLOW_OUT = {"summary": "Qwen low summary.", "replies": {"r1": "I've already approved the budget.", "r2": "Sure."}}
QMED_OUT = {"summary": "Qwen medium summary.", "replies": {"r1": "Yes, Thursday works.", "r2": ""}}
# the three setups of the 0.3.3 evaluation, as contenders (compared against Q-med)
LEGACY = [{"name": "G0", "model": G},
          {"name": "Q-low", "model": Q, "thinking": True, "effort": "low", "budget": 2048,
           "temperature": 1.0, "top_p": 0.95, "top_k": 20},
          {"name": "Q-med", "model": Q, "thinking": True, "effort": "medium", "budget": 4096,
           "temperature": 1.0, "top_p": 0.95, "top_k": 20}]
DEFAULTS = {"enabled": False, "mode": "each", "baseline": None, "contenders": []}


def as_040(out, ask, details=()):
    """An answer in the 0.4.0 shape a live brief asks for: areas, top keyed by the rules' refs, gists."""
    gist = {"summary": "They want a decision.", "details": [dict(d) for d in details]}
    return {"summary": out["summary"], "areas": {a: "Area line." for a in ask["areas"]},
            "top": {r: "Top line." for r in ask["refs"]}, "gists": {i: gist for i in out["replies"]},
            "replies": out["replies"]}


class EvalLocal(fake_ai.FakeLocal):
    """Answers per model id: .by_model[model] is a queue of contents (the last one repeats). .loading_for[model]:
    the next N requests for that model get HTTP 503 "Loading model"."""

    def __init__(self):
        super().__init__()
        self.by_model = {}
        self.loading_for = {}

    def answer(self, req):
        body = req["body"] or {}
        model = body.get("model")
        if req["method"] == "POST" and req["path"] == "/v1/chat/completions" and model in self.by_model:
            if self.loading_for.get(model):
                self.loading_for[model] -= 1
                return 503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}, None
            q = self.by_model[model]
            content = q.pop(0) if len(q) > 1 else q[0]
            return 200, {"id": "c1", "object": "chat.completion", "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                                                              "reasoning_content": "thinking " * 10},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 900, "completion_tokens": 120},
                         "timings": {"prompt_n": 880, "cache_n": 20, "predicted_per_second": 31.5}}, None
        if req["method"] == "POST" and req["path"] == "/v1/chat/completions":
            return 404, {"error": {"message": "model not found"}}, None
        return super().answer(req)


class EvalBase(WriterBase):
    def setUp(self):
        super().setUp()
        self.loc.close()
        self.loc = EvalLocal()
        self.loc.models = [G, Q, "other-model"]
        self.loc.by_model = {G: [json.dumps(G0_OUT), "OK"],
                             Q: [json.dumps(QLOW_OUT), json.dumps(QMED_OUT)]}
        sleep = mock.patch.object(writer, "_sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.ev = self.app.evaluator
        self.ev.log = self.logs.append
        today = dt.datetime.now(UTC).date()
        self.at = lambda h, m=0: dt.datetime.combine(today, dt.time(h, m), tzinfo=UTC).timestamp()  # noqa: E731
        self.ev.clock = lambda: self.at(9)  # far from the default call times (06:30, 12:30, 17:00 UTC)
        # every day a workday, so the check-ins count on weekends too (they are skipped on days off)
        self.ok("PATCH", "/api/settings", {"workdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})

    def setup_all(self, enabled=True, contenders=LEGACY, mode="baseline", baseline="Q-med"):
        """Google, the local writer (everyday model G) and, when enabled, the contenders (default: the old
        three setups, compared against Q-med)."""
        self.seed()
        self.ok("PATCH", "/api/writer", {"provider": "local", "local": {"base_url": self.loc.base, "model": G,
                                                                        "api_key": LOCAL_KEY}})
        if not enabled:
            return None
        self.ok("PATCH", "/api/eval/settings", {"enabled": True})
        e = None
        for c in contenders:
            e = self.ok("POST", "/api/eval/contenders", c)
        ids = {c["name"]: c["id"] for c in e["contenders"]} if e else {}
        patch = {"mode": mode}
        if baseline:
            patch["baseline"] = ids[baseline]
        return self.ok("PATCH", "/api/eval/settings", patch)

    def ids(self):
        return {c["name"]: c["id"] for c in self.ok("GET", "/api/state")["eval"]["contenders"]}

    def run_eval(self):
        st = self.ok("POST", "/api/eval/run", {})
        self.assertTrue(st["running"])
        deadline = time.time() + 30
        while self.ev.job is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.ev.job)
        return self.ok("GET", "/api/eval/status")["runs"][0]["id"]

    def run_dir(self, run):
        return self.app.store.dir / "writer-eval" / run

    def info(self, run):
        return json.loads((self.run_dir(run) / "run.json").read_text())

    def raw(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r, body


class ToggleAndRefusalTest(EvalBase):
    def test_off_by_default_and_refused(self):
        self.setup_all(enabled=False)
        self.assertEqual(self.ok("GET", "/api/state")["eval"], DEFAULTS)
        for method, path in (("GET", "/api/eval/status"), ("POST", "/api/eval/run"), ("GET", "/api/eval/models"),
                             ("POST", "/api/eval/contenders"), ("PATCH", "/api/eval/contenders/con_1"),
                             ("DELETE", "/api/eval/contenders/con_1"),
                             ("GET", "/api/eval/20261029T090000Z"), ("GET", "/api/eval/20261029T090000Z/export"),
                             ("POST", "/api/eval/20261029T090000Z/choices")):
            status, data, _ = self.call(method, path, {} if method in ("POST", "PATCH") else None)
            self.assertEqual(status, 404, path)
            self.assertEqual(data["error"], evaluate.OFF)
        self.assertEqual(self.loc.requests, [])
        self.assertFalse((self.app.store.dir / "writer-eval").exists())

    def test_settings_validation(self):
        self.assertEqual(self.ok("PATCH", "/api/eval/settings", {"enabled": True}), dict(DEFAULTS, enabled=True))
        self.assertEqual(self.ok("PATCH", "/api/eval/settings", {"mode": "baseline"})["mode"], "baseline")
        for bad, msg in (({"enabled": "yes"}, "enabled must be true or false."),
                         ({"mode": "all"}, "The mode must be each against each, or against a baseline."),
                         ({"baseline": "con_nope"}, "Pick one of the contenders as the baseline."),
                         ({"gemma_model": "x"}, "The evaluation settings can't change gemma_model."),
                         ({"other": 1}, "The evaluation settings can't change other.")):
            status, data, _ = self.call("PATCH", "/api/eval/settings", bad)
            self.assertEqual((status, data["error"]), (400, msg), bad)
        self.assertEqual(self.call("PATCH", "/api/eval/settings", {"enabled": True}, header=False)[0], 403)

    def test_call_time_window(self):
        self.setup_all()
        for h, m, refused in ((12, 21, True), (12, 30, True), (12, 39, True), (12, 19, False), (12, 41, False),
                              (6, 25, True), (17, 5, True), (16, 49, False)):
            self.ev.clock = lambda h=h, m=m: self.at(h, m)
            reason = self.ok("GET", "/api/eval/status")["reason"]
            status, data, _ = self.call("POST", "/api/eval/run", {}) if refused else (None, None, None)
            if refused:
                self.assertEqual(status, 409, (h, m))
                self.assertIn("too close", data["error"])
                self.assertIn("more than 10 minutes before or after a brief", data["error"])
                self.assertEqual(reason, data["error"])
            else:
                self.assertIsNone(reason, (h, m))
        # a call time that is turned off doesn't count
        st = self.ok("GET", "/api/state")["settings"]
        cts = [dict(c, enabled=c["id"] != "midday") for c in st["call_times"]]
        self.ok("PATCH", "/api/settings", {"call_times": cts})
        self.ev.clock = lambda: self.at(12, 30)
        self.assertIsNone(self.ok("GET", "/api/eval/status")["reason"])
        self.assertEqual(self.loc.chats(), [])

    def test_setup_problems_refuse_the_run(self):
        self.seed()
        self.ok("PATCH", "/api/eval/settings", {"enabled": True})

        def refused(msg):
            status, data, _ = self.call("POST", "/api/eval/run", {})
            self.assertEqual((status, data["error"]), (409, msg))
            self.assertEqual(self.ok("GET", "/api/eval/status")["reason"], msg)
        refused("Add at least two contenders first.")
        self.ok("POST", "/api/eval/contenders", LEGACY[0])
        refused("Add at least two contenders first.")
        self.ok("POST", "/api/eval/contenders", LEGACY[1])
        refused("Add the local model's server address in Setup, Delivery first.")
        # a writing profile deleted after a contender picked it
        self.ok("PATCH", "/api/writer", {"local": {"base_url": self.loc.base}})
        prof = self.ok("POST", "/api/writer/profiles", {"name": "Short"})
        self.ok("PATCH", f"/api/eval/contenders/{self.ids()['G0']}", {"profile": prof["id"]})
        self.ok("DELETE", f"/api/writer/profiles/{prof['id']}")
        refused("The writing profile of G0 was deleted. Pick another one.")
        self.assertEqual(self.loc.chats(), [])

    def test_manual_sync_refused_while_running(self):
        self.setup_all()
        self.ev.job = {"run": "x", "step": 2, "steps": 4, "config": "Q-low", "t0": time.monotonic()}
        try:
            status, data, _ = self.call("POST", "/api/sync", {})
            self.assertEqual((status, data["error"]), (409, "An evaluation is running. Try again when it's finished."))
        finally:
            self.ev.job = None
        self.assertEqual(self.ok("GET", "/api/state")["last_sync"]["at"], None)
        self.assertEqual(self.call("POST", "/api/sync", {})[0], 200)

    def test_concurrency(self):
        self.setup_all()
        self.app.syncer.lock.acquire()
        try:
            status, data, _ = self.call("POST", "/api/eval/run", {})
            self.assertEqual((status, data["error"]), (409, "A sync is running. Try again in a minute."))
        finally:
            self.app.syncer.lock.release()
        self.ev.job = {"run": "x", "step": 1, "steps": 4, "config": "G0", "t0": time.monotonic()}
        try:
            status, data, _ = self.call("POST", "/api/eval/run", {})
            self.assertEqual((status, data["error"]), (409, "An evaluation is already running."))
            prog = self.ok("GET", "/api/eval/status")["progress"]
            self.assertEqual((prog["step"], prog["steps"], prog["config"]), (1, 4, "G0"))
        finally:
            self.ev.job = None
        self.assertEqual(self.loc.chats(), [])


class ContenderTest(EvalBase):
    def setUp(self):
        super().setUp()
        self.setup_all(contenders=[], mode="each", baseline=None)

    def test_add_edit_remove(self):
        e = self.ok("POST", "/api/eval/contenders", {"name": " Gemma plain ", "model": G})
        c = e["contenders"][0]
        self.assertTrue(c["id"].startswith("con_"))
        self.assertEqual({k: v for k, v in c.items() if k != "id"},
                         {"name": "Gemma plain", "provider": "local", "model": G, "thinking": False, "effort": None,
                          "budget": None, "temperature": None, "top_p": None, "top_k": None, "profile": "default"})
        self.assertEqual(e["baseline"], c["id"])  # the first contender is the baseline until the owner picks
        prof = self.ok("POST", "/api/writer/profiles", {"name": "Short"})
        e = self.ok("POST", "/api/eval/contenders", {"name": "Qwen", "model": Q, "thinking": True, "effort": "high",
                                                      "budget": 1024, "temperature": 0.6, "top_p": 0.9, "top_k": 40,
                                                      "profile": prof["id"]})
        q = e["contenders"][1]
        self.assertEqual((q["effort"], q["budget"], q["temperature"], q["top_p"], q["top_k"], q["profile"]),
                         ("high", 1024, 0.6, 0.9, 40, prof["id"]))
        # edit: thinking off clears effort and budget; sampling can be cleared with null
        e = self.ok("PATCH", f"/api/eval/contenders/{q['id']}", {"thinking": False, "temperature": None,
                                                                 "name": "Qwen fast"})
        q2 = e["contenders"][1]
        self.assertEqual((q2["id"], q2["name"], q2["thinking"], q2["effort"], q2["budget"], q2["temperature"],
                          q2["top_p"]), (q["id"], "Qwen fast", False, None, None, None, 0.9))
        self.assertEqual(self.ok("GET", "/api/state")["eval"], e)  # saved for re-runs
        # baseline moves when the baseline is removed
        self.ok("PATCH", "/api/eval/settings", {"mode": "baseline"})
        e = self.ok("DELETE", f"/api/eval/contenders/{c['id']}")
        self.assertEqual(([x["name"] for x in e["contenders"]], e["baseline"], e["mode"]), (["Qwen fast"], q["id"],
                                                                                            "baseline"))
        for method in ("PATCH", "DELETE"):
            status, data, _ = self.call(method, f"/api/eval/contenders/{c['id']}", {} if method == "PATCH" else None)
            self.assertEqual((status, data["error"]), (404, "That contender doesn't exist."))

    def test_at_most_four_and_unique_names(self):
        for i in range(4):
            self.ok("POST", "/api/eval/contenders", {"name": f"C{i}", "model": G})
        status, data, _ = self.call("POST", "/api/eval/contenders", {"name": "C5", "model": G})
        self.assertEqual((status, data["error"]), (400, "There can be at most 4 contenders. Remove one first."))
        ids = self.ids()
        status, data, _ = self.call("PATCH", f"/api/eval/contenders/{ids['C1']}", {"name": "c0"})
        self.assertEqual((status, data["error"]), (400, "Another contender is already called c0."))
        self.assertEqual(self.ok("PATCH", f"/api/eval/contenders/{ids['C1']}", {"name": "C1"})["contenders"][1]["name"],
                         "C1")  # its own name is fine

    def test_validation_messages(self):
        good = {"name": "A", "model": G}
        cases = (
            ({"name": ""}, "Give the contender a name of 1 to 40 characters."),
            ({"name": "x" * 41}, "Give the contender a name of 1 to 40 characters."),
            ({"name": "a\nb"}, "Give the contender a name of 1 to 40 characters."),
            ({"provider": "openai"}, "Pick a local model or Claude."),
            ({"model": ""}, "Pick a model for A."),
            ({"model": "m" * 121}, "The model name must be text of at most 120 characters."),
            ({"model": 5}, "The model name must be text of at most 120 characters."),
            ({"thinking": "yes"}, "Thinking must be on or off."),
            ({"effort": "max"}, "Effort must be low, medium or high."),
            ({"thinking": True, "budget": 100}, "The thinking budget must be a whole number from 256 to 32,768 tokens."),
            ({"thinking": True, "budget": 1.5}, "The thinking budget must be a whole number from 256 to 32,768 tokens."),
            ({"temperature": 3}, "The temperature must be a number from 0 to 2."),
            ({"temperature": True}, "The temperature must be a number from 0 to 2."),
            ({"top_p": 0}, "Top p must be a number above 0 and at most 1."),
            ({"top_k": 0}, "Top k must be a whole number from 1 to 1,000."),
            ({"profile": "prof_nope"}, "That writing profile doesn't exist."),
            ({"provider": "claude", "model": "claude-opus-5-5"}, "Add an Anthropic API key in Setup, Delivery to use Claude."),
            ({"provider": "claude", "model": G}, "Pick one of the Claude models."),
            ({"colour": "red"}, "Contenders can't change colour."),
        )
        for patch, msg in cases:
            status, data, _ = self.call("POST", "/api/eval/contenders", dict(good, **patch))
            self.assertEqual((status, data["error"]), (400, msg), patch)
        self.assertEqual(self.call("POST", "/api/eval/contenders", ["x"])[0], 400)
        self.assertEqual(self.ok("GET", "/api/state")["eval"]["contenders"], [])
        # Claude with a key: no budget and no sampling (the Messages API has none for these models)
        self.ok("PATCH", "/api/writer", {"claude": {"api_key": KEY}})
        claude = {"name": "Opus", "provider": "claude", "model": "claude-opus-5-5"}
        for patch in ({"thinking": True, "budget": 2048}, {"top_k": 5}, {"temperature": 0.5}, {"top_p": 0.9}):
            msg = "Claude doesn't take a thinking budget or sampling settings. Use effort instead."
            status, data, _ = self.call("POST", "/api/eval/contenders", dict(claude, **patch))
            self.assertEqual((status, data["error"]), (400, msg), patch)
        c = self.ok("POST", "/api/eval/contenders", dict(claude, thinking=True, effort="low"))["contenders"][0]
        self.assertEqual((c["provider"], c["thinking"], c["effort"]), ("claude", True, "low"))
        self.assertNotIn(KEY, json.dumps(self.ok("GET", "/api/state")["eval"]))

    def test_models_from_the_server_and_claude(self):
        m = self.ok("GET", "/api/eval/models")
        self.assertEqual((m["local"], m["local_error"], m["everyday"], m["claude"]), ([G, Q, "other-model"], None, G, []))
        self.assertEqual([r["method"] + " " + r["path"] for r in self.loc.requests], ["GET /v1/models"])
        self.assertEqual(self.loc.requests[0]["headers"].get("authorization"), "Bearer " + LOCAL_KEY)
        self.ok("PATCH", "/api/writer", {"claude": {"api_key": KEY}})
        m = self.ok("GET", "/api/eval/models")
        self.assertEqual([x["id"] for x in m["claude"]], list(writer.MODELS))
        self.assertEqual(m["claude"][0]["label"], "Claude Opus 5.5")
        self.loc.models = []
        self.assertEqual(self.ok("GET", "/api/eval/models")["local_error"], "The local model server lists no models.")
        self.ok("PATCH", "/api/writer", {"local": {"base_url": ""}})
        m = self.ok("GET", "/api/eval/models")
        self.assertEqual(m["local_error"], "Add the local model's server address in Setup, Delivery to pick a local model.")
        self.assertNotIn(KEY, json.dumps(m))


class MigrationTest(EvalBase):
    def test_old_settings_become_the_three_setups(self):
        self.setup_all(enabled=False)
        self.app.store._set_meta("eval", {"enabled": True, "gemma_model": "g-x", "qwen_model": "q-x"})
        e = self.ok("GET", "/api/state")["eval"]
        self.assertEqual((e["enabled"], e["mode"], e["baseline"]), (True, "baseline", "legacy-q-med"))
        self.assertEqual([(c["name"], c["model"], c["thinking"], c["effort"], c["budget"], c["top_k"])
                          for c in e["contenders"]],
                         [("G0", "g-x", False, None, None, None), ("Q-low", "q-x", True, "low", 2048, 20),
                          ("Q-med", "q-x", True, "medium", 4096, 20)])
        # editable like any contender; the change is saved in the new shape
        e = self.ok("PATCH", "/api/eval/contenders/legacy-g0", {"model": G})
        saved = self.app.store._get_meta("eval")
        self.assertNotIn("gemma_model", saved)
        self.assertEqual(saved["contenders"][0]["model"], G)
        self.assertEqual(self.ok("GET", "/api/state")["eval"], e)
        # a 0.3.4 database's default value migrates too
        self.app.store._set_meta("eval", {"enabled": False, "gemma_model": G, "qwen_model": Q})
        self.assertEqual(len(self.ok("GET", "/api/state")["eval"]["contenders"]), 3)

    def old_run(self, run="20261029T235959Z"):
        """A run written by 0.3.3 to 0.4.x: fixed setups, lines keyed by "config", no contenders in run.json."""
        d = self.app.store.dir / "writer-eval" / run
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"id": run, "started": "2026-10-29T23:59:59+00:00", "status": "done",
                                                "errors": [], "seed": 7, "models": {"gemma": G, "qwen": Q},
                                                "ids": ["r1"]}))
        (d / "payload.txt").write_text("<data>old</data>")
        (d / "data.json").write_text(json.dumps({"replies": [{"id": "r1", "who": "Ann", "subject": "Hi",
                                                              "suggest": True}]}))
        outs = {"G0": "Old gemma.", "Q-low": "Old low.", "Q-med": "Old med."}
        (d / "responses.jsonl").write_text("".join(json.dumps(
            {"config": k, "model": G if k == "G0" else Q, "request": {"model": "m"}, "response": {},
             "metrics": {"schema_valid": True, "wall_s": 1.0, "empties": 0, "flagged": 0, "error": None},
             "parsed": {"summary": v, "replies": {"r1": v}}, "error": None}) + "\n" for k, v in outs.items()))
        return run

    def test_old_runs_stay_readable_and_downloadable(self):
        self.setup_all()
        run = self.old_run()
        st = self.ok("GET", "/api/eval/status")
        self.assertEqual((st["runs"][0]["id"], st["runs"][0]["contenders"]), (run, 3))
        v = self.ok("GET", f"/api/eval/{run}")
        self.assertEqual((len(v["pairs"]), v["revealed"]), (2, False))
        self.assertNotIn("G0", json.dumps(v))
        orders = evaluate.pair_orders(7)  # the same A/B order as before 0.5.0
        summ = {"G0": "Old gemma.", "Q-low": "Old low.", "Q-med": "Old med."}
        for p, (a, b) in zip(v["pairs"], orders):
            self.assertEqual((p["a"]["summary"], p["b"]["summary"]), (summ[a], summ[b]))
        out = self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "same"}})
        self.assertEqual([(p["a_name"], p["b_name"], p["pick"]) for p in out["pairs"]],
                         [orders[0] + ("A",), orders[1] + ("same",)])
        self.assertEqual([r["name"] for r in out["results"]], ["G0", "Q-low", "Q-med"])
        self.assertEqual(out["baseline"], "Q-med")
        r, body = self.raw(f"/api/eval/{run}/export")
        self.assertEqual(r.status, 200)
        lines = [json.loads(x) for x in body.splitlines()]
        self.assertEqual([x["type"] for x in lines], ["payload", "config", "config", "config", "choices"])
        self.assertEqual((lines[0]["payload"], lines[1]["config"], lines[1]["summary"]),
                         ("<data>old</data>", "G0", "Old gemma."))
        self.assertEqual(lines[-1]["picks"]["p1"]["better"], orders[0][0])


class RunTest(EvalBase):
    def snapshot(self):
        st = self.app.store
        with st.lock:
            meta = {r["key"]: r["value"] for r in st.db.execute("SELECT key, value FROM meta")}
            accs = [tuple(r) for r in st.db.execute("SELECT id, status, error FROM accounts")]
        return meta, accs, self.ok("GET", "/api/brief")

    def test_run_end_to_end(self):
        self.setup_all()
        self.sync_brief()
        live = self.loc.chats()[-1]["body"]  # what the live brief sent (the 0.4.0 shape)
        sent = live["response_format"]["json_schema"]["schema"]
        ask = writer.make_ask(sent["properties"]["areas"]["required"], sent["properties"]["top"]["required"])
        self.assertTrue(ask["areas"] and ask["refs"])
        details = ({"label": "Deadline", "value": "Thursday"}, {"label": "Time", "value": "Friday 9:00"})
        self.loc.requests.clear()
        self.loc.by_model = {G: [json.dumps(as_040(G0_OUT, ask, details)), "OK"],
                             Q: [json.dumps(as_040(QLOW_OUT, ask)), json.dumps(as_040(QMED_OUT, ask))]}
        self.loc.loading_for = {Q: 1}  # the server swaps models: one 503 "Loading model", then it answers
        before = self.snapshot()
        n_google = len(self.fake.requests)
        run = self.run_eval()

        # live brief, last sync, last writer, account status, the evaluation settings: unchanged
        self.assertEqual(self.snapshot(), before)
        google = self.fake.requests[n_google:]
        self.assertTrue(google)
        self.assertTrue(all(m == "GET" for m, p, q in google if not p.startswith("/token")))

        # order: G0, Q-low (503, retry), Q-med, then the everyday model (G) again; bodies per contender
        chats = self.loc.chats()
        self.assertEqual([c["body"]["model"] for c in chats], [G, Q, Q, Q, G])
        g0, qlow, qmed, reload = chats[0]["body"], chats[2]["body"], chats[3]["body"], chats[4]["body"]
        self.assertEqual(chats[1]["body"], qlow)
        # exactly what a live brief sends: the same system message, JSON schema and payload
        self.assertEqual(g0["messages"], live["messages"])
        self.assertEqual(g0["response_format"], live["response_format"])
        self.assertTrue(g0["messages"][0]["content"].endswith(writer.LOCAL_RULE))
        self.assertEqual(set(sent["properties"]), {"summary", "areas", "top", "gists", "replies"})
        self.assertEqual(g0["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(g0["max_tokens"], 4096)
        for k in ("reasoning_effort", "reasoning_budget_tokens", "temperature", "top_p", "top_k"):
            self.assertNotIn(k, g0)
        for body, effort, budget in ((qlow, "low", 2048), (qmed, "medium", 4096)):
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})
            self.assertEqual((body["reasoning_effort"], body["reasoning_budget_tokens"], body["max_tokens"]),
                             (effort, budget, budget + 4096))
            self.assertEqual((body["temperature"], body["top_p"], body["top_k"]), (1.0, 0.95, 20))
            self.assertEqual(body["messages"], g0["messages"])  # the same payload and profile
            self.assertEqual(body["response_format"], g0["response_format"])
        self.assertEqual((reload["model"], reload["max_tokens"], len(reload["messages"])), (G, 8, 1))
        self.assertFalse(any("id_slot" in c["body"] for c in chats))  # no server slot set: never sent
        self.assertTrue(all(c["headers"].get("authorization") == "Bearer " + LOCAL_KEY for c in chats))
        q = evaluate.legacy_contenders()[1]
        self.assertEqual(evaluate.contender_body(q, "p", [], writer.local_options({}))[1]["timeout"], 450)

        # storage
        d = self.run_dir(run)
        self.assertEqual(stat.S_IMODE(os.stat(d.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["data.json", "metrics.json", "payload.txt", "responses.jsonl", "run.json"])
        for p in d.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p.name)
        payload = (d / "payload.txt").read_text()
        self.assertEqual(g0["messages"][1]["content"], payload)
        self.assertIn("Can you sign off the budget by Thursday?", payload)
        lines = [json.loads(x) for x in (d / "responses.jsonl").read_text().splitlines()]
        self.assertEqual([(x["contender"], x["config"], x["model"]) for x in lines],
                         [("c1", "G0", G), ("c2", "Q-low", Q), ("c3", "Q-med", Q)])
        self.assertTrue(all("messages" not in x["request"] and x["response"]["choices"] for x in lines))
        self.assertNotIn(LOCAL_KEY, "".join(p.read_text() for p in d.iterdir()))
        info = self.info(run)
        self.assertEqual((info["status"], info["errors"], info["ids"], info["mode"], info["baseline"]),
                         ("done", [], ["r1", "r2"], "baseline", "c3"))
        self.assertEqual(info["pairs"], [["c1", "c3"], ["c2", "c3"]])
        self.assertEqual([c["name"] for c in info["contenders"]], ["G0", "Q-low", "Q-med"])

        # metrics
        m = json.loads((d / "metrics.json").read_text())
        self.assertEqual(set(m), {"G0", "Q-low", "Q-med"})
        g, lo, me = m["G0"], m["Q-low"], m["Q-med"]
        self.assertTrue(g["valid_json"] and g["schema_valid"])
        n_areas, n_top = len(ask["areas"]), len(ask["refs"])
        self.assertEqual({k: g[k] for k in ("areas_expected", "areas_written", "top_expected", "top_written",
                                             "gists_expected", "gists_written", "details_returned", "details_kept",
                                             "details_dropped")},
                         {"areas_expected": n_areas, "areas_written": n_areas, "top_expected": n_top,
                          "top_written": n_top, "gists_expected": 2, "gists_written": 2, "details_returned": 4,
                          "details_kept": 1, "details_dropped": 3})  # "Thursday" only in r1's thread
        self.assertEqual((g["finish_reason"], g["prompt_tokens"], g["completion_tokens"]), ("stop", 900, 120))
        self.assertEqual((g["prompt_n"], g["cache_n"], g["predicted_per_second"], g["reasoning_chars"]),
                         (880, 20, 31.5, 90))
        self.assertEqual((g["ids_expected"], g["ids_returned"], g["empties"], g["flagged"]), (2, 2, 0, 0))
        self.assertEqual((g["placeholder_used"], g["placeholder_needed"], g["invented_when_needed"]), (1, 1, 0))
        self.assertEqual((lo["flagged"], lo["flagged_ids"], lo["commitment_ids"]), (1, ["r1"], ["r1"]))
        self.assertFalse(me["schema_valid"])  # r2 empty: minLength 1
        self.assertEqual((me["ids_returned"], me["empties"], me["invented_when_needed"]), (1, 1, 1))

        # status and logs: counts only
        st = self.ok("GET", "/api/eval/status")
        self.assertEqual((st["running"], st["progress"], st["reason"]), (False, None, None))
        self.assertEqual(st["runs"][0]["status"], "done")
        logs = "\n".join(self.logs)
        for secret in ("Priya", "budget", "Gemma summary", "Lunch", LOCAL_KEY, "Q-low", "G0"):
            self.assertNotIn(secret, logs)

    def test_each_contender_uses_its_profile_model_and_thinking(self):
        prof = self.ok("POST", "/api/writer/profiles", {"name": "Pirate"})
        self.ok("PATCH", f"/api/writer/profiles/{prof['id']}", {"drafts": "Write every draft like a pirate."})
        self.setup_all(contenders=[
            {"name": "Plain", "model": G},
            {"name": "Pirate", "model": G, "profile": prof["id"], "thinking": True, "effort": "high", "budget": 1024,
             "temperature": 0.3},
            {"name": "Other", "model": "other-model", "top_p": 0.8}], mode="each", baseline=None)
        self.loc.by_model["other-model"] = [json.dumps(G0_OUT)]
        run = self.run_eval()
        chats = [c["body"] for c in self.loc.chats()]
        self.assertEqual([b["model"] for b in chats], [G, G, "other-model", G])  # then the everyday model again
        plain, pirate, other = chats[:3]
        self.assertNotIn("like a pirate", plain["messages"][0]["content"])
        self.assertIn("Suggested replies:\nWrite every draft like a pirate.", pirate["messages"][0]["content"])
        self.assertNotIn("like a pirate", other["messages"][0]["content"])
        self.assertEqual(plain["messages"][1], pirate["messages"][1])  # the same payload
        self.assertEqual((plain["chat_template_kwargs"], pirate["chat_template_kwargs"]),
                         ({"enable_thinking": False}, {"enable_thinking": True}))
        self.assertEqual((pirate["reasoning_effort"], pirate["reasoning_budget_tokens"], pirate["max_tokens"],
                          pirate["temperature"]), ("high", 1024, 1024 + 4096, 0.3))
        self.assertNotIn("top_p", pirate)
        self.assertEqual((other["top_p"], other["max_tokens"]), (0.8, 4096))
        self.assertNotIn("temperature", other)
        info = self.info(run)
        self.assertEqual(info["pairs"], [["c1", "c2"], ["c1", "c3"], ["c2", "c3"]])
        self.assertEqual(info["contenders"][1]["profile_name"], "Pirate")
        # the live writer's active profile is untouched
        self.assertEqual(self.ok("GET", "/api/state")["writer"]["active_profile"], "default")

    def test_no_reload_when_only_the_everyday_model_ran(self):
        self.setup_all(contenders=[{"name": "A", "model": G}, {"name": "B", "model": G, "thinking": True}],
                       mode="baseline", baseline="A")
        run = self.run_eval()
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G, G])
        b = self.loc.chats()[1]["body"]
        self.assertEqual(b["reasoning_budget_tokens"], writer.LOCAL_DEFAULTS["reasoning_budget"])  # the owner's
        self.assertNotIn("reasoning_effort", b)
        self.assertEqual(self.info(run)["pairs"], [["c2", "c1"]])

    def test_pairings_in_both_modes(self):
        four = [{"name": n, "model": G} for n in ("W", "X", "Y", "Z")]
        self.setup_all(contenders=four, mode="each", baseline=None)
        run = self.run_eval()
        v = self.ok("GET", f"/api/eval/{run}")
        self.assertEqual(len(v["pairs"]), 6)
        self.assertEqual(self.info(run)["pairs"], [["c1", "c2"], ["c1", "c3"], ["c1", "c4"], ["c2", "c3"],
                                                   ["c2", "c4"], ["c3", "c4"]])
        self.ok("PATCH", "/api/eval/settings", {"mode": "baseline", "baseline": self.ids()["Y"]})
        self.ev.clock = lambda: self.at(9, 1)
        run2 = self.run_eval()
        self.assertNotEqual(run2, run)
        self.assertEqual(self.info(run2)["pairs"], [["c1", "c3"], ["c2", "c3"], ["c4", "c3"]])
        v = self.ok("GET", f"/api/eval/{run2}")
        self.assertEqual(len(v["pairs"]), 3)
        picks = {"p1": "A", "p2": "B", "p3": "same"}
        out = self.ok("POST", f"/api/eval/{run2}/choices", {"picks": picks})
        self.assertEqual(out["baseline"], "Y")
        orders = evaluate.pair_orders(self.info(run2)["seed"], [("c1", "c3"), ("c2", "c3"), ("c4", "c3")])
        name = {"c1": "W", "c2": "X", "c3": "Y", "c4": "Z"}
        want = {n: {"wins": 0, "losses": 0, "ties": 0} for n in "WXYZ"}
        for (a, b), pick in zip(orders, ("A", "B", "same")):
            if pick == "same":
                want[name[a]]["ties"] += 1
                want[name[b]]["ties"] += 1
            else:
                win, lose = (a, b) if pick == "A" else (b, a)
                want[name[win]]["wins"] += 1
                want[name[lose]]["losses"] += 1
        got = {r["name"]: {k: r[k] for k in ("wins", "losses", "ties")} for r in out["results"]}
        self.assertEqual(got, want)
        self.assertEqual(sum(r["wins"] + r["losses"] + r["ties"] for r in out["results"]), 6)

    def test_blind_view_and_choices(self):
        self.setup_all()
        run = self.run_eval()
        v = self.ok("GET", f"/api/eval/{run}")
        text = json.dumps(v)
        for hidden in ("G0", "Q-low", "Q-med", G, Q, "config", "metrics", "valid_json", "results", "c1", "legacy"):
            self.assertNotIn(hidden, text)
        self.assertFalse(v["revealed"])
        self.assertEqual([p["id"] for p in v["pairs"]], ["p1", "p2"])
        self.assertEqual(v["rows"], [{"id": "r1", "who": "Priya N", "subject": "Budget"},
                                     {"id": "r2", "who": "Bob", "subject": "Lunch plan?"}])
        orders = evaluate.pair_orders(self.info(run)["seed"], [("c1", "c3"), ("c2", "c3")])
        summ = {"c1": "Gemma summary.", "c2": "Qwen low summary.", "c3": "Qwen medium summary."}
        name = {"c1": "G0", "c2": "Q-low", "c3": "Q-med"}
        for p, (a, b) in zip(v["pairs"], orders):
            self.assertEqual((p["a"]["summary"], p["b"]["summary"]), (summ[a], summ[b]))
        self.assertEqual(self.ok("GET", f"/api/eval/{run}"), v)  # stable per run

        self.assertEqual(self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A"}})[0], 400)
        self.assertEqual(self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "C"}})[0], 400)
        out = self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "same"}})
        self.assertTrue(out["revealed"])
        self.assertEqual([(p["a_name"], p["b_name"], p["pick"]) for p in out["pairs"]],
                         [(name[orders[0][0]], name[orders[0][1]], "A"), (name[orders[1][0]], name[orders[1][1]], "same")])
        res = {r["name"]: r for r in out["results"]}
        self.assertEqual(list(res), ["G0", "Q-low", "Q-med"])
        self.assertEqual((res["G0"]["ok"], res["G0"]["schema_valid"], res["G0"]["prompt_tokens"],
                          res["G0"]["completion_tokens"], res["G0"]["empties"], res["G0"]["flagged"]),
                         (True, False, 900, 120, 0, 0))  # the 0.3.x-shaped fake answer misses areas/top/gists
        self.assertEqual((res["Q-low"]["flagged"], res["Q-med"]["empties"]), (1, 1))
        self.assertEqual((res["Q-med"]["thinking"], res["Q-med"]["effort"], res["Q-med"]["budget"], res["Q-med"]["profile"]),
                         (True, "medium", 4096, "Default"))
        self.assertIsInstance(res["G0"]["wall_s"], float)
        self.assertEqual(out["mode"], "baseline")
        status, data, _ = self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "B", "p2": "B"}})
        self.assertEqual((status, data["error"]), (409, "Choices for this run were already submitted."))
        ch = self.run_dir(run) / "choices.json"
        self.assertEqual(stat.S_IMODE(os.stat(ch).st_mode), 0o600)
        self.assertEqual(json.loads(ch.read_text())["picks"]["p1"]["better"], name[orders[0][0]])

    def test_export(self):
        self.setup_all()
        run = self.run_eval()
        r, body = self.raw(f"/api/eval/{run}/export")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/x-ndjson; charset=utf-8")
        self.assertEqual(r.getheader("Content-Disposition"),
                         f'attachment; filename="callsheet-writer-eval-{run}.jsonl"')
        lines = [json.loads(x) for x in body.splitlines()]
        self.assertEqual([x["type"] for x in lines], ["payload", "contender", "contender", "contender"])
        self.assertEqual(lines[0]["payload"], (self.run_dir(run) / "payload.txt").read_text())
        self.assertEqual((lines[0]["mode"], lines[0]["baseline"], lines[0]["pairs"]),
                         ("baseline", "Q-med", [["G0", "Q-med"], ["Q-low", "Q-med"]]))
        g0, qlow, qmed = lines[1:]
        self.assertEqual((g0["contender"], g0["provider"], g0["model"], g0["thinking"], g0["profile"]),
                         ("G0", "local", G, False, "Default"))
        self.assertEqual(g0["prompts"]["drafts"], writer.DEFAULT_PROMPTS["drafts"])
        self.assertEqual((g0["summary"], g0["drafts"]), ("Gemma summary.", G0_OUT["replies"]))
        self.assertEqual(qmed["drafts"], {"r1": "Yes, Thursday works."})
        self.assertEqual((qlow["metrics"]["flagged_ids"], qlow["request"]["reasoning_effort"]), (["r1"], "low"))
        self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "B", "p2": "A"}})
        lines = [json.loads(x) for x in self.raw(f"/api/eval/{run}/export")[1].splitlines()]
        self.assertEqual(lines[-1]["type"], "choices")
        self.assertEqual(lines[-1]["picks"]["p1"]["pick"], "B")
        self.assertEqual(sum(sum(t.values()) for t in lines[-1]["tally"].values()), 4)
        self.assertEqual(set(lines[-1]["tally"]), {"G0", "Q-low", "Q-med"})
        self.assertEqual(self.raw("/api/eval/20200101T000000Z/export")[0].status, 404)
        self.assertEqual(self.raw("/api/eval/..%2f..%2fx/export")[0].status, 404)

    def test_editing_contenders_later_keeps_the_run(self):
        self.setup_all()
        run = self.run_eval()
        before = self.raw(f"/api/eval/{run}/export")[1]
        for cid in self.ids().values():
            self.ok("DELETE", f"/api/eval/contenders/{cid}")
        self.assertEqual(self.raw(f"/api/eval/{run}/export")[1], before)
        self.assertEqual(len(self.ok("GET", f"/api/eval/{run}")["pairs"]), 2)

    def test_retention_keeps_five(self):
        self.setup_all()
        root = self.app.store.dir / "writer-eval"
        root.mkdir(mode=0o700)
        for i in range(6):
            (root / f"2020010{i + 1}T000000Z").mkdir()
        (root / "notes").mkdir()  # not a run: never touched
        run = self.run_eval()
        runs = sorted(p.name for p in root.iterdir())
        self.assertEqual(runs, ["20200103T000000Z", "20200104T000000Z", "20200105T000000Z", "20200106T000000Z",
                                run, "notes"])
        self.assertEqual(self.ok("GET", "/api/eval/status")["runs"][1]["status"], "unreadable")

    def test_server_slot_in_every_eval_body(self):
        opts = writer.local_options({"id_slot": 7})
        for c in evaluate.legacy_contenders():
            self.assertEqual(evaluate.contender_body(c, "p", ["r1"], opts)[0]["id_slot"], 7, c["name"])
            self.assertNotIn("id_slot", evaluate.contender_body(c, "p", ["r1"], writer.local_options({}))[0])
        self.assertEqual(evaluate.reload_body(G, opts)["id_slot"], 7)
        self.assertNotIn("id_slot", evaluate.reload_body(G, writer.local_options({})))

    def test_failures_are_plain_and_reload_still_runs(self):
        self.setup_all()
        self.loc.by_model = {G: ["not json", "OK"]}  # the Qwen model isn't known to this server: 404
        run = self.run_eval()
        info = self.info(run)
        self.assertEqual(info["status"], "done")
        self.assertEqual(info["errors"], ["Q-low: The local model server doesn't know that address or model.",
                                          "Q-med: The local model server doesn't know that address or model."])
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G, Q, Q, G])
        m = json.loads((self.run_dir(run) / "metrics.json").read_text())
        self.assertFalse(m["G0"]["valid_json"])
        self.assertEqual(m["Q-low"]["error"], "The local model server doesn't know that address or model.")
        st = self.ok("GET", "/api/eval/status")
        self.assertEqual(st["last"]["errors"], info["errors"])
        out = self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "A"}})
        res = {r["name"]: r for r in out["results"]}
        self.assertEqual((res["G0"]["ok"], res["Q-low"]["ok"], res["Q-low"]["error"]),
                         (True, False, "The local model server doesn't know that address or model."))

    def test_stops_before_a_contender_when_a_brief_is_near(self):
        self.setup_all()
        # a brief comes near once G0 has answered
        self.ev.clock = lambda: self.at(12, 25) if self.loc.chats() else self.at(9)
        run = self.run_eval()
        info = self.info(run)
        self.assertEqual(info["status"], "stopped")
        self.assertEqual(info["errors"][0], "Stopped before Q-low: the Midday check-in at 12:30 is too close.")
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G])  # only G ran, so no reload either
        v = self.ok("GET", f"/api/eval/{run}")  # one side of G0 vs Q-med only
        self.assertEqual(len(v["pairs"]), 1)


@unittest.skipUnless(HAVE_SDK, "anthropic SDK not installed")
class ClaudeContenderTest(EvalBase):
    def test_claude_and_local_side_by_side(self):
        self.setup_all(contenders=[], mode="each", baseline=None)
        self.ok("PATCH", "/api/writer", {"claude": {"api_key": KEY}})
        prof = self.ok("POST", "/api/writer/profiles", {"name": "Brisk"})
        self.ok("PATCH", f"/api/writer/profiles/{prof['id']}", {"summary": "Two brisk sentences."})
        self.ok("POST", "/api/eval/contenders", {"name": "Local", "model": G})
        self.ok("POST", "/api/eval/contenders", {"name": "Opus", "provider": "claude", "model": "claude-opus-5-5",
                                                 "thinking": True, "effort": "high", "profile": prof["id"]})
        self.ok("POST", "/api/eval/contenders", {"name": "Haiku", "provider": "claude", "model": "claude-haiku-5-5"})
        self.ok("PATCH", "/api/eval/settings", {"mode": "each"})
        self.ant.output = G0_OUT
        run = self.run_eval()
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G])  # no reload: G is the everyday model
        opus, haiku = [m["body"] for m in self.ant.messages()]
        self.assertEqual((opus["model"], opus["thinking"], opus["output_config"]["effort"], opus["max_tokens"]),
                         ("claude-opus-5-5", {"type": "adaptive"}, "high", 16000))
        self.assertEqual(opus["output_config"]["format"]["type"], "json_schema")
        system = opus["system"] if isinstance(opus["system"], str) else "".join(b["text"] for b in opus["system"])
        self.assertIn("Day summary:\nTwo brisk sentences.", system)
        self.assertNotIn("tools", opus)
        self.assertEqual(opus["messages"][0]["content"], self.loc.chats()[0]["body"]["messages"][1]["content"])
        self.assertEqual((haiku["thinking"], haiku["output_config"]["effort"]), ({"type": "disabled"}, "medium"))
        self.assertNotIn("temperature", haiku)
        info = self.info(run)
        self.assertEqual((info["status"], info["errors"]), ("done", []))
        lines = [json.loads(x) for x in (self.run_dir(run) / "responses.jsonl").read_text().splitlines()]
        self.assertTrue(all("messages" not in x["request"] and "system" not in x["request"] for x in lines))
        m = lines[1]["metrics"]
        self.assertEqual((m["prompt_tokens"], m["completion_tokens"], m["ids_returned"], m["valid_json"]),
                         (3120, 410, 2, True))
        for p in self.run_dir(run).iterdir():
            self.assertNotIn(KEY, p.read_text(), p.name)
        self.assertNotIn(KEY, "\n".join(self.logs))
        self.assertEqual(len(self.ok("GET", f"/api/eval/{run}")["pairs"]), 3)

    def test_claude_needs_the_key_at_run_time(self):
        self.setup_all(contenders=[], mode="each", baseline=None)
        self.ok("PATCH", "/api/writer", {"claude": {"api_key": KEY}})
        self.ok("POST", "/api/eval/contenders", {"name": "Local", "model": G})
        self.ok("POST", "/api/eval/contenders", {"name": "Opus", "provider": "claude", "model": "claude-opus-5-5"})
        self.ok("DELETE", "/api/writer/claude-key")
        status, data, _ = self.call("POST", "/api/eval/run", {})
        self.assertEqual((status, data["error"]),
                         (409, "Add an Anthropic API key in Setup, Delivery first, or remove the Claude contenders."))


class PureTest(unittest.TestCase):
    def test_pairings(self):
        self.assertEqual(evaluate.pairings(["a", "b"], "each"), [("a", "b")])
        self.assertEqual(len(evaluate.pairings(list("abcd"), "each")), 6)
        self.assertEqual(evaluate.pairings(list("abcd"), "baseline", "b"), [("a", "b"), ("c", "b"), ("d", "b")])
        self.assertEqual(evaluate.pairings(list("ab"), "baseline", "z"), [])

    def test_pair_orders_random_but_stable(self):
        self.assertEqual(evaluate.pair_orders(42), evaluate.pair_orders(42))
        seen = {tuple(evaluate.pair_orders(s)) for s in range(40)}
        self.assertEqual({o[0] for o in seen}, {("G0", "Q-med"), ("Q-med", "G0")})
        self.assertEqual({o[1] for o in seen}, {("Q-low", "Q-med"), ("Q-med", "Q-low")})

    def test_tally(self):
        t = evaluate.tally(["a", "b", "c"], {"p1": {"a_key": "a", "b_key": "b", "pick": "A"},
                                             "p2": {"a_key": "c", "b_key": "a", "pick": "same"},
                                             "p3": {"a_key": "b", "b_key": "c", "pick": "B"}})
        self.assertEqual(t, {"a": {"wins": 1, "losses": 0, "ties": 1}, "b": {"wins": 0, "losses": 2, "ties": 0},
                             "c": {"wins": 1, "losses": 0, "ties": 1}})

    def test_scorer_object_replies(self):
        self.assertEqual(evalscore.reply_texts({"replies": {"r1": "a", "r2": 3}}), {"r1": "a"})
        self.assertEqual(evalscore.reply_texts({"replies": [{"id": "r1", "suggestion": "a"},
                                                            {"id": "r1", "suggestion": "b"}]}), {"r1": "a"})
        self.assertTrue(evalscore.schema_valid({"summary": "s", "replies": {"r1": "a"}}, ["r1"]))
        self.assertFalse(evalscore.schema_valid({"summary": "s", "replies": {"r1": "a", "r9": "b"}}, ["r1"]))
        self.assertFalse(evalscore.schema_valid({"summary": "s", "replies": {}, "x": 1}, []))
        s = evalscore.score_drafts({"r1": "Friday at 3pm works.", "r2": "I'll check [date]."},
                                   {"r1": "When can you come?", "r2": "What date suits you?"})
        self.assertEqual((s["placeholder_needed"], s["placeholder_used"], s["invented_when_needed"]), (2, 1, 1))
        self.assertTrue(evalscore.claims_commitment("We have paid the invoice."))
        self.assertFalse(evalscore.claims_commitment("Once it's approved, I'll book it."))


if __name__ == "__main__":
    unittest.main()
