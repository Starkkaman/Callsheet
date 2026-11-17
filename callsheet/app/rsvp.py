"""0.21.0 Answer invites (approved 2026-11-13).

The one thing Callsheet ever sends, and only when the owner presses Yes, No or Maybe on one calendar invite (Agenda,
Today, the item view). Google tells that event's organizer; nothing else is sent. The UI promise reads "Nothing is sent
from here except an RSVP you press".

- Same grant as Add to calendar: `calendar.events`, per Google account ("Allow adding events", calwrite.py). Without it
  the buttons stay off and say how to allow it.
- The press reads the event as Google has it now (events.get, a GET), then makes exactly one call:
  `PATCH /calendar/v3/calendars/<id>/events/<eventId>?sendUpdates=all` with a body of only `attendees`, equal to the
  list just fetched except the owner's own `self: true` entry's responseStatus (google.check_rsvp_body refuses anything
  else before any network I/O). No comment, no other attendee, no other field.
- A 403 or any failure is said plainly and never retried. Mail, messages, feeds, Chat and tool results can never offer,
  fill or press it: only the api route `POST api/agenda/rsvp` calls `answer`, and only the page's own button posts it.
"""
import json
import threading
import time

import conflicts as C
import google
import ledger as L
import sources
from store import Conflict, InputError, NotFound

ANSWERS = {"yes": "accepted", "no": "declined", "maybe": "tentative"}
KEYS = {v: k for k, v in ANSWERS.items()}
WORDS = {"accepted": "Yes", "declined": "No", "tentative": "Maybe"}
VIA = ("agenda", "today", "item")
SCHEMA = """CREATE TABLE IF NOT EXISTS rsvp_answers (
  id INTEGER PRIMARY KEY, event INTEGER NOT NULL, item INTEGER, answer TEXT NOT NULL, via TEXT NOT NULL,
  at REAL NOT NULL)"""
NEEDS = "Answering invites needs “Allow adding events” for {email}: Setup › Connections."
DENIED = ("Google didn't let Callsheet answer it. Check “Allow adding events” for {email} in Setup › Connections, "
          "or answer it in Google Calendar.")


def self_entry(attendees):
    """The owner's own entry in a stored attendee list ({name, address, response, self}), or None."""
    return next((a for a in attendees or [] if isinstance(a, dict) and a.get("self")), None)


