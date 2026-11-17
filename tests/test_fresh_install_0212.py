"""0.21.2: a new install carries nothing from the developer's own setup.

A fresh App in an empty data folder: every settings view a newcomer sees in Setup starts empty or neutral (no texts
folder or phone sensor, only Google Messages ticked, the assistant called Bruce, no quiet times for the model server,
no model server, model or SearXNG address, only the built-in Personal area, nothing connected or switched on). Texts
is off until a folder is saved. An install from before 0.21.2 that never saved a folder gets none either.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import api  # noqa: E402
import live  # noqa: E402
import msgstore  # noqa: E402
import texts  # noqa: E402

GM = "com.google.android.apps.messaging"
# words that only the developer's own setup had; none may show in a new install's Setup
PERSONAL = ("_last_notification", "192.168.", "searx")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.share = os.path.join(self.root, "share")
        self.backup = os.path.join(self.share, "texts")      # a folder with backups on disk, never saved
        os.makedirs(self.share)
        self.data = os.path.join(self.root, "data")
        p = mock.patch.dict(os.environ, {"CALLSHEET_REPORTS_ROOTS": self.share, "SUPERVISOR_TOKEN": ""})
        p.start()
        self.addCleanup(p.stop)

    def app(self):
        a = api.App(self.data)
        self.addCleanup(a.close)
        return a

    def get(self, app, route):
        return app.handle("GET", route, None)


class FreshInstall(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.backup)     # even with a backup folder on disk, a new install doesn't read it
        self.a = self.app()

    def test_texts_empty_folder_google_messages_only_no_sensor(self):
        v = self.get(self.a, "texts")
        self.assertEqual(v["settings"], {"folder": "", "folder_set": False, "sensor": "", "apps": [GM]})
        self.assertEqual(v["folder_hint"], "/share/<your folder>")
        self.assertNotIn("default_folder", v)
        self.assertEqual([a["name"] for a in v["apps"]], ["Google Messages", "Signal", "WhatsApp"])
        self.assertEqual(texts.BackupReader(self.a.messages).targets(), [])      # texts reading is off
        self.assertEqual(texts.NotificationReader(self.a.messages).targets(), [])
        self.assertEqual(self.get(self.a, "sources")["sources"], [])
        self.assertEqual(self.get(self.a, "texts/sensors"), {"available": False, "sensors": [],
                                                              "error": texts.ha.NOT_IN_HA})

    def test_name_quiet_times_and_areas(self):
        st = self.get(self.a, "state")
        s = st["settings"]
        self.assertEqual(s["assistant_name"], "Bruce")
        self.assertEqual(s["model_quiet_times"], [])
        self.assertEqual(s["name"], "")
        self.assertEqual([(a["id"], a["name"], a["fixed"]) for a in st["areas"]], [("personal", "Personal", True)])
        self.assertEqual(st["areas"][0]["accounts"], [])
        self.assertEqual(st["areas"][0]["slack"], [])
        self.assertEqual(st["vips"], [])
        self.assertFalse(st["google_client"]["configured"])
        self.assertEqual(st["background"]["model"], "")
        prof = self.get(self.a, "profile")
        self.assertEqual([a["id"] for a in prof["areas"]], ["personal"])
        self.assertEqual((prof["people"], prof["projects"]), ([], []))

    def test_no_model_server_or_model(self):
        ai = self.get(self.a, "ai")
        self.assertEqual(ai["server"], {"base_url": "", "key_hint": None})
        self.assertEqual(ai["chat"]["model"], "")
        self.assertEqual(ai["background"]["model"], "")
        self.assertIsNone(ai["background"]["extra_body"])
        self.assertIsNone(ai["claude"]["key_hint"])
        self.assertFalse(ai["claude"]["fallback"])
        bg = self.get(self.a, "background")
        self.assertEqual(bg["settings"]["model"], "")
        self.assertFalse(bg["status"]["server"])
        w = self.get(self.a, "writer")
        self.assertEqual(w["provider"], "off")
        self.assertEqual((w["local"]["base_url"], w["local"]["model"], w["local"]["key_hint"]), ("", "", None))
        self.assertIsNone(w["claude"]["key_hint"])
        cs = self.get(self.a, "chat/settings")
        self.assertEqual((cs["local"]["base_url"], cs["local"]["model"]), ("", ""))
        self.assertFalse(cs["claude"]["has_key"])
        self.assertFalse(cs["ready"])

    def test_no_searxng(self):
        ws = self.get(self.a, "web-search")
        self.assertEqual(ws["base_url"], "")
        self.assertFalse(ws["on"])
        self.assertFalse(ws["configured"])
        self.assertIsNone(ws["added"])

    def test_other_setup_views_start_empty(self):
        g = lambda r: self.get(self.a, r)  # noqa: E731
        push = g("push")["settings"]
        self.assertEqual(push["targets"], [])
        self.assertFalse(push["at_call_times"])
        self.assertFalse(push["urgent"])
        voice = g("voice")
        self.assertEqual(voice["settings"], {"enabled": False, "hosts": []})
        self.assertEqual(voice["tokens"], [])
        c = g("connectors")
        self.assertEqual((c["connectors"], c["hosts"], c["interests"]), ([], [], []))
        gh = g("github")
        self.assertEqual((gh["token_set"], gh["repos"]), (False, []))
        self.assertFalse(g("projects/settings")["find"]["github"])
        self.assertEqual(g("gdrafts")["accounts"], [])
        self.assertEqual(g("contacts")["accounts"], [])
        self.assertEqual(g("calendar/access"), {"accounts": [], "can_add": False})
        sig = g("signal")
        self.assertFalse(sig["linked"])
        self.assertIsNone(sig["number"])
        self.assertEqual((sig["ignored"], sig["chats"]), ([], []))
        self.assertEqual(g("index/settings")["vault"], "")
        self.assertEqual(g("preferences")["rules"], [])
        self.assertEqual(g("memory")["notes"], [])
        ppl = g("people")
        self.assertEqual((ppl["people"], ppl["me"]["names"], ppl["me"]["handles"]), ([], [], []))
        self.assertEqual(g("old-data")["groups"], [])

    def test_nothing_personal_in_any_setup_view(self):
        import json
        for r in ("state", "texts", "ai", "web-search", "background", "writer", "chat/settings", "push", "voice",
                  "connectors", "github", "projects/settings", "signal", "index/settings", "profile", "people",
                  "sources"):
            body = json.dumps(self.get(self.a, r))
            for w in PERSONAL:
                self.assertNotIn(w, body, (r, w))

    def test_texts_stay_off_after_a_restart(self):
        self.a.store.update_settings({"name": "Alex"})          # the newcomer saves Setup
        self.a.close()
        a2 = self.app()
        self.assertEqual(self.get(a2, "texts")["settings"]["folder"], "")
        self.assertEqual(texts.BackupReader(a2.messages).targets(), [])


class Upgrade(Base):
    """An install from before 0.21.2 that never saved a folder (folder_set false) gets no folder: texts stay off."""

    def old_install(self, texts_meta=None, settings=True):
        a = api.App(self.data)
        if settings:
            a.store.update_settings({"name": "Alex"})
        if texts_meta is None:      # as 0.21.1 left it: no texts settings
            a.messages.write(lambda db: db.execute("DELETE FROM meta WHERE key='texts'"))
        else:
            a.messages.set_meta("texts", texts_meta)
        a.close()

    def test_no_folder_without_a_saved_one(self):
        os.makedirs(self.backup)
        self.old_install({"sensor": "sensor.phone_last_notification", "apps": [GM]})
        a = self.app()
        st = self.get(a, "texts")["settings"]
        self.assertEqual((st["folder"], st["folder_set"]), ("", False))
        self.assertEqual(st["sensor"], "sensor.phone_last_notification")      # the rest is untouched
        self.assertEqual(texts.BackupReader(a.messages).targets(), [])

    def test_no_texts_settings_stays_empty(self):
        os.makedirs(self.backup)
        self.old_install(None)
        self.assertEqual(self.get(self.app(), "texts")["settings"]["folder"], "")

    def test_an_unsaved_folder_from_before_is_not_read(self):
        os.makedirs(self.backup)
        self.old_install({"folder": self.backup, "folder_set": False})
        a = self.app()
        self.assertEqual(self.get(a, "texts")["settings"]["folder"], "")
        self.assertEqual(texts.BackupReader(a.messages).targets(), [])

    def test_a_saved_folder_is_never_changed(self):
        os.makedirs(self.backup)
        self.old_install({"folder": self.backup, "folder_set": True})
        a = self.app()
        self.assertEqual(self.get(a, "texts")["settings"]["folder"], self.backup)
        self.assertEqual(texts.BackupReader(a.messages).targets(), [("phone", "Texts")])


class UiAndRunSh(unittest.TestCase):
    def test_placeholder_and_start_line(self):
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("placeholder: v.folder_hint", js)
        self.assertNotIn("default_folder", js)
        run = (ROOT / "callsheet" / "run.sh").read_text(encoding="utf-8")
        self.assertIn('bashio::log.info "Starting Callsheet (nothing is sent except an RSVP you press)"', run)
        self.assertNotIn("\r", run)
        src = (APP / "texts.py").read_text(encoding="utf-8")
        for w in ("DEFAULT_FOLDER", "LEGACY_FOLDER", "def migrate"):
            self.assertNotIn(w, src)


if __name__ == "__main__":
    unittest.main()
