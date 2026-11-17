"""0.17.0 (rework stage 5): preferences set by talking to Chat, and the owner's own ledger actions from Chat.

Approved 2026-11-11. Chat
proposes a change (`propose_setting`, `propose_action`); the deterministic owner's-words gate (chatstore.setting_mode)
applies it at once when the owner's own latest message says all of it (decided 2026-11-12, with Undo), else
the card waits for the owner's Confirm. Either way a one-time token bound to the chat and the turn (chat.py) carries the change.
Applying writes here, in messages.db next to the ledger:

- `preferences`: one row per change the owner applied (kind, structured fields, a plain label, the value it replaced
  for settings that live in the phone-notification config, where it came from). Undo and delete mark the row; nothing
  is erased, and a row that changed the push config puts the old value back.
- `owner_actions`: the owner's ledger actions (done, snooze, remind me at, a note on an item), each undoable.

Readers: conflicts.Alerter (VIPs, alert rules, VIP-through-quiet, reminders), ledger.Ledger.view (snoozed items, VIPs
first in Needs you), and later stages (clean up, digests, the brief) through `Rules`. Nothing here is ever written from
mail, feeds or tool results: only Chat._rule_card (the gate passed on the owner's own words), Chat.card_apply (the owner's
Confirm), Chat.card_undo and the Setup list call apply/undo/delete.
"""
import datetime as dt
import json
import re
import time

import assistant
import ledger as ledger_mod

KINDS = ("vip", "alert_rule", "quiet_hours", "vip_through_quiet", "alerts", "cleanup", "digest", "brief", "brief_schedule",
         "assistant_name", "model_quiet_times", "area", "area_move")
SOURCES = ("gmail", "slack", "signal", "sms")
SOURCE_NAMES = {"gmail": "Email", "slack": "Slack", "signal": "Signal", "sms": "Texts"}
WHAT = ("clashes", "asks", "all")
MODES = ("never", "quiet_hours", "always")
MENTION = ("always", "stands_out", "when_asked", "never")
BRIEF_CHOICES = ("include", "skip", "short", "first")
ACTIONS = ("done", "snooze", "remind", "note")
CALLS = ("morning", "midday", "afternoon")   # store.default_settings call_times ids
CALL_NAMES = {"morning": "Morning brief", "midday": "Midday check-in", "afternoon": "Afternoon check-in"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
DIGEST_DEFAULT = "stands_out"   # the owner's answer (v4 review): mention a digest only when something stands out
# push.LEDGER_DEFAULTS (not imported: push pulls in the connectors); Undo puts these back when unset
PUSH_DEFAULTS = {"clashes": True, "vip_asks": True, "vip_through_quiet": True}
PUSH_KINDS = ("quiet_hours", "vip_through_quiet", "alerts")   # these change the phone-notification config itself
SETTING_KINDS = ("brief_schedule", "assistant_name", "model_quiet_times")   # these change Store.settings itself (Undo puts `before` back)
# 0.22.0 areas by chat (mockup areas-v1): rename / recolour an area, move a connection; applied through areas.Areas
# (self.areas) with its own Undo token kept in `before`
AREA_KINDS = ("area", "area_move")
MAX_NAMES = 10
MAX_NOTE = 500
MAX_ITEMS = 10
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}', label TEXT NOT NULL DEFAULT '',
  before TEXT, ref TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, undone_at REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS owner_actions (
  id INTEGER PRIMARY KEY, item INTEGER NOT NULL, action TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}',
  label TEXT NOT NULL DEFAULT '', before TEXT, until_ts REAL, ref TEXT NOT NULL DEFAULT '', at REAL NOT NULL,
  undone_at REAL, fired_at REAL);
CREATE INDEX IF NOT EXISTS owner_actions_item ON owner_actions (item)
"""


class Invalid(ValueError):
    """A change that can't be applied; the message is a plain sentence."""


def ensure(ms):
    def run(db):
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
    ms.write(run)


# --- checking a change ---------------------------------------------------------------------------------------------
def _name(v, what="name", n=80):
    if not isinstance(v, str) or not " ".join(v.split()):
        raise Invalid(f"The {what} is missing.")
    v = " ".join(v.split())
    if len(v) > n or re.search(r"[<>{}\[\]\\]|https?:", v):
        raise Invalid(f"That {what} doesn't look right.")
    return v


def _names(v):
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise Invalid("Names come as a list.")
    return list(dict.fromkeys(_name(x) for x in v))[:MAX_NAMES]


def _sources(v):
    if v in (None, "", "all"):
        return []
    if not isinstance(v, list) or any(s not in SOURCES for s in v):
        raise Invalid("Sources are email, Slack, Signal and texts.")
    return [s for s in SOURCES if s in v]


