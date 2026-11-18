"""0.19.0 (rework stage 6a): visual blocks for the talk-through brief (docs/visuals.md).

The brief model writes each turn as words plus up to MAX_BLOCKS blocks ("show"). A block is a type, ids that point
into Callsheet's own store (`msg:12`, `item:7`, `cal:3`, `clash:2`) and a few options. Callsheet:

1. validates each block against its strict, versioned schema (SCHEMAS; unknown types, bad blocks and oversized ones
   are dropped and counted, never rendered raw);
2. fills the facts from the store (fill()): names, times, ages, sources and counts come from the ledger, the
   calendar and the messages, never from the model. A field the ledger doesn't hold yet (an amount, a place, a
   tracking number) is a *quote*: the model copies it from the referenced message and Callsheet keeps it only when it
   is found there, taking the store's own text; a quote that isn't found is left out and listed in the block's checks;
3. hands the filled block to static/visuals.js, which draws it in the current look with textContent only.

The `composite` type is the path for model-defined types later (chart specs and drawn pictures, options B and C):
a small tree of safe primitives (row, column, text, number, time axis, range bar, dot, list, icon, progress, table),
validated like the rest; any text with a digit must be quoted from a referenced message.

stdlib only. Nothing here calls a model or the network.
"""
import datetime as dt
import json
import math
import re
from decimal import Decimal, InvalidOperation

VERSION = 1
MAX_BLOCKS = 3                    # blocks in one turn
MAX_BLOCK_BYTES = 4096            # one block's JSON, as the model wrote it
MAX_NODES = 40                    # composite: nodes in the tree
MAX_DEPTH = 4                     # composite: nesting
REF_RE = r"(msg|item|cal|clash):[1-9][0-9]{0,11}"
HOUR_RE = r"(1[0-2]|[1-9])(:[0-5][0-9])? ?(am|pm)"
MONEY_RE = r"[-+]?[$€£][0-9][0-9,]*(\.[0-9]{1,2})?( ?(min|minimum|a month|/mo|a year|/yr))?"
NUM_RE = r"[0-9][0-9,]*(\.[0-9]+)?"
ICONS = ("mail", "signal", "slack", "text", "cal", "box", "lock", "repeat", "plane", "alert", "check", "news")
SRC = {"gmail": "mail", "signal": "signal", "slack": "slack", "sms": "text", "text": "text", "calendar": "cal",
       "feed": "news", "report": "news"}
FROM = {"mail": "email", "signal": "Signal", "slack": "Slack", "text": "text", "cal": "your calendar",
        "news": "a feed"}
CLEAN_KINDS = {"newsletter": "Newsletters", "receipt": "Receipts", "notification": "Notifications",
               "slack": "Slack chatter", "promo": "Offers", "other": "Other"}


# --- schemas (JSON Schema, draft 2020-12 subset; `x-quote`: must be found in the referenced message) -------------------
def _s(n=80, pattern=None, quote=False):
    d = {"type": "string", "minLength": 1, "maxLength": n}
    if pattern:
        d["pattern"] = pattern
    if quote:
        d["x-quote"] = True
    return d


def _q(n=80, pattern=None):
    return _s(n, pattern, quote=True)


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _arr(item, mx, mn=0):
    return {"type": "array", "items": item, "minItems": mn, "maxItems": mx}


def _int(lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi}


def _enum(*v):
    return {"type": "string", "enum": list(v)}


REF = {"type": "string", "pattern": REF_RE}
AMOUNT = _q(32, MONEY_RE)
NUMQ = _q(16, NUM_RE)
BOOL = {"type": "boolean"}
PLAIN = r"[^0-9]*"                # words only: a number has to be quoted from the store

_NODE_KINDS = {
    "row": {"c": _arr({"$ref": "#/$defs/node"}, 6, 1)},
    "column": {"c": _arr({"$ref": "#/$defs/node"}, 8, 1)},
    "text": {"text": _s(160), "style": _enum("body", "dim", "label", "big"), "ref": REF},
    "number": {"value": _q(32, NUM_RE + "|" + MONEY_RE), "unit": _s(24, PLAIN), "ref": REF},
    "time_axis": {"from": _int(0, 23), "to": _int(1, 24), "c": _arr({"$ref": "#/$defs/node"}, 8, 1)},
    "range_bar": {"ref": REF, "label": _s(40, PLAIN)},
    "dot": {"ref": REF, "label": _s(40, PLAIN)},
    "list": {"items": _arr(_s(100), 6, 1), "ref": REF},
    "icon": {"name": _enum(*ICONS), "label": _s(30, PLAIN)},
    "progress": {"value": _q(16, NUM_RE), "of": _q(16, NUM_RE), "label": _s(40, PLAIN), "ref": REF},
    "table": {"head": _arr(_s(30), 4, 1), "rows": _arr(_arr(_s(40), 4, 1), 6, 1), "ref": REF},
}
_NODE_REQ = {"row": ("c",), "column": ("c",), "text": ("text",), "number": ("value", "ref"), "time_axis": ("from", "to", "c"),
             "range_bar": ("ref",), "dot": ("ref",), "list": ("items",), "icon": ("name",),
             "progress": ("value", "of", "ref"), "table": ("rows",)}
NODE = {"oneOf": [_obj(dict({"t": {"const": k}}, **p), ("t",) + _NODE_REQ[k]) for k, p in _NODE_KINDS.items()]}

_BLOCKS = {
    "day": ("Day timeline", "Now line, events, free gaps. Dashed = only in a message.",
            {"date": _enum("today", "tomorrow"), "from": _s(8, HOUR_RE), "to": _s(8, HOUR_RE),
             "items": _arr(REF, 8), "free": BOOL}, ()),
    "clash": ("Clash diagram", "Two asks on one time axis; the overlap is hatched.",
              {"ref": REF, "sides": _arr(REF, 2, 2)}, ()),
    "asks": ("Needs-you stack", "Who is waiting, oldest first; bar = how long.",
             {"order": _enum("oldest", "newest"), "items": _arr(REF, 6), "max": _int(1, 6)}, ()),
    "upcoming": ("Coming-up strip", "30 days, one square per day, grouped by week.",
                 {"days": _int(7, 30), "items": _arr(REF, 12)}, ()),
    "money": ("Money card", "Bills with amounts and due dates.",
              {"bills": _arr(_obj({"ref": REF, "amount": AMOUNT}, ("ref",)), 4, 1)}, ("bills",)),
    "trip": ("Trip card", "Where, how long, both flights.",
             {"to": _q(40), "legs": _arr(REF, 2, 1)}, ("to", "legs")),
    "person": ("Person and thread snippet", "One message, the asked words marked.",
               {"ref": REF, "mark": _q(160)}, ("ref",)),
    "cleanup": ("Clean-up summary", "How many, and what kind.",
                {"kinds": _arr(_enum(*CLEAN_KINDS), 6),
                 # 0.24.0 (B1): Callsheet's own groups (Today's Clean up), so the picture and the words say one count
                 "groups": _arr(_obj({"label": {"type": "string", "minLength": 1, "maxLength": 40, "pattern": PLAIN},
                                      "n": _int(0, 100000)}, ("label", "n")), 8)}, ()),
    "bills": ("Bills ladder", "Several due dates on one 30-day axis: minimum, balance, amount.",
              {"days": _int(7, 30), "items": _arr(_obj({"ref": REF, "amount": AMOUNT, "min": BOOL, "balance": AMOUNT,
                                                         "card": _q(4, r"[0-9]{4}")}, ("ref",)), 6, 1)},
              ("items",)),
    "ledger": ("Money in and out", "A small ledger: who, what for, which way.",
               {"range": _enum("this week", "last week", "this month"),
                "items": _arr(_obj({"ref": REF, "dir": _enum("in", "out"), "amount": AMOUNT, "memo": _q(60)},
                                   ("ref", "dir")), 6, 1)}, ("items",)),
    "package": ("Package tracker", "Ordered, shipped, on the way, delivered; the day it arrives.",
                {"name": _q(60), "steps": _arr(_obj({"ref": REF, "stage": _enum("ordered", "shipped", "on_the_way",
                                                                                 "delivered")}, ("ref",)), 4, 1),
                 "arrives": REF, "tracking": _q(40)}, ("name", "steps")),
    "ticket": ("Event ticket", "Date, time, place, and registered or register-by.",
               {"ref": REF, "place": _q(80), "state": _enum("registered", "register_by"), "register_by": REF},
               ("ref",)),
    "tasks": ("Account tasks", "To-dos with deadlines, soonest first.",
              {"order": _enum("deadline"), "items": _arr(REF, 5, 1)}, ("items",)),
    "health": ("Health inbox", "A message is waiting. Callsheet fills in sender and time only; the text is never read out.",
               {"ref": REF}, ("ref",)),
    "stat": ("Weekly stat", "One number, seven bars, your goal as a line.",
             {"ref": REF, "metric": _q(30), "values": _arr(NUMQ, 7, 7), "goal": NUMQ, "average": NUMQ,
              "range": _q(30), "change": _q(60)}, ("ref", "metric", "values")),
    "local": ("Local alert", "Level as shape and word, and how far from home.",
              {"items": _arr(_obj({"ref": REF, "level": _enum("info", "advisory", "warning"), "title": _q(60),
                                   "detail": _q(100), "distance": _q(12, r"[0-9]+(\.[0-9])? ?(mi|km|miles?)")},
                                  ("ref", "level", "title")), 3, 1)}, ("items",)),
    "price": ("Price watch", "New matches against the price range you saved.",
              {"ref": REF, "name": _q(60), "range": _arr(AMOUNT, 2, 2), "new": NUMQ,
               "matches": _arr(_obj({"price": AMOUNT, "note": _q(60)}, ("price",)), 6, 1)},
              ("ref", "name", "matches")),
    "leads": ("Leads digest", "How many, and the top few.",
              {"ref": REF, "count": NUMQ, "what": _q(80),
               "top": _arr(_obj({"title": _q(60), "org": _q(60), "rate": _q(20)}, ("title",)), 3)}, ("ref",)),
    "series": ("Recurring series", "Every session on a mini calendar.",
               {"ref": REF, "name": _q(60), "dates": _arr(_q(20), 12, 1), "time": _q(30)}, ("ref", "name", "dates")),
    "invite": ("Invite card", "Who invited you to what; accepting happens in the app.",
               {"ref": REF, "what": _q(80), "app": _q(60), "expires": _q(30)}, ("ref",)),
    "subs": ("Subscriptions and renewals", "Next charge dates, trials ending, yearly ones.",
             {"days": _int(7, 90), "items": _arr(_obj({"ref": REF, "price": _q(40, MONEY_RE + r"|.*[$€£][0-9].*"),
                                                       "trial": BOOL}, ("ref", "price")), 6, 1)}, ("items",)),
    "composite": ("Composite", "A model-defined block from safe parts (the path for later types).",
                  {"name": _s(32, r"[a-z][a-z0-9_]{1,31}"), "title": _s(60, PLAIN), "ref": REF,
                   "body": {"$ref": "#/$defs/node"}}, ("name", "body")),
}
_ONE_OF = {"clash": (("ref",), ("sides",))}
TYPES = tuple(_BLOCKS)


