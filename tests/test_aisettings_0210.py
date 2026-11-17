"""Setup › AI in one place (aisettings.py; approved 2026-11-12): one model server, a model per job from that
server's own list (GET /models on the owner's saved server only), "Use one model for everything", Claude only as a
fallback the owner turns on, and the one-time upgrade of older settings (presets, Chat's own server, Claude as the
writer). No live model calls: an OpenAI-compatible fake server on 127.0.0.1."""
import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import aisettings  # noqa: E402
import api  # noqa: E402
import background  # noqa: E402
import chat  # noqa: E402
import writer  # noqa: E402
from fake_ai import FakeLocal  # noqa: E402
from store import InputError, Store  # noqa: E402

LKEY = "loc-key-SECRET9876"
CKEY = "sk-ant-api03-SECRETKEY-abcdefgh1234"
OWN_KEY = "own-chat-key-SECRET5555"


class Router(FakeLocal):
    """A llama.cpp router: each model with its status and arguments."""
    def __init__(self):
        super().__init__()
        self.entries = [
            {"id": "example-large-27b-q3", "status": {"value": "loaded", "args": ["--ctx-size", "65536"]}},
            {"id": "example-model-26b-q4", "status": {"value": "unloaded", "args": "-c 32768"}},
            {"id": "gpt-oss-20b-MXFP4"},
            {"id": "example-large-27b-q3"},          # a repeat is listed once
            {"id": "bad name with spaces"}, "Qwen3.5-9B-Q8_0"]

    def answer(self, req):
        if self.status == 200 and req["method"] == "GET" and req["path"] == "/v1/models":
            return 200, {"object": "list", "data": self.entries}, None
        return super().answer(req)


class Base(unittest.TestCase):
    def setUp(self):
        aisettings.reset_cache()
        writer.reset_ctx()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.local = Router()
        self.addCleanup(self.local.close)
        self.bg = background.Background(self.store, None, models=lambda b, k: {"data": []})

    def meta(self, k):
        return self.store._get_meta(k, None)  # noqa: SLF001

    def put(self, k, v):
        self.store._set_meta(k, v)  # noqa: SLF001


