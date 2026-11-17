"""0.21.0 Add to calendar: the one thing Callsheet writes in a Google account (approved
2026-11-11).

- Per Google account, an incremental grant of `calendar.events` only ("Allow adding events", like Drive's); whether an
  account gave it is read from the token endpoint's `scope` answer and kept in messages.db meta (`cal_write`), with the
  owner's default calendar for it (primary unless the owner picks another from the account's calendar list, read-only).
- Only the owner's press adds an event (Today's confirm card, the item view, or Confirm on a Chat card): google.py's
  `calendar_insert_event` (events.insert, sendUpdates=none, no attendees). There is no update or delete: an event once
  added is changed or removed by the owner in Google Calendar.
- Idempotent: one row per ledger item in `cal_adds` (taken under a lock before the call), the event carries the private
  extendedProperty `callsheet_item=<item id>` and, before inserting, Callsheet looks for an event with it (a GET), so a
  retry after a lost answer never adds the item twice.
- The calendar reader later stores the event like any other; `link_events` (run in the same write as the reader's
  upsert) points the ledger item's `cal_ref` at it, so Today, Coming up and clashes treat the two as one.
"""
import datetime as dt
import re
import threading
import time
import zoneinfo

import google
import ledger as L
import sources
from store import Conflict, InputError, NotFound

META = "cal_write"            # {account id: {"granted": bool, "at": ts, "calendar": id, "calendars": [...]}}
ADDING_TTL = 120              # an "adding" row older than this is a lost attempt and may be tried again
MAX_TITLE = 200
SCHEMA = """CREATE TABLE IF NOT EXISTS cal_adds (
  item INTEGER PRIMARY KEY, state TEXT NOT NULL, account TEXT NOT NULL DEFAULT '', calendar TEXT NOT NULL DEFAULT '',
  calendar_name TEXT NOT NULL DEFAULT '', event_id TEXT NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', start TEXT NOT NULL DEFAULT '', end TEXT NOT NULL DEFAULT '',
  all_day INTEGER NOT NULL DEFAULT 0, via TEXT NOT NULL DEFAULT '', at REAL NOT NULL)"""
NEEDS_GRANT = "Allow adding events for a Google account first (Setup › Connections)."
NO_DELETE = "Callsheet can't delete events: remove it in Google Calendar."


def link_events(db):
    """Point each added item's cal_ref at the event the reader stored (same account, calendar and event id). Runs
    inside the reader's write (msgstore.upsert_events(after=…)); a no-op before the first Add."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cal_adds'").fetchone() is None:
        return
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_items'").fetchone() is None:
        return
    db.execute("UPDATE ledger_items SET cal_ref=(SELECT e.id FROM events e JOIN cal_adds c ON e.account=c.account AND "
               "e.calendar=c.calendar AND e.event_id=c.event_id WHERE c.item=ledger_items.id AND c.state='added') "
               "WHERE cal_ref IS NULL AND id IN (SELECT c.item FROM cal_adds c JOIN events e ON e.account=c.account "
               "AND e.calendar=c.calendar AND e.event_id=c.event_id WHERE c.state='added')")


class _Tokens(sources.GoogleAccess):
    """Access tokens like the live readers' (saved back to the store); the granted scopes go to CalWrite."""

    def __init__(self, store, owner, clock):
        super().__init__(store, None, clock)
        self.owner = owner

    def _save_scopes(self, acc_id, scope):
        self.owner._save_scope(acc_id, scope)


