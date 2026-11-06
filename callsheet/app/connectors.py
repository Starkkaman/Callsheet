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

KIND_IDS = ("youtube", "news", "reports")
EVERY_CHOICES = ((3600, "every hour"), (7200, "every 2 hours"), (21600, "every 6 hours"), (86400, "once a day"))
DEFAULT_EVERY = 7200
LEVELS = (("always", "Always tell me"), ("big", "If it’s big"), ("never", "Never"))
# 0.11.1: "When to tell you" for every kind (reports had it since 0.10.2a; owner's decision 2026-11-06), stored per
# connector in its config for the rundown; today "always" only lists a channel's or feed's items first on Today
TELLS = (("always", "Always tell me"), ("important", "If it’s important"), ("list", "Only in the list"))
TELL_DEFAULT = "important"
TELL_WORDS = "Choose Always tell me, If it’s important or Only in the list."
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
ALT_S = 16 * 60        # a kind's other feed address is tried after the host's 15-minute gap
LINK_OFFERS = 3
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
    """The registry: kind id -> plugin. A new kind needs the owner's approval (CLAUDE.md) and its own tests.
    0.10.2a: "reports" (owner approved 2026-11-05), a local folder kind (`local`: no host, no net.py)."""
    import conn_news
    import conn_reports
    import conn_youtube
    return {"youtube": conn_youtube.YouTube(), "news": conn_news.News(), "reports": conn_reports.Reports()}


def is_local(k):
    """A kind that reads a local folder (0.10.2a reports): no host, no network, its own read()."""
    return bool(getattr(k, "local", False))


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


def defang(text, note=DEFANG_NOTE):
    """Every link in feed text becomes plain text without its scheme plus `note` (DEFANG_NOTE; reports have their own). The page shows feed text as
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
        if not u or rest.startswith(" " + note):
            return m.group(0)
        shown = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u, flags=re.I)
        return clip(shown, 80) + " " + note + m.group(0)[len(u):]
    return inject.URL.sub(bare, text)


def _plain_title(raw):
    t = str(raw or "")
    if "<" in t or "&" in t:
        t = htmltext.html_text(t)[0]
    return clean(t, single_line=True)


def _plain_summary(raw, kind):
    raw = str(raw or "")[:200000]
    hidden = False
    if kind == "html" or re.search(r"<[a-zA-Z/!]", raw):
        raw, hidden = htmltext.html_text(raw)
    return clean(raw), bool(hidden)


def title_text(raw):
    """An entry or feed title as the page shows it (titles may carry HTML entities or tags in RSS)."""
    return clip(defang(_plain_title(raw)), TITLE_MAX)


def summary_text(raw, kind="html", limit=SUMMARY_MAX):
    """(text, hidden_text): an entry's summary as plain text, hidden HTML dropped (htmltext), links defanged."""
    t, hidden = _plain_summary(raw, kind)
    return clip(defang(t), limit), hidden


def entry_text(raw_title, raw_summary, summary_type="html"):
    """(title, summary, hidden_text, keys) for one feed entry. inject's rules (link lures included) read the ORIGINAL
    text, links intact, before defang() rewrites them (a lure needs its link); the title and summary returned are the
    shown, defanged form. `keys`: the flagged sentences' hashes (what index.db stores as `instr`), [] when none."""
    t = _plain_title(raw_title)
    s, hidden = _plain_summary(raw_summary, summary_type)
    keys = inject.keys(t, s[:4 * SUMMARY_MAX], None)
    return clip(defang(t), TITLE_MAX), clip(defang(s), SUMMARY_MAX), hidden, keys


def flagged(title, summary):
    """Does the item read like instructions to an assistant (inject.py's rules, link lures included)? On text that
    is already defanged this can miss a link lure: items carry the flag found on the original text (entry_text)."""
    return bool(inject.item_instructions(title, summary, None))


