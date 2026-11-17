"""Setup › Advanced › Old data from earlier versions (0.21.0, approved 2026-11-13). What the features retired in the rework left in /data, listed with counts and
sizes, and removed only when the owner ticks it and presses Clear.

- Only by the owner's press. GET api/old-data lists the groups and hands out a one-time token; POST api/old-data/clear
  removes the groups the owner ticked, with that token (used once, for 10 minutes, only the latest one). Nothing calls this
  at start, on upgrade, on a schedule or from Chat: there is no propose_setting or propose_action for it.
- Only what was listed. Each group is a fixed list: tables in callsheet.db and meta keys that no code reads any more
  (tests/test_oldata_0210.py checks each against the code), setting keys nothing reads, and files of removed
  features. A POST removes what its GET listed and is still there, nothing else.
- Never: messages.db (only library's two import markers are read), the ledger, people, contacts, calendar adds,
  drafts, chats, memory notes, Know me answers, connectors, reports, tokens or current settings. A setting key an
  upgrade migration still reads (aisettings.migrate: presets, Chat's own server, a background server) is offered
  only once that migration has applied on this install, or when it is empty. Values are never returned, only names.
- callsheet.db changes in one transaction, then VACUUM. index.db (with its -wal, -shm) and writer-eval/ are deleted
  as files; index.db only after library's two one-time imports have recorded completion with index.db there.
stdlib only.
"""
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import threading
import time

import aisettings
import chat
import library
import store as store_mod
from store import Conflict, InputError

TOKEN_S = 600
INDEX_FILES = ("index.db", "index.db-wal", "index.db-shm", "index.db-journal")
EVAL_DIR = "writer-eval"
_NAME = re.compile(r"[a-z][a-z0-9_]{1,40}")

# id, label, what it was for, tables in callsheet.db, whole meta keys in callsheet.db, folders under /data.
# Each table and key here is read by no code since the rework (tests/test_oldata_0210.py greps every module).
GROUPS = (
    ("sorter", "AI sorting",
     "Sorted your messages before the ledger read them: its labels, your corrections, its queue and the Check the "
     "sorting counts.",
     ("triage_labels", "triage_corrections", "triage_queue", "review_counts"),
     ("sorting_ix", "sorting_backfill", "sorting_review_day", "sorting_rescued_logged"), ()),
    ("learner", "What it learned",
     "What the sorter learned from your corrections, by sender, domain and person.",
     ("learn_events",), (), ()),
    ("reader", "The reader",
     "Read your threads for a gist and a due date before the ledger did.",
     ("reader_items", "reader_queue", "reader_counts"), ("reader_day", "reader_counts_logged"), ()),
    ("picker", "The “What you should know” picker",
     "Picked news and updates for you: its picks, its queue and your More or Less like this.",
     ("picker_items", "picker_queue", "picker_prefs"), ("picker", "picker_day", "picker_perf"), ()),
    ("eval", "Writer evaluation",
     "Compared writer setups on a day’s brief: its contenders and its saved runs.",
     (), ("eval",), (EVAL_DIR,)),
    ("brief", "The old brief",
     "The last brief written the old way, its Done marks and seen rows, and the invite, promotion, calendar and "
     "Coming up caches of See everything.",
     (), ("brief", "done", "seen_day", "see_ics", "see_promos", "see_snapshots", "horizon_marks"), ()),
)
SETTINGS_GROUP = ("settings", "Settings nothing uses",
                  "Saved choices of features that are gone: presets, per-job and Chat’s own servers, the sorter’s and "
                  "the reader’s modes, “needs a reply after N days” and the like.")
AI_BEFORE_GROUP = ("ai_before", "Your AI settings from before 0.21.0",
                   "A copy kept when Setup › AI moved to one model server, in case anything was missed. API keys were "
                   "never copied into it.")
INDEX_GROUP = ("index", "The old index (index.db)",
               "0.6.0’s separate index and scan. Everything in it was copied into the message store once.")

# The saved settings' keys that are still read (store.settings, writer_config, chat.load_settings,
# background.Background.settings and background_model, background.date_order); any other key in these is unread.
SORTING_KEEP = ("model", "ctx_sizes", "backfill", "model_extra_body")   # backfill: only "windows" is read
READER_KEEP = ("date_order",)
CHAT_KEEP = ("provider", "local", "claude", "v062")
# What aisettings.migrate reads from the saved settings: kept until it has applied (meta `ai`.v), unless empty
MIGRATE_READS = {("writer", "presets"), ("writer", "call_presets"), ("chat", "local", "base_url"),
                 ("chat", "local", "api_key"), ("sorting", "base_url"), ("sorting", "api_key")}