def schema(t):
    name, desc, props, req = _BLOCKS[t]
    s = _obj(dict({"v": {"const": VERSION}, "type": {"const": t}}, **props), ("type",) + tuple(req))
    s.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"callsheet:visual/{t}/{VERSION}",
              "title": name, "description": desc})
    if t in _ONE_OF:
        s["oneOf"] = [{"required": list(r)} for r in _ONE_OF[t]]
    if t == "composite":
        s["$defs"] = {"node": NODE}
    return s


SCHEMAS = {t: schema(t) for t in TYPES}


def catalogue():
    """GET api/visuals/schema: every type's schema and the limits (also what the brief model is shown)."""
    return {"version": VERSION, "limits": {"blocks": MAX_BLOCKS, "bytes": MAX_BLOCK_BYTES, "nodes": MAX_NODES,
                                           "depth": MAX_DEPTH},
            "refs": {"msg": "a stored message", "item": "a ledger item", "cal": "a calendar event",
                     "clash": "a clash Callsheet found"}, "types": SCHEMAS}


# --- the validator (the subset the schemas use) -----------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


def _check(s, v, path, errs, defs):
    if len(errs) > 20:
        return
    if "$ref" in s:
        s = defs[s["$ref"].rsplit("/", 1)[1]]
    if "oneOf" in s and "type" not in s:     # a node: the discriminator "t" picks the branch
        t = v.get("t") if isinstance(v, dict) else None
        opts = [o for o in s["oneOf"] if o["properties"]["t"]["const"] == t]
        if not opts:
            errs.append(f"{path}: unknown part {t!r}" if t else f"{path}: a part needs \"t\"")
            return
        return _check(opts[0], v, path, errs, defs)
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: must be {s['const']!r}")
        return
    t = s.get("type")
    if t == "object":
        if not isinstance(v, dict):
            errs.append(f"{path}: must be an object")
            return
        for k in v:
            if k not in s["properties"]:
                errs.append(f"{path}: unknown field {k!r}")
        for k in s.get("required", ()):
            if k not in v:
                errs.append(f"{path}: {k!r} is missing")
        for k, sub in s["properties"].items():
            if k in v:
                _check(sub, v[k], f"{path}.{k}", errs, defs)
        if "oneOf" in s and sum(all(k in v for k in o["required"]) for o in s["oneOf"]) != 1:
            errs.append(f"{path}: give exactly one of " + " or ".join(repr(o["required"][0]) for o in s["oneOf"]))
    elif t == "array":
        if not isinstance(v, list):
            errs.append(f"{path}: must be a list")
            return
        if not s.get("minItems", 0) <= len(v) <= s["maxItems"]:
            errs.append(f"{path}: {len(v)} entries (allowed {s.get('minItems', 0)} to {s['maxItems']})")
            return
        for i, x in enumerate(v):
            _check(s["items"], x, f"{path}[{i}]", errs, defs)
    elif t == "string":
        if not isinstance(v, str):
            errs.append(f"{path}: must be text")
        elif not s.get("minLength", 0) <= len(v) <= s.get("maxLength", 1 << 30):
            errs.append(f"{path}: too long or empty")
        elif _CTRL.search(v):
            errs.append(f"{path}: control characters")
        elif "enum" in s and v not in s["enum"]:
            errs.append(f"{path}: not one of {s['enum']}")
        elif "pattern" in s and not re.fullmatch(s["pattern"], v, re.I):
            errs.append(f"{path}: not in the expected form")
    elif t == "integer":
        if not isinstance(v, int) or isinstance(v, bool) or not s["minimum"] <= v <= s["maximum"]:
            errs.append(f"{path}: must be a whole number from {s['minimum']} to {s['maximum']}")
    elif t == "boolean":
        if not isinstance(v, bool):
            errs.append(f"{path}: must be true or false")


def _walk(n, depth=1):
    """(nodes, deepest) of a composite tree."""
    kids = n.get("c", []) if isinstance(n, dict) else []
    sub = [_walk(k, depth + 1) for k in kids]
    return 1 + sum(x[0] for x in sub), max([depth] + [x[1] for x in sub])


def validate(block):
    """(block, None) when it checks out, else (None, {"why": unknown|invalid|oversized, "type", "errors"})."""
    if not isinstance(block, dict):
        return None, {"why": "invalid", "type": None, "errors": ["a block must be an object"]}
    t = block.get("type")
    try:
        size = len(json.dumps(block, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        size = MAX_BLOCK_BYTES + 1
    if size > MAX_BLOCK_BYTES:
        return None, {"why": "oversized", "type": t if isinstance(t, str) else None,
                      "errors": [f"{size} bytes (at most {MAX_BLOCK_BYTES})"]}
    if not isinstance(t, str) or t not in SCHEMAS:
        return None, {"why": "unknown", "type": str(t)[:40] if t is not None else None, "errors": ["unknown type"]}
    if "v" in block and block["v"] != VERSION:
        return None, {"why": "invalid", "type": t, "errors": [f"version {block['v']!r} (this Callsheet reads {VERSION})"]}
    errs = []
    _check(SCHEMAS[t], block, t, errs, {"node": NODE})
    if not errs and t == "composite":
        n, d = _walk(block["body"])
        if n > MAX_NODES or d > MAX_DEPTH:
            errs.append(f"composite: {n} parts, {d} deep (at most {MAX_NODES} and {MAX_DEPTH})")
        errs += _axis_rules(block["body"], False)
    if errs:
        return None, {"why": "invalid", "type": t, "errors": errs[:8]}
    return dict(block, v=VERSION), None


def _axis_rules(n, in_axis):
    """range_bar and dot only inside a time_axis; a time_axis runs forward."""
    out = []
    if n["t"] in ("range_bar", "dot") and not in_axis:
        out.append(f"composite: {n['t']} must sit in a time_axis")
    if n["t"] == "time_axis" and n["to"] <= n["from"]:
        out.append("composite: time_axis must run forward")
    for k in n.get("c", []):
        out += _axis_rules(k, in_axis or n["t"] == "time_axis")
    return out


# --- quotes: a fact the ledger doesn't hold must be found in the referenced message ------------------------------------
_NORM = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "−": "-", " ": " ", " ": " "})


def quote(needle, hay):
    """The store's own text for `needle` (case and spacing ignored), or None. A number must match whole."""
    n = re.sub(r"\s+", " ", (needle or "").translate(_NORM)).strip()
    if not n or not hay:
        return None
    pat = r"\s+".join(re.escape(w) for w in n.split(" "))
    if n[0].isalnum():
        pat = r"(?<![0-9A-Za-z])" + pat
    if n[-1].isalnum():
        pat += r"(?![0-9A-Za-z]|[.,][0-9])"
    m = re.search(pat, hay.translate(_NORM), re.I)
    return hay[m.start():m.end()] if m else None


