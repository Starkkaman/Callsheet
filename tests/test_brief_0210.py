"""0.21.0 Talk it through (brief2.py): the plan, the model's JSON, grounding, the fallback, replies and the push."""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "callsheet" / "app"), str(ROOT / "tests")]

import brief2  # noqa: E402
import chat  # noqa: E402
import extract  # noqa: E402
import msgstore  # noqa: E402
import push  # noqa: E402
import today  # noqa: E402
import today_fixtures as F  # noqa: E402
import writer  # noqa: E402
import api  # noqa: E402
from fake_ai import FakeLocal  # noqa: E402


class Base(unittest.TestCase):
    stage = "0840"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [0.0]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.ex = extract.Extractor(self.ms, F.TZ, F.OWNER, None, clock=lambda: self.now[0])
        self.td = today.Today(self.ms, self.ex.lv, lambda: self.now[0], alerter=F.FakeAlerter())
        self.ids = F.seed(self.ms, self.ex, self.now, self.td, self.stage)
        self.calls = []

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()

    def brief(self, answer=None, model="local-test", prefs=None):
        def call(body):
            self.calls.append(body)
            return answer(body) if callable(answer) else answer
        return brief2.Brief(self.ms, self.ex.lv, self.td, prefs, clock=lambda: self.now[0],
                            call=call if answer is not None else None, model=lambda: model,
                            owner=lambda: [F.OWNER], sleep=lambda s: None)


def answer_for(text, vis=None, more=None):
    def a(body):
        key = body["response_format"]["json_schema"]["schema"]["properties"]["step"]["enum"][0]
        t = text(key) if callable(text) else text
        return {"choices": [{"message": {"content": json.dumps({"step": key, "text": t,
                                                                 "visual": vis or {"type": "none", "refs": []},
                                                                 "more": more or []})}}]}
    return a


class TestPlan(Base):
    def test_order_and_steps(self):
        p = self.brief().plan()
        self.assertEqual([s["key"] for s in p["steps"]], ["day", "clashes", "needs", "coming", "money", "cleanup"])
        self.assertEqual([s["n"] for s in p["steps"]], [1, 2, 3, 4, 5, 6])
        self.assertLessEqual(len(p["steps"]), brief2.MAX_STEPS)
        money = p["steps"][4]
        self.assertEqual(money["title"], "A bill and a trip")
        self.assertEqual([b["type"] for b in money["visual"]], ["bills", "trip"])
        self.assertEqual(money["visual"][1]["to"], "Austin")
        self.assertEqual(p["steps"][1]["count"], 3)
        self.assertEqual([b["type"] for b in p["steps"][1]["visual"]], ["clash"] * 3)

    def test_deterministic(self):
        a, b = self.brief().plan(), self.brief().plan()
        self.assertEqual(json.dumps(a, sort_keys=True, default=str), json.dumps(b, sort_keys=True, default=str))
        self.assertEqual(a["sig"], b["sig"])

    def test_stages_plan(self):
        for stage in ("0905", "1205", "1220"):
            with self.subTest(stage=stage):
                self.tearDown()
                self.stage = stage
                self.setUp()
                p = self.brief().plan()
                self.assertEqual(p["steps"][0]["key"], "day")
                self.assertTrue(all(s["template"] for s in p["steps"]))
                self.assertEqual(p["sig"], self.brief().plan()["sig"])

    def test_prefs_skip_short_first(self):
        class R:
            def brief(self):
                return [{"topic": "clean up", "choice": "skip"}, {"topic": "bills", "choice": "first", "note": "be brief"},
                        {"topic": "coming up", "choice": "short"}]

            def digest(self, name):
                return "stands_out"

        class P:
            def rules(self):
                return R()
        p = self.brief(prefs=P()).plan()
        keys = [s["key"] for s in p["steps"]]
        self.assertEqual(keys[0], "money")
        self.assertNotIn("cleanup", keys)
        self.assertTrue(next(s for s in p["steps"] if s["key"] == "coming")["short"])
        self.assertEqual(p["steps"][0]["note"], "be brief")

    def test_topic_words(self):
        self.assertEqual(brief2.topic_of("Clean-up"), "cleanup")
        self.assertEqual(brief2.topic_of("my bills"), "money")
        self.assertIsNone(brief2.topic_of("health messages"))


