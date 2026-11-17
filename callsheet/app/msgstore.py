"""Rework stage 1: the message store, $CALLSHEET_DATA/messages.db (docs/messages.md).

Every message Callsheet reads, from every connected source and in both directions (received and sent), with its
thread, people, time and text; plus calendar events, each source's read cursors and each reader's freshness. It is
kept (nothing is deleted: an edit keeps the earlier text in `revisions`, a delete sets `deleted_at`) and included in
Home Assistant backups (not in config.yaml's backup_exclude).

One writer thread owns the write connection: every write goes through `write(fn)`, which runs fn(db) in one
transaction on that thread and waits for it. Reads use a second connection (WAL mode, so reads never wait for the
writer). Nothing here talks to the network: live.py's readers write, Chat (ChatView) and the Sources panel read.

Messages are keyed by (source, account, msg_id): upserts are idempotent (a content hash; an unchanged message is not
rewritten). Text is plain text: mail prefers text/plain, else the HTML as text (htmltext); quoted history is kept as
it is. Full-text search is FTS5 over subject, text and people when SQLite has it, else LIKE.
"""
import datetime as dt
import hashlib
import json
import os
import pathlib
import queue
import re
import sqlite3
import threading
import time

import inject

DB_NAME = "messages.db"
SCHEMA_VERSION = 1
STORE_ID_BASE = 1_000_000_000   # Chat item ids of store rows (index.db's own ids stay far below)
EVENT_ID_BASE = 2_000_000_000   # 0.17.0: Chat item ids of calendar events (the calendar tool)
MAX_TEXT = 200_000              # characters of text per message (a pathological mail is cut, never dropped)
MAX_FIELD = 2000
DEFAULT_BACKFILL_DAYS = 90
BACKFILL_CHOICES = (7, 30, 90, 180, 365)
SOURCES = ("gmail", "slack", "signal", "sms", "sms_live")   # 0.16.0: sms; 0.16.1: sms_live (texts.py)
# 0.21.0: Reports copied in for the ledger (heads.py). Not a Chat source: Chat finds them as "report" documents.
STORED_SOURCES = SOURCES + ("reports",)
DIRECTIONS = ("in", "out")
# 0.21.0: the read-only documents (table docs; library.py): Drive files, notes from the /share folders, the
# connectors' feed items and reports, GitHub. They replaced index.db (index.py, scan.py).
DOC_SOURCES = ("drive", "notes", "feed", "report", "github")
MAX_BODY = 8000        # characters of a document's body
MAX_LIMIT = 100        # rows per search
MAX_TERMS = 12         # words per search
SHORTENED = "[…]"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  account TEXT NOT NULL,
  thread TEXT NOT NULL DEFAULT '',
  msg_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  sender TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  recipients TEXT NOT NULL DEFAULT '[]',
  ts REAL NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  labels TEXT NOT NULL DEFAULT '[]',
  channel TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  attachments TEXT NOT NULL DEFAULT '[]',
  people TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL,
  ingested_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  deleted_at REAL,
  UNIQUE (source, account, msg_id));
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (source, account, thread, ts);
CREATE TABLE IF NOT EXISTS revisions (
  message INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS revisions_message ON revisions (message);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  calendar TEXT NOT NULL,
  event_id TEXT NOT NULL,
  ical_uid TEXT NOT NULL DEFAULT '',
  start TEXT NOT NULL DEFAULT '',
  end TEXT NOT NULL DEFAULT '',
  tz TEXT NOT NULL DEFAULT '',
  start_ts REAL,
  end_ts REAL,
  all_day INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  attendees TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  transparency TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL,
  ingested_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (account, calendar, event_id));
CREATE INDEX IF NOT EXISTS events_start ON events (start_ts);
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT NOT NULL, account TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (source, account, key));
CREATE TABLE IF NOT EXISTS freshness (
  source TEXT NOT NULL, account TEXT NOT NULL,
  last_attempt REAL, last_success REAL, last_new REAL, newest_ts REAL,
  errors INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', error_at REAL,
  backlog INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, account));
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  account TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  thread TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  people TEXT NOT NULL DEFAULT '',
  area TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  snippet TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL,
  indexed_at REAL NOT NULL,
  instr TEXT NOT NULL DEFAULT '',
  instr_v INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  own INTEGER NOT NULL DEFAULT 0,
  UNIQUE (source, account, source_id));
