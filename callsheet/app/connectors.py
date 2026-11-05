"""0.10.0 Connectors: read-only public feeds the owner adds (owner approved 2026-11-04; docs/connectors.md).

A connector is a read-only source: the owner pastes an address in Setup → Connections → Connectors, presses Check
(Callsheet reads that one address, and for a YouTube @handle the channel page once, and shows what it found), then
Add (the consent: the host is written to `outbound_hosts` with the date). From then on the poller reads the feed every
`every_s` (2 h by default) through `net.py`, which only contacts hosts in `outbound_hosts` for that connector.

    fetch (net.Net.get) → parse (feedxml) → normalized items (kind plugin) → index.db (src = kind, account = id)
                                                                         → the Briefing's "From your channels and feeds"

A kind plugin (conn_youtube.py, conn_news.py) has:
    kind, noun, item_kind, every_s, feed_cap
    check(ctx, address, via)  -> a draft dict (the owner's Check; GETs only through ctx.get)
    feed_url(cfg)             -> the address the poller reads
    items(cfg, feed, now)     -> normalized items (pure)
A normalized item: {source (kind), connector_id, id ("yt:<videoId>", "news:<sha16>"), kind (video, short, article),
title (≤ 200, plain), published (UTC epoch, never in the future), author (the feed's, display only), url (built by
Callsheet: youtube.com/watch?v=<id>, or a story link on the feed's own site; "" otherwise), summary (plain text, hidden
HTML stripped, links defanged, ≤ 1,500), hidden_text, flagged (inject.instructions)}.

Feed text is untrusted data: it is never an instruction, never in fixed or system text, shown with textContent only,
links in it are defanged, and an item that reads like instructions is shown as text with a mark and no link. The
connector's name is the owner's label, never the feed's own claim. Nothing about the owner is sent (net.py).

Storage (callsheet.db, additive): connectors, outbound_hosts, interests. Items live in index.db (rebuildable): at most
MAX_ITEMS per connector and PRUNE_DAYS days. Logs: host, status, bytes and counts only, never titles or text.
"""
import hashlib
import json
import random
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.parse

import htmltext
import inject
import net
from store import Conflict, InputError, NotFound

KIND_IDS = ("youtube", "news")
EVERY_CHOICES = ((3600, "every hour"), (7200, "every 2 hours"), (21600, "every 6 hours"), (86400, "once a day"))
DEFAULT_EVERY = 7200
LEVELS = (("always", "Always tell me"), ("big", "If it’s big"), ("never", "Never"))
MAX_CONNECTORS = 40
MAX_INTERESTS = 20
INTEREST_MAX = 80
LABEL_MAX = 60
TITLE_MAX = 200
SUMMARY_MAX = 1500
MAX_ITEMS = 500
PRUNE_DAYS = 30
BRIEF_HOURS = 48
BRIEF_MAX = 10
DRAFT_S = 15 * 60
MAX_DRAFTS = 20
TICK_S = 60
BACKOFF_S = (3600, 3 * 3600, 12 * 3600)
FUTURE_SLACK_S = 600
CID = r"(c[0-9a-f]{8})"
IID = r"(i[0-9a-f]{8})"
DEFANG_NOTE = "(from a feed; not opened)"
FLAG_MARK = "Reads like instructions · shown as text"
HIDDEN_MARK = "Had hidden text · not shown"
NOTHING_SENT = "Nothing about you is sent."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  every_s INTEGER NOT NULL, added REAL NOT NULL, paused INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS outbound_hosts (
  host TEXT NOT NULL, connector TEXT NOT NULL, added REAL NOT NULL, how TEXT NOT NULL DEFAULT 'owner',
  PRIMARY KEY (host, connector));
CREATE TABLE IF NOT EXISTS interests (
  id TEXT PRIMARY KEY, text TEXT NOT NULL, level TEXT NOT NULL, added REAL NOT NULL);
