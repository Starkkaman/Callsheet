"""0.21.0: messages.db as the one library (library.py). It replaced 0.6.0's index.db (index.py, scan.py); the rules
of the old index that still hold are checked here: idempotent documents, ranking and highlights, the relaxed retry,
filters, odd queries, Drive (the incremental read-only grant, reading files), the notes folder (validation, parsing,
deleted notes leave), and the one-time import from an existing index.db (nothing the owner had is lost).
Stdlib only, no network: Google is fake_google_index.FakeGoogleIndex."""
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import api  # noqa: E402
import google  # noqa: E402
import library  # noqa: E402
import live  # noqa: E402
import msgstore  # noqa: E402
import sources  # noqa: E402
from fake_google import CLIENT_ID, CLIENT_SECRET, FULL_SCOPE  # noqa: E402
from fake_google_index import DRIVE_SCOPE, FakeGoogleIndex  # noqa: E402
from store import InputError  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
DAY = 86400


def doc(sid, title="", body="", people="", ts=None, source="drive", account="acc1", area="personal", **kw):
    d = {"source": source, "account": account, "source_id": sid, "kind": "doc", "title": title, "body": body,
         "people": people, "area": area, "ts": ts if ts is not None else T0.timestamp(), "url": "https://x.example/" + sid}
    d.update(kw)
    return d