class MigrationTest(Base):
    def test_new_install_writes_nothing(self):
        self.assertEqual(aisettings.migrate(self.store), [])
        for k in ("writer", "chat", "ai"):
            self.assertIsNone(self.meta(k))
        self.assertTrue(aisettings.settings(self.store)["one_model"])

    def test_an_install_already_in_shape_writes_nothing(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": ""}})
        raw = self.meta("writer")
        self.assertEqual(aisettings.migrate(self.store), [])
        self.assertEqual(self.meta("writer"), raw)
        self.assertIsNone(self.meta("ai"))
        self.assertIsNone(self.meta("chat"))

    def test_chats_own_server_becomes_the_server_when_none_is_saved(self):
        self.put("writer", {"provider": "off", "local": {"base_url": "", "model": ""}})
        self.put("chat", {"provider": "local", "local": {"base_url": self.local.base, "model": "example-model",
                                                         "api_key": OWN_KEY, "timeout": 200}})
        notes = aisettings.migrate(self.store)
        w = self.store.writer_config()
        self.assertEqual((w["local"]["base_url"], w["local"]["api_key"], w["provider"]), (self.local.base, OWN_KEY, "local"))
        c = chat.load_settings(self.store)
        self.assertEqual((c["local"]["base_url"], c["local"]["api_key"], c["local"]["model"], c["local"]["timeout"]),
                         ("", None, "example-model", 200))
        self.assertEqual(chat.effective_local(self.store)["model"], "example-model")
        self.assertTrue(any("is now your model server" in n for n in notes))
        self.assertNotIn(OWN_KEY, json.dumps(self.meta("ai")))   # the snapshot never copies a key

    def test_chats_other_server_gives_way_and_chat_keeps_its_model(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": "qwen3-coder",
                                                           "api_key": LKEY}})
        self.put("chat", {"provider": "local", "local": {"base_url": "http://10.0.0.9:8080/v1", "model": "example-model",
                                                         "api_key": OWN_KEY}})
        notes = aisettings.migrate(self.store)
        eff = chat.effective_local(self.store)
        self.assertEqual((eff["base_url"], eff["api_key"], eff["model"]), (self.local.base, LKEY, "example-model"))
        self.assertIn("Chat had its own server (10.0.0.9:8080)", notes[0])
        self.assertFalse(self.meta("ai")["one_model"])

    def test_chat_that_followed_the_writer_keeps_that_model(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": "qwen3-coder"}})
        self.put("chat", {"provider": "local", "local": {"base_url": "", "model": ""}})
        self.put("sorting", {"model": "example-model-26b-q4"})
        self.assertEqual(aisettings.migrate(self.store), [])
        self.assertEqual(chat.load_settings(self.store)["local"]["model"], "qwen3-coder")
        # picking a Background model later doesn't move Chat
        aisettings.update(self.store, self.bg, {"background_model": "example-large-27b-q3"})
        self.assertEqual(chat.effective_local(self.store)["model"], "qwen3-coder")
        self.assertEqual(self.store.writer_config()["local"]["model"], "example-large-27b-q3")

    def test_same_model_everywhere_turns_one_model_on(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": "example-model"}})
        self.put("sorting", {"model": "example-model"})
        aisettings.migrate(self.store)
        self.assertTrue(aisettings.view(self.store, self.bg)["one_model"])

    def test_claude_writer_becomes_the_fallback_only_with_a_key(self):
        self.put("writer", {"provider": "claude", "claude": {"api_key": CKEY, "model": "claude-opus-5-5"},
                            "local": {"base_url": self.local.base, "model": "example-model"}})
        notes = aisettings.migrate(self.store)
        w = self.store.writer_config()
        self.assertEqual((w["provider"], w["fallback_to_claude"], w["claude"]["api_key"]), ("local", True, CKEY))
        self.assertTrue(any("fallback" in n for n in notes))
        self.assertNotIn(CKEY, json.dumps(self.meta("ai")))

    def test_claude_writer_without_a_key_stays_local_only(self):
        self.put("writer", {"provider": "claude", "local": {"base_url": "", "model": ""}})
        aisettings.migrate(self.store)
        w = self.store.writer_config()
        self.assertEqual((w["provider"], w["fallback_to_claude"]), ("off", False))
        self.assertFalse(aisettings.view(self.store, self.bg)["claude"]["fallback"])

    def test_off_with_a_server_stays_off_until_a_choice_in_setup(self):
        self.put("writer", {"provider": "off", "local": {"base_url": self.local.base, "model": "example-model"}})
        aisettings.migrate(self.store)
        self.assertEqual(self.store.writer_config()["provider"], "off")   # the owner's own choice
        aisettings.update(self.store, self.bg, {"background_model": "example-model"})
        self.assertEqual(self.store.writer_config()["provider"], "local")

    def test_presets_and_the_per_call_mix_are_kept_aside(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": "example-model",
                                                           "api_key": LKEY},
                            "presets": [{"id": "fast", "builtin": True},
                                        {"id": "p1", "name": "Careful", "model": "example-large", "api_key": LKEY}],
                            "call_presets": {"morning": "p1", "midday": "fast"}})
        self.put("sorting", {"model": "example-model", "mode": "shadow", "shadow_until": "2026-11-01"})
        notes = aisettings.migrate(self.store)
        self.assertTrue(any("Presets are gone" in n and "example-large" in n for n in notes))
        w = self.meta("writer")
        self.assertNotIn("presets", w)
        self.assertNotIn("call_presets", w)
        before = self.meta("ai")["before"]
        self.assertEqual(before["writer"]["call_presets"], {"morning": "p1", "midday": "fast"})
        self.assertEqual(before["sorting"]["mode"], "shadow")
        self.assertNotIn(LKEY, json.dumps(self.meta("ai")))
        self.assertEqual(self.bg.settings()["model"], "example-model")     # Background is untouched

    def test_runs_once(self):
        self.put("writer", {"provider": "claude", "claude": {"api_key": CKEY}, "local": {"base_url": self.local.base}})
        self.assertTrue(aisettings.migrate(self.store))
        self.store.update_writer({"fallback_to_claude": False})
        self.assertEqual(aisettings.migrate(self.store), [])
        self.assertFalse(self.store.writer_config()["fallback_to_claude"])

    def test_the_app_migrates_at_start_and_keeps_working(self):
        self.put("writer", {"provider": "local", "local": {"base_url": self.local.base, "model": "qwen3-coder"},
                            "presets": [{"id": "p1", "model": "x"}], "call_presets": {"morning": "p1"}})
        self.put("chat", {"provider": "local", "local": {"base_url": self.local.base, "model": "example-model"}})
        self.store.close()
        a = api.App(self.tmp.name, clock=time.time)
        self.addCleanup(a.close)
        v = a.ai_view()
        self.assertEqual((v["server"]["base_url"], v["chat"]["model"]), (self.local.base, "example-model"))
        self.assertEqual(v["notes"], ["Presets are gone: one model per job now. Your Careful preset used x."])
        self.assertTrue(chat.readiness(a.store)[0])
        v = a.patch_ai({"notes_seen": True})
        self.assertEqual(v["notes"], [])