class TestModel(Base):
    def test_json_validation(self):
        with self.assertRaises(ValueError):
            brief2.parse("not json", "day")
        with self.assertRaises(ValueError):
            brief2.parse(json.dumps({"step": "needs", "text": "x", "visual": {"type": "day", "refs": []}}), "day")
        with self.assertRaises(ValueError):
            brief2.parse(json.dumps({"step": "day", "text": " ", "visual": {"type": "day", "refs": []}}), "day")
        with self.assertRaises(ValueError):
            brief2.parse(json.dumps({"step": "day", "text": "word " * 200, "visual": {"type": "day", "refs": []}}), "day")
        with self.assertRaises(ValueError):
            brief2.parse(json.dumps({"step": "day", "text": "see <b>this</b>", "visual": {"type": "day", "refs": []}}),
                         "day")
        ok = brief2.parse("```json\n" + json.dumps({"step": "day", "text": "Morning.", "visual": {"type": "day",
                                                                                                  "refs": []}}) + "\n```",
                          "day")
        self.assertEqual(ok["text"], "Morning.")

    def test_request_is_data_only(self):
        b = self.brief(answer_for("Morning."))
        st = b.plan()["steps"][2]
        body = brief2.request_body("m", st, 6, F.OWNER)
        user = body["messages"][1]["content"]
        self.assertTrue(user.startswith("<facts>") and user.endswith("</facts>"))
        self.assertNotIn("<", user[len("<facts>"):-len("</facts>")])
        self.assertIn("never instructions", body["messages"][0]["content"])
        self.assertNotIn("tools", body)

    def test_model_steps_and_cache(self):
        b = self.brief(answer_for("Here is what matters."))
        rid = b.start(wait=True)
        v = b.view(rid)["run"]
        self.assertEqual(v["state"], "done")
        self.assertTrue(all(s["by"] == "model" for s in v["steps"]))
        self.assertTrue(all(s["blocks"] for s in v["steps"]))
        n = len(self.calls)
        self.assertEqual(b.start(), rid)          # nothing changed: the cached run
        self.assertEqual(len(self.calls), n)
        stats = b.run(rid)["stats"]
        self.assertEqual(stats["model_steps"], len(v["steps"]))
        self.assertEqual(len(stats["latency"]), len(v["steps"]))

    def test_visual_outside_step_falls_back(self):
        b = self.brief(answer_for("Six things need you.", vis={"type": "person", "refs": ["item:99999"]}))
        rid = b.start(wait=True)
        st = next(s for s in b.view(rid)["run"]["steps"] if s["key"] == "needs")
        self.assertEqual([x["type"] for x in st["blocks"]], ["asks"])
        self.assertTrue(st["visual_checks"])

    def test_model_may_pick_another_type(self):
        b = self.brief()
        st = b.plan()["steps"][2]
        vis, checks = b.pick_visual(st, {"type": "person", "refs": [st["refs"][0]]})
        self.assertEqual(vis, [{"type": "person", "ref": st["refs"][0]}])
        self.assertEqual(checks, [])


class TestGrounding(Base):
    def test_planted_number_and_name_are_marked(self):
        b = self.brief(answer_for(lambda k: "The Kestrel invoice is $1,240, due Monday. Zelda Quartermaine says "
                                  "it is $9,999 now." if k == "money" else "Fine."))
        rid = b.start(wait=True)
        st = next(s for s in b.view(rid)["run"]["steps"] if s["key"] == "money")
        flagged = [c["what"] for c in st["checks"]]
        self.assertIn("$9,999", flagged)
        self.assertIn("Zelda", flagged)
        self.assertNotIn("$1,240", flagged)
        self.assertNotIn("Kestrel", flagged)
        self.assertEqual("".join(t for t, _ in st["segs"]), st["text"])   # nothing dropped

    def test_times_from_items_pass(self):
        segs, checks = brief2.ground("Color session at 8:30, then Lumen Films at 9:45 am.",
                                     ['{"time": "8:30 am", "what": "Color session"}',
                                      '{"time": "9:45 am", "what": "Lumen Films call"}'], [2])
        self.assertEqual(checks, [])

    def test_decision_warning(self):
        said = next((c for c in ("I approved the budget.", "Approved, I'll pay Kestrel today.", "We agreed to the rate.",
                                 "You approved the budget and paid Kestrel.")
                     if writer.states_decision(c, attributed_ok=True, owner=(F.OWNER,))), None)
        if said is None:
            self.skipTest("writer.states_decision flags none of these")
        b = self.brief(answer_for(said))
        rid = b.start(wait=True)
        self.assertTrue(any(s.get("warning") == writer.WARNING for s in b.view(rid)["run"]["steps"]))


