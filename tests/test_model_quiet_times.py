"""Quiet times for the model server (settings model_quiet_times) and the background model with no pick.

One setting replaces the fixed blocked windows (readrules) and the fixed night windows (background): a new install
has no quiet times, an install with settings saved before the setting keeps the old ones (store.UPGRADE_VALUES), and
the night's windows are the night (22:00-06:00) minus the quiet times. Setup › AI › Advanced edits them, and so does
Chat (prefs kind model_quiet_times, with Undo). With no background model picked, the server's first listed model is
used, else none ("Pick a model"); a saved pick stays. No model server is called: the list comes from a fake fetch.
"""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import aisettings  # noqa: E402
import background  # noqa: E402
import chatstore  # noqa: E402
import msgstore  # noqa: E402
import prefs  # noqa: E402
import store  # noqa: E402

SOME = [["01:00", "04:00"], ["07:16", "07:45"], ["12:16", "12:45"], ["17:46", "18:15"]]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        aisettings.reset_cache()
        self.addCleanup(aisettings.reset_cache)

    def open(self):
        s = store.Store(self.dir)
        self.addCleanup(s.close)
        return s


class Setting(Tmp):
    def test_a_new_install_has_none_and_the_whole_night(self):
        s = self.open()
        self.assertEqual(s.settings()["model_quiet_times"], [])
        bg = background.Background(s).settings()
        self.assertEqual((bg["quiet_times"], bg["backfill"]["windows"]), ([], [["22:00", "06:00"]]))

    def test_an_older_install_gets_none_too(self):
        s = self.open()
        s._set_meta("settings", {"name": "Alex", "timezone": "UTC"})   # saved before the setting existed
        s.close()
        s = self.open()
        self.assertEqual(s.settings()["model_quiet_times"], [])
        self.assertFalse(hasattr(store, "LEGACY_QUIET_TIMES"))
        self.assertEqual(background.Background(s).settings()["backfill"]["windows"], [["22:00", "06:00"]])
        s.update_settings({"model_quiet_times": SOME})
        bg = background.Background(s).settings()
        self.assertEqual(bg["backfill"]["windows"], [["22:00", "01:00"], ["04:00", "06:00"]])   # the 0.9.0 windows

    def test_setup_validates_and_tidies(self):
        s = self.open()
        got = s.update_settings({"model_quiet_times": [["12:16", "12:45"], {"start": "01:00", "end": "04:00"},
                                                       "23:30-00:30", ["01:00", "04:00"]]})["model_quiet_times"]
        self.assertEqual(got, [["01:00", "04:00"], ["12:16", "12:45"], ["23:30", "00:30"]])   # sorted, no repeats
        for bad in ([["25:00", "04:00"]], [["01:00", "01:00"]], [["1:00", "4:00"]], "01:00-04:00", [["01:00"]],
                    [["01:00", "04:00"]] * 1 + [[f"0{i}:10", f"0{i}:20"] for i in range(9)]):
            with self.assertRaises(store.InputError, msg=repr(bad)):
                s.update_settings({"model_quiet_times": bad})
        self.assertEqual(s.update_settings({"model_quiet_times": []})["model_quiet_times"], [])

    def test_night_windows_fit_around_the_quiet_times(self):
        nw = background.night_windows
        self.assertEqual(nw([]), [["22:00", "06:00"]])
        self.assertEqual(nw([["01:00", "04:00"]]), [["22:00", "01:00"], ["04:00", "06:00"]])
        self.assertEqual(nw([["12:16", "12:45"]]), [["22:00", "06:00"]])              # outside the night
        self.assertEqual(nw([["21:00", "07:00"]]), [])                                 # the whole night
        self.assertEqual(nw([["23:00", "23:30"], ["01:00", "02:00"], ["03:00", "03:10"], ["04:00", "04:05"]]),
                         [["22:00", "23:00"], ["23:30", "01:00"], ["04:05", "06:00"]])  # the 3 longest, in order


class Model(Tmp):
    def server(self, s):
        s.update_writer({"local": {"base_url": "http://192.168.1.50:8080/v1"}})

    def test_no_pick_uses_the_first_listed_model_else_none(self):
        s = self.open()
        self.server(s)
        calls = []

        def fetch(base, key):
            calls.append(base)
            return {"data": [{"id": "qwen-9b"}, {"id": "other-2b"}]}
        bg = background.Background(s, models=fetch)
        self.assertEqual(bg.settings()["model"], "")            # nothing picked, nothing listed yet
        self.assertEqual(background.background_model(s), background.NO_MODEL)
        self.assertEqual(bg.refresh_models(), "qwen-9b")        # asks the owner's server once
        self.assertEqual(bg.refresh_models(), "qwen-9b")
        self.assertEqual(len(calls), 1)
        self.assertEqual(bg.settings()["model"], "qwen-9b")
        bg.update_settings({"ctx_size": 8192})                  # saving something else doesn't pin the model
        self.assertNotIn("model", s._get_meta("sorting", {}))
        bg.update_settings({"model": "other-2b"})               # a pick stays
        aisettings.reset_cache()
        self.assertEqual(bg.settings()["model"], "other-2b")

    def test_a_saved_pick_stays(self):
        s = self.open()
        s._set_meta("sorting", {"model": "my-model-q4"})
        self.assertEqual(background.Background(s).settings()["model"], "my-model-q4")
        self.assertEqual(background.background_model(s), "my-model-q4")

    def test_no_built_in_model_left(self):
        self.assertEqual(background.NO_MODEL, "")
        for p in APP.glob("*.py"):
            self.assertNotIn("LEGACY_MODEL", p.read_text(encoding="utf-8"), p.name)


