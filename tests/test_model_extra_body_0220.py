"""Extra request fields per model (Setup › AI › Advanced; meta `sorting`, key model_extra_body {model: {key: number}}).
Some models need a model-specific field to stop reasoning (LFM2.5: "thinking_budget_tokens": 0). None by default; the
owner sets a small JSON object for the background model; keys only from background.EXTRA_KEYS, numbers only. Every
background request for that model (the ledger reader's and the brief's, both through Background.post_bg) carries
them at the top level; other models' requests don't. No model server is called: a fake post_fn keeps the bodies."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import aisettings  # noqa: E402
import background  # noqa: E402
import oldata  # noqa: E402
import store  # noqa: E402


class Extra(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        aisettings.reset_cache()
        self.addCleanup(aisettings.reset_cache)
        self.store = store.Store(pathlib.Path(tmp.name))
        self.addCleanup(self.store.close)
        self.bodies = []

        def post(body, timeout):
            self.bodies.append(dict(body))
            return {"choices": [{"message": {"content": "{}"}}]}
        self.bg = background.Background(self.store, post=post, log=lambda *a: None)
        self.bg.update_settings({"model": "lfm2.5"})

    def send(self, model, kind="ledger"):
        body = {"model": model, "messages": [{"role": "user", "content": "x"}], "id_slot": 1,
                "chat_template_kwargs": {"enable_thinking": False}}
        self.bg.post_bg(body, 30, post_fn=lambda b, t: (self.bodies.append(dict(b)), {"choices": []})[1], kind=kind)
        return self.bodies[-1]

    def test_none_by_default(self):
        self.assertIsNone(self.bg.view()["settings"]["extra_body"])
        self.assertNotIn("thinking_budget_tokens", self.send("lfm2.5"))

    def test_sent_for_that_model_only(self):
        self.bg.update_settings({"extra_body": {"thinking_budget_tokens": 0, "top_k": 20}})
        self.assertEqual(self.bg.view()["settings"]["extra_body"], {"thinking_budget_tokens": 0, "top_k": 20})
        for kind in ("ledger", "brief"):
            b = self.send("lfm2.5", kind)
            self.assertEqual((b["thinking_budget_tokens"], b["top_k"], b["model"], b["id_slot"]),
                             (0, 20, "lfm2.5", 1))
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("top_k", self.send("other-model"))
        self.bg.update_settings({"model": "other-model"})          # another pick: its own (none); lfm2.5's kept
        self.assertIsNone(self.bg.view()["settings"]["extra_body"])
        self.assertEqual(self.bg.extra_for("lfm2.5"), {"thinking_budget_tokens": 0, "top_k": 20})
        self.assertEqual(self.store._get_meta("sorting", None)["model_extra_body"],  # noqa: SLF001
                         {"lfm2.5": {"thinking_budget_tokens": 0, "top_k": 20}})

    def test_cleared(self):
        self.bg.update_settings({"extra_body": {"top_p": 0.9}})
        self.bg.update_settings({"extra_body": None})
        self.assertIsNone(self.bg.view()["settings"]["extra_body"])
        self.bg.update_settings({"extra_body": {"min_p": 0.05}})
        self.bg.update_settings({"extra_body": {}})
        self.assertEqual(self.bg.extra_for("lfm2.5"), {})

    def test_validated(self):
        for bad in ([1], "x", {"model": "y"}, {"messages": 1}, {"id_slot": 2}, {"stream": 1}, {"top_k": "20"},
                    {"top_k": True}, {"top_p": float("nan")}, {"top_k": 10 ** 9}, {"response_format": {}}):
            with self.assertRaises(background.InputError, msg=repr(bad)):
                self.bg.update_settings({"extra_body": bad})
        self.assertEqual(self.bg.extra_for("lfm2.5"), {})
        self.bg.update_settings({"extra_body": {k: 0 for k in background.EXTRA_KEYS}})
        self.assertEqual(set(self.bg.extra_for("lfm2.5")), set(background.EXTRA_KEYS))

    def test_a_tampered_saved_value_is_ignored(self):
        self.store._set_meta("sorting", {"model": "lfm2.5", "model_extra_body": {  # noqa: SLF001
            "lfm2.5": {"model": "evil", "top_k": 1}}})
        self.assertEqual(self.bg.extra_for("lfm2.5"), {})
        self.assertEqual(self.send("lfm2.5")["model"], "lfm2.5")

    def test_setup_view_and_old_data_keep_it(self):
        self.bg.update_settings({"extra_body": {"thinking_budget_tokens": 0}})
        self.assertIn("model_extra_body", oldata.SORTING_KEEP)
        self.assertEqual(list(background.EXTRA_KEYS), self.bg.view()["settings"]["extra_keys"])
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function aiExtra(", js)
        self.assertIn("api('PATCH', 'api/background', { extra_body: obj })", js)


if __name__ == "__main__":
    unittest.main()
