"""0.21.0: the library, Callsheet's one searchable view of messages.db (msgstore.py; docs/messages.md).

It replaced 0.6.0's separate index (index.py, index.db, and its scanner scan.py). Chat's read-only tools, Find
(api/index/search), Know me, Projects, People and the connectors all read here, and nothing else is searched:
- messages (mail, Slack, Signal, texts) and calendar events, as the live readers store them (live.py);
- read-only documents, the table `docs`: Drive files ("drive") and notes from the /share folders ("notes"), read by
  live.DocsReader through sources.py's readers; the connectors' feed items ("feed") and reports ("report"); GitHub
  ("github", for Projects). Nothing here writes to Drive, the notes folder, a feed or GitHub.

Item ids: a document is its docs rowid (below msgstore.STORE_ID_BASE), a message STORE_ID_BASE + rowid, a calendar
event EVENT_ID_BASE + rowid. Documents are keyed by (source, account, source_id); upserts are idempotent (a content
hash) and flag planted instructions (inject.py) as the index did.

The upgrade: index.db stays on disk, unused, until the owner clears it (Setup › Advanced › Old data, oldata.py, only
once both imports below have recorded completion). On the first start import_index copies, once, its
documents, notes, feed items and reports WITH THEIR IDS (so the ids Projects, Know me and the ledger's report cursor
keep still point at them), its meta (Know me's draft and saved answers, the Drive grant cache, the notes folder
setting) without overwriting anything, and a map from its message and event ids to the store's (old_ids). Then
import_index_messages copies, once and in the background, the mail, Slack and Signal messages index.db holds and the
store doesn't (the 0.6.0 scan often went further back than the live readers' backfill): every message is kept. They
keep their source, account, source id and thread (deduplicated by source message id; a Signal message the live
reader also stored, by its time, direction and text) and their old ids map to the new ones.
"""
import datetime as dt
import hashlib
import heapq
import json
import pathlib
import re
import sqlite3
import sys
import time

import areas as areas_mod   # 0.22.0 an item's area (moves keep or take past items)
import inject
import msgstore
from msgstore import (DOC_SOURCES, EVENT_ID_BASE, MAX_LIMIT, STORE_ID_BASE, cap_body, clip, fts_query, segments,
                      squash)

MESSAGE_SOURCES = msgstore.SOURCES
SOURCES = MESSAGE_SOURCES + ("calendar",) + DOC_SOURCES
SOURCE_KEY = re.compile(r"[a-z][a-z0-9_]{1,19}")
MAX_TITLE = 300
MAX_PEOPLE = 1500
MAX_SNIPPET = 300
MAX_URL = 1000
SETTINGS_KEY = "docs_settings"
READERS = ("drive", "notes", "github")    # the documents live.DocsReader reads (each can be turned off)
# The two one-time imports' completion markers in messages.db's meta: their counts, the time and whether index.db was
# there ("file"; markers written before 0.21.0 was released have no "file"). oldata.py offers index.db only after both.
IMPORT_KEY = "index_import"
MSG_IMPORT_KEY = "index_import_messages"   # 0.21.0: index.db's mail, Slack and Signal the store doesn't hold, once
IMPORT_BATCH = 500
_FIELDS = ("source", "account", "source_id", "kind", "thread", "title", "people", "area", "ts", "url", "snippet",
           "body")
_META_SKIP = ("job", "refresh", "synced", "triage")   # the old scanner's own state: not carried over
_OLD_SOURCES = {"obsidian": "notes", "reports": "report"}


def doc_source(kind):
    """The source a connector's items are stored under: its reports are "report", every other kind is "feed"."""
    return "report" if kind == "reports" else "feed"


def default_settings():
    return {"vault": "", "sources": {k: True for k in READERS}}


def normalize(item):
    """The stored form of one document (a dict with _FIELDS); raises ValueError for a malformed item."""
    if not isinstance(item, dict):
        raise ValueError("item must be a dict")
    src = str(item.get("source") or "")
    if src not in DOC_SOURCES:
        raise ValueError("not a document source")
    sid = str(item.get("source_id") or "")
    if not sid or len(sid) > 500:
        raise ValueError("bad source_id")
    people = item.get("people") or ""
    if isinstance(people, (list, tuple)):
        seen, out = set(), []
        for p in people:
            p = squash(p)
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        people = "; ".join(out)
    body = cap_body(item.get("body"))
    snippet = squash(item.get("snippet")) or squash(body)
    try:
        ts = float(item.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return {"source": src, "account": str(item.get("account") or "")[:200], "source_id": sid,
            "kind": str(item.get("kind") or "item")[:20], "thread": str(item.get("thread") or "")[:300],
            "title": clip(squash(item.get("title")), MAX_TITLE), "people": clip(squash(people), MAX_PEOPLE),
            "area": str(item.get("area") or "")[:60], "ts": round(ts, 3),
            "url": str(item.get("url") or "")[:MAX_URL], "snippet": clip(snippet, MAX_SNIPPET), "body": body}


def _hash(row):
    return hashlib.sha256(json.dumps([row[f] for f in _FIELDS], ensure_ascii=False).encode()).hexdigest()


def _own(r):
    """The owner's own text (inject's owner-voice rule doesn't run there)."""
    keys = r.keys() if hasattr(r, "keys") else ()
    return ("kind" in keys and r["kind"] == "sent") or bool("own" in keys and r["own"])


def _instr(title, body, source=None, owner=(), own=False):
    """The stored flag: a JSON list of the flagged sentences' short hashes, "" when there are none."""
    ks = inject.keys(title, body, source, owner, own)
    return json.dumps(ks) if ks else ""


def _flagged(r, owner=()):
    """Does this document hold a planted instruction? The stored flag, or (checked by older rules) the rules now."""
    keys = r.keys()
    if "instr_v" in keys and r["instr_v"] == inject.VERSION:
        return bool(r["instr"])
    if "body" in keys:
        return bool(inject.keys(r["title"], r["body"], r["source"] if "source" in keys else None,
                                owner() if callable(owner) else owner, _own(r)))
    return False


def _person(name, addr):
    name, addr = squash(name), squash(addr)
    if name and addr and "@" in addr:
        return f"{name} <{addr}>"
    return addr if "@" in addr else (name or addr)


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts or 0), dt.timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _series(event_id):
    """A recurring event's instances share the id before "_" (Google: <series id>_<original start>)."""
    return event_id.rsplit("_", 1)[0] if "_" in str(event_id or "") else ""