def _size(n):
    return max(0, int(n or 0))


def _jlen(v):
    return len(json.dumps(v, ensure_ascii=False).encode("utf-8"))


def _get(d, path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _drop(d, path):
    """Removes one key (path of one or two parts) from a settings dict in place."""
    if len(path) == 1:
        d.pop(path[0], None)
    elif isinstance(d.get(path[0]), dict):
        d[path[0]].pop(path[1], None)


class OldData:
    def __init__(self, store, messages=lambda: None, clock=time.time):
        self.store = store
        self.dir = pathlib.Path(store.dir)
        self._messages = messages          # () -> msgstore.MessageStore or None (only its import markers are read)
        self.clock = clock
        self._lock = threading.Lock()
        self._offer = None                 # {"token", "until", "items": {group id: [item names]}}

    def routes(self):
        return [("GET", r"old-data", self.view),
                ("POST", r"old-data/clear", self.clear)]

    # --- what is there ---------------------------------------------------------------------------------------------
    def _raw(self, key):
        v = self.store._get_meta(key, None)  # noqa: SLF001
        return v if isinstance(v, dict) else None

    def _tables(self):
        with self.store.lock:
            return {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _table_bytes(self, names):
        """{table: bytes} with its indexes (dbstat when SQLite has it, else the stored values' lengths)."""
        db, out = self.store.db, {}
        with self.store.lock:
            idx = {}
            for r in db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'"):
                idx.setdefault(r[1], []).append(r[0])
            try:
                pages = {r[0]: r[1] for r in db.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
            except sqlite3.Error:
                pages = None
            for t in names:
                if pages is not None:
                    out[t] = sum(_size(pages.get(n)) for n in [t] + idx.get(t, []))
                    continue
                cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
                expr = " + ".join(f'LENGTH(quote("{c}"))' for c in cols) or "0"
                out[t] = _size(db.execute(f'SELECT SUM({expr}) FROM "{t}"').fetchone()[0])
        return out

    def _setting_paths(self):
        """[(meta key, path)] of saved setting keys no code reads."""
        out = []
        s = self._raw("settings")
        if s:
            keep = store_mod.default_settings()
            out += [("settings", (k,)) for k in s if k not in keep]
        w = self._raw("writer")
        if w:
            dw = store_mod.default_writer()
            for k, v in w.items():
                if k not in dw:
                    out.append(("writer", (k,)))
                elif isinstance(dw[k], dict) and isinstance(v, dict):
                    out += [("writer", (k, kk)) for kk in v if kk not in dw[k]]
        c = self._raw("chat")
        if c:
            parts = {"local": chat.LOCAL_DEFAULTS, "claude": chat.CLAUDE_DEFAULTS}
            for k, v in c.items():
                if k not in CHAT_KEEP:
                    out.append(("chat", (k,)))
                elif k in parts and isinstance(v, dict):
                    # Chat's own server went in the rework: Chat always asks the one model server (chat.effective_local)
                    out += [("chat", (k, kk)) for kk, vv in v.items() if kk not in parts[k] or
                            (k == "local" and kk in ("base_url", "api_key") and vv)]
        so = self._raw("sorting")
        if so:
            for k, v in so.items():
                if k not in SORTING_KEEP:
                    out.append(("sorting", (k,)))
                elif k == "backfill" and isinstance(v, dict):
                    out += [("sorting", ("backfill", kk)) for kk in v if kk != "windows"]
        r = self._raw("reader")
        if r:
            out += [("reader", (k,)) for k in r if k not in READER_KEEP]
        applied = aisettings.settings(self.store)["v"] >= aisettings.VERSION
        return [(m, p) for m, p in out
                if applied or (m,) + p not in MIGRATE_READS or not _get(self._raw(m), p)]

    def _index_state(self):
        """The imported counts once both of library's one-time imports recorded completion with index.db there,
        else None (index.db isn't offered)."""
        try:
            ms = self._messages()
            if ms is None:
                return None
            a, b = ms.meta(library.IMPORT_KEY), ms.meta(library.MSG_IMPORT_KEY)
        except Exception:  # noqa: BLE001 (no message store: nothing to check the import against)
            return None
        if not (isinstance(a, dict) and isinstance(b, dict)):
            return None
        if a.get("file") is False or b.get("file") is False:
            return None    # index.db wasn't there when the import ran: it was never read
        counts = {"documents": a.get("docs"), "old_ids": a.get("ids"), "settings": a.get("meta"),
                  "messages": b.get("messages"), "already_stored": b.get("same")}
        counts = {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0 for k, v in counts.items()}
        if not (a.get("file") is True or b.get("file") is True or any(counts.values())):
            return None    # a marker from before "file" was recorded, and nothing came over: not confirmed
        return counts

    def _index_rows(self):
        try:
            src = sqlite3.connect(f"file:{self.dir / 'index.db'}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            return src.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        except sqlite3.Error:
            return None
        finally:
            src.close()

    def _folder(self, name):
        """(files, bytes) of a folder under /data, not following links; None when it isn't there."""
        p = self.dir / name
        if not os.path.lexists(p):
            return None
        if p.is_symlink() or not p.is_dir():
            return 1, _size(os.lstat(p).st_size)
        n = b = 0
        for root, dirs, files in os.walk(p, followlinks=False):
            for f in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                n += 1
                b += _size(os.lstat(os.path.join(root, f)).st_size)
        return n, b

    def scan(self):
        """[group]: {id, label, why, items: [{name, kind, count, unit, bytes, ref}], imported?}; only non-empty
        groups. `ref` is internal (what to remove)."""
        groups = []
        tables = self._tables()
        old_tables = [t for g in GROUPS for t in g[3] if t in tables]
        tbytes = self._table_bytes(old_tables) if old_tables else {}
        with self.store.lock:
            meta = {r[0]: _size(r[1]) for r in self.store.db.execute(
                "SELECT key, LENGTH(CAST(key AS BLOB)) + LENGTH(CAST(value AS BLOB)) FROM meta")}
            rows = {t: self.store.db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in old_tables}
        for gid, label, why, gtables, keys, folders in GROUPS:
            items = [{"name": t, "kind": "table", "count": rows[t], "unit": "row", "bytes": tbytes.get(t, 0),
                      "ref": ("table", t)} for t in gtables if t in rows]
            items += [{"name": k, "kind": "key", "count": 1, "unit": "setting", "bytes": meta[k], "ref": ("key", k)}
                      for k in keys if k in meta]
            for f in folders:
                got = self._folder(f)
                if got:
                    items.append({"name": f + "/", "kind": "folder", "count": got[0], "unit": "file", "bytes": got[1],
                                  "ref": ("folder", f)})
            if items:
                groups.append({"id": gid, "label": label, "why": why, "items": items})
        items = []
        for m, path in self._setting_paths():
            v = _get(self._raw(m), path)
            items.append({"name": m + ": " + ".".join(path), "kind": "setting", "count": 1, "unit": "setting",
                          "bytes": _jlen(v), "ref": ("setting", m, path)})
        if items:
            groups.append({"id": SETTINGS_GROUP[0], "label": SETTINGS_GROUP[1], "why": SETTINGS_GROUP[2],
                           "items": items})
        ai = self._raw("ai")
        if ai and "before" in ai:
            groups.append({"id": AI_BEFORE_GROUP[0], "label": AI_BEFORE_GROUP[1], "why": AI_BEFORE_GROUP[2],
                           "items": [{"name": "ai: before", "kind": "setting", "count": 1, "unit": "setting",
                                      "bytes": _jlen(ai["before"]), "ref": ("setting", "ai", ("before",))}]})
        imported = self._index_state() if (self.dir / "index.db").is_file() else None
        if imported is not None:
            items = []
            for f in INDEX_FILES:
                p = self.dir / f
                if os.path.lexists(p) and (p.is_symlink() or p.is_file()):
                    n = self._index_rows() if f == "index.db" else None
                    items.append({"name": f, "kind": "file", "count": n if n is not None else 1,
                                  "unit": "row" if n is not None else "file", "bytes": _size(os.lstat(p).st_size),
                                  "ref": ("file", f)})
            groups.append({"id": INDEX_GROUP[0], "label": INDEX_GROUP[1], "why": INDEX_GROUP[2], "items": items,
                           "imported": imported})
        return groups

    @staticmethod
    def _public(g):
        items = [{k: it[k] for k in ("name", "kind", "count", "unit", "bytes")} for it in g["items"]]
        out = {"id": g["id"], "label": g["label"], "why": g["why"], "items": items, "count": len(items),
               "rows": sum(it["count"] for it in items if it["unit"] == "row"),
               "bytes": sum(it["bytes"] for it in items)}
        if "imported" in g:
            out["imported"] = dict(g["imported"])
        return out

    # --- the API -------------------------------------------------------------------------------------------------------
    def view(self, _body=None):
        """GET api/old-data: {groups: [{id, label, why, count, rows, bytes, items, imported?}], total, token}. Reads
        only; the token (None when nothing is left) is the one a Clear needs."""
        groups = self.scan()
        with self._lock:
            if groups:
                self._offer = {"token": secrets.token_urlsafe(24), "until": self.clock() + TOKEN_S,
                               "items": {g["id"]: [it["name"] for it in g["items"]] for g in groups}}
                token = self._offer["token"]
            else:
                self._offer, token = None, None
        pub = [self._public(g) for g in groups]
        return {"groups": pub, "token": token,
                "total": {"count": sum(g["count"] for g in pub), "bytes": sum(g["bytes"] for g in pub)}}

    def clear(self, body):
        """POST api/old-data/clear {token, groups: [ids]}: removes the ticked groups' items that the GET with this
        token listed. Returns {cleared: {groups, count, bytes}, freed, errors} and the fresh view (a new token)."""
        if not isinstance(body, dict):
            raise InputError("Send a JSON object.")
        token, want = body.get("token"), body.get("groups")
        if not isinstance(want, list) or not want or not all(isinstance(g, str) for g in want):
            raise InputError("Tick what to clear first.")
        with self._lock:
            offer, self._offer = self._offer, None      # one use, right or wrong
            if not (offer and isinstance(token, str) and hmac.compare_digest(token.encode(), offer["token"].encode())
                    and self.clock() <= offer["until"]):
                raise Conflict("This list is out of date. Look again, then clear.")
            if any(g not in offer["items"] for g in want):
                raise InputError("Tick what to clear from the list.")
            chosen = [g for g in self.scan() if g["id"] in set(want)]
            todo = [it for g in chosen for it in g["items"] if it["name"] in offer["items"][g["id"]]]
            errors, freed = [], 0
            db_items = [it for it in todo if it["ref"][0] in ("table", "key", "setting")]
            if db_items:
                freed += self._clear_db(db_items)
            for it in todo:
                kind, name = it["ref"][0], it["ref"][-1]
                if kind not in ("file", "folder"):
                    continue
                if not (name in INDEX_FILES or name == EVAL_DIR):
                    continue
                p = self.dir / name
                try:
                    if kind == "folder" and p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                    freed += it["bytes"]
                except FileNotFoundError:
                    pass
                except OSError as e:
                    errors.append(f"Couldn’t delete {name} ({type(e).__name__}).")
        out = self.view()
        out.update(cleared={"groups": [g["id"] for g in chosen], "count": len(todo),
                            "bytes": sum(it["bytes"] for it in todo)}, freed=freed, errors=errors)
        return out

    def _db_bytes(self):
        n = 0
        for extra in ("", "-wal"):
            try:
                n += os.path.getsize(str(self.store.path) + extra)
            except OSError:
                pass
        return n

    def _clear_db(self, items):
        """callsheet.db's part, in one transaction, then VACUUM. Returns the bytes the file shrank by."""
        db = self.store.db
        tables = [it["ref"][1] for it in items if it["ref"][0] == "table"]
        keys = [it["ref"][1] for it in items if it["ref"][0] == "key"]
        settings = {}
        for it in items:
            if it["ref"][0] == "setting":
                settings.setdefault(it["ref"][1], []).append(it["ref"][2])
        allowed_t = {t for g in GROUPS for t in g[3]}
        allowed_k = {k for g in GROUPS for k in g[4]}
        if not (set(tables) <= allowed_t and set(keys) <= allowed_k and all(_NAME.fullmatch(t) for t in tables)):
            raise InputError("That isn't old data.")
        with self.store.lock:
            before = self._db_bytes()
            try:
                db.execute("BEGIN IMMEDIATE")
                for t in tables:
                    db.execute(f'DROP TABLE IF EXISTS "{t}"')
                if tables and db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    db.executemany("DELETE FROM sqlite_sequence WHERE name=?", [(t,) for t in tables])
                db.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in keys])
                for m, paths in settings.items():
                    r = db.execute("SELECT value FROM meta WHERE key=?", (m,)).fetchone()
                    v = json.loads(r[0]) if r else None
                    if not isinstance(v, dict):
                        continue
                    for p in paths:
                        _drop(v, p)
                    # the row stays (an empty dict reads as the defaults; store.settings' upgrade values still hold)
                    db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(v), m))
                db.execute("COMMIT")
            except (sqlite3.Error, ValueError):
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise Conflict("Couldn’t clear it just now; nothing was changed. Try again.") from None
            try:
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass   # cleared; the space comes back at the next VACUUM
            return max(0, before - self._db_bytes())