class ByChat(Tmp):
    def setUp(self):
        super().setUp()
        self.st = self.open()
        self.ms = msgstore.MessageStore(self.dir)
        self.addCleanup(self.ms.close)
        self.pp = prefs.Prefs(self.ms, settings_get=self.st.settings, settings_set=self.st.update_settings)

    def card(self, text, args, earlier=()):
        intent = chatstore.setting_intent(text)
        self.assertIsNotNone(intent, text)
        c = chatstore.build_setting_card(intent, dict(args, kind="model_quiet_times"), list(earlier) + [text])
        if c["status"] == "ready":
            c["mode"], c["why"] = chatstore.setting_mode(intent, c)
        return c

    def test_add_at_once_with_undo(self):
        c = self.card("Keep the model server quiet from 1 am to 4 am", {"start": "01:00", "end": "04:00"})
        self.assertEqual((c["status"], c["mode"], c["fields"]["on"]), ("ready", "apply", True))
        self.assertEqual(c["title"], "Model server quiet time")
        row = self.pp.apply("model_quiet_times", c["fields"])
        self.assertEqual(self.st.settings()["model_quiet_times"], [["01:00", "04:00"]])
        self.pp.undo(row["id"])
        self.assertEqual(self.st.settings()["model_quiet_times"], [])

    def test_remove(self):
        self.st.update_settings({"model_quiet_times": SOME})
        c = self.card("Remove the model server quiet time from 12:16 to 12:45", {"start": "12:16", "end": "12:45", "on": False})
        self.assertEqual((c["status"], c["mode"], c["fields"]["on"]), ("ready", "apply", False))
        row = self.pp.apply("model_quiet_times", c["fields"])
        self.assertNotIn(["12:16", "12:45"], self.st.settings()["model_quiet_times"])
        self.pp.undo(row["id"])
        self.assertEqual(self.st.settings()["model_quiet_times"], SOME)
        with self.assertRaises(prefs.Invalid):
            self.pp.apply("model_quiet_times", {"start": "09:00", "end": "10:00", "on": False})   # not one of them

    def test_only_the_owners_hours(self):
        c = self.card("Keep the model server quiet at night", {"start": "01:00", "end": "04:00"})
        self.assertEqual(c["status"], "refused")
        c = self.card("Don't use the GPU from 2 am to 5 am", {"start": "01:00", "end": "04:00"})
        self.assertEqual(c["status"], "refused")
        c = self.card("Keep the model server quiet from 1 am to 4 am", {"start": "01:00", "end": "04:00", "on": False})
        self.assertEqual((c["status"], c["mode"], c["fields"]["on"]), ("ready", "confirm", True))   # Callsheet changed it
        c = chatstore.build_setting_card(chatstore.setting_intent("Make my phone quiet from 1 am to 4 am"),
                                         {"kind": "model_quiet_times", "start": "01:00", "end": "04:00"},
                                         ["Make my phone quiet from 1 am to 4 am"])
        self.assertEqual(c["status"], "refused")                  # the phone's quiet hours are another kind

    def test_the_setting_tool_offers_the_kind(self):
        self.assertIn("model_quiet_times", chatstore.SETTING_DEF["schema"]["properties"]["kind"]["enum"])
        self.assertIn("model_quiet_times", chatstore.SETTING_DEF["description"])


class Gate(Tmp):
    def test_quiet_times_gate_the_reading(self):
        import extract
        s = self.open()
        s.update_writer({"local": {"base_url": "http://192.168.1.50:8080/v1"}})
        s._set_meta("sorting", {"model": "m"})
        bg = background.Background(s)
        at = [dt.datetime(2027, 3, 1, 2, 0)]
        bg._now = lambda: at[0]
        bg.call_guard = lambda now=None: (300, None)
        bg.chat_first = lambda: False
        ms = msgstore.MessageStore(self.dir)
        self.addCleanup(ms.close)
        r = extract.Runner(extract.Extractor(ms, "UTC", "Alex", call=lambda *a, **k: None), bg)
        self.assertNotEqual(r.gate(), "blocked")                  # a new install: no quiet times
        s.update_settings({"model_quiet_times": [["01:00", "04:00"]]})
        self.assertEqual(r.gate(), "blocked")
        at[0] = dt.datetime(2027, 3, 1, 0, 59, 30)                # less than a minute before one
        self.assertEqual(r.gate(), "blocked")


class Page(unittest.TestCase):
    JS = (APP / "static" / "app.js").read_text(encoding="utf-8")

    def test_setup_ai_advanced_edits_them(self):
        a = self.JS.index("function aiQuiet()")
        src = self.JS[a:self.JS.index("function aiAdvanced(", a)]
        self.assertIn("'Quiet times for the model server'", src)
        self.assertIn("patchSettings({ model_quiet_times: next }", src)
        self.assertIn("type: 'time'", src)
        self.assertIn("aiQuiet(), chatG", self.JS)
        self.assertIn("'▲ Pick a model'", self.JS)
        self.assertIn("'Pick a model'", self.JS[self.JS.index("function aiPicker("):])


if __name__ == "__main__":
    unittest.main()