def money(s):
    """("$", Decimal, has_cents) of the first amount in s, or None."""
    m = re.search(r"([$€£])\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", s or "")
    if not m:
        return None
    try:
        return m.group(1), Decimal(m.group(2).replace(",", "")), "." in m.group(2)
    except InvalidOperation:
        return None


def fmt_money(sym, v, cents, sign=False):
    s = f"{abs(v):,.2f}" if cents else f"{abs(v):,.0f}"
    return ("+" if v >= 0 else "−") * sign + ("" if sign or v >= 0 else "−") + sym + s


def total(amounts):
    """Sum of quoted amounts in one currency (None when mixed or empty)."""
    ms = [m for m in (money(a) for a in amounts) if m]
    if not ms or len({m[0] for m in ms}) > 1:
        return None
    return fmt_money(ms[0][0], sum(m[1] for m in ms), any(m[2] for m in ms))


def _digits(s):
    return re.sub(r"[^0-9]", "", str(s or "")).lstrip("0")


def number(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


# --- time words (12-hour everywhere) ----------------------------------------------------------------------------------
def clock(d, short=False):
    h = d.hour % 12 or 12
    ap = "am" if d.hour < 12 else "pm"
    return f"{h} {ap}" if short and not d.minute else f"{h}:{d.minute:02d} {ap}"


def span_words(a, b):
    """"8:30 to 9:30 am", "11:30 am to 1:00 pm"."""
    if b is None:
        return clock(a)
    if (a.hour < 12) == (b.hour < 12):
        return f"{clock(a)[:-3]} to {clock(b)}"
    return f"{clock(a)} to {clock(b)}"


def day_words(d):
    return f"{d:%a} {d:%b} {d.day}"


def short_date(d):
    return f"{d:%b} {d.day}"


def rel_days(n):
    if n < 0:
        return f"{-n} day{'s' * (n != -1)} ago"
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n <= 13:
        return f"in {n} days"
    if n < 45:
        return f"in {round(n / 7)} weeks"
    return f"in {round(n / 30)} months"


def until_words(secs):
    if secs < 3600:
        return f"in {max(1, int(secs // 60))} min"
    if secs < 48 * 3600:
        return f"in {int(secs // 3600)} h"
    return f"in {int(secs // 86400)} days"


def age_words(secs):
    secs = max(0, secs)
    if secs < 3600:
        return f"{max(1, int(secs // 60))} min"
    if secs < 48 * 3600:
        return f"{int(secs // 3600)} h"
    return f"{int(secs // 86400)} days"


def gap_words(mins):
    h, m = divmod(int(round(mins)), 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                           "dec"))}


def parse_day(s, anchor):
    """"Nov 30", "Dec 2", "Mon Nov 30", "11/30": the date nearest after the anchor (a message's date), or None."""
    s = (s or "").lower()
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+([0-9]{1,2})\b", s)
    if m:
        mo, d = _MONTHS[m.group(1)], int(m.group(2))
    else:
        m = re.search(r"\b([0-9]{1,2})/([0-9]{1,2})\b", s)
        if not m:
            return None
        mo, d = int(m.group(1)), int(m.group(2))
    for y in (anchor.year, anchor.year + 1):
        try:
            c = dt.date(y, mo, d)
        except ValueError:
            return None
        if c >= anchor - dt.timedelta(days=60):
            return c
    return None


# --- the facts: what fill() reads -------------------------------------------------------------------------------------
class Facts:
    """The store as fill() sees it. Records are dicts: ref, kind (item|msg|cal), type (ask|commitment|event|cleanup|
    calendar|message), subtype, title, who, purpose, source (mail|signal|slack|text|cal|news), start/end (epoch or
    None), all_day, created (epoch), status, on_calendar, link, msg_ts, sender, hay (text quotes are checked
    against; never shown as a whole). Subclasses: StoreFacts (messages.db) and DictFacts (samples, tests)."""
    tz = None
    now = 0.0

    def resolve(self, ref):
        raise NotImplementedError

    def conflict(self, n):
        return None

    def asks(self):
        return []

    def upcoming(self, days):
        return []

    def cleanup(self):
        return []

    def on_day(self, day):
        return []


def _active(r):
    return r.get("status", "open") not in ("declined", "done", "cancelled")


class DictFacts(Facts):
    """Facts from plain records (the gallery's sample store and the tests' fakes)."""

    def __init__(self, tz, now, records, conflicts=None):
        self.tz, self.now = tz, now
        self.records = {r["ref"]: r for r in records}
        self.conflicts = conflicts or {}

    def resolve(self, ref):
        return self.records.get(ref)

    def conflict(self, n):
        c = self.conflicts.get(n)
        if not c:
            return None
        sides = [self.resolve(r) for r in c["sides"]]
        return dict(c, sides=sides) if all(sides) else None

    def asks(self):
        return sorted((r for r in self.records.values() if r.get("type") == "ask" and _active(r)),
                      key=lambda r: r.get("created") or 0)

    def upcoming(self, days):
        end = self.now + days * 86400
        return sorted((r for r in self.records.values()
                       if r.get("type") in ("event", "commitment", "calendar") and _active(r)
                       and r.get("start") is not None and self.now - 3600 < r["start"] < end),
                      key=lambda r: r["start"])

    def cleanup(self):
        return [r for r in self.records.values() if r.get("type") == "cleanup" and _active(r)]

    def on_day(self, day):
        return [r for r in self.records.values() if r.get("start") is not None and _active(r)
                and r.get("type") in ("event", "commitment", "calendar", "ask")
                and dt.datetime.fromtimestamp(r["start"], self.tz).date() == day]