def mail(mid, subject, text, ts=None, account="acc1", sender="eli@venue.example", direction="in", **kw):
    m = {"source": "gmail", "account": account, "msg_id": mid, "thread": "t" + mid, "direction": direction,
         "sender": sender, "sender_name": "Eli Tellez", "recipients": [{"name": "Sam", "address": "me@example.com"}],
         "ts": ts if ts is not None else T0.timestamp(), "subject": subject, "text": text}
    m.update(kw)
    return m


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: T0.timestamp())
        self.addCleanup(self.ms.close)
        self.lib = library.Library(self.ms)

    def test_documents_are_idempotent_and_only_documents(self):
        rows = [doc("d1", "Budget sign-off", "Priya needs the Q4 budget"), doc("d2", "Plumber", "Home at 4")]
        self.assertEqual(self.lib.upsert(rows), {"added": 2, "updated": 0, "same": 0, "bad": 0})
        first = self.lib.get(self.lib.search("budget")[0]["id"])
        self.assertEqual(self.lib.upsert(rows), {"added": 0, "updated": 0, "same": 2, "bad": 0})
        self.assertEqual(self.lib.get(first["id"]), first)
        self.assertEqual(self.lib.count(), 2)
        self.assertEqual(self.lib.upsert([doc("d1", "Budget sign-off", "moved to the roadmap review")])["updated"], 1)
        self.assertEqual(self.lib.search("Q4"), [])            # the FTS row follows the update
        self.assertEqual(len(self.lib.search("roadmap")), 1)
        # messages never go in as documents; a bad item is counted, not stored
        self.assertEqual(self.lib.upsert([doc("x", source="gmail"), {"source": "BAD", "source_id": "x"}, "no"])["bad"], 3)
        self.assertLess(first["id"], msgstore.STORE_ID_BASE)
        self.assertFalse((pathlib.Path(self.tmp.name) / "index.db").exists())

    def test_one_search_over_messages_events_and_documents(self):
        self.ms.upsert_messages([mail("m1", "Kitchen renovation quote", "see attached", ts=T0.timestamp() - DAY)])
        self.ms.upsert_events([{"account": "acc1", "calendar": "primary", "event_id": "e1_20261030", "title":
                                "Kitchen walkthrough", "start_ts": T0.timestamp() + DAY, "end_ts": T0.timestamp() + DAY,
                                "attendees": [{"name": "Robin", "email": "robin@x.example"}]}])
        self.lib.upsert([doc("a", "Weekly notes", "we talked about the kitchen renovation and more", source="notes"),
                         doc("c", "Lunch", "nothing here", people="Kitchen Crew <crew@x.example>")])
        res = self.lib.search("kitchen")
        self.assertEqual({r["source"] for r in res}, {"gmail", "calendar", "notes", "drive"})
        self.assertEqual(res[0]["title"], "Kitchen renovation quote")      # subject x8 first
        hl = res[0]["highlight"]
        self.assertTrue(any(t.lower() == "kitchen" and on for t, on in hl), hl)
        self.assertNotIn("\x02", res[0]["snippet"])
        self.assertEqual(len(self.lib.search("kitch")), 4)                  # the last word is a prefix
        self.assertEqual([r["source"] for r in self.lib.search("", source="calendar")], ["calendar"])
        ev = self.lib.search("", source="calendar")[0]
        self.assertEqual((ev["thread"], ev["kind"]), ("e1", "event"))      # a series id for Projects
        self.assertEqual(self.lib.get(ev["id"])["title"], "Kitchen walkthrough")
        m = self.lib.search("quote")[0]
        self.assertGreaterEqual(m["id"], msgstore.STORE_ID_BASE)
        self.assertEqual(self.lib.get(m["id"])["body"], "see attached")
        st = self.lib.stats()
        self.assertEqual((st["total"], set(st["sources"])), (4, {"gmail", "calendar", "notes", "drive"}))
        meta = list(self.lib.rows())
        self.assertEqual([r["ts"] for r in meta], sorted(r["ts"] for r in meta))   # oldest first
        g = next(r for r in meta if r["source"] == "gmail")
        self.assertEqual((g["kind"], g["people"]), ("email", "Eli Tellez <eli@venue.example>; Sam <me@example.com>"))

    def test_relaxed_retry_filters_and_odd_queries(self):
        self.ms.upsert_messages([mail("m1", "Your appointment is confirmed", "Chen Family Dental, Friday 2:30 pm"),
                                 mail("m2", "Invoice", "pay", account="acc2", ts=T0.timestamp() - 400 * DAY)])
        self.lib.upsert([doc("d1", "Dentist - Dr. Chen", "Check-up and cleaning", source="notes"),
                         doc("d2", "Invoice", "pay", people="Jonas <jonas@mlw.example>", area="work")])
        res = self.lib.search("dentist appointment")
        self.assertEqual({r["title"] for r in res}, {"Dentist - Dr. Chen", "Your appointment is confirmed"})
        self.assertTrue(all(r["relaxed"] for r in res))
        self.assertTrue(all("relaxed" not in r for r in self.lib.search("dentist chen")))   # AND still wins
        self.assertEqual(self.lib.search("zebra"), [])
        self.assertEqual(self.lib.search('"dentist appointment"'), [])
        self.assertEqual(self.lib.search("dentist appointment", offset=1), [])
        self.assertEqual(len(self.lib.search("invoice")), 2)
        self.assertEqual(len(self.lib.search("invoice", source="gmail")), 1)
        self.assertEqual(len(self.lib.search("invoice", account="acc2")), 1)
        self.assertEqual(len(self.lib.search("invoice", area="work")), 1)
        self.assertEqual(len(self.lib.search("invoice", people="jonas")), 1)
        self.assertEqual(len(self.lib.search("invoice", since=T0.timestamp() - 30 * DAY)), 1)
        self.assertEqual(self.lib.search("invoice", people="100%_"), [])
        for q in ('"', 'NEAR(a b)', '*', 'a OR', '-x', 'title:hello', "'; DROP TABLE docs; --", "()", "^", "x" * 600):
            self.assertIsInstance(self.lib.search(q), list, q)
        self.assertEqual(msgstore.fts_query('title:hello "big deal" NOT'), '"big deal" "title" "hello"*')

    def test_body_cap_delete_and_missing(self):
        self.lib.upsert([doc("d1", "Long", "word " * 5000), doc("n1", "A", source="notes", account="vault"),
                         doc("n2", "B", source="notes", account="vault")])
        got = self.lib.get(self.lib.search("long")[0]["id"])
        self.assertLessEqual(len(got["body"]), msgstore.MAX_BODY + 10)
        self.assertTrue(got["body"].endswith(msgstore.SHORTENED))
        self.assertIsNone(self.lib.get(999999))
        self.assertIsNone(self.lib.get("x"))
        self.assertEqual(self.lib.delete_missing("notes", "vault", {"n1"}), 1)
        self.assertEqual(self.lib.count("notes"), 1)
        self.assertEqual(self.lib.delete("drive"), 1)
        self.assertEqual(self.lib.count(), 1)


