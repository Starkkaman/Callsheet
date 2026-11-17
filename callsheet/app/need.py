"""0.23.0 Clashes vs heads-ups (the owner, 2026-11-17; mockup clash-vs-headsup-v1; PLAN "clashes vs heads-ups").

One question decides an overlap, asked of each side: does it need the owner there? YES, MAYBE or NO.
Yes + Yes is a clash ("You're double-booked"); any Maybe is a Maybe clash; any No is a heads-up ("Happening" or
"FYI"); No + No isn't shown on Today. Heads-ups never alert, never take the loud key and are never red or orange.

Code first: the owner's per-calendar choice in Setup (Mine / Ask me / Heads-up / Hide; `calendar_use`, copied here from
Setup's account list so the ledger can read it), who organizes or made the entry, whether he is an attendee and
required or optional, his own reply, related messages (an open ask that stands for the entry) and his history with that
organizer. When the code can't tell whether he is expected to be busy or it is a general notice, the entry is a Maybe
and Today shows the "Does this need you?" card. His answer (a press, or his own words in Chat through prefs' gate)
is a `need` preference for that organizer (else that calendar); message text never answers it.

"Got it" (per item, with Undo) hides one heads-up from Today and the brief (`headsup_seen`); a changed one (a new
time or place) comes back. It stays on Agenda.
"""
import datetime as dt
import hashlib
import json
import re
import sqlite3

YES, MAYBE, NO = "yes", "maybe", "no"
USES = ("mine", "ask", "headsup", "hide")
USE_WORDS = {"mine": "Mine", "ask": "Ask me", "headsup": "Heads-up", "hide": "Hide"}
VERDICTS = ("clash", "maybe", "headsup", "none")
SMALL_MEETING = 8            # a required invite with at most this many people, from a named organizer: it needs him
HISTORY_MIN = 3              # his answers to one organizer's past invites that count as a habit
OWNER = "owner"              # ledger.OWNER (not imported: the ledger imports this module)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_use (
  account TEXT NOT NULL, calendar TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', use TEXT NOT NULL DEFAULT 'ask',
  primary_cal INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, calendar));
