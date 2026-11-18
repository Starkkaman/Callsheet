"""0.24.0 Filtering fixes from the owner's secret-shopper audit (2026-11-17, pain points 1-5 and 9).

- Sender reputation (`Reputation`): mail stored before Gmail labels were kept has no labels and no bulk flag, so the
  promo rules (tidy.promo_of) had only the address to go on. A sender whose mail is never answered and that writes
  from a no-reply, role or marketing address, or whose mail mostly carries unsubscribe text (or the bulk flag or the
  Promotions tab), counts as a bulk sender. The owner's own "Never from this sender" / "Treat as promotion" presses
  (`sender_rules`) count first. Never people he writes to, threads he wrote in, security alerts, codes or invites
  (tidy.promo_of keeps those checks).
- Gmail Trash: an item whose every source message is in Trash or deleted closes (never a security alert or invite).
- Clashes Today never shows: a side that is empty, a cancelled entry, an entry on a calendar he only reads (a shared
  calendar where he isn't a personal attendee), an event against its own invite or mail (the same iCalUID, or the same
  title at the same time), and two invites of one series or service.
- One row per conversation: Needs you keeps one row per thread and person (the others are counted on it), and one
  alert per sender, subject and day.
- Direction: what others owe him waits on others (not Needs you); his own promises stay his, timed or not.

Data is text: message text is only matched here, never shown or sent anywhere.
"""
import datetime as dt
import json
import re
import sqlite3
import threading
import time

REP_MIN = 3                  # messages from one sender before its history counts
REP_ANSWERED = 0.1           # at most this share of its threads answered by the owner
REP_UNSUB = 0.3              # share of its mail with unsubscribe text that makes it a list sender
REP_TTL = 600                # seconds a computed reputation is reused
RULE_KINDS = {"never": "Never from this sender", "promo": "Treat as promotion"}
MERGE_KINDS = ("ask", "commitment")

_REP_CACHE = {}
_REP_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sender_rules (
  id INTEGER PRIMARY KEY, address TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL, item INTEGER,
  run INTEGER, label TEXT NOT NULL DEFAULT '', undone_at REAL);