class ImportTest(unittest.TestCase):
    """The upgrade: an existing index.db's documents, notes, feeds, reports and Know me state come over, once."""

    def make_index(self, path):
        db = sqlite3.connect(str(path))
        db.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT, account TEXT, source_id TEXT, kind TEXT, "
            "thread TEXT, title TEXT, people TEXT, area TEXT, ts REAL, url TEXT, snippet TEXT, body TEXT, hash TEXT, "
            "indexed_at REAL, instr TEXT DEFAULT '', instr_v INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, "
            "own INTEGER DEFAULT 0);")
        rows = [(7, "drive", "acc1", "f1", "doc", "Q4 roadmap", "launch in spring"),
                (8, "obsidian", "vault", "Projects/Kitchen.md", "note", "Kitchen renovation", "call Quinn"),
                (9, "news", "c1", "n1", "article", "Harbour news", "the ferry"),
                (10, "reports", "c2", "r1.md", "report", "Printer report", "printer firmware 2.1"),
                (11, "github", "o/r", "pr1", "pr_open", "Fix login", ""),
                (12, "gmail", "acc1", "m1", "email", "Venue", "Can you confirm?"),
                (13, "gmail", "acc1", "gone", "email", "Old mail", "only in the old index")]
        for r in rows:
            db.execute("INSERT INTO items (id, source, account, source_id, kind, title, body, people, area, ts, url, "
                       "snippet, hash, indexed_at, thread) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', '', 'h', 1, '')",
                       r + (T0.timestamp(),))
        db.execute("INSERT INTO meta VALUES ('profile', ?)", (json.dumps({"saved": {"projects": {"j1": {"name": "Atlas"}}}}),))
        db.execute("INSERT INTO meta VALUES ('settings', ?)", (json.dumps({"vault": "/share/Notes",
                                                                          "sources": {"obsidian": False}}),))
        db.execute("INSERT INTO meta VALUES ('scopes', ?)", (json.dumps({"acc1": "drive"}),))
        db.execute("INSERT INTO meta VALUES ('job', '{}')")
        db.commit()
        db.close()

    def test_import_once_keeps_ids_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "index.db"
            self.make_index(path)
            before = path.read_bytes()
            ms = msgstore.MessageStore(d, clock=lambda: T0.timestamp())
            self.addCleanup(ms.close)
            ms.upsert_messages([mail("m1", "Venue", "Can you confirm?")])
            ms.set_meta("scopes", {"acc1": "newer"})
            lib = library.Library(ms)
            done = lib.import_index(d, log=lambda line: None)
            self.assertEqual((done["docs"], done["ids"]), (5, 1))
            self.assertEqual(lib.get(7)["title"], "Q4 roadmap")                 # the same ids as before
            self.assertEqual((lib.get(8)["source"], lib.get(9)["source"], lib.get(10)["source"]),
                             ("notes", "feed", "report"))
            self.assertEqual(lib.get(12)["title"], "Venue")                     # an old mail id finds its message
            self.assertGreaterEqual(lib.get(12)["id"], msgstore.STORE_ID_BASE)
            self.assertIsNone(lib.get(13))                                      # mail only the index had
            self.assertEqual(lib.search("printer firmware")[0]["id"], 10)
            self.assertEqual(lib.meta("profile")["saved"]["projects"]["j1"]["name"], "Atlas")   # Know me answers
            self.assertEqual(lib.settings(), {"vault": "/share/Notes", "sources": {"drive": True, "notes": False,
                                                                                   "github": True}})
            self.assertEqual(ms.meta("scopes"), {"acc1": "newer"})              # never overwritten
            self.assertIsNone(ms.meta("job"))
            self.assertIsNone(lib.import_index(d))                              # once
            self.assertEqual(path.read_bytes(), before)                         # index.db only read, kept

    def test_older_messages_are_kept_once_read_only(self):
        """Every message is kept: the mail, Slack and Signal the 0.6.0 scan read further back than the live readers'
        backfill come over once, with their source, thread and old ids; what the store has is left as it is."""
        old = T0.timestamp() - 400 * DAY
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "index.db"
            self.make_index(path)
            db = sqlite3.connect(str(path))
            for r in [(20, "gmail", "acc1", "old1", "sent", "t-old", "Lease 2024", "Sam <me@example.com>; "
                       "Eli Tellez <eli@venue.example>", "Signed, see attached.", 0),
                      (21, "slack", "T1", "C1:1700000000.000100", "message", "", "#crew", "Alex", "Load-in at 7", 1),
                      (22, "slack", "T1", "C2:1700000005.000200", "message", "C2:1700000001.000100", "DM with Dana",
                       "Dana; Alex", "Bring the cables", 0),
                      (23, "signal", "signal", "chat1:s2:1700000010.123", "message", "chat1", "Signal with Mo", "Mo",
                       "Dinner Friday?", 0),
                      (24, "signal", "signal", "chat1:s1:1700000020.000", "sent", "chat1", "Signal with Mo", "Mo",
                       "Yes, 8 pm", 1),
                      (25, "gmail", "acc1", "", "email", "t-bad", "No id", "", "", 0)]:
                db.execute("INSERT INTO items (id, source, account, source_id, kind, thread, title, people, body, own, "
                           "area, ts, url, snippet, hash, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', "
                           "'', 'h', 5)", r + (float(r[3].rpartition(":")[2]) if r[1] == "signal" else old + r[0],))
            db.commit()
            db.close()
            before = path.read_bytes()
            ms = msgstore.MessageStore(d, clock=lambda: T0.timestamp())
            self.addCleanup(ms.close)
            ms.upsert_messages([mail("m1", "Venue", "Can you confirm? (the live copy)"),
                                {"source": "signal", "account": "signal", "msg_id": "+4915:1700000010123",
                                 "thread": "d:+4915", "direction": "in", "sender": "+4915", "sender_name": "Mo",
                                 "ts": 1700000010.123, "text": "Dinner Friday?"}])
            lib = library.Library(ms)
            lib.import_index(d, log=lambda line: None)
            done = lib.import_index_messages(d, log=lambda line: None)
            self.assertEqual((done["messages"], done["same"], done["bad"]), (5, 2, 1))   # "gone", old1, 2 Slack, 1 Signal
            rows = {r["msg_id"]: dict(r) for r in ms._read("SELECT * FROM messages")}
            self.assertEqual(len(rows), 7)
            self.assertEqual(rows["m1"]["text"], "Can you confirm? (the live copy)")       # the store's copy unchanged
            self.assertNotIn("chat1:s2:1700000010.123", rows)                              # Signal's live copy: once
            m = rows["old1"]
            self.assertEqual((m["account"], m["thread"], m["direction"], m["sender"], m["subject"], m["text"]),
                             ("acc1", "t-old", "out", "me@example.com", "Lease 2024", "Signed, see attached."))
            self.assertEqual(json.loads(m["recipients"]), [{"name": "Eli Tellez", "address": "eli@venue.example"}])
            self.assertAlmostEqual(m["ts"], old + 20)
            s1, s2 = rows["C1:1700000000.000100"], rows["C2:1700000005.000200"]
            self.assertEqual((s1["thread"], s1["direction"], s1["channel"]), ("C1", "out", "#crew"))
            self.assertEqual((s2["thread"], s2["direction"], s2["sender_name"]), ("C2:1700000001.000100", "in", "Dana"))
            sig = rows["chat1:s1:1700000020.000"]
            self.assertEqual((sig["source"], sig["thread"], sig["direction"], sig["text"]),
                             ("signal", "chat1", "out", "Yes, 8 pm"))
            self.assertEqual(lib.get(20)["title"], "Lease 2024")                # old ids find their messages
            self.assertEqual(lib.get(13)["title"], "Old mail")                  # (the docs import left it unmapped)
            self.assertIn("Dinner", lib.get(23)["body"])                        # mapped to the live copy
            self.assertEqual([r["title"] for r in lib.search("lease")], ["Lease 2024"])
            self.assertIsNone(lib.import_index_messages(d))                     # once
            self.assertEqual(len(ms._read("SELECT id FROM messages")), 7)
            self.assertEqual(path.read_bytes(), before)                         # index.db only read, kept

    def test_older_messages_without_index_db(self):
        with tempfile.TemporaryDirectory() as d:
            ms = msgstore.MessageStore(d)
            self.addCleanup(ms.close)
            lib = library.Library(ms)
            self.assertEqual(lib.import_index_messages(d)["messages"], 0)
            self.assertIsNone(lib.import_index_messages(d))
            self.assertFalse((pathlib.Path(d) / "index.db").exists())


