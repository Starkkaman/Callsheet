"""0.6.0 "Know me": the local index of the owner's mail, calendars, Slack, Drive and Obsidian notes.

A separate SQLite database, $CALLSHEET_DATA/index.db (0600, excluded from Home Assistant backups because it can be
rebuilt), with an FTS5 table over title, people and body. Nothing here talks to the network: sources.py reads, the
scanner (scan.py) writes through `upsert`/`commit_step`, and later stages (3b profile/interview, 3c chat) read through
`search`, `get` and `stats` only.

Items are keyed by (source, account, source_id): upserts are deterministic and idempotent (a content hash; an
unchanged item is not rewritten). Removing a source or an account removes its items. Bodies are plain text, already
stripped of quoted history and signatures by the source, and capped at MAX_BODY characters.
"""
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import threading
import time

DB_NAME = "index.db"
SCHEMA_VERSION = 1
MAX_BODY = 8000        # characters of body text per item
MAX_TITLE = 300
MAX_PEOPLE = 1500
MAX_SNIPPET = 300
MAX_URL = 1000
MAX_LIMIT = 100
MAX_TERMS = 12
SOURCE_KEY = re.compile(r"[a-z][a-z0-9_]{1,19}")   # gmail, calendar, slack, drive, obsidian; later signal
SHORTENED = "[…]"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  account TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
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
  UNIQUE (source, account, source_id));