# 0.10.0 follow-up: a story link on another host of the feed's own site (feeds.example.com -> www.example.com) is
# offered to the owner once in Setup, never allowed by itself. Without the public suffix list (stdlib only), these
# suffixes count as one label so a.co.uk and b.co.uk, or two people's blogs on one platform, are never siblings.
_SUFFIX2 = frozenset(
    "co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk police.uk com.au net.au org.au edu.au "
    "gov.au asn.au id.au co.nz org.nz net.nz govt.nz ac.nz co.jp ne.jp or.jp ac.jp go.jp com.br net.br org.br "
    "gov.br co.in net.in org.in gov.in ac.in co.za org.za gov.za ac.za com.cn net.cn org.cn gov.cn com.hk org.hk "
    "com.tw org.tw com.sg org.sg gov.sg com.mx org.mx gob.mx com.ar com.tr gov.tr co.kr or.kr go.kr co.il org.il "
    "ac.il com.my com.ph com.pk com.ng com.eg com.sa com.ua co.id or.id ac.id "
    "github.io gitlab.io blogspot.com wordpress.com substack.com tumblr.com medium.com netlify.app vercel.app "
    "pages.dev herokuapp.com appspot.com web.app firebaseapp.com cloudfront.net azurewebsites.net "
    "s3.amazonaws.com fly.dev onrender.com glitch.me neocities.org wixsite.com squarespace.com ghost.io".split())


def site_of(host):
    """The registrable domain of a host, approximately: the last two labels, three under a listed suffix."""
    parts = str(host or "").lower().rstrip(".").split(".")
    if len(parts) < 2:
        return ""
    n = 3 if ".".join(parts[-2:]) in _SUFFIX2 else 2
    return ".".join(parts[-n:]) if len(parts) >= n else ""


def link_sibling(host, feed_hosts):
    """A story link's host that isn't allowed but sits on the same registrable domain as the feed: Setup asks."""
    site = site_of(host)
    return bool(site) and not same_site(host, feed_hosts) and any(site_of(f) == site for f in feed_hosts)


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


def link_offer(href, base, link_hosts, allow_test_names=False):
    """The host to ask the owner about when story_link() dropped a link: an https link on another host of the feed's
    own registrable domain (feeds.example.com -> news.example.com). "" for anything else (never offered)."""
    try:
        u = urllib.parse.urljoin(base, str(href or "").strip())
        host = net.split_url(u, allow_test_names)[0]
    except (ValueError, net.NetError):
        return ""
    return host if link_sibling(host, link_hosts) else ""


def item_id(prefix, *parts):
    return prefix + ":" + hashlib.sha256("\n".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:16]