class ModelListTest(Base):
    def setUp(self):
        super().setUp()
        self.store.update_writer({"local": {"base_url": self.local.base, "api_key": LKEY}})

    def test_lists_the_owners_server_with_context_and_state(self):
        out = aisettings.list_models(self.store)
        self.assertTrue(out["ok"])
        self.assertEqual(out["models"], [
            {"id": "example-large-27b-q3", "ctx": 65536, "state": "loaded"},
            {"id": "example-model-26b-q4", "ctx": 32768, "state": "unloaded"},
            {"id": "gpt-oss-20b-MXFP4", "ctx": None, "state": None},
            {"id": "Qwen3.5-9B-Q8_0", "ctx": None, "state": None}])
        self.assertEqual(out["message"], "Connected · 4 models")
        reqs = self.local.requests
        self.assertEqual([(r["method"], r["path"]) for r in reqs], [("GET", "/v1/models")])
        self.assertEqual(reqs[0]["headers"]["authorization"], "Bearer " + LKEY)
        v = aisettings.view(self.store, self.bg)
        self.assertEqual(v["models"]["models"][0]["id"], "example-large-27b-q3")   # remembered, no new request
        self.assertEqual(len(self.local.requests), 1)
        self.assertNotIn(LKEY, json.dumps(v))
        self.assertEqual(v["server"]["key_hint"], "…9876")

    def test_clear_errors(self):
        self.store.update_writer({"local": {"base_url": ""}})
        self.assertEqual(aisettings.list_models(self.store)["message"], "Add your model server's address first.")
        self.store.update_writer({"local": {"base_url": "http://127.0.0.1:9/v1"}})
        out = aisettings.list_models(self.store)
        self.assertEqual((out["ok"], out["message"]),
                         (False, "Couldn't reach your model server. Check the address and that it's running."))
        self.store.update_writer({"local": {"base_url": self.local.base}})
        self.local.status = 401
        self.assertEqual(aisettings.list_models(self.store)["message"], "The local model server refused the API key.")
        self.local.status = 200
        self.local.entries = None
        self.assertIn("not like an OpenAI-compatible server", aisettings.list_models(self.store)["message"])
        self.local.entries = []
        self.assertEqual(aisettings.list_models(self.store)["message"], "Connected, but the server lists no models.")

    def test_short_timeout(self):
        seen = {}

        def slow(method, base, path, key, timeout=None, **_kw):
            seen.update(method=method, path=path, timeout=timeout)
            raise writer.LocalTimeout("x")
        orig = writer._local  # noqa: SLF001
        writer._local = slow  # noqa: SLF001
        try:
            out = aisettings.list_models(self.store)
        finally:
            writer._local = orig  # noqa: SLF001
        self.assertEqual(seen, {"method": "GET", "path": "/models", "timeout": aisettings.MODELS_TIMEOUT})
        self.assertLessEqual(aisettings.MODELS_TIMEOUT, 10)
        self.assertEqual(out["message"], "Your model server didn't answer within 8 seconds.")

    def test_only_the_saved_server(self):
        # the endpoint takes no address; Chat's old "Load models" can't probe another server either
        a = api.App.__new__(api.App)
        a.store, a.clock = self.store, time.time
        self.assertTrue(a.ai_models({"base_url": "http://127.0.0.1:9/v1"})["ok"])
        self.assertEqual({r["path"] for r in self.local.requests}, {"/v1/models"})
        out = chat.list_models(self.store, "http://127.0.0.1:9/v1")
        self.assertEqual(out["ok"], False)
        self.assertIn("only asks your saved model server", out["message"])
        self.assertTrue(chat.list_models(self.store, self.local.base + "/")["ok"])