CREATE INDEX IF NOT EXISTS sender_rules_address ON sender_rules (address)
"""


def ensure(db):
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt)


def _read(src, sql, args=()):
    """Rows from a MessageStore (its _read) or a sqlite connection."""
    if hasattr(src, "_read"):
        return [dict(r) for r in src._read(sql, args)]  # noqa: SLF001
    return [dict(r) for r in src.execute(sql, args).fetchall()]


# --- sender reputation -------------------------------------------------------------------------------------------------
class Reputation:
    """Per sender address: {n, unsub, bulk, promo, threads, answered}; rules: {address: kind} (the owner's presses)."""

    def __init__(self, stats=None, rules=None):
        self.stats = stats or {}
        self.rules = rules or {}

    @classmethod
    def load(cls, src, now=None, fresh=False):
        key = id(src)
        now = time.time() if now is None else now
        with _REP_LOCK:
            got = _REP_CACHE.get(key)
            if got and not fresh and now - got[0] < REP_TTL:
                return got[1]
        try:
            rows = _read(src, (
                "SELECT lower(m.sender) AS a, count(*) AS n, "
                "sum(CASE WHEN m.text LIKE '%unsubscrib%' OR m.text LIKE '%opt out%' OR m.text LIKE '%opt-out%' OR "
                "m.text LIKE '%email preferences%' OR m.text LIKE '%manage your subscription%' OR "
                "m.text LIKE '%receiving this email because%' THEN 1 ELSE 0 END) AS u, "
                "sum(CASE WHEN m.bulk THEN 1 ELSE 0 END) AS b, "
                "sum(CASE WHEN m.labels LIKE '%CATEGORY_PROMOTIONS%' THEN 1 ELSE 0 END) AS p, "
                "count(DISTINCT m.thread) AS t, count(DISTINCT o.thread) AS r "
                "FROM messages m LEFT JOIN (SELECT DISTINCT account, thread FROM messages WHERE source='gmail' AND "
                "direction='out') o ON o.account=m.account AND o.thread=m.thread "
                "WHERE m.source='gmail' AND m.direction='in' GROUP BY lower(m.sender)"))
        except sqlite3.OperationalError:
            rows = []
        stats = {}
        for r in rows:
            a = _addr(r["a"])
            if a:
                s = stats.setdefault(a, {"n": 0, "unsub": 0, "bulk": 0, "promo": 0, "threads": 0, "answered": 0})
                for k, col in (("n", "n"), ("unsub", "u"), ("bulk", "b"), ("promo", "p"), ("threads", "t"),
                               ("answered", "r")):
                    s[k] += int(r[col] or 0)
        rep = cls(stats, rules_of(src))
        with _REP_LOCK:
            _REP_CACHE[key] = (now, rep)
        return rep

    @classmethod
    def peek(cls, src, now=None):
        """0.24.0 (B2): the loaded reputation if Today or Tidy loaded it lately, else None (no 2 s table scan; Projects
        then sorts by the message's own signals)."""
        now = time.time() if now is None else now
        with _REP_LOCK:
            got = _REP_CACHE.get(id(src))
        return got[1] if got and now - got[0] < REP_TTL else None

    def rule(self, addr):
        return self.rules.get(_addr(addr), "")

    def bulk(self, addr):
        """Why this sender is a bulk sender ("" when it isn't): the owner's rule, else its history in the store."""
        import people                                   # late: people imports a lot
        import tidy as TY
        a = _addr(addr)
        if not a:
            return ""
        if a in self.rules:
            return "you marked this sender"
        s = self.stats.get(a)
        if not s or s["n"] < REP_MIN:
            return ""
        if s["threads"] and s["answered"] / s["threads"] > REP_ANSWERED:
            return ""                                    # he answers them: a person or a service he deals with
        local, dom = a.split("@", 1)[0], TY.domain_of(a)
        machine = bool(people.AUTOMATED_SENDER.search(local) or people.ROLE.match(local) or
                       TY.LIST_LOCAL.match(local) or TY.ESP_DOMAINS.search(dom) or TY.MARKETING_SUB.match(dom))
        listy = s["unsub"] / s["n"] >= REP_UNSUB or s["bulk"] / s["n"] >= REP_UNSUB or s["promo"] > 0
        if listy:
            return "a list sender you never answer"
        if machine and s["unsub"]:
            return "an automated sender you never answer"
        return ""


def _addr(a):
    a = str(a or "").strip().lower()
    m = re.search(r"<([^>]+)>", a)
    return (m.group(1) if m else a).strip()


def forget(src=None):
    """Drop the cached reputation (after the owner's press, or in tests)."""
    with _REP_LOCK:
        if src is None:
            _REP_CACHE.clear()
        else:
            _REP_CACHE.pop(id(src), None)


def rules_of(src):
    try:
        rows = _read(src, "SELECT address, kind FROM sender_rules WHERE undone_at IS NULL ORDER BY id")
    except sqlite3.OperationalError:
        return {}
    return {_addr(r["address"]): r["kind"] for r in rows if r["kind"] in RULE_KINDS}


# --- Gmail Trash ---------------------------------------------------------------------------------------------------------
def trashed(row):
    if row.get("deleted_at") is not None:
        return True
    try:
        labels = json.loads(row.get("labels") or "[]")
    except (TypeError, ValueError):
        labels = []
    return "TRASH" in (labels or [])


def close_trashed(db, now, items=None):
    """Open items whose every source message is a Gmail message in Trash or deleted close ("its message is in Trash").
    Never a security alert or an invite (only the owner's Done closes those). Returns the closed ids."""
    import ledger as L
    where = "" if items is None else f" AND s.item IN ({','.join(str(int(i)) for i in items) or '0'})"
    rows = db.execute("SELECT s.item, m.source, m.deleted_at, m.labels FROM ledger_sources s JOIN messages m ON "
                      "m.id=s.message JOIN ledger_items i ON i.id=s.item WHERE i.merged_into IS NULL AND i.status NOT IN "
                      "('declined','done','cancelled') AND coalesce(i.subtype,'') NOT IN ('security','invite')"
                      + where).fetchall()
    by = {}
    for r in rows:
        r = dict(r)
        by.setdefault(r["item"], []).append(r["source"] == "gmail" and trashed(r))
    out = []
    for item, flags in by.items():
        if flags and all(flags):
            L.set_status(db, item, "done", now, note="its message is in Trash in Gmail", kind="source")
            out.append(item)
    return out


# --- clashes Today shows -----------------------------------------------------------------------------------------------
_SERIES = re.compile(r"\s*[-–—:|(\[].*$")
_WORDS = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset("the and for with you your our from this that are can will about please re fwd fw has have was "
                  "were just what when need want get got into off out not but all any its his her him she they them "
                  "their there here then than also some more most very".split())


def norm_title(t):
    return re.sub(r"\s+", " ", re.sub(r"^(?:re|fwd?|invitation|updated invitation|invite)\s*:\s*", "",
                                      str(t or "").strip().lower(), flags=re.I)).strip()


def _uid_root(u):
    return re.sub(r"_R\d{8}T\d{6}.*$|_\d{8}(?:T\d{6}Z?)?$", "", str(u or ""))


def _words(t):
    return {w for w in _WORDS.findall(str(t or "").lower()) if w not in _STOP}


def similar(a, b):
    """Two short "what" lines about one thing: the same words mostly, or one inside the other."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = _words(na), _words(nb)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def side_facts(kind, row, events=None):
    """{title, start, uid, organizer, invite, cancelled} for one clash side (an item or a calendar entry)."""
    if row is None:
        return None
    if kind == "event":
        atts = []
        try:
            atts = json.loads(row.get("attendees") or "[]") if isinstance(row.get("attendees"), str) else []
        except ValueError:
            atts = []
        return {"title": row.get("title") or "", "start": row.get("start_ts"), "uid": row.get("ical_uid") or "",
                "organizer": str(row.get("organizer") or "").lower(), "invite": bool(atts),
                "cancelled": row.get("status") == "cancelled", "event": row.get("id")}
    ev = (events or {}).get(row.get("cal_ref")) if row.get("cal_ref") is not None else None
    return {"title": row.get("what") or "", "start": row.get("start_ts"), "uid": (ev or {}).get("ical_uid") or "",
            "organizer": str((ev or {}).get("organizer") or "").lower(), "invite": row.get("subtype") == "invite",
            "cancelled": row.get("status") in ("cancelled", "declined", "done"), "event": row.get("cal_ref")}


def clash_drop(a, b):
    """Why a clash between two sides (side_facts) isn't one ("" when it is)."""
    if a is None or b is None:
        return ""
    if not norm_title(a["title"]) or not norm_title(b["title"]):
        return "an empty side"
    if a["cancelled"] or b["cancelled"]:
        return "cancelled"
    if a["event"] is not None and a["event"] == b["event"]:
        return "the same event"
    if a["uid"] and a["uid"] == b["uid"]:
        return "the same event"
    if a["start"] is not None and a["start"] == b["start"] and similar(a["title"], b["title"]):
        return "the same event"
    if a["invite"] and b["invite"]:
        if a["uid"] and _uid_root(a["uid"]) == _uid_root(b["uid"]):
            return "one series"
        ta, tb = _SERIES.sub("", norm_title(a["title"])), _SERIES.sub("", norm_title(b["title"]))
        if (a["organizer"] and a["organizer"] == b["organizer"] and a["organizer"] != "self") or \
                (ta and ta == tb) or similar(ta, tb):
            return "one series or service"
    return ""


# --- one row per conversation, one alert -------------------------------------------------------------------------------
def alert_key(who, what, ts, tz):
    """Duplicate alerts (a delivery, a payment alert, a service alert sent twice): one per sender, subject and day."""
    day = dt.datetime.fromtimestamp(ts, tz).date().isoformat() if ts else ""
    w = re.sub(r"\d+", "#", norm_title(what))
    return (str(who or "").strip().lower(), w, day)


def thread_merges(db, now):
    """The thread-level merge: open asks and commitments from one thread, one other person, a similar what. The newer
    ones merge into the oldest (merged_into, a history line on both). Never security alerts or invites. Returns
    [(kept, merged)]."""
    import ledger as L
    rows = db.execute("SELECT i.*, m.source AS msrc, m.account AS macc, m.thread AS mthr FROM ledger_items i "
                      "JOIN ledger_sources s ON s.item=i.id AND s.role='created' JOIN messages m ON m.id=s.message "
                      "WHERE i.merged_into IS NULL AND i.status IN ('open','claimed') AND i.type IN ('ask','commitment') "
                      "AND coalesce(i.subtype,'') NOT IN ('security','invite') ORDER BY i.id").fetchall()
    groups, seen = {}, set()
    for r in rows:
        r = dict(r)
        if r["id"] in seen or not r.get("mthr"):
            continue
        seen.add(r["id"])
        groups.setdefault((r["msrc"], r["macc"], r["mthr"], r["type"], str(r.get("who") or "").lower()), []).append(r)
    out = []
    for grp in groups.values():
        kept = []
        for r in grp:
            lead = next((k for k in kept if similar(k["what"], r["what"])), None)
            if lead is None:
                kept.append(r)
                continue
            db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (lead["id"], now, r["id"]))
            L.history(db, r["id"], "merged", now, note=f"one conversation: merged into #{lead['id']}"[:300])
            L.history(db, lead["id"], "merged_in", now, note=f"one conversation: #{r['id']} merged in"[:300])
            out.append((lead["id"], r["id"]))
    return out
