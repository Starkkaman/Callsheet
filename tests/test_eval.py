"""Writer evaluation (0.3.3) against fakes only: fake Google and a fake OpenAI-compatible server that answers
per model id and can answer 503 "Loading model". Never contacts the real llama-server or any real API."""
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
from test_writer import LOCAL_KEY, WriterBase  # noqa: E402

UTC = dt.timezone.utc
G, Q = evaluate.GEMMA_MODEL, evaluate.QWEN_MODEL
G0_OUT = {"summary": "Gemma summary.", "replies": {"r1": "I'll look at the budget by [day].", "r2": "Lunch works."}}
QLOW_OUT = {"summary": "Qwen low summary.", "replies": {"r1": "I've already approved the budget.", "r2": "Sure."}}
QMED_OUT = {"summary": "Qwen medium summary.", "replies": {"r1": "Yes, Thursday works.", "r2": ""}}


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

    def setup_all(self, enabled=True):
        self.seed()
        self.ok("PATCH", "/api/writer", {"provider": "local", "local": {"base_url": self.loc.base, "model": G,
                                                                        "api_key": LOCAL_KEY}})
        if enabled:
            self.ok("PATCH", "/api/eval/settings", {"enabled": True})

    def run_eval(self):
        st = self.ok("POST", "/api/eval/run", {})
        self.assertTrue(st["running"])
        deadline = time.time() + 30
        while self.ev.job is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.ev.job)
        return self.ok("GET", "/api/eval/status")["runs"][0]["id"]

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
        self.assertEqual(self.ok("GET", "/api/state")["eval"], {"enabled": False, "gemma_model": G, "qwen_model": Q})
        for method, path in (("GET", "/api/eval/status"), ("POST", "/api/eval/run"),
                             ("GET", "/api/eval/20261029T090000Z"), ("GET", "/api/eval/20261029T090000Z/export"),
                             ("POST", "/api/eval/20261029T090000Z/choices")):
            status, data, _ = self.call(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 404, path)
            self.assertEqual(data["error"], evaluate.OFF)
        self.assertEqual(self.loc.requests, [])
        self.assertFalse((self.app.store.dir / "writer-eval").exists())

    def test_settings_validation(self):
        st = self.ok("PATCH", "/api/eval/settings", {"enabled": True, "qwen_model": " other-qwen "})
        self.assertEqual(st, {"enabled": True, "gemma_model": G, "qwen_model": "other-qwen"})
        self.assertEqual(self.ok("PATCH", "/api/eval/settings", {"qwen_model": ""})["qwen_model"], Q)
        for bad in ({"enabled": "yes"}, {"gemma_model": "x" * 121}, {"other": 1}, {"gemma_model": "a\nb"}):
            self.assertEqual(self.call("PATCH", "/api/eval/settings", bad)[0], 400)
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

    def test_no_local_server(self):
        self.seed()
        self.ok("PATCH", "/api/eval/settings", {"enabled": True})
        status, data, _ = self.call("POST", "/api/eval/run", {})
        self.assertEqual((status, data["error"]), (409, "Add the local model's server address in Setup, Delivery first."))

    def test_manual_sync_refused_while_running(self):
        self.setup_all()
        self.ev.job = {"run": "x", "step": 2, "config": "Q-low", "t0": time.monotonic()}
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
        self.ev.job = {"run": "x", "step": 1, "config": "G0", "t0": time.monotonic()}
        try:
            status, data, _ = self.call("POST", "/api/eval/run", {})
            self.assertEqual((status, data["error"]), (409, "An evaluation is already running."))
            prog = self.ok("GET", "/api/eval/status")["progress"]
            self.assertEqual((prog["step"], prog["steps"], prog["config"]), (1, 4, "G0"))
        finally:
            self.ev.job = None
        self.assertEqual(self.loc.chats(), [])


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
        self.loc.requests.clear()
        self.loc.by_model = {G: [json.dumps(G0_OUT), "OK"], Q: [json.dumps(QLOW_OUT), json.dumps(QMED_OUT)]}
        self.loc.loading_for = {Q: 1}  # the server swaps models: one 503 "Loading model", then it answers
        before = self.snapshot()
        n_google = len(self.fake.requests)
        run = self.run_eval()

        # live brief, last sync, last writer, account status: unchanged
        self.assertEqual(self.snapshot(), before)
        google = self.fake.requests[n_google:]
        self.assertTrue(google)
        self.assertTrue(all(m == "GET" for m, p, q in google if not p.startswith("/token")))

        # order: G0, Q-low (503, retry), Q-med, Gemma reload; bodies per setup
        chats = self.loc.chats()
        self.assertEqual([c["body"]["model"] for c in chats], [G, Q, Q, Q, G])
        g0, qlow, qmed, reload = chats[0]["body"], chats[2]["body"], chats[3]["body"], chats[4]["body"]
        self.assertEqual(chats[1]["body"], qlow)
        self.assertEqual(g0["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(g0["max_tokens"], 4096)
        self.assertNotIn("reasoning_effort", g0)
        self.assertNotIn("reasoning_budget_tokens", g0)
        for k in ("temperature", "top_p", "top_k"):
            self.assertNotIn(k, g0)
        for body, effort, budget in ((qlow, "low", 2048), (qmed, "medium", 4096)):
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})
            self.assertEqual((body["reasoning_effort"], body["reasoning_budget_tokens"], body["max_tokens"]),
                             (effort, budget, budget + 4096))
            self.assertEqual((body["temperature"], body["top_p"], body["top_k"]), (1.0, 0.95, 20))
            self.assertEqual(body["messages"], g0["messages"])  # the same payload for every setup
            self.assertEqual(body["response_format"], g0["response_format"])
        self.assertEqual(reload["max_tokens"], 8)
        self.assertEqual(len(reload["messages"]), 1)
        self.assertFalse(any("id_slot" in c["body"] for c in chats))  # no server slot set: never sent
        self.assertTrue(all(c["headers"].get("authorization") == "Bearer " + LOCAL_KEY for c in chats))
        self.assertEqual(evaluate.config_body(evaluate.CONFIGS[1], Q, "p", [], writer.local_options({}))[1]["timeout"],
                         450)

        # storage
        d = self.app.store.dir / "writer-eval" / run
        self.assertEqual(stat.S_IMODE(os.stat(d.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        names = sorted(p.name for p in d.iterdir())
        self.assertEqual(names, ["data.json", "metrics.json", "payload.txt", "responses.jsonl", "run.json"])
        for p in d.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p.name)
        payload = (d / "payload.txt").read_text()
        self.assertEqual(g0["messages"][1]["content"], payload)
        self.assertIn("Can you sign off the budget by Thursday?", payload)
        lines = [json.loads(x) for x in (d / "responses.jsonl").read_text().splitlines()]
        self.assertEqual([x["config"] for x in lines], ["G0", "Q-low", "Q-med"])
        self.assertEqual([x["model"] for x in lines], [G, Q, Q])
        self.assertTrue(all("messages" not in x["request"] and x["response"]["choices"] for x in lines))
        self.assertNotIn(LOCAL_KEY, "".join(p.read_text() for p in d.iterdir()))
        info = json.loads((d / "run.json").read_text())
        self.assertEqual((info["status"], info["errors"], info["ids"]), ("done", [], ["r1", "r2"]))

        # metrics
        m = json.loads((d / "metrics.json").read_text())
        self.assertEqual(set(m), {"G0", "Q-low", "Q-med"})
        g, lo, me = m["G0"], m["Q-low"], m["Q-med"]
        self.assertTrue(g["valid_json"] and g["schema_valid"])
        self.assertEqual((g["finish_reason"], g["prompt_tokens"], g["completion_tokens"]), ("stop", 900, 120))
        self.assertEqual((g["prompt_n"], g["cache_n"], g["predicted_per_second"], g["reasoning_chars"]),
                         (880, 20, 31.5, 90))
        self.assertEqual((g["ids_expected"], g["ids_returned"], g["empties"], g["flagged"]), (2, 2, 0, 0))
        self.assertEqual((g["placeholder_used"], g["placeholder_needed"], g["invented_when_needed"]), (1, 1, 0))
        self.assertEqual((lo["flagged"], lo["flagged_ids"], lo["commitment_ids"]), (1, ["r1"], ["r1"]))
        self.assertEqual((lo["placeholder_used"], lo["placeholder_needed"]), (0, 1))
        self.assertFalse(me["schema_valid"])  # r2 empty: minLength 1
        self.assertTrue(me["valid_json"])
        self.assertEqual((me["ids_returned"], me["empties"], me["invented_when_needed"]), (1, 1, 1))

        # status and logs: counts only
        st = self.ok("GET", "/api/eval/status")
        self.assertEqual((st["running"], st["progress"], st["reason"]), (False, None, None))
        self.assertEqual(st["runs"][0]["status"], "done")
        logs = "\n".join(self.logs)
        for secret in ("Priya", "budget", "Gemma summary", "Lunch", LOCAL_KEY):
            self.assertNotIn(secret, logs)

    def test_blind_view_and_choices(self):
        self.setup_all()
        run = self.run_eval()
        v = self.ok("GET", f"/api/eval/{run}")
        text = json.dumps(v)
        for hidden in ("G0", "Q-low", "Q-med", G, Q, "config", "metrics", "valid_json"):
            self.assertNotIn(hidden, text)
        self.assertFalse(v["revealed"])
        self.assertEqual([p["id"] for p in v["pairs"]], ["p1", "p2"])
        self.assertEqual(v["rows"], [{"id": "r1", "who": "Priya N", "subject": "Budget"},
                                     {"id": "r2", "who": "Bob", "subject": "Lunch plan?"}])
        seed = json.loads((self.app.store.dir / "writer-eval" / run / "run.json").read_text())["seed"]
        orders = evaluate.pair_orders(seed)
        summ = {"G0": "Gemma summary.", "Q-low": "Qwen low summary.", "Q-med": "Qwen medium summary."}
        for p, (a, b) in zip(v["pairs"], orders):
            self.assertEqual((p["a"]["summary"], p["b"]["summary"]), (summ[a], summ[b]))
        self.assertEqual(self.ok("GET", f"/api/eval/{run}"), v)  # stable per run

        self.assertEqual(self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A"}})[0], 400)
        self.assertEqual(self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "C"}})[0], 400)
        out = self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "A", "p2": "same"}})
        self.assertTrue(out["revealed"])
        self.assertEqual([(p["a_config"], p["b_config"], p["pick"]) for p in out["pairs"]],
                         [orders[0] + ("A",), orders[1] + ("same",)])
        self.assertEqual([x["config"] for x in out["metrics"]], ["G0", "Q-low", "Q-med"])
        status, data, _ = self.call("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "B", "p2": "B"}})
        self.assertEqual((status, data["error"]), (409, "Choices for this run were already submitted."))
        ch = self.app.store.dir / "writer-eval" / run / "choices.json"
        self.assertEqual(stat.S_IMODE(os.stat(ch).st_mode), 0o600)
        self.assertEqual(json.loads(ch.read_text())["picks"]["p1"]["better"], orders[0][0])

    def test_export(self):
        self.setup_all()
        run = self.run_eval()
        r, body = self.raw(f"/api/eval/{run}/export")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/x-ndjson; charset=utf-8")
        self.assertEqual(r.getheader("Content-Disposition"),
                         f'attachment; filename="callsheet-writer-eval-{run}.jsonl"')
        lines = [json.loads(x) for x in body.splitlines()]
        self.assertEqual([x["type"] for x in lines], ["payload", "config", "config", "config"])
        d = self.app.store.dir / "writer-eval" / run
        self.assertEqual(lines[0]["payload"], (d / "payload.txt").read_text())
        self.assertEqual(lines[1]["config"], "G0")
        self.assertEqual(lines[1]["summary"], "Gemma summary.")
        self.assertEqual(lines[1]["drafts"], G0_OUT["replies"])
        self.assertEqual(lines[3]["drafts"], {"r1": "Yes, Thursday works."})
        self.assertEqual(lines[2]["metrics"]["flagged_ids"], ["r1"])
        self.ok("POST", f"/api/eval/{run}/choices", {"picks": {"p1": "B", "p2": "A"}})
        lines = [json.loads(x) for x in self.raw(f"/api/eval/{run}/export")[1].splitlines()]
        self.assertEqual(lines[-1]["type"], "choices")
        self.assertEqual(lines[-1]["picks"]["p1"]["pick"], "B")
        self.assertEqual(self.raw("/api/eval/20200101T000000Z/export")[0].status, 404)
        self.assertEqual(self.raw("/api/eval/..%2f..%2fx/export")[0].status, 404)

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
        for cfg in evaluate.CONFIGS:
            self.assertEqual(evaluate.config_body(cfg, Q, "p", ["r1"], opts)[0]["id_slot"], 7, cfg["name"])
            self.assertNotIn("id_slot", evaluate.config_body(cfg, Q, "p", ["r1"], writer.local_options({}))[0])
        self.assertEqual(evaluate.reload_body(G, opts)["id_slot"], 7)
        self.assertNotIn("id_slot", evaluate.reload_body(G, writer.local_options({})))

    def test_failures_are_plain_and_reload_still_runs(self):
        self.setup_all()
        self.loc.by_model = {G: ["not json", "OK"]}  # the Qwen model isn't known to this server: 404
        run = self.run_eval()
        info = json.loads((self.app.store.dir / "writer-eval" / run / "run.json").read_text())
        self.assertEqual(info["status"], "done")
        self.assertEqual(info["errors"], ["Q-low: The local model server doesn't know that address or model.",
                                          "Q-med: The local model server doesn't know that address or model."])
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G, Q, Q, G])
        m = json.loads((self.app.store.dir / "writer-eval" / run / "metrics.json").read_text())
        self.assertFalse(m["G0"]["valid_json"])
        self.assertEqual(m["Q-low"]["error"], "The local model server doesn't know that address or model.")
        st = self.ok("GET", "/api/eval/status")
        self.assertEqual(st["last"]["errors"], info["errors"])

    def test_stops_before_a_setup_when_a_brief_is_near(self):
        self.setup_all()
        # a brief comes near once G0 has answered
        self.ev.clock = lambda: self.at(12, 25) if self.loc.chats() else self.at(9)
        run = self.run_eval()
        info = json.loads((self.app.store.dir / "writer-eval" / run / "run.json").read_text())
        self.assertEqual(info["status"], "stopped")
        self.assertIn("Stopped before", info["errors"][0])
        self.assertEqual([c["body"]["model"] for c in self.loc.chats()], [G])  # no Qwen, so no reload either


class PureTest(unittest.TestCase):
    def test_pair_orders_random_but_stable(self):
        self.assertEqual(evaluate.pair_orders(42), evaluate.pair_orders(42))
        seen = {tuple(evaluate.pair_orders(s)) for s in range(40)}
        self.assertEqual({o[0] for o in seen}, {("G0", "Q-med"), ("Q-med", "G0")})
        self.assertEqual({o[1] for o in seen}, {("Q-low", "Q-med"), ("Q-med", "Q-low")})

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