def _list(source):
    if not source:
        return None
    srcs = [s for s in (source if isinstance(source, (list, tuple)) else str(source).split(",")) if s]
    return srcs or None


def _like(t):
    return "%" + re.sub(r"([%_\\])", r"\\\1", str(t)) + "%"


def _terms(people):
    if not people:
        return []
    return [str(p)[:100] for p in (people if isinstance(people, (list, tuple)) else [people]) if str(p).strip()][:12]


class _AreaMap(dict):
    """{account: area id}; at() gives one item's area (areas.Resolver when the store has one)."""
    res = None

    def at(self, source, account, ts=None, calendar=None):
        if self.res is not None:
            return self.res.at(source, account, ts, calendar)
        return self.get(account, "")


def _area_at(areas, source, account, ts=None, calendar=None):
    return areas.at(source, account, ts, calendar) if isinstance(areas, _AreaMap) else (areas or {}).get(account, "")


class Library:
    def __init__(self, ms, store=None, clock=None):
        self.ms, self.store = ms, store
        self.clock = clock or ms.clock

    # --- plumbing ---------------------------------------------------------------------------------------------------
    @property
    def owner_names(self):
        return self.ms.owner_names

    @owner_names.setter
    def owner_names(self, fn):
        self.ms.owner_names = fn

    @property
    def lock(self):
        return self.ms.lock

    def meta(self, key, default=None):
        return self.ms.meta(key, default)

    def set_meta(self, key, value):
        self.ms.set_meta(key, value)

    def _read(self, sql, args=()):
        return self.ms._read(sql, args)  # noqa: SLF001

    def _owner(self):
        return self.ms._owner()  # noqa: SLF001

    def _areas(self):
        """{account or workspace id: area id} for filters, with .at(source, account, ts, calendar) for one item's area
        (0.22.0: a connection moved with "Leave as it is" keeps its older items in their old area; areas.Resolver)."""
        out = _AreaMap()
        if self.store is None:
            return out
        try:
            for a in self.store.accounts():
                out[a["id"]] = a.get("area_id") or ""
            for w in self.store.slack_workspaces():
                if w.get("area_id"):
                    out[w["id"]] = w["area_id"]
            if hasattr(self.store, "db"):
                out.res = areas_mod.Areas(self.store).resolver()
        except Exception:  # noqa: BLE001 (an area is a nicety)
            pass
        return out
        try:
            for a in self.store.accounts():
                out[a["id"]] = a.get("area_id") or ""
            for w in self.store.slack_workspaces():
                if w.get("area_id"):
                    out[w["id"]] = w["area_id"]
        except Exception:  # noqa: BLE001 (an area is a nicety)
            pass
        return out

    # --- settings: which documents are read, and the notes folder --------------------------------------------------
    def settings(self):
        s = default_settings()
        saved = self.meta(SETTINGS_KEY) or {}
        s["vault"] = saved.get("vault") or ""
        s["sources"].update({k: bool(v) for k, v in (saved.get("sources") or {}).items() if k in s["sources"]})
        return s

    def update_settings(self, patch):
        """{vault: folder under /share or /media, or ""; sources: {drive|notes|github: bool}}. Turning a source off
        (or changing the notes folder) removes what it read; the source itself is never touched."""
        import sources as srcs   # sources.py imports this module's helpers
        from store import InputError
        if not isinstance(patch, dict):
            raise InputError("Send the change as a JSON object.")
        s = self.settings()
        removed = []
        for key, val in patch.items():
            if key == "vault":
                new = "" if val in (None, "") else (srcs.check_vault(val) and val.strip())
                if new != s["vault"]:
                    removed.append(("notes", "vault"))
                s["vault"] = new
            elif key == "sources":
                if not isinstance(val, dict) or any(k not in READERS or not isinstance(v, bool) for k, v in val.items()):
                    raise InputError("sources takes true/false for " + ", ".join(READERS) + ".")
                for k, v in val.items():
                    if s["sources"][k] and not v:
                        removed.append((k, None))
                s["sources"].update(val)
            else:
                raise InputError(f"Document settings can't change {str(key)[:40]}.")
        self.set_meta(SETTINGS_KEY, s)
        for source, account in removed:
            self.delete(source, account)
        return s

    # --- documents: writes ------------------------------------------------------------------------------------------
    def _upsert(self, db, items, now):
        n = {"added": 0, "updated": 0, "same": 0, "bad": 0}
        owner = self._owner()
        for it in items:
            try:
                row = normalize(it)
            except ValueError:
                n["bad"] += 1
                continue
            h = _hash(row)
            old = db.execute("SELECT id, hash, instr_v, own FROM docs WHERE source=? AND account=? AND source_id=?",
                             (row["source"], row["account"], row["source_id"])).fetchone()
            own = 1 if it.get("from_owner") is True else 0
            hidden = 1 if it.get("hidden_text") is True else 0
            recheck = old is not None and (old["instr_v"] != inject.VERSION or old["own"] != own)
            instr = ""
            if old is None or old["hash"] != h or recheck:
                instr = _instr(row["title"], row["body"], row["source"], owner, bool(own))
            if old is None:
                db.execute(f"INSERT INTO docs ({', '.join(_FIELDS)}, hash, indexed_at, instr, instr_v, hidden, own) "
                           f"VALUES ({', '.join('?' * len(_FIELDS))}, ?, ?, ?, ?, ?, ?)",
                           [row[f] for f in _FIELDS] + [h, now, instr, inject.VERSION, hidden, own])
                n["added"] += 1
            elif old["hash"] != h:
                db.execute(f"UPDATE docs SET {', '.join(f + '=?' for f in _FIELDS)}, hash=?, indexed_at=?, instr=?, "
                           "instr_v=?, hidden=?, own=? WHERE id=?",
                           [row[f] for f in _FIELDS] + [h, now, instr, inject.VERSION, hidden, own, old["id"]])
                n["updated"] += 1
            else:
                if recheck:
                    db.execute("UPDATE docs SET instr=?, instr_v=?, own=? WHERE id=?",
                               (instr, inject.VERSION, own, old["id"]))
                n["same"] += 1
        return n

    def commit_step(self, items, source=None, account=None, key="docs", cursor=None, clear_cursor=False):
        """Documents and (optionally) the reader's resume point in one transaction. Returns {added, updated, same,
        bad}. Same input, same rows."""
        now = round(self.clock(), 3)

        def run(db):
            out = self._upsert(db, list(items or []), now)
            if source is not None and (cursor is not None or clear_cursor):
                msgstore.MessageStore._set_cursor(db, source, account or "", key, None if clear_cursor else cursor)  # noqa: SLF001
            return out
        return self.ms.write(run)

    def upsert(self, items):
        return self.commit_step(items)

    def set_flags(self, source, account, flags, only_empty=True):
        """Connectors: the flag found on an item's ORIGINAL text [(json keys, source_id)] (where none is stored)."""
        def run(db):
            for k, sid in flags:
                db.execute("UPDATE docs SET instr=? WHERE source=? AND account=? AND source_id=?" +
                           (" AND instr=''" if only_empty else ""), (k, source, account, sid))
        if flags:
            self.ms.write(run)

    def delete(self, source=None, account=None):
        """Removes the documents (and resume points) of a source, or of one account of a source. Returns the count."""
        if source is None and account is None:
            raise ValueError("name a source or an account")
        where, args = [], []
        if source is not None:
            where.append("source=?")
            args.append(source)
        if account is not None:
            where.append("account=?")
            args.append(account)
        w = " AND ".join(where)

        def run(db):
            n = db.execute("DELETE FROM docs WHERE " + w, args).rowcount
            db.execute("DELETE FROM cursors WHERE key='docs' AND " + w, args)
            return n
        return self.ms.write(run)

    def delete_missing(self, source, account, keep):
        """Removes this source/account's documents whose source_id is not in `keep` (a note deleted from the folder)."""
        gone = [r["source_id"] for r in self._read("SELECT source_id FROM docs WHERE source=? AND account=?",
                                                   (source, account)) if r["source_id"] not in keep]

        def run(db):
            for k in range(0, len(gone), 500):
                part = gone[k:k + 500]
                db.execute("DELETE FROM docs WHERE source=? AND account=? AND source_id IN (" +
                           ",".join("?" * len(part)) + ")", [source, account] + part)
        if gone:
            self.ms.write(run)
        return len(gone)

    def prune(self, source, account, before, keep):
        """Connectors: only the newest `keep` items, and none older than `before`."""
        self.ms.write(lambda db: db.execute(
            "DELETE FROM docs WHERE source=? AND account=? AND (ts<? OR id NOT IN (SELECT id FROM docs WHERE source=? "
            "AND account=? ORDER BY ts DESC, id DESC LIMIT ?))", (source, account, before, source, account, keep)))

    def backfill_instructions(self, batch=500, pause=0.0, sleep=time.sleep):
        """Flags for documents checked by older rules, in short transactions. Returns the number checked."""
        done = 0
        while True:
            rows = self._read("SELECT id, source, kind, own, title, body FROM docs WHERE instr_v<>? LIMIT ?",
                              (inject.VERSION, int(batch)))
            if not rows:
                return done
            owner = self._owner()
            upd = [(_instr(r["title"], r["body"], r["source"], owner, _own(r)), inject.VERSION, r["id"]) for r in rows]
            self.ms.write(lambda db, u=upd: db.executemany("UPDATE docs SET instr=?, instr_v=? WHERE id=?", u))
            done += len(rows)
            if pause:
                sleep(pause)

    # --- documents: reads for the connectors ----------------------------------------------------------------------
    def doc_items(self, source, account=None, since=None, after=None, limit=200, oldest_first=False):
        where, args = ["source=?"], [source]
        if account is not None:
            where.append("account=?")
            args.append(account)
        if since is not None:
            where.append("ts>=?")
            args.append(float(since))
        if after is not None:
            where.append("id>?")
            args.append(int(after))
        order = "id" if oldest_first else "ts DESC, id DESC"
        return self._read(f"SELECT * FROM docs WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                          args + [int(limit)])

    def source_ids(self, source, account):
        return {r["source_id"] for r in self._read("SELECT source_id FROM docs WHERE source=? AND account=?",
                                                   (source, account))}

    # --- reads -------------------------------------------------------------------------------------------------------
    def _doc_row(self, r, areas=None, with_body=False):
        out = {"id": r["id"], "source": r["source"], "account": r["account"], "kind": r["kind"],
               "thread": r["thread"], "title": r["title"], "people": r["people"], "area": r["area"], "ts": r["ts"],
               "date": _iso(r["ts"]), "url": r["url"], "instructions": _flagged(r, self._owner)}
        if r["hidden"]:
            out["hidden_text"] = True
        if with_body:
            out.update(snippet=r["snippet"], body=r["body"], source_id=r["source_id"], own=_own(r))
        return out

    def _event_row(self, e, areas):
        try:
            att = json.loads(e["attendees"] or "[]")
        except ValueError:
            att = []
        people = "; ".join(_person(a.get("name") or "", a.get("email") or "") if isinstance(a, dict) else str(a)
                           for a in att[:30] if a)
        snip = clip(squash(" · ".join(x for x in (e["location"], people) if x)), MAX_SNIPPET)
        link = e["link"] if str(e["link"] or "").startswith("https://") else ""
        return {"id": EVENT_ID_BASE + e["id"], "source": "calendar", "account": e["account"], "kind": "event",
                "thread": _series(e["event_id"]), "title": e["title"] or "(no title)", "people": people,
                "area": _area_at(areas, "calendar", e["account"], e["start_ts"], e["calendar"]), "ts": e["start_ts"] or 0, "date": _iso(e["start_ts"]),
                "url": link, "snippet": snip, "highlight": [[snip, False]] if snip else [],
                "instructions": bool(inject.keys(e["title"], e["location"], "calendar", self._owner(), False))}

    def _docs(self, q, srcs, account, area, people, since, until, n, ids, relax):
        where, args = [], []
        if srcs is not None:
            where.append("d.source IN (" + ",".join("?" * len(srcs)) + ")")
            args += srcs
        if account:
            where.append("d.account=?")
            args.append(str(account))
        if area:
            where.append("d.area=?")
            args.append(str(area))
        terms = _terms(people)
        if terms:
            where.append("(" + " OR ".join(["d.people LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
            args += [_like(t) for t in terms]
        if since is not None:
            where.append("d.ts>=?")
            args.append(float(since))
        if until is not None:
            where.append("d.ts<?")
            args.append(float(until))
        if ids is not None:
            where.append("d.id IN (" + ",".join("?" * len(ids)) + ")")
            args += ids
        tail = "".join(" AND " + w for w in where)
        match = fts_query(q, relaxed=bool(relax))
        if match and self.ms.fts:
            sql = ("SELECT d.*, snippet(docs_fts, -1, char(2), char(3), '…', 18) AS marked, bm25(docs_fts, 8.0, 4.0, "
                   "1.0) AS score FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE docs_fts MATCH ?" + tail +
                   " ORDER BY score, d.ts DESC LIMIT ?")
            try:
                rows = self._read(sql, [match] + args + [n])
            except sqlite3.OperationalError:
                rows = []
        else:
            if match:
                ws = re.findall(r"\w+", str(q))[:msgstore.MAX_TERMS]
                cond = ["(d.title LIKE ? ESCAPE '\\' OR d.body LIKE ? ESCAPE '\\' OR d.people LIKE ? ESCAPE '\\')"] \
                    * len(ws)
                where.append("(" + (" OR " if relax else " AND ").join(cond) + ")")
                for w in ws:
                    args += [_like(w)] * 3
            rows = self._read("SELECT d.*, d.snippet AS marked, 0 AS score FROM docs d" +
                              (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.ts DESC LIMIT ?",
                              args + [n])
        out = []
        for r in rows:
            row = self._doc_row(r)
            segs = segments(squash(r["marked"]).replace("\x02 ", " \x02").replace(" \x03", "\x03 ")) or \
                [[r["snippet"], False]]
            row.update(highlight=segs, snippet="".join(s for s, _ in segs), score=r["score"] or 0)
            out.append(row)
        return out

    def _events(self, q, account, accounts, people, since, until, n, ids, relax, areas):
        where, args = ["status != 'cancelled'"], []
        if account:
            where.append("account=?")
            args.append(str(account))
        if accounts is not None:
            where.append("account IN (" + ",".join("?" * len(accounts)) + ")" if accounts else "0")
            args += accounts
        terms = _terms(people)
        if terms:
            where.append("(" + " OR ".join(["attendees LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
            args += [_like(t) for t in terms]
        if since is not None:
            where.append("start_ts>=?")
            args.append(float(since))
        if until is not None:
            where.append("start_ts<?")
            args.append(float(until))
        if ids is not None:
            where.append("id IN (" + ",".join("?" * len(ids)) + ")")
            args += [i - EVENT_ID_BASE for i in ids]
        ws = re.findall(r"\w+", str(q or ""))[:msgstore.MAX_TERMS]
        if ws:
            cond = ["(title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' OR attendees LIKE ? ESCAPE '\\')"] * len(ws)
            where.append("(" + (" OR " if relax else " AND ").join(cond) + ")")
            for w in ws:
                args += [_like(w)] * 3
        rows = self._read("SELECT * FROM events WHERE " + " AND ".join(where) + " ORDER BY start_ts DESC LIMIT ?",
                          args + [n])
        return [dict(self._event_row(e, areas), score=0) for e in rows]

    def resolve(self, ids):
        """Item ids as the library knows them: an old index.db message or event id becomes the store's."""
        out = set()
        olds = []
        for i in ids or ():
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            out.add(i)
            if i < STORE_ID_BASE:
                olds.append(i)
        for k in range(0, len(olds), 500):
            part = olds[k:k + 500]
            out |= {r["new"] for r in self._read("SELECT new FROM old_ids WHERE old IN (" + ",".join("?" * len(part)) +
                                                 ")", part)}
        return out

    def search(self, q="", source=None, account=None, area=None, people=None, since=None, until=None, limit=20,
               offset=0, ids=None):
        """Ranked rows for the owner's words over messages, events and documents (bm25 within each; then newest), or
        the newest rows matching the filters when q is empty. Each row: id, source, account, kind, thread, title,
        people, area, ts, date (UTC ISO), url, snippet and highlight ([[text, matched], ...]). When several words
        match nothing together, the first page is searched again with any of the words (rows carry relaxed: true).
        ids (a set of item ids: a Chat scope) keeps only those items."""
        limit = max(1, min(int(limit or 20), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        srcs = _list(source)
        want_msg = [s for s in (srcs or MESSAGE_SOURCES) if s in MESSAGE_SOURCES]
        want_cal = srcs is None or "calendar" in srcs
        want_doc = None if srcs is None else [s for s in srcs if s not in MESSAGE_SOURCES and s != "calendar"]
        areas = self._areas()
        accounts = sorted(a for a, ar in areas.items() if ar == area) if area else None
        mids = eids = dids = None
        if ids is not None:
            rid = self.resolve(ids)
            mids = [i for i in rid if STORE_ID_BASE <= i < EVENT_ID_BASE][:1000]
            eids = [i for i in rid if i >= EVENT_ID_BASE][:1000]
            dids = [i for i in rid if 0 < i < STORE_ID_BASE][:1000]
        n = limit + offset

        def run(relax):
            out = []
            if want_msg and (mids is None or mids):
                got = self.ms.search(q, source=want_msg, account=account, people=people, since=since, until=until,
                                     limit=n, ids=mids, accounts=accounts, relax=relax)
                ppl = self._people_of([r["id"] for r in got])
                for r in got:
                    r.update(area=_area_at(areas, r["source"], r["account"], r["ts"]), people=ppl.get(r["id"], r["people"]))
                    r.pop("source_id", None)
                    out.append(r)
            if want_cal and (eids is None or eids):
                out += self._events(q, account, accounts, people, since, until, n, eids, relax, areas)
            if (want_doc is None or want_doc) and (dids is None or dids):
                out += self._docs(q, want_doc, account, area, people, since, until, n, dids, relax)
            return out

        match = fts_query(q)
        rows, relaxed = run(False), False
        if match and not rows and not offset and len(re.findall(r'"[^"]*"', fts_query(q, relaxed=True))) > 1:
            rows, relaxed = run(True), True
        rows.sort(key=(lambda r: (r["score"], -float(r["ts"] or 0))) if match else (lambda r: -float(r["ts"] or 0)))
        out = []
        for r in rows[offset:offset + limit]:
            r.pop("score", None)
            r.pop("relaxed", None)
            if relaxed:
                r["relaxed"] = True
            out.append(r)
        return out

    def get(self, item_id):
        """One full item (with body and source_id) by its id, or None. An old index.db id still finds its item."""
        try:
            v = int(item_id)
        except (TypeError, ValueError):
            return None
        if v >= EVENT_ID_BASE:
            r = self.ms.event_item(v)
            if r:
                e = self._read("SELECT calendar, event_id FROM events WHERE id=?", (v - EVENT_ID_BASE,))
                r.update(area=self._areas().get(r["account"], ""), date=_iso(r["ts"]),
                         thread=_series(e[0]["event_id"]) if e else "",
                         source_id=f"{e[0]['calendar']}:{e[0]['event_id']}" if e else "")
            return r
        if v >= STORE_ID_BASE:
            r = self.ms.get(v)
            if r:
                r.update(area=_area_at(self._areas(), r["source"], r["account"], r["ts"]), people=self._people_of([v]).get(v, r["people"]))
            return r
        if v < 1:
            return None
        r = self._read("SELECT * FROM docs WHERE id=?", (v,))
        if r:
            return self._doc_row(r[0], with_body=True)
        m = self._read("SELECT new FROM old_ids WHERE old=?", (v,))
        return self.get(m[0]["new"]) if m and m[0]["new"] >= STORE_ID_BASE else None

    def light(self, item_id, areas=None):
        """0.24.0 (B2): one stored message's title, text, link and area for Projects' slip check, plus its raw row
        ("raw", for the sorting rules): no instruction scan and no people join. get() on every one of 400 threads made
        Projects take 11 s on the owner's store. Anything that isn't a stored message falls back to get()."""
        try:
            v = int(item_id)
        except (TypeError, ValueError):
            return None
        if not STORE_ID_BASE <= v < EVENT_ID_BASE:
            return self.get(v)
        r = self._read("SELECT * FROM messages WHERE id=?", (v - STORE_ID_BASE,))
        if not r:
            return None
        r = dict(r[0])
        return {"id": v, "source": r["source"], "account": r["account"], "title": self.ms.title_of(r),
                "body": r["text"] or "", "url": r["link"], "ts": r["ts"], "deleted": bool(r["deleted_at"]),
                "area": _area_at(self._areas() if areas is None else areas, r["source"], r["account"], r["ts"]),
                "raw": r}

    def body_of(self, source, account, source_id):
        """A message's stored text or a document's body, or None."""
        if source in MESSAGE_SOURCES:
            r = self._read("SELECT text AS body FROM messages WHERE source=? AND account=? AND msg_id=?",
                           (str(source), str(account or ""), str(source_id or "")))
        else:
            r = self._read("SELECT body FROM docs WHERE source=? AND account=? AND source_id=?",
                           (str(source), str(account or ""), str(source_id or "")))
        return (r[0]["body"] or None) if r else None

    def _people_of(self, ids):
        """{message item id: "Name <address>; ..."} (sender first), the people field as Chat and Know me read it."""
        rids = [i - STORE_ID_BASE for i in ids if STORE_ID_BASE <= i < EVENT_ID_BASE][:500]
        if not rids:
            return {}
        rows = self._read("SELECT id, source, account, thread, direction, sender, sender_name, recipients, subject, "
                          "channel, ts FROM messages WHERE id IN (" + ",".join("?" * len(rids)) + ")", rids)
        return {STORE_ID_BASE + r["id"]: self._msg_meta(r, {})["people"] for r in rows}

    # metadata rows (Know me, Projects, People): oldest first, no bodies
    def _msg_meta(self, r, areas):
        out = r["direction"] == "out"
        try:
            rec = json.loads(r["recipients"] or "[]")
        except ValueError:
            rec = []
        ppl = [_person(r["sender_name"], r["sender"])] + \
            [_person(x.get("name") or "", x.get("address") or "") if isinstance(x, dict) else str(x) for x in rec[:50]]
        seen, people = set(), []
        for p in ppl:
            if p and p.lower() not in seen:
                seen.add(p.lower())
                people.append(p)
        kind = "sent" if out else ("email" if r["source"] == "gmail" else "message")
        return {"id": STORE_ID_BASE + r["id"], "source": r["source"], "account": r["account"], "kind": kind,
                "thread": r["thread"], "title": msgstore.MessageStore.title_of(r),
                "people": clip("; ".join(people), MAX_PEOPLE), "area": _area_at(areas, r["source"], r["account"], r["ts"]), "ts": r["ts"]}

    def _paged(self, sql, args, conv, batch):
        last_ts, last_id = float("-inf"), -1
        while True:
            rows = self._read(sql, [last_ts, last_ts, last_id] + args + [int(batch)])
            for r in rows:
                yield conv(r)
            if len(rows) < batch:
                return
            last_ts, last_id = rows[-1]["k_ts"], rows[-1]["id"]

    def rows(self, source=None, since=None, until=None, batch=2000):
        """Every item's metadata (id, source, account, kind, thread, title, people, area, ts; no body), oldest first,
        in batches with keyset paging. Read-only."""
        srcs = _list(source)
        areas = self._areas()
        span, sargs = "", []
        if since is not None:
            span += " AND {ts}>=?"
            sargs.append(float(since))
        if until is not None:
            span += " AND {ts}<?"
            sargs.append(float(until))
        parts = []
        msg = [s for s in (srcs or MESSAGE_SOURCES) if s in MESSAGE_SOURCES]
        if msg:
            parts.append(self._paged(
                "SELECT id, source, account, thread, direction, sender, sender_name, recipients, subject, channel, "
                "ts AS k_ts, ts FROM messages WHERE (ts>? OR (ts=? AND id>?)) AND deleted_at IS NULL AND same_as IS "
                "NULL AND source IN (" + ",".join("?" * len(msg)) + ")" + span.format(ts="ts") + " ORDER BY ts, id "
                "LIMIT ?", msg + sargs, lambda r: self._msg_meta(r, areas), batch))
        if srcs is None or "calendar" in srcs:
            def ev(r):
                e = self._event_row(r, areas)
                return {k: e[k] for k in ("id", "source", "account", "kind", "thread", "title", "people", "area",
                                          "ts")}
            parts.append(self._paged(
                "SELECT *, coalesce(start_ts, 0) AS k_ts FROM events WHERE (coalesce(start_ts, 0)>? OR "
                "(coalesce(start_ts, 0)=? AND id>?)) AND status != 'cancelled'" + span.format(ts="start_ts") +
                " ORDER BY coalesce(start_ts, 0), id LIMIT ?", sargs, ev, batch))
        docs = None if srcs is None else [s for s in srcs if s not in MESSAGE_SOURCES and s != "calendar"]
        if docs is None or docs:
            dw = "" if docs is None else " AND source IN (" + ",".join("?" * len(docs)) + ")"
            parts.append(self._paged(
                "SELECT id, source, account, kind, thread, title, people, area, ts, ts AS k_ts FROM docs WHERE (ts>? OR "
                "(ts=? AND id>?))" + dw + span.format(ts="ts") + " ORDER BY ts, id LIMIT ?", (docs or []) + sargs,
                lambda r: {k: r[k] for k in ("id", "source", "account", "kind", "thread", "title", "people", "area",
                                             "ts")}, batch))
        yield from heapq.merge(*parts, key=lambda r: (float(r["ts"] or 0), r["id"]))

    def bodies(self, source, since, limit=2000, chars=6000):
        """Recent items' kind, people and the first `chars` characters of the text (People's signature lines), newest
        first. Read-only."""
        if source in MESSAGE_SOURCES:
            rows = self._read("SELECT id, source, account, thread, direction, sender, sender_name, recipients, subject, "
                              "channel, ts, substr(text, 1, ?) AS body FROM messages WHERE source=? AND ts>=? AND "
                              "deleted_at IS NULL ORDER BY ts DESC LIMIT ?",
                              (int(chars), str(source), float(since), int(limit)))
            out = []
            for r in rows:
                m = self._msg_meta(r, {})
                out.append({"kind": m["kind"], "people": m["people"], "body": r["body"]})
            return out
        rows = self._read("SELECT kind, people, substr(body, 1, ?) AS body FROM docs WHERE source=? AND ts>=? "
                          "ORDER BY ts DESC LIMIT ?", (int(chars), str(source), float(since), int(limit)))
        return [{"kind": r["kind"], "people": r["people"], "body": r["body"]} for r in rows]

    def count(self, source=None, account=None):
        """How many items (messages, events, documents), optionally of one source and/or account."""
        srcs = _list(source)
        n = 0
        acc = " AND account=?" if account else ""
        aa = [str(account)] if account else []
        msg = [s for s in (srcs or MESSAGE_SOURCES) if s in MESSAGE_SOURCES]
        if msg:
            n += self._read("SELECT count(*) AS n FROM messages WHERE deleted_at IS NULL AND same_as IS NULL AND "
                            "source IN (" + ",".join("?" * len(msg)) + ")" + acc, msg + aa)[0]["n"]
        if srcs is None or "calendar" in srcs:
            n += self._read("SELECT count(*) AS n FROM events WHERE status != 'cancelled'" + acc, aa)[0]["n"]
        docs = None if srcs is None else [s for s in srcs if s not in MESSAGE_SOURCES and s != "calendar"]
        if docs is None or docs:
            dw = "" if docs is None else " AND source IN (" + ",".join("?" * len(docs)) + ")"
            n += self._read("SELECT count(*) AS n FROM docs WHERE 1" + dw + acc, (docs or []) + aa)[0]["n"]
        return n

    def stats(self):
        """{total, bytes, oldest, newest, sources: {source: {items, years: {YYYY: n}, accounts: {account: n}}}}."""
        rows = self._read(
            "SELECT source, account, strftime('%Y', ts, 'unixepoch') AS y, count(*) AS n, min(ts) AS lo, max(ts) AS hi "
            "FROM messages WHERE deleted_at IS NULL AND same_as IS NULL AND source != 'reports' GROUP BY source, "
            "account, y UNION ALL SELECT 'calendar', account, strftime('%Y', start_ts, 'unixepoch'), count(*), "
            "min(start_ts), max(start_ts) FROM events WHERE status != 'cancelled' GROUP BY account, 2 UNION ALL "
            "SELECT source, account, strftime('%Y', ts, 'unixepoch'), count(*), min(ts), max(ts) FROM docs GROUP BY "
            "source, account, 3")
        sources, total, lo, hi = {}, 0, None, None
        for r in rows:
            s = sources.setdefault(r["source"], {"items": 0, "years": {}, "accounts": {}})
            s["items"] += r["n"]
            s["years"][r["y"] or "?"] = s["years"].get(r["y"] or "?", 0) + r["n"]
            s["accounts"][r["account"]] = s["accounts"].get(r["account"], 0) + r["n"]
            total += r["n"]
            if r["lo"] is not None:
                lo = r["lo"] if lo is None else min(lo, r["lo"])
            if r["hi"] is not None:
                hi = r["hi"] if hi is None else max(hi, r["hi"])
        return {"total": total, "bytes": self.ms.size_bytes(), "oldest": lo, "newest": hi, "sources": sources}

    # --- the upgrade from index.db ------------------------------------------------------------------------------------
    @staticmethod
    def _old_message(r, own):
        """One index.db mail, Slack or Signal item -> a store record (msgstore.normalize_message's input). The 0.6.0
        scanner kept the text (mail without quoted history, at most 8,000 characters), the people ("Name <address>",
        the sender first), the title and the link; what it never kept (labels, attachments, Slack user ids) stays
        empty."""
        src, sid = r["source"], str(r["source_id"])
        ppl = [p.strip() for p in str(r["people"] or "").split(";") if p.strip()]

        def split(p):
            if p.endswith(">") and "<" in p:
                return p[:p.rindex("<")].strip(), p[p.rindex("<") + 1:-1].strip()
            return ("", p) if "@" in p else (p, "")
        out = r["kind"] == "sent" or bool(own)
        thread = str(r["thread"] or "")
        rec = {"source": src, "account": str(r["account"] or ""), "msg_id": sid, "direction": "out" if out else "in",
               "ts": float(r["ts"] or 0), "text": r["body"] or r["snippet"] or "", "link": r["url"] or "",
               "subject": "", "channel": "", "labels": [], "attachments": []}
        if src == "gmail":
            name, addr = split(ppl[0]) if ppl else ("", "")
            rec.update(thread=thread, sender=addr or name, sender_name=name if addr else "",
                       recipients=[{"name": n, "address": a} for n, a in map(split, ppl[1:])],
                       subject="" if r["title"] == "(no subject)" else (r["title"] or ""))
        elif src == "slack":
            rec.update(thread=thread or sid.rpartition(":")[0], sender="", sender_name="" if not ppl else ppl[0],
                       recipients=[{"name": p, "address": ""} for p in ppl[1:]], channel=r["title"] or "")
        else:   # signal: chat:sender:ts, the 0.5.1 store's salted ids (never a number)
            sender = sid.split(":")[1] if sid.count(":") >= 2 else ""
            rec.update(thread=thread, sender="me" if out else sender, sender_name="" if out or not ppl else ppl[0],
                       recipients=[{"name": ppl[0], "address": ""}] if out and ppl else [], channel=r["title"] or "")
        return rec

    def import_index_messages(self, data_dir, log=None, batch=IMPORT_BATCH):
        """Once: index.db's mail, Slack and Signal messages that messages.db doesn't hold, read-only from index.db, in
        short transactions. Nothing in the store is changed: a message it already has (same source, account and
        source message id; for Signal, the live reader's copy: same time to the millisecond, direction and text) is
        left as it is. Returns the counts, or None when it ran before or index.db couldn't be read (tried again at the
        next start)."""
        log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        if self.meta(MSG_IMPORT_KEY) is not None:
            return None
        path = pathlib.Path(data_dir) / "index.db"
        done = {"at": round(self.clock(), 3), "messages": 0, "same": 0, "bad": 0, "file": path.exists()}
        if not done["file"]:
            self.set_meta(MSG_IMPORT_KEY, done)
            return done
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            log(f"Callsheet library: index.db couldn't be read ({type(e).__name__}); its messages weren't imported")
            return None
        now = round(self.clock(), 3)
        try:
            have = {r[1] for r in src.execute("PRAGMA table_info(items)")}
            own_col = "own" if "own" in have else "0"
            last = 0
            while True:
                rows = src.execute(f"SELECT id, source, account, source_id, kind, thread, title, people, ts, url, "
                                   f"snippet, body, indexed_at, {own_col} AS own FROM items WHERE id>? AND source IN "
                                   f"('gmail', 'slack', 'signal') ORDER BY id LIMIT ?", (last, int(batch))).fetchall()
                if not rows:
                    break
                last = rows[-1]["id"]
                recs = []
                for r in rows:
                    try:
                        recs.append((r["id"], msgstore.normalize_message(self._old_message(r, r["own"])),
                                     r["indexed_at"] or now))
                    except (ValueError, TypeError):
                        done["bad"] += 1

                def run(db, recs=recs):
                    n = {"messages": 0, "same": 0}
                    for old, row, at in recs:
                        got = db.execute("SELECT id FROM messages WHERE source=? AND account=? AND msg_id=?",
                                         (row["source"], row["account"], row["msg_id"])).fetchone()
                        if got is None and row["source"] == "signal":
                            got = db.execute("SELECT id FROM messages WHERE source='signal' AND ts BETWEEN ? AND ? AND "
                                             "direction=? AND text=?", (row["ts"] - 0.0005, row["ts"] + 0.0005,
                                                                         row["direction"], row["text"])).fetchone()
                        if got is None:
                            cols = list(row) + ["ingested_at", "updated_at"]
                            cur = db.execute(f"INSERT INTO messages ({', '.join(cols)}) VALUES "
                                             f"({', '.join('?' * len(cols))})", [row[c] for c in row] + [at, now])
                            new = cur.lastrowid
                            n["messages"] += 1
                        else:
                            new = got[0]
                            n["same"] += 1
                        db.execute("INSERT OR IGNORE INTO old_ids (old, new) VALUES (?, ?)", (old, STORE_ID_BASE + new))
                    return n
                got = self.ms.write(run)
                done["messages"] += got["messages"]
                done["same"] += got["same"]
        except sqlite3.Error as e:
            log(f"Callsheet library: index.db's messages import stopped ({type(e).__name__}); it goes on at the next "
                "start")
            return None
        finally:
            src.close()
        self.set_meta(MSG_IMPORT_KEY, done)
        log(f"Callsheet library: kept {done['messages']} older messages from index.db ({done['same']} were already "
            "stored; index.db is kept, unused)")
        return done

    def import_index(self, data_dir, log=None):
        """Once: index.db's documents, notes, feed items and reports (with their ids), its meta (never overwriting) and
        a map of its message and event ids. index.db itself is only read (it stays on disk, unused). Returns the
        counts, or None when it ran before."""
        log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        if self.meta(IMPORT_KEY) is not None:
            return None
        path = pathlib.Path(data_dir) / "index.db"
        done = {"at": round(self.clock(), 3), "docs": 0, "ids": 0, "meta": 0, "file": path.exists()}
        if not done["file"]:
            self.set_meta(IMPORT_KEY, done)
            return done
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            log(f"Callsheet library: index.db couldn't be read ({type(e).__name__}); nothing imported")
            return None
        try:
            have = {r[1] for r in src.execute("PRAGMA table_info(items)")}
            extra = [c for c in ("instr", "instr_v", "hidden", "own") if c in have]
            last = 0
            while True:
                rows = src.execute("SELECT * FROM items WHERE id>? AND source NOT IN ('gmail', 'calendar', 'slack', "
                                   "'signal') ORDER BY id LIMIT ?", (last, IMPORT_BATCH)).fetchall()
                if not rows:
                    break
                last = rows[-1]["id"]
                vals = []
                for r in rows:
                    s = r["source"]
                    s = _OLD_SOURCES.get(s, s if s in ("drive", "github") else doc_source(s))
                    row = {f: r[f] for f in _FIELDS}
                    row["source"] = s
                    vals.append([r["id"]] + [row[f] for f in _FIELDS] + [_hash(row), r["indexed_at"]] +
                                [r[c] for c in extra])
                cols = ["id"] + list(_FIELDS) + ["hash", "indexed_at"] + extra
                sql = f"INSERT OR IGNORE INTO docs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                done["docs"] += sum(self.ms.write(lambda db, v=vals: [db.execute(sql, x).rowcount for x in v]))
            last = 0
            while True:
                rows = src.execute("SELECT id, source, account, source_id FROM items WHERE id>? AND source IN ('gmail', "
                                   "'calendar', 'slack', 'signal') ORDER BY id LIMIT ?", (last, IMPORT_BATCH)).fetchall()
                if not rows:
                    break
                last = rows[-1]["id"]
                pairs = [(r["id"], r["source"], r["account"], r["source_id"]) for r in rows]

                def run(db, pairs=pairs):
                    n = 0
                    for old, s, acc, sid in pairs:
                        if s == "calendar":
                            cal, _, eid = str(sid).partition(":")
                            got = db.execute("SELECT id FROM events WHERE account=? AND calendar=? AND event_id=?",
                                             (acc, cal, eid)).fetchone()
                            new = EVENT_ID_BASE + got[0] if got else None
                        else:
                            got = db.execute("SELECT id FROM messages WHERE source=? AND account=? AND msg_id=?",
                                             (s, acc, sid)).fetchone()
                            new = STORE_ID_BASE + got[0] if got else None
                        if new:
                            n += db.execute("INSERT OR IGNORE INTO old_ids (old, new) VALUES (?, ?)", (old, new)).rowcount
                    return n
                done["ids"] += self.ms.write(run)
            for r in src.execute("SELECT key, value FROM meta").fetchall():
                key = "docs_settings" if r["key"] == "settings" else r["key"]
                if key in _META_SKIP or key.startswith("github_etags:") or self.meta(key) is not None:
                    continue
                if key == "docs_settings":
                    try:
                        v = json.loads(r["value"])
                        v["sources"] = {_OLD_SOURCES.get(k, k): b for k, b in (v.get("sources") or {}).items()
                                        if _OLD_SOURCES.get(k, k) in READERS}
                        value = json.dumps(v)
                    except (ValueError, AttributeError):
                        continue
                else:
                    value = r["value"]
                self.ms.write(lambda db, k=key, v=value: db.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (k, v)))
                done["meta"] += 1
        except sqlite3.Error as e:
            log(f"Callsheet library: index.db import stopped ({type(e).__name__}); it runs again at the next start")
            return None
        finally:
            src.close()
        self.set_meta(IMPORT_KEY, done)
        log(f"Callsheet library: imported {done['docs']} documents and {done['meta']} settings from index.db "
            f"(index.db is kept, unused)")
        return done