class TestFallback(Base):
    def test_no_model_uses_templates(self):
        b = self.brief(model="")
        rid = b.start(wait=True)
        v = b.view(rid)["run"]
        self.assertTrue(all(s["by"] == "template" and s["text"] for s in v["steps"]))
        self.assertEqual(b.run(rid)["steps"]["day"]["why"], "no local model")

    def test_garbage_and_errors(self):
        for ans, why in (("{oops", "not JSON"), (lambda body: (_ for _ in ()).throw(writer.ServerFault("x")), "ServerFault")):
            with self.subTest(why=why):
                b = self.brief(ans)
                rid = b.start(force=True, wait=True)
                r = b.run(rid)
                self.assertEqual(r["steps"]["day"]["by"], "template")
                self.assertEqual(r["steps"]["day"]["why"], why)

    def test_busy_waits_then_template(self):
        t = [0.0]

        def busy(body):
            t[0] += 50
            raise writer.Busy("chat")
        b = self.brief(busy)
        b.monotonic = lambda: t[0]
        st = b.plan()["steps"][0]
        out = b.write_step(st, 1, "m")
        self.assertEqual((out["by"], out["why"]), ("template", "the model was busy"))
        self.assertGreater(len(self.calls), 1)


class TestReplies(Base):
    def test_step_scope_from_plan(self):
        b = self.brief(model="")
        rid = b.start(wait=True)
        sc = b.step_scope(rid, "clashes")
        self.assertEqual(sc["title"], "Clashes")
        self.assertTrue(sc["items"])
        self.assertIsNone(b.step_scope(rid, "nope"))

    def test_brief_pre_marks_items_seen(self):
        b = self.brief(model="")
        sc = b.step_scope(b.start(wait=True), "needs")

        class T:
            seen_items = set()

            def _ledger_out(self, it):
                self.seen_items.add(int(it["id"]))
                return {"item": it["id"], "what": it["what"]}
        stub = type("S", (), {"ledger": self.ex.lv})()
        tools = T()
        args, res, label, name = chat.Chat._brief_pre(stub, tools, sc)
        self.assertEqual(name, "ledger_list")
        self.assertEqual(label, "Looked at this step: Needs you")
        self.assertEqual(tools.seen_items, set(sc["items"][:20]))
        self.assertEqual(len(json.loads(res)["items"]), len(tools.seen_items))


