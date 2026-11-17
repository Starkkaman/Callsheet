"""0.18.0 (rework stage 4): the new Today, made from the ledger (docs/today.md).

Today reads the ledger (ledger.py), its clashes and follow-ups (conflicts.py), the calendar and the message store
(msgstore.py) and shapes what the page draws: Now and next (clashes soonest first, follow-ups, today's calendar),
Needs you (oldest first), Coming up (the next 30 days, from the calendar AND messages) and Clean up; the item view
(where you stand, the threads with the ask and the owner's yes marked, Chat about this); and the Replies tab. Every
label is made here (12-hour times, "in 3 days"), so the page only draws text. 0.21.0 (heads.py): security alerts at
the top of Now and next until Done, invites to answer in Needs you ("RSVP: …"), and the quiet "From your channels,
feeds and reports" group below Coming up (connectors.py and conn_reports.py's lists).

The owner's own presses are written here: Done, Dismiss, a note to the assistant, Remind me. Nothing is sent to anyone and
no account changes: "Reply to Marco" is a link the owner opens themselves (Gmail, Slack, Signal). 0.21.0: "Add to calendar"
(calwrite.py, approved 2026-11-11) is on once an account allows adding events; the press opens a confirm card
on the page and only its Add writes (one event per item, ever); before that it stays off ("needs calendar access"). Reminders go to the owner's own
phones through the 0.5.1 push (names, sources and times, never message text).
"""
import datetime as dt
import json
import re

import conflicts as C
import urllib.parse

import rules
import tidy as TY
import heads
import ledger as L

SOURCES = {"gmail": ("mail", "Email"), "signal": ("signal", "Signal"), "slack": ("slack", "Slack"),
           "sms": ("text", "Text"), "calendar": ("cal", "Calendar"), "reports": ("note", "Report"),
           "contacts": ("areas", "Contacts")}   # 0.21.0: birthdays from Google Contacts (contacts.py)
FROM = {"gmail": "From email", "signal": "From Signal", "slack": "From Slack", "sms": "From text",
        "reports": "From a report", "contacts": "From contacts"}
APP = {"gmail": "Gmail", "slack": "Slack", "signal": "Signal", "sms": "Messages"}
for _m in (SOURCES, FROM, APP):          # 0.20.0: a live text notification (texts.py, sms_live) reads as a text
    _m["sms_live"] = _m["sms"]
HORIZON_DAYS = 30
EXPAND_S = 48 * 3600            # a clash this near shows both sides; a later one is one line
RED_S = 86400                   # under 24 h: the red treatment
WEEK_S = 7 * 86400              # under a week: amber
THREAD_SHOWN = 6                # the newest messages of a thread shown before "N earlier"
THREAD_MAX = 60
TEXT_MAX = 900
REMIND_HOUR = 9                 # "tomorrow" means 9:00 am
CHIPS = {"not_for_me": "Not for me", "junk": "Junk mail", "remind_tomorrow": "Remind me tomorrow"}
FILES = frozenset({"not_for_me", "junk"})        # these chips file the items (done) and teach the sorting
NOTE_MAX = 300
FEEDS_SHOWN = 5                 # 0.21.0: "From your channels, feeds and reports" shows this many, then "n more"
TYPE_WORD = {"ask": "Ask", "commitment": "Your word", "event": "Event", "cleanup": "Clean up"}
CLEAN_GROUPS = (("channels", "From your channels", "slack"), ("promo", "Promotions", "news"),
                ("newsletter", "Newsletters", "news"), ("receipt", "Receipts", "mail"),
                ("chatter", "Slack chatter", "slack"), ("other", "Notifications", "note"))
_RECEIPT = re.compile(r"receipt|order|invoice|payment|paid|statement|shipping|delivery", re.I)
_NEWS = re.compile(r"newsletter|digest|weekly|promo|offer|deal|sale|update", re.I)
_BIZ = re.compile(r"\b(rentals?|billing|films?|studios?|supply|inc|llc|ltd|co|team|support|bank|store|shop|club|"
                  r"weekly|digest|news|notes?)\b", re.I)