"""


def _stderr(line):
    print(line, file=sys.stderr, flush=True)


def kinds():
    """The registry: kind id -> plugin. A new kind needs the owner's approval (CLAUDE.md) and its own tests."""
    import conn_news
    import conn_youtube
    return {"youtube": conn_youtube.YouTube(), "news": conn_news.News()}


# --- text: feed text is someone else's words ---------------------------------------------------------------------
_SPACE = re.compile(r"[ \t  -   　]+")


def clean(text, single_line=False):
    """Plain text from a feed: control, format (bidi overrides, zero-width, soft hyphen, BOM) and private-use
    characters removed, line separators to newlines, runs of spaces collapsed."""
    out = []
    for ch in str(text or ""):
        if ch in "\n\t":
            out.append(" " if single_line else ch)
            continue
        if ch in "  \r\x85":
            out.append(" " if single_line else "\n")
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn"):
            continue
        out.append(ch)
    t = _SPACE.sub(" ", "".join(out))
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def defang(text):
    """Every link in feed text becomes plain text without its scheme plus DEFANG_NOTE. The page shows feed text as
    text only; this keeps a planted link from looking like one (and from being copied as one). Idempotent."""
    if not text:
        return text

    def bare(m):
        u = m.group(0)
        while u and u[-1] in ".,;:!?)]}*_>'\"":
            if u[-1] == ")" and u.count("(") >= u.count(")"):
                break
            u = u[:-1]
        rest = m.string[m.start() + len(u):]
        if not u or rest.startswith(" " + DEFANG_NOTE):
            return m.group(0)
        shown = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u, flags=re.I)
        return clip(shown, 80) + " " + DEFANG_NOTE + m.group(0)[len(u):]
    return inject.URL.sub(bare, text)


def title_text(raw):
    """An entry or feed title as the page shows it (titles may carry HTML entities or tags in RSS)."""
    t = str(raw or "")
    if "<" in t or "&" in t:
        t = htmltext.html_text(t)[0]
    return clip(defang(clean(t, single_line=True)), TITLE_MAX)


def summary_text(raw, kind="html", limit=SUMMARY_MAX):
    """(text, hidden_text): an entry's summary as plain text, hidden HTML dropped (htmltext), links defanged."""
    raw = str(raw or "")[:200000]
    hidden = False
    if kind == "html" or re.search(r"<[a-zA-Z/!]", raw):
        raw, hidden = htmltext.html_text(raw)
    return clip(defang(clean(raw)), limit), bool(hidden)


def flagged(title, summary):
    """Does the item read like instructions to an assistant (inject.py's rules, link lures included)?"""
    return bool(inject.item_instructions(title, summary, None))


def when_ok(ts, now):
    """A feed's date, never in the future (a far-future date can't pin an item to the top) and never missing."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return float(now)
    return float(now) if ts > now + FUTURE_SLACK_S or ts <= 0 else ts


def same_site(host, allowed):
    """A link host is on the feed's site: exactly one of `allowed`, or its www./bare sibling."""
    if not host:
        return False
    for a in allowed:
        if host == a or host == "www." + a or "www." + host == a:
            return True
    return False


def story_link(href, base, link_hosts, allow_test_names=False):
    """A story link Callsheet shows: https, port 443, on the feed's own site (exact host or its www. sibling),
    resolved against the feed address; "" for anything else (another host, http:, javascript:, data:, a look-alike)."""
    href = str(href or "").strip()
    if not href or len(href) > 2000:
        return ""
    try:
        u = urllib.parse.urljoin(base, href)
        host, target = net.split_url(u, allow_test_names)
    except (ValueError, net.NetError):
        return ""
    if not same_site(host, link_hosts):
        return ""
    return f"https://{host}{target}"


def item_id(prefix, *parts):
    return prefix + ":" + hashlib.sha256("\n".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:16]


def every_label(s):
    for v, label in EVERY_CHOICES:
        if v == s:
            return label
    return f"every {max(1, round(s / 3600))} hours"


def _iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else None