class Env(unittest.TestCase):
    def setUp(self):
        self.now = [T0.timestamp()]
        self.fake = FakeGoogleIndex()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vroot = pathlib.Path(self.tmp.name) / "share"
        self.vroot.mkdir()
        env = mock.patch.dict(os.environ, {**self.fake.env(), sources.VAULT_ROOTS_ENV: str(self.vroot)})
        env.start()
        self.addCleanup(env.stop)
        self.app = api.App(str(pathlib.Path(self.tmp.name) / "data"), clock=lambda: self.now[0])
        self.app.store.update_settings({"timezone": "UTC"})
        self.addCleanup(self.app.close)

    def connect(self, email):
        if not self.app.store.google_client():
            self.app.handle("POST", "google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user(email)
        flow = self.app.handle("POST", "accounts/start", {"area_id": "personal"})
        return self.app.handle("POST", "accounts/finish", {"flow_id": flow["flow_id"],
                                                           "pasted": self.fake.approve(flow["auth_url"], email)})


class DriveTest(Env):
    def setUp(self):
        super().setUp()
        self.acc = self.connect("me@example.com")
        f = self.fake
        f.add_file("me@example.com", "doc1", "Q4 roadmap", "application/vnd.google-apps.document",
                   "2026-10-20T10:00:00.000Z", "Roadmap: launch in spring")
        f.add_file("me@example.com", "sh1", "Budget", "application/vnd.google-apps.spreadsheet",
                   "2026-10-21T10:00:00.000Z", "item,cost\nvenue,18400\n")
        f.add_file("me@example.com", "big", "huge.txt", "text/plain", "2026-10-23T10:00:00.000Z", "x", size=50_000_000)

    def test_no_grant_no_drive_reads(self):
        self.assertIs(self.app.handle("POST", "index/drive/check", {})["accounts"][0]["granted"], False)
        self.assertEqual(self.app.live.readers["drive"].targets(), [])
        self.app.live.run_due("drive", force=True)
        self.assertFalse([r for r in self.fake.requests if r[1].startswith("/drive/")])

    def test_incremental_grant_url_and_finish(self):
        out = self.app.handle("POST", "index/drive/start", {"account_id": self.acc["id"]})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(out["auth_url"]).query)
        self.assertIn(DRIVE_SCOPE, q["scope"][0].split())
        self.assertEqual(q["include_granted_scopes"], ["true"])
        normal = urllib.parse.parse_qs(urllib.parse.urlsplit(google.build_auth_url("id", "s", "c")).query)
        self.assertNotIn(DRIVE_SCOPE, normal["scope"][0])          # new logins don't ask for Drive
        self.fake.scope = FULL_SCOPE + " " + DRIVE_SCOPE
        done = self.app.handle("POST", "index/drive/finish", {"flow_id": out["flow_id"],
                                                               "pasted": self.fake.approve(out["auth_url"], "me@example.com")})
        self.assertTrue(done["granted"])
        st = self.app.handle("GET", "index/status", None)
        self.assertTrue(next(s for s in st["sources"] if s["key"] == "drive")["accounts"][0]["granted"])
        self.assertNotIn("refresh_token", json.dumps(st))

    def test_drive_files_become_documents(self):
        self.fake.scope = FULL_SCOPE + " " + DRIVE_SCOPE
        self.app.handle("POST", "index/drive/check", {})
        res = self.app.live.run_due("drive", force=True)
        self.assertEqual(res[self.acc["id"]]["backlog"], 0)
        by = {r["title"]: self.app.index.get(r["id"]) for r in self.app.index.search("", source="drive", limit=10)}
        self.assertEqual(set(by), {"Q4 roadmap", "Budget", "huge.txt"})
        self.assertIn("launch in spring", by["Q4 roadmap"]["body"])
        self.assertEqual(by["huge.txt"]["body"], "")                # too large: name only
        found = self.app.handle("GET", "index/search", {"q": "roadmap"})["results"]
        self.assertEqual(found[0]["source"], "drive")
        rows = {r["key"]: r for r in self.app.live.status()["sources"]}
        self.assertEqual(rows["drive"]["state"], "current")
        self.app.handle("PATCH", "index/settings", {"sources": {"drive": False}})   # off: what it read leaves
        self.assertEqual(self.app.index.count("drive"), 0)