class UpdateTest(Base):
    def setUp(self):
        super().setUp()
        aisettings.migrate(self.store)

    def test_server_address_and_key(self):
        v = aisettings.update(self.store, self.bg, {"base_url": self.local.base + "/", "api_key": LKEY})
        self.assertEqual((v["server"]["base_url"], v["server"]["key_hint"]), (self.local.base, "…9876"))
        self.assertEqual(self.store.writer_config()["provider"], "local")
        v = aisettings.update(self.store, self.bg, {"api_key": ""})
        self.assertIsNone(v["server"]["key_hint"])
        v = aisettings.update(self.store, self.bg, {"base_url": ""})
        self.assertEqual(self.store.writer_config()["provider"], "off")
        with self.assertRaises(InputError):
            aisettings.update(self.store, self.bg, {"base_url": "ftp://x"})

    def test_one_model_for_everything(self):
        aisettings.update(self.store, self.bg, {"base_url": self.local.base})
        v = aisettings.update(self.store, self.bg, {"background_model": "example-model"})
        self.assertTrue(v["one_model"])
        self.assertEqual((v["chat"]["model"], v["background"]["model"]), ("example-model", "example-model"))
        v = aisettings.update(self.store, self.bg, {"chat_model": "example-large"})   # Chat on its own: the switch goes off
        self.assertFalse(v["one_model"])
        self.assertEqual((v["chat"]["model"], v["background"]["model"]), ("example-large", "example-model"))
        v = aisettings.update(self.store, self.bg, {"background_model": "Qwen3.5-9B"})   # off: Chat stays
        self.assertEqual(v["chat"]["model"], "example-large")
        v = aisettings.update(self.store, self.bg, {"one_model": True})
        self.assertEqual((v["one_model"], v["chat"]["model"]), (True, "Qwen3.5-9B"))
        v = aisettings.update(self.store, self.bg, {"background_model": "example-large"})   # on: Chat follows
        self.assertEqual((v["chat"]["model"], self.store.writer_config()["local"]["model"]), ("example-large", "example-large"))
        self.assertTrue(chat.shares_background(self.store))
        v = aisettings.update(self.store, self.bg, {"one_model": False})
        self.assertFalse(v["one_model"])
        self.assertEqual(v["chat"]["model"], "example-large")

    def test_claude_needs_a_key_and_is_never_on_by_default(self):
        v = aisettings.view(self.store, self.bg)
        self.assertEqual((v["claude"]["key_hint"], v["claude"]["fallback"], v["chat"]["provider"]), (None, False, "local"))
        with self.assertRaises(InputError):
            aisettings.update(self.store, self.bg, {"chat_claude": "claude-opus-5-5"})
        self.store.update_writer({"claude": {"api_key": CKEY}})
        v = aisettings.view(self.store, self.bg)
        self.assertFalse(v["claude"]["fallback"])
        self.assertNotIn(CKEY, json.dumps(v))
        v = aisettings.update(self.store, self.bg, {"chat_claude": "claude-haiku-5-5"})
        self.assertEqual((v["chat"]["provider"], v["chat"]["claude_model"], v["one_model"]), ("claude", "claude-haiku-5-5", False))
        # Background picks never move Chat off Claude
        v = aisettings.update(self.store, self.bg, {"background_model": "example-model"})
        self.assertEqual(v["chat"]["provider"], "claude")
        v = aisettings.update(self.store, self.bg, {"chat_model": "example-model"})   # back on the server; the switch is the owner's
        self.assertEqual((v["chat"]["provider"], v["chat"]["model"], v["one_model"]), ("local", "example-model", False))

    def test_bad_input_saves_nothing(self):
        aisettings.update(self.store, self.bg, {"base_url": self.local.base})
        before = (self.meta("writer"), self.meta("chat"), self.meta("sorting"), self.meta("ai"))
        for bad in [{}, [], {"nope": 1}, {"background_model": "a b"}, {"chat_model": ""},
                    {"background_model": "ok", "chat_claude": "gpt-9"}, {"one_model": "yes"},
                    {"notes_seen": False}, {"presets": []}, {"call_presets": {}}]:
            with self.assertRaises(InputError, msg=bad):
                aisettings.update(self.store, self.bg, bad)
        self.assertEqual(before, (self.meta("writer"), self.meta("chat"), self.meta("sorting"), self.meta("ai")))


class ChatSettingsTest(Base):
    def test_chat_has_no_server_of_its_own(self):
        self.store.update_writer({"local": {"base_url": self.local.base, "model": "qwen3-coder"}})
        for bad in [{"local": {"base_url": "http://10.0.0.9:8080/v1"}}, {"local": {"api_key": OWN_KEY}}]:
            with self.assertRaises(InputError):
                chat.update_settings(self.store, bad)
        s = chat.public_settings(self.store)
        self.assertEqual((s["local"]["base_url"], s["local"]["model"], s["ready"]), (self.local.base, "qwen3-coder", True))

    def test_chat_follows_background_when_nothing_else_is_set(self):
        self.store.update_writer({"local": {"base_url": self.local.base}})
        self.bg.update_settings({"model": "example-model"})
        self.assertEqual(chat.effective_local(self.store)["model"], "example-model")
        self.assertTrue(chat.shares_background(self.store))

    def test_show_thinking_still_works(self):   # the Chat tab's switch writes the same settings
        s = chat.update_settings(self.store, {"local": {"thinking": True}, "claude": {"thinking": True}})
        self.assertTrue(s["local"]["thinking"])
        v = aisettings.view(self.store, self.bg)
        self.assertTrue(v["chat"]["thinking"])


if __name__ == "__main__":
    unittest.main()
