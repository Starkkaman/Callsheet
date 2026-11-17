"""0.21.0 Setup › Advanced › Old data from earlier versions (oldata.py; owner 2026-11-13). An old install's leftovers
(the sorter's, learner's, reader's and picker's tables, the writer evaluation, the old brief's caches, settings
nothing reads, the pre-0.21.0 AI settings copy, index.db) are each found and cleared; everything current is
untouched (a checksum of every kept table before and after); the one-time token; index.db only after its import
recorded completion; nothing clears without the POST, and nothing in the code reads what is offered.
Stdlib only, no network, no model calls."""
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import aisettings  # noqa: E402
import api  # noqa: E402
import background  # noqa: E402
import chat  # noqa: E402
import library  # noqa: E402
import msgstore  # noqa: E402
import oldata  # noqa: E402
from store import Conflict, InputError, Store  # noqa: E402

T0 = 1_794_000_000.0
LOC_KEY = "loc-key-SECRET-7731"
OWN_KEY = "own-chat-key-SECRET-5150"
BG_KEY = "bg-key-SECRET-2468"
CLIENT_SECRET = "GOCSPX-client-SECRET-9911"

# The retired modules' tables, as they created them (triage.py, learn.py, reader.py, picker.py before 8b6e4b3)
OLD_SCHEMA = """
CREATE TABLE triage_labels (key TEXT PRIMARY KEY, src TEXT NOT NULL, hash TEXT NOT NULL, day TEXT NOT NULL,
  at REAL NOT NULL, status TEXT NOT NULL, pre_kind TEXT NOT NULL, strength TEXT NOT NULL, grp TEXT NOT NULL,
  why TEXT NOT NULL, dup TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]');
CREATE INDEX triage_labels_dup ON triage_labels (dup);
CREATE TABLE triage_corrections (id INTEGER PRIMARY KEY, at REAL NOT NULL, key TEXT NOT NULL, action TEXT NOT NULL);
CREATE TABLE review_counts (week TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL, label TEXT NOT NULL,
  truth TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (week, model, prompt, label, truth));
CREATE TABLE triage_queue (key TEXT PRIMARY KEY, src TEXT NOT NULL, origin TEXT NOT NULL, ix INTEGER,
  dup TEXT NOT NULL, hash TEXT NOT NULL, ts REAL NOT NULL, queued REAL NOT NULL);
CREATE INDEX triage_queue_order ON triage_queue (ts, queued);
CREATE TABLE learn_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, action TEXT NOT NULL,
  label TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '');
CREATE TABLE reader_items (key TEXT PRIMARY KEY, hash TEXT NOT NULL, day TEXT NOT NULL, at REAL NOT NULL,
  state TEXT NOT NULL, gist TEXT);
CREATE TABLE reader_queue (key TEXT PRIMARY KEY, hash TEXT NOT NULL, origin TEXT NOT NULL, ts REAL NOT NULL,
  queued REAL NOT NULL);
CREATE TABLE reader_counts (day TEXT NOT NULL, field TEXT NOT NULL, outcome TEXT NOT NULL, n INTEGER NOT NULL
  DEFAULT 0, PRIMARY KEY (day, field, outcome));
CREATE TABLE picker_items (key TEXT PRIMARY KEY, hash TEXT NOT NULL, day TEXT NOT NULL, at REAL NOT NULL,
  origin TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE picker_queue (key TEXT PRIMARY KEY, hash TEXT NOT NULL, origin TEXT NOT NULL, ts REAL NOT NULL,
  queued REAL NOT NULL);
CREATE TABLE picker_prefs (id TEXT PRIMARY KEY, target TEXT NOT NULL UNIQUE, way TEXT NOT NULL, added REAL NOT NULL);
"""
OLD_TABLES = ("triage_labels", "triage_corrections", "review_counts", "triage_queue", "learn_events", "reader_items",
              "reader_queue", "reader_counts", "picker_items", "picker_queue", "picker_prefs")
OLD_KEYS = {"sorting_ix": {"indexed_at": 5}, "sorting_backfill": {"done": True}, "sorting_review_day": {"d": 1},
            "sorting_rescued_logged": "2026-11-01", "reader_day": {"day": "2026-11-01"},
            "reader_counts_logged": "2026-11-01", "picker": {"on": True}, "picker_day": {"cards": 3},
            "picker_perf": {"models": {"g": [1.0]}}, "eval": {"contenders": [{"id": "c1", "model": "x"}]},
            "brief": {"rows": [{"text": "Old brief row"}]}, "done": {"k1": {"last": "x"}},
            "seen_day": {"date": "2026-11-01"}, "see_ics": {"m1": {}}, "see_promos": {"t1": {}},
            "see_snapshots": {"base": {}}, "horizon_marks": {"k": "done"}}