def _bool(v, what, default=None):
    if v is None:
        if default is None:
            raise Invalid(f"Say whether {what} is on or off.")
        return default
    if not isinstance(v, bool):
        raise Invalid(f"{what} is on or off.")
    return v


def _hhmm(v, what):
    if not isinstance(v, str) or not _HHMM.fullmatch(v):
        raise Invalid(f"The {what} time is missing.")
    return v


def _one(v, allowed, what, default=None):
    if v in (None, "") and default is not None:
        return default
    if v not in allowed:
        raise Invalid(f"{what} must be one of: {', '.join(allowed)}.")
    return v


def check(kind, f):
    if kind in AREA_KINDS:
        return _check_area(kind, f)
    """The normalized fields of one change, or Invalid."""
    f = f if isinstance(f, dict) else {}
    if kind == "vip":
        tq = f.get("through_quiet")
        return {"person": _name(f.get("person"), "person"), "sources": _sources(f.get("sources")),
                "dm_only": _bool(f.get("dm_only"), "DMs only", False), "alerts": _bool(f.get("alerts"), "alerts", True),
                "needs_you": _bool(f.get("needs_you"), "Needs you", True),
                "through_quiet": tq if isinstance(tq, bool) else None}
    if kind == "alert_rule":
        mode = _one(f.get("mode"), MODES, "mode")
        out = {"sources": _sources(f.get("sources")), "what": _one(f.get("what"), WHAT, "what", "all"), "mode": mode,
               "except": _names(f.get("except"))}
        if mode == "quiet_hours":
            out["start"], out["end"] = _hhmm(f.get("start"), "start"), _hhmm(f.get("end"), "end")
            if out["start"] == out["end"]:
                raise Invalid("The quiet time starts and ends at the same time.")
        return out
    if kind == "quiet_hours":
        return {"start": _hhmm(f.get("start"), "start"), "end": _hhmm(f.get("end"), "end")}
    if kind == "vip_through_quiet":
        return {"on": _bool(f.get("on"), "VIPs through quiet hours")}
    if kind == "alerts":
        return {"what": _one(f.get("what"), ("clashes", "asks"), "what"), "on": _bool(f.get("on"), "alerts")}
    if kind == "cleanup":
        return {"match": _one(f.get("match"), ("sender", "words"), "match"), "value": _name(f.get("value"), "match", 120),
                "counts": _bool(f.get("counts"), "counts as clean up", True)}
    if kind == "digest":
        return {"name": _name(f.get("name"), "digest"), "mention": _one(f.get("mention"), MENTION, "mention")}
    if kind == "brief_schedule":   # 0.17.0: "move my morning brief to 7 am on weekdays" (Setup › Your day › Brief times)
        days = f.get("days")
        if days in (None, "", []):
            days = None
        elif not isinstance(days, list) or any(d not in WEEKDAYS for d in days):
            raise Invalid("Days are mon, tue, wed, thu, fri, sat and sun.")
        else:
            days = [d for d in WEEKDAYS if d in days]
        return {"call": _one(f.get("call"), CALLS, "call", "morning"), "time": _hhmm(f.get("time"), "brief"),
                "days": days}
    if kind == "assistant_name":   # "call yourself Jarvis" (Setup › Your day › You)
        try:
            return {"name": assistant.clean(f.get("name"))}
        except assistant.Invalid as e:
            raise Invalid(str(e)) from None
    if kind == "model_quiet_times":   # "keep the model server quiet from 1 am to 4 am" (Setup › AI › Advanced)
        out = {"start": _hhmm(f.get("start"), "start"), "end": _hhmm(f.get("end"), "end"),
               "on": _bool(f.get("on"), "the quiet time", True)}
        if out["start"] == out["end"]:
            raise Invalid("The quiet time starts and ends at the same time.")
        return out
    if kind == "brief":
        note = f.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 200):
            raise Invalid("A brief note is at most 200 characters.")
        return {"topic": _name(f.get("topic"), "topic"), "choice": _one(f.get("choice"), BRIEF_CHOICES, "choice"),
                "note": " ".join((note or "").split())}
    raise Invalid("Callsheet can't set that by chat yet.")