class StoreFacts(Facts):
    """Facts from the message store and the ledger (messages.db). Read-only."""

    def __init__(self, ms, ledger, now=None):
        self.ms, self.ledger = ms, ledger
        self.tz = ledger.tz()
        self.now = ledger.clock() if now is None else now

    def _read(self, sql, args=()):
        return [dict(r) for r in self.ms._read(sql, args)]  # noqa: SLF001

    def _msg(self, mid):
        r = self._read("SELECT * FROM messages WHERE id=?", (mid,))
        return r[0] if r else None

    def _item_rec(self, it, msg=None):
        if msg is None:
            src = self._read("SELECT m.* FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE s.item=? "
                             "ORDER BY m.ts LIMIT 1", (it["id"],))
            msg = src[0] if src else None
        start, end = it.get("start_ts"), it.get("end_ts")
        if start is None and it.get("start_date"):
            a = dt.date.fromisoformat(it["start_date"])
            start = dt.datetime(a.year, a.month, a.day, tzinfo=self.tz).timestamp()
            b = dt.date.fromisoformat(it.get("end_date") or it["start_date"]) + dt.timedelta(days=1)
            end = dt.datetime(b.year, b.month, b.day, tzinfo=self.tz).timestamp()
        hay = " \n".join(x for x in (it.get("what"), it.get("purpose"), it.get("phrase"), it.get("who_name"),
                                     msg and msg["subject"], msg and msg["text"]) if x)
        return {"ref": f"item:{it['id']}", "kind": "item", "type": it["type"], "subtype": it.get("subtype") or "",
                "title": it.get("what") or (msg and msg["subject"]) or "", "who": it.get("who_name") or it.get("who") or
                (msg and (msg["sender_name"] or msg["sender"])) or "", "purpose": it.get("purpose") or "",
                "source": SRC.get(msg["source"], "mail") if msg else "mail", "start": start, "end": end,
                "all_day": bool(it.get("all_day")), "created": it.get("created_at") or (msg and msg["ts"]),
                "status": it.get("status") or "open", "on_calendar": it.get("cal_ref") is not None,
                "link": msg["link"] if msg else "", "msg_ts": msg["ts"] if msg else None,
                "sender": (msg["sender_name"] or msg["sender"]) if msg else "", "hay": hay,
                "text": msg["text"] if msg else "", "subject": msg["subject"] if msg else "",
                "direction": msg["direction"] if msg else "in",
                "amount": it.get("amount") or "", "currency": it.get("currency") or "", "stage": it.get("stage") or ""}

    def _msg_rec(self, m):
        its = self._read("SELECT i.* FROM ledger_sources s JOIN ledger_items i ON i.id=s.item WHERE s.message=? "
                         "AND i.merged_into IS NULL ORDER BY s.role='created' DESC, i.id LIMIT 1", (m["id"],))
        if its:
            r = self._item_rec(its[0], m)
        else:
            r = {"ref": "", "kind": "msg", "type": "message", "subtype": "", "title": m["subject"] or "",
                 "who": m["sender_name"] or m["sender"], "purpose": "", "source": SRC.get(m["source"], "mail"),
                 "start": None, "end": None, "all_day": False, "created": m["ts"], "status": "open",
                 "on_calendar": False, "link": m["link"], "msg_ts": m["ts"], "sender": m["sender_name"] or m["sender"],
                 "hay": " \n".join(x for x in (m["subject"], m["text"]) if x), "text": m["text"],
                 "subject": m["subject"], "direction": m["direction"]}
        r.update({"ref": f"msg:{m['id']}", "kind": "msg", "msg_ts": m["ts"], "link": m["link"],
                  "sender": m["sender_name"] or m["sender"], "text": m["text"], "subject": m["subject"],
                  "source": SRC.get(m["source"], "mail"), "direction": m["direction"]})
        return r

    def _cal_rec(self, e):
        return {"ref": f"cal:{e['id']}", "kind": "cal", "type": "calendar", "subtype": "", "title": e["title"],
                "who": "", "purpose": e.get("location") or "", "source": "cal", "start": e["start_ts"],
                "end": e["end_ts"], "all_day": bool(e["all_day"]), "created": e["start_ts"],
                "status": "cancelled" if e.get("status") == "cancelled" else "open", "on_calendar": True,
                "link": e.get("link") or "", "msg_ts": None, "sender": "", "hay": " ".join((e["title"],
                                                                                            e.get("location") or "")),
                "text": "", "subject": "", "direction": "in"}

    def resolve(self, ref):
        kind, _, n = ref.partition(":")
        n = int(n)
        if kind == "item":
            r = self._read("SELECT * FROM ledger_items WHERE id=?", (n,))
            if r and r[0].get("merged_into"):
                r = self._read("SELECT * FROM ledger_items WHERE id=?", (r[0]["merged_into"],))
            return self._item_rec(r[0]) if r else None
        if kind == "msg":
            m = self._msg(n)
            return self._msg_rec(m) if m and not m.get("deleted_at") else None
        if kind == "cal":
            r = self._read("SELECT * FROM events WHERE id=?", (n,))
            return self._cal_rec(r[0]) if r else None
        return None

    def conflict(self, n):
        r = self._read("SELECT * FROM ledger_conflicts WHERE id=?", (n,))
        if not r:
            return None
        c = r[0]
        sides = [self.resolve(("item:" if k == "item" else "cal:") + str(ref))
                 for k, ref in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"]))]
        if not all(sides):
            return None
        return {"slot_start": c["slot_start"], "slot_end": c["slot_end"], "state": c["state"], "sides": sides,
                "verdict": c["verdict"] if "verdict" in c.keys() else "clash"}

    def _items(self, where, args=()):
        return [self._item_rec(it) for it in self._read(
            "SELECT * FROM ledger_items WHERE merged_into IS NULL AND status NOT IN ('declined','done','cancelled') "
            f"AND {where} ORDER BY created_at", args)]

    def asks(self):
        return [r for r in self._items("type='ask' AND status != 'waiting_on_them'")]

    def upcoming(self, days):
        end = self.now + days * 86400
        out = [r for r in self._items("type IN ('event','commitment')")
               if r["start"] is not None and self.now - 3600 < r["start"] < end]
        cal = {r["ref"] for r in out}
        for e in self.ms.events(since=self.now, until=end):
            if f"cal:{e['id']}" not in cal:
                out.append(self._cal_rec(e))
        return sorted(out, key=lambda r: r["start"])

    def cleanup(self):
        return self._items("type='cleanup'")

    def on_day(self, day):
        a = dt.datetime(day.year, day.month, day.day, tzinfo=self.tz).timestamp()
        out = [r for r in self._items("type IN ('event','commitment','ask')")
               if r["start"] is not None and a <= r["start"] < a + 86400]
        seen = {r.get("on_calendar") and r["title"] for r in out}
        out += [self._cal_rec(e) for e in self.ms.events(since=a, until=a + 86400) if e["title"] not in seen]
        return out


# --- fill: one validated block plus the facts -> what visuals.js draws --------------------------------------------------
class Unfilled(Exception):
    """The block's refs aren't in the store (or say nothing): the block is dropped, the words still play."""


class _Ctx:
    def __init__(self, facts):
        self.f, self.tz, self.now = facts, facts.tz, facts.now
        self.checks = []

    def rec(self, ref, need=True):
        r = self.f.resolve(ref) if ref else None
        if r is None and ref:
            self.checks.append({"ref": ref, "why": "not in the store"})
        if r is None and need:
            raise Unfilled(f"{ref} is not in the store")
        return r

    def q(self, rec, field, val):
        if val is None or rec is None:
            return None
        got = quote(val, rec.get("hay", ""))
        if got is None:
            self.checks.append({"ref": rec["ref"], "field": field, "why": "not found in the message"})
        return got

    def amount(self, rec, val, field="amount"):
        """0.20.0: the ledger's amount (copied at extraction from the sentence the model cited, by the 0.12.2 rules)
        when the item holds one; the block's own amount is then only compared, never shown. Without one (a plain
        message, an item read before 0.20.0, a sentence with two amounts) the block's amount is a quote, checked
        against the message as before."""
        if rec is not None and rec.get("amount"):
            if val is not None and _digits(val) != _digits(rec["amount"]):
                self.checks.append({"ref": rec["ref"], "field": field, "why": "the ledger's amount is shown"})
            return rec["amount"]
        return self.q(rec, field, val)

    def stage(self, rec, val):
        """0.20.0: a package step's stage from the ledger (out_for_delivery is drawn as on the way), else the block's."""
        got = _LEDGER_STAGE.get((rec or {}).get("stage") or "")
        if got:
            if val and val != got:
                self.checks.append({"ref": rec["ref"], "field": "stage", "why": "the ledger's stage is shown"})
            return got
        if not val and rec is not None:
            self.checks.append({"ref": rec["ref"], "field": "stage", "why": "no stage in the ledger"})
        return val

    def local(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz)

    def today(self):
        return self.local(self.now).date()

    def days_to(self, ts):
        return (self.local(ts).date() - self.today()).days

    def hours(self, ts, base):
        d = self.local(ts)
        return (d.date() - base).days * 24 + d.hour + d.minute / 60


def _hint_hour(s):
    m = re.fullmatch(HOUR_RE, s.strip(), re.I)
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return h + (int(m.group(2)[1:]) / 60 if m.group(2) else 0)


def _ticks(a, b):
    step = max(1, round((b - a) / 4))
    out, h = [], a + step
    while h < b:
        out.append([h, clock(dt.time(int(h) % 24), short=True)])
        h += step
    return out


def _src_word(r):
    return {"mail": "Email", "signal": "Signal", "slack": "Slack", "text": "Text", "cal": "Calendar",
            "news": "Feed"}.get(r["source"], "Message")


def _day(c, b, items, date_word):
    base = c.today() + dt.timedelta(days=1 if b.get("date") == "tomorrow" else 0)
    recs = [c.rec(r, need=False) for r in b.get("items", [])] if b.get("items") else c.f.on_day(base)
    recs = sorted((r for r in recs if r and r.get("start") is not None and not r.get("all_day")
                   and c.local(r["start"]).date() == base), key=lambda r: r["start"])
    if not recs:
        raise Unfilled("nothing timed on that day")
    lanes, spans = [], []
    for r in recs:
        s = c.hours(r["start"], base)
        e = c.hours(r["end"], base) if r.get("end") else s + 1
        e = max(e, s + .25)
        spans.append((s, e))
        lanes.append({"l": r["title"] or r["who"], "s": round(s, 3), "e": round(e, 3),
                      "k": "cal" if r.get("on_calendar") else "msg",
                      "sub": span_words(c.local(r["start"]), c.local(r["end"]) if r.get("end") else None)})
    for i, ln in enumerate(lanes):           # a lane that overlaps another and is only in a message: a clash
        clash = any(j != i and spans[j][0] < spans[i][1] and spans[i][0] < spans[j][1] for j in range(len(spans)))
        if ln["k"] == "msg":
            ln["sub"] = f"From {FROM[recs[i]['source']]}" + (" · clash" if clash else "")
            ln["al"] = clash
    a = math.floor(min(s for s, _ in spans)) - 1
    z = math.ceil(max(e for _, e in spans)) + 1
    if b.get("from"):
        a = min(a, math.floor(_hint_hour(b["from"])))
    if b.get("to"):
        z = max(z, math.ceil(_hint_hour(b["to"])))
    a, z = max(0, a), min(24 + 6, max(z, a + 6))
    if b.get("free", True):                  # free gaps of two hours or more, at most two
        gaps, end = [], spans[0][1]
        for i in range(1, len(spans)):
            if spans[i][0] - end >= 2:
                gaps.append((i, end, spans[i][0]))
            end = max(end, spans[i][1])
        for i, s, e in sorted(sorted(gaps, key=lambda g: g[1] - g[2])[:2], key=lambda g: -g[0]):
            until = dt.datetime.combine(base, dt.time()) + dt.timedelta(hours=e)
            lanes.insert(i, {"l": "Free", "sub": f"until {clock(until)}", "s": round(s, 3), "e": round(e, 3),
                             "gap": gap_words((e - s) * 60)})
    now_h = c.hours(c.now, base) if base == c.today() else None
    d = {"date": f"{date_word(base)} · {day_words(base)}", "a": a, "b": z, "ticks": _ticks(a, z), "lanes": lanes,
         "now": round(now_h, 3) if now_h is not None and a <= now_h <= z else None}
    n_free = sum(1 for ln in lanes if "gap" in ln)
    alt = f"{d['date']}: " + "; ".join(f"{ln['l']}, {ln['sub']}" for ln in lanes if "gap" not in ln)
    if n_free:
        alt += "; " + "; ".join(f"free {ln['gap']} {ln['sub']}" for ln in lanes if "gap" in ln)
    return d, alt + "."


def _date_word(c):
    def w(d):
        n = (d - c.today()).days
        return "Today" if n == 0 else "Tomorrow" if n == 1 else f"{d:%A}"
    return w


def _when_word(c, ts):
    d = c.local(ts)
    n = c.days_to(ts)
    lead = ("Tonight" if d.hour >= 17 else "Today") if n == 0 else "Tomorrow" if n == 1 else \
        f"{d:%a}" if 1 < n < 7 else day_words(d)
    return f"{lead} {clock(d)}"


_OWNER_WHO = ("owner", "the owner", "me", "you")


def _clash(c, b):
    if b.get("ref"):
        kind, _, n = b["ref"].partition(":")
        cf = c.f.conflict(int(n)) if kind == "clash" else None
        if not cf:
            c.checks.append({"ref": b["ref"], "why": "not in the store"})
            raise Unfilled("no such clash")
        sides = cf["sides"]
    else:
        sides, cf = [c.rec(r) for r in b["sides"]], None
    sides = [s for s in sides if s.get("start") is not None]
    if len(sides) != 2:
        raise Unfilled("a clash needs two timed sides")
    base = c.local(min(s["start"] for s in sides)).date()
    sp = [(c.hours(s["start"], base), c.hours(s["end"], base) if s.get("end") else c.hours(s["start"], base) + 1)
          for s in sides]
    ov = [max(sp[0][0], sp[1][0]), min(sp[0][1], sp[1][1])]
    start = min(s["start"] for s in sides)
    a, z = math.floor(min(x for x, _ in sp)) - 1, math.ceil(max(y for _, y in sp)) + 1
    left = start - c.now
    # 0.23.0: a side that is the owner's own (his yes, his plan) is named by what it is, never "the owner"
    mine = [s["kind"] == "cal" or not s["who"] or str(s["who"]).strip().lower() in _OWNER_WHO for s in sides]
    lanes = [{"l": s["title"] if own else s["who"],
              "sub": " · ".join(x for x in (_src_word(s), s.get("purpose") or ("" if own else s["title"])) if x),
              "s": round(x, 3), "e": round(y, 3), "k": "cal" if s.get("on_calendar") or s["kind"] == "cal" else "msg"}
             for s, own, (x, y) in zip(sides, mine, sp)]
    d = {"lv": "al" if left < 86400 else "wn", "when": _when_word(c, start), "until": until_words(max(0, left)),
         "a": a, "b": z, "ticks": _ticks(a, z), "ov": [round(ov[0], 3), round(ov[1], 3)] if ov[1] > ov[0] else None,
         "lanes": lanes}
    if (cf or {}).get("verdict") == "maybe":  # 0.23.0: a Maybe clash is quiet: △, dim, never the alert red
        d["lv"] = "mb"
    alt = f"{'Maybe clash' if d['lv'] == 'mb' else 'Clash'} {d['when']}, {d['until']}: " + " and ".join(f"{ln['l']} ({ln['sub']})" for ln in lanes) + "."
    return d, alt


def _asks(c, b):
    recs = [c.rec(r, need=False) for r in b["items"]] if b.get("items") else c.f.asks()
    recs = [r for r in recs if r and _active(r)]   # 0.21.0: an ask marked done leaves the stack (the brief's picture updates)
    if not recs:
        raise Unfilled("no asks")
    recs.sort(key=lambda r: r.get("created") or 0, reverse=b.get("order") == "newest")
    n = b.get("max", 6)
    more = max(0, len(recs) - n)
    recs = recs[:n]
    ages = [max(60, c.now - (r.get("created") or c.now)) for r in recs]
    top = max(ages)
    rows = [{"src": r["source"], "who": r["who"] or r["title"], "age": age_words(a),
             "p": max(3, round(a / top * 100)), "why": r.get("purpose") or r["title"]} for r, a in zip(recs, ages)]
    alt = f"{len(rows)} waiting on you, oldest first: " + "; ".join(f"{x['who']}, {x['age']}" for x in rows) + "."
    return {"rows": rows, "more": more}, alt


def _upcoming(c, b):
    days = b.get("days", 30)
    recs = [c.rec(r, need=False) for r in b["items"]] if b.get("items") else c.f.upcoming(days)
    recs = sorted((r for r in recs if r and r.get("start") is not None and 0 <= c.days_to(r["start"]) < days),
                  key=lambda r: r["start"])
    if not recs:
        raise Unfilled("nothing coming up")
    today = c.today()
    cells = ["td"] + [""] * (days - 1)
    for r in recs:
        i = c.days_to(r["start"])
        if r.get("end") and r["end"] - r["start"] > 86400:
            for j in range(i + 1, min(days, c.days_to(r["end"] - 1) + 1)):
                if not cells[j]:
                    cells[j] = "tr"
        if i:
            cells[i] = "ev"
    weeks, i = [], 0
    while i < days:
        d = today + dt.timedelta(days=i)
        n = min(days - i, 7 - d.weekday())
        weeks.append([i + 1, n, "Today" if i == 0 else short_date(d)])
        i += n
    sun = today + dt.timedelta(days=6 - today.weekday())
    last = today + dt.timedelta(days=days - 1)
    groups = []
    for r in recs:
        d = c.local(r["start"]).date()
        g = 0 if d <= sun else 1 if d <= sun + dt.timedelta(days=7) else 2
        name = ("This week", "Next week", "Later")[g]
        rng = (f"to {day_words(sun)}", f"{short_date(sun + dt.timedelta(days=1))} to {(sun + dt.timedelta(days=7)).day}",
               f"to {short_date(last)}")[g]
        if not groups or groups[-1]["h"] != name:
            groups.append({"h": name, "r": rng, "rows": []})
        what = r["title"] or r["who"]
        if not r.get("all_day"):
            what += f" · {clock(c.local(r['start']))}"
        groups[-1]["rows"].append({"rel": rel_days((d - today).days), "d": day_words(d), "w": what,
                                   "src": r["source"], "from": None if r["source"] == "cal" or r.get("on_calendar")
                                   else f"From {FROM[r['source']]}"})
    shown, out = 0, []
    for g in groups:
        rows = g["rows"][:max(0, 5 - shown)]
        shown += len(rows)
        if rows:
            out.append(dict(g, rows=rows))
    d = {"days": days, "cells": cells, "weeks": weeks, "groups": out, "count": len(recs), "more": len(recs) - shown,
         "last": short_date(last)}
    first = out[0]["rows"][0]
    alt = f"{len(recs)} things in the next {days} days, the nearest {first['rel']}: " + "; ".join(
        f"{x['w']}, {x['rel']}" for g in out for x in g["rows"]) + "."
    return d, alt


def _due(c, r):
    if r.get("start") is None:
        return "", None
    n = c.days_to(r["start"])
    return f"Due {day_words(c.local(r['start']))} · {rel_days(n)}", n


def _money(c, b):
    rows, amts = [], []
    for x in b["bills"]:
        r = c.rec(x["ref"])
        amt = c.amount(r, x.get("amount"))
        due, _ = _due(c, r)
        rows.append({"n": r["who"] or r["title"], "due": due, "amt": amt})
        if amt:
            amts.append(amt)
    tot = total(amts) if len(amts) > 1 and len(amts) == len(rows) else None
    alt = "Bills: " + "; ".join(f"{x['n']}{', ' + x['amt'] if x['amt'] else ''}{', ' + x['due'] if x['due'] else ''}"
                                for x in rows) + (f". Total {tot}." if tot else ".")
    return {"rows": rows, "total": tot}, alt


def _trip(c, b):
    legs = sorted((c.rec(r) for r in b["legs"]), key=lambda r: r.get("start") or 0)
    to = None
    for r in legs:
        to = to or c.q(r, "to", b["to"])
    if not to or legs[0].get("start") is None:
        raise Unfilled("the trip's place or time isn't in the store")
    rows = []
    for k, r in zip(("Out", "Back"), legs):
        if r.get("start") is None:
            continue
        a = c.local(r["start"])
        t = f"{day_words(a)} · {clock(a)}"
        if r.get("end") and r["end"] - r["start"] < 86400:
            t += f" · {'lands' if k == 'Out' else 'home'} {clock(c.local(r['end']))}"
        rows.append({"k": k, "t": t})
    nights = None
    if len(legs) > 1 and legs[-1].get("start") is not None:
        n = (c.local(legs[-1]["start"]).date() - c.local(legs[0]["start"]).date()).days
        nights = f"{n} night{'s' * (n != 1)}" if n > 0 else None
    src = legs[0]["source"]
    note = f"From {FROM[src]} · " + ("on your calendar" if all(r.get("on_calendar") for r in legs)
                                     else "not on your calendar yet")
    alt = f"Trip to {to}" + (f", {nights}" if nights else "") + ": " + "; ".join(f"{x['k']} {x['t']}" for x in rows) + "."
    return {"to": to, "nights": nights, "rows": rows, "note": note}, alt


def _person(c, b):
    r = c.rec(b["ref"])
    text = r.get("text") or ""
    if not text or r.get("subtype") == "health":
        raise Unfilled("no message text to show")
    mark = c.q({"ref": r["ref"], "hay": text}, "mark", b.get("mark"))
    i = text.find(mark) if mark else 0
    a = max(0, i - 120) if mark else 0
    z = min(len(text), max(a + 280, i + len(mark or "") + 60))
    ex = ("…" if a else "") + text[a:z].strip() + ("…" if z < len(text) else "")
    parts = [[ex, False]]
    if mark and mark in ex:
        k = ex.index(mark)
        parts = [[ex[:k], False], [mark, True], [ex[k + len(mark):], False]]
    who = r["sender"] or r["who"]
    ts = r.get("msg_ts") or r.get("created") or c.now
    d = {"name": who, "initial": (who[:1] or "?").upper(), "sub": _src_word(r), "age": age_words(c.now - ts),
         "meta": f"{who.split(' ')[0]} · {f'{c.local(ts):%a}'} {clock(c.local(ts))}",
         "parts": [p for p in parts if p[0]], "ask": r.get("type") == "ask", "mine": r.get("direction") == "out"}
    alt = f"{who}, {d['sub']}, {d['age']} ago: “{ex}”" + (f" The ask: {mark}." if mark else "")
    return d, alt


def _cleanup(c, b):
    if b.get("groups"):   # 0.24.0 (B1): the plan's groups (Today's Clean up): the same count as the words
        groups = [{"label": g["label"], "n": int(g["n"])} for g in b["groups"] if int(g["n"]) > 0]
        tot = sum(g["n"] for g in groups)
        if not tot:
            raise Unfilled("nothing to clean up")
        return {"total": tot, "groups": groups}, f"{tot} to clean up: " + ", ".join(
            f"{g['label']} {g['n']}" for g in groups) + "."
    recs = c.f.cleanup()
    counts = {}
    for r in recs:
        k = r.get("subtype") if r.get("subtype") in CLEAN_KINDS else "other"
        counts[k] = counts.get(k, 0) + 1
    if b.get("kinds"):
        counts = {k: v for k, v in counts.items() if k in b["kinds"]}
    if not counts:
        raise Unfilled("nothing to clean up")
    order = sorted(counts.items(), key=lambda kv: (kv[0] == "other", -kv[1]))
    if len(order) > 3:
        order = order[:2] + [("other", sum(v for _, v in order[2:]))]
    groups = [{"label": CLEAN_KINDS[k], "n": v} for k, v in order]
    tot = sum(counts.values())
    return {"total": tot, "groups": groups}, f"{tot} to clean up: " + ", ".join(
        f"{g['label']} {g['n']}" for g in groups) + "."


def _bills(c, b):
    days = b.get("days", 30)
    rows, amts = [], []
    for x in b["items"]:
        r = c.rec(x["ref"])
        if r.get("start") is None:
            c.checks.append({"ref": r["ref"], "why": "no due date in the store"})
            continue
        n = c.days_to(r["start"])
        amt = c.q(r, "amount", x.get("amount")) if x.get("min") else c.amount(r, x.get("amount"))   # a minimum: quoted
        bal = c.q(r, "balance", x.get("balance"))
        card = c.q(r, "card", x.get("card"))
        rows.append({"n": r["who"] or r["title"], "s": f"Card ··{card}" if card else (r.get("purpose") or "Bill"),
                     "day": max(0, min(days, n)), "dt": short_date(c.local(r["start"])),
                     "soon": rel_days(n) if n <= 3 else None,
                     "a": (amt + " min") if amt and x.get("min") and "min" not in amt else amt,
                     "b": f"of {bal}" if bal else "bill"})
        if amt and 0 <= n <= days:
            amts.append(amt)
    if not rows:
        raise Unfilled("no bills with dates")
    rows.sort(key=lambda x: x["day"])
    today = c.today()
    axis = [[0, "Today"], [days // 2, short_date(today + dt.timedelta(days=days // 2))],
            [days, short_date(today + dt.timedelta(days=days))]]
    tot = total(amts)
    d = {"days": days, "axis": axis, "rows": rows, "total": tot, "total_label": f"Due in {days} days"}
    alt = f"{len(rows)} bills in the next {days} days: " + "; ".join(
        f"{x['n']}, {x['a'] or 'amount not found'}, due {x['dt']}" for x in rows) + (f". Total {tot}." if tot else ".")
    return d, alt


def _ledger(c, b):
    rows, ins, outs = [], [], []
    for x in b["items"]:
        r = c.rec(x["ref"])
        amt = c.amount(r, x.get("amount"))
        memo = c.q(r, "memo", x.get("memo"))
        ts = r.get("msg_ts") or r.get("created") or c.now
        m = money(amt) if amt else None
        if m:
            (ins if x["dir"] == "in" else outs).append(m)
        who = r["who"] or r["sender"] or r["title"]
        if r.get("sender") and r["sender"] != who:
            who += f" · {r['sender']}"
        rows.append({"io": x["dir"], "w": who,
                     "sub": " · ".join(y for y in (f"“{memo}”" if memo else "", day_words(c.local(ts))) if y),
                     "amt": fmt_money(m[0], m[1] if x["dir"] == "in" else -m[1], m[2], sign=True) if m else None})
    all_m = ins + outs
    net = split = None
    if all_m and len({m[0] for m in all_m}) == 1:
        sym, cents = all_m[0][0], any(m[2] for m in all_m)
        si, so = sum((m[1] for m in ins), Decimal(0)), sum((m[1] for m in outs), Decimal(0))
        net = fmt_money(sym, si - so, cents, sign=True)
        split = f"In {fmt_money(sym, si, cents)} · out {fmt_money(sym, so, cents)}"
    rng = b.get("range", "this week")
    d = {"rows": rows, "net": net, "net_label": f"Net {rng}", "split": split}
    alt = "Money in and out: " + "; ".join(f"{'in' if x['io'] == 'in' else 'out'} {x['amt'] or ''} {x['w']}"
                                           for x in rows) + (f". Net {rng} {net}." if net else ".")
    return d, alt


_STAGES = ("ordered", "shipped", "on_the_way", "delivered")
_LEDGER_STAGE = {"ordered": "ordered", "shipped": "shipped", "out_for_delivery": "on_the_way", "delivered": "delivered"}
_STAGE_WORDS = ("Ordered", "Shipped", "On the way", "Delivered")


def _package(c, b):
    got = {}
    name = None
    for x in b["steps"]:
        r = c.rec(x["ref"])
        st = c.stage(r, x.get("stage"))
        if st:
            got[st] = r
        name = name or c.q(r, "name", b["name"])
    if not got:
        raise Unfilled("no stage for the package in the ledger")
    if not name:
        raise Unfilled("the package's name isn't in its messages")
    first = c.rec(b["steps"][0]["ref"])
    arr = c.rec(b["arrives"], need=False) if b.get("arrives") else None
    track = None
    for r in got.values():
        track = track or (quote(b["tracking"], r.get("hay", "")) if b.get("tracking") else None)
    if b.get("tracking") and not track:
        c.checks.append({"field": "tracking", "why": "not found in the message"})
    cur = max(_STAGES.index(s) for s in got)
    steps = []
    for i, (s, w) in enumerate(zip(_STAGES, _STAGE_WORDS)):
        r = got.get(s)
        if r is not None:
            ts = r.get("msg_ts") or r.get("created")
            d = "Today" if c.days_to(ts) == 0 else short_date(c.local(ts))
        elif s == "on_the_way" and i == cur + 1 and got.get("shipped") is not None and \
                (arr is None or arr.get("start") is None or c.days_to(arr["start"]) > 0):
            d, cur = "Today", i
        elif s == "delivered" and arr and arr.get("start") is not None:
            d = day_words(c.local(arr["start"]))
        else:
            d = ""
        steps.append({"t": w, "d": d, "st": "done" if i < cur else "now" if i == cur else "next"})
    if cur == 3:
        steps[3]["st"] = "done"
    arrives = None
    if arr and arr.get("start") is not None and cur < 3:
        arrives = f"Arrives {day_words(c.local(arr['start']))}"
        if arr.get("end") and not arr.get("all_day"):
            arrives += f" · by {clock(c.local(arr['end']))}"
    sub = first["sender"] or first["who"]
    if track:
        sub += f" · tracking ··{re.sub(r'[^0-9A-Za-z]', '', track)[-4:]}"
    d = {"name": name, "sub": sub, "steps": steps, "arrives": arrives}
    alt = f"{name}: {_STAGE_WORDS[cur].lower()}." + (f" {arrives}." if arrives else "")
    return d, alt


def _ticket(c, b):
    r = c.rec(b["ref"])
    if r.get("start") is None:
        raise Unfilled("the event has no date in the store")
    a = c.local(r["start"])
    place = c.q(r, "place", b.get("place"))
    st, sw = None, None
    if b.get("state") == "registered":
        st, sw = "ok", "Registered"
    rb = c.rec(b["register_by"], need=False) if b.get("register_by") else None
    if b.get("state") == "register_by" or rb:
        if rb and rb.get("start") is not None:
            st, sw = "wn", f"Register by {day_words(c.local(rb['start']))}"
        elif b.get("state") == "register_by":
            st, sw = "wn", "Registration needed"
    d = {"dw": f"{a:%a}", "d": str(a.day), "m": f"{a:%b}", "t": r["title"],
         "w": "All day" if r.get("all_day") else span_words(a, c.local(r["end"]) if r.get("end") else None),
         "p": place, "st": st, "sw": sw}
    alt = f"{r['title']}, {day_words(a)}, {d['w']}" + (f", {place}" if place else "") + (f". {sw}." if sw else ".")
    return d, alt


def _tasks(c, b):
    rows = []
    for ref in b["items"]:
        r = c.rec(ref, need=False)
        if not r:
            continue
        n = c.days_to(r["start"]) if r.get("start") is not None else None
        rows.append({"t": r["title"], "s": " · ".join(x for x in (r["who"], r.get("purpose")) if x),
                     "dl": f"by {day_words(c.local(r['start']))}" if n is not None else "",
                     "rl": rel_days(n) if n is not None else "", "wn": n is not None and n <= 7,
                     "_n": 1e9 if n is None else n})
    if not rows:
        raise Unfilled("no tasks")
    rows.sort(key=lambda x: x.pop("_n"))
    alt = f"{len(rows)} account tasks: " + "; ".join(f"{x['t']} {x['dl']}".strip() for x in rows) + "."
    return {"rows": rows}, alt


def _health(c, b):
    """Sender and time only: the message's subject and text are never read into the block."""
    r = c.rec(b["ref"])
    ts = r.get("msg_ts") or r.get("created") or c.now
    who = r["sender"] or r["who"] or "your care team"
    when = f"{day_words(c.local(ts))}, {clock(c.local(ts))}"
    link = r.get("link") if str(r.get("link") or "").startswith("https://") else None
    return {"who": who, "sub": f"{_src_word(r)} · {when}", "link": link}, \
        f"A new message from {who}, {when}. What it says stays behind the portal login."


def _stat(c, b):
    r = c.rec(b["ref"])
    vals = [c.q(r, f"values[{i}]", v) for i, v in enumerate(b["values"])]
    if any(v is None for v in vals):
        raise Unfilled("the week's numbers aren't all in the message")
    nums = [number(v) for v in vals]
    metric = c.q(r, "metric", b["metric"]) or b["metric"]
    goal = c.q(r, "goal", b.get("goal"))
    g = number(goal) if goal else None
    avg = c.q(r, "average", b.get("average"))
    big = avg or f"{round(sum(nums) / 7):,}"
    over = sum(1 for v in nums if g is not None and v >= g)
    top = max(nums + ([g] if g else [])) * 1.07 or 1
    d = {"big": big, "unit": f"{metric.lower()} a day", "range": c.q(r, "range", b.get("range")),
         "change": c.q(r, "change", b.get("change")),
         "over": f"{over} of 7 days over your goal" if g is not None else None,
         "bars": [{"l": w, "p": round(v / top * 100, 1), "hi": g is not None and v >= g, "v": vals[i]}
                  for i, (w, v) in enumerate(zip("MTWTFSS", nums))],
         "goal": round(g / top * 100, 1) if g else None, "goal_label": f"Goal {goal}" if goal else None}
    alt = f"{big} {d['unit']}" + (f", {d['range']}" if d["range"] else "") + ". Days: " + ", ".join(
        f"{('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[i]} {v}" for i, v in enumerate(vals)) + \
        (f". {d['over']}." if d["over"] else ".")
    return d, alt


_LEVELS = {"info": (1, "Info"), "advisory": (2, "Advisory"), "warning": (3, "Warning")}


def _local(c, b):
    rows = []
    for x in b["items"]:
        r = c.rec(x["ref"])
        lv, word = _LEVELS[x["level"]]
        if quote(word, r.get("hay", "")) is None:      # the level is the sender's word, not a guess
            c.checks.append({"ref": r["ref"], "field": "level", "why": "not found in the message"})
            lv, word = 1, "Info"
        title = c.q(r, "title", x["title"])
        if not title:
            continue
        dist = c.q(r, "distance", x.get("distance"))
        mi = number(re.match(r"[0-9.]+", dist).group(0)) if dist else None
        if mi is not None and re.search(r"km", dist, re.I):
            mi = mi * 0.621
        rows.append({"lv": lv, "word": word, "wn": lv >= 2, "t": title, "s": c.q(r, "detail", x.get("detail")),
                     "mi": round(min(mi, 5) / 5 * 100, 1) if mi is not None else None,
                     "dist": f"{dist} away" if dist else None})
    if not rows:
        raise Unfilled("no local alerts")
    alt = "Local alerts: " + "; ".join(f"{x['word']}: {x['t']}" + (f", {x['dist']}" if x["dist"] else "")
                                       for x in rows) + "."
    return {"rows": rows, "note": "Level from the sender: Info, Advisory, Warning. Distance from home, up to 5 mi."}, alt


def _price(c, b):
    r = c.rec(b["ref"])
    name = c.q(r, "name", b["name"])
    rng = [c.q(r, "range", v) for v in b.get("range", [])]
    rng = [money(v)[1] for v in rng if v] if len(rng) == 2 and all(rng) else None
    marks, rows = [], []
    for x in b["matches"]:
        p = c.q(r, "price", x["price"])
        if not p:
            continue
        v = money(p)[1]
        note = c.q(r, "note", x.get("note"))
        inr = bool(rng and rng[0] <= v <= rng[1])
        marks.append({"v": float(v), "in": inr, "p": p})
        if inr or not rng:
            rows.append({"t": p + (f" · {note}" if note else "")})
    if not name or not marks:
        raise Unfilled("the watch's name or prices aren't in the message")
    vals = [m["v"] for m in marks] + ([float(x) for x in rng] if rng else [])
    lo = math.floor(min(vals) / 100) * 100
    hi = max(lo + 100, math.ceil(max(vals) / 100) * 100)
    sym = money(marks[0]["p"])[0]

    def pos(v):
        return round((v - lo) / (hi - lo) * 100, 1)
    ticks = sorted({lo, hi} | ({float(x) for x in rng} if rng else set()))
    new = c.q(r, "new", b.get("new"))
    k = sum(1 for m in marks if m["in"])
    sub = " · ".join(x for x in ("Saved search", f"{new or len(marks)} new matches",
                                 f"{k} in your range" if rng else "") if x)
    d = {"name": name, "sub": sub, "range": [pos(float(rng[0])), pos(float(rng[1]))] if rng else None,
         "marks": [{"x": pos(m["v"]), "in": m["in"]} for m in marks],
         "ticks": [[pos(t), f"{sym}{t:,.0f}"] for t in ticks], "rows": rows[:2]}
    alt = f"{name}: {sub}. " + "; ".join(m["p"] + (" in range" if m["in"] else "") for m in marks) + "."
    return d, alt


def _leads(c, b):
    r = c.rec(b["ref"])
    cnt = c.q(r, "count", b.get("count"))
    rows = []
    for x in b.get("top", []):
        t = c.q(r, "title", x["title"])
        if t:
            rows.append({"t": t, "s": c.q(r, "org", x.get("org")), "r": c.q(r, "rate", x.get("rate"))})
    n = int(number(cnt)) if cnt else len(rows)
    if not n:
        raise Unfilled("nothing in the digest")
    what = c.q(r, "what", b.get("what")) or f"new in {r['sender'] or r['who']}"
    more = f"{n - len(rows)} more in the digest · from {r['sender'] or r['who']}" if n > len(rows) else None
    alt = f"{n} {what}" + (": " + "; ".join(x["t"] for x in rows) if rows else "") + "."
    return {"n": f"{n:,}", "what": what, "rows": rows, "more": more}, alt


def _series(c, b):
    r = c.rec(b["ref"])
    name = c.q(r, "name", b["name"])
    anchor = c.local(r.get("msg_ts") or r.get("created") or c.now).date()
    days = []
    for i, s in enumerate(b["dates"]):
        got = c.q(r, f"dates[{i}]", s)
        d = parse_day(got, anchor) if got else None
        if d:
            days.append(d)
    if not name or not days:
        raise Unfilled("the series' name or dates aren't in the message")
    days = sorted(set(days))
    t = c.q(r, "time", b.get("time"))
    first, last = days[0], days[-1]
    start = first - dt.timedelta(days=first.weekday())
    end = min(last + dt.timedelta(days=6 - last.weekday()), start + dt.timedelta(days=41))
    cells, d = [], start
    while d <= end:
        cells.append({"d": d.day, "s": d in days, "x": d < c.today() or d > last})
        d += dt.timedelta(days=1)
    wd = sorted({x.weekday() for x in days})
    wdw = " and ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[w] for w in wd) if len(wd) <= 3 else ""
    sub = " · ".join(x for x in (wdw, t, f"{short_date(first)} to {short_date(last)}") if x)
    n = (first - c.today()).days
    d = {"name": f"{name} · {len(days)} sessions", "sub": sub, "cells": cells,
         "first": ("First " + rel_days(n)) if n >= 0 else f"Started {short_date(first)}"}
    alt = f"{name}: {len(days)} sessions, {sub}. " + ", ".join(day_words(x) for x in days) + "."
    return d, alt


def _invite(c, b):
    r = c.rec(b["ref"])
    who = r["sender"] or r["who"]
    what = c.q(r, "what", b.get("what"))
    app = c.q(r, "app", b.get("app"))
    exp = c.q(r, "expires", b.get("expires"))
    ts = r.get("msg_ts") or r.get("created") or c.now
    sent = f"{c.local(ts):%a}" if 0 <= c.now - ts < 6 * 86400 else short_date(c.local(ts))
    t = f"{who} invited you" + (f" {what}" if what and not what.lower().startswith("invited") else "")
    note = (f"The invite expires {exp}. " if exp else "") + "Callsheet never accepts for you."
    link = r.get("link") if str(r.get("link") or "").startswith("https://") else None
    d = {"initial": (who[:1] or "?").upper(), "t": t, "sub": " · ".join(x for x in (app, f"sent {sent}") if x),
         "note": note, "link": link}
    return d, f"{t}. {d['sub']}. {note}"


def _subs(c, b):
    days = b.get("days", 30)
    rows, due = [], []
    for x in b["items"]:
        r = c.rec(x["ref"])
        price = c.q(r, "price", x["price"])
        if r.get("start") is None:
            continue
        n = c.days_to(r["start"])
        wn = bool(x.get("trial")) and n <= 14
        rows.append({"n": r["who"] or r["title"], "s": price or "", "d": day_words(c.local(r["start"])),
                     "r": f"trial ends {rel_days(n)}" if x.get("trial") else rel_days(n), "wn": wn, "_n": n,
                     "trial": bool(x.get("trial"))})
        if price and 0 <= n <= days and not x.get("trial"):
            due.append(price)
    if not rows:
        raise Unfilled("no renewals with dates")
    rows.sort(key=lambda x: x.pop("_n"))
    tot = total(due)
    trials = [x for x in rows if x.pop("trial")]
    note = f"Plus {len(trials)} trial{'s' * (len(trials) != 1)} ending" if trials else None
    d = {"rows": rows, "total": tot, "total_label": f"Next {days} days", "note": note}
    alt = "Renewals: " + "; ".join(f"{x['n']}, {x['s']}, {x['d']}" for x in rows) + (
        f". Next {days} days {tot}." if tot else ".")
    return d, alt


def _composite(c, b):
    ctx_ref = b.get("ref")
    words = []

    def need_quote(text, ref, field):
        if not re.search(r"[0-9]", text):
            return text
        r = c.rec(ref or ctx_ref, need=False) if (ref or ctx_ref) else None
        got = c.q(r, field, text) if r else None
        if r is None:
            c.checks.append({"field": field, "why": "a number without a message to quote"})
        return got

    def node(n, axis=None):
        t = n["t"]
        out = {"t": t}
        if t in ("row", "column"):
            out["c"] = [k for k in (node(k) for k in n["c"]) if k]
        elif t == "text":
            txt = need_quote(n["text"], n.get("ref"), "text")
            if not txt:
                return None
            out.update(text=txt, style=n.get("style", "body"))
        elif t == "number":
            r = c.rec(n["ref"], need=False)
            v = c.q(r, "value", n["value"]) if r else None
            if not v:
                return None
            out.update(value=v, unit=n.get("unit", ""))
        elif t == "time_axis":
            a, z = n["from"], n["to"]
            out.update(a=a, b=z, ticks=_ticks(a, z), c=[k for k in (node(k, (a, z)) for k in n["c"]) if k])
        elif t in ("range_bar", "dot"):
            r = c.rec(n["ref"], need=False)
            if not r or r.get("start") is None:
                return None
            base = c.local(r["start"]).date()
            s = c.hours(r["start"], base)
            e = c.hours(r["end"], base) if r.get("end") else s + 1
            out.update(s=round(s, 3), e=round(e, 3), label=n.get("label") or r["title"] or r["who"],
                       sub=span_words(c.local(r["start"]), c.local(r["end"]) if t == "range_bar" and r.get("end")
                                      else None))
        elif t == "list":
            out["items"] = [x for x in (need_quote(i, n.get("ref"), "items") for i in n["items"]) if x]
        elif t == "icon":
            out.update(name=n["name"], label=n.get("label", ""))
        elif t == "progress":
            r = c.rec(n["ref"], need=False)
            v, o = (c.q(r, "value", n["value"]), c.q(r, "of", n["of"])) if r else (None, None)
            if not v or not o or not number(o):
                return None
            out.update(value=v, of=o, p=round(max(0, min(1, number(v) / number(o))) * 100, 1),
                       label=n.get("label", ""))
        elif t == "table":
            out["head"] = [need_quote(x, n.get("ref"), "head") or "" for x in n.get("head", [])]
            out["rows"] = [[need_quote(x, n.get("ref"), "rows") or "" for x in row] for row in n["rows"]]
        for k in ("text", "label", "value", "unit"):
            if isinstance(out.get(k), str) and out[k]:
                words.append(out[k])
        for k in out.get("items", []):
            words.append(k)
        return out

    body = node(b["body"])
    if not body:
        raise Unfilled("nothing left to draw")
    title = b.get("title")
    return {"name": b["name"], "title": title, "body": body}, (title + ": " if title else "") + "; ".join(words[:12]) + "."


FILL = {"day": None, "clash": _clash, "asks": _asks, "upcoming": _upcoming, "money": _money, "trip": _trip,
        "person": _person, "cleanup": _cleanup, "bills": _bills, "ledger": _ledger, "package": _package,
        "ticket": _ticket, "tasks": _tasks, "health": _health, "stat": _stat, "local": _local, "price": _price,
        "leads": _leads, "series": _series, "invite": _invite, "subs": _subs, "composite": _composite}


def fill(block, facts):
    """A validated block -> {"v", "type", "label", "alt", "d", "checks"} for visuals.js. Raises Unfilled."""
    c = _Ctx(facts)
    t = block["type"]
    d, alt = _day(c, block, None, _date_word(c)) if t == "day" else FILL[t](c, block)
    return {"v": VERSION, "type": t, "label": _BLOCKS[t][0], "alt": re.sub(r"\s+", " ", alt).strip(), "d": d,
            "checks": c.checks}


def compose(show, facts):
    """A turn's "show" (one block or a list) -> {"blocks": [...filled], "dropped": {why: n}, "problems": [...]}.
    Nothing unknown or unchecked is ever passed on; the words of the turn still play."""
    if show is None:
        show = []
    items = show if isinstance(show, list) else [show]
    dropped = {"unknown": 0, "invalid": 0, "oversized": 0, "unfilled": 0, "too_many": 0}
    problems, blocks = [], []
    for i, b in enumerate(items):
        if len(blocks) >= MAX_BLOCKS:
            dropped["too_many"] += 1
            problems.append({"at": i, "why": "too_many", "type": b.get("type") if isinstance(b, dict) else None,
                             "errors": [f"at most {MAX_BLOCKS} blocks in a turn"]})
            continue
        ok, bad = validate(b)
        if bad:
            dropped[bad["why"]] += 1
            problems.append(dict(bad, at=i))
            continue
        try:
            blocks.append(fill(ok, facts))
        except Unfilled as e:
            dropped["unfilled"] += 1
            problems.append({"at": i, "why": "unfilled", "type": ok["type"], "errors": [str(e)]})
    return {"blocks": blocks, "dropped": dropped, "problems": problems}