def every_label(s):
    for v, label in EVERY_CHOICES:
        if v == s:
            return label
    if s < 3600:
        return f"every {max(1, round(s / 60))} minutes"
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
            for k in self.kinds.values():          # 0.10.2a: a kind's own tables (reports: report_files)
                if getattr(k, "schema", None):
                    store.db.executescript(k.schema)

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
                "SELECT host FROM outbound_hosts WHERE connector=? AND how<>'link' ORDER BY host", (cid,)).fetchall()]

    def _link_rows(self, cid):
        """Story-link hosts the owner allowed for this connector (never contacted: links only)."""
        with self.store.lock:
            return self.store.db.execute("SELECT host, added FROM outbound_hosts WHERE connector=? AND how='link' "
                                         "ORDER BY host", (cid,)).fetchall()

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
               "next_at": _iso(st.get("next_at")),
               "links_allowed": [{"host": x["host"], "added": _day(x["added"])} for x in self._link_rows(r["id"])],
               "link_offer": [h for h in (st.get("link_offer") or []) if isinstance(h, str)][:LINK_OFFERS]}
        if r["kind"] == "youtube":
            out["skip_shorts"] = bool(cfg.get("skip_shorts"))
        tell = cfg.get("tell") if cfg.get("tell") in dict(k_tells(k)) else TELL_DEFAULT   # 0.11.1: every kind
        out.update(tell=tell, tell_label=dict(k_tells(k))[tell], tells=[list(x) for x in k_tells(k)])
        if is_local(k):
            out = k.public(self, r, out)
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
                "FROM outbound_hosts o JOIN connectors c ON c.id = o.connector WHERE o.how<>'link' GROUP BY o.host "
                "ORDER BY o.host"
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
            raise InputError("Choose a YouTube channel, a news feed or a reports folder.")
        address = str(body.get("address") or "").strip()
        if not address or len(address) > 2000:
            raise InputError("Pick a folder first." if kind == "reports" else "Paste an address first.")
        via = self._draft(body["via"]) if body.get("via") else None
        ctx = Ctx(self)
        extra = {}
        if is_local(k) and body.get("where") is not None:   # 0.12.1 reports: where="drive" with the account
            extra = {"where": body.get("where"), "account": body.get("account")}
        try:
            draft = k.check(ctx, address, via, **extra)
        except net.NetError as e:
            raise InputError(str(e)) from None
        finally:
            for h, n in ctx.fetched:
                self.log(f"Callsheet connectors: check {h}, {n} bytes")
        if is_local(k):           # 0.10.2a reports: a folder, nothing contacted
            dup = self._duplicate(kind, k.key(draft["config"]))
            draft["fetched_at"] = self.clock()
            token = self._keep(draft)
            out = {key: draft.get(key) for key in ("title", "label", "folder", "latest", "files", "unreadable",
                                                   "unreadable_n", "instruction", "note")}
            out.update(where=draft.get("where") or "folder", email=draft.get("email"))   # 0.12.1
            out.update(stage="found", token=token, kind=kind, noun=k.noun, host="", every_s=k.every_s,
                       every=every_label(k.every_s), duplicate=dup,
                       hostline=draft.get("hostline") or k.hostline(draft["folder"], k.every_s),
                       tells=[list(x) for x in k_tells(k)], link_hosts=[], link_offer=[])
            return out
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
               "duplicate": dup, "link_offer": draft.get("link_offer") or [], "tells": [list(x) for x in k_tells(k)]}
        if kind == "youtube":
            out["shorts_seen"] = draft.get("shorts_seen", 0)
        return out

    @staticmethod
    def hostline(k, host, every_s, link_hosts=None):
        """The consent line next to Add: the one place contacted, how often, and that nothing about the owner goes."""
        return f"Callsheet will read {host} {every_label(every_s)}. {NOTHING_SENT}"

    def _duplicate(self, kind, feed_url):
        k = self.kinds.get(kind)
        for r in self._rows():
            if r["kind"] == kind and (k.key(self._cfg(r)) if is_local(k) else self._cfg(r).get("feed_url")) == feed_url:
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
        if is_local(k):
            return self._add_local(k, d, body)
        label = clean(body.get("label") if body.get("label") is not None else d["title"], single_line=True)
        label = clip(label, LABEL_MAX) if label else clip(clean(d["title"], True), LABEL_MAX) or k.noun
        tell = body.get("tell", TELL_DEFAULT)   # 0.11.1: When to tell you, for channels and feeds too
        if tell not in [x[0] for x in k_tells(k)]:
            raise InputError(TELL_WORDS)
        if self._duplicate(d["kind"], d["config"].get("feed_url")):
            raise Conflict("You've already added this " + ("channel." if d["kind"] == "youtube" else "feed."))
        if len(self._rows()) >= MAX_CONNECTORS:
            raise Conflict(f"Callsheet reads at most {MAX_CONNECTORS} connectors. Remove one first.")
        now = self.clock()
        cid = "c" + secrets.token_hex(4)
        cfg = dict(d["config"], tell=tell)
        cfg["link_hosts"] = d.get("link_hosts") or []
        st = {"last_read": d["fetched_at"] if d.get("items") is not None else None, "last_new": 0,
              "next_at": self._next(now, k.every_s), "feed_i": int(d.get("feed_i") or 0),
              "link_offer": list(d.get("link_offer") or [])[:LINK_OFFERS]}
        if d.get("items") is None and d.get("note"):      # Check couldn't read a feed: say so in Setup at once
            st.update(error=getattr(k, "missing", None) or d["note"], fails=len(_feed_urls(k, cfg)), next_at=now + BACKOFF_S[0])
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

    def _add_local(self, k, d, body):
        """0.10.2a reports: the folder the owner checked, the name and the owner's choice (Always tell me, If it's
        important, Only in the list); no host is written (nothing is contacted). The first look happens at once."""
        label = clean(body.get("label") if body.get("label") is not None else d.get("label"), single_line=True)
        label = clip(label, LABEL_MAX) if label else clip(clean(d.get("label") or "", True), LABEL_MAX) or k.noun
        tell = body.get("tell", "important")
        if tell not in [x[0] for x in k_tells(k)]:
            raise InputError("Choose Always tell me, If it’s important or Only in the list.")
        if self._duplicate(d["kind"], k.key(d["config"])):
            raise Conflict("You've already added this folder.")
        if len(self._rows()) >= MAX_CONNECTORS:
            raise Conflict(f"Callsheet reads at most {MAX_CONNECTORS} connectors. Remove one first.")
        now = self.clock()
        cid = "c" + secrets.token_hex(4)
        cfg = dict(d["config"], tell=tell)
        st = {"last_read": None, "last_new": 0, "next_at": now}
        with self.store.lock:
            self.store.db.execute("INSERT INTO connectors (id, kind, label, config, every_s, added, paused, state) "
                                  "VALUES (?,?,?,?,?,?,0,?)", (cid, d["kind"], label, json.dumps(cfg), k.every_s, now,
                                                               json.dumps(st)))
        with self.lock:
            self.drafts.pop(str(body.get("token")), None)
        self.log(f"Callsheet connectors: added {cid} ({d['kind']})")
        return self.read(cid)

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
                if is_local(self.kinds.get(r["kind"])):
                    raise InputError("every_s can't be changed here.")
                if val not in [x[0] for x in EVERY_CHOICES]:
                    raise InputError("Choose how often from the list.")
                sets.append("every_s=?")
                args.append(int(val))
                st = self._state(r)
                st["next_at"] = min(st.get("next_at") or self.clock(), self.clock() + int(val))
                sets.append("state=?")
                args.append(json.dumps(st))
            elif key == "tell" and r["kind"] in self.kinds:   # 0.11.1: every kind
                if val not in [x[0] for x in k_tells(self.kinds[r["kind"]])]:
                    raise InputError(TELL_WORDS)
                cfg["tell"] = val
                sets.append("config=?")
                args.append(json.dumps(cfg))
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
        k = self.kinds.get(r["kind"])
        if is_local(k):
            k.forget(self.store, cid)
        self.log(f"Callsheet connectors: removed {cid} ({n} items)")
        return self.view()

    def remove_host(self, host):
        """Every connector that reads `host` goes (the host leaves Places Callsheet contacts)."""
        host = str(host or "").lower()
        with self.store.lock:
            ids = [r["connector"] for r in self.store.db.execute(
                "SELECT connector FROM outbound_hosts WHERE host=? AND how<>'link'", (host,)).fetchall()]
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
                if not is_local(self.kinds.get(r["kind"])):
                    hosts = self._hosts_of(r["id"])
                    if not hosts or self.net.wait_s(hosts[0]) > 0:
                        continue
                self.read(r["id"])
                n += 1
            return n
        finally:
            self.read_lock.release()

    def _fail(self, cid, st, now, msg, alts=1):
        """A failed read: with another feed address left to try (YouTube's uploads playlist), try it after the host's
        gap without an error yet; otherwise show `msg` in Setup and back off 1 h, 3 h, 12 h."""
        fails = int(st.get("fails") or 0) + 1
        st["fails"] = fails
        if fails < alts:
            st.update(error=None, next_at=now + ALT_S)
        else:
            st.update(error=msg, next_at=now + BACKOFF_S[min(max(1, fails - alts + 1), len(BACKOFF_S)) - 1])
        self._set_state(cid, st)

    def read(self, cid):
        """Read one connector now (the poller, or the owner's Try again). Returns its public view."""
        r = self._row(cid)
        k = self.kinds.get(r["kind"])
        cfg, st = self._cfg(r), self._state(r)
        now = self.clock()
        if is_local(k):
            return self._read_local(k, r, st, now)
        hosts = self._hosts_of(cid)
        if k is None or not hosts:
            st.update(error="This connector can't be read. Remove it and add it again.", next_at=now + BACKOFF_S[-1])
            self._set_state(cid, st)
            return self.public(self._row(cid))
        urls = _feed_urls(k, cfg)
        fi = int(st.get("feed_i") or 0) % len(urls)
        url = urls[fi]
        missing = getattr(k, "missing", None)

        def other_address(msg):       # the feed isn't there: next time read the kind's other address (if any)
            if len(urls) > 1:
                st.update(feed_i=(fi + 1) % len(urls), etag=None, last_modified=None)
            self._fail(cid, st, now, missing or msg, len(urls))
            return self.public(self._row(cid))
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
            self.log(f"Callsheet connectors: {hosts[0]} error {e.why} {e.status or ''}".rstrip())
            if e.status in (403, 404, 410, 500, 502, 503) and missing:
                return other_address(str(e))
            self._fail(cid, st, now, str(e))
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
            self.log(f"Callsheet connectors: {hosts[0]} 200, {len(a.body)} bytes, not a feed")
            return other_address(f"{hosts[0]} sent something Callsheet couldn't read: {e}")
        items = k.items(self._items_cfg(r, cfg), feed, now, self.net.allow_test_names)
        new = self._store_items(r, items)
        st.update(error=None, fails=0, last_read=now, last_new=new, next_at=self._next(now, r["every_s"]),
                  etag=(a.headers.get("etag") or "")[:200] or None,
                  last_modified=(a.headers.get("last-modified") or "")[:100] or None,
                  link_offer=self._offers(r, st, items))
        self._set_state(cid, st)
        self.log(f"Callsheet connectors: {hosts[0]} 200, {len(a.body)} bytes, {len(items)} items, {new} new")
        return self.public(self._row(cid))

    def _read_local(self, k, r, st, now):
        """0.10.2a reports: one look at the folder (conn_reports.Reports.read); a failure shows in Setup in plain
        words and is tried again after BACKOFF_S."""
        try:
            new, ch = k.read(self, r)
        except Exception as e:  # noqa: BLE001 (type only: the folder is someone else's)
            self.log(f"Callsheet connectors: {r['id']} folder error {type(e).__name__}")
            new, ch = 0, {"error": "Callsheet couldn't read the folder just now."}
        if ch.get("error"):
            fails = int(st.get("fails") or 0) + 1
            st.update(error=ch["error"], fails=fails,
                      next_at=now + BACKOFF_S[min(fails, len(BACKOFF_S)) - 1])
        else:
            later = ch.get("later") or 0
            st.update(error=None, fails=0, last_read=now, last_new=new,
                      next_at=now + 60 if later else self._next(now, r["every_s"]))
        self._set_state(r["id"], st)
        return self.public(self._row(r["id"]))

    def _items_cfg(self, r, cfg):
        """The config a kind's items() reads: the connector id and every story-link host the owner allowed."""
        c = dict(cfg, id=r["id"])
        c["link_hosts"] = (list(cfg.get("link_hosts") or self._hosts_of(r["id"])) +
                           [x["host"] for x in self._link_rows(r["id"])])
        return c

    def _offers(self, r, st, items):
        """Hosts to ask the owner about (story links on another host of the feed's site), at most LINK_OFFERS: the
        open questions plus new ones, without the hosts already allowed or declined."""
        known = set(self._items_cfg(r, self._cfg(r))["link_hosts"]) | set(st.get("link_declined") or [])
        out = [h for h in (st.get("link_offer") or []) if isinstance(h, str) and h not in known]
        for it in items:
            h = it.get("link_offer")
            if h and h not in known and h not in out:
                out.append(h)
        return out[:LINK_OFFERS]

    def read_now(self, cid):
        """The owner's Try again: one read now, within the host's 15-minute gap (net.py)."""
        r = self._row(cid)
        if r["paused"]:
            raise Conflict("This connector is paused. Resume it first.")
        hosts = self._hosts_of(cid)
        wait = self.net.wait_s(hosts[0]) if hosts and not is_local(self.kinds.get(r["kind"])) else 0
        if wait > 0:
            mins = max(1, int((wait + 59) // 60))
            raise Conflict(f"Callsheet read {hosts[0]} a moment ago and reads each place at most once in 15 minutes. "
                           f"Try again in {mins} minute{'s' if mins != 1 else ''}.")
        if not self.read_lock.acquire(timeout=30):
            raise Conflict("Callsheet is reading your connectors right now. Try again in a minute.")
        try:
            return self.read(cid)
        finally:
            self.read_lock.release()

    def answer_link(self, cid, body):
        """The owner's answer to "Stories in this feed link to <host>. Show those links?": allow (recorded with the
        date, like the feed's host, and removable) or keep hidden (never asked again). Only an open question can be
        answered: Callsheet never allows a link host by itself."""
        if not isinstance(body, dict) or not isinstance(body.get("allow"), bool):
            raise InputError("Send {host, allow: true or false}.")
        r = self._row(cid)
        st = self._state(r)
        host = str(body.get("host") or "").lower()
        if host not in (st.get("link_offer") or []):
            raise Conflict("Callsheet isn't asking about that host now.")
        st["link_offer"] = [h for h in st["link_offer"] if h != host]
        if body["allow"]:
            st.update(etag=None, last_modified=None, next_at=min(st.get("next_at") or 0, self.clock()) or self.clock())
            with self.store.lock:
                self.store.db.execute("INSERT OR IGNORE INTO outbound_hosts (host, connector, added, how) "
                                      "VALUES (?,?,?,'link')", (host, cid, self.clock()))
            self.log(f"Callsheet connectors: {cid} links to {host} allowed by the owner")
        else:
            st["link_declined"] = (list(st.get("link_declined") or []) + [host])[-20:]
        self._set_state(cid, st)
        return self.public(self._row(cid))

    def remove_link(self, cid, host):
        """Links to `host` aren't shown any more (the items keep no link from the next read; shown links are checked
        again at display time)."""
        self._row(cid)
        with self.store.lock:
            n = self.store.db.execute("DELETE FROM outbound_hosts WHERE connector=? AND host=? AND how='link'",
                                      (cid, str(host or "").lower())).rowcount
        if not n:
            raise NotFound("That host isn't allowed for this connector.")
        r = self._row(cid)
        st = self._state(r)
        st["link_declined"] = (list(st.get("link_declined") or []) + [str(host).lower()])[-20:]
        st.update(etag=None, last_modified=None)
        self._set_state(cid, st)
        return self.public(r)

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
        # the flag found on the ORIGINAL text (entry_text): the index's own check reads the stored, defanged text,
        # where a link lure has lost its link. Re-applied on every read, so a later re-check can't drop it for long.
        keys = [(json.dumps(it["instr"][:inject.MAX_KEYS]), it["id"]) for it in items if it.get("instr")]
        if keys:
            with self.index.lock:
                for k, sid in keys:
                    self.index.db.execute("UPDATE items SET instr=? WHERE source=? AND account=? AND source_id=? "
                                          "AND instr=''", (k, r["kind"], r["id"], sid))
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
        rows = [r for r in self._rows() if not is_local(self.kinds.get(r["kind"]))]
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
        # 0.11.1: a connector set to Always tell me lists first; otherwise newest first (the rest is for the rundown)
        found.sort(key=lambda p: (self._cfg(p[0]).get("tell") != "always", -p[1]["ts"], p[1]["id"]))
        out = []
        for r, x in found[:limit]:
            flag = bool(x["instr"]) if x["instr_v"] == inject.VERSION else flagged(x["title"], x["body"])
            k = self.kinds.get(r["kind"])
            out.append({"id": x["id"], "connector": r["id"], "label": r["label"], "noun": k.noun if k else "",
                        "kind": x["kind"], "title": x["title"], "when": _iso(x["ts"]),
                        "url": "" if flag else _shown_link(x["url"], r, self._items_cfg(r, self._cfg(r))),
                        "flagged": flag, "hidden": bool(x["hidden"]),
                        "marks": ([["instructions", FLAG_MARK]] if flag else []) +
                                 ([["hidden", HIDDEN_MARK]] if x["hidden"] else []) +
                                 ([["short", "Short"]] if x["kind"] == "short" else [])})
        return {"items": out, "more": max(0, len(found) - limit), "hours": hours, "connectors": len(rows)}

    def reports_list(self):
        """0.10.2a "From your reports" (conn_reports): None when the owner has no Reports folders."""
        k = self.kinds.get("reports")
        if k is None:
            return None
        return k.brief_list(self, [r for r in self._rows() if r["kind"] == "reports"])

    def folders(self, body):
        """0.10.2a: the Reports folder picker (subfolder names under /share and /media; no file is opened). 0.12.1:
        where="drive": the owner's Google accounts, then the Drive folders of one (conn_reports_drive.folders)."""
        import conn_reports
        if isinstance(body, dict) and body.get("where") == "drive":
            import conn_reports_drive as D
            k = self.kinds.get("reports")
            if k is None:
                raise InputError("Reports aren't available.")
            try:
                return D.folders(self, k._access(self), body)
            except D.Trouble as e:
                raise InputError(str(e)) from None
        path = body.get("path") if isinstance(body, dict) else None
        if path is not None and not isinstance(path, str):
            raise InputError("Send {path}.")
        return conn_reports.folders(path)

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


def k_tells(k):
    """A kind's per-source choices: Always tell me, If it's important, Only in the list (reports since 0.10.2a, every
    kind since 0.11.1)."""
    return getattr(k, "tells", None) or TELLS


def _shown_link(url, r, cfg):
    """The stored link checked once more at display time: https and on the connector's own site."""
    host = net.host_of(url, True)
    if not host:
        return ""
    if r["kind"] == "youtube":
        return url if host == "www.youtube.com" and re.fullmatch(
            r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}", url) else ""
    return url if same_site(host, cfg.get("link_hosts") or []) else ""


def _feed_urls(k, cfg):
    """The feed addresses a kind may read for one connector, in order (YouTube: the channel feed, then the channel's
    uploads playlist feed); one for most kinds."""
    return k.feed_urls(cfg) if hasattr(k, "feed_urls") else [k.feed_url(cfg)]


class ConnectorsApi:
    def __init__(self, conns):
        self.c = conns

    def routes(self):
        return [("GET", r"connectors", lambda b: self.c.view()),
                ("POST", r"connectors/check", lambda b: self.c.check(b)),
                ("POST", r"connectors/reports/folders", lambda b: self.c.folders(b)),  # 0.10.2a
                ("POST", r"connectors", lambda b: self.c.add(b)),
                ("PATCH", rf"connectors/{CID}", lambda b, cid: self.c.patch(cid, b)),
                ("DELETE", rf"connectors/{CID}", lambda b, cid: self.c.remove(cid)),
                ("DELETE", r"connectors/hosts/([a-z0-9.-]{1,253})", lambda b, h: self.c.remove_host(h)),
                ("POST", rf"connectors/{CID}/read", lambda b, cid: self.c.read_now(cid)),
                ("POST", rf"connectors/{CID}/links", lambda b, cid: self.c.answer_link(cid, b)),
                ("DELETE", rf"connectors/{CID}/links/([a-z0-9.-]{{1,253}})", lambda b, cid, h: self.c.remove_link(cid, h)),
                ("POST", r"connectors/interests", lambda b: self.c.add_interest(b)),
                ("PATCH", rf"connectors/interests/{IID}", lambda b, iid: self.c.patch_interest(iid, b)),
                ("DELETE", rf"connectors/interests/{IID}", lambda b, iid: self.c.delete_interest(iid))]
