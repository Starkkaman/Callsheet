"""0.24.3 the voice assistant's three actions (owner approved 2026-11-18: "Yes, those three"; docs/voice-api.md).

The phone assistant may dismiss, snooze or mark done one item Today shows, and undo that within 24 hours:
- POST /v1/items/{l…|c…}/dismiss, /snooze {until}, /done -> {ok, undo_token, item}; POST /v1/undo/{token}.
- Only with the token scope `actions:write`, off by default and granted per device in Setup › Connections › Voice
  assistant ("Can dismiss, snooze and mark done").
- Each one runs through actions.py, the registry Today's buttons and Chat use (`item.done` like Done, `item.snooze`
  like Remind me's set-aside, `clash.dismiss` like Dismiss; dismissing an item is its Done, as in Chat), so it is
  journaled in action_log with the device's name and shows in Setup › Your rules › Recent changes with Undo.
- In Callsheet only: it never reaches a calendar, mail or message write, and nothing is ever sent. The ids come only
  from the request path (never from message text); the body holds at most `until`.
- Rate-limited (ACT_N a minute per device, on top of the API's 60), and the undo tokens are kept only as hashes.
"""
import datetime as dt
import hashlib
import json
import re
import secrets

import actions as actions_mod
import apitokens

VERBS = ("dismiss", "snooze", "done")
UNDO_S = 24 * 3600
ACT_N, ACT_S = 10, 60          # actions a minute per device
BODY_MAX = 1024
UNDO_PREFIX = "u"
MORNING, AFTERNOON, EVENING, NIGHT = 9, 13, 18, 20
_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_undo (
  hash TEXT PRIMARY KEY, log INTEGER NOT NULL, token TEXT NOT NULL, created REAL NOT NULL, used REAL)"""
_WORDN = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45}
_WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PARTS = {"morning": MORNING, "afternoon": AFTERNOON, "evening": EVENING, "night": NIGHT, "tonight": EVENING}


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _bad(msg):
    return Refused(400, msg)


def _clock_time(s):
    """"9", "9am", "9:30 pm", "noon", "15:00" -> (hour, minute) or None."""
    s = s.strip().replace(".", "")
    if s in ("noon", "midday"):
        return 12, 0
    if s == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if h > 23 or mi > 59 or (ap and not 1 <= h <= 12):
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 7:
        h += 12                     # "at 3" in a snooze: the afternoon
    return h, mi


def parse_until(v, now, tz):
    """`until`: an ISO date or date-time (no offset: the owner's time zone; a date: 9 in the morning), or a phrase
    ("tomorrow morning", "tonight", "monday", "next week", "in 2 hours", "tomorrow at 3 pm"). -> seconds."""
    if isinstance(v, bool) or not isinstance(v, str) or not v.strip() or len(v) > 80:
        raise _bad("until should be a date and time, like 2026-11-19T09:00, or words like “tomorrow morning”.")
    s = re.sub(r"\s+", " ", v.strip().lower())
    t0 = dt.datetime.fromtimestamp(now, tz)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return dt.datetime.combine(dt.date.fromisoformat(s), dt.time(MORNING), tz).timestamp()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[t ][\d:.]+(z|[+-]\d{2}:?\d{2})?", s):
            t = dt.datetime.fromisoformat(s.upper().replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=tz)).timestamp()
    except ValueError:
        raise _bad("That date isn't a real one.") from None
    s = re.sub(r"^(until|till|til|to|for|on)\s+", "", s)
    m = re.fullmatch(r"in (\d{1,3}|[a-z]+(?: [a-z]+)?) (minute|min|hour|hr|day|week)s?", s)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _WORDN.get(m.group(1))
        if n is None:
            raise _bad("How long? Say it like “in 2 hours”.")
        unit = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400}[m.group(2)]
        return now + n * unit
    if s in ("in half an hour", "half an hour"):
        return now + 1800
    if s in ("later", "later today"):
        return now + 3 * 3600
    at = None
    m = re.fullmatch(r"(.*?)\s*\bat (.+)", s)
    if m:
        at = _clock_time(m.group(2))
        if at is None:
            raise _bad("What time? Say it like “at 3 pm”.")
        s = m.group(1).strip()
    day, part = None, None
    words = s.split()
    if s in ("tonight", "this evening"):
        day, part = t0.date(), EVENING
    elif s in ("this afternoon",):
        day, part = t0.date(), AFTERNOON
    elif s in ("this morning",):
        day, part = t0.date(), MORNING
    elif s in ("", "today") and at is not None:
        day = t0.date()
    elif words and words[0] == "tomorrow" and len(words) <= 2:
        day = t0.date() + dt.timedelta(days=1)
        part = _PARTS.get(words[1]) if len(words) == 2 else MORNING
        if len(words) == 2 and part is None:
            raise _bad("Say “tomorrow morning”, “tomorrow afternoon” or “tomorrow evening”.")
    elif s in ("next week",):
        day, part = t0.date() + dt.timedelta(days=7 - t0.weekday()), MORNING
    elif s in ("this weekend", "the weekend", "weekend"):
        day, part = t0.date() + dt.timedelta(days=(5 - t0.weekday()) % 7 or 7), MORNING
    else:
        w = [x for x in words if x not in ("next", "this", "on")]
        if w and w[0] in _WEEK and len(w) <= 2:
            ahead = (_WEEK.index(w[0]) - t0.weekday()) % 7 or 7
            day = t0.date() + dt.timedelta(days=ahead)
            part = _PARTS.get(w[1]) if len(w) == 2 else MORNING
            if len(w) == 2 and part is None:
                raise _bad("Say it like “Monday morning”.")
    if day is None:
        raise _bad("Callsheet didn't understand that time. Try “tomorrow morning”, “Monday”, “in 2 hours” or "
                   "2026-11-19T09:00.")
    h, mi = at if at is not None else (part, 0)
    t = dt.datetime.combine(day, dt.time(h, mi), tz)
    if at is not None and not s and t.timestamp() <= now:
        t += dt.timedelta(days=1)       # "at 9" when 9 has gone: tomorrow at 9
    return t.timestamp()


class VoiceActions:
    """`tokens`: apitokens.Tokens (its store holds voice_undo); `app`: api.App (actions, ledger)."""

    def __init__(self, app, tokens, clock):
        self.app, self.tokens, self.clock = app, tokens, clock
        self.rate = apitokens.RateLimit(n=ACT_N, s=ACT_S, clock=clock)
        with tokens.store.lock:
            tokens.store.db.executescript(_SCHEMA)

    def _acts(self):
        a = getattr(self.app, "actions", None)
        if a is None or getattr(self.app, "ledger", None) is None:
            raise Refused(503, "Callsheet's ledger isn't available right now. Try again later.")
        return a

    def _tz(self):
        try:
            return self.app.ledger.tz()
        except Exception:  # noqa: BLE001
            return dt.timezone.utc

    def _iso(self, ts):
        return dt.datetime.fromtimestamp(ts, self._tz()).isoformat(timespec="seconds") if ts else None

    def _shown(self):
        """The ids Today shows (the same lists the brief and /v1/ledger read): open items and open clashes."""
        v = self.app.ledger.view()
        items = {r["id"] for sec in ("security", "needs_you", "now_next", "coming_up", "clean_up")
                 for r in v.get(sec) or []}
        return items, {c["id"] for c in v.get("conflicts") or []}

    def item(self, kind, n):
        """The item as it is now: {id, kind, label, status} (+ snoozed_until while a snooze holds it)."""
        a = self.app.actions
        if kind == "c":
            r = a.ms._read("SELECT state, resolved_why, why FROM ledger_conflicts WHERE id=?", (n,))  # noqa: SLF001
            if not r:
                return {"id": f"c{n}", "kind": "clash", "label": "", "status": "gone"}
            st = "dismissed" if r[0]["resolved_why"] == "dismissed" else r[0]["state"]
            return {"id": f"c{n}", "kind": "clash", "label": actions_mod.clash_words(dict(r[0])), "status": st}
        it = a.item_labels([n]).get(n, "")
        r = a.ms._read("SELECT status FROM ledger_items WHERE id=?", (n,))  # noqa: SLF001
        out = {"id": f"l{n}", "kind": "item", "label": it, "status": r[0]["status"] if r else "gone"}
        due = a.ms._read("SELECT max(due_ts) AS due FROM today_reminders WHERE item=? AND state='open' AND "  # noqa: SLF001
                         "hide=1", (n,))
        if due and due[0]["due"]:
            out["snoozed_until"] = self._iso(due[0]["due"])
        return out

    @staticmethod
    def body_of(raw):
        if not raw:
            return {}
        if len(raw) > BODY_MAX:
            raise Refused(413, "That request is too big.")
        try:
            b = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _bad("Send JSON, like {\"until\": \"tomorrow morning\"}.") from None
        if not isinstance(b, dict):
            raise _bad("Send a JSON object.")
        return b

    def _take(self, tok):
        wait = self.rate.take(tok["id"])
        if wait is not True:
            raise Refused(429, f"Too many changes: at most {ACT_N} a minute. Try again in {wait} seconds.")

    def act(self, tok, ref, verb, raw):
        """One action from one device. -> {ok, undo_token, item, label, undo_until}."""
        body = self.body_of(raw)
        allowed = {"until"} if verb == "snooze" else set()
        extra = sorted(set(body) - allowed)
        if extra:
            raise _bad(f"Unknown field: {str(extra[0])[:40]}.")
        if verb not in VERBS:
            raise Refused(404, "There's nothing here.")
        kind, n = ref[0], int(ref[1:])
        acts = self._acts()
        if kind == "c" and verb != "dismiss":
            raise _bad("A clash can only be dismissed.")
        until = None
        if verb == "snooze":
            if "until" not in body:
                raise _bad("Say until when, like {\"until\": \"tomorrow morning\"}.")
            until = parse_until(body["until"], self.clock(), self._tz())
            if until <= self.clock():
                raise _bad("Pick a time in the future.")
            if until > self.clock() + 400 * 86400:
                raise _bad("Pick a time in the next year.")
        items, clashes = self._shown()
        if (kind == "l" and n not in items) or (kind == "c" and n not in clashes):
            raise Refused(404, "That item isn't on Today now (done, dismissed or gone).")
        self._take(tok)
        name = "clash.dismiss" if kind == "c" else "item.snooze" if verb == "snooze" else "item.done"
        label = None
        if kind == "l" and verb == "dismiss":
            label = "Dismissed: " + (acts.item_labels([n]).get(n) or "an item")
        try:
            e = acts.run(name, [n], {"until_ts": until} if until else {}, via="voice", ref=tok["name"], label=label)
        except actions_mod.Invalid as ex:
            raise Refused(409, str(ex)) from None
        utok = UNDO_PREFIX + secrets.token_urlsafe(24)
        now = self.clock()
        with self.tokens.store.lock:
            self.tokens.store.db.execute("DELETE FROM voice_undo WHERE created < ?", (now - 7 * 86400,))
            self.tokens.store.db.execute("INSERT INTO voice_undo (hash, log, token, created) VALUES (?,?,?,?)",
                                         (self._hash(utok), e["id"], tok["id"], now))
        return {"ok": True, "undo_token": utok, "undo_until": self._iso(now + UNDO_S), "label": e["label"],
                "item": self.item(kind, n)}

    def _hash(self, utok):
        return hashlib.sha256((self.tokens._salt() + ":undo:" + utok).encode()).hexdigest()  # noqa: SLF001

    def undo(self, tok, utok, raw):
        if self.body_of(raw):
            raise _bad("Undo takes no fields.")
        acts = self._acts()
        h = self._hash(utok)
        with self.tokens.store.lock:
            r = self.tokens.store.db.execute("SELECT * FROM voice_undo WHERE hash=?", (h,)).fetchone()
        if r is None or r["token"] != tok["id"]:
            raise Refused(404, "There's no such undo for this device.")
        if r["used"] is not None:
            raise Refused(409, actions_mod.DONE_ALREADY)
        if self.clock() - r["created"] > UNDO_S:
            raise Refused(410, "Undo lasts 24 hours, and this one has run out. Undo it in Callsheet: Setup › Your "
                               "rules › Recent changes.")
        self._take(tok)
        row = acts._row(r["log"])  # noqa: SLF001
        try:
            e = acts.undo(r["log"], via="voice")
        except actions_mod.Invalid as ex:
            raise Refused(409, str(ex)) from None
        with self.tokens.store.lock:
            self.tokens.store.db.execute("UPDATE voice_undo SET used=? WHERE hash=?", (self.clock(), h))
        t = (row or {}).get("targets") or []
        kind = "c" if (row or {}).get("action") == "clash.dismiss" else "l"
        return {"ok": True, "label": e["label"], "item": self.item(kind, t[0]) if t else None}