class NotesTest(Env):
    def vault(self):
        v = self.vroot / "Obsidian" / "Work vault"
        (v / ".obsidian").mkdir(parents=True)
        (v / ".obsidian" / "workspace.md").write_text("# secret config")
        (v / "Projects").mkdir()
        (v / "Projects" / "Kitchen.md").write_text(
            "---\ntitle: Kitchen renovation\ntags: [home, renovation]\ndate: 2026-09-01\narea: Personal\npeople:\n"
            "  - Robin\n---\nCall [[Quinn Grant|Quinn]] about [[Quotes#march]]. #budget\n")
        (v / "Inbox.md").write_text("# Inbox note\nplain text, no front matter\n")
        (v / "plain.txt").write_text("not markdown")
        return v

    def test_validation(self):
        v = self.vault()
        outside = pathlib.Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (self.vroot / "link").symlink_to(outside)
        for bad, words in ((str(outside), "inside /share or /media"), ("relative/path", "full folder path"),
                           (str(self.vroot / "missing"), "can't find"), (str(v / "Inbox.md"), "not a folder"),
                           (str(self.vroot / "link"), "inside /share or /media")):
            with self.assertRaises(InputError, msg=bad) as cm:
                sources.check_vault(bad)
            self.assertIn(words, str(cm.exception))
        with self.assertRaises(InputError):
            self.app.handle("PATCH", "index/settings", {"vault": "/etc"})
        self.assertEqual(self.app.handle("PATCH", "index/settings", {"vault": str(v)})["vault"], str(v))

    def test_notes_read_and_deleted_notes_leave(self):
        v = self.vault()
        self.app.handle("PATCH", "index/settings", {"vault": str(v)})
        self.app.live.run_due("notes", force=True)
        rows = {r["title"]: self.app.index.get(r["id"]) for r in self.app.index.search("", source="notes")}
        self.assertEqual(set(rows), {"Kitchen renovation", "Inbox note"})   # .obsidian and non-.md skipped
        k = rows["Kitchen renovation"]
        for hint in ("Quinn Grant", "Quotes", "Robin", "#home", "#budget"):
            self.assertIn(hint, k["people"])
        self.assertEqual((k["area"], k["url"]), ("personal", os.path.join("Projects", "Kitchen.md")))
        self.assertEqual(self.app.index.search("secret"), [])
        (v / "Inbox.md").unlink()
        self.now[0] += 3600
        self.app.live.run_due("notes", force=True)
        self.assertEqual(self.app.index.count("notes"), 1)
        self.app.handle("PATCH", "index/settings", {"vault": ""})   # changing the folder removes its notes
        self.assertEqual(self.app.index.count("notes"), 0)
        self.assertEqual(os.listdir(v / "Projects"), ["Kitchen.md"])  # the folder itself is never touched