# --- plain words ---------------------------------------------------------------------------------------------------
def clock12(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def _src_words(srcs, dm=False):
    if not srcs:
        return "every source" if not dm else "Slack DMs"
    words = [SOURCE_NAMES[s] for s in srcs]
    if dm and srcs == ["slack"]:
        return "Slack DMs"
    return " and ".join(words)


def days_words(days):
    """mon..fri -> "weekdays", sat sun -> "weekends", all -> "every day", else "Mon, Wed and Fri"."""
    d = [x for x in WEEKDAYS if x in (days or [])]
    if d == list(WEEKDAYS):
        return "every day"
    if d == list(WEEKDAYS[:5]):
        return "weekdays"
    if d == ["sat", "sun"]:
        return "weekends"
    return _list([DAY_NAMES[x] for x in d]) if d else ""


def _list(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


_WHAT = {"clashes": "clash alerts", "asks": "alerts for asks", "all": "alerts"}
_MENTION = {"always": "always mention", "stands_out": "only when something stands out", "when_asked": "only when you ask",
            "never": "never mention"}
_BRIEF = {"include": "included", "skip": "left out", "short": "kept short", "first": "first"}


def _check_area(kind, f):
    def txt(k, n, req=True):
        v = f.get(k)
        if v in (None, "") and not req:
            return ""
        if not isinstance(v, str) or not v.strip() or len(v.strip()) > n:
            raise Invalid("Say which area and what to change.")
        return v.strip()
    if kind == "area":
        out = {"area_id": txt("area_id", 40), "area_name": txt("area_name", 60), "name": txt("name", 40, False),
               "colour": txt("colour", 7, False), "colour_name": txt("colour_name", 20, False)}
        if not out["name"] and not out["colour"]:
            raise Invalid("Say the new name or colour.")
        return out
    return {"conn": txt("conn", 200), "conn_name": txt("conn_name", 200), "to": txt("to", 40),
            "to_name": txt("to_name", 60), "past": _bool(f.get("past"), "past items", True)}


def label(kind, f):
    if kind == "area":
        bits = ([f"{f['area_name']} renamed {f['name']}"] if f.get("name") else []) + \
            ([f"{f.get('name') or f['area_name']} in {f['colour_name'] or f['colour']}"] if f.get("colour") else [])
        return "; ".join(bits)
    if kind == "area_move":
        return f"Move {f['conn_name']} to {f['to_name']}" + (" with what it already read" if f["past"] else "")
    """One plain sentence for the card's "Done: …" and the rules list."""
    if kind == "vip":
        parts = [w for w, on in (("alerts", f["alerts"]), ("Needs you", f["needs_you"])) if on] or ["nothing yet"]
        lab = f"{f['person']} is VIP for {' and '.join(parts)}"
        if f["sources"] or f["dm_only"]:
            lab += f" ({_src_words(f['sources'], f['dm_only'])})"
        return lab
    if kind == "alert_rule":
        what, src = _WHAT[f["what"]], _src_words(f["sources"])
        lab = {"never": f"No {what} from {src}", "always": f"{what.capitalize()} from {src} any time, even in quiet hours",
               "quiet_hours": f"{what.capitalize()} from {src}: quiet from {clock12(f.get('start', '00:00'))} to "
                              f"{clock12(f.get('end', '00:00'))}"}[f["mode"]]
        return lab + (f", except {_list(f['except'])}" if f["except"] else "")
    if kind == "quiet_hours":
        return f"Quiet hours from {clock12(f['start'])} to {clock12(f['end'])}"
    if kind == "vip_through_quiet":
        return "VIPs get through quiet hours" if f["on"] else "VIPs wait for quiet hours to end"
    if kind == "alerts":
        return f"{_WHAT[f['what']].capitalize()} {'on' if f['on'] else 'off'}"
    if kind == "cleanup":
        what = f"Mail from {f['value']}" if f["match"] == "sender" else f"Messages about “{f['value']}”"
        mail = f["match"] == "sender"
        return f"{what} {('counts' if mail else 'count') if f['counts'] else ('doesn’t count' if mail else 'don’t count')} as Clean up"
    if kind == "digest":
        return f"{f['name']}: {_MENTION[f['mention']]}"
    if kind == "brief_schedule":
        return f"{CALL_NAMES[f['call']]} at {clock12(f['time'])}" + (f" on {days_words(f['days'])}" if f.get("days") else "")
    if kind == "assistant_name":
        return f"The assistant is called {f['name']}"
    if kind == "model_quiet_times":
        span = f"{clock12(f['start'])} to {clock12(f['end'])}"
        return f"Model server quiet from {span}" if f["on"] else f"Model server no longer quiet from {span}"
    if kind == "brief":
        return f"In the brief, {f['topic']}: {_BRIEF[f['choice']]}" + (f" ({f['note']})" if f.get("note") else "")
    return kind


def rows(kind, f):
    """The card's rows: [field, value] pairs (as in the v4 mockup: Who, Alerts, Needs you, Quiet …)."""
    if kind == "area":
        return [["Area", f["area_name"]]] + ([["New name", f["name"]]] if f.get("name") else []) + \
            ([["Colour", f["colour_name"] or f["colour"]]] if f.get("colour") else [])
    if kind == "area_move":
        return [["Connection", f["conn_name"]], ["Move to", f["to_name"]],
                ["What it already read", "Moves too" if f["past"] else "Stays where it is"]]
    if kind == "vip":
        tq = {True: "Gets through quiet hours", False: "Waits for quiet hours to end",
              None: "Your VIP setting for quiet hours"}[f["through_quiet"]]
        return [["Who", f["person"] + (f" · {_src_words(f['sources'], f['dm_only'])}" if f["sources"] or f["dm_only"] else "")],
                ["Alerts", "Asks alert as they arrive" if f["alerts"] else "No alerts"],
                ["Needs you", "Goes to the top" if f["needs_you"] else "In the usual order"], ["Quiet hours", tq]]
    if kind == "alert_rule":
        out = [["Source", _src_words(f["sources"]).capitalize()], ["What", _WHAT[f["what"]].capitalize()]]
        out.append(["When", {"never": "Never", "always": "Any time, even in quiet hours",
                             "quiet_hours": f"Quiet from {clock12(f.get('start', '00:00'))} to {clock12(f.get('end', '00:00'))}"}[f["mode"]]])
        if f["except"]:
            out.append(["Except", _list(f["except"])])
        return out
    if kind == "assistant_name":
        return [["Name", f["name"]]]
    if kind == "model_quiet_times":
        return [["Quiet time", f"{clock12(f['start'])} to {clock12(f['end'])}"],
                ["Change", "Callsheet leaves your model server alone then" if f["on"] else "Removed from your quiet times"]]
    if kind == "brief_schedule":
        out = [["Brief", CALL_NAMES[f["call"]]], ["Time", clock12(f["time"])]]
        out.append(["Days", days_words(f["days"]).capitalize() if f.get("days") else "The days it has now"])
        return out
    return [["Change", label(kind, f)]]


# --- matching people -----------------------------------------------------------------------------------------------
def _toks(s):
    return [t for t in re.findall(r"[^\W\d_]+", str(s or "").lower()) if len(t) > 1]


def name_matches(rule_name, names=(), handle=""):
    """A rule's person matches a sender when every word of the rule's name is a word of one of the sender's names
    ("Lena" matches "Lena Park"), or the rule names the handle itself (an email address)."""
    rn = str(rule_name or "").strip().lower()
    if not rn:
        return False
    if "@" in rn:
        return rn == str(handle or "").strip().lower()
    want = set(_toks(rn))
    return bool(want) and any(want <= set(_toks(n)) for n in names if n)


def _stems(s):
    """Words for matching clean-up rules: lower case, a plural s folded ("receipts" matches "receipt")."""
    return {t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t for t in _toks(s)}


def _in_window(local, start, end):
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    t = local.hour * 60 + local.minute
    return s <= t < e if s < e else (t >= s or t < e)


def _src(source):
    """A store source as rules name it: a live text notification (0.16.1 sms_live) is a text ("sms")."""
    return "sms" if source == "sms_live" else source


VIP_DEFAULTS = {"sources": [], "dm_only": False, "alerts": True, "needs_you": True, "through_quiet": None}
VIP_OPTS = tuple(VIP_DEFAULTS)


def vip_fields(v):
    """0.22.0 VIPs in one store: one VIP of Setup's list (store.Store.vips) as the fields a VIP rule had: {person,
    sources, dm_only, alerts, needs_you, through_quiet}. A VIP added in Setup has the defaults."""
    o = v.get("opts") if isinstance(v.get("opts"), dict) else {}
    f = {k: o.get(k, d) for k, d in VIP_DEFAULTS.items()}
    f["sources"] = [s for s in (f["sources"] if isinstance(f["sources"], list) else []) if s in SOURCES]
    f["through_quiet"] = f["through_quiet"] if isinstance(f["through_quiet"], bool) else None
    for k in ("dm_only", "alerts", "needs_you"):
        f[k] = bool(f[k])
    f["person"] = v.get("name") or v.get("email") or ""
    return f


def vip_words(v):
    """Setup's VIP list: what Chat set for this VIP, in a few words ("" for a VIP with the defaults)."""
    f = vip_fields(v)
    if all(f[k] == VIP_DEFAULTS[k] for k in VIP_OPTS):
        return ""
    bits = [_src_words(f["sources"], f["dm_only"])] if f["sources"] or f["dm_only"] else []
    if not f["alerts"]:
        bits.append("no alerts")
    if not f["needs_you"]:
        bits.append("usual order in Needs you")
    if f["through_quiet"] is not None:
        bits.append("gets through quiet hours" if f["through_quiet"] else "waits for quiet hours")
    return ", ".join(bits)


def find_vip(vips, person):
    """The VIP on the list that `person` (as the owner named them, or an address) means, or None: the same name or
    address first, else the one VIP whose name has all of its words ("Lena" for Lena Park); several: None."""
    p = " ".join(str(person or "").split()).lower()
    if not p:
        return None
    same = [v for v in vips if v["name"].strip().lower() == p or (v.get("email") and v["email"].lower() == p)]
    if same:
        return same[-1]
    part = [v for v in vips if "@" not in p and name_matches(p, [v["name"]])]
    return part[0] if len(part) == 1 else None


class Rules:
    """A snapshot of the active preferences, for the alerter, the ledger view and later stages. `vips`: Setup's VIP
    list (store.Store.vips), the one VIP home since 0.22.0."""

    def __init__(self, rows_, vips=()):
        self.rows = rows_
        self.vips = list(vips or ())
        self.by_kind = {}
        for r in rows_:
            if r["kind"] != "vip":   # 0.22.0: a VIP set by Chat lives on Setup's list; its row is the journal only
                self.by_kind.setdefault(r["kind"], []).append(r)

    def of(self, kind):
        return [r["fields"] for r in self.by_kind.get(kind, [])]

    def vip(self, source, handle, names=(), dm=False):
        """The newest matching VIP's fields (vip_fields), or None. `names`: the sender's names (message, directory).
        A VIP matches by its address; one Chat added or set options for matches by name too, as its rule did."""
        source = _src(source)
        h = str(handle or "").strip().lower()
        for v in reversed(self.vips):
            f = vip_fields(v)
            if f["sources"] and source not in f["sources"]:
                continue
            if f["dm_only"] and not (source == "slack" and dm):
                continue
            em = str(v.get("email") or "").lower()
            if (em and h == em) or ((not em or v.get("opts")) and name_matches(v.get("name"), names, handle)):
                return f
        return None

    def alert(self, what, senders, local):
        """For one alert ("clashes" or "asks") whose messages came from `senders` [(source, handle, names)]: "off",
        "quiet" (held now), "through" (even in quiet hours) or None (the usual settings), from the newest matching
        alert rule."""
        srcs = {_src(s[0]) for s in senders}
        for f in reversed(self.of("alert_rule")):
            if f["what"] not in (what, "all"):
                continue
            if f["sources"] and not (srcs & set(f["sources"])):
                continue
            if any(name_matches(x, s[2], s[1]) for x in f["except"] for s in senders):
                continue
            if f["mode"] == "never":
                return "off"
            if f["mode"] == "always":
                return "through"
            if _in_window(local, f["start"], f["end"]):
                return "quiet"
        return None

    def cleanup(self):
        return self.of("cleanup")

    def cleanup_rule(self, handle="", names=(), text=""):
        """The newest cleanup rule (the whole row: id, label, fields) that matches a message's sender (an address, a
        domain, or a name: name_matches) or its words (every word of the rule in `text`, plurals folded), else None."""
        have = _stems(text)
        for r in reversed(self.by_kind.get("cleanup", [])):
            f = r["fields"]
            v = str(f.get("value") or "").strip().lower()
            if f.get("match") == "sender":
                h = str(handle or "").strip().lower()
                if "@" in v or " " in v or "." not in v:
                    hit = name_matches(v, names, h)
                else:   # a domain: the sender's address is at it or below it
                    hit = h.endswith("@" + v) or h.endswith("." + v)
            else:
                want = _stems(v)
                hit = bool(want) and want <= have
            if hit:
                return r
        return None

    def digest(self, name):
        """When to mention one digest (newsletter): the newest rule naming it, else DIGEST_DEFAULT."""
        for f in reversed(self.of("digest")):
            if name_matches(f["name"], [name]):
                return f["mention"]
        return DIGEST_DEFAULT

    def digests(self):
        return self.of("digest")

    def brief(self):
        return self.of("brief")


# --- the store -----------------------------------------------------------------------------------------------------
def _row(r):
    d = dict(r)
    d["fields"] = json.loads(d.get("fields") or "{}")
    d["before"] = json.loads(d["before"]) if d.get("before") else None
    return d


class Prefs:
    """Preferences and the owner's ledger actions in messages.db. push_get() -> the push config, push_set(patch)
    changes it (Store.update_push)."""

    def __init__(self, ms, push_get=None, push_set=None, clock=time.time, settings_get=None, settings_set=None):
        self.ms, self.push_get, self.push_set, self.clock = ms, push_get, push_set, clock
        # brief_schedule changes the brief times themselves (Store.settings / Store.update_settings), like the push kinds
        self.settings_get, self.settings_set = settings_get, settings_set
        self.areas = None   # 0.22.0: areas.Areas (api.App sets it)
        self.vip_store = None   # 0.22.0: store.Store, Setup's VIP list, the one VIP home (api.App sets it)
        ensure(ms)
        ledger_mod.ensure(ms)

    # preferences
    def apply(self, kind, fields, ref=""):
        if kind not in KINDS:
            raise Invalid("Callsheet can't set that by chat yet.")
        f = check(kind, fields)
        before = None
        if kind in PUSH_KINDS:
            if self.push_get is None or self.push_set is None:
                raise Invalid("Phone notifications aren't available.")
            patch = self._push_patch(kind, f)
            cur = self.push_get()
            before = {k: cur.get(k, PUSH_DEFAULTS.get(k)) for k in patch}
            self.push_set(patch)
        elif kind == "brief_schedule":
            if self.settings_get is None or self.settings_set is None:
                raise Invalid("The brief times aren't available.")
            cur = next((c for c in self.settings_get().get("call_times") or [] if c.get("id") == f["call"]), None)
            if cur is None:
                raise Invalid("That brief time doesn't exist.")
            before = {"call_times": [{k: cur[k] for k in ("id", "time", "days", "enabled") if k in cur}]}
            patch = {"id": f["call"], "time": f["time"], "enabled": True}
            if f["days"]:
                patch["days"] = f["days"]
            try:
                self.settings_set({"call_times": [patch]})
            except ValueError as e:   # store.InputError: a plain sentence
                raise Invalid(str(e)) from None
        elif kind == "assistant_name":
            if self.settings_get is None or self.settings_set is None:
                raise Invalid("The assistant's name isn't available.")
            before = {"assistant_name": assistant.of(self.settings_get())}
            try:
                self.settings_set({"assistant_name": f["name"]})
            except ValueError as e:
                raise Invalid(str(e)) from None
        elif kind == "model_quiet_times":
            if self.settings_get is None or self.settings_set is None:
                raise Invalid("The model server's quiet times aren't available.")
            cur = [list(w) for w in self.settings_get().get("model_quiet_times") or []]
            win = [f["start"], f["end"]]
            if f["on"]:
                new = cur if win in cur else cur + [win]
            elif win in cur:
                new = [w for w in cur if w != win]
            else:
                raise Invalid(f"There's no quiet time from {clock12(f['start'])} to {clock12(f['end'])}.")
            before = {"model_quiet_times": cur}
            try:
                self.settings_set({"model_quiet_times": new})
            except ValueError as e:   # store.InputError: too many, or a time it can't read
                raise Invalid(str(e)) from None
        elif kind in AREA_KINDS:
            if getattr(self, "areas", None) is None:
                raise Invalid("Areas aren't available.")
            try:
                if kind == "area":
                    _, token = self.areas.update(f["area_id"], f["name"] or None, f["colour"] or None)
                else:
                    _, token = self.areas.move(f["conn"], f["to"], f["past"])
            except (ValueError, LookupError) as e:   # areas.AreaError, store.InputError / NotFound: plain sentences
                raise Invalid(str(e)) from None
            before = {"area_undo": token}
        elif kind == "vip":
            before = self._vip_apply(f)
        now = self.clock()
        lab = label(kind, f)
        rid = self.ms.write(lambda db: db.execute(
            "INSERT INTO preferences (kind, fields, label, before, ref, created_at) VALUES (?,?,?,?,?,?)",
            (kind, json.dumps(f), lab, json.dumps(before) if before is not None else None, str(ref)[:80], now)).lastrowid)
        return self.get(rid)

    def _vips(self):
        st = getattr(self, "vip_store", None)
        if st is None:
            return []
        try:
            return st.vips()
        except Exception:  # noqa: BLE001 (the usual settings still apply)
            return []

    def _vip_apply(self, f, add=True):
        """0.22.0 VIPs in one store: Chat's VIP goes on Setup's list (a new one by name, in Personal, or the options
        of the one it names). Returns `before` for Undo: {vip_id, prev: the options it had, or None when added}."""
        st = getattr(self, "vip_store", None)
        if st is None:
            raise Invalid("The VIP list isn't available just now.")
        opts = {k: f[k] for k in VIP_OPTS}
        v = find_vip(st.vips(), f["person"])
        try:
            if v is not None:
                st.set_vip_opts(v["id"], opts)
                return {"vip_id": v["id"], "prev": v.get("opts") or {}}
            p = f["person"]
            v = st.add_vip(p, p.lower() if "@" in p else "", "personal", opts=opts, by_name=True)
        except (ValueError, LookupError) as e:   # store.InputError / NotFound: a plain sentence
            raise Invalid(str(e)) from None
        return {"vip_id": v["id"], "prev": None}

    def _vip_undo(self, before):
        st = getattr(self, "vip_store", None)
        if st is None or not isinstance(before, dict) or not before.get("vip_id"):
            return
        try:
            if before.get("prev") is None:
                st.delete_vip(before["vip_id"])
            else:
                st.set_vip_opts(before["vip_id"], before["prev"])
        except LookupError:   # removed in Setup since: nothing to put back
            pass

    def migrate_vips(self):
        """0.22.0, once per row: VIPs that earlier versions kept as Chat rules move onto Setup's VIP list (the same
        person keeps being a VIP, with the rule's options). The rule row stays as the journal, with Undo."""
        if getattr(self, "vip_store", None) is None:
            return 0
        n = 0
        for r in self.ms._read("SELECT * FROM preferences WHERE kind='vip' AND undone_at IS NULL AND "  # noqa: SLF001
                               "deleted_at IS NULL ORDER BY id"):
            r = _row(r)
            if isinstance(r["before"], dict) and r["before"].get("vip_id"):
                continue
            try:
                before = dict(self._vip_apply(check("vip", r["fields"])), migrated=True)
            except Invalid:
                continue
            self.ms.write(lambda db, b=before, i=r["id"]: db.execute("UPDATE preferences SET before=? WHERE id=?",
                                                                     (json.dumps(b), i)))
            n += 1
        return n

    @staticmethod
    def _push_patch(kind, f):
        if kind == "quiet_hours":
            return {"quiet_start": f["start"], "quiet_end": f["end"]}
        if kind == "vip_through_quiet":
            return {"vip_through_quiet": f["on"]}
        return {"clashes" if f["what"] == "clashes" else "vip_asks": f["on"]}

    def get(self, pid):
        r = self.ms._read("SELECT * FROM preferences WHERE id=?", (int(pid),))  # noqa: SLF001
        return _row(r[0]) if r else None

    def _end(self, pid, col):
        r = self.get(pid)
        if r is None or r["deleted_at"] is not None or (col == "undone_at" and r["undone_at"] is not None):
            raise Invalid("That rule is already gone.")
        if r["undone_at"] is None and r["kind"] == "vip":
            self._vip_undo(r["before"])
        elif r["undone_at"] is None and r["before"] and r["kind"] in AREA_KINDS:
            if getattr(self, "areas", None) is None:
                raise Invalid("Areas aren't available.")
            try:
                self.areas.undo(r["before"].get("area_undo"))
            except ValueError as e:
                raise Invalid(str(e)) from None
        elif r["undone_at"] is None and r["before"] and r["kind"] in SETTING_KINDS:
            if self.settings_set is not None:
                self.settings_set(r["before"])
        elif r["undone_at"] is None and r["before"] and self.push_set is not None:
            self.push_set({k: PUSH_DEFAULTS.get(k) if v is None else v for k, v in r["before"].items()
                           if v is not None or k in PUSH_DEFAULTS})
        now = self.clock()
        self.ms.write(lambda db: db.execute(f"UPDATE preferences SET {col}=?, undone_at=coalesce(undone_at, ?) "
                                            "WHERE id=?", (now, now, int(pid))))
        return self.get(pid)

    def undo(self, pid):
        return self._end(pid, "undone_at")

    def delete(self, pid):
        return self._end(pid, "deleted_at")

    def active(self):
        return [_row(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM preferences WHERE undone_at IS NULL AND deleted_at IS NULL ORDER BY id")]

    def rules(self):
        return Rules(self.active(), self._vips())

    def listing(self):
        """GET api/preferences: the active rules (newest first) and the last undone ones, plus recent ledger actions."""
        have = {v["id"] for v in self._vips()}
        # 0.22.0: a VIP set by Chat and since removed from Setup's list is no longer a live rule
        live = [self.public(r) for r in reversed(self.active()) if r["kind"] != "vip" or
                not isinstance(r["before"], dict) or r["before"].get("vip_id") in have]
        undone = [self.public(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM preferences WHERE undone_at IS NOT NULL AND deleted_at IS NULL ORDER BY id DESC LIMIT 10")]
        acts = [self.public_action(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM owner_actions ORDER BY id DESC LIMIT 20")]
        return {"rules": live, "undone": undone, "actions": acts, "count": len(live), "for_brief": self.for_brief()}

    def for_brief(self):
        """Stored for the brief (rework stage 6): the digests' mention flags (unnamed digests: DIGEST_DEFAULT) and the
        owner's brief preferences, newest last."""
        r = self.rules()
        return {"digests": r.digests(), "digest_default": DIGEST_DEFAULT, "brief": r.brief()}

    @staticmethod
    def public(r):
        r = r if isinstance(r, dict) and isinstance(r.get("fields"), dict) else _row(r)
        ref = str(r.get("ref") or "")
        m = re.fullmatch(r"(c_[0-9a-f]{16}):(\d{1,6})", ref)
        return {"id": r["id"], "kind": r["kind"], "label": r["label"], "fields": r["fields"],
                "rows": rows(r["kind"], r["fields"]), "created_at": r["created_at"], "undone": r["undone_at"] is not None,
                "from_chat": ref.startswith("c_"), "chat": m.group(1) if m else None,
                "turn": int(m.group(2)) if m else None}

    # ledger actions
    def act(self, action, items, fields, ref="", tz=None):
        """The owner's action on ledger items: done (status done), snooze (hidden until a time), remind (hidden until
        a time, then a phone reminder), note (the owner's note in the item's history). One row per item."""
        if action not in ACTIONS:
            raise Invalid("That isn't something Callsheet can do to an item.")
        if action in ("snooze", "remind"):   # 0.22.0 one reminder store: today.Today.set_aside (actions.py)
            raise Invalid("Snooze and Remind me go through Today.")
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
            raise Invalid("Pick one to ten items.")
        until = fields.get("until_ts")
        note = " ".join(str(fields.get("note") or "").split())[:MAX_NOTE]
        now = self.clock()
        if action in ("snooze", "remind") and not (isinstance(until, (int, float)) and now < until <= now + 400 * 86400):
            raise Invalid("Pick a time in the future.")
        if action == "note" and not note:
            raise Invalid("Write the note first.")
        out = []

        def run(db):
            for i in items:
                it = ledger_mod.get_item(db, int(i))
                if it is None:
                    raise Invalid("That item isn't in the ledger any more.")
                before = {"status": it["status"]}
                lab = action_label(action, it, fields, tz)
                if action == "done":
                    ledger_mod.set_status(db, it["id"], "done", now, note="Marked done in Chat", kind="owner")
                elif action == "note":
                    ledger_mod.history(db, it["id"], "owner_note", now, note=note)
                else:
                    ledger_mod.history(db, it["id"], "owner_" + action, now, note=f"until {until:.0f}")
                cur = db.execute("INSERT INTO owner_actions (item, action, fields, label, before, until_ts, ref, at) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (it["id"], action, json.dumps({"note": note} if note else {}),
                                                              lab, json.dumps(before), until if action in ("snooze", "remind")
                                                              else None, str(ref)[:80], now))
                out.append(cur.lastrowid)
        self.ms.write(run)
        return [self.action(a) for a in out]

    def action(self, aid):
        r = self.ms._read("SELECT * FROM owner_actions WHERE id=?", (int(aid),))  # noqa: SLF001
        return _row(r[0]) if r else None

    def undo_action(self, aid):
        a = self.action(aid)
        if a is None or a["undone_at"] is not None:
            raise Invalid("That was already undone.")
        now = self.clock()

        def run(db):
            if a["action"] == "done":
                it = ledger_mod.get_item(db, a["item"])
                if it is not None and it["status"] == "done":
                    ledger_mod.set_status(db, a["item"], (a["before"] or {}).get("status") or "open", now,
                                          note="Undone in Chat", kind="owner_undo")
            else:
                ledger_mod.history(db, a["item"], "owner_undo", now, note=f"undid {a['action']}")
                if (a["fields"] or {}).get("moved"):   # 0.22.0: moved to Today's reminders; cancel it there
                    db.execute("UPDATE today_reminders SET state='cancelled' WHERE id=? AND state='open'",
                               (a["fields"]["moved"],))
            db.execute("UPDATE owner_actions SET undone_at=? WHERE id=?", (now, a["id"]))
        self.ms.write(run)
        return self.action(aid)

    @staticmethod
    def public_action(r):
        r = r if isinstance(r, dict) and isinstance(r.get("fields"), dict) else _row(r)
        ref = str(r.get("ref") or "")
        m = re.fullmatch(r"(c_[0-9a-f]{16}):(\d{1,6})", ref)
        return {"id": r["id"], "item": r["item"], "action": r["action"], "label": r["label"], "at": r["at"],
                "until_ts": r["until_ts"], "undone": r["undone_at"] is not None, "fired": r["fired_at"] is not None,
                "chat": m.group(1) if m else None, "turn": int(m.group(2)) if m else None}

    def snoozed(self, now=None):
        """{item id: until} for items hidden until a snooze or reminder is due. 0.22.0: one reminder store, Today's
        (today_reminders, written by today.Today; the same read as Today.snoozed)."""
        now = self.clock() if now is None else now
        try:
            rows = self.ms._read("SELECT item, max(due_ts) AS u FROM today_reminders WHERE state='open' AND hide=1 AND "  # noqa: SLF001
                                 "due_ts > ? AND item IS NOT NULL GROUP BY item", (now,))
        except Exception:  # noqa: BLE001 (Today hasn't made its table yet: nothing is set aside)
            return {}
        return {r["item"]: r["u"] for r in rows}


def when_words(ts, tz):
    d = dt.datetime.fromtimestamp(ts, tz)
    return f"{d:%a} {d.day} {d:%b}, {(d.hour % 12) or 12}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"


def item_words(it):
    who = it.get("who_name") or ("you" if it.get("who") == ledger_mod.OWNER else it.get("who") or "")
    what = " ".join(str(it.get("what") or it.get("type") or "item").split())
    what = what if len(what) <= 80 else what[:79] + "…"
    return f"{who}: {what}" if who else what


def action_label(action, it, fields, tz=None):
    tz = tz or dt.timezone.utc
    w = item_words(it)
    if action == "done":
        return f"Marked done: {w}"
    if action == "snooze":
        return f"Snoozed until {when_words(fields['until_ts'], tz)}: {w}"
    if action == "remind":
        return f"Reminder {when_words(fields['until_ts'], tz)}: {w}"
    return f"Note on {w}"