class CalWrite:
    def __init__(self, store, ms, tz_name, clock=time.time, api=google):
        self.store, self.ms, self.tz_name, self.clock, self.api = store, ms, tz_name, clock, api
        self.tokens = _Tokens(store, self, clock)
        self.lock = threading.Lock()
        ms.write(lambda db: db.execute(SCHEMA))

    # --- the grant and the default calendar ----------------------------------------------------------------------
    def _meta(self):
        v = self.ms.meta(META, {})
        return v if isinstance(v, dict) else {}

    def _put(self, acc_id, **kw):
        m = self._meta()
        m[acc_id] = dict(m.get(acc_id) or {}, **kw)
        self.ms.set_meta(META, m)

    def _save_scope(self, acc_id, scope):
        self._put(acc_id, granted=google.CAL_WRITE_SCOPE in str(scope).split(), at=self.clock())

    def granted(self, acc_id):
        """True / False from the last answer Google gave, None when not known yet."""
        g = (self._meta().get(acc_id) or {}).get("granted")
        return g if isinstance(g, bool) else None

    def check(self, acc_id):
        """Ask Google (a token refresh) whether this account gave calendar.events; when it did, read its calendar list
        (calendarList, a GET) for the default-calendar picker. Returns the account's public state."""
        acc = self.store.account(acc_id)       # NotFound for an unknown id
        try:
            self.tokens._refresh(acc_id)       # noqa: SLF001 (saves the scope through _save_scope)
        except (sources.SourceError, sources.Retry) as e:
            raise Conflict(str(e)) from None
        if self.granted(acc_id):
            try:
                items = self.tokens.call(acc_id, self.api.calendar_list)
                cals = [{"id": str(c["id"]), "summary": str(c.get("summaryOverride") or c.get("summary") or c["id"])[:200],
                         "primary": bool(c.get("primary"))} for c in items
                        if isinstance(c, dict) and c.get("id") and c.get("accessRole") in (None, "owner", "writer")]
                self._put(acc_id, calendars=cals)
            except (sources.SourceError, sources.Retry):
                pass
        return self.public(acc)

    def calendars(self, acc):
        """The calendars this account can add to: the list read after the grant, else the stored calendar list."""
        cals = (self._meta().get(acc["id"]) or {}).get("calendars")
        if not isinstance(cals, list) or not cals:
            cals = [{"id": c["id"], "summary": c.get("summary") or c["id"], "primary": bool(c.get("primary"))}
                    for c in acc.get("calendars") or []]
        return cals or [{"id": "primary", "summary": "Main calendar", "primary": True}]

    def default_calendar(self, acc):
        cals = self.calendars(acc)
        want = (self._meta().get(acc["id"]) or {}).get("calendar")
        hit = next((c for c in cals if c["id"] == want), None) or next((c for c in cals if c.get("primary")), None)
        return hit or {"id": "primary", "summary": "Main calendar", "primary": True}

    def set_default(self, acc_id, cal_id):
        acc = self.store.account(acc_id)
        if not isinstance(cal_id, str) or not any(c["id"] == cal_id for c in self.calendars(acc)):
            raise InputError("Pick one of this account's calendars.")
        self._put(acc_id, calendar=cal_id)
        return self.public(acc)

    def public(self, acc):
        cal = self.default_calendar(acc)
        g = self.granted(acc["id"])
        return {"account_id": acc["id"], "email": acc["email"], "granted": g,
                "calendar": cal["id"], "calendar_name": cal["summary"],
                "calendars": [{"id": c["id"], "name": c["summary"], "primary": bool(c.get("primary"))}
                              for c in self.calendars(acc)] if g else [],
                "status": f"Can add events · to {cal['summary']}" if g else ""}

    def accounts(self):
        return [self.public(a) for a in self.store.accounts()]

    def targets(self):
        """Where an event can go: [{account_id, email, calendar, name, default}], the first granted account's default
        calendar first. Empty when no account allowed adding events."""
        out = []
        for a in self.store.accounts():
            if a.get("status") == "needs_reconnect" or not self.granted(a["id"]):
                continue
            d = self.default_calendar(a)
            for c in sorted(self.calendars(a), key=lambda c: c["id"] != d["id"]):
                out.append({"account_id": a["id"], "email": a["email"], "calendar": c["id"], "name": c["summary"],
                            "default": not out})
        return out

    def can_add(self):
        return any(self.granted(a["id"]) and a.get("status") != "needs_reconnect" for a in self.store.accounts())

    # --- the item side --------------------------------------------------------------------------------------------
    def _tz(self):
        try:
            return zoneinfo.ZoneInfo(self.tz_name() if callable(self.tz_name) else self.tz_name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
            return dt.timezone.utc

    def _item(self, item_id):
        rows = self.ms._read("SELECT * FROM ledger_items WHERE id=?", (int(item_id),))  # noqa: SLF001
        return L.row_dict(rows[0]) if rows else None

    def prefill(self, it):
        """What the confirm card starts with: {title, date, time, minutes, end, ask_end} from the ledger item (all
        editable). 0.21.0: ask_end when the message gave a start and no end (decided 2026-11-13),
        so the card asks for it instead of guessing an hour; end is the known end ("HH:MM") otherwise."""
        tz = self._tz()
        what = " ".join(str(it.get("what") or "").split())[:MAX_TITLE]
        title = what[:1].upper() + what[1:]
        date, tm, minutes, end, ask_end = "", "", 60, "", False
        if it.get("all_day") and it.get("start_date"):
            date = it["start_date"]
        elif it.get("start_ts") is not None:
            s = dt.datetime.fromtimestamp(it["start_ts"], tz)
            date, tm = s.date().isoformat(), f"{s:%H:%M}"
            if it.get("end_ts") and it["end_ts"] > it["start_ts"]:
                minutes = max(5, min(24 * 60, int(round((it["end_ts"] - it["start_ts"]) / 60))))
                end = f"{dt.datetime.fromtimestamp(it['end_ts'], tz):%H:%M}"
            else:
                ask_end = True
        return {"title": title, "date": date, "time": tm, "minutes": minutes, "end": end, "ask_end": ask_end}

    def added(self, ids=None):
        """{item id: {link, calendar, at}} for items Callsheet added (state 'added')."""
        rows = self.ms._read("SELECT * FROM cal_adds WHERE state='added'")  # noqa: SLF001
        want = None if ids is None else {int(i) for i in ids}
        return {r["item"]: {"link": r["link"], "calendar": r["calendar_name"], "at": r["at"], "event_id": r["event_id"]}
                for r in rows if want is None or r["item"] in want}

    def states(self, ids):
        """Today and the item view, per item id: {"added": {link, calendar}} once Callsheet added it, {} when the item is
        on the calendar anyway, else {"add": {on, why, title, date, time, minutes}} (on: some account allows adding)."""
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        added, on, out = self.added(ids), self.can_add(), {}
        rows = self.ms._read(f"SELECT * FROM ledger_items WHERE id IN ({','.join('?' * len(ids))})", ids)  # noqa: SLF001
        for r in rows:
            it = L.row_dict(r)
            a = added.get(it["id"])
            if a:
                out[it["id"]] = {"added": {"link": {"href": a["link"], "label": "Open"} if a["link"] else None,
                                           "calendar": a["calendar"]}}
            elif it.get("cal_ref") is not None:
                out[it["id"]] = {}
            else:
                out[it["id"]] = {"add": dict(self.prefill(it), on=on, why="" if on else "needs calendar access")}
        return out

    # --- the press --------------------------------------------------------------------------------------------------
    def _event(self, item_id, title, date, time_, minutes):
        title = " ".join(str(title or "").split())
        if not title or len(title) > MAX_TITLE or any(ord(c) < 32 for c in title):
            raise InputError(f"Give the event a title (at most {MAX_TITLE} characters).")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise InputError("Pick a date.")
        try:
            d = dt.date.fromisoformat(date)
        except ValueError:
            raise InputError("Pick a date.") from None
        xp = {"private": {"callsheet_item": str(int(item_id))}}
        if not time_:
            ev = {"summary": title, "start": {"date": d.isoformat()},
                  "end": {"date": (d + dt.timedelta(days=1)).isoformat()}, "extendedProperties": xp}
            return ev, d.isoformat(), (d + dt.timedelta(days=1)).isoformat(), True
        if not isinstance(time_, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_):
            raise InputError("Pick a time, or leave it empty for an all-day event.")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 24 * 60:
            raise InputError("The length must be between 5 minutes and a day.")
        tz = self._tz()
        s = dt.datetime.combine(d, dt.time.fromisoformat(time_))
        e = s + dt.timedelta(minutes=minutes)
        name = getattr(tz, "key", "UTC")
        ev = {"summary": title, "start": {"dateTime": s.isoformat(timespec="seconds"), "timeZone": name},
              "end": {"dateTime": e.isoformat(timespec="seconds"), "timeZone": name}, "extendedProperties": xp}
        return ev, s.replace(tzinfo=tz).isoformat(), e.replace(tzinfo=tz).isoformat(), False

    def _target(self, account_id, calendar_id):
        tg = self.targets()
        if not tg:
            raise Conflict(NEEDS_GRANT)
        if account_id is None and calendar_id is None:
            return tg[0]
        hit = next((t for t in tg if t["account_id"] == account_id and t["calendar"] == calendar_id), None)
        if hit is None:
            raise InputError("Pick one of the calendars Callsheet may add to.")
        return hit

    def add(self, item_id, title=None, date=None, time_=None, minutes=60, account_id=None, calendar_id=None,
            via="today"):
        """The owner pressed Add (Today's card, the item view) or Confirm (a Chat card). Returns {ok, already, item,
        link, calendar}. Never adds an item twice."""
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise InputError("Which item?")
        it = self._item(item_id)
        if it is None:
            raise NotFound("There's no such item.")
        pre = self.prefill(it)
        title = pre["title"] if title is None else title
        date = pre["date"] if date is None else date
        time_ = pre["time"] if time_ is None else time_
        ev, start, end, all_day = self._event(item_id, title, date, time_, minutes)
        tgt = self._target(account_id, calendar_id)
        now = self.clock()
        with self.lock:
            row = self.ms._read("SELECT * FROM cal_adds WHERE item=?", (item_id,))  # noqa: SLF001
            if row and row[0]["state"] == "added":
                return self._out(row[0], already=True)
            if row and row[0]["state"] == "adding" and now - row[0]["at"] < ADDING_TTL:
                raise Conflict("Callsheet is adding this one already.")
            self.ms.write(lambda db: db.execute(
                "INSERT INTO cal_adds (item, state, account, calendar, calendar_name, title, start, end, all_day, via, "
                "at) VALUES (?, 'adding', ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(item) DO UPDATE SET state='adding', "
                "account=excluded.account, calendar=excluded.calendar, calendar_name=excluded.calendar_name, "
                "title=excluded.title, start=excluded.start, end=excluded.end, all_day=excluded.all_day, "
                "via=excluded.via, at=excluded.at",
                (item_id, tgt["account_id"], tgt["calendar"], tgt["name"], ev["summary"], start, end, int(all_day),
                 via, now)))
        try:
            found = self.tokens.call(tgt["account_id"], self.api.calendar_event_by_item, tgt["calendar"], item_id)
            res = found[0] if found else self.tokens.call(tgt["account_id"], self.api.calendar_insert_event,
                                                          tgt["calendar"], ev)
        except (sources.SourceError, sources.Retry) as e:
            self.ms.write(lambda db: db.execute("DELETE FROM cal_adds WHERE item=? AND state='adding'", (item_id,)))
            msg = str(e)
            if getattr(e, "status", 0) == 403:
                msg = "Google didn't let Callsheet add it. Check “Allow adding events” in Setup › Connections."
            raise Conflict(msg) from None
        except Exception:
            self.ms.write(lambda db: db.execute("DELETE FROM cal_adds WHERE item=? AND state='adding'", (item_id,)))
            raise
        eid, link = str(res.get("id") or ""), str(res.get("htmlLink") or "")
        if not link.startswith("https://"):
            link = ""

        def done(db):
            db.execute("UPDATE cal_adds SET state='added', event_id=?, link=?, at=? WHERE item=?",
                       (eid, link, self.clock(), item_id))
            L.history(db, item_id, "calendar", self.clock(),
                      note=f"Added to {tgt['name']} ({'found' if found else 'new'}, by the owner via {via})")
            link_events(db)
        self.ms.write(done)
        return self._out(self.ms._read("SELECT * FROM cal_adds WHERE item=?", (item_id,))[0], already=bool(found))  # noqa: SLF001

    @staticmethod
    def _out(r, already=False):
        return {"ok": True, "already": already, "item": r["item"], "calendar": r["calendar_name"], "title": r["title"],
                "link": {"href": r["link"], "label": "Open"} if r["link"] else None}