EXPECTED = {"sorter", "learner", "reader", "picker", "eval", "brief", "settings", "ai_before", "index"}


def make_index(path):
    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT, account TEXT, source_id TEXT, kind TEXT, "
        "thread TEXT, title TEXT, people TEXT, area TEXT, ts REAL, url TEXT, snippet TEXT, body TEXT, hash TEXT, "
        "indexed_at REAL, own INTEGER DEFAULT 0);")
    for r in [(7, "drive", "acc1", "f1", "doc", "Q4 roadmap", "launch in spring"),
              (8, "obsidian", "vault", "Kitchen.md", "note", "Kitchen", "call Quinn"),
              (12, "gmail", "acc1", "m-old", "email", "Lease", "Signed, see attached.")]:
        db.execute("INSERT INTO items (id, source, account, source_id, kind, title, body, people, area, ts, url, "
                   "snippet, hash, indexed_at, thread) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', '', 'h', 1, 't')",
                   r + (T0 - 400 * 86400,))
    db.execute("INSERT INTO meta VALUES ('profile', ?)", (json.dumps({"saved": {"projects": {"j1": {"n": "Atlas"}}}}),))
    db.commit()
    db.close()


def make_old_install(d):
    """callsheet.db as an install from before the rework left it, plus index.db and writer-eval/."""
    d = pathlib.Path(d)
    make_index(d / "index.db")
    run = d / "writer-eval" / "20261101T060000Z"
    run.mkdir(parents=True)
    (run / "payload.txt").write_text("brief data " * 50)
    (run / "run.json").write_text("{}")
    st = Store(d)
    st.update_settings({"name": "Sam", "timezone": "UTC"})
    st.set_google_client("123-abc.apps.googleusercontent.com", CLIENT_SECRET)
    st.add_vip("Dana", "dana@example.com", "personal")
    db = st.db
    db.executescript(OLD_SCHEMA)
    for i in range(30):
        db.execute("INSERT INTO triage_labels (key, src, hash, day, at, status, pre_kind, strength, grp, why) VALUES "
                   "(?, 'mail', 'h', '2026-11-01', 1, 'ok', 'new', 'weak', 'g', 'w')", (f"k{i}",))
        db.execute("INSERT INTO learn_events (at, action, label) VALUES (1, 'more', 'x')")
    db.execute("INSERT INTO reader_items VALUES ('r1', 'h', '2026-11-01', 1, 'read', 'Gist-SECRET')")
    db.execute("INSERT INTO picker_prefs VALUES ('p1', 'harbour', 'more', 1)")
    for k, v in OLD_KEYS.items():
        st._set_meta(k, v)  # noqa: SLF001
    s = st._get_meta("settings")  # noqa: SLF001
    st._set_meta("settings", dict(s, reply_age_days=2))  # noqa: SLF001
    st._set_meta("writer", {"provider": "local", "local": {"base_url": "http://127.0.0.1:9", "model": "old-model",  # noqa: SLF001
                                                           "api_key": LOC_KEY, "preset_note": "x"},
                            "presets": [{"id": "careful", "model": "big-model"}], "call_presets": {"morning": "careful"},
                            "reload_fast": True, "configured_by_owner": True})
    st._set_meta("chat", {"provider": "local", "local": {"base_url": "http://127.0.0.1:8", "api_key": OWN_KEY,  # noqa: SLF001
                                                         "model": "chat-model", "timeout": 300}, "v062": True})
    st._set_meta("sorting", {"mode": "on", "model": "old-model", "batch": 8, "shadow_since": "2026-10-01",  # noqa: SLF001
                             "base_url": "http://127.0.0.1:7", "api_key": BG_KEY, "ctx_sizes": {"old-model": 32768},
                             "backfill": {"days": 30, "windows": [["01:00", "05:00"]]}})
    st._set_meta("reader", {"on": True, "blocked": [["06:00", "07:00"]], "audit": [], "date_order": "dmy"})  # noqa: SLF001
    st._set_meta("suppress", [{"id": "s1", "kind": "sender", "value": "x@y"}])  # noqa: SLF001 (push still reads it)
    st._set_meta("brief_refs", {"r1": {"k": 1}})  # noqa: SLF001 (gdrafts still reads it)
    st._set_meta("sorting_day", {"day": "2026-11-01", "cards": 1})  # noqa: SLF001 (background still reads it)
    st.close()