class Rsvp:
    def __init__(self, ms, cal, clock=time.time, api=google, tz=None):
        self.ms, self.cal, self.clock, self.api = ms, cal, clock, api
        self.tz = tz                     # () -> tzinfo (the ledger's); else the calendar writer's zone
        self.lock = threading.Lock()
        ms.write(lambda db: db.execute(SCHEMA))

    def _event(self, event_id):
        rows = self.ms._read("SELECT * FROM events WHERE id=?", (int(event_id),))  # noqa: SLF001
        if not rows:
            raise NotFound("There's no such invite.")
        e = dict(rows[0])
        try:
            e["attendees"] = json.loads(e["attendees"] or "[]")
        except ValueError:
            e["attendees"] = []
        return e

    def _tz(self):
        try:
            return self.tz() if self.tz is not None else self.cal._tz()  # noqa: SLF001
        except Exception:  # noqa: BLE001 (no zone: the next sweep updates the group)
            return None

    def _email(self, acc_id):
        try:
            return self.cal.store.account(acc_id)["email"]
        except Exception:  # noqa: BLE001 (the account was removed)
            return "this account"

    def state(self, e):
        """What the page shows for one calendar event: None when the owner isn't on its guest list (nothing to
        answer), else {event, answer: yes|no|maybe|"", word, on, why}."""
        att = e["attendees"] if isinstance(e.get("attendees"), list) else json.loads(e.get("attendees") or "[]")
        me = self_entry(att)
        if me is None or e.get("status") == "cancelled":
            return None
        resp = str(me.get("response") or "")
        on = bool(self.cal.granted(e["account"])) if self.cal is not None else False
        return {"event": e["id"], "answer": KEYS.get(resp, ""), "word": WORDS.get(resp, "Not answered"), "on": on,
                "why": "" if on else NEEDS.format(email=self._email(e["account"]))}

    def state_of_item(self, it):
        if not it or it.get("cal_ref") is None:
            return None
        try:
            return self.state(self._event(it["cal_ref"]))
        except NotFound:
            return None

    def answer(self, body, via="agenda"):
        """The owner pressed Yes, No or Maybe. body: {item | event, answer}. Returns {ok, answer, word, event, item}."""
        if not isinstance(body, dict):
            raise InputError("Send a JSON object.")
        bad = [k for k in body if k not in ("item", "event", "answer", "via")]
        if bad:
            raise InputError("Unknown field: " + bad[0])
        want = body.get("answer")
        if want not in ANSWERS:
            raise InputError("Answer Yes, No or Maybe.")
        via = body.get("via") if body.get("via") in VIA else via
        item, ev_id = body.get("item"), body.get("event")
        if (item is None) == (ev_id is None):
            raise InputError("Which invite?")
        for v in (item, ev_id):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise InputError("Which invite?")
        if item is not None:
            rows = self.ms._read("SELECT * FROM ledger_items WHERE id=?", (item,))  # noqa: SLF001
            it = L.row_dict(rows[0]) if rows else None
            if it is None:
                raise NotFound("There's no such invite.")
            if it.get("subtype") != "invite" or it.get("cal_ref") is None:
                raise Conflict("This invite isn't on your calendar, so answer it where it came from.")
            ev_id = it["cal_ref"]
        e = self._event(ev_id)
        if self_entry(e["attendees"]) is None:
            raise Conflict("You aren't on this event's guest list, so there's nothing to answer here.")
        email = self._email(e["account"])
        if not self.cal.granted(e["account"]):
            raise Conflict(NEEDS.format(email=email))
        status = ANSWERS[want]
        with self.lock:
            try:
                cur = self.cal.tokens.call(e["account"], self.api.calendar_event_get, e["calendar"], e["event_id"])
                if not isinstance(cur, dict) or cur.get("status") == "cancelled":
                    raise Conflict("This event was cancelled.")
                if (cur.get("organizer") or {}).get("self") is True:
                    raise Conflict("You organised this one, so there's nothing to answer.")
                att = cur.get("attendees")
                if not isinstance(att, list) or sum(1 for a in att if isinstance(a, dict) and a.get("self") is True) != 1:
                    raise Conflict("You aren't on this event's guest list, so there's nothing to answer here.")
                new = [dict(a, responseStatus=status) if isinstance(a, dict) and a.get("self") is True else a
                       for a in att]
                self.cal.tokens.call(e["account"], self.api.calendar_rsvp, e["calendar"], e["event_id"], new, att)
            except (sources.SourceError, sources.Retry) as err:
                if getattr(err, "status", 0) == 403:
                    raise Conflict(DENIED.format(email=email)) from None
                raise Conflict(f"Callsheet couldn't answer it: {err}") from None
            except google.NotAllowed:
                raise Conflict("Callsheet couldn't send this answer safely, so it sent nothing.") from None
        now = self.clock()

        def done(db):
            for a in e["attendees"]:
                if isinstance(a, dict) and a.get("self"):
                    a["response"] = status
            db.execute("UPDATE events SET attendees=? WHERE id=?", (json.dumps(e["attendees"]), e["id"]))
            db.execute("INSERT INTO rsvp_answers (event, item, answer, via, at) VALUES (?,?,?,?,?)",
                       (e["id"], item, status, via, now))
            for r in db.execute("SELECT id FROM ledger_items WHERE subtype='invite' AND cal_ref=? AND status='open' "
                                "AND merged_into IS NULL", (e["id"],)).fetchall():
                L.set_status(db, r["id"], "done", now, note=f"you answered {WORDS[status]} (Google told the organizer)")
            # 0.22.x: the owner's trip's grouped clash follows the answer at once (a No takes the entry off its list, a Yes
            # or Maybe keeps or puts it on), not at the next sweep
            tz = self._tz()
            if tz is not None:
                C.regroup_event(db, e["id"], now, tz)
        self.ms.write(done)
        return {"ok": True, "answer": want, "word": WORDS[status], "event": e["id"], "item": item}