_QUOTE = re.compile(r"^\s*(On .{4,200} wrote:|-{2,}\s*Original Message|From: .+@.+)\s*$", re.I)
_TRIP_WORD = re.compile(r"(?:trip|stay|vacation|holiday|travel|visit|weekend|getaway|flight|drive)\b", re.I)
_VIDEO = re.compile(r"meet\.google|zoom\.us|teams\.microsoft|\bvideo\b|google meet|\bzoom\b|facetime", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS today_notes (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, text TEXT NOT NULL DEFAULT '', chips TEXT NOT NULL DEFAULT '[]',
  items TEXT NOT NULL DEFAULT '[]', label TEXT NOT NULL DEFAULT '', learned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS today_reminders (
  id INTEGER PRIMARY KEY, item INTEGER, conflict INTEGER, followup INTEGER, due_ts REAL NOT NULL,
  hide INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, sent_at REAL,
  alert TEXT NOT NULL DEFAULT '', batch INTEGER);
CREATE INDEX IF NOT EXISTS today_reminders_state ON today_reminders (state)
"""


class Invalid(ValueError):
    """A request the page shouldn't have sent (api.py turns it into a 400)."""


# --- words and times -----------------------------------------------------------------------------------------------
LINK_HOSTS = ("calendar.google.com", "www.google.com")


def safe_cal_link(url):
    """A Calendar event's htmlLink, kept only when it is https on Google's own calendar hosts (from gather.py)."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return None
    return url if p.scheme == "https" and p.hostname in LINK_HOSTS else None


def agenda(ms, lv, tz, day, fmt="24h"):
    """The Agenda tab's day (the rework; no redesign yet): timed calendar events from messages.db plus open ledger items
    with a time that day and no calendar event of their own, as the page's schedule rows (time, end, start_hhmm,
    end_hhmm, title, short, clash and its flag). Rules only; nothing is sent."""
    start = dt.datetime.combine(day, dt.time(), tz)
    s_ts, e_ts = start.timestamp(), (start + dt.timedelta(days=1)).timestamp()
    rows, seen = [], set()
    for ev in ms.events(since=s_ts, until=e_ts) if ms else []:
        if ev["all_day"] or ev["transparency"] == "transparent" or ev["start_ts"] is None:
            continue
        key = (ev["ical_uid"] or ev["event_id"], int(ev["start_ts"]))
        if key in seen:      # the same event on two of the owner's calendars
            continue
        seen.add(key)
        rows.append({"s": ev["start_ts"], "e": max(ev["end_ts"] or ev["start_ts"], ev["start_ts"]),
                     "title": (ev["title"] or "").strip() or "(No title)", "key": f"cal:{key[0]}:{key[1]}",
                     "link": safe_cal_link(ev["link"]), "where": ev["location"] or ""})
    for it in lv.items("status='open' AND start_ts IS NOT NULL AND start_ts < ? AND coalesce(end_ts, start_ts) >= ? "
                       "AND all_day=0 AND cal_ref IS NULL", (e_ts, s_ts)) if lv else []:
        rows.append({"s": it["start_ts"], "e": max(it["end_ts"] or it["start_ts"], it["start_ts"]),
                     "title": (it["what"] or "").strip() or "(No title)", "key": f"item:{it['id']}", "item": it["id"]})
    rows.sort(key=lambda r: (r["s"], r["e"], r["title"]))
    out = []
    for i, r in enumerate(rows):
        a, b = max(r["s"], s_ts), min(max(r["e"], r["s"] + 1800), e_ts)
        sa, sb = rules.hhmm(dt.datetime.fromtimestamp(a, tz)), "24:00" if b >= e_ts else rules.hhmm(
            dt.datetime.fromtimestamp(b, tz))
        row = {"id": f"e{i + 1}", "key": r["key"], "time": rules.clock(sa, fmt), "end": rules.clock(sb, fmt),
               "start_hhmm": sa, "end_hhmm": sb, "title": r["title"], "short": rules._clip(r["title"], 24)}  # noqa: SLF001
        for k in ("link", "where", "item"):
            if r.get(k):
                row[k] = r[k]
        other = next((o for o in rows if o is not r and o["s"] < max(r["e"], r["s"] + 1800) and
                      r["s"] < max(o["e"], o["s"] + 1800)), None)
        if other is not None:
            at = rules.clock(rules.hhmm(dt.datetime.fromtimestamp(max(other["s"], s_ts), tz)), fmt)
            row["clash"] = True
            row["flags"] = [{"kind": "overlap", "at": at, "title": rules._clip(other["title"], 40)}]  # noqa: SLF001
            row["flag"] = {"text": f"Clashes with {rules._clip(other['title'], 40)} at {at}"}  # noqa: SLF001
        out.append(row)
    return out


def t12(d):
    """'7:00 pm' (12-hour, always with minutes)."""
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"


def short_day(d):
    return f"{d:%a} {d:%b} {d.day}"


def local(ts, tz):
    return dt.datetime.fromtimestamp(ts, tz)


def until(sec):
    """How far off a time is: 'in 40 min', 'in 9 h', 'in 3 days', 'in 3 weeks', 'now'."""
    if sec is None:
        return ""
    if sec <= 0:
        return "now"
    if sec < 3600:
        return f"in {max(1, int(sec // 60))} min"
    if sec < 48 * 3600:
        return f"in {int(sec // 3600)} h"
    days = int(sec // 86400)
    return f"in {days} days" if days < 14 else f"in {round(days / 7)} weeks"


def rel_day(d, today):
    n = (d - today).days
    if n <= 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days" if n < 14 else f"in {round(n / 7)} weeks"


def waited(sec):
    """Needs you: how long it has waited ('40 min', '18 h', '5 days')."""
    sec = max(0, sec)
    if sec < 3600:
        return f"{max(1, int(sec // 60))} min"
    if sec < 48 * 3600:
        return f"{int(sec // 3600)} h"
    return f"{int(sec // 86400)} days"


def ago(ts, now, tz):
    """When a message came: '48 min ago', '7:52 am' (today), 'Tue' (this week), 'Nov 2'."""
    if ts is None:
        return ""
    sec = now - ts
    d, n = local(ts, tz), local(now, tz)
    if 0 <= sec < 3600:
        return f"{max(1, int(sec // 60))} min ago"
    if d.date() == n.date():
        return t12(d)
    if 0 < (n.date() - d.date()).days < 7:
        return f"{d:%a}"
    return f"{d:%b} {d.day}"


def day_word(d, today):
    n = (d - today).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(n) or (f"{d:%a}" if 0 < n < 7 or -7 < n < 0 else short_day(d))


def slot_word(ts, now, tz):
    """'Today 6:00 pm', 'Tomorrow 7:00 pm', 'Sat 10:00 am', 'Thu Nov 19, 7:00 pm'."""
    d, today = local(ts, tz), local(now, tz).date()
    n = (d.date() - today).days
    if n == 0:
        return f"Today {t12(d)}"
    if n == 1:
        return f"Tomorrow {t12(d)}"
    if 1 < n < 7:
        return f"{d:%a} {t12(d)}"
    return f"{short_day(d)}, {t12(d)}"


def first(name):
    """A person's first name for short lines ('Marco'); a business or a handle stays whole."""
    name = str(name or "").strip()
    if not name or "@" in name or name[:1] in "+#" or _BIZ.search(name):
        return name
    parts = name.split()
    return parts[0] if len(parts) in (2, 3) and all(p[:1].isupper() for p in parts) else name


def _loads(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def seg(*parts):
    """Rich text for the page as [[text, bold], ...] (drawn with textContent, never HTML)."""
    return [[str(t), bool(b)] for t, b in parts if t]


def clean_text(text):
    """A message's own words: quoted replies cut, whitespace squeezed, capped."""
    out = []
    for line in str(text or "").splitlines():
        if _QUOTE.match(line) or line.lstrip().startswith(">"):
            break
        out.append(line.rstrip())
    s = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX].rstrip() + " …"


def reply_link(src, address=None):
    """A link the owner opens themselves to answer: Gmail or Slack (the message's own https link), Signal (sgnl:// with
    the number), a text (sms:). None when there is no safe link."""
    s = src.get("source")
    link = str(src.get("link") or "")
    if s in ("gmail", "slack") and re.match(r"https://[a-z0-9.-]+\.(google|slack)\.com/", link):
        return {"href": link, "app": APP[s]}
    h = str(address if address is not None else src.get("sender") or "")
    if s == "signal" and re.fullmatch(r"\+\d{6,16}", h):
        return {"href": "sgnl://signal.me/#p/" + h, "app": "Signal"}
    if s in ("sms", "sms_live") and re.fullmatch(r"\+?\d{6,16}", h):
        return {"href": "sms:" + h, "app": "Messages"}
    return None


def clean_group(it, source):
    # 0.21.4 (tidy.py): a line the rules moved or made says its group in a "tidy" check
    tg = next((c.get("value") for c in it.get("checks") or [] if isinstance(c, dict) and c.get("field") == "tidy"), "")
    if tg in ("channels", "promo"):
        return tg
    sub = f"{it.get('subtype') or ''} {it.get('what') or ''}"
    if source == "slack":
        return "chatter"
    if _RECEIPT.search(sub):
        return "receipt"
    if _NEWS.search(sub):
        return "newsletter"
    return "other"


class Today:
    """The Today view model and the owner's presses. ms: MessageStore, lv: ledger.Ledger."""

    def __init__(self, ms, lv, clock, alerter=None, reader=None, log=None, feeds=None):
        self.ms, self.lv, self.clock = ms, lv, clock
        # 0.21.0: feeds() -> {"feeds": connectors' brief_list or None, "reports": reports_list or None}
        self.feeds = feeds
        self.cal = None        # 0.21.0 Add to calendar (calwrite.CalWrite), set by api.App
        # 0.21.0 Google Contacts (contacts.Contacts), set by api.App: birthdays(first, last) for Coming up
        self.contacts = None
        self.areas = None      # 0.22.0 areas.Areas, set by api.App: each row's area id (area_res / area_of)
        self.alerter = alerter
        self.tidy = None       # 0.21.4 Tidy up (tidy.Tidy), set by api.App: the banner's count
        self.refreshed_at = None   # 0.21.4: the owner's last Refresh (Today and the day's brief rebuilt)
        self.reader = reader or (lambda: {})
        self.log = log or (lambda line: None)
        ms.write(lambda db: [db.execute(s) for s in _SCHEMA.split(";") if s.strip()])
        ms.write(self._migrate)

    def _migrate(self, db):
        """0.22.0 one reminder store: `push` (0: a snooze, hidden until then with no phone reminder), and the snoozes
        and reminders Chat kept in prefs' owner_actions (0.17.0) moved here once, each row marked with where it went."""
        if "push" not in {r[1] for r in db.execute("PRAGMA table_info(today_reminders)")}:
            db.execute("ALTER TABLE today_reminders ADD COLUMN push INTEGER NOT NULL DEFAULT 1")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_actions'").fetchone():
            return
        now = self.clock()
        for r in db.execute("SELECT * FROM owner_actions WHERE action IN ('snooze','remind') AND undone_at IS NULL "
                            "AND fired_at IS NULL AND until_ts IS NOT NULL ORDER BY until_ts, id").fetchall():   # the latest stays open
            try:
                f = json.loads(r["fields"] or "{}")
            except ValueError:
                f = {}
            if not isinstance(f, dict) or "moved" in f:
                continue
            rid = None
            if r["action"] == "remind" or r["until_ts"] > now:   # a past snooze has nothing left to carry
                rid = self._set_aside(db, r["item"], r["until_ts"], r["at"], r["action"] == "remind")
            db.execute("UPDATE owner_actions SET fields=? WHERE id=?", (json.dumps(dict(f, moved=rid)), r["id"]))

    @staticmethod
    def _set_aside(db, item, due, now, push=True, batch=None):
        """Hide one item until `due`, then remind the owner (push) or just show it again (a snooze). Replaces the
        item's open reminder. Today's "Remind me tomorrow" and Chat's snooze and remind (actions.py) both land here."""
        db.execute("UPDATE today_reminders SET state='replaced' WHERE state='open' AND item=?", (item,))
        return db.execute("INSERT INTO today_reminders (item, due_ts, hide, state, created_at, batch, push) "
                          "VALUES (?,?,1,'open',?,?,?)", (item, due, now, batch, int(bool(push)))).lastrowid

    def set_aside(self, items, until, remind=True):
        """0.22.0: Chat's snooze (remind False) and remind me at (remind True) on items, through the same store and
        tick as Today's buttons. Returns {reminders: [ids], items, at}."""
        its = self._ids(items)
        now = self.clock()
        if isinstance(until, bool) or not isinstance(until, (int, float)) or not now < until <= now + 400 * 86400:
            raise Invalid("Pick a time in the future.")

        def run(db):
            out = []
            for i in its:
                L.history(db, i, "owner_" + ("remind" if remind else "snooze"), now, note=f"until {until:.0f}")
                out.append(self._set_aside(db, i, until, now, remind))
            return out
        rids = self.ms.write(run)
        return {"reminders": rids, "items": sorted(its), "at": self._due_word(until, now, self.tz())}

    def cancel_reminders(self, rids):
        """Undo a snooze or reminder (actions.py): the open ones among `rids` are cancelled; returns how many."""
        ids = [int(r) for r in rids or [] if isinstance(r, int) and not isinstance(r, bool)]
        if not ids:
            return 0
        now = self.clock()

        def run(db):
            n = 0
            for r in db.execute(f"SELECT id, item FROM today_reminders WHERE state='open' AND id IN "
                                f"({','.join('?' * len(ids))})", ids).fetchall():
                db.execute("UPDATE today_reminders SET state='cancelled' WHERE id=?", (r["id"],))
                if r["item"] is not None:
                    L.history(db, r["item"], "owner_undo", now, note="undid set aside")
                n += 1
            return n
        return self.ms.write(run)

    def snoozed(self, now=None):
        """{item: until} for items hidden until a snooze or reminder is due (the ledger view's "snoozed")."""
        now = self.clock() if now is None else now
        return {r["item"]: r["u"] for r in self._read(
            "SELECT item, max(due_ts) AS u FROM today_reminders WHERE state='open' AND hide=1 AND due_ts > ? AND "
            "item IS NOT NULL GROUP BY item", (now,))}

    def area_res(self):
        """0.22.0: one read of the areas (areas.Resolver), or None. An area is a nicety: never fails a view."""
        try:
            return self.areas.resolver() if self.areas is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def area_of(res, src, calendar=None):
        """The area id of a source message or event row ({source, account, ts}), "" when none."""
        if res is None or not src:
            return ""
        try:
            return res.at(src["source"], src["account"], src["ts"], calendar) or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def tz(self):
        return self.lv.tz()

    def _read(self, sql, args=()):
        return [dict(r) for r in self.ms._read(sql, args)]  # noqa: SLF001

    # --- rows ------------------------------------------------------------------------------------------------------
    def _items(self, ids):
        ids = [int(i) for i in ids if i is not None]
        if not ids:
            return {}
        rows = self._read(f"SELECT * FROM ledger_items WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: L.row_dict(r) for r in rows}

    def _item(self, item_id):
        return self._items([item_id]).get(int(item_id))

    def _srcs(self, item_ids):
        ids = [int(i) for i in item_ids if i is not None]
        if not ids:
            return {}
        out = {}
        for r in self._read("SELECT s.item, s.role, m.id, m.source, m.account, m.thread, m.direction, m.sender, "
                            "m.sender_name, m.recipients, m.ts, m.subject, m.link FROM ledger_sources s JOIN messages m "
                            f"ON m.id=s.message WHERE s.item IN ({','.join('?' * len(ids))}) ORDER BY m.ts", ids):
            out.setdefault(r["item"], []).append(r)
        return out

    @staticmethod
    def _origin(srcs):
        made = [s for s in srcs if s["role"] == "created"]
        return (made or srcs or [None])[0]

    def _person(self, it, srcs, items=None):
        """The other person of an item: who it's from or for."""
        if it.get("who") and it["who"] != L.OWNER:
            return it.get("who_name") or it["who"]
        if it.get("predecessor"):
            p = (items or {}).get(it["predecessor"]) or self._item(it["predecessor"])
            if p and p.get("who") and p["who"] != L.OWNER:
                return p.get("who_name") or p["who"]
        for s in srcs:
            if s["direction"] == "in" and (s["sender_name"] or s["sender"]):
                return self.ms.shown_name(s) or s["sender"]
        for s in srcs:
            for r in _loads(s["recipients"], []):
                n = (r.get("name") or r.get("address")) if isinstance(r, dict) else str(r)
                if n:
                    return n
        return it.get("says_name") or ""

    def _reply(self, it, srcs):
        """The link to answer this item's person (their newest message in the item's threads), or None."""
        threads = {(s["source"], s["account"], s["thread"]) for s in srcs if s["thread"]}
        best = None
        for src, acc, th in threads:
            r = self._read("SELECT * FROM messages WHERE source=? AND account=? AND thread=? AND direction='in' "
                           "AND deleted_at IS NULL ORDER BY ts DESC LIMIT 1", (src, acc, th))
            if r and (best is None or r[0]["ts"] > best["ts"]):
                best = r[0]
        if best is not None:
            return reply_link(best)
        for s in reversed(srcs):
            if s["direction"] == "out":
                for r in _loads(s["recipients"], []):
                    if isinstance(r, dict) and r.get("address"):
                        got = reply_link(s, r["address"])
                        if got:
                            return got
        return None

    def _span(self, it):
        return L.span(it, self.tz())

    def _hidden(self, now):
        """Items the owner snoozed ("remind me tomorrow") until their reminder is due."""
        return {r["item"] for r in self._read("SELECT item FROM today_reminders WHERE state='open' AND hide=1 AND "
                                              "due_ts > ? AND item IS NOT NULL", (now,))}

    def _reminders(self, state=None):
        sql = "SELECT * FROM today_reminders" + (" WHERE state=?" if state else "") + " ORDER BY id"
        return self._read(sql, (state,) if state else ())

    # --- clashes -----------------------------------------------------------------------------------------------------
    def _side(self, kind, ref, items, srcs, now, tz):
        if kind == "event":
            r = self._read("SELECT * FROM events WHERE id=?", (ref,))
            e = r[0] if r else {}
            end = local(e["end_ts"], tz) if e.get("end_ts") else None
            return {"kind": "event", "id": ref, "source": "calendar", "icon": "cal", "chip": "Calendar",
                    "name": e.get("title") or "Calendar", "first": e.get("title") or "your calendar",
                    "title": e.get("title") or "", "until": f"until {t12(end)}" if end and not e.get("all_day") else "",
                    "ts": e.get("start_ts"), "link": None, "status": e.get("status") or ""}
        it = items.get(ref) or {}
        ss = srcs.get(ref, [])
        o = self._origin(ss) or {}
        if it.get("subtype") == "invite" and it.get("cal_ref") is not None:    # 0.21.0: a calendar invite
            name = it.get("who_name") or "Calendar invite"
            return {"kind": "item", "id": ref, "type": it.get("type"), "status": it.get("status"), "mine": False,
                    "source": "calendar", "icon": "cal", "chip": "Calendar", "name": name, "first": first(name),
                    "what": "RSVP: " + (it.get("what") or ""), "for": "", "subtype": "invite",
                    "phrase": it.get("phrase") or "", "ts": it.get("created_at"), "yes_ts": None,
                    "link": self._invite_link(it, ss), "predecessor": None, "confidence": it.get("confidence")}
        name = self._person(it, ss, items)
        mine = it.get("type") == "commitment" and it.get("who") == L.OWNER
        yes_ts = None
        if mine:
            outs = [s for s in ss if s["direction"] == "out"]
            yes_ts = (outs[0] if outs else o).get("ts")
        src = o.get("source") or "gmail"
        return {"kind": "item", "id": ref, "type": it.get("type"), "status": it.get("status"), "mine": mine,
                "source": src, "icon": SOURCES.get(src, ("mail", ""))[0], "chip": SOURCES.get(src, ("", "Message"))[1],
                "name": name, "first": first(name), "what": it.get("what") or "", "for": it.get("purpose") or "",
                "subtype": it.get("subtype") or "", "phrase": it.get("phrase") or "", "ts": o.get("ts"),
                "yes_ts": yes_ts, "link": self._reply(it, ss), "predecessor": it.get("predecessor"),
                "confidence": it.get("confidence")}

    def _invite_link(self, it, srcs):
        """Where the owner answers an invite themselves: the calendar entry (Google's own https link), else the mail."""
        if it.get("cal_ref") is not None:
            r = self._read("SELECT link FROM events WHERE id=?", (it["cal_ref"],))
            href = safe_cal_link(r[0]["link"]) if r else None
            if href:
                return {"href": href, "app": "Calendar", "label": "Open in Calendar"}
        o = self._origin(srcs) or {}
        got = reply_link(o) if o else None
        return dict(got, label="Open in " + got["app"]) if got else None

    def _rsvp(self, it, tz):
        """"RSVP: Team sync, Wed Nov 18, 2 pm" (the time words of the invite, or where to find them)."""
        what = "RSVP: " + (it.get("what") or "an invite")
        if it.get("all_day") and it.get("start_date"):
            return what + ", " + L.when_label(it, tz)
        if it.get("start_ts") is not None:
            d = local(it["start_ts"], tz)
            return f"{what}, {d:%a} {d:%b} {d.day}, {t12(d)}"
        return what + " · the time is in the email"

    # --- 0.21.0: security alerts and the connectors' group ---------------------------------------------------------
    def _security(self, lv, now, tz):
        """Security alerts (heads.py), newest first: who, the subject, when, the look-alike mark and where to open it.
        Always shown; nothing hides them but the owner's Done."""
        rows = []
        for it in lv.get("security") or []:
            f = it["from"][0] if it["from"] else {}
            m = self._read("SELECT * FROM messages WHERE id=?", (f["message"],)) if f.get("message") else []
            alert = heads.security_of(m[0]) if m else None
            src = f.get("source") or "gmail"
            link = reply_link(m[0]) if m else None
            ts = f.get("ts") or it["created_at"]
            d = local(ts, tz)
            rows.append({"id": it["id"], "who": it["who_name"] or it["who"] or "A sender", "what": it["what"],
                         "icon": SOURCES.get(src, ("mail", ""))[0], "source": SOURCES.get(src, ("", "Message"))[1],
                         "at": t12(d) if d.date() == local(now, tz).date() else ago(ts, now, tz),
                         "marks": heads.security_marks(alert), "ts": ts,
                         "link": dict(link, label="Open in " + link["app"]) if link else None})
        rows.sort(key=lambda r: -r["ts"])
        return rows

    def _feeds(self, now, tz):
        """"From your channels, feeds and reports": the connectors' newest items and the unexpired reports, newest
        first, with the source's name and the marks as before. None when the owner has no connectors or nothing is
        new."""
        if self.feeds is None:
            return None
        try:
            got = self.feeds() or {}
        except Exception as e:  # noqa: BLE001 (the page still draws)
            self.log(f"Callsheet Today: connectors list failed ({type(e).__name__})")
            return None
        fd, rp = got.get("feeds") or {}, got.get("reports") or {}
        rows = []
        for kind, src in (("feed", fd), ("report", rp)):
            for x in src.get("items") or []:
                if not isinstance(x, dict) or not x.get("title"):
                    continue
                try:
                    ts = dt.datetime.fromisoformat(str(x.get("when")).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = 0.0
                rows.append({"key": f"{kind}:{x.get('id')}", "kind": kind, "title": x["title"],
                             "source": x.get("label") or "", "noun": x.get("noun") or ("Report" if kind == "report" else ""),
                             "excerpt": x.get("excerpt") or "", "url": "" if x.get("flagged") else (x.get("url") or ""),
                             "flagged": bool(x.get("flagged")), "ts": ts,
                             "when": ago(ts, now, tz) if ts else "",
                             "marks": [m for m in x.get("marks") or [] if isinstance(m, list) and len(m) == 2],
                             "tell": x.get("tell") or "important"})
        if not rows:
            return None
        rows.sort(key=lambda r: (-r["ts"], r["key"]))
        more = int(fd.get("more") or 0) + int(rp.get("more") or 0)
        return {"total": len(rows) + more, "rows": rows, "shown": FEEDS_SHOWN, "more": more,
                "note": f"and {more} more. Ask Chat to find them." if more else ""}

    @staticmethod
    def _about(side):
        return side.get("for") or side.get("what") or side.get("title") or ""

    def _clash_sides(self, rows, now, tz):
        ids = set()
        for c in rows:
            for k, r in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"])):
                if k == "item":
                    ids.add(r)
        items = self._items(ids)
        items.update(self._items([it["predecessor"] for it in items.values() if it.get("predecessor")]))
        srcs = self._srcs(ids)
        out = {}
        for c in rows:
            group = L.group_entries(c)
            if group:                        # 0.22.x: the owner's trip, then every calendar entry of its grouped clash in
                out[c["id"]] = [self._side(c["a_kind"], c["a_ref"], items, srcs, now, tz)] + [  # time order (the
                    self._side("event", e, items, srcs, now, tz) for e in group]                # Agenda, reply draft)
                continue
            sides = [self._side(c["a_kind"], c["a_ref"], items, srcs, now, tz),
                     self._side(c["b_kind"], c["b_ref"], items, srcs, now, tz)]
            sides.sort(key=lambda s: (s["kind"] != "event", not s.get("mine"), -(s.get("ts") or 0)))
            out[c["id"]] = sides
        return out

    def _followups(self):
        return self._read("SELECT * FROM ledger_followups WHERE state='open' ORDER BY id")

    def _clash(self, c, sides, fu, rems, now, tz, expand):
        secs = (c["slot_start"] or now) - now
        live = c["slot_end"] is not None and c["slot_start"] is not None and c["slot_start"] <= now < c["slot_end"]
        level = "red" if live or 0 <= secs < RED_S else "amber" if secs < WEEK_S else "far"
        tell = None
        if fu:
            tell = next((s for s in sides if s["kind"] == "item" and s["id"] == fu["item"]), None)
        rows = []
        for s in sides:
            if s["kind"] == "event":
                rows.append({"icon": s["icon"], "chip": s["chip"],
                             "text": seg((s["title"], True), ((", " + s["until"]) if s["until"] else "", False)),
                             "age": "On calendar"})
            elif fu and s is tell:
                rows.append({"icon": s["icon"], "chip": s["chip"],
                             "text": seg((s["first"], True), (" hasn’t heard from you: " + self._about(s) + ".", False)),
                             "age": "asked " + ago(s["ts"], now, tz).replace(" ago", "")})
            elif s.get("mine"):
                word = "You told " if fu else ""
                when = ""
                if fu and c["slot_start"]:
                    d = local(c["slot_start"], tz)
                    when = f", {d:%a} {t12(d)}"
                rows.append({"icon": s["icon"], "chip": s["chip"], "yes": True,
                             "text": seg((word, False), (s["first"], True),
                                         ((" yes: " if fu else ": you said yes, ") + self._about(s) + when, False)),
                             "age": t12(local(s["yes_ts"], tz)) if fu and s.get("yes_ts") else ago(s["ts"], now, tz)})
            else:
                rows.append({"icon": s["icon"], "chip": s["chip"],
                             "text": seg((s["first"], True), (": " + self._about(s), False)), "age": ago(s["ts"], now, tz)})
        asks = [s for s in sides if s["kind"] == "item" and s.get("type") == "ask" and s.get("status") == "open"]
        target = tell or (min(asks, key=lambda s: s.get("ts") or 0) if asks else None) or \
            next((s for s in sides if s.get("mine")), None)
        reply = None
        if target is not None and target.get("link"):
            verb = "Reply to " if target.get("type") == "ask" else "Message "
            reply = dict(target["link"], label=verb + target["first"], who=target["first"])
        rem = None
        for r in rems:
            if (fu and r["followup"] == fu["id"]) or r["conflict"] == c["id"]:
                rem = r
        out = {"id": c["id"], "level": level, "when": slot_word(c["slot_start"], now, tz) if c["slot_start"] else "",
               "until": "now" if live else until(secs), "seconds": int(secs), "kind": c["kind"],
               "confidence": c["confidence"], "compact": not expand, "rows": rows, "reply": reply,
               "names": [s["first"] for s in sides],
               "line": seg(*[p for s in sides for p in ((s["first"], True), (": " + (self._about(s) or "busy"), False),
                                                         (" · ", False))][:-1]),
               "alert": {"state": c.get("alert") or "", "at": t12(local(c["alerted_at"], tz)) if c.get("alerted_at") else ""},
               "items": [s["id"] for s in sides if s["kind"] == "item"]}
        if c["kind"] == "away":
            out["away"] = c.get("why") or "you may be away or off that day"
        g = self._group(c, sides, now, tz)
        if g:                              # 0.22.x: the owner's trip's one grouped clash: one row, its entries when expanded
            out.update(group=g, line=g["title"], names=[g["plain"]], reply=None)
        if fu:
            out["followup"] = {"id": fu["id"], "who": tell["first"] if tell else first(fu["tell_name"]),
                               "state": "open"}
            if rem and rem["state"] == "sent":
                out["followup"]["note"] = f"{out['followup']['who']} still doesn’t know you’re busy then."
                out["followup"]["reminded"] = t12(local(rem["sent_at"], tz))
            elif rem and rem["state"] == "open":
                out["followup"]["remind_at"] = self._due_word(rem["due_ts"], now, tz)
        return out

    def _group(self, c, sides, now, tz):
        """0.22.x: the owner's trip or stay against the busy calendar entries during it, one clash (conflicts.check_item):
        {title: "Your trip to Tromsø overlaps 3 entries", plain, count, entries: [{id, icon, chip, text, age}]} with
        each entry's title and 12-hour time, or None for a clash between two sides."""
        ids = L.group_entries(c)
        if not ids:
            return None
        rows = {r["id"]: r for r in self._read(
            f"SELECT * FROM events WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
        trip = next((s for s in sides if s["kind"] == "item"), None) or {}
        what = re.sub(r"^(?:your|my|his)\s+", "", str(trip.get("what") or "").strip(), flags=re.I) or "trip"
        if _TRIP_WORD.match(what):
            what = what[:1].lower() + what[1:]
        n = len(ids)
        count = f" overlaps {n} {'entry' if n == 1 else 'entries'}"
        entries = []
        for i in ids:
            e = rows.get(i)
            if e is None:
                continue
            when = slot_word(e["start_ts"], now, tz) if e["start_ts"] is not None else ""
            if when and e["end_ts"] and e["end_ts"] > e["start_ts"]:
                when += f" to {t12(local(e['end_ts'], tz))}" if local(e["end_ts"], tz).date() == \
                    local(e["start_ts"], tz).date() else f" to {slot_word(e['end_ts'], now, tz)}"
            entries.append({"id": i, "icon": "cal", "chip": "Calendar", "title": e["title"] or "Calendar entry",
                            "when": when, "text": seg((e["title"] or "Calendar entry", True),
                                                      ((", " + when) if when else "", False)),
                            "age": "On calendar"})
        return {"title": seg(("Your " + what, True), (count, False)), "plain": f"Your {what}{count}", "count": n,
                "trip": trip.get("id"), "entries": entries}

    def _due_word(self, ts, now, tz):
        d = local(ts, tz)
        return t12(d) if d.date() == local(now, tz).date() else f"{day_word(d.date(), local(now, tz).date())} {t12(d)}"

    # --- the page ---------------------------------------------------------------------------------------------------
    def view(self, now=None):
        """GET api/today: header, now_next, needs_you, coming_up, clean_up, notes."""
        now = self.clock() if now is None else now
        tz = self.tz()
        n = local(now, tz)
        today = n.date()
        day0, day1 = L.day_bounds(today, tz)
        rows = [c for c in self._read("SELECT * FROM ledger_conflicts WHERE state='open' ORDER BY slot_start")
                if (c["slot_end"] or c["slot_start"] or now) > now]
        rows, left_out = self._clash_rule(rows, now, tz)     # 0.21.4: no promo, closed or started side
        sides = self._clash_sides(rows, now, tz)
        fus = self._followups()
        rems = self._reminders()
        clashes, busy_items, busy_events = [], set(), set()
        for c in sorted(rows, key=lambda c: c["slot_start"] or 0):
            ss = sides[c["id"]]
            ids = {s["id"] for s in ss if s["kind"] == "item"}
            fu = next((f for f in fus if f["conflict"] == c["id"] or f["item"] in ids), None)
            secs = (c["slot_start"] or now) - now
            clashes.append(self._clash(c, ss, fu, rems, now, tz, expand=bool(fu) or secs < EXPAND_S))
            busy_items |= ids
            busy_events |= {s["id"] for s in ss if s["kind"] == "event"}
        clashes = self._clash_cap(clashes, sides)          # 0.21.4: at most CLASH_PER_ITEM per item, the rest grouped
        lead = next((c for c in clashes if c["level"] == "red"), clashes[0] if clashes else None)
        for c in clashes:
            c["lead"] = c is lead
        in_clash = {f["id"] for c in clashes if c.get("followup") for f in [c["followup"]]}
        loose = []
        for f in fus:
            if f["id"] in in_clash:
                continue
            it = self._item(f["item"])
            if not it:
                continue
            rem = next((r for r in rems if r["followup"] == f["id"]), None)
            loose.append({"id": f["id"], "item": f["item"], "who": first(f["tell_name"] or f["tell"]),
                          "reason": f["reason"] or "", "what": it.get("what") or "",
                          # 0.22.0: nobody to tell (chase a past-due invoice): the reason is the line
                          "line": (C.chase_line(it, now, tz) or f["reason"] or "") if not (f["tell"] or f["tell_name"])
                          else "",
                          "remind_at": self._due_word(rem["due_ts"], now, tz) if rem and rem["state"] == "open" else "",
                          "reminded": t12(local(rem["sent_at"], tz)) if rem and rem["state"] == "sent" else ""})
        hidden = self._hidden(now)
        lv = self.lv.view(now=now, horizon_days=HORIZON_DAYS)
        return {"now": n.isoformat(timespec="minutes"),
                "header": {"time": t12(n), "date_long": f"{n:%A}, {n:%B} {n.day}", "date_short": f"{n:%a}, {n:%b} {n.day}",
                           "fresh": self._fresh(), "updated": self._updated(now)},
                "banner": self._banner(),
                "now_next": {"security": self._security(lv, now, tz),
                             "clashes": clashes, "followups": loose, "cleared": self._cleared(day0, now, tz),
                             "clashes_left_out": left_out,
                             "timeline": self._timeline(day0, day1, now, tz, busy_events, busy_items, lv)},
                "needs_you": self._needs(lv, busy_items | hidden, now, tz),
                "coming_up": self._coming(lv, busy_items, busy_events, now, tz),
                "clean_up": self._clean(lv, hidden, now, tz, samples=3),
                "feeds": self._feeds(now, tz),
                "calendar_write": False}

    # --- 0.21.4: the clash rule, the banner, Refresh -------------------------------------------------------------
    def _clash_rule(self, rows, now, tz):
        """Open clashes Today shows: none where either side is a Clean up line, closed or already started
        (tidy.clash_ok). Returns (rows, how many were left out); the items stay in their own sections."""
        ids = {c[k + "_ref"] for c in rows for k in ("a", "b") if c[k + "_kind"] == "item"}
        items = self._items(ids)
        keep = [c for c in rows if all(TY.clash_ok(items.get(c[k + "_ref"]), now, tz)
                                       for k in ("a", "b") if c[k + "_kind"] == "item")]
        return keep, len(rows) - len(keep)

    def _clash_cap(self, clashes, sides):
        """At most CLASH_PER_ITEM clashes per item (sure first, then soonest); the rest of one item's clashes become one
        grouped line ("12 more clashes with …") that opens to list them."""
        per, out, rest = {}, [], {}
        order = sorted(clashes, key=lambda c: (c["confidence"] != "sure", c["seconds"]))
        for c in order:
            key = next(iter(c["items"]), None)
            many = [i for i in c["items"] if sum(1 for x in clashes if i in x["items"]) > TY.CLASH_PER_ITEM]
            key = many[0] if many else key
            if key is None or not many:
                out.append(c)
                continue
            per[key] = per.get(key, 0) + 1
            (out if per[key] <= TY.CLASH_PER_ITEM else rest.setdefault(key, [])).append(c)
        for key, cs in rest.items():
            side = next((s for c in cs for s in sides.get(c["id"], []) if s["kind"] == "item" and s["id"] == key), {})
            what = self._about(side) if side else "one item"
            n = len(cs)
            g = {"title": seg((f"{n} more clash{'' if n == 1 else 'es'}", True), (f" with {what}", False)),
                 "plain": f"{n} more clash{'' if n == 1 else 'es'} with {what}", "count": n, "trip": None,
                 "entries": [{"id": c["id"], "icon": "cal", "chip": c["when"], "text": c["line"], "age": c["until"]}
                             for c in cs]}
            first_c = min(cs, key=lambda c: c["seconds"])
            out.append(dict(first_c, group=g, more=True, compact=True, line=g["title"], names=[g["plain"]], reply=None,
                            followup=None, lead=False))
        out.sort(key=lambda c: c["seconds"])
        return out

    def _banner(self):
        """0.21.4: the one-time Tidy up offer after the update ({count, text}), or None."""
        if self.tidy is None:
            return None
        try:
            return self.tidy.banner()
        except Exception as e:  # noqa: BLE001 (Today always shows)
            self.log(f"Callsheet today: tidy banner unavailable ({type(e).__name__})")
            return None

    def _updated(self, now):
        """'Updated just now' after the owner's Refresh, then 'Updated 9:05 am'; '' before any."""
        if not self.refreshed_at:
            return ""
        s = now - self.refreshed_at
        if s < 90:
            return "Updated just now"
        if s < 3600:
            return f"Updated {int(s // 60)} min ago"
        return f"Updated {t12(local(self.refreshed_at, self.tz()))}"

    def refresh(self, now=None):
        """0.21.4 POST api/today/refresh: rebuild Today from what is already read (no mail is read again) and note the
        time; api.App starts a fresh day's brief beside it."""
        now = self.clock() if now is None else now
        self.refreshed_at = now
        return self.view(now)

    def _fresh(self):
        try:
            r = self.reader() or {}
        except Exception:  # noqa: BLE001 (the page still draws)
            r = {}
        n = int(r.get("backlog") or 0)
        if n:
            return {"word": f"Reading {n} new", "state": "busy"}
        return {"word": "Up to date", "state": "ok"}

    def _cleared(self, day0, now, tz):
        """The newest thing that cleared itself today ('Cleared 12:14 pm · you wrote to Marco')."""
        best = None
        for f in self._read("SELECT * FROM ledger_followups WHERE state='cleared' AND cleared_at >= ?", (day0,)):
            if not (f["tell"] or f["tell_name"]):
                why = "it’s done"                          # 0.22.0: a chase follow-up (nobody to tell): paid
            elif f["event"] and f["cleared_message"] is None:
                why = "the calendar entry was updated"     # 0.22.0: a stale entry moved to the new time
            else:
                why = f"you wrote to {first(f['tell_name'] or f['tell'])}"
            best = max(best or (0, ""), (f["cleared_at"], why))
        for c in self._read("SELECT * FROM ledger_conflicts WHERE state='resolved' AND resolved_at >= ?", (day0,)):
            why = self._why_cleared(c, now)
            if why:
                best = max(best or (0, ""), (c["resolved_at"], why))
        if not best:
            return []
        return [{"at": t12(local(best[0], tz)), "why": best[1]}]

    def _why_cleared(self, c, now):
        w = c["resolved_why"] or ""
        if w == "dismissed":
            return ""                      # the owner's own press: no need to tell the owner
        if w == "event cancelled":
            return "the calendar entry was cancelled"
        if w == "grouped":
            return ""                      # 0.22.x: folded into the trip's one grouped clash
        if w == "entries cleared":
            return "nothing on your calendar during the trip now"
        for k, ref in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"])):
            if k != "item":
                continue
            h = self._read("SELECT h.message, m.direction, m.recipients, m.sender_name FROM ledger_history h LEFT JOIN "
                           "messages m ON m.id=h.message WHERE h.item=? AND h.at >= ? ORDER BY h.id DESC LIMIT 1",
                           (ref, (c["resolved_at"] or now) - 5))
            if h and h[0]["direction"] == "out":
                names = [r.get("name") for r in _loads(h[0]["recipients"], []) if isinstance(r, dict) and r.get("name")]
                return f"you wrote to {first(names[0])}" if names else "you answered"
            if h and h[0]["direction"] == "in":
                it = self._item(ref) or {}
                return f"{first(it.get('who_name') or h[0]['sender_name'])} changed it"
        return {"moved away": "it moved", "declined": "you said no", "done": "it’s done",
                "cancelled": "it was cancelled"}.get(w, "")

    def _timeline(self, day0, day1, now, tz, busy_events, busy_items, lv):
        evs = [e for e in self.ms.events(since=day0, until=day1) if not e["all_day"] and e["start_ts"] is not None]
        spans = [(e["start_ts"], e["end_ts"] or e["start_ts"] + 3600) for e in evs]
        rows, done = [], 0
        entries = []
        for e in evs:
            end = e["end_ts"] or e["start_ts"] + 3600
            if end <= now:
                done += 1
                continue
            if e["id"] in busy_events:
                continue
            entries.append((e["start_ts"], end, e["title"], e.get("location") or "", e.get("link") or "", None))
        for it in lv["now_next"] + lv["coming_up"]:
            if it["id"] in busy_items or it["when"]["all_day"] or not it["when"]["start"]:
                continue
            s = dt.datetime.fromisoformat(it["when"]["start"]).timestamp()
            e = dt.datetime.fromisoformat(it["when"]["end"]).timestamp() if it["when"]["end"] else s + 3600
            if day0 <= s < day1 and e > now and not it.get("cal_ref"):
                src = (it["from"][0]["source"] if it["from"] else "")
                entries.append((s, e, it["what"], "", "", src))
                spans.append((s, e))
        entries.sort()
        busy_until = now
        for s, e, title, loc, link, src in entries:
            free = s - max(busy_until, now)
            if rows and free >= 3600:
                rows.append({"kind": "gap", "text": "Free for " + self._dur(free)})
            elif not rows and done and free >= 3600:
                rows.append({"kind": "gap", "text": "Free for " + self._dur(free), "done": done})
                done = 0
            d0, d1 = local(s, tz), local(e, tz)
            if s <= now:
                meta = f"On now · until {t12(d1)}"
            elif s - now <= 3600:
                meta = f"In {max(1, int((s - now) // 60))} min"
            else:
                meta = self._dur(e - s) if e - s < 3600 else f"Until {t12(d1)}"
            if _VIDEO.search(loc + " " + link):
                meta += " · video"
            if src:
                meta += " · " + FROM.get(src, "From a message").lower()
            rows.append({"kind": "event", "time": t12(d0), "title": title, "meta": meta, "on": s <= now})
            busy_until = max(busy_until, e)
            for a, b in spans:          # what's hidden (a clash side) still takes its time
                if a <= busy_until < b:
                    busy_until = b
        if done:
            rows.insert(0, {"kind": "gap", "text": "", "done": done})
        return rows

    @staticmethod
    def _dur(sec):
        m = int(round(sec / 60))
        h, m = divmod(m, 60)
        return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"

    def _needs(self, lv, skip, now, tz):
        rows, older = [], []
        res = self.area_res()   # 0.22.0: each row's area (its first message's connection then)
        osrc = self._srcs([it["id"] for it in lv["needs_you"] if it["id"] not in skip]) if res is not None else {}
        # 0.21.4 (tidy.older_of): long-past asks and quiet items from the first catch-up go to "Older, still open?"
        back = TY.backfill_before(self.ms)
        quiet = TY.quiet_items(self.ms, [it["id"] for it in lv["needs_you"] if it["id"] not in skip and (
            (it["from"][0].get("ts") if it["from"] else None) or it["created_at"]) < back]) if back else set()
        for it in lv["needs_you"]:
            if it["id"] in skip:
                continue
            f = it["from"][0] if it["from"] else {}
            src = f.get("source") or "gmail"
            mine = it["type"] == "commitment"
            who = it["who_name"] if it["who"] != L.OWNER else ""
            if not who:
                full = self._item(it["id"])
                who = self._person(full, self._srcs([it["id"]]).get(it["id"], [])) if full else ""
            row = {"id": it["id"], "icon": SOURCES.get(src, ("mail", ""))[0], "source": SOURCES.get(src, ("", "Message"))[1],
                   "who": who or "Someone", "age": waited(now - (f.get("ts") or it["created_at"])),
                   "why": it["purpose"] or it["what"], "mine": mine, "likely": it["confidence"] != "sure",
                   "never_hide": it["never_hide"], "seconds": int(now - (f.get("ts") or it["created_at"])),
                   "vip": bool(it.get("vip")), "invite": None, "late": "", "amount": "",
                   "area": self.area_of(res, self._origin(osrc.get(it["id"], [])))}
            came = next((c.get("why") or "" for c in it.get("checks") or [] if isinstance(c, dict) and
                         c.get("field") == "back"), "")
            if came:                                       # 0.22.1: back from Clean up, saying why ("Sam wrote about it")
                row["back"] = came
            if it.get("late"):                             # 0.22.0: a passed deadline (a past-due bill): urgent, late
                row.update(late=it["late"]["word"], amount=it.get("amount") or "", urgent=True)
            if it.get("subtype") == "invite":              # 0.21.0 (heads.py): an invite to answer
                full = self._item(it["id"]) or {}
                cal = full.get("cal_ref") is not None
                row.update(why=self._rsvp(full, tz), likely=False, never_hide=True,
                           who=it["who_name"] or ("Calendar invite" if cal else row["who"]),
                           invite={"link": self._invite_link(full, self._srcs([it["id"]]).get(it["id"], [])),
                                   "repeats": full.get("purpose") == "repeats"})
                if cal:
                    row.update(icon="cal", source="Calendar")
            why_old = TY.older_of(it, now, back, it["id"] in quiet)
            if why_old:
                older.append(dict(row, old_why=why_old))
                continue
            rows.append(row)
        # 0.20.0: a VIP's asks first (0.17.0 rules), 0.22.0: then what is late, then oldest
        rows.sort(key=lambda r: (not r["vip"], not r["late"], -r["seconds"]))
        older.sort(key=lambda r: r["seconds"])
        return {"total": len(rows), "rows": rows,
                "older": {"total": len(older), "rows": older, "ids": [r["id"] for r in older],
                          "label": "Older, still open?",
                          "note": f"Asks and items more than {TY.AGE_DAYS} days old, or from your first catch-up with "
                                  "nothing new since. Still open and still found in search."}}

    def _coming(self, lv, busy_items, busy_events, now, tz):
        today = local(now, tz).date()
        end_day = today + dt.timedelta(days=HORIZON_DAYS - 1)
        tomorrow0, _ = L.day_bounds(today + dt.timedelta(days=1), tz)
        _, horizon1 = L.day_bounds(end_day, tz)
        rows, cal_refs = [], set()
        for it in lv["coming_up"] + lv["now_next"]:
            if it["id"] in busy_items:
                continue
            w = it["when"]
            if w["all_day"] and w["start_date"]:
                d0 = dt.date.fromisoformat(w["start_date"])
                d1 = dt.date.fromisoformat(w["end_date"] or w["start_date"])
                timed = None
            elif w["start"]:
                s = dt.datetime.fromisoformat(w["start"])
                d0, timed = s.date(), s
                d1 = dt.datetime.fromisoformat(w["end"]).date() if w["end"] else d0
            else:
                continue                                   # no date: shown in the item list, not on the strip
            if d0 <= today or d0 > end_day:
                continue
            if it.get("cal_ref"):
                cal_refs.add(it["cal_ref"])
            f = it["from"][0] if it["from"] else {}
            src = f.get("source") or ""
            what = it["what"] + (f" · {t12(timed)}" if timed else "")
            rows.append({"d0": d0, "d1": d1, "ts": timed.timestamp() if timed else L.day_bounds(d0, tz)[0],
                         "what": what, "item": it["id"], "event": it.get("cal_ref"), "source": src,
                         "icon": SOURCES.get(src, ("", ""))[0], "from": FROM.get(src, ""),
                         "on_calendar": bool(it.get("cal_ref")), "likely": it["confidence"] != "sure"})
        for e in self.ms.events(since=tomorrow0, until=horizon1):
            if e["id"] in busy_events or e["id"] in cal_refs or e["start_ts"] is None or e["start_ts"] < tomorrow0:
                continue
            if e["all_day"] and e["start"]:
                d0 = dt.date.fromisoformat(e["start"][:10])
                d1 = dt.date.fromisoformat(e["end"][:10]) - dt.timedelta(days=1) if e["end"] else d0
                what = e["title"]
            else:
                s = local(e["start_ts"], tz)
                d0 = s.date()
                d1 = local(e["end_ts"], tz).date() if e["end_ts"] else d0
                what = f"{e['title']} · {t12(s)}"
            rows.append({"d0": d0, "d1": max(d0, d1), "ts": e["start_ts"], "what": what, "item": None, "event": e["id"],
                         "source": "calendar", "icon": "", "from": "", "on_calendar": True, "likely": False})
        rows += self._birthdays(rows, tomorrow0, today, end_day, tz)
        rows.sort(key=lambda r: (r["ts"], r["what"]))
        marks = [""] * HORIZON_DAYS
        marks[0] = "td"
        for r in rows:
            i = (r["d0"] - today).days
            if 0 <= i < HORIZON_DAYS:
                marks[i] = "ev"
            for k in range(i + 1, min(HORIZON_DAYS, (r["d1"] - today).days + 1)):
                if k > 0 and not marks[k]:
                    marks[k] = "tr"
        weeks, i = [], 0
        while i < HORIZON_DAYS:
            d = today + dt.timedelta(days=i)
            n = 7 - d.weekday() if i == 0 else 7
            n = min(n, HORIZON_DAYS - i)
            weeks.append({"col": i + 1, "span": n, "label": "Today" if i == 0 else f"{d:%b} {d.day}"})
            i += n
        sunday = today + dt.timedelta(days=6 - today.weekday())
        groups, cur = [], None
        for r in rows:
            g = "this" if r["d0"] <= sunday else "next" if r["d0"] <= sunday + dt.timedelta(days=7) else "later"
            if cur is None or cur["key"] != g:
                if g == "this":
                    rng = f"to {short_day(sunday)}"
                elif g == "next":
                    a, b = sunday + dt.timedelta(days=1), sunday + dt.timedelta(days=7)
                    rng = f"{a:%b} {a.day} to {b.day}" if a.month == b.month else f"{a:%b} {a.day} to {b:%b} {b.day}"
                else:
                    rng = f"to {end_day:%b} {end_day.day}"
                cur = {"key": g, "label": {"this": "This week", "next": "Next week", "later": "Later"}[g],
                       "range": rng, "rows": []}
                groups.append(cur)
            span_txt = ""
            if r["d1"] > r["d0"] and r["source"] == "calendar":
                span_txt = f" · to {short_day(r['d1'])}"
            cur["rows"].append({"rel": rel_day(r["d0"], today), "date": short_day(r["d0"]), "what": r["what"] + span_txt,
                                "item": r["item"], "event": r["event"], "icon": r["icon"], "from": r["from"],
                                "on_calendar": r["on_calendar"], "likely": r["likely"],
                                "add": None if r["on_calendar"] or r.get("birthday") else
                                {"on": False, "why": "needs calendar access"}, "birthday": bool(r.get("birthday"))})
        last = rows[-1]["d0"] if rows else None
        nearest = rel_day(rows[0]["d0"], today) if rows else ""
        can_add, targets = False, []
        if self.cal is not None:      # 0.21.0: Add to calendar, per item row; "On your calendar ✓" once added
            try:
                st = self.cal.states([r["item"] for g in groups for r in g["rows"] if r["item"] is not None])
                targets = self.cal.targets()
                can_add = bool(targets)
            except Exception as e:  # noqa: BLE001 (Today always shows; the button stays off)
                self.log(f"Callsheet today: add-to-calendar state unavailable ({type(e).__name__})")
                st = {}
            for g in groups:
                for r in g["rows"]:
                    s = st.get(r["item"]) if r["item"] is not None else None
                    if s is None:
                        continue
                    if "added" in s:
                        r["add"], r["added"], r["on_calendar"] = None, s["added"], True
                    elif "add" in s and r["add"] is not None:
                        r["add"] = s["add"]
        return {"total": len(rows), "marks": marks, "weeks": weeks, "groups": groups,
                "can_add": can_add, "cal_targets": targets,
                "last": f"{last:%b} {last.day}" if last else "", "nearest": nearest,
                "summary": f"{len(rows)} things in the next {HORIZON_DAYS} days" + (f", the nearest {nearest}." if rows else ".")}

    def _birthdays(self, rows, tomorrow0, today, end_day, tz):
        """0.21.0: contacts' birthdays as Coming up rows (yearly; source "contacts"), unless the calendar or the ledger
        already has that birthday on that day."""
        if self.contacts is None:
            return []
        try:
            bdays = self.contacts.birthdays(today + dt.timedelta(days=1), end_day)
        except Exception as e:  # noqa: BLE001 (Today always shows)
            self.log(f"Callsheet today: birthdays unavailable ({type(e).__name__})")
            return []
        out = []
        for b in bdays:
            fn = (b["name"].split() or [""])[0].lower()
            if any(r["d0"] == b["date"] and fn and fn in r["what"].lower() and "birthday" in r["what"].lower()
                   for r in rows):
                continue                                   # Google Calendar's Birthdays, or a message about it
            out.append({"d0": b["date"], "d1": b["date"], "ts": L.day_bounds(b["date"], tz)[0],
                        "what": f"{b['name']}’s birthday", "item": None, "event": None, "source": "contacts",
                        "icon": SOURCES["contacts"][0], "from": FROM["contacts"], "on_calendar": False,
                        "likely": False, "birthday": True})
        return out

    def _clean(self, lv, hidden, now, tz, samples=None):
        groups = {k: [] for k, _, _ in CLEAN_GROUPS}
        for it in lv["clean_up"]:
            if it["id"] in hidden:
                continue
            f = it["from"][0] if it["from"] else {}
            src = f.get("source") or "gmail"
            g = clean_group(it, src)
            d = local(f.get("ts") or it["created_at"], tz)
            groups[g].append({"id": it["id"], "what": it["what"], "icon": dict((k, i) for k, _, i in CLEAN_GROUPS)[g],
                              "source": src, "day": ago(f.get("ts") or it["created_at"], now, tz) if (now - d.timestamp()) >= 86400 or d.date() != local(now, tz).date() else t12(d),
                              "ts": f.get("ts") or it["created_at"]})
        out, ids = [], []
        for k, label, icon in CLEAN_GROUPS:
            rows = sorted(groups[k], key=lambda r: -r["ts"])
            if rows:
                out.append({"key": k, "label": label, "icon": icon, "rows": rows})
                ids += [r["id"] for r in rows]
        smp = [g["rows"][0] for g in out][:samples] if samples else None
        oldest = min((r["ts"] for g in out for r in g["rows"]), default=now)
        days = (local(now, tz).date() - local(oldest, tz).date()).days + 1
        return {"total": len(ids), "ids": ids, "groups": out if not samples else [], "samples": smp or [],
                "since": f"From the last {days} days" if days > 1 else "From today"}

    # --- the clean-up page ----------------------------------------------------------------------------------------------
    def cleanup(self, now=None):
        now = self.clock() if now is None else now
        tz = self.tz()
        lv = self.lv.view(now=now, horizon_days=HORIZON_DAYS)
        out = self._clean(lv, self._hidden(now), now, tz)
        out["notes"] = self.notes(now)
        return out

    def notes(self, now=None):
        now = self.clock() if now is None else now
        tz = self.tz()
        today = local(now, tz).date()
        out = []
        for r in self._read("SELECT * FROM today_notes ORDER BY at DESC, id DESC LIMIT 30"):
            chips = [CHIPS[c] for c in _loads(r["chips"], []) if c in CHIPS]
            said = "; ".join(chips + ([r["text"]] if r["text"] else []))
            d = local(r["at"], tz).date()
            out.append({"id": r["id"], "said": said, "about": r["label"],
                        "at": "today" if d == today else f"{d:%b} {d.day}", "learned": bool(r["learned"])})
        return out

    # --- the item and clash views ------------------------------------------------------------------------------------
    def item_view(self, item_id, now=None):
        now = self.clock() if now is None else now
        tz = self.tz()
        it = self._item(item_id)
        if it is None:
            return None
        srcs = self._srcs([it["id"]])
        items = {it["id"]: it}
        if it.get("predecessor"):
            items.update(self._items([it["predecessor"]]))
        side = self._side("item", it["id"], items, srcs, now, tz)
        clashes = self._read("SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref=?) OR "
                             "(b_kind='item' AND b_ref=?)) ORDER BY slot_start", (it["id"], it["id"]))
        sp = self._span(it)
        kicker = [t12(local(now, tz)), TYPE_WORD.get(it["type"], "Item")]
        when = None
        if sp:
            label = self._when_word(it, sp, tz)
            kicker.append(label)
            when = {"label": label, "sub": self._when_sub([side], sp, tz)}
        name = side["name"]
        title = f"{name}: {it['what']}" if name and it["who"] != L.OWNER else it["what"]
        fus = [f for f in self._followups() if f["item"] == it["id"]]
        rems = [r for r in self._reminders("open") if r["item"] == it["id"] or (fus and r["followup"] == fus[0]["id"])]
        out = {"kind": "item", "id": it["id"], "type": it["type"], "status": it["status"], "kicker": kicker,
               "title": title, "until": until(sp[0] - now) if sp and sp[0] > now else "",
               "stand": [self._stand(side, now, tz)], "when": when,
               "reply": (side["link"] if it.get("subtype") == "invite" else
                         dict(side["link"], label=("Reply to " if it["type"] == "ask" else "Message ") + side["first"]))
               if side.get("link") and side["first"] else None,
               "remind_at": self._due_word(rems[-1]["due_ts"], now, tz) if rems else "",
               "followup": {"id": fus[0]["id"], "who": first(fus[0]["tell_name"])} if fus else None,
               "clashes": [{"id": c["id"], "when": slot_word(c["slot_start"], now, tz) if c["slot_start"] else ""}
                           for c in clashes],
               "confidence": it["confidence"], "likely_from": self._likely(it, srcs.get(it["id"], [])),
               "threads": [self._thread(side, items, srcs, now, tz)],
               "context": self._context(it, side, srcs.get(it["id"], []), now, tz),
               "chat": self._chat_sugs([side], sp, tz), "open": it["status"] not in L.CLOSED}
        out["threads"] = [t for t in out["threads"] if t]
        out["add"] = out["added"] = None   # 0.21.0 Add to calendar (calwrite.py): only for an item with a time
        if self.cal is not None and sp:
            try:
                s = self.cal.states([it["id"]]).get(it["id"]) or {}
                out["add"], out["added"] = s.get("add"), s.get("added")
                out["cal_targets"] = self.cal.targets() if out["add"] else []
            except Exception as e:  # noqa: BLE001
                self.log(f"Callsheet today: add-to-calendar state unavailable ({type(e).__name__})")
        return out

    def clash_view(self, cid, now=None):
        now = self.clock() if now is None else now
        tz = self.tz()
        rows = self._read("SELECT * FROM ledger_conflicts WHERE id=?", (int(cid),))
        if not rows:
            return None
        c = rows[0]
        sides = self._clash_sides([c], now, tz)[c["id"]]
        ids = [s["id"] for s in sides if s["kind"] == "item"]
        items = self._items(ids)
        items.update(self._items([it["predecessor"] for it in items.values() if it.get("predecessor")]))
        srcs = self._srcs(list(items))
        fu = next((f for f in self._followups() if f["conflict"] == c["id"] or f["item"] in ids), None)
        rems = self._reminders()
        card = self._clash(c, sides, fu, rems, now, tz, True) if c["state"] == "open" else None
        names = [s["first"] for s in sides]

        def noun(s):
            if s["kind"] == "event":
                return s["title"]
            word = s["subtype"] if s["subtype"] and s["subtype"] not in ("other", "help") else (
                "plan" if s.get("mine") else "ask")
            return f"{s['first']}’s {word}"
        tell = next((s for s in sides if fu and s["kind"] == "item" and s["id"] == fu["item"]), None)
        reply = card["reply"] if card else None
        out = {"kind": "clash", "id": c["id"], "state": c["state"], "resolved_why": c["resolved_why"],
               "kicker": [t12(local(now, tz)), "Clash", slot_word(c["slot_start"], now, tz) if c["slot_start"] else ""],
               "title": " and ".join(noun(s) for s in sides),
               "until": until(c["slot_start"] - now) if c["slot_start"] and c["slot_start"] > now else "",
               "level": card["level"] if card else "",
               "stand": [self._stand(s, now, tz) for s in sides],
               "when": {"label": L.when_label({"start_ts": c["slot_start"]}, tz).replace(" 7 pm", " 7:00 pm")
                        if False else self._slot_label(c, tz), "sub": self._when_sub(sides, (c["slot_start"], c["slot_end"]), tz)}
               if c["slot_start"] else None,
               "reply": reply, "followup": card.get("followup") if card else None,
               "threads": [t for t in (self._thread(s, items, srcs, now, tz) for s in sides if s["kind"] == "item") if t],
               "chat": self._chat_sugs(sides, (c["slot_start"], c["slot_end"]) if c["slot_start"] else None, tz,
                                       target=tell or (next((s for s in sides if card and card["reply"] and
                                                             s.get("first") == card["reply"]["who"]), None))),
               "names": names, "open": c["state"] == "open", "items": ids}
        g = card.get("group") if card else self._group(c, sides, now, tz)
        if g:                              # 0.22.x: the owner's trip and each calendar entry during it
            out["title"] = g["plain"]
            out["stand"] = [self._stand(s, now, tz) for s in sides if s["kind"] == "item"] + [
                {"badge": "On your calendar", "mark": "", "text": seg((e["title"], True)), "age": e["when"]}
                for e in g["entries"]]
            out["group"] = g
            if out["when"]:                # the entries are listed above: no "your calendar is clear" under them
                out["when"]["sub"] = self._when_sub(sides, None, tz, calendar=False)
        return out

    @staticmethod
    def _when_word(it, sp, tz):
        """'Thu Nov 12, 7:00 pm to 8:00 pm', 'About Thu Nov 12, 7:00 pm', 'Tue Dec 8 to Thu Dec 10' (12-hour)."""
        a, b = local(sp[0], tz), local(sp[1], tz) if sp[1] else None
        if it.get("all_day"):
            last = (b - dt.timedelta(seconds=1)).date() if b else a.date()
            out = short_day(a) if last == a.date() else f"{short_day(a)} to {short_day(last)}"
        else:
            out = f"{short_day(a)}, {t12(a)}"
            if b and b > a:
                out += f" to {t12(b)}" if b.date() == a.date() else f" to {short_day(b)}, {t12(b)}"
        return ("About " + out) if it.get("approx") else out

    @staticmethod
    def _slot_label(c, tz):
        d = local(c["slot_start"], tz)
        return f"{short_day(d)}, {t12(d)}"

    def _stand(self, s, now, tz):
        today = local(now, tz).date()
        if s["kind"] == "event":
            return {"badge": "On your calendar", "mark": "", "text": seg((s["title"], True)), "age": s["until"]}
        about = self._about(s)
        text = seg((s["first"], True), (": " + about, False)) if s["first"] else seg((about, False))
        st = s.get("status")
        if s.get("mine"):
            if st in ("declined", "cancelled"):
                return {"badge": "You said no", "mark": "", "text": text, "age": ""}
            yd = local(s["yes_ts"], tz) if s.get("yes_ts") else None
            return {"badge": "You said yes", "mark": "ok", "text": text,
                    "age": f"{day_word(yd.date(), today)} {t12(yd)}" if yd else ""}
        if st == "waiting_on_them":
            return {"badge": "Waiting on them", "mark": "", "text": text, "age": ""}
        if st in L.CLOSED:
            return {"badge": {"done": "Done", "declined": "You said no", "cancelled": "Cancelled"}[st], "mark": "ok",
                    "text": text, "age": ""}
        rep = self._replied(s)
        if rep:
            d = local(rep, tz)
            return {"badge": "You replied", "mark": "ok", "text": text, "age": f"{day_word(d.date(), today)} {t12(d)}"}
        return {"badge": "No reply yet", "mark": "wn", "text": text,
                "age": "asked " + ago(s["ts"], now, tz).replace(" ago", "") if s.get("ts") else ""}

    def _replied(self, s):
        """The time of the owner's first message in the item's thread after it was asked, or None."""
        if not s.get("ts"):
            return None
        srcs = self._srcs([s["id"]]).get(s["id"], [])
        for x in srcs:
            if x["thread"]:
                r = self._read("SELECT ts FROM messages WHERE source=? AND account=? AND thread=? AND direction='out' AND "
                               "ts > ? AND deleted_at IS NULL ORDER BY ts LIMIT 1", (x["source"], x["account"], x["thread"], s["ts"]))
                if r:
                    return r[0]["ts"]
        return None

    def _when_sub(self, sides, sp, tz, calendar=True):
        bits = []
        for s in sides:
            if s["kind"] == "item" and s.get("phrase"):
                bits.append(f"“{s['phrase']}” ({s['first']})" if s["first"] else f"“{s['phrase']}”")
        out = ("From " + " and ".join(bits) + ". ") if bits else ""
        if not calendar:
            return out.strip()
        return out + self._calendar_then(sp, tz, {s["id"] for s in sides if s["kind"] == "event"})

    def _calendar_then(self, sp, tz, skip=()):
        a, b = sp
        b = b or a + 3600
        d = local(a, tz).date()
        day0, day1 = L.day_bounds(d, tz)
        evs = [e for e in self.ms.events(since=day0, until=day1) if not e["all_day"] and e["start_ts"] is not None
               and e["id"] not in skip]
        over = [e for e in evs if e["start_ts"] < b and (e["end_ts"] or e["start_ts"] + 3600) > a]
        if over:
            return "On your calendar then: " + ", ".join(e["title"] for e in over[:2]) + "."
        before = [e for e in evs if (e["end_ts"] or e["start_ts"]) <= a]
        if before:
            last = max(before, key=lambda e: e["end_ts"] or e["start_ts"])
            return f"Your calendar is clear after {t12(local(last['end_ts'] or last['start_ts'], tz))}."
        return "Your calendar is clear that day."

    def _likely(self, it, srcs):
        if it["confidence"] == "sure":
            return ""
        o = self._origin(srcs) or {}
        what = o.get("subject") or (SOURCES.get(o.get("source"), ("", "a message"))[1] + " message")
        shown = self.ms.shown_name(o) if o.get("direction") == "in" else ""
        return f"Likely, from {first(shown or o.get('sender') or '')}’s “{what}”".replace("’s “", "’s “") \
            if o.get("direction") == "in" else f"Likely, from your “{what}”"

    def _thread(self, side, items, srcs, now, tz):
        """One side's conversation: the newest messages, the ask (ochre) and the owner's yes (teal) marked."""
        if side["kind"] != "item":
            return None
        it = items.get(side["id"]) or {}
        mine = list(srcs.get(side["id"], []))
        ask_ids, yes_ids = set(), set()
        for s in mine:
            if s["role"] == "created" and s["direction"] == "in" and it.get("type") in ("ask", "event"):
                ask_ids.add(s["id"])
            if s["direction"] == "out" and (it.get("type") == "commitment" or s["role"] == "updated"):
                yes_ids.add(s["id"])
        pred = it.get("predecessor")
        if pred:
            ps = self._srcs([pred]).get(pred, [])
            mine += ps
            ask_ids |= {s["id"] for s in ps if s["role"] == "created" and s["direction"] == "in"}
        if it.get("type") == "ask" and it.get("status") in ("done", "accepted"):
            for c in self._read("SELECT id FROM ledger_items WHERE predecessor=?", (it["id"],)):
                yes_ids |= {s["id"] for s in self._srcs([c["id"]]).get(c["id"], []) if s["direction"] == "out"}
        if not mine:
            return None
        o = next((s for s in mine if s["id"] in ask_ids), None) or self._origin(mine)
        rows = [r for r in self.ms.thread(o["source"], o["account"], o["thread"], THREAD_MAX) if not r.get("deleted_at")] \
            if o["thread"] else []
        if not rows:
            rows = self._read("SELECT * FROM messages WHERE id=?", (o["id"],))
        today = local(now, tz).date()
        msgs = []
        for r in rows:
            d = local(r["ts"], tz)
            n = (today - d.date()).days
            day = "Today" if n == 0 else "Yesterday" if n == 1 else f"{d:%A}" if 0 < n < 7 else f"{d:%a} {d:%b} {d.day}"
            text = clean_text(r["text"]) or r["subject"] or "(no text)"
            m = {"id": r["id"], "me": r["direction"] == "out", "who": "You" if r["direction"] == "out" else
                 first(r["sender_name"] or r["sender"]), "time": t12(d), "day": day, "text": text,
                 "mark": "ask" if r["id"] in ask_ids else "yes" if r["id"] in yes_ids else ""}
            if m["mark"] == "ask":
                m["hl"] = self._highlight(text, side)
            msgs.append(m)
        ask_at = next((i for i, m in enumerate(msgs) if m["mark"] == "ask"), None)
        start = max(0, len(msgs) - THREAD_SHOWN)
        if ask_at is not None and ask_at < start:
            start = max(0, ask_at - 1)
        last_in = msgs[-1] if msgs else None
        no_reply = bool(ask_at is not None and not any(m["me"] for m in msgs[ask_at + 1:]) and last_in and not last_in["me"]
                        and it.get("status") == "open")
        subj = o["subject"] if o["source"] == "gmail" else ""
        return {"item": side["id"], "who": side["name"], "first": side["first"], "source": o["source"],
                "icon": SOURCES.get(o["source"], ("mail", ""))[0], "chip": SOURCES.get(o["source"], ("", "Message"))[1],
                "subject": subj, "messages": msgs, "earlier": start, "end": "No reply from you yet" if no_reply else ""}

    @staticmethod
    def _highlight(text, side):
        """[start, end] of the words to underline in the ask: the time phrase, else nothing."""
        for p in (side.get("phrase"),):
            if p and len(p) >= 3:
                i = text.lower().find(p.lower())
                if i >= 0:
                    return [i, i + len(p)]
        return None

    def _context(self, it, side, srcs, now, tz):
        """Replies: what helps answer (the days asked about on the calendar, a call with them today, a later
        message from them in the thread)."""
        out = []
        sp = self._span(it)
        if sp:
            d0 = local(sp[0], tz).date()
            d1 = local(sp[1] - 1, tz).date() if sp[1] > sp[0] else d0
            n = min(5, (d1 - d0).days + 1)
            days = []
            for k in range(n):
                d = d0 + dt.timedelta(days=k)
                a, b = L.day_bounds(d, tz)
                evs = [e for e in self.ms.events(since=a, until=b) if (e.get("transparency") or "") != "transparent"]
                days.append({"wd": f"{d:%a}", "d": str(d.day),
                             "busy": "Free" if not evs else ("Busy" if len(evs) == 1 else f"{len(evs)} things")})
            label = short_day(d0)[4:] if n == 1 else (f"{d0:%b} {d0.day} to {d1.day}" if d0.month == d1.month
                                                      else f"{d0:%b} {d0.day} to {d1:%b} {d1.day}")
            out.append({"kind": "days", "label": label, "days": days})
        nm = (side.get("name") or "").lower()
        f = side.get("first", "").lower()
        if f and len(f) >= 3:
            a, b = L.day_bounds(local(now, tz).date(), tz)
            for e in self.ms.events(since=now, until=b):
                hay = (e["title"] + " " + json.dumps(e.get("attendees") or [])).lower()
                if f in hay or (nm and nm in hay):
                    s = local(e["start_ts"], tz)
                    out.append({"kind": "today", "label": "Today", "title": e["title"],
                                "sub": f"{t12(s)} · {until(e['start_ts'] - now)}"})
                    break
        if side.get("ts"):
            for s in srcs[:1]:
                if not s["thread"]:
                    continue
                later = self._read("SELECT ts, text, subject FROM messages WHERE source=? AND account=? AND thread=? AND "
                                   "direction='in' AND ts > ? AND deleted_at IS NULL ORDER BY ts DESC LIMIT 1",
                                   (s["source"], s["account"], s["thread"], side["ts"] + 60))
                if later:
                    t = clean_text(later[0]["text"]).split("\n")[0]
                    t = re.split(r"(?<=[.!?])\s", t)[0]
                    d = local(later[0]["ts"], tz)
                    out.append({"kind": "heads", "label": "Heads-up", "title": t if len(t) <= 70 else t[:68].rstrip() + "…",
                                "sub": f"From {side['first']}’s {day_word(d.date(), local(now, tz).date())} message"
                                .replace("’s today message", "’s message today")})
        return out[:3]

    def _chat_sugs(self, sides, sp, tz, target=None):
        people = [s for s in sides if s["kind"] == "item" and s.get("first")]
        t = target or next((s for s in people if s.get("type") == "ask" and s.get("status") == "open"), None) or \
            (people[0] if people else None)
        out = []
        if t:
            out.append(f"Draft a reply to {t['first']}")
            out.append(f"What does {t['first']} need from me?")
        if sp and sp[0]:
            out.append(f"What else is on {local(sp[0], tz):%A}?")
        return out[:3]

    def chat_scope(self, kind, ref):
        """The Chat scope of an item or a clash (the 0.7.0 project pattern): a name and the Chat ids of the messages in
        its threads (msgstore.STORE_ID_BASE + rowid). A filter on Chat's read-only tools, not a new tool."""
        import msgstore  # noqa: PLC0415
        if kind == "clash":
            v = self.clash_view(ref)
            ids = (v or {}).get("items") or []
        else:
            v = self.item_view(ref)
            ids = [int(ref)] if v else []
        if not v:
            return None
        rows = set()
        srcs = self._srcs(ids)
        for item_id in ids:
            it = self._item(item_id) or {}
            more = srcs.get(item_id, []) + (self._srcs([it["predecessor"]]).get(it["predecessor"], [])
                                            if it.get("predecessor") else [])
            for s in more:
                rows.add(s["id"])
                if s["thread"]:
                    rows |= {r["id"] for r in self._read("SELECT id FROM messages WHERE source=? AND account=? AND "
                                                         "thread=? ORDER BY ts DESC LIMIT 200",
                                                         (s["source"], s["account"], s["thread"]))}
        return {"project": f"ledger:{kind}:{int(ref)}", "name": v["title"][:60], "status": "",
                "ids": sorted(msgstore.STORE_ID_BASE + r for r in rows)[-500:]}

    # --- replies tab ------------------------------------------------------------------------------------------------
    def replies(self, now=None):
        """Every open ask waiting on the owner (clash sides too), oldest first, with how long each has waited."""
        now = self.clock() if now is None else now
        tz = self.tz()
        lv = self.lv.view(now=now, horizon_days=HORIZON_DAYS)
        rows = self._needs(lv, self._hidden(now), now, tz)["rows"]
        top = max([r["seconds"] for r in rows] + [1])
        for r in rows:
            r["bar"] = round(max(0.04, r["seconds"] / top), 3)
        return {"total": len(rows), "rows": rows, "time": t12(local(now, tz))}

    # --- the owner's presses ------------------------------------------------------------------------------------------
    def _ids(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(isinstance(i, bool) or not isinstance(i, int)
                                                                        for i in ids):
            raise Invalid("Send the item ids as a list of numbers.")
        got = self._items(ids)
        if len(got) != len(set(ids)):
            raise Invalid("Some of those items aren't in the ledger.")
        return got

    def done(self, ids, note="you marked it done"):
        items = self._ids(ids)
        now = self.clock()
        tz = self.tz()

        def run(db):
            out = []
            for i, it in items.items():
                if it["status"] in L.CLOSED:
                    continue
                L.set_status(db, i, "done", now, note=note, kind="owner")
                C.check_item(db, i, now, tz)
                db.execute("UPDATE today_reminders SET state='cancelled' WHERE state='open' AND item=?", (i,))
                out.append(i)
            return out
        return {"done": self.ms.write(run)}

    def undo(self, ids):
        """Undo Done: each item goes back to the status it had before the owner's press."""
        items = self._ids(ids)
        now = self.clock()
        tz = self.tz()

        def run(db):
            out = []
            for i, it in items.items():
                h = db.execute("SELECT status_from FROM ledger_history WHERE item=? AND kind='owner' AND status_to='done' "
                               "ORDER BY id DESC LIMIT 1", (i,)).fetchone()
                if it["status"] == "done" and h:
                    L.set_status(db, i, h["status_from"] or "open", now, note="you undid Done", kind="owner")
                    C.check_item(db, i, now, tz)
                    out.append(i)
            return out
        return {"reopened": self.ms.write(run)}

    def dismiss(self, cid):
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise Invalid("Send the clash id as a number.")
        now = self.clock()

        def run(db):
            c = db.execute("SELECT * FROM ledger_conflicts WHERE id=?", (cid,)).fetchone()
            if c is None:
                return None
            if c["state"] == "open":
                C._resolve(db, cid, "dismissed", now)  # noqa: SLF001
            ids = [r for k, r in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"])) if k == "item"]
            db.execute("UPDATE ledger_followups SET state='dismissed', cleared_at=? WHERE state='open' AND (conflict=? OR "
                       f"item IN ({','.join('?' * len(ids)) or 'NULL'}))", [now, cid] + ids)
            db.execute("UPDATE today_reminders SET state='cancelled' WHERE state='open' AND conflict=?", (cid,))
            return cid
        got = self.ms.write(run)
        if got is None:
            raise Invalid("There's no such clash.")
        return {"dismissed": got}

    def reopen(self, cid, followups=(), reminders=()):
        """0.22.0 Undo a Dismiss (actions.py): the clash opens again with the follow-ups and reminders that Dismiss
        closed (only those, and only when nothing else changed them since). True when it opened."""
        now = self.clock()
        fus, rems = [int(f) for f in followups], [int(r) for r in reminders]

        def run(db):
            n = db.execute("UPDATE ledger_conflicts SET state='open', resolved_at=NULL, resolved_why='', updated_at=? "
                           "WHERE id=? AND state='resolved' AND resolved_why='dismissed'", (now, cid)).rowcount
            if n and fus:
                db.execute(f"UPDATE ledger_followups SET state='open', cleared_at=NULL WHERE state='dismissed' AND "
                           f"id IN ({','.join('?' * len(fus))})", fus)
            if n and rems:
                db.execute(f"UPDATE today_reminders SET state='open' WHERE state='cancelled' AND "
                           f"id IN ({','.join('?' * len(rems))})", rems)
            return bool(n)
        return self.ms.write(run)

    def told(self, fid):
        """0.22.0: the owner says he told them (a follow-up marked told; Undo: untold). Its reminder clears at tick."""
        if isinstance(fid, bool) or not isinstance(fid, int):
            raise Invalid("Send the follow-up id as a number.")
        now = self.clock()
        n = self.ms.write(lambda db: db.execute("UPDATE ledger_followups SET state='told', cleared_at=? WHERE id=? AND "
                                                "state='open'", (now, fid)).rowcount)
        if not n:
            raise Invalid("That follow-up isn't open.")
        return {"told": fid}

    def untold(self, fid):
        return bool(self.ms.write(lambda db: db.execute("UPDATE ledger_followups SET state='open', cleared_at=NULL "
                                                        "WHERE id=? AND state='told'", (fid,)).rowcount))

    def due_at(self, when, now=None):
        """'noon', '5pm', 'tomorrow' (9:00 am), 'HH:MM' (today, else tomorrow) or 'YYYY-MM-DDTHH:MM' (local)."""
        now = self.clock() if now is None else now
        tz = self.tz()
        n = local(now, tz)
        w = str(when or "").strip().lower()
        if w == "tomorrow":
            d = dt.datetime.combine(n.date() + dt.timedelta(days=1), dt.time(REMIND_HOUR), tzinfo=tz)
            return d.timestamp()
        m = {"noon": (12, 0), "5pm": (17, 0)}.get(w)
        if m is None:
            hm = re.fullmatch(r"(\d{1,2}):(\d{2})", w)
            if hm and int(hm.group(1)) < 24 and int(hm.group(2)) < 60:
                m = (int(hm.group(1)), int(hm.group(2)))
        if m is not None:
            d = dt.datetime.combine(n.date(), dt.time(*m), tzinfo=tz)
            if d.timestamp() <= now + 60:
                d = dt.datetime.combine(n.date() + dt.timedelta(days=1), dt.time(*m), tzinfo=tz)
            return d.timestamp()
        try:
            d = dt.datetime.fromisoformat(w.upper().replace("T", "T") if "t" in w else w)
        except ValueError:
            raise Invalid("Pick a time: noon, 5 pm, tomorrow, or a date and time.") from None
        d = d.replace(tzinfo=tz) if d.tzinfo is None else d
        if d.timestamp() <= now:
            raise Invalid("Pick a time that's still ahead.")
        if d.timestamp() > now + 366 * 86400:
            raise Invalid("Pick a time within a year.")
        return d.timestamp()

    def remind(self, body):
        """Remind me: {followup | item | clash, at}. One open reminder per target (a new one replaces it)."""
        keys = [k for k in ("followup", "item", "clash") if body.get(k) is not None]
        if len(keys) != 1 or isinstance(body[keys[0]], bool) or not isinstance(body[keys[0]], int):
            raise Invalid("Say what to remind you about: one followup, item or clash id.")
        k, ref = keys[0], body[keys[0]]
        due = self.due_at(body.get("at"))
        now = self.clock()
        table = {"followup": "ledger_followups", "item": "ledger_items", "clash": "ledger_conflicts"}[k]
        if not self._read(f"SELECT 1 FROM {table} WHERE id=?", (ref,)):
            raise Invalid("There's nothing like that to remind you about.")
        col = {"followup": "followup", "item": "item", "clash": "conflict"}[k]

        def run(db):
            db.execute(f"UPDATE today_reminders SET state='replaced' WHERE state='open' AND {col}=?", (ref,))
            cur = db.execute(f"INSERT INTO today_reminders ({col}, due_ts, state, created_at) VALUES (?,?,?,?)",
                             (ref, due, "open", now))
            return cur.lastrowid
        rid = self.ms.write(run)
        return {"reminder": rid, "at": self._due_word(due, now, self.tz())}

    def note(self, body):
        """A note to the assistant on one item or a batch: chips (not_for_me, junk, remind_tomorrow) and free text. Kept
        with the items; not_for_me and junk file the items (done) and teach the sorting; remind_tomorrow hides them
        until 9:00 am tomorrow and then reminds the owner."""
        items = self._ids(body.get("items"))
        chips = body.get("chips") or []
        if not isinstance(chips, list) or any(c not in CHIPS for c in chips):
            raise Invalid("Unknown quick note.")
        text = body.get("text") or ""
        if not isinstance(text, str):
            raise Invalid("The note must be text.")
        text = re.sub(r"\s+", " ", text).strip()[:NOTE_MAX]
        if not chips and not text:
            raise Invalid("Write a note or pick one of the quick notes.")
        now = self.clock()
        label = self._label(items)
        files = bool(FILES & set(chips))
        later = "remind_tomorrow" in chips
        due = self.due_at("tomorrow") if later else None
        learned = 0   # the 0.8.5 learner's sender priors went with the sorter (the column stays for old rows)

        def run(db):
            cur = db.execute("INSERT INTO today_notes (at, text, chips, items, label, learned) VALUES (?,?,?,?,?,?)",
                             (now, text, json.dumps(chips), json.dumps(sorted(items)), label, learned))
            nid = cur.lastrowid
            for i, it in items.items():
                L.history(db, i, "note", now, note=("; ".join([CHIPS[c] for c in chips] + ([text] if text else []))))
                if files and it["status"] not in L.CLOSED:
                    L.set_status(db, i, "done", now, note="filed by your note", kind="owner")
                if later:
                    self._set_aside(db, i, due, now, True, nid)
            return nid
        nid = self.ms.write(run)
        return {"note": nid, "label": label, "filed": sorted(items) if files else [],
                "hidden_until": self._due_word(due, now, self.tz()) if due else ""}

    def _label(self, items):
        its = list(items.values())
        if len(its) == 1:
            it = its[0]
            return (f"{it['who_name']}: " if it.get("who_name") and it["who"] != L.OWNER else "") + it["what"]
        srcs = self._srcs(list(items))
        kinds = {clean_group(it, (self._origin(srcs.get(it["id"], [])) or {}).get("source") or "") for it in its}
        word = {"newsletter": "newsletters", "receipt": "receipts", "chatter": "Slack threads"}.get(
            kinds.pop() if len(kinds) == 1 else "", "items")
        return f"{len(its)} {word}"

    # --- reminders (the runner calls tick) -------------------------------------------------------------------------
    def tick(self, now=None):
        """Fire due reminders through the push alerter; a follow-up that cleared by itself cancels its reminder."""
        now = self.clock() if now is None else now
        tz = self.tz()
        out = []
        self._arm_stale(now)
        due = self._read("SELECT * FROM today_reminders WHERE state='open' AND due_ts <= ? ORDER BY id", (now,))
        batches = {}
        for r in due:
            if r["followup"] is not None:
                f = self._read("SELECT * FROM ledger_followups WHERE id=?", (r["followup"],))
                if not f or f[0]["state"] != "open":
                    self._set_rem(r["id"], "cleared", now)
                    out.append((r["id"], "cleared"))
                    continue
                title, msg = self._fu_words(f[0], now, tz)
                state = self._push(title, msg, f"callsheet-remind-{r['id']}")
                self._set_rem(r["id"], "sent", now, state)
                out.append((r["id"], state))
            elif r["item"] is not None:
                it = self._item(r["item"])
                if not it or it["status"] in L.CLOSED:
                    self._set_rem(r["id"], "cleared", now)
                    continue
                if not r["push"]:   # 0.22.0: a snooze ends: the item shows again, no phone reminder
                    self._set_rem(r["id"], "ended", now)
                    out.append((r["id"], "ended"))
                    continue
                batches.setdefault(r["batch"] or -r["id"], []).append((r, it))
            elif r["conflict"] is not None:
                c = self._read("SELECT * FROM ledger_conflicts WHERE id=?", (r["conflict"],))
                if not c or c[0]["state"] != "open":
                    self._set_rem(r["id"], "cleared", now)
                    continue
                state = self._push(f"Clash {slot_word(c[0]['slot_start'], now, tz)}", "You asked to be reminded.",
                                   f"callsheet-remind-{r['id']}")
                self._set_rem(r["id"], "sent", now, state)
                out.append((r["id"], state))
        for _key, rs in batches.items():
            its = [it for _, it in rs]
            who = its[0].get("who_name") or its[0]["what"]
            title = f"Reminder: {who}" if len(its) == 1 else f"Reminder: {len(its)} things you set aside"
            src = SOURCES.get((self._origin(self._srcs([its[0]['id']]).get(its[0]['id'], [])) or {}).get("source"),
                              ("", "a message"))[1]
            msg = f"From {src}. You asked to see it again today." if len(its) == 1 else "You asked to see them again today."
            state = self._push(title, msg, f"callsheet-remind-{rs[0][0]['id']}")
            for r, _ in rs:
                self._set_rem(r["id"], "sent", now, state)
                out.append((r["id"], state))
        return out

    def _arm_stale(self, now):
        """0.22.0 (conflicts FUP-04): a follow-up about a stale calendar entry (people not told of a move) gets a
        reminder before the entry starts (its due time), unless the owner already set one for it."""
        def run(db):
            for f in db.execute("SELECT f.id, f.due_ts FROM ledger_followups f WHERE f.state='open' AND f.event IS NOT "
                                "NULL AND f.due_ts IS NOT NULL AND NOT EXISTS (SELECT 1 FROM today_reminders r WHERE "
                                "r.followup=f.id)").fetchall():
                db.execute("INSERT INTO today_reminders (followup, due_ts, state, created_at) VALUES (?,?,?,?)",
                           (f["id"], max(f["due_ts"], now), "open", now))
        try:
            self.ms.write(run)
        except Exception as e:  # noqa: BLE001 (the other reminders still fire)
            self.log(f"Callsheet today: stale-entry reminders unavailable ({type(e).__name__})")

    def _fu_words(self, f, now, tz):
        """'Marco still doesn't know you're busy Thu 7:00 pm' / 'You told Dana yes (Signal) at 9:02 am. Marco (email)
        hasn't heard from you.' Names, sources and times only."""
        who = first(f["tell_name"] or f["tell"])
        title, msg = f"{who} still doesn’t know your answer", f"{who} hasn’t heard from you."
        e = self._read("SELECT title, start_ts FROM events WHERE id=?", (f["event"],)) if f["event"] else []
        if e and e[0]["start_ts"]:        # 0.22.0: a stale calendar entry (names and times only)
            d = local(e[0]["start_ts"], tz)
            return f"{who} hasn’t heard about the change", f"The calendar still has it {d:%a} {t12(d)}."
        c = self._read("SELECT * FROM ledger_conflicts WHERE id=?", (f["conflict"],)) if f["conflict"] else []
        if c:
            sides = self._clash_sides(c, now, tz)[c[0]["id"]]
            d = local(c[0]["slot_start"], tz)
            title = f"{who} still doesn’t know you’re busy {d:%a} {t12(d)}"
            yes = next((s for s in sides if s.get("mine")), None)
            tell = next((s for s in sides if s["kind"] == "item" and s["id"] == f["item"]), None)
            src = C.SOURCE_WORD.get(tell["source"], "a message") if tell else "a message"
            if yes and yes.get("yes_ts"):
                msg = (f"You told {yes['first']} yes ({C.SOURCE_WORD.get(yes['source'], 'a message')}) at "
                       f"{t12(local(yes['yes_ts'], tz))}. {who} ({src}) hasn’t heard from you.")
            else:
                msg = f"{who} ({src}) hasn’t heard from you."
        return title, msg

    def _push(self, title, msg, tag):
        if self.alerter is None:
            return "off"
        try:
            return self.alerter.remind(title, msg, tag)
        except Exception as e:  # noqa: BLE001 (a failed push never stops Today)
            self.log(f"Callsheet today: reminder push failed ({type(e).__name__})")
            return "failed"

    def _set_rem(self, rid, state, now, alert=""):
        self.ms.write(lambda db: db.execute("UPDATE today_reminders SET state=?, sent_at=?, alert=? WHERE id=?",
                                            (state, now, alert or "", rid)))