CREATE INDEX IF NOT EXISTS docs_src ON docs (source, account, ts);
CREATE INDEX IF NOT EXISTS docs_ts ON docs (ts);
CREATE TABLE IF NOT EXISTS old_ids (old INTEGER PRIMARY KEY, new INTEGER NOT NULL);
"""
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, text, people, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts (rowid, subject, text, people) VALUES (new.id, new.subject, new.text, new.people);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, text, people ON messages BEGIN
  INSERT INTO messages_fts (messages_fts, rowid, subject, text, people)
    VALUES ('delete', old.id, old.subject, old.text, old.people);
  INSERT INTO messages_fts (rowid, subject, text, people) VALUES (new.id, new.subject, new.text, new.people);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  title, people, body, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts (rowid, title, people, body) VALUES (new.id, new.title, new.people, new.body);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts (docs_fts, rowid, title, people, body) VALUES ('delete', old.id, old.title, old.people, old.body);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF title, people, body ON docs BEGIN
  INSERT INTO docs_fts (docs_fts, rowid, title, people, body) VALUES ('delete', old.id, old.title, old.people, old.body);
  INSERT INTO docs_fts (rowid, title, people, body) VALUES (new.id, new.title, new.people, new.body);
END;
"""
_MSG_FIELDS = ("thread", "direction", "sender", "sender_name", "recipients", "ts", "subject", "text", "channel",
               "link", "attachments")
_EVENT_FIELDS = ("ical_uid", "start", "end", "tz", "start_ts", "end_ts", "all_day", "title", "location",
                 "attendees", "status", "updated", "link")
_EVENT_EXTRA = ("transparency", "event_type", "organizer", "creator")   # hashed only when set, so older rows keep their hash
# 0.23.0: organizer / creator ("self" for the owner, else the address) for "does it need you?" (need.py)


