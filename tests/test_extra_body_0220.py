"""--extra-body on the live replays (tools/replay_ledger.py, tools/replay_brief.py): a JSON object of model-specific
request fields (LFM2.5 needs a top-level "thinking_budget_tokens": 0) merged at the top level into every live request
body. It never overrides model, messages or id_slot, and the --out summary records it (extra_body).
In-process fakes only: writer._post is replaced, so no model server and no network."""
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

import replay_brief as RB  # noqa: E402
import replay_ledger as R  # noqa: E402
import writer  # noqa: E402

CASE = {"case": "S-X-01", "set": "synthetic", "set_version": "1.4", "replay": "isolated", "owner": "Jordan Vale",
        "patterns": [], "updates": [], "follow_ups": [], "links": [], "no_items": True, "unjudged": [],
        "calendar": [], "conflicts": [], "expect": [],
        "messages": [{"mid": "S-X-01:m1", "source": "signal", "direction": "in", "sender": "p:ben", "to": ["owner"],
                      "chat": "c_ben", "chat_kind": "dm", "ts": "2026-11-03T09:00-08:00", "text": "thanks!"}]}


class Fake:
    """writer._post replaced: keeps each request body, answers with an empty JSON object."""

    def __enter__(self):
        self.bodies, self.orig = [], writer._post  # noqa: SLF001

        def post(base, key, body, *a, **k):
            self.bodies.append(json.loads(json.dumps(body)))
            return {"choices": [{"message": {"content": "{}"}}]}
        writer._post = post  # noqa: SLF001
        return self

    def __exit__(self, *exc):
        writer._post = self.orig  # noqa: SLF001


def quietly(fn, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = fn(argv)
    return code, out.getvalue()


class TestParse(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(R.parse_extra_body(""), {})
        self.assertEqual(R.parse_extra_body('{"thinking_budget_tokens": 0}'), {"thinking_budget_tokens": 0})
        for bad in ("[1]", "0", "{nope", '{"model": "x"}', '{"messages": []}', '{"id_slot": 3, "top_k": 1}'):
            with self.assertRaises(ValueError, msg=bad):
                R.parse_extra_body(bad)

    def test_merge_keeps_model_messages_slot(self):
        body = {"model": "m", "messages": [1], "id_slot": 1, "max_tokens": 9}
        got = R.with_extra(body, {"thinking_budget_tokens": 0, "max_tokens": 5, "model": "x", "id_slot": 2})
        self.assertEqual(got, {"model": "m", "messages": [1], "id_slot": 1, "max_tokens": 5,
                               "thinking_budget_tokens": 0})
        self.assertEqual(body["max_tokens"], 9)                 # the original body untouched
        self.assertIs(R.with_extra(body, {}), body)

    def test_bad_option_refused(self):
        for fn, argv in ((R.main, ["--cases", "x.jsonl", "--extractor", "live", "--base", "b", "--model", "m"]),
                         (RB.main, ["--fixture", "--base", "b", "--model", "m"])):
            with self.assertRaises(SystemExit):
                quietly(fn, argv + ["--extra-body", '{"model": "other"}'])


class TestLedger(unittest.TestCase):
    def run_live(self, *extra):
        with tempfile.TemporaryDirectory() as d, Fake() as f:
            p = pathlib.Path(d, "cases.jsonl")
            p.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
            code, out = quietly(R.main, ["--cases", str(p), "--quiet", "--extractor", "live", "--base",
                                         "http://127.0.0.1:9/v1", "--model", "lfm", "--out", f"{d}/s.json", *extra])
            return f.bodies, json.loads(pathlib.Path(d, "s.json").read_text())

    def test_every_request_carries_it(self):
        bodies, s = self.run_live("--extra-body", '{"thinking_budget_tokens": 0}')
        self.assertTrue(bodies)
        for b in bodies:
            self.assertEqual(b["thinking_budget_tokens"], 0)
            self.assertEqual(b["model"], "lfm")
            self.assertTrue(b["messages"])
        self.assertEqual(s["extra_body"], {"thinking_budget_tokens": 0})

    def test_none_by_default(self):
        bodies, s = self.run_live()
        self.assertTrue(bodies)
        self.assertFalse(any("thinking_budget_tokens" in b for b in bodies))
        self.assertEqual(s["extra_body"], {})


class TestBrief(unittest.TestCase):
    def test_every_step_carries_it(self):
        with tempfile.TemporaryDirectory() as d, Fake() as f:
            quietly(RB.main, ["--fixture", "--quiet", "--base", "http://127.0.0.1:9/v1", "--model", "lfm",
                              "--out", f"{d}/s.json", "--extra-body", '{"thinking_budget_tokens": 0, "top_k": 20}'])
            s = json.loads(pathlib.Path(d, "s.json").read_text())
        self.assertTrue(f.bodies)
        for b in f.bodies:
            self.assertEqual((b["thinking_budget_tokens"], b["top_k"], b["model"]), (0, 20, "lfm"))
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(s["extra_body"], {"thinking_budget_tokens": 0, "top_k": 20})


if __name__ == "__main__":
    unittest.main()