class BulkTest(unittest.TestCase):
    def test_bulk_headers(self):
        self.assertTrue(msgstore.is_bulk({"list-unsubscribe": "<mailto:u@x.example>"}))
        self.assertTrue(msgstore.is_bulk({"precedence": "Bulk"}))
        self.assertTrue(msgstore.is_bulk({"precedence": "list"}))
        self.assertFalse(msgstore.is_bulk({"precedence": "first-class", "from": "a@x.example"}))
        msg = {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "internalDate": "1700000000000",
               "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "News <n@x.example>"},
                                                                 {"name": "List-Unsubscribe", "value": "<https://u>"}],
                           "body": {"data": ""}}}
        rec = live.gmail_record(msg, {"id": "acc1", "email": "me@example.com"})
        self.assertTrue(rec["bulk"])
        with tempfile.TemporaryDirectory() as d:
            ms = msgstore.MessageStore(d)
            try:
                ms.upsert_messages([rec, dict(rec, msg_id="m2", bulk=False)])
                got = {r["msg_id"]: r["bulk"] for r in ms._read("SELECT msg_id, bulk FROM messages")}  # noqa: SLF001
                self.assertEqual(got, {"m1": 1, "m2": 0})
            finally:
                ms.close()


class StaticChecks(unittest.TestCase):
    APP = ROOT / "callsheet" / "app"

    def test_old_index_is_gone_and_library_opens_no_connections(self):
        self.assertFalse((self.APP / "index.py").exists())
        self.assertFalse((self.APP / "scan.py").exists())
        for name in ("library.py", "sources.py", "msgstore.py"):
            text = (self.APP / name).read_text()
            for mod in ("urllib.request", "http.client", "socket", "ssl", "subprocess"):
                self.assertNotIn("import " + mod, text, name)
        for p in self.APP.glob("*.py"):
            self.assertIsNone(re.search(r"^\s*(import (index|scan)\b|from (index|scan) import)", p.read_text(), re.M),
                              p.name)


if __name__ == "__main__":
    unittest.main()