# --- plain text and search words (from 0.6.0's index.py) --------------------------------------------------------------
def squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def cap_body(text, n=MAX_BODY):
    """Plain body text: CRLF to LF, at most two blank lines, capped at n characters with a [...] mark."""
    text = re.sub(r"[ \t\r\f\v]+\n", "\n", str(text or "").replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > n:
        text = text[:n].rstrip() + "\n" + SHORTENED
    return text


def fts_query(q, relaxed=False):
    """The owner's words as a safe FTS5 query: "quoted phrases" stay phrases, other words are quoted terms (AND),
    the last word also matches as a prefix. relaxed: terms joined with OR and every word a prefix. Returns "" when
    nothing searchable is left."""
    q = str(q or "")[:500]
    terms = []
    for phrase in re.findall(r'"([^"]+)"', q):
        words = re.findall(r"\w+", phrase)
        if words:
            terms.append('"' + " ".join(words) + '"')
    rest = re.sub(r'"[^"]*"?', " ", q)
    words = [w for w in re.findall(r"\w+", rest) if w.upper() not in ("AND", "OR", "NOT", "NEAR")]
    for i, w in enumerate(words):
        prefix = relaxed or (i == len(words) - 1 and not q.rstrip().endswith('"'))
        terms.append('"' + w + '"' + ("*" if prefix else ""))
    return (" OR " if relaxed else " ").join(terms[:MAX_TERMS])


def segments(marked):
    """snippet() output with \\x02...\\x03 marks -> [[text, highlighted], ...] for textContent rendering."""
    out = []
    for i, part in enumerate(re.split("[\x02\x03]", marked or "")):
        if part:
            out.append([part, i % 2 == 1])
    return out


BULK_PRECEDENCE = ("bulk", "list", "junk")


def is_bulk(headers):
    """0.21.0: mail sent to a list (a List-Unsubscribe header, or Precedence: bulk/list/junk). headers: lower-case
    names -> values. Such mail never wants a reply."""
    if not isinstance(headers, dict):
        return False
    if str(headers.get("list-unsubscribe") or "").strip():
        return True
    return str(headers.get("precedence") or "").strip().lower() in BULK_PRECEDENCE


class StoreError(RuntimeError):
    """The message store couldn't be opened (plain words)."""


def _s(v, n=MAX_FIELD):
    return str(v if v is not None else "")[:n]


def _j(v):
    return json.dumps(v if isinstance(v, (list, dict)) else [], ensure_ascii=False, sort_keys=True)


def _hash(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def _people(sender_name, sender, recipients):
    out = [sender_name, sender]
    for r in recipients or []:
        out.append(r.get("name", "") + " " + r.get("address", "") if isinstance(r, dict) else str(r))
    return " ".join(p.strip() for p in out if p and str(p).strip())[:MAX_FIELD * 2]


def normalize_message(m):
    """A reader's message dict -> the stored columns (validated, capped)."""
    src = _s(m.get("source"), 20)
    if src not in STORED_SOURCES:
        raise ValueError("unknown source")
    if m.get("direction") not in DIRECTIONS:
        raise ValueError("direction must be in or out")
    if not m.get("msg_id"):
        raise ValueError("msg_id is required")
    recips = [r for r in (m.get("recipients") or []) if isinstance(r, (dict, str))][:200]
    row = {"source": src, "account": _s(m.get("account"), 200), "msg_id": _s(m.get("msg_id"), 300),
           "thread": _s(m.get("thread"), 300), "direction": m["direction"], "sender": _s(m.get("sender"), 300),
           "sender_name": _s(m.get("sender_name"), 300), "recipients": _j(recips), "ts": float(m.get("ts") or 0),
           "subject": _s(m.get("subject"), 1000), "text": _s(m.get("text"), MAX_TEXT),
           "labels": _j(sorted({str(x)[:100] for x in m.get("labels") or []})), "channel": _s(m.get("channel"), 300),
           "link": _s(m.get("link"), 1000), "attachments": _j([a for a in m.get("attachments") or []
                                                                 if isinstance(a, dict)][:50]),
           "bulk": 1 if m.get("bulk") else 0}
    row["people"] = _people(row["sender_name"], row["sender"], recips)
    row["hash"] = _hash([row[f] for f in _MSG_FIELDS])
    return row


def _event_row(e):
    row = {"account": _s(e.get("account"), 200), "calendar": _s(e.get("calendar"), 300),
           "event_id": _s(e.get("event_id"), 300), "ical_uid": _s(e.get("ical_uid"), 300),
           "start": _s(e.get("start"), 60), "end": _s(e.get("end"), 60), "tz": _s(e.get("tz"), 80),
           "start_ts": e.get("start_ts"), "end_ts": e.get("end_ts"), "all_day": 1 if e.get("all_day") else 0,
           "title": _s(e.get("title"), 1000), "location": _s(e.get("location"), 1000),
           "attendees": _j(e.get("attendees") or []), "status": _s(e.get("status"), 40),
           "updated": _s(e.get("updated"), 60), "link": _s(e.get("link"), 1000),
           "transparency": _s(e.get("transparency"), 20), "event_type": _s(e.get("event_type"), 40),
           "organizer": _s(e.get("organizer"), 300).lower(), "creator": _s(e.get("creator"), 300).lower()}
    if not row["event_id"]:
        raise ValueError("event_id is required")
    row["hash"] = _hash([row[f] for f in _EVENT_FIELDS] + [row[f] for f in _EVENT_EXTRA if row[f]])
    return row


class MessageStore:
    def __init__(self, data_dir, clock=time.time):
        self.dir = pathlib.Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / DB_NAME
        self.clock = clock
        self.owner_names = None   # a function that returns the owner's names (inject's owner-voice rule), set by api.App
        # 0.21.0 (contacts.py): name_of(source, handle, name) -> the contact's name ("" if none), set by api.App, so
        # Chat and Today show "Dana Ruiz" instead of a raw number
        self.name_of = None
        self.lock = threading.RLock()       # the read connection
        self._q = queue.Queue()
        self._writer = None
        self._wlock = threading.Lock()
        self._closed = False
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        try:
            os.chmod(self.path, 0o600)
            self._wdb = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            self._wdb.row_factory = sqlite3.Row
            self._wdb.execute("PRAGMA journal_mode=WAL")
            self._wdb.execute("PRAGMA synchronous=NORMAL")
            self._wdb.executescript(_SCHEMA)
            have = {r[1] for r in self._wdb.execute("PRAGMA table_info(events)")}
            for col in _EVENT_EXTRA:                  # 0.16.0: free/busy and out-of-office, for the ledger's clashes
                if col not in have:
                    self._wdb.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            if "same_as" not in {r[1] for r in self._wdb.execute("PRAGMA table_info(messages)")}:
                # 0.16.1: a live text notification that the nightly backup also holds points at the backup's row
                self._wdb.execute("ALTER TABLE messages ADD COLUMN same_as INTEGER")
            if "bulk" not in {r[1] for r in self._wdb.execute("PRAGMA table_info(messages)")}:
                # 0.21.0: Gmail mail sent to a list (is_bulk), stored from now on; it never wants a reply
                self._wdb.execute("ALTER TABLE messages ADD COLUMN bulk INTEGER NOT NULL DEFAULT 0")
            row = self._wdb.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._wdb.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                raise StoreError("The message store was written by a newer Callsheet.")
            try:
                self._wdb.executescript(_FTS)
                self.fts = True
            except sqlite3.OperationalError:   # SQLite without FTS5: search falls back to LIKE
                self.fts = False
            self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            raise StoreError(f"Couldn't open the message store ({type(e).__name__}).") from None

    # --- the one writer -------------------------------------------------------------------------------------------
    def _ensure_writer(self):
        with self._wlock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(target=self._write_loop, name="callsheet-msgstore-writer", daemon=True)
                self._writer.start()

    def _write_loop(self):
        while True:
            job = self._q.get()
            if job is None:
                return
            fn, done, box = job
            try:
                self._wdb.execute("BEGIN IMMEDIATE")
                try:
                    box["result"] = fn(self._wdb)
                    self._wdb.execute("COMMIT")
                except BaseException:
                    self._wdb.execute("ROLLBACK")
                    raise
            except Exception as e:  # noqa: BLE001 (handed back to the caller)
                box["error"] = e
            done.set()

    def write(self, fn):
        """Run fn(db) in one transaction on the writer thread and return its result (its exception is re-raised)."""
        if self._closed:
            raise StoreError("The message store is closed.")
        if threading.current_thread() is self._writer:
            return fn(self._wdb)
        self._ensure_writer()
        done, box = threading.Event(), {}
        self._q.put((fn, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._writer is not None and self._writer.is_alive():
            self._q.put(None)
            self._writer.join(timeout=5)
        for c in (self.db, self._wdb):
            try:
                c.close()
            except sqlite3.Error:
                pass

    def _read(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # --- meta, settings and cursors -------------------------------------------------------------------------------
    def meta(self, key, default=None):
        r = self._read("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_meta(self, key, value):
        self.write(lambda db: db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                         "value=excluded.value", (key, json.dumps(value))))

    def backfill_days(self):
        v = self.meta("backfill_days", DEFAULT_BACKFILL_DAYS)
        return v if v in BACKFILL_CHOICES else DEFAULT_BACKFILL_DAYS

    def set_backfill_days(self, days):
        if days not in BACKFILL_CHOICES:
            raise ValueError("backfill_days must be one of " + ", ".join(map(str, BACKFILL_CHOICES)))
        old = self.backfill_days()
        self.set_meta("backfill_days", days)
        if days > old:   # read further back: every Gmail account's backfill runs again (stored mail is skipped)
            def reopen(db):
                for r in db.execute("SELECT account, value FROM cursors WHERE source='gmail' AND key='backfill'"):
                    v = json.loads(r["value"])
                    v.update(done=False, page=None, days=days)
                    db.execute("UPDATE cursors SET value=? WHERE source='gmail' AND account=? AND key='backfill'",
                               (json.dumps(v), r["account"]))
            self.write(reopen)

    def cursor(self, source, account, key="cursor", default=None):
        r = self._read("SELECT value FROM cursors WHERE source=? AND account=? AND key=?", (source, account, key))
        return json.loads(r[0]["value"]) if r else default

    def set_cursor(self, source, account, key, value):
        self.write(lambda db: self._set_cursor(db, source, account, key, value))

    @staticmethod
    def _set_cursor(db, source, account, key, value):
        db.execute("INSERT INTO cursors (source, account, key, value) VALUES (?, ?, ?, ?) ON CONFLICT(source, account, "
                   "key) DO UPDATE SET value=excluded.value", (source, account, key, json.dumps(value)))

    # --- messages ---------------------------------------------------------------------------------------------------
    def _upsert(self, db, row, now):
        old = db.execute("SELECT id, hash, labels, subject, text, deleted_at FROM messages WHERE source=? AND account=? "
                         "AND msg_id=?", (row["source"], row["account"], row["msg_id"])).fetchone()
        if old is None:
            cols = list(row) + ["ingested_at", "updated_at"]
            db.execute(f"INSERT INTO messages ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [row[c] for c in row] + [now, now])
            return "new"
        if old["hash"] == row["hash"]:
            if old["labels"] != row["labels"]:
                db.execute("UPDATE messages SET labels=?, updated_at=? WHERE id=?", (row["labels"], now, old["id"]))
                return "labels"
            return "same"
        if (old["subject"], old["text"]) != (row["subject"], row["text"]):
            db.execute("INSERT INTO revisions (message, at, kind, subject, text, hash) VALUES (?, ?, 'edit', ?, ?, ?)",
                       (old["id"], now, old["subject"], old["text"], old["hash"]))
        sets = [c for c in row if c not in ("source", "account", "msg_id")]
        db.execute(f"UPDATE messages SET {', '.join(c + '=?' for c in sets)}, updated_at=? WHERE id=?",
                   [row[c] for c in sets] + [now, old["id"]])
        return "updated"

    def upsert_messages(self, messages):
        """Insert or update messages (reader dicts). Returns {"new", "updated", "same", "newest"} (newest: the
        newest ts of the new ones, or None)."""
        rows = [normalize_message(m) for m in messages]
        now = self.clock()

        def run(db):
            out = {"new": 0, "updated": 0, "same": 0, "newest": None}
            for r in rows:
                k = self._upsert(db, r, now)
                if k == "new":
                    out["new"] += 1
                    out["newest"] = max(out["newest"] or 0, r["ts"])
                elif k in ("updated", "labels"):
                    out["updated"] += 1
                else:
                    out["same"] += 1
            return out
        return self.write(run) if rows else {"new": 0, "updated": 0, "same": 0, "newest": None}

    def upsert_message(self, m):
        return self.upsert_messages([m])

    def edit_message(self, source, account, msg_id, text, at=None):
        """A source's edit of a stored message: the earlier text goes to revisions. False when it isn't stored."""
        now = self.clock()

        def run(db):
            old = db.execute("SELECT * FROM messages WHERE source=? AND account=? AND msg_id=?",
                             (source, account, msg_id)).fetchone()
            if old is None:
                return False
            new = {f: old[f] for f in _MSG_FIELDS}
            new["text"] = _s(text, MAX_TEXT)
            h = _hash([new[f] for f in _MSG_FIELDS])
            if h == old["hash"]:
                return True
            db.execute("INSERT INTO revisions (message, at, kind, subject, text, hash) VALUES (?, ?, 'edit', ?, ?, ?)",
                       (old["id"], at or now, old["subject"], old["text"], old["hash"]))
            db.execute("UPDATE messages SET text=?, hash=?, updated_at=? WHERE id=?", (new["text"], h, now, old["id"]))
            return True
        return self.write(run)

    def mark_deleted(self, source, account, msg_id, at=None):
        """Recorded, never removed: deleted_at is set and a revision notes it. False when it isn't stored."""
        now = self.clock()

        def run(db):
            old = db.execute("SELECT id, deleted_at FROM messages WHERE source=? AND account=? AND msg_id=?",
                             (source, account, msg_id)).fetchone()
            if old is None:
                return False
            if old["deleted_at"] is None:
                db.execute("UPDATE messages SET deleted_at=?, updated_at=? WHERE id=?", (at or now, now, old["id"]))
                db.execute("INSERT INTO revisions (message, at, kind) VALUES (?, ?, 'delete')", (old["id"], at or now))
            return True
        return self.write(run)

    def set_labels(self, source, account, msg_id, labels):
        """Gmail label changes (read, archived, starred ...) on a stored message. False when it isn't stored."""
        val, now = _j(sorted({str(x)[:100] for x in labels or []})), self.clock()

        def run(db):
            cur = db.execute("UPDATE messages SET labels=?, updated_at=? WHERE source=? AND account=? AND msg_id=? AND "
                             "labels != ?", (val, now, source, account, str(msg_id), val))
            return cur.rowcount > 0
        return self.write(run)

    def has_message(self, source, account, msg_id):
        return bool(self._read("SELECT 1 FROM messages WHERE source=? AND account=? AND msg_id=?",
                               (source, account, str(msg_id))))

    def message(self, source, account, msg_id):
        r = self._read("SELECT * FROM messages WHERE source=? AND account=? AND msg_id=?", (source, account, str(msg_id)))
        return dict(r[0]) if r else None

    def revisions(self, message_id):
        return [dict(r) for r in self._read("SELECT * FROM revisions WHERE message=? ORDER BY at", (int(message_id),))]

    def count(self, source=None, direction=None):
        where, args = [], []
        if source:
            where.append("source=?")
            args.append(source)
        if direction:
            where.append("direction=?")
            args.append(direction)
        return self._read("SELECT count(*) AS n FROM messages" + (" WHERE " + " AND ".join(where) if where else ""),
                          args)[0]["n"]

    def newest_ts(self, source=None, account=None):
        where, args = [], []
        for k, v in (("source", source), ("account", account)):
            if v:
                where.append(k + "=?")
                args.append(v)
        r = self._read("SELECT max(ts) AS t FROM messages" + (" WHERE " + " AND ".join(where) if where else ""), args)
        return r[0]["t"]

    # --- calendar events --------------------------------------------------------------------------------------------
    def upsert_events(self, events, after=None):
        """after (0.21.0): run in the same write (calwrite.link_events: an added item meets its event at once)."""
        rows = [_event_row(e) for e in events]
        now = self.clock()

        def run(db):
            out = {"new": 0, "updated": 0, "same": 0}
            for r in rows:
                old = db.execute("SELECT id, hash FROM events WHERE account=? AND calendar=? AND event_id=?",
                                 (r["account"], r["calendar"], r["event_id"])).fetchone()
                if old is None:
                    cols = list(r) + ["ingested_at", "updated_at"]
                    db.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               [r[c] for c in r] + [now, now])
                    out["new"] += 1
                elif old["hash"] != r["hash"]:
                    sets = [c for c in r if c not in ("account", "calendar", "event_id")]
                    db.execute(f"UPDATE events SET {', '.join(c + '=?' for c in sets)}, updated_at=? WHERE id=?",
                               [r[c] for c in sets] + [now, old["id"]])
                    out["updated"] += 1
                else:
                    out["same"] += 1
            if after is not None:
                after(db)
            return out
        return self.write(run) if rows else {"new": 0, "updated": 0, "same": 0}

    def events(self, since=None, until=None, include_cancelled=False):
        where, args = [], []
        if since is not None:
            where.append("coalesce(end_ts, start_ts) >= ?")
            args.append(since)
        if until is not None:
            where.append("start_ts < ?")
            args.append(until)
        if not include_cancelled:
            where.append("status != 'cancelled'")
        rows = self._read("SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") +
                          " ORDER BY start_ts", args)
        return [dict(r, attendees=json.loads(r["attendees"])) for r in rows]

    def event(self, account, calendar, event_id):
        r = self._read("SELECT * FROM events WHERE account=? AND calendar=? AND event_id=?", (account, calendar, event_id))
        return dict(r[0]) if r else None

    # --- freshness ----------------------------------------------------------------------------------------------------
    def _fresh(self, source, account, sets, args):
        def run(db):
            db.execute("INSERT OR IGNORE INTO freshness (source, account) VALUES (?, ?)", (source, account))
            db.execute(f"UPDATE freshness SET {sets} WHERE source=? AND account=?", list(args) + [source, account])
        self.write(run)

    def fresh_attempt(self, source, account, at=None):
        self._fresh(source, account, "last_attempt=?", (at or self.clock(),))

    def fresh_ok(self, source, account, at=None, new=0, newest_ts=None, backlog=None):
        """A read that worked: errors reset; last_new when it stored something; newest_ts only moves forward."""
        at = at or self.clock()
        sets, args = ["last_attempt=?", "last_success=?", "errors=0"], [at, at]
        if new:
            sets.append("last_new=?")
            args.append(at)
        if newest_ts:
            sets.append("newest_ts=max(coalesce(newest_ts, 0), ?)")
            args.append(float(newest_ts))
        if backlog is not None:
            sets.append("backlog=?")
            args.append(max(0, int(backlog)))
        self._fresh(source, account, ", ".join(sets), args)

    def fresh_error(self, source, account, message, at=None):
        at = at or self.clock()
        self._fresh(source, account, "last_attempt=?, errors=errors+1, last_error=?, error_at=?",
                    (at, _s(message, 300), at))

    def freshness(self, source=None):
        rows = self._read("SELECT * FROM freshness" + (" WHERE source=?" if source else "") + " ORDER BY source, account",
                          (source,) if source else ())
        return [dict(r) for r in rows]

    def forget_fresh(self, source, account):
        """A disconnected account's row (its messages stay)."""
        self.write(lambda db: db.execute("DELETE FROM freshness WHERE source=? AND account=?", (source, account)))

    # --- reads for Chat and Setup -------------------------------------------------------------------------------------
    def size_bytes(self):
        total = 0
        for extra in ("", "-wal", "-shm"):
            try:
                total += self.path.with_name(self.path.name + extra).stat().st_size
            except OSError:
                pass
        return total

    def stats(self):
        rows = self._read("SELECT source, direction, count(*) AS n, max(ts) AS newest FROM messages GROUP BY source, "
                          "direction")
        out = {"messages": 0, "events": self._read("SELECT count(*) AS n FROM events")[0]["n"],
               "bytes": self.size_bytes(), "sources": {}}
        for r in rows:
            s = out["sources"].setdefault(r["source"], {"in": 0, "out": 0, "newest": None})
            s[r["direction"]] = r["n"]
            s["newest"] = max(s["newest"] or 0, r["newest"] or 0) or None
            out["messages"] += r["n"]
        return out

    def _owner(self):
        try:
            return tuple(self.owner_names() or ()) if callable(self.owner_names) else ()
        except Exception:  # noqa: BLE001 (the plain rules still run)
            return ()

    @staticmethod
    def title_of(r):
        if r["subject"]:
            return r["subject"]
        if r["channel"]:
            return r["channel"]
        return {"slack": "Slack message", "signal": "Signal message", "sms": "Text message",
                "sms_live": "Text message"}.get(r["source"], "(no subject)")

    def shown_name(self, r):
        """0.21.0: the sender's name to show: the owner's contact name for the handle, else the name the source gave."""
        if r["direction"] == "in" and callable(self.name_of):
            try:
                n = self.name_of(r["source"], r["sender"], r["sender_name"])
            except Exception:  # noqa: BLE001 (a name is a nicety)
                n = ""
            if n:
                return n
        return r["sender_name"]

    def _row(self, r, with_body=False):
        own = r["direction"] == "out"
        title = self.title_of(r)
        people = r["people"]
        out = {"id": STORE_ID_BASE + r["id"], "source": r["source"], "account": r["account"],
               "kind": ("sent" if own else "email") if r["source"] == "gmail" else "message",
               "thread": r["thread"], "title": title, "people": people, "area": "", "ts": r["ts"],
               "date": dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).isoformat(timespec="seconds"),
               "url": r["link"], "store": True, "direction": r["direction"], "source_id": r["msg_id"],
               "sender": r["sender"], "sender_name": self.shown_name(r),
               "instructions": bool(inject.keys(title, r["text"], r["source"], self._owner(), own))}
        if r["deleted_at"]:
            out["deleted"] = True
        if with_body:
            out["snippet"] = cap_body(r["text"], 300)
            out["body"] = r["text"]
            out["own"] = own
        return out

    def get(self, item_id):
        """One message by its Chat id (STORE_ID_BASE + rowid) with its text, or None."""
        try:
            rid = int(item_id) - STORE_ID_BASE
        except (TypeError, ValueError):
            return None
        if int(item_id) >= EVENT_ID_BASE:
            return self.event_item(int(item_id))
        if rid < 1:
            return None
        r = self._read("SELECT * FROM messages WHERE id=?", (rid,))
        return self._row(r[0], with_body=True) if r else None

    def event_item(self, item_id):
        """0.17.0: one calendar event as a Chat item (id EVENT_ID_BASE + rowid), or None."""
        r = self._read("SELECT * FROM events WHERE id=?", (int(item_id) - EVENT_ID_BASE,))
        if not r:
            return None
        e = r[0]
        try:
            att = json.loads(e["attendees"] or "[]")
        except ValueError:
            att = []
        people = "; ".join(str((a.get("name") or a.get("email") or "") if isinstance(a, dict) else a)
                           for a in att[:20] if a)
        body = "\n".join(x for x in (e["title"], f"When: {e['start']} to {e['end']}",
                                     f"Where: {e['location']}" if e["location"] else "",
                                     f"With: {people}" if people else "") if x)
        link = e["link"] if str(e["link"] or "").startswith("https://") else ""
        return {"id": int(item_id), "source": "calendar", "account": e["account"], "kind": "event",
                "title": e["title"] or "(no title)", "people": people, "area": "", "ts": e["start_ts"] or 0,
                "url": link, "store": True, "snippet": cap_body(body, 300), "body": body, "own": False,
                "instructions": bool(inject.keys(e["title"], e["location"], "calendar", self._owner(), False))}

    def search(self, q="", source=None, account=None, people=None, since=None, until=None, limit=20, direction=None,
               ids=None, offset=0, accounts=None, relax=None):
        """Rows like Index.search (id = STORE_ID_BASE + rowid, highlight segments, snippet), best match first
        (bm25: subject x8, people x4, text x1; then newest), or newest first when q is empty. Deleted messages are
        left out, and so is a live text notification the backup also holds (same_as: the backup's copy is shown).
        0.21.0 (library.py): offset; accounts (a list: only these); relax None = strict words, then any word when
        nothing has them all; False = strict only; True = any word only. Each row carries its bm25 "score"."""
        limit = max(1, min(int(limit or 20), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        where, args = ["m.deleted_at IS NULL", "m.same_as IS NULL"], []
        if source:
            srcs = [s for s in (source if isinstance(source, (list, tuple)) else str(source).split(",")) if s]
            where.append("m.source IN (" + ",".join("?" * len(srcs)) + ")")
            args += srcs
        else:
            where.append("m.source != 'reports'")   # 0.21.0: Chat finds reports in the index
        if account:
            where.append("m.account=?")
            args.append(str(account))
        if accounts is not None:
            accs = [str(a) for a in accounts] or [""]
            where.append("m.account IN (" + ",".join("?" * len(accs)) + ")")
            args += accs
        if direction in ("in", "out"):   # 0.17.0: search_messages' received/sent filter
            where.append("m.direction=?")
            args.append(direction)
        if people:
            terms = [str(p)[:100] for p in (people if isinstance(people, (list, tuple)) else [people])
                     if str(p).strip()][:12]
            if terms:
                where.append("(" + " OR ".join(["m.people LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
                args += ["%" + re.sub(r"([%_\\])", r"\\\1", t) + "%" for t in terms]
        if since is not None:
            where.append("m.ts>=?")
            args.append(float(since))
        if until is not None:
            where.append("m.ts<?")
            args.append(float(until))
        if ids is not None:                     # 0.18.0: a Chat scope (Chat ids, STORE_ID_BASE + rowid)
            rids = sorted({int(i) - STORE_ID_BASE for i in ids if int(i) >= STORE_ID_BASE})[:1000] or [-1]
            where.append("m.id IN (" + ",".join("?" * len(rids)) + ")")
            args += rids
        tail = "".join(" AND " + w for w in where)
        rows, relaxed = [], False
        match = fts_query(q)
        if match and self.fts:
            sql = ("SELECT m.*, snippet(messages_fts, -1, char(2), char(3), '…', 18) AS marked, "
                   "bm25(messages_fts, 8.0, 1.0, 4.0) AS score FROM messages_fts JOIN messages m ON "
                   "m.id = messages_fts.rowid WHERE messages_fts MATCH ?" + tail +
                   " ORDER BY score, m.ts DESC LIMIT ? OFFSET ?")
            tries = [(0, match), (1, fts_query(q, relaxed=True))]
            for i, mq in (tries[:1] if relax is False else tries[1:] if relax else tries):
                try:
                    rows = self._read(sql, [mq] + args + [limit, offset])
                except sqlite3.OperationalError:
                    rows = []
                if rows or len(re.findall(r'"[^"]*"', mq)) < 2 or offset:
                    relaxed = bool(rows) and i == 1
                    break
        elif match:   # no FTS5: every word somewhere in subject, text or people
            words = re.findall(r"\w+", str(q))[:MAX_TERMS]
            for w in words:
                where.append("(m.subject LIKE ? ESCAPE '\\' OR m.text LIKE ? ESCAPE '\\' OR m.people LIKE ? ESCAPE '\\')")
                args += ["%" + re.sub(r"([%_\\])", r"\\\1", w) + "%"] * 3
            rows = self._read("SELECT m.*, NULL AS marked, 0 AS score FROM messages m WHERE " + " AND ".join(where) +
                              " ORDER BY m.ts DESC LIMIT ? OFFSET ?", args + [limit, offset])
        else:
            rows = self._read("SELECT m.*, NULL AS marked, 0 AS score FROM messages m WHERE " + " AND ".join(where) +
                              " ORDER BY m.ts DESC LIMIT ? OFFSET ?", args + [limit, offset])
        out = []
        for r in rows:
            row = self._row(r)
            marked = r["marked"] if r["marked"] else cap_body(squash(r["text"]), 300)
            segs = segments(squash(marked)) or [[cap_body(r["text"], 300), False]]  # noqa: SLF001
            row["highlight"] = segs
            row["snippet"] = "".join(s for s, _ in segs)
            row["score"] = r["score"] or 0
            if relaxed:
                row["relaxed"] = True
            out.append(row)
        return out

    def thread(self, source, account, thread, limit=50):
        rows = self._read("SELECT * FROM messages WHERE source=? AND account=? AND thread=? ORDER BY ts DESC LIMIT ?",
                          (source, account, thread, int(limit)))
        return [dict(r, sender_name=self.shown_name(r)) for r in reversed(rows)]