# --- the framework ---------------------------------------------------------------------------------------------
class Ctx:
    """What a kind's check() may use: GETs for the owner's Check (purpose "check") and the test-name switch."""

    def __init__(self, conns):
        self.c = conns
        self.allow_test_names = conns.net.allow_test_names
        self.fetched = []           # [(host, bytes)] for the log

    def get(self, url, hosts, cap, truncate=False):
        a = self.c.net.get(url, hosts, cap, purpose="check", truncate=truncate)
        self.fetched.append((net.host_of(url, self.allow_test_names), len(a.body)))
        return a


class Connectors:
    def __init__(self, store, index, net_=None, clock=time.time, log=None, rand=random.random, kinds_=None):
        self.store, self.index = store, index
        self.clock = clock
        self.net = net_ or net.Net(clock=clock)
        self.log = log or _stderr
        self.rand = rand
        self.kinds = kinds_ or kinds()
        self.lock = threading.RLock()       # drafts and the read loop
        self.read_lock = threading.Lock()   # one poll pass at a time
        self.drafts = {}                    # token -> (expires, draft with items)
        self.stop_event = threading.Event()
        self.thread = None
        with store.lock:
            store.db.executescript(_SCHEMA)

    # rows -----------------------------------------------------------------------------------------------------
    def _rows(self):
        with self.store.lock:
            return self.store.db.execute("SELECT * FROM connectors ORDER BY added, id").fetchall()

    def _row(self, cid):
        with self.store.lock:
            r = self.store.db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise NotFound("That connector isn't there any more.")
        return r

    def _hosts_of(self, cid):
        with self.store.lock:
            return [r["host"] for r in self.store.db.execute(
                "SELECT host FROM outbound_hosts WHERE connector=? ORDER BY host", (cid,)).fetchall()]

    @staticmethod
    def _cfg(r):
        try:
            v = json.loads(r["config"] or "{}")
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}

    @staticmethod
    def _state(r):
        try:
            v = json.loads(r["state"] or "{}")
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}

    def _set_state(self, cid, st):
        with self.store.lock:
            self.store.db.execute("UPDATE connectors SET state=? WHERE id=?", (json.dumps(st), cid))

    def _count(self, kind, cid):
        if self.index is None:
            return 0
        with self.index.lock:
            return self.index.db.execute("SELECT COUNT(*) FROM items WHERE source=? AND account=?",
                                         (kind, cid)).fetchone()[0]

    def public(self, r):
        cfg, st = self._cfg(r), self._state(r)
        k = self.kinds.get(r["kind"])
        hosts = self._hosts_of(r["id"])
        status = "paused" if r["paused"] else ("error" if st.get("error") else ("ok" if st.get("last_read") else "new"))
        out = {"id": r["id"], "kind": r["kind"], "noun": k.noun if k else r["kind"], "label": r["label"],
               "host": hosts[0] if hosts else "", "hosts": hosts, "link_hosts": cfg.get("link_hosts") or [],
               "every_s": r["every_s"], "every": every_label(r["every_s"]), "paused": bool(r["paused"]),
               "added": _day(r["added"]), "last_read": _iso(st.get("last_read")), "last_new": st.get("last_new", 0),
               "items": self._count(r["kind"], r["id"]), "status": status, "error": st.get("error") or None,
               "next_at": _iso(st.get("next_at"))}
        if r["kind"] == "youtube":
            out["skip_shorts"] = bool(cfg.get("skip_shorts"))
        return out

    def view(self):
        return {"connectors": [self.public(r) for r in self._rows()], "hosts": self.hosts(),
                "interests": self.interests(), "levels": [list(x) for x in LEVELS],
                "every_choices": [list(x) for x in EVERY_CHOICES], "max": MAX_CONNECTORS,
                "max_interests": MAX_INTERESTS, "nothing_sent": NOTHING_SENT}

    def hosts(self):
        """Places Callsheet contacts for connectors: each host with the date the owner added it and its connectors."""
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT o.host, MIN(o.added) AS added, GROUP_CONCAT(c.label, ' · ') AS labels, COUNT(*) AS n "
                "FROM outbound_hosts o JOIN connectors c ON c.id = o.connector GROUP BY o.host ORDER BY o.host"
            ).fetchall()
        return [{"host": r["host"], "added": _day(r["added"]), "connectors": r["labels"] or "", "n": r["n"]}
                for r in rows]

    # the owner's Check ----------------------------------------------------------------------------------------
    def _drafts_gc(self):
        now = self.clock()
        for t in [t for t, (exp, _d) in self.drafts.items() if exp < now]:
            del self.drafts[t]
        while len(self.drafts) >= MAX_DRAFTS:
            del self.drafts[min(self.drafts, key=lambda t: self.drafts[t][0])]

    def _keep(self, draft):
        with self.lock:
            self._drafts_gc()
            token = secrets.token_urlsafe(18)
            self.drafts[token] = (self.clock() + DRAFT_S, draft)
        return token

    def _draft(self, token):
        with self.lock:
            self._drafts_gc()
            d = self.drafts.get(str(token or ""))
        if d is None:
            raise Conflict("That check is too old. Press Check again.")
        return d[1]

    def check(self, body):
        if not isinstance(body, dict):
            raise InputError("Send the address as a JSON object.")
        kind = body.get("kind")
        k = self.kinds.get(kind)
        if k is None:
            raise InputError("Choose a YouTube channel or a news feed.")
        address = str(body.get("address") or "").strip()
        if not address or len(address) > 2000:
            raise InputError("Paste an address first.")
        via = self._draft(body["via"]) if body.get("via") else None
        ctx = Ctx(self)
        try:
            draft = k.check(ctx, address, via)
        except net.NetError as e:
            raise InputError(str(e)) from None
        finally:
            for h, n in ctx.fetched:
                self.log(f"Callsheet connectors: check {h}, {n} bytes")
        if draft.get("stage") == "other_host":
            token = self._keep(draft)
            return {"stage": "other_host", "token": token, "host": draft["host"], "feed_url": draft["feed_url"],
                    "title": draft.get("title") or ""}
        dup = self._duplicate(kind, draft["config"].get("feed_url"))
        draft["fetched_at"] = self.clock()
        token = self._keep(draft)
        out = {"stage": "found", "token": token, "kind": kind, "noun": k.noun, "title": draft["title"],
               "label": clip(draft["title"], LABEL_MAX) or k.noun, "latest": draft["latest"],
               "host": draft["host"], "link_hosts": draft.get("link_hosts") or [], "every_s": k.every_s,
               "every": every_label(k.every_s), "note": draft.get("note") or None,
               "hostline": self.hostline(k, draft["host"], k.every_s, draft.get("link_hosts")),
               "duplicate": dup}
        if kind == "youtube":
            out["shorts_seen"] = draft.get("shorts_seen", 0)
        return out

    @staticmethod
    def hostline(k, host, every_s, link_hosts=None):
        """The consent line next to Add: the one place contacted, how often, and that nothing about the owner goes."""
        return f"Callsheet will read {host} {every_label(every_s)}. {NOTHING_SENT}"

    def _duplicate(self, kind, feed_url):
        for r in self._rows():
            if r["kind"] == kind and self._cfg(r).get("feed_url") == feed_url:
                return r["label"]
        return None

    # Add, change, remove ---------------------------------------------------------------------------------------
    def add(self, body):
        if not isinstance(body, dict):
            raise InputError("Send the connector as a JSON object.")
        d = self._draft(body.get("token"))
        if d.get("stage") == "other_host":
            raise InputError("Check the feed's own address first.")
        k = self.kinds[d["kind"]]
        label = clean(body.get("label") if body.get("label") is not None else d["title"], single_line=True)
        label = clip(label, LABEL_MAX) if label else clip(clean(d["title"], True), LABEL_MAX) or k.noun
        if self._duplicate(d["kind"], d["config"].get("feed_url")):
            raise Conflict("You've already added this " + ("channel." if d["kind"] == "youtube" else "feed."))
        if len(self._rows()) >= MAX_CONNECTORS:
            raise Conflict(f"Callsheet reads at most {MAX_CONNECTORS} connectors. Remove one first.")
        now = self.clock()
        cid = "c" + secrets.token_hex(4)
        cfg = dict(d["config"])
        cfg["link_hosts"] = d.get("link_hosts") or []
        st = {"last_read": d["fetched_at"] if d.get("items") is not None else None, "last_new": 0,
              "next_at": self._next(now, k.every_s)}
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("INSERT INTO connectors (id, kind, label, config, every_s, added, paused, state) "
                                      "VALUES (?,?,?,?,?,?,0,?)", (cid, d["kind"], label, json.dumps(cfg),
                                                                   k.every_s, now, json.dumps(st)))
                # the owner's Add is the consent for this host (CLAUDE.md, 0.10.0)
                self.store.db.execute("INSERT OR IGNORE INTO outbound_hosts (host, connector, added, how) "
                                      "VALUES (?,?,?,'owner')", (d["host"], cid, now))
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
        with self.lock:
            self.drafts.pop(str(body.get("token")), None)
        if d.get("items") is not None:
            self.net.note_poll(d["host"], d["fetched_at"])
            st["last_new"] = self._store_items(self._row(cid), d["items"])
            self._set_state(cid, st)
        self.log(f"Callsheet connectors: added {cid} ({d['kind']}, {d['host']})")
        return self.public(self._row(cid))

    def patch(self, cid, body):
        if not isinstance(body, dict):
            raise InputError("Send the change as a JSON object.")
        r = self._row(cid)
        cfg = self._cfg(r)
        sets, args = [], []
        for key, val in body.items():
            if key == "label":
                v = clip(clean(val, single_line=True), LABEL_MAX) if isinstance(val, str) else ""
                if not v:
                    raise InputError(f"A name must be 1 to {LABEL_MAX} characters.")
                sets.append("label=?")
                args.append(v)
            elif key == "paused":
                if not isinstance(val, bool):
                    raise InputError("paused takes true or false.")
                sets.append("paused=?")
                args.append(1 if val else 0)
            elif key == "every_s":
                if val not in [x[0] for x in EVERY_CHOICES]:
                    raise InputError("Choose how often from the list.")
                sets.append("every_s=?")
                args.append(int(val))
                st = self._state(r)
                st["next_at"] = min(st.get("next_at") or self.clock(), self.clock() + int(val))
                sets.append("state=?")
                args.append(json.dumps(st))
            elif key == "skip_shorts" and r["kind"] == "youtube":
                if not isinstance(val, bool):
                    raise InputError("skip_shorts takes true or false.")
                cfg["skip_shorts"] = val
                sets.append("config=?")
                args.append(json.dumps(cfg))
            else:
                raise InputError(f"{key} can't be changed here.")
        if sets:
            with self.store.lock:
                self.store.db.execute(f"UPDATE connectors SET {', '.join(sets)} WHERE id=?", args + [cid])
        return self.public(self._row(cid))

    def remove(self, cid):
        """The connector, its hosts (a host no other connector uses leaves Places Callsheet contacts) and its items."""
        r = self._row(cid)
        with self.store.lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("DELETE FROM outbound_hosts WHERE connector=?", (cid,))
                self.store.db.execute("DELETE FROM connectors WHERE id=?", (cid,))
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
        n = self.index.delete(r["kind"], cid) if self.index is not None else 0
        self.log(f"Callsheet connectors: removed {cid} ({n} items)")
        return self.view()

    def remove_host(self, host):
        """Every connector that reads `host` goes (the host leaves Places Callsheet contacts)."""
        host = str(host or "").lower()
        with self.store.lock:
            ids = [r["connector"] for r in self.store.db.execute(
                "SELECT connector FROM outbound_hosts WHERE host=?", (host,)).fetchall()]
        if not ids:
            raise NotFound("Callsheet doesn't contact that place.")
        for cid in ids:
            try:
                self.remove(cid)
            except NotFound:
                continue
        return self.view()

    # reading --------------------------------------------------------------------------------------------------
    def _next(self, now, every_s):
        return now + every_s * (0.9 + 0.2 * self.rand())

    def due(self):
        now = self.clock()
        out = []
        for r in self._rows():
            if r["paused"]:
                continue
            st = self._state(r)
            if (st.get("next_at") or 0) <= now:
                out.append(r)
        return out

    def tick(self):
        """One pass: read every due connector whose host may be polled now (net's 15-minute host gap). Returns the
        number read."""
        if not self.read_lock.acquire(blocking=False):
            return 0
        try:
            n = 0
            for r in self.due():
                if self.stop_event.is_set():
                    break
                hosts = self._hosts_of(r["id"])
                if not hosts or self.net.wait_s(hosts[0]) > 0:
                    continue
                self.read(r["id"])
                n += 1
            return n
        finally:
            self.read_lock.release()

    def read(self, cid):
        """Read one connector now (the poller). Returns its public view."""
        r = self._row(cid)
        k = self.kinds.get(r["kind"])
        cfg, st = self._cfg(r), self._state(r)
        now = self.clock()
        hosts = self._hosts_of(cid)
        if k is None or not hosts:
            st.update(error="This connector can't be read. Remove it and add it again.", next_at=now + BACKOFF_S[-1])
            self._set_state(cid, st)
            return self.public(self._row(cid))
        url = k.feed_url(cfg)
        cond = {}
        if self._count(r["kind"], cid):          # after an index wipe, read everything again
            if st.get("etag"):
                cond["If-None-Match"] = st["etag"]
            if st.get("last_modified"):
                cond["If-Modified-Since"] = st["last_modified"]
        try:
            a = self.net.get(url, hosts, k.feed_cap, purpose="poll", headers=cond)
        except net.RateLimited as e:
            st["next_at"] = now + max(e.wait, 60)
            if e.status:
                st["error"] = str(e)
            self._set_state(cid, st)
            self.log(f"Callsheet connectors: {hosts[0]} wait {int(e.wait)} s")
            return self.public(self._row(cid))
        except net.NetError as e:
            fails = int(st.get("fails") or 0) + 1
            st.update(error=str(e), fails=fails, next_at=now + BACKOFF_S[min(fails, len(BACKOFF_S)) - 1])
            self._set_state(cid, st)
            self.log(f"Callsheet connectors: {hosts[0]} error {e.why} {e.status or ''}".rstrip())
            return self.public(self._row(cid))
        if a.not_modified:
            st.update(error=None, fails=0, last_read=now, last_new=0, next_at=self._next(now, r["every_s"]))
            self._set_state(cid, st)
            self.log(f"Callsheet connectors: {hosts[0]} 304")
            return self.public(self._row(cid))
        import feedxml
        try:
            feed = feedxml.parse(a.body)
        except feedxml.FeedError as e:
            fails = int(st.get("fails") or 0) + 1
            st.update(error=f"{hosts[0]} sent something Callsheet couldn't read: {e}", fails=fails,
                      next_at=now + BACKOFF_S[min(fails, len(BACKOFF_S)) - 1])
            self._set_state(cid, st)
            self.log(f"Callsheet connectors: {hosts[0]} 200, {len(a.body)} bytes, not a feed")
            return self.public(self._row(cid))
        items = k.items(cfg, feed, now, self.net.allow_test_names)
        new = self._store_items(r, items)
        st.update(error=None, fails=0, last_read=now, last_new=new, next_at=self._next(now, r["every_s"]),
                  etag=(a.headers.get("etag") or "")[:200] or None,
                  last_modified=(a.headers.get("last-modified") or "")[:100] or None)
        self._set_state(cid, st)
        self.log(f"Callsheet connectors: {hosts[0]} 200, {len(a.body)} bytes, {len(items)} items, {new} new")
        return self.public(self._row(cid))

    def _store_items(self, r, items):
        """Items into index.db (src = kind, account = connector id; the index flags planted instructions). Skip
        Shorts applies here. Returns the number of new items."""
        if self.index is None:
            return 0
        cfg = self._cfg(r)
        rows = []
        for it in items:
            if it.get("kind") == "short" and cfg.get("skip_shorts"):
                continue
            rows.append({"source": r["kind"], "account": r["id"], "source_id": it["id"], "kind": it["kind"],
                         "title": it["title"], "people": it.get("author") or "", "ts": it["published"],
                         "url": it.get("url") or "", "body": it.get("summary") or "",
                         "hidden_text": bool(it.get("hidden_text"))})
        n = self.index.commit_step(rows)
        self._prune(r["kind"], r["id"])
        return n.get("added", 0)

    def _prune(self, kind, cid):
        cut = self.clock() - PRUNE_DAYS * 86400
        with self.index.lock:
            self.index.db.execute(
                "DELETE FROM items WHERE source=? AND account=? AND (ts<? OR id NOT IN (SELECT id FROM items WHERE "
                "source=? AND account=? ORDER BY ts DESC, id DESC LIMIT ?))", (kind, cid, cut, kind, cid, MAX_ITEMS))

    # the Briefing's list --------------------------------------------------------------------------------------
    def brief_list(self, hours=BRIEF_HOURS, limit=BRIEF_MAX):
        """"From your channels and feeds": the newest items of the active connectors, newest first. None when the
        owner has no connectors. Feed text only as text; links only Callsheet-built and never on flagged items."""
        rows = [r for r in self._rows()]
        if not rows:
            return None
        if self.index is None:
            return {"items": [], "more": 0, "hours": hours, "connectors": len(rows)}
        since = self.clock() - hours * 3600
        found = []
        for r in rows:
            if r["paused"]:
                continue
            skip = r["kind"] == "youtube" and self._cfg(r).get("skip_shorts")
            with self.index.lock:
                got = self.index.db.execute(
                    "SELECT id, source, source_id, kind, title, people, ts, url, body, instr, instr_v, hidden "
                    "FROM items WHERE source=? AND account=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                    (r["kind"], r["id"], since, limit * 3)).fetchall()
            for x in got:
                if skip and x["kind"] == "short":
                    continue
                found.append((r, x))
        found.sort(key=lambda p: (-p[1]["ts"], p[1]["id"]))
        out = []
        for r, x in found[:limit]:
            flag = bool(x["instr"]) if x["instr_v"] == inject.VERSION else flagged(x["title"], x["body"])
            k = self.kinds.get(r["kind"])
            out.append({"id": x["id"], "connector": r["id"], "label": r["label"], "noun": k.noun if k else "",
                        "kind": x["kind"], "title": x["title"], "when": _iso(x["ts"]),
                        "url": "" if flag else _shown_link(x["url"], r, self._cfg(r)),
                        "flagged": flag, "hidden": bool(x["hidden"]),
                        "marks": ([["instructions", FLAG_MARK]] if flag else []) +
                                 ([["hidden", HIDDEN_MARK]] if x["hidden"] else []) +
                                 ([["short", "Short"]] if x["kind"] == "short" else [])})
        return {"items": out, "more": max(0, len(found) - limit), "hours": hours, "connectors": len(rows)}

    # interests ("What to watch for", stored for 0.10.2) --------------------------------------------------------
    def interests(self):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM interests ORDER BY added, rowid").fetchall()
        return [{"id": r["id"], "text": r["text"], "level": r["level"]} for r in rows]

    @staticmethod
    def _interest_text(val):
        if not isinstance(val, str):
            raise InputError("Write something to watch for.")
        t = clean(val, single_line=True)
        t = clean(inject.URL.sub(" ", t), single_line=True)
        if not t:
            raise InputError("Write something to watch for, e.g. “drone rules in Canada”.")
        if len(t) > INTEREST_MAX:
            raise InputError(f"Keep it under {INTEREST_MAX} characters.")
        if inject.instructions(t):
            raise InputError("That reads like an instruction to the assistant. Write a topic instead, "
                             "e.g. “Home Assistant releases”.")
        return t

    @staticmethod
    def _level(val):
        if val not in [x[0] for x in LEVELS]:
            raise InputError("Choose Always tell me, If it’s big or Never.")
        return val

    def add_interest(self, body):
        if not isinstance(body, dict):
            raise InputError("Send it as a JSON object.")
        t = self._interest_text(body.get("text"))
        level = self._level(body.get("level", "always"))
        have = self.interests()
        if len(have) >= MAX_INTERESTS:
            raise Conflict(f"Up to {MAX_INTERESTS} things to watch for. Remove one first.")
        if any(x["text"].lower() == t.lower() for x in have):
            raise Conflict("That's already on the list.")
        iid = "i" + secrets.token_hex(4)
        with self.store.lock:
            self.store.db.execute("INSERT INTO interests (id, text, level, added) VALUES (?,?,?,?)",
                                  (iid, t, level, self.clock()))
        return {"interests": self.interests()}

    def patch_interest(self, iid, body):
        if not isinstance(body, dict):
            raise InputError("Send the change as a JSON object.")
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM interests WHERE id=?", (iid,)).fetchone() is None:
                raise NotFound("That isn't on the list any more.")
        for key, val in body.items():
            if key == "level":
                v = self._level(val)
                with self.store.lock:
                    self.store.db.execute("UPDATE interests SET level=? WHERE id=?", (v, iid))
            elif key == "text":
                v = self._interest_text(val)
                with self.store.lock:
                    self.store.db.execute("UPDATE interests SET text=? WHERE id=?", (v, iid))
            else:
                raise InputError(f"{key} can't be changed here.")
        return {"interests": self.interests()}

    def delete_interest(self, iid):
        with self.store.lock:
            cur = self.store.db.execute("DELETE FROM interests WHERE id=?", (iid,))
        if not cur.rowcount:
            raise NotFound("That isn't on the list any more.")
        return {"interests": self.interests()}

    # the thread -----------------------------------------------------------------------------------------------
    def start_thread(self):
        self.thread = threading.Thread(target=self._loop, name="callsheet-connectors", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        while not self.stop_event.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 (type only)
                self.log(f"Callsheet connectors: loop error: {type(e).__name__}")


def _shown_link(url, r, cfg):
    """The stored link checked once more at display time: https and on the connector's own site."""
    host = net.host_of(url, True)
    if not host:
        return ""
    if r["kind"] == "youtube":
        return url if host == "www.youtube.com" and re.fullmatch(
            r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}", url) else ""
    return url if same_site(host, cfg.get("link_hosts") or []) else ""


class ConnectorsApi:
    def __init__(self, conns):
        self.c = conns

    def routes(self):
        return [("GET", r"connectors", lambda b: self.c.view()),
                ("POST", r"connectors/check", lambda b: self.c.check(b)),
                ("POST", r"connectors", lambda b: self.c.add(b)),
                ("PATCH", rf"connectors/{CID}", lambda b, cid: self.c.patch(cid, b)),
                ("DELETE", rf"connectors/{CID}", lambda b, cid: self.c.remove(cid)),
                ("DELETE", r"connectors/hosts/([a-z0-9.-]{1,253})", lambda b, h: self.c.remove_host(h)),
                ("POST", r"connectors/interests", lambda b: self.c.add_interest(b)),
                ("PATCH", rf"connectors/interests/{IID}", lambda b, iid: self.c.patch_interest(iid, b)),
                ("DELETE", rf"connectors/interests/{IID}", lambda b, iid: self.c.delete_interest(iid))]
