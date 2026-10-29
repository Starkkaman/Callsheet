"""Upgrading a 0.2.0 database to 0.3.0 loses and rewrites nothing: accounts, tokens, the Google client,
areas, VIPs and settings stay byte-identical; the writer settings appear with their defaults. The app
options seed only the writer. A database that can't be opened stops the app instead of being replaced.

The 0.2.0 database is built with a frozen copy of the 0.2.0 store (tests/fixtures/store_0_2_0.py). A 0.3.0
database (tests/fixtures/store_0_3_0.py) opens unchanged with 0.3.2 code, which adds the local model options."""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
import api  # noqa: E402
import store  # noqa: E402

TABLES = ("schema_version", "meta", "areas", "accounts", "vips")


def old_store_module():
    spec = importlib.util.spec_from_file_location("store_0_2_0", ROOT / "tests" / "fixtures" / "store_0_2_0.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dump(path):
    """Every row of every table, as stored (raw column values)."""
    db = sqlite3.connect(str(path))
    try:
        return {t: sorted(db.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TABLES}
    finally:
        db.close()


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        old = old_store_module().Store(self.dir)
        old.update_settings({"name": "Sam", "timezone": "Europe/Helsinki", "reply_age_days": 3, "look": "clean"})
        old.set_google_client("123-abc.apps.googleusercontent.com", "GOCSPX-test-secret-value")
        work = old.add_area("Acme", 2)
        work_id = work["id"] if isinstance(work, dict) else work
        self.a1 = old.save_account("personal", "me@example.com", "1//refresh-one", "ya29.access-one", 1_900_000_000.5, 1_800_000_000)
        self.a2 = old.save_account(work_id, "me@acme.example", "1//refresh-two", "ya29.access-two", 1_900_000_100.0, 1_800_000_000)
        old.update_account(self.a2, {"services": {"calendar": True, "mail": False}})
        old.add_vip("Priya N", "priya@acme.example", work_id)
        old.set_last_sync({"at": "2026-10-28T06:30:00+02:00", "ok": True, "errors": []})
        old.set_cached_brief({"date": "2026-10-28", "kind": "Morning brief", "schedule": [],
                              "replies": [{"who": "Bob", "area": "personal", "text": "Lunch?"}], "heads_up": []})
        self.public = (old.settings(), old.areas(), old.accounts(), old.vips(), old.google_client())
        self.secrets = [old.account_secret(a) for a in (self.a1, self.a2)]
        old.close()
        self.before = dump(self.dir / store.DB_NAME)

    def test_new_code_keeps_everything_and_adds_writer_defaults(self):
        s = store.Store(self.dir)
        self.addCleanup(s.close)
        self.assertEqual(dump(self.dir / store.DB_NAME), self.before)   # opening rewrites nothing
        settings, areas, accounts, vips, client = self.public
        self.assertEqual(s.settings(), settings)
        self.assertEqual(s.accounts(), accounts)
        self.assertEqual(s.vips(), vips)
        self.assertEqual(s.google_client(), client)
        self.assertEqual([s.account_secret(a) for a in (self.a1, self.a2)], self.secrets)
        new_areas = s.areas()
        self.assertEqual([{k: v for k, v in a.items() if k != "reads"} for a in new_areas],
                         [{k: v for k, v in a.items() if k != "reads"} for a in areas])
        for old_a, new_a in zip(areas, new_areas):
            self.assertEqual(new_a["reads"], dict(old_a["reads"], writer=True))
        w = s.writer_public()
        self.assertEqual((w["provider"], w["claude"]["key_hint"], w["local"]["base_url"], w["suggest_replies"],
                          w["fallback_to_claude"]), ("off", None, "", True, False))
        self.assertEqual(s.last_writer()["at"], None)

    def test_new_app_serves_the_old_cached_brief(self):
        app = api.App(self.dir)
        self.addCleanup(app.close)
        st = app.state()
        self.assertEqual(len([x for a in st["areas"] for x in a["accounts"]]), 2)
        self.assertEqual(st["writer"]["provider"], "off")
        self.assertEqual(dump(self.dir / store.DB_NAME), self.before)

    def test_options_seed_writes_only_the_writer(self):
        (self.dir / "options.json").write_text(json.dumps(
            {"writer_local_url": "http://192.168.10.157:8080/v1", "writer_local_model": "gemma-4-26b-a4b-it-qat-q4_0"}))
        app = api.App(self.dir)
        self.addCleanup(app.close)
        self.assertEqual(app.store.writer_public()["provider"], "local")
        after = dump(self.dir / store.DB_NAME)
        for t in TABLES:
            if t != "meta":
                self.assertEqual(after[t], self.before[t], t)
        self.assertEqual([r for r in after["meta"] if r[0] != "writer"], self.before["meta"])
        self.assertEqual([r[0] for r in after["meta"] if r[0] == "writer"], ["writer"])

    def test_unreadable_database_stops_instead_of_starting_fresh(self):
        db = self.dir / store.DB_NAME
        for extra in ("-wal", "-shm"):
            p = db.with_name(db.name + extra)
            if p.exists():
                p.unlink()
        db.write_bytes(b"this is not a database" * 100)
        raw = db.read_bytes()
        with self.assertRaises(store.StoreError):
            store.Store(self.dir)
        self.assertEqual(db.read_bytes(), raw)

    def test_newer_database_stops(self):
        db = sqlite3.connect(str(self.dir / store.DB_NAME))
        db.execute("UPDATE schema_version SET version=?", (store.SCHEMA_VERSION + 1,))
        db.commit()
        db.close()
        before = dump(self.dir / store.DB_NAME)
        with self.assertRaises(store.StoreError) as cm:
            store.Store(self.dir)
        self.assertIn("newer Callsheet", str(cm.exception))
        self.assertEqual(dump(self.dir / store.DB_NAME), before)

    def test_server_refuses_to_start(self):
        import server
        (self.dir / store.DB_NAME).write_bytes(b"x" * 4096)
        for extra in ("-wal", "-shm"):
            p = self.dir / (store.DB_NAME + extra)
            if p.exists():
                p.unlink()
        umask = os.umask(0o022)
        os.umask(umask)
        self.addCleanup(os.umask, umask)  # main() sets 077 for the process
        with mock.patch.dict(os.environ, {"CALLSHEET_DATA": str(self.dir)}), \
                mock.patch("sys.stderr") as err, self.assertRaises(SystemExit) as cm:
            server.main(["--port", "0", "--host", "127.0.0.1", "--no-scheduler"])
        self.assertEqual(cm.exception.code, 1)
        logged = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("Callsheet can't start", logged)
        self.assertIn("left unchanged", logged)


class Upgrade030Test(unittest.TestCase):
    """A 0.3.0 database (local writer set up, a cached brief with suggestions) opens unchanged with 0.3.2
    code; the new local options appear with their defaults and the saved writer settings are kept."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("store_0_3_0", ROOT / "tests" / "fixtures" / "store_0_3_0.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = mod.Store(self.dir)
        old.update_settings({"name": "Sam", "timezone": "Europe/Helsinki"})
        old.update_writer({"provider": "local", "local": {"base_url": "http://192.168.10.157:8080/v1",
                                                          "model": "gemma-4-26b-a4b-it-qat-q4_0", "api_key": "local-k-1234"},
                           "claude": {"api_key": "sk-ant-api03-test-key-abcd"}, "fallback_to_claude": True})
        old.set_last_writer({"at": "2026-10-29T06:30:00+02:00", "ok": True, "by": "Local · gemma",
                             "input_tokens": 3000, "output_tokens": 400, "error": None})
        old.set_cached_brief({"date": "2026-10-29", "kind": "Morning brief", "schedule": [], "heads_up": [],
                              "replies": [{"id": "r1", "who": "Bob", "area": "personal", "text": "Lunch?",
                                           "suggestion": "Friday works."}]})
        self.writer_before = old.writer_config()
        old.close()
        self.before = dump(self.dir / store.DB_NAME)

    def test_opens_unchanged_with_local_defaults(self):
        s = store.Store(self.dir)
        self.addCleanup(s.close)
        self.assertEqual(dump(self.dir / store.DB_NAME), self.before)
        w = s.writer_config()
        for k in ("provider", "claude", "fallback_to_claude", "suggest_replies", "configured_by_owner"):
            self.assertEqual(w[k], self.writer_before[k], k)
        for k, v in self.writer_before["local"].items():
            self.assertEqual(w["local"][k], v, k)
        self.assertEqual({k: w["local"][k] for k in ("thinking", "reasoning_budget", "max_tokens", "context", "timeout")},
                         {"thinking": False, "reasoning_budget": 1536, "max_tokens": 4096, "context": 32768,
                          "timeout": 300})
        self.assertEqual(s.last_writer()["input_tokens"], 3000)
        self.assertEqual(s.cached_brief()["replies"][0]["suggestion"], "Friday works.")

    def test_new_app_serves_the_old_brief(self):
        app = api.App(self.dir)
        self.addCleanup(app.close)
        self.assertEqual(app.state()["writer"]["local"]["max_tokens"], 4096)
        b = app.brief()  # the old cached brief (from another day) still composes
        self.assertTrue(all("suggestion_warning" not in r for r in b.get("replies") or []))
        self.assertEqual(dump(self.dir / store.DB_NAME), self.before)


if __name__ == "__main__":
    unittest.main()