class TestEndToEnd(unittest.TestCase):
    """A reply at a step -> a Chat turn scoped to that step -> an action card -> applied at once with Undo, saved in
    the day's Morning brief conversation; the step's picture follows the store (no live model: FakeLocal)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [F.ts(F.STAGES["0840"])]
        self.app = api.App(self.tmp.name, clock=lambda: self.now[0])
        self.addCleanup(lambda: getattr(self.app.chat, "close", lambda: None)())
        self.app.store.update_settings({"timezone": F.TZ})
        self.ids = F.seed(self.app.messages, self.app.extractor, self.now, self.app.today, "0840")
        self.local = FakeLocal()
        self.addCleanup(self.local.close)
        self.app.store.update_writer({"local": {"base_url": self.local.base, "model": "qwen3-coder"}})
        self.app.brief2.model = lambda: ""          # the steps' words from the templates; replies go to Chat

    def start(self):
        self.app.handle("POST", "talk/start", {})
        self.app.brief2.thread.join(10)

    def turn(self, step, text, args):
        self.local.messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": "p1", "type": "function",
                                "function": {"name": "propose_action", "arguments": json.dumps(args)}}]},
                               {"role": "assistant", "content": "Done, with Undo."}]
        run = self.app.handle("GET", "talk", None)["run"]
        conv = self.app.handle("POST", "talk/reply", {"text": text, "step": step, "run": run["id"]})
        self.assertTrue(self.app.chat.wait(conv["id"], 15), "the chat didn't answer")
        return self.app.handle("GET", "talk", None)

    def asks(self, d):
        st = next(s for s in d["run"]["steps"] if s["key"] == "needs")
        return [r["who"] for b in st["blocks"] if b["type"] == "asks" for r in b["d"]["rows"]]

    def test_reply_card_undo_in_morning_brief(self):
        self.start()
        d = self.app.handle("GET", "talk", None)
        self.assertEqual(d["run"]["state"], "done")
        self.assertIsNone(d["conversation"])                           # no replies yet: nothing created
        priya = self.ids["priya"]
        self.assertIn("Priya Shah", self.asks(d))
        d = self.turn("needs", "Mark Priya's ask done", {"action": "done", "items": [priya]})
        conv = d["conversation"]
        self.assertTrue(conv["title"].startswith(chat.BRIEF_TITLE))    # the day's Morning brief chat
        self.assertEqual(conv["id"], self.app.chat.brief_conversation(create=False))
        q, a = conv["messages"][-2:]
        self.assertEqual((q["role"], q["brief_step"]["key"], a["brief_step"]["key"]), ("user", "needs", "needs"))
        self.assertIn(priya, q["brief_step"]["items"])
        self.assertIn("Looked at this step: Needs you", a["steps"])   # the scoped prefetch
        tools = [m for m in self.local.chats()[0]["body"]["messages"] if m.get("role") == "tool"]
        self.assertIn('\\"list\\": \\"brief_step\\"', tools[0]["content"])
        card = a["card"]
        self.assertEqual((card["status"], card["mode"]), ("ready", "apply"))
        self.assertTrue(card["applied"] and card["token"])             # applied at once, Undo kept
        self.assertEqual(self.app.ledger.item(priya, detail=False)["status"], "done")
        self.assertNotIn("Priya Shah", self.asks(d))                   # the picture follows the store
        out = self.app.handle("POST", "chat/undo", {"chat": conv["id"], "turn": card["turn"], "token": card["token"]})
        self.assertTrue(out["undone"])
        self.assertNotEqual(self.app.ledger.item(priya, detail=False)["status"], "done")
        d = self.app.handle("GET", "talk", None)
        self.assertIn("Priya Shah", self.asks(d))
        self.assertTrue(d["conversation"]["messages"][-1]["card"]["undone"])

    def test_remind_at_noon_card(self):
        self.start()
        priya = self.ids["priya"]
        d = self.turn("needs", "Remind me about Priya at noon", {"action": "remind", "items": [priya],
                                                                  "until": "2026-11-11T12:00"})
        card = d["conversation"]["messages"][-1]["card"]
        self.assertEqual((card["kind"], card["status"]), ("remind", "ready"))
        self.assertIn("12:00 pm", card["label"])
        self.assertTrue(card["applied"])
        out = self.app.handle("POST", "chat/undo", {"chat": d["conversation"]["id"], "turn": card["turn"],
                                                    "token": card["token"]})
        self.assertTrue(out["undone"])
        with self.assertRaises(api.InputError):
            self.app.handle("POST", "talk/reply", {"text": "hi", "step": "nope", "run": d["run"]["id"]})


class TestReplayBrief(unittest.TestCase):
    """tools/replay_brief.py on the seeded ledger: --dry scores 100 %; a fault and a blocked window stop the run with a
    partial summary; no live server."""

    def main(self, *extra):
        sys.path.insert(0, str(ROOT / "tools"))
        import replay_brief
        with tempfile.TemporaryDirectory() as d:
            code = replay_brief.main(["--fixture", "--quiet", "--out", f"{d}/s.json", *extra])
            return code, json.loads(pathlib.Path(f"{d}/s.json").read_text())

    def test_dry_scores_full(self):
        code, s = self.main("--dry")
        self.assertEqual((code, s["gate"]), (0, "pass"))
        self.assertEqual((s["planned_steps"], s["model_steps"]), (6, 6))
        self.assertEqual((s["json"]["rate"], s["grounding"]["rate"], s["coverage"]["rate"]), (1.0, 1.0, 1.0))
        self.assertEqual(s["grounding"]["flagged_words"], 0)
        self.assertNotIn("Priya", json.dumps(s))                       # counts and keys only

    def test_fault_and_window_stop(self):
        import replay_brief
        import replay_ledger
        calls = []

        def fault(*a, **k):
            calls.append(1)
            if len(calls) == 5:              # 0.22.x: each unusable answer is asked once more (repair_body)
                raise writer.ServerFault("500")
            return {"choices": [{"message": {"content": "{oops"}}]}
        # the quiet times by a fake clock (never the time of day the suite runs at): 10:00 is clear, 12:20 blocked
        at = [dt.datetime(2026, 11, 11, 10, 0)]
        waits = []

        def wait(sec):
            waits.append(sec)
            at[0] += dt.timedelta(seconds=sec)
        orig, on, ow = writer._post, replay_ledger.now_in, replay_ledger.wait  # noqa: SLF001
        writer._post = fault  # noqa: SLF001
        replay_ledger.now_in, replay_ledger.wait = (lambda tz: at[0].replace(tzinfo=tz)), wait
        try:
            code, s = self.main("--base", "http://127.0.0.1:9/v1", "--model", "m")
            self.assertEqual(code, 1)
            self.assertEqual((s["gate"], s["written_steps"], len(calls)), ("partial", 2, 5))
            self.assertTrue(s["run"]["stopped"].startswith("server fault"))
            self.assertEqual(s["json"], {"ok": 0, "answers": 2, "rate": 0.0})
            self.assertEqual(waits, [])
            at[0] = dt.datetime(2026, 11, 11, 12, 20)
            code, s = self.main("--base", "http://127.0.0.1:9/v1", "--model", "m", "--on-block", "stop",
                              "--quiet-times", "12:16-12:45")
            self.assertEqual((s["gate"], s["written_steps"], s["run"]["stopped"]), ("partial", 0, "blocked window"))
            self.assertEqual(waits, [])
            calls.clear()
            at[0] = dt.datetime(2026, 11, 11, 12, 40)      # waits (on the fake clock) until 3 min after the window
            replay_ledger.guard(argparse.Namespace(tz="America/Los_Angeles", on_block="wait",
                                                    quiet_times="01:00-04:00,12:16-12:45"), lambda line: None)
            self.assertTrue(waits and at[0].time() >= dt.time(12, 45), (waits, at[0]))
        finally:
            writer._post, replay_ledger.now_in, replay_ledger.wait = orig, on, ow  # noqa: SLF001


class TestPushAndPage(unittest.TestCase):
    def test_ready_payload_deep_link(self):
        orig = push.app_slug
        push.app_slug = lambda: "abc_callsheet"
        try:
            p = push.ready_payload(6)
        finally:
            push.app_slug = orig
        self.assertEqual(p["title"], "Your brief is ready")
        self.assertEqual(p["data"]["url"], "/abc_callsheet#brief")
        self.assertEqual(p["data"]["clickAction"], "/abc_callsheet#brief")

    def test_page_static(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        self.assertNotIn("'Old brief'", js)
        self.assertIsNone(re.search(r"TD\.old\b", js))
        self.assertIn("function tkView()", js)
        self.assertIn("api/talk/reply", js)
        self.assertIn("COMING LATER", js)
        self.assertIn("location.hash === '#brief'", js)
        self.assertNotIn("innerHTML", js[js.index("function tkView"):js.index("function tkView") + 4000])
        api_src = (ROOT / "callsheet" / "app" / "api.py").read_text()
        for r in ('r"talk"', 'r"talk/start"', 'r"talk/reply"'):
            self.assertIn(r, api_src)
        self.assertTrue(re.search(r'APP_VERSION = "0\.21\.3"', api_src))


if __name__ == "__main__":
    unittest.main()