CREATE INDEX IF NOT EXISTS items_ts ON items (ts);
CREATE INDEX IF NOT EXISTS items_src ON items (source, account, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, people, body, content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts (rowid, title, people, body) VALUES (new.id, new.title, new.people, new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts (items_fts, rowid, title, people, body) VALUES ('delete', old.id, old.title, old.people, old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts (items_fts, rowid, title, people, body) VALUES ('delete', old.id, old.title, old.people, old.body);
  INSERT INTO items_fts (rowid, title, people, body) VALUES (new.id, new.title, new.people, new.body);
END;
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT NOT NULL, account TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (source, account, key));
"""
_FIELDS = ("source", "account", "source_id", "kind", "thread", "title", "people", "area", "ts", "url", "snippet",
           "body")


class IndexError_(RuntimeError):
    """The index database can't be opened (a plain sentence). The rest of Callsheet keeps working."""


def _squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def cap_body(text, n=MAX_BODY):
    """Plain body text: CRLF to LF, at most two blank lines, capped at n characters with a [...] mark."""
    text = re.sub(r"[ \t\r\f\v]+\n", "\n", str(text or "").replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > n:
        text = text[:n].rstrip() + "\n" + SHORTENED
    return text


def normalize(item):
    """The stored form of one item (a dict with _FIELDS); raises ValueError for a malformed item."""
    if not isinstance(item, dict):
        raise ValueError("item must be a dict")
    src = str(item.get("source") or "")
    if not SOURCE_KEY.fullmatch(src):
        raise ValueError("bad source key")
    sid = str(item.get("source_id") or "")
    if not sid or len(sid) > 500:
        raise ValueError("bad source_id")
    people = item.get("people") or ""
    if isinstance(people, (list, tuple)):
        seen, out = set(), []
        for p in people:
            p = _squash(p)
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        people = "; ".join(out)
    body = cap_body(item.get("body"))
    snippet = _squash(item.get("snippet")) or _squash(body)
    try:
        ts = float(item.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return {"source": src, "account": str(item.get("account") or "")[:200], "source_id": sid,
            "kind": str(item.get("kind") or "item")[:20], "thread": str(item.get("thread") or "")[:300],
            "title": _clip(_squash(item.get("title")), MAX_TITLE), "people": _clip(_squash(people), MAX_PEOPLE),
            "area": str(item.get("area") or "")[:60], "ts": round(ts, 3),
            "url": str(item.get("url") or "")[:MAX_URL], "snippet": _clip(snippet, MAX_SNIPPET), "body": body}


def _hash(row):
    return hashlib.sha256(json.dumps([row[f] for f in _FIELDS], ensure_ascii=False).encode()).hexdigest()


def fts_query(q):
    """The owner's words as a safe FTS5 query: "quoted phrases" stay phrases, other words are quoted terms (AND),
    the last word also matches as a prefix. Returns "" when nothing searchable is left."""
    q = str(q or "")[:500]
    terms = []
    for phrase in re.findall(r'"([^"]+)"', q):
        words = re.findall(r"\w+", phrase)
        if words:
            terms.append('"' + " ".join(words) + '"')
    rest = re.sub(r'"[^"]*"?', " ", q)
    words = [w for w in re.findall(r"\w+", rest) if w.upper() not in ("AND", "OR", "NOT", "NEAR")]
    for i, w in enumerate(words):
        terms.append('"' + w + '"' + ("*" if i == len(words) - 1 and not q.rstrip().endswith('"') else ""))
    return " ".join(terms[:MAX_TERMS])


def _segments(marked):
    """snippet() output with \\x02...\\x03 marks -> [[text, highlighted], ...] for textContent rendering."""
    out = []
    for i, part in enumerate(re.split("[\x02\x03]", marked or "")):
        if part:
            out.append([part, i % 2 == 1])
    return out


class Index:
    def __init__(self, data_dir, clock=time.time):
        self.dir = pathlib.Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / DB_NAME
        self.clock = clock
        self.lock = threading.RLock()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        try:
            self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        except sqlite3.Error as e:
            raise IndexError_(f"Couldn't open the index ({type(e).__name__}).") from None
        self.db.row_factory = sqlite3.Row
        try:
            with self.lock:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=NORMAL")
                self.db.executescript(_SCHEMA)
                row = self.db.execute("SELECT version FROM schema_version").fetchone()
                if row is None:
                    self.db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                elif row["version"] > SCHEMA_VERSION:
                    raise IndexError_("The index was written by a newer Callsheet. Delete it in Setup to rebuild it.")
        except sqlite3.Error as e:
            self.db.close()
            raise IndexError_(f"Couldn't open the index ({type(e).__name__}: {e}).") from None
        except IndexError_:
            self.db.close()
            raise
        self._chmod()

    def _chmod(self):
        for extra in ("", "-wal", "-shm"):
            p = self.path.with_name(self.path.name + extra)
            if p.exists():
                os.chmod(p, 0o600)

    def close(self):
        with self.lock:
            self.db.close()

    # --- meta and cursors ---------------------------------------------------------------------------------------
    def meta(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        with self.lock:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                "value=excluded.value", (key, json.dumps(value)))

    def cursor(self, source, account, key="scan"):
        with self.lock:
            row = self.db.execute("SELECT value FROM cursors WHERE source=? AND account=? AND key=?",
                                  (source, account, key)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_cursor(self, source, account, key, value):
        with self.lock:
            self._set_cursor(source, account, key, value)

    def _set_cursor(self, source, account, key, value):
        if value is None:
            self.db.execute("DELETE FROM cursors WHERE source=? AND account=? AND key=?", (source, account, key))
        else:
            self.db.execute("INSERT INTO cursors (source, account, key, value) VALUES (?,?,?,?) ON CONFLICT"
                            "(source, account, key) DO UPDATE SET value=excluded.value",
                            (source, account, key, json.dumps(value)))

    def clear_cursors(self, key=None):
        with self.lock:
            if key is None:
                self.db.execute("DELETE FROM cursors")
            else:
                self.db.execute("DELETE FROM cursors WHERE key=?", (key,))

    # --- writes -------------------------------------------------------------------------------------------------
    def _upsert(self, items, now):
        n = {"added": 0, "updated": 0, "same": 0, "bad": 0}
        for it in items:
            try:
                row = normalize(it)
            except ValueError:
                n["bad"] += 1
                continue
            h = _hash(row)
            old = self.db.execute("SELECT id, hash FROM items WHERE source=? AND account=? AND source_id=?",
                                  (row["source"], row["account"], row["source_id"])).fetchone()
            if old is None:
                self.db.execute(f"INSERT INTO items ({', '.join(_FIELDS)}, hash, indexed_at) VALUES "
                                f"({', '.join('?' * len(_FIELDS))}, ?, ?)", [row[f] for f in _FIELDS] + [h, now])
                n["added"] += 1
            elif old["hash"] != h:
                self.db.execute(f"UPDATE items SET {', '.join(f + '=?' for f in _FIELDS)}, hash=?, indexed_at=? "
                                "WHERE id=?", [row[f] for f in _FIELDS] + [h, now, old["id"]])
                n["updated"] += 1
            else:
                n["same"] += 1
        return n

    def upsert(self, items):
        """Adds or updates items; returns counts {added, updated, same, bad}. Same input, same rows."""
        return self.commit_step(items)

    def commit_step(self, items, source=None, account=None, key="scan", cursor=None, clear_cursor=False):
        """One scan step in one transaction: the items and the source's resume point (so a restart never loses or
        repeats more than one step)."""
        now = round(self.clock(), 3)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                n = self._upsert(items or [], now)
                if source is not None and (cursor is not None or clear_cursor):
                    self._set_cursor(source, account or "", key, None if clear_cursor else cursor)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return n

    def delete(self, source=None, account=None):
        """Removes the items (and resume points) of a source, or of one account of a source. Returns the count."""
        if source is None and account is None:
            raise ValueError("use wipe() to remove everything")
        where, args = [], []
        if source is not None:
            where.append("source=?")
            args.append(source)
        if account is not None:
            where.append("account=?")
            args.append(account)
        w = " AND ".join(where)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.execute("DELETE FROM items WHERE " + w, args)
                self.db.execute("DELETE FROM cursors WHERE " + w, args)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return cur.rowcount

    def delete_missing(self, source, account, keep):
        """Removes this source/account's items whose source_id is not in `keep` (e.g. notes deleted from the vault)."""
        with self.lock:
            ids = [r["source_id"] for r in self.db.execute(
                "SELECT source_id FROM items WHERE source=? AND account=?", (source, account)).fetchall()]
            gone = [i for i in ids if i not in keep]
            for k in range(0, len(gone), 500):
                part = gone[k:k + 500]
                self.db.execute("DELETE FROM items WHERE source=? AND account=? AND source_id IN (" +
                                ",".join("?" * len(part)) + ")", [source, account] + part)
        return len(gone)

    def accounts(self):
        """[(source, account)] present in the index."""
        with self.lock:
            return [(r["source"], r["account"]) for r in
                    self.db.execute("SELECT DISTINCT source, account FROM items").fetchall()]

    def wipe(self):
        """Everything goes: items, resume points, scan state. Settings (vault folder, sources) stay."""
        with self.lock:
            settings = self.meta("settings")
            self.db.execute("DELETE FROM items")
            self.db.execute("INSERT INTO items_fts (items_fts) VALUES ('rebuild')")
            self.db.execute("DELETE FROM cursors")
            self.db.execute("DELETE FROM meta")
            if settings is not None:
                self.set_meta("settings", settings)
            self.db.execute("VACUUM")
        self._chmod()

    # --- reads (the interface for 3b/3c) -----------------------------------------------------------------------
    @staticmethod
    def _filters(source, account, area, people, since, until):
        where, args = [], []
        if source:
            srcs = [s for s in (source if isinstance(source, (list, tuple)) else str(source).split(",")) if s]
            if srcs:
                where.append("i.source IN (" + ",".join("?" * len(srcs)) + ")")
                args += srcs
        if account:
            where.append("i.account=?")
            args.append(str(account))
        if area:
            where.append("i.area=?")
            args.append(str(area))
        if people:
            where.append("i.people LIKE ? ESCAPE '\\'")
            args.append("%" + re.sub(r"([%_\\])", r"\\\1", str(people)[:100]) + "%")
        if since is not None:
            where.append("i.ts>=?")
            args.append(float(since))
        if until is not None:
            where.append("i.ts<?")
            args.append(float(until))
        return where, args

    @staticmethod
    def _row(r, with_body=False):
        out = {"id": r["id"], "source": r["source"], "account": r["account"], "kind": r["kind"],
               "thread": r["thread"], "title": r["title"], "people": r["people"], "area": r["area"], "ts": r["ts"],
               "date": dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).isoformat(timespec="seconds"),
               "url": r["url"]}
        if with_body:
            out["snippet"] = r["snippet"]
            out["body"] = r["body"]
            out["source_id"] = r["source_id"]
        return out

    def search(self, q="", source=None, account=None, area=None, people=None, since=None, until=None, limit=20,
               offset=0):
        """Ranked rows for the owner's words (FTS5 bm25: title x8, people x4, body x1; then newest first), or the
        newest rows matching the filters when q is empty. Each row: id, source, account, kind, thread, title, people,
        area, ts, date (UTC ISO), url, snippet (plain text) and highlight ([[text, matched], ...])."""
        limit = max(1, min(int(limit or 20), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        where, args = self._filters(source, account, area, people, since, until)
        match = fts_query(q)
        with self.lock:
            if match:
                sql = ("SELECT i.*, snippet(items_fts, -1, char(2), char(3), '…', 18) AS marked, "
                       "bm25(items_fts, 8.0, 4.0, 1.0) AS score FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                       "WHERE items_fts MATCH ?" + "".join(" AND " + w for w in where) +
                       " ORDER BY score, i.ts DESC LIMIT ? OFFSET ?")
                try:
                    rows = self.db.execute(sql, [match] + args + [limit, offset]).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                sql = ("SELECT i.*, i.snippet AS marked FROM items i" + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY i.ts DESC LIMIT ? OFFSET ?")
                rows = self.db.execute(sql, args + [limit, offset]).fetchall()
        out = []
        for r in rows:
            row = self._row(r)
            segs = _segments(_squash(r["marked"]).replace("\x02 ", " \x02").replace(" \x03", "\x03 ")) or \
                [[r["snippet"], False]]
            row["highlight"] = segs
            row["snippet"] = "".join(s for s, _ in segs)
            out.append(row)
        return out

    def get(self, item_id):
        """One full item (with body and source_id), or None."""
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return None
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._row(r, with_body=True) if r else None

    def size_bytes(self):
        total = 0
        for extra in ("", "-wal", "-shm"):
            try:
                total += (self.path.with_name(self.path.name + extra)).stat().st_size
            except OSError:
                pass
        return total

    def stats(self):
        """{total, bytes, oldest, newest, sources: {source: {items, years: {YYYY: n}, accounts: {account: n}}}}."""
        with self.lock:
            rows = self.db.execute("SELECT source, account, strftime('%Y', ts, 'unixepoch') AS y, count(*) AS n "
                                   "FROM items GROUP BY source, account, y").fetchall()
            span = self.db.execute("SELECT min(ts) AS lo, max(ts) AS hi, count(*) AS n FROM items").fetchone()
        sources = {}
        for r in rows:
            s = sources.setdefault(r["source"], {"items": 0, "years": {}, "accounts": {}})
            s["items"] += r["n"]
            s["years"][r["y"] or "?"] = s["years"].get(r["y"] or "?", 0) + r["n"]
            s["accounts"][r["account"]] = s["accounts"].get(r["account"], 0) + r["n"]
        return {"total": span["n"], "bytes": self.size_bytes(), "oldest": span["lo"], "newest": span["hi"],
                "sources": sources}

    def count(self, source=None, account=None):
        where, args = self._filters(source, account, None, None, None, None)
        with self.lock:
            return self.db.execute("SELECT count(*) AS n FROM items i" + (" WHERE " + " AND ".join(where) if where
                                                                         else ""), args).fetchone()["n"]