def join_backfill():
    for t in threading.enumerate():
        if t.name == "callsheet-doc-flags":
            t.join(30)


def table_sums(path, skip=()):
    """{table: sha256 of its rows} for every table of a database file but `skip`."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if t in skip or t.endswith(("_data", "_idx", "_docsize", "_config", "_content")):
                continue
            rows = sorted(repr(tuple(r)) for r in db.execute(f'SELECT * FROM "{t}"'))
            out[t] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
        return out
    finally:
        db.close()


class OldInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)
        make_old_install(self.d)
        self.app = api.App(self.tmp.name, clock=lambda: T0)
        self.addCleanup(self.app.close)
        join_backfill()
        self.app.messages.upsert_messages([{"source": "gmail", "account": "acc1", "msg_id": "m1", "thread": "t1",
                                            "direction": "in", "sender": "eli@venue.example", "ts": T0,
                                            "subject": "Venue", "text": "Can you confirm?"}])

    def current(self):
        st = self.app.store
        return {"settings": st.settings(), "writer": st.writer_config(), "chat": chat.load_settings(st),
                "bg": self.app.background.settings(), "date_order": background.date_order(st),
                "client": st.google_client(), "vips": st.vips(), "suppress": st.suppress_rules(),
                "brief_refs": st.brief_refs(), "ai": aisettings.settings(st), "ai_view": aisettings.view(st, self.app.background)}

    def kept_meta(self):
        with self.app.store.lock:
            return {k: v for k, v in self.app.store.db.execute("SELECT key, value FROM meta")
                    if k not in OLD_KEYS and k not in ("settings", "writer", "chat", "sorting", "reader", "ai")}

    def test_each_group_is_found_and_cleared_and_current_data_is_untouched(self):
        cdb, mdb = self.d / "callsheet.db", self.d / "messages.db"
        kept_before = table_sums(cdb, skip=OLD_TABLES + ("meta",))
        msgs_before = table_sums(mdb)
        meta_before, cur_before = self.kept_meta(), self.current()
        self.assertIn("meta", table_sums(cdb))

        v = self.app.handle("GET", "old-data", None)
        self.assertEqual({g["id"] for g in v["groups"]}, EXPECTED)
        by = {g["id"]: g for g in v["groups"]}
        self.assertEqual({it["name"] for it in by["sorter"]["items"]},
                         {"triage_labels", "triage_corrections", "review_counts", "triage_queue", "sorting_ix",
                          "sorting_backfill", "sorting_review_day", "sorting_rescued_logged"})
        self.assertEqual(by["sorter"]["rows"], 30)
        self.assertEqual(by["learner"]["rows"], 30)
        self.assertGreater(by["sorter"]["bytes"], 0)
        self.assertEqual({it["name"] for it in by["eval"]["items"]}, {"eval", "writer-eval/"})
        self.assertEqual(next(it for it in by["eval"]["items"] if it["kind"] == "folder")["count"], 2)
        names = {it["name"] for it in by["settings"]["items"]}
        for want in ("settings: reply_age_days", "sorting: mode", "sorting: batch", "sorting: shadow_since",
                     "sorting: base_url", "sorting: api_key", "sorting: backfill.days", "reader: on", "reader: blocked",
                     "reader: audit"):
            self.assertIn(want, names)
        for kept in ("sorting: model", "sorting: ctx_sizes", "sorting: backfill.windows", "reader: date_order",
                     "settings: name", "settings: timezone", "writer: local.api_key", "chat: v062", "chat: local.model"):
            self.assertNotIn(kept, names)
        # aisettings.migrate already moved Chat's own server and the presets (writer and chat saved in the new shape)
        self.assertFalse(any(n.startswith(("writer: presets", "chat: local.base_url")) for n in names))
        self.assertEqual(by["index"]["imported"]["documents"], 2)
        self.assertEqual(by["index"]["imported"]["messages"], 1)
        self.assertEqual(by["index"]["items"][0], {"name": "index.db", "kind": "file", "count": 3, "unit": "row",
                                                   "bytes": (self.d / "index.db").stat().st_size})
        self.assertEqual(v["total"]["count"], sum(g["count"] for g in v["groups"]))
        text = json.dumps(v)
        for secret in (LOC_KEY, OWN_KEY, BG_KEY, CLIENT_SECRET, "Old brief row", "Gist-SECRET", "big-model"):
            self.assertNotIn(secret, text)   # names only, never values

        out = self.app.handle("POST", "old-data/clear", {"token": v["token"], "groups": sorted(EXPECTED)})
        self.assertEqual(out["errors"], [])
        self.assertEqual(set(out["cleared"]["groups"]), EXPECTED)
        self.assertEqual(out["groups"], [])
        self.assertIsNone(out["token"])
        self.assertEqual(self.app.handle("GET", "old-data", None)["groups"], [])

        self.assertFalse((self.d / "index.db").exists())
        self.assertFalse((self.d / "writer-eval").exists())
        with self.app.store.lock:
            tables = {r[0] for r in self.app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            keys = {r[0] for r in self.app.store.db.execute("SELECT key FROM meta")}
            seq = [r[0] for r in self.app.store.db.execute("SELECT name FROM sqlite_sequence")]
        self.assertEqual(tables & set(OLD_TABLES), set())
        self.assertEqual(keys & set(OLD_KEYS), set())
        self.assertNotIn("learn_events", seq)
        self.assertNotIn("before", self.app.store._get_meta("ai"))  # noqa: SLF001
        self.assertNotIn("reply_age_days", self.app.store._get_meta("settings"))  # noqa: SLF001
        self.assertEqual(self.app.store._get_meta("sorting")["backfill"], {"windows": [["01:00", "05:00"]]})  # noqa: SLF001

        # everything current is as it was: every kept table, every other meta row, every value the code reads
        self.assertEqual(table_sums(cdb, skip=OLD_TABLES + ("meta",)), kept_before)
        self.assertEqual(table_sums(mdb), msgs_before)
        self.assertEqual(self.kept_meta(), meta_before)
        self.assertEqual(self.current(), cur_before)
        self.assertEqual(self.app.index.get(7)["title"], "Q4 roadmap")        # imported documents keep their ids
        self.assertEqual(self.app.index.get(12)["title"], "Lease")            # and old message ids still map

    def test_only_the_ticked_groups_go(self):
        v = self.app.handle("GET", "old-data", None)
        out = self.app.handle("POST", "old-data/clear", {"token": v["token"], "groups": ["learner", "settings"]})
        self.assertEqual({g["id"] for g in out["groups"]}, EXPECTED - {"learner", "settings"})
        self.assertTrue((self.d / "index.db").exists())
        self.assertTrue((self.d / "writer-eval").is_dir())
        with self.app.store.lock:
            self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM triage_labels").fetchone()[0], 30)

    def test_the_token(self):
        cdb = self.d / "callsheet.db"
        whole = table_sums(cdb)
        v = self.app.handle("GET", "old-data", None)
        for body in ({"groups": ["learner"]}, {"token": "nope", "groups": ["learner"]}, {"token": None, "groups": ["learner"]}):
            v = self.app.handle("GET", "old-data", None)
            with self.assertRaises(Conflict):
                self.app.handle("POST", "old-data/clear", body)
            with self.assertRaises(Conflict):     # a wrong try uses the right token up too
                self.app.handle("POST", "old-data/clear", {"token": v["token"], "groups": ["learner"]})
        old = self.app.handle("GET", "old-data", None)["token"]
        new = self.app.handle("GET", "old-data", None)["token"]
        self.assertNotEqual(old, new)
        with self.assertRaises(Conflict):         # only the latest list's token
            self.app.handle("POST", "old-data/clear", {"token": old, "groups": ["learner"]})
        t = self.app.handle("GET", "old-data", None)["token"]
        with self.assertRaises(InputError):
            self.app.handle("POST", "old-data/clear", {"token": t, "groups": []})
        t = self.app.handle("GET", "old-data", None)["token"]
        with self.assertRaises(InputError):       # not in the list
            self.app.handle("POST", "old-data/clear", {"token": t, "groups": ["messages"]})
        with self.assertRaises(Conflict):         # and that used it
            self.app.handle("POST", "old-data/clear", {"token": t, "groups": ["learner"]})
        now = [T0]
        self.app.oldata.clock = lambda: now[0]
        t = self.app.handle("GET", "old-data", None)["token"]
        now[0] += oldata.TOKEN_S + 1
        with self.assertRaises(Conflict):         # 10 minutes
            self.app.handle("POST", "old-data/clear", {"token": t, "groups": ["learner"]})
        self.assertEqual(table_sums(cdb), whole)
        t = self.app.handle("GET", "old-data", None)["token"]
        self.app.handle("POST", "old-data/clear", {"token": t, "groups": ["learner"]})
        with self.assertRaises(Conflict):         # once
            self.app.handle("POST", "old-data/clear", {"token": t, "groups": ["picker"]})

    def test_nothing_clears_without_the_post(self):
        cdb = self.d / "callsheet.db"
        whole = table_sums(cdb)
        files = sorted(str(p.relative_to(self.d)) for p in self.d.rglob("*") if "writer-eval" in str(p) or
                       p.name == "index.db")
        for _ in range(3):
            self.app.handle("GET", "old-data", None)
        with self.assertRaises(api.MethodNotAllowed):
            self.app.handle("GET", "old-data/clear", None)
        self.app.close()
        again = api.App(self.tmp.name, clock=lambda: T0)   # a restart (and so an upgrade) clears nothing
        self.addCleanup(again.close)
        join_backfill()
        again.handle("GET", "old-data", None)
        self.assertEqual(table_sums(cdb), whole)
        self.assertEqual(sorted(str(p.relative_to(self.d)) for p in self.d.rglob("*") if "writer-eval" in str(p) or
                                p.name == "index.db"), files)


class Offers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)
        self.st = Store(self.d)
        self.addCleanup(self.st.close)
        self.ms = msgstore.MessageStore(self.d, clock=lambda: T0)
        self.addCleanup(self.ms.close)
        self.od = oldata.OldData(self.st, lambda: self.ms, clock=lambda: T0)

    def ids(self):
        return {g["id"] for g in self.od.view()["groups"]}

    def test_a_new_install_has_nothing(self):
        self.st.update_settings({"name": "Sam"})
        self.assertEqual(self.od.view(), {"groups": [], "token": None, "total": {"count": 0, "bytes": 0}})

    def test_index_db_only_after_its_import_is_complete(self):
        make_index(self.d / "index.db")
        self.assertEqual(self.ids(), set())                                  # no import recorded yet
        lib = library.Library(self.ms)
        lib.import_index(self.d, log=lambda line: None)
        self.assertEqual(self.ids(), set())                                  # the documents only, not the messages
        self.assertTrue(self.ms.meta(library.IMPORT_KEY)["file"])
        lib.import_index_messages(self.d, log=lambda line: None)
        g = self.od.view()["groups"]
        self.assertEqual([x["id"] for x in g], ["index"])
        self.assertEqual(g[0]["imported"], {"documents": 2, "old_ids": 0, "settings": 1, "messages": 1,
                                            "already_stored": 0})
        # a marker written when index.db wasn't there: index.db appeared later and was never read
        self.ms.set_meta(library.IMPORT_KEY, {"at": T0, "docs": 0, "ids": 0, "meta": 0, "file": False})
        self.assertEqual(self.ids(), set())
        # markers from before "file" was recorded: confirmed only by what came over
        self.ms.set_meta(library.IMPORT_KEY, {"at": T0, "docs": 0, "ids": 0, "meta": 0})
        self.ms.set_meta(library.MSG_IMPORT_KEY, {"at": T0, "messages": 0, "same": 0, "bad": 0})
        self.assertEqual(self.ids(), set())
        self.ms.set_meta(library.IMPORT_KEY, {"at": T0, "docs": 2, "ids": 0, "meta": 1})
        self.assertEqual(self.ids(), {"index"})
        self.od._messages = lambda: None                                     # no message store: not offered
        self.assertEqual(self.ids(), set())

    def test_a_key_the_upgrade_still_reads_waits_for_it(self):
        self.st._set_meta("writer", {"provider": "local", "presets": [{"id": "careful", "model": "m"}],  # noqa: SLF001
                                     "call_presets": {}, "reload_fast": True})
        self.st._set_meta("chat", {"local": {"base_url": "http://127.0.0.1:8", "api_key": OWN_KEY}})  # noqa: SLF001
        names = {it["name"] for g in self.od.view()["groups"] for it in g["items"]}
        # reload_fast isn't read by aisettings.migrate; an empty call_presets changes nothing it does
        self.assertEqual(names, {"writer: reload_fast", "writer: call_presets"})
        notes = aisettings.migrate(self.st, lambda: T0)
        self.assertTrue(notes)
        self.assertGreaterEqual(aisettings.settings(self.st)["v"], aisettings.VERSION)
        self.st._set_meta("writer", dict(self.st._get_meta("writer"), presets=[{"id": "x"}]))  # noqa: SLF001
        self.st._set_meta("chat", {"local": {"base_url": "http://127.0.0.1:8", "api_key": OWN_KEY}})  # noqa: SLF001
        names = {it["name"] for g in self.od.view()["groups"] for it in g["items"]}
        self.assertTrue({"writer: presets", "chat: local.base_url", "chat: local.api_key", "ai: before"} <= names)


class ReadByNothing(unittest.TestCase):
    """What oldata offers is read by no code, and nothing but the owner's POST reaches it."""

    @classmethod
    def setUpClass(cls):
        cls.src = {p.name: p.read_text(encoding="utf-8") for p in APP.glob("*.py") if p.name != "oldata.py"}

    def test_old_tables_are_read_by_nothing(self):
        for g in oldata.GROUPS:
            for t in g[3]:
                for name, s in self.src.items():
                    self.assertIsNone(re.search(r"\b" + t + r"\b", s), f"{name} mentions {t}")

    def test_old_meta_keys_are_read_by_nothing(self):
        dead = {"cached_brief", "done_keys", "seen_day", "see_meta"}   # store.py's readers of them, called by nothing
        for g in oldata.GROUPS:
            for k in g[4]:
                for name, s in self.src.items():
                    self.assertIsNone(re.search(r"""_meta\(\s*["']""" + k + """["']""", s) if name != "store.py"
                                      else None, f"{name} reads {k}")
                    self.assertNotIn(f'"{k}_perf"' if k == "picker" else f'perf_get(self.store, "{k}"', s)
        store_src = self.src["store.py"]
        for fn in dead:
            for name, s in self.src.items():
                calls = re.findall(r"\." + fn + r"\(", s)
                if name == "store.py":
                    calls = [c for c in re.findall(r"self\." + fn + r"\(", s)]
                    if fn == "done_keys":   # only set_done and clear_done, which nothing calls
                        self.assertIsNone(re.search(r"\.(set_done|clear_done)\(", "".join(
                            v for n, v in self.src.items() if n != "store.py")))
                        continue
                self.assertEqual(calls, [], f"{name} calls {fn}")
        for k in ("brief", "done", "seen_day"):
            self.assertIn(f'_get_meta("{k}"', store_src)

    def test_only_the_api_route_reaches_it(self):
        users = sorted(n for n, s in self.src.items() if re.search(r"import oldata|oldata\.(?!py)|old-data", s))
        # 0.22.0 (owner 2026-11-16, Clear old data by Chat: "Yes, with Confirm"): Chat's card reaches it through the
        # action registry, only on the owner's Confirm (tests/test_chat_actions_0220.py)
        self.assertEqual(users, ["actions.py", "api.py", "chat.py"])
        a = self.src["api.py"]
        # made once, its two routes, and (0.22.0) handed to the action registry and Chat for the Confirm card
        self.assertEqual(len(re.findall(r"self\.oldata\b", a)), 4)
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"api\('POST', 'api/old-data/clear'", js)), 1)
        for name in ("prefs.py", "scheduler.py", "background.py", "server.py"):
            self.assertNotIn("old_data", self.src[name])
        # 0.22.0: in Chat only as the clear_old_data card, which is always the owner's Confirm (WHY_DESTRUCTIVE)
        self.assertIn('if card["action"] == "clear_old_data":\n        return "confirm", WHY_DESTRUCTIVE',
                      self.src["chatstore.py"])

    def test_the_device_list_keeps_every_key_the_page_uses(self):
        """On this device: a callsheet.* key is offered only when it isn't one the page reads (odKept)."""
        js = (APP / "static" / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r"\b(KEY_[A-Z]+) = 'callsheet\.", js))
        kept = set(re.search(r"function odKept\(\) \{ return \[([^\]]*)\]", js).group(1).replace(" ", "").split(","))
        self.assertTrue(used)
        self.assertEqual(kept, used)
        self.assertNotIn("removeItem", js[js.index("function odDeviceKeys"):js.index("function odDevice()")])

    def test_no_route_lists_values(self):
        o = oldata.OldData.__dict__
        self.assertIn("_public", o)
        self.assertEqual(sorted(oldata.OldData._public({"id": "x", "label": "l", "why": "w", "items": [
            {"name": "n", "kind": "setting", "count": 1, "unit": "setting", "bytes": 3, "ref": ("setting", "chat", ("k",))}]})
            ["items"][0]), ["bytes", "count", "kind", "name", "unit"])


if __name__ == "__main__":
    unittest.main()