CREATE TABLE IF NOT EXISTS headsup_seen (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, sig TEXT NOT NULL DEFAULT '', at REAL NOT NULL, undone_at REAL);
CREATE INDEX IF NOT EXISTS headsup_seen_key ON headsup_seen (key)
"""

# a general notice on a calendar he only reads: deliveries, closures, school and community days, someone's trip
_NOTICE = re.compile(r"\b(deliver\w*|closed|closure|road ?works?|market|fair|festival|parade|holiday|half[- ]term|"
                     r"inset day|school|dismissal|reminder|fyi|notice|birthday|bday|anniversary|out of (?:the )?office|"
                     r"ooo|away|trip|vacation|bins?|recycling|service window|maintenance|outage)\b", re.I)
_SOMEONES = re.compile(r"^\s*[A-Z][\w-]+(?:'s|’s|:)\s")       # "Mira: book club", "Leo's dentist"
# "can you be home for it?": an ask to cover someone else's window is his, but only maybe (the rule sheet's sofa)
_BE_THERE = re.compile(r"\bbe (?:home|around|in|there|at home)\b|\bhome for (?:it|the)\b|\blet (?:them|him|her) in\b", re.I)
_LARGE = re.compile(r"\b(all[- ]hands|town ?hall|webinar|broadcast|all staff|company[- ]wide|readout|announcement)\b", re.I)


def ensure(db):
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt)


def verdict(a, b):
    """The rule's matrix: two sides' needs -> clash / maybe / headsup / none."""
    if a == NO and b == NO:
        return "none"
    if NO in (a, b):
        return "headsup"
    if MAYBE in (a, b):
        return "maybe"
    return "clash"


def conflict_verdict(a, b, confidence="sure"):
    """A clash whose time is a guess (the ledger's "likely") is a Maybe clash."""
    v = verdict(a, b)
    return "maybe" if v == "clash" and confidence != "sure" else v


def _get(row, key, default=""):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def attendees(e):
    raw = _get(e, "attendees", "[]")
    try:
        atts = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except ValueError:
        return []
    return [a for a in atts if isinstance(a, dict)]


def _who(addr, atts):
    a = next((x for x in atts if (x.get("address") or "").lower() == addr), None)
    name = (a or {}).get("name") or ""
    return name or (addr.split("@")[0].replace(".", " ").title() if addr and addr != "self" else "")


def t12(d):
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"


class Need:
    """One pass's reader: calendar choices, the owner's taught answers and the entries' needs (cached by id)."""

    def __init__(self, db):
        self.db = db
        self._uses = None
        self._rules = None
        self._ev = {}

    # --- what the owner set ------------------------------------------------------------------------------------------
    def uses(self):
        if self._uses is None:
            try:
                self._uses = {(r["account"], r["calendar"]): (r["use"], r["name"], r["primary_cal"])
                              for r in self.db.execute("SELECT * FROM calendar_use")}
            except sqlite3.OperationalError:
                self._uses = {}
        return self._uses

    def use_of(self, e):
        """The owner's choice for this entry's calendar; a calendar Setup doesn't list (an old row, a replay) is his."""
        got = self.uses().get((_get(e, "account"), _get(e, "calendar")))
        return got[0] if got and got[0] in USES else "mine"

    def cal_name(self, e):
        got = self.uses().get((_get(e, "account"), _get(e, "calendar")))
        return (got[1] if got else "") or str(_get(e, "calendar"))

    def rules(self):
        """{("organizer"|"calendar", key): (need, pref id)}: the owner's answers, newest last (prefs kind `need`)."""
        if self._rules is None:
            self._rules = {}
            try:
                rows = self.db.execute("SELECT id, fields FROM preferences WHERE kind='need' AND undone_at IS NULL AND "
                                       "deleted_at IS NULL ORDER BY id").fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                try:
                    f = json.loads(r["fields"])
                except ValueError:
                    continue
                if f.get("need") in (YES, NO) and f.get("scope") in ("organizer", "calendar") and f.get("key"):
                    self._rules[(f["scope"], str(f["key"]).lower())] = (f["need"], r["id"])
        return self._rules

    def taught(self, e):
        """The owner's answer that covers this entry: its organizer (by address, or by the name he used in Chat), else
        its calendar (by id, or by its name)."""
        org = str(_get(e, "organizer")).lower()
        rules = self.rules()
        if org and org != "self":
            got = rules.get(("organizer", org))
            if got:
                return got[0], "organizer"
            names = {_who(org, attendees(e)).lower(), org.split("@")[0]}
            for (scope, key), (val, _) in reversed(list(rules.items())):
                if scope == "organizer" and "@" not in key and (key in names or key.split(" ")[0] in
                                                               {x.split(" ")[0] for x in names if x}):
                    return val, "organizer"
        cal = f"{_get(e, 'account')}/{_get(e, 'calendar')}".lower()
        got = rules.get(("calendar", cal))
        if got:
            return got[0], "calendar"
        name = self.cal_name(e).lower()
        for (scope, key), (val, _) in reversed(list(rules.items())):
            bare = re.sub(r"\b(?:the|my|calendars?)\b", " ", key).strip()
            if scope == "calendar" and "/" not in key and bare and (key == name or bare in name):
                return val, "calendar"
        return None, None

    # --- one calendar entry ----------------------------------------------------------------------------------------
    def event(self, e):
        """{need, why, word (Happening / FYI), ask (show "Does this need you?"), hide, use} for a calendar entry."""
        eid = _get(e, "id", None)
        if eid is not None and eid in self._ev:
            return self._ev[eid]
        out = self._event(e)
        if eid is not None:
            self._ev[eid] = out
        return out

    def _event(self, e):
        use = self.use_of(e)

        def res(need, why, word="Happening", ask=False, hide=False):
            return {"need": need, "why": why, "word": word, "ask": ask, "hide": hide, "use": use}
        if use == "hide":
            return res(NO, "on a calendar you hid", hide=True)
        if _get(e, "status") == "cancelled":
            return res(NO, "cancelled", hide=True)
        atts = attendees(e)
        me = next((a for a in atts if a.get("self")), None)
        resp = str((me or {}).get("response") or "")
        org = str(_get(e, "organizer")).lower()
        mine_made = org == "self" or str(_get(e, "creator")).lower() == "self" or bool(me and me.get("organizer"))
        if resp == "declined":
            return res(NO, "you said no", hide=True)
        if resp == "accepted":
            return res(YES, "you said yes")
        if resp == "tentative" or _get(e, "status") == "tentative":
            return res(MAYBE, "you said maybe" if resp == "tentative" else "it’s pencilled in")
        if _get(e, "transparency") == "transparent":
            return res(NO, "marked free", "FYI")
        if mine_made and (me is None or me.get("organizer") or org == "self"):
            return res(YES, "you made it")
        taught, scope = self.taught(e)
        if me is not None:                  # an invite he hasn't answered: decided by context
            if me.get("optional"):
                return res(NO, "optional for you", "FYI")
            if taught:
                return res(taught, "you said these need you" if taught == YES else "you said these are only FYI",
                           "FYI" if taught == NO else "Happening")
            if self._waiting(e):
                return res(YES, "you’re required and they’re waiting for your answer")
            hist = self._history(org, _get(e, "id", None))
            if hist == YES:
                return res(YES, "you’re required; you usually go to " + (_who(org, atts) or "theirs"))
            if hist == NO:
                return res(NO, "you usually say no to " + (_who(org, atts) or "these"), "FYI")
            others = [a for a in atts if not a.get("self")]
            if org != "self" and 0 < len(others) <= SMALL_MEETING and not _LARGE.search(str(_get(e, "title"))):
                return res(YES, "you’re required; not answered yet")
            return res(MAYBE, "not answered yet", ask=True)
        if use == "mine":
            return res(YES, "on your calendar")
        if use == "headsup":
            return res(NO, "on " + self.cal_name(e))
        if taught:                          # "Ask me": the owner's answer for this organizer or calendar
            return res(taught, ("you said these need you" if taught == YES else "you said these are only FYI"))
        title = str(_get(e, "title"))
        if _get(e, "all_day") or _NOTICE.search(title) or _SOMEONES.match(title):
            return res(NO, "on " + self.cal_name(e))
        if mine_made:
            return res(YES, "you made it")
        return res(MAYBE, "on " + self.cal_name(e), ask=True)

    def _waiting(self, e):
        """Related messages: an open ask (not the invite's own item) that stands for this entry."""
        eid = _get(e, "id", None)
        if eid is None:
            return False
        try:
            return self.db.execute("SELECT 1 FROM ledger_items WHERE cal_ref=? AND merged_into IS NULL AND type='ask' AND "
                                   "subtype != 'invite' AND status='open' LIMIT 1", (eid,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def _history(self, org, eid):
        """His answers to this organizer's earlier invites: YES / NO when one answer is a clear habit, else None."""
        if not org or org == "self":
            return None
        try:
            rows = self.db.execute("SELECT attendees FROM events WHERE organizer=? AND id != ? ORDER BY start_ts DESC "
                                   "LIMIT 40", (org, eid if eid is not None else -1)).fetchall()
        except sqlite3.OperationalError:
            return None
        said = [str(a.get("response") or "") for r in rows for a in attendees(r) if a.get("self")]
        yes, no = said.count("accepted"), said.count("declined")
        if yes + no < HISTORY_MIN:
            return None
        return YES if yes >= 2 * max(no, 1) else NO if no >= 2 * max(yes, 1) else None

    # --- one ledger item -----------------------------------------------------------------------------------------------
    def item(self, it):
        """{need, why, word} for a ledger item side. An invite is its calendar entry; the owner's own word is his;
        someone else's plan is a heads-up; a guessed time or a maybe is a Maybe."""
        def res(need, why, word="Happening"):
            return {"need": need, "why": why, "word": word, "ask": False, "hide": False}
        if it.get("subtype") == "invite" and it.get("cal_ref") is not None:
            e = self.db.execute("SELECT * FROM events WHERE id=?", (it["cal_ref"],)).fetchone()
            if e is not None:
                return self.event(e)
        if it.get("status") == "tentative":
            return res(MAYBE, "you said maybe")
        who = it.get("who") or ""
        # an event someone told him about (a job day, a dinner) is his unless a calendar says otherwise; someone
        # else's own plans come from the calendars he marks Heads-up (0.23.0 oracle check: 9 synthetic clashes)
        if it.get("confidence") != "sure" or it.get("approx"):
            return res(MAYBE, "the time is a guess")
        if it.get("type") == "ask" and who != OWNER:
            if _BE_THERE.search(f"{it.get('what') or ''} {it.get('purpose') or ''}"):
                return res(MAYBE, f"{it.get('who_name') or who} asked if you can be there")
            return res(YES, f"{it.get('who_name') or who} asked for your time")
        return res(YES, "you said yes" if it.get("type") == "commitment" else "yours")

    def side(self, kind, row):
        if kind == "event":
            return self.event(row)
        return self.item(row if isinstance(row, dict) else dict(row))


class ReadOnly:
    """A db-like reader over MessageStore._read (execute -> rows with fetchall / fetchone), for the page's views."""

    def __init__(self, ms):
        self.ms = ms

    def execute(self, sql, args=()):
        return _Rows(self.ms._read(sql, args))  # noqa: SLF001


class _Rows(list):
    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


# --- Setup's calendar choices, copied where the ledger reads them ----------------------------------------------------
def default_use(cal):
    return "mine" if cal.get("primary") else "ask"


def sync_calendars(db, accounts):
    """accounts: store.Store.accounts() (each calendar's `use`, else its default). Returns True when a choice changed."""
    ensure(db)
    old = {(r["account"], r["calendar"]): r["use"] for r in db.execute("SELECT account, calendar, use FROM calendar_use")}
    new = {}
    for a in accounts or []:
        for c in a.get("calendars") or []:
            use = c.get("use") if c.get("use") in USES else default_use(c)
            new[(str(a["id"]), str(c["id"]))] = (use, str(c.get("summary") or c["id"])[:120], 1 if c.get("primary") else 0)
    for (acc, cal), (use, name, prim) in new.items():
        db.execute("INSERT INTO calendar_use (account, calendar, name, use, primary_cal) VALUES (?,?,?,?,?) ON CONFLICT "
                   "(account, calendar) DO UPDATE SET name=excluded.name, use=excluded.use, primary_cal=excluded.primary_cal",
                   (acc, cal, name, use, prim))
    for key in set(old) - set(new):
        db.execute("DELETE FROM calendar_use WHERE account=? AND calendar=?", key)
    return any(old.get(k) != v[0] for k, v in new.items()) or bool(set(old) - set(new))


# --- the heads-up list, the ask cards and "Got it" ------------------------------------------------------------------
def sig_of(kind, row):
    """What makes a heads-up "the same one": its time, place and title (a change brings it back after Got it)."""
    parts = [_get(row, "start_ts"), _get(row, "end_ts"), _get(row, "location") if kind == "event" else "",
             _get(row, "title") if kind == "event" else _get(row, "what")]
    return hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:16]


def seen(db):
    """{key: sig} of the heads-ups the owner pressed Got it on (not undone)."""
    try:
        return {r["key"]: r["sig"] for r in db.execute(
            "SELECT key, sig FROM headsup_seen WHERE undone_at IS NULL ORDER BY id")}
    except sqlite3.OperationalError:
        return {}


HEADSUP_ITEM_SUBTYPES = ("delivery",)    # a delivery or service window: happening, never his time


def headsups(db, since, until, tz, now):
    """Heads-ups touching the owner's days [since, until): calendar entries that don't need him (not hidden) and ledger
    items that are someone else's plans or a delivery window, in time order. Each: {key, kind, id, start_ts, end_ts,
    all_day, title, sub, tag, word, sig, day, when}. Ask cards: entries the code can't place (`asks`)."""
    n = Need(db)
    out, asks, keys = [], [], set()
    for e in db.execute("SELECT * FROM events WHERE start_ts < ? AND coalesce(end_ts, start_ts) > ? AND status != 'cancelled' "
                        "ORDER BY start_ts, id", (until, since)).fetchall():
        r = n.event(e)
        k = (e["ical_uid"] or e["event_id"], int(e["start_ts"] or 0))
        if r["hide"] or k in keys:
            continue
        if r["ask"]:
            keys.add(k)
            asks.append(_entry("event", e, r, n, tz, now))
            continue
        if r["need"] != NO:
            continue
        keys.add(k)
        out.append(_entry("event", e, r, n, tz, now))
    try:
        items = db.execute("SELECT * FROM ledger_items WHERE merged_into IS NULL AND status NOT IN ('declined','done','cancelled','postponed') AND type != 'cleanup' AND "
                           "start_ts IS NOT NULL AND start_ts < ? AND coalesce(end_ts, start_ts) >= ? AND cal_ref IS NULL",
                           (until, since)).fetchall()
    except sqlite3.OperationalError:
        items = []
    for row in items:
        it = dict(row)
        if it.get("subtype") in HEADSUP_ITEM_SUBTYPES:
            r = {"need": NO, "why": "a delivery window", "word": "Happening"}
        else:
            r = n.item(it)
            if r["need"] != NO:
                continue
        out.append(_entry("item", it, r, n, tz, now))
    out.sort(key=lambda x: (x["start_ts"] or 0, x["title"]))
    return out, asks


def _entry(kind, row, r, n, tz, now):
    s, e = _get(row, "start_ts", None), _get(row, "end_ts", None)
    all_day = bool(_get(row, "all_day", 0))
    d0 = dt.datetime.fromtimestamp(s, tz) if s is not None else None
    d1 = dt.datetime.fromtimestamp(e, tz) if e else None
    if all_day:
        when = "All day"
    elif d0 and d1 and d1 > d0:
        when = f"{t12(d0)} – {t12(d1)}" if d1.date() == d0.date() else f"{t12(d0)} – {d1:%a} {t12(d1)}"
    else:
        when = t12(d0) if d0 else ""
    if kind == "event":
        title = (row["title"] or "").strip() or "(No title)"
        tag = "FYI" if r["word"] == "FYI" else (n.cal_name(row) or "Calendar")[:24]
        rid = row["id"]
    else:
        title = (row.get("what") or "").strip() or "(No title)"
        tag = "Delivery" if row.get("subtype") == "delivery" else (row.get("who_name") or "Message")[:24]
        rid = row["id"]
    return {"key": f"{'e' if kind == 'event' else 'i'}:{rid}", "kind": kind, "id": rid, "start_ts": s, "end_ts": e,
            "all_day": all_day, "title": title, "sub": r["why"][:1].upper() + r["why"][1:], "tag": tag,
            "word": r["word"], "sig": sig_of(kind, row), "day": d0.date().isoformat() if d0 else "", "when": when}


# --- the day drawing (Today's Day strip, the phone's vertical day, the Agenda day) ------------------------------------
def lanes(db, day0, day1, tz, blocks_item=None):
    """One day as lanes: all_day bands, `you` blocks (need yes or maybe), `around` bands (heads-ups) and the places
    where two of his blocks overlap (`clashes`: {s, e, verdict, a, b}). blocks_item(it): conflicts.blocks."""
    n = Need(db)
    allday, you, around, keys = [], [], [], set()
    for e in db.execute("SELECT * FROM events WHERE start_ts < ? AND coalesce(end_ts, start_ts) > ? AND status != 'cancelled' "
                        "ORDER BY start_ts, id", (day1, day0)).fetchall():
        r = n.event(e)
        k = (e["ical_uid"] or e["event_id"], int(e["start_ts"] or 0))
        if r["hide"] or k in keys:
            continue
        keys.add(k)
        row = {"key": f"e:{e['id']}", "title": (e["title"] or "").strip() or "(No title)", "need": r["need"],
               "why": r["why"], "word": r["word"], "ask": r["ask"]}
        if e["all_day"]:
            allday.append(dict(row, mine=r["need"] != NO))
            continue
        s, en = max(e["start_ts"], day0), min(e["end_ts"] or e["start_ts"] + 1800, day1)
        row.update(s=s, e=max(en, s + 900), time=t12(dt.datetime.fromtimestamp(e["start_ts"], tz)),
                   end=t12(dt.datetime.fromtimestamp(e["end_ts"] or e["start_ts"], tz)))
        (around if r["need"] == NO or _get(e, "transparency") == "transparent" else you).append(row)
    try:
        items = db.execute("SELECT * FROM ledger_items WHERE merged_into IS NULL AND status NOT IN ('declined','done','cancelled','postponed') AND type != 'cleanup' AND "
                           "start_ts IS NOT NULL AND start_ts < ? AND coalesce(end_ts, start_ts) >= ? AND all_day=0 AND "
                           "cal_ref IS NULL", (day1, day0)).fetchall()
    except sqlite3.OperationalError:
        items = []
    for row0 in items:
        it = dict(row0)
        delivery = it.get("subtype") in HEADSUP_ITEM_SUBTYPES
        if not delivery and blocks_item is not None and not blocks_item(it):
            continue
        r = {"need": NO, "why": "a delivery window", "word": "Happening"} if delivery else n.item(it)
        s = max(it["start_ts"], day0)
        en = min(it["end_ts"] or it["start_ts"] + 3600, day1)
        row = {"key": f"i:{it['id']}", "item": it["id"], "title": (it.get("what") or "").strip() or "(No title)",
               "need": r["need"], "why": r["why"], "word": r["word"], "ask": False, "s": s, "e": max(en, s + 900),
               "time": t12(dt.datetime.fromtimestamp(it["start_ts"], tz)),
               "end": t12(dt.datetime.fromtimestamp(it["end_ts"] or it["start_ts"] + 3600, tz))}
        (around if r["need"] == NO else you).append(row)
    you.sort(key=lambda x: (x["s"], x["e"]))
    around.sort(key=lambda x: (x["s"], x["e"]))
    clashes = []
    for i, a in enumerate(you):
        for b in you[i + 1:]:
            if b["s"] < a["e"] and a["s"] < b["e"]:
                clashes.append({"s": max(a["s"], b["s"]), "e": min(a["e"], b["e"]), "a": a["key"], "b": b["key"],
                                "verdict": verdict(a["need"], b["need"]),
                                "time": t12(dt.datetime.fromtimestamp(max(a["s"], b["s"]), tz)),
                                "end": t12(dt.datetime.fromtimestamp(min(a["e"], b["e"]), tz))})
    return {"all_day": allday, "you": you, "around": around, "clashes": clashes}


# --- teaching from an answer ----------------------------------------------------------------------------------------
def teach_fields(db, event_id, answer):
    """The `need` preference an answer to "Does this need you?" makes: that organizer when someone else organizes it,
    else that calendar. answer: "yes" (Yes, needs me) or "no" (No, just FYI)."""
    if answer not in (YES, NO):
        raise ValueError("Answer yes or no.")
    e = db.execute("SELECT * FROM events WHERE id=?", (int(event_id),)).fetchone()
    if e is None:
        raise LookupError("There's no such calendar entry.")
    n = Need(db)
    org = str(_get(e, "organizer")).lower()
    if org and org != "self" and "@" in org:
        name = _who(org, attendees(e)) or org
        return {"scope": "organizer", "key": org, "name": name[:80], "need": answer}
    return {"scope": "calendar", "key": f"{e['account']}/{e['calendar']}".lower(), "name": n.cal_name(e)[:80],
            "need": answer}
