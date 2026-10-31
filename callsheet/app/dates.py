"""Dates found in mail and chat (0.5.0): concrete proposed dates and times in a reply row's latest incoming
message (meeting proposals, appointments, deadlines) become "Possible events" on the Agenda, each with an
"Add to calendar" link: a prefilled Google Calendar page the owner saves themselves (Callsheet has no calendar
write access). Deterministic rules, no AI: the date and time pieces are facts.py's (_PIECE, _date_of, _time_of,
due()). Pure: no network.
"""
import datetime as dt
import re
import urllib.parse

import facts
import memory
import rules

LOOKAHEAD_DAYS = 14   # the sync reads the calendars this many days ahead (today included) for the check
MAX_AHEAD_DAYS = 120  # dates further out than this are left out
MAX_PER_ROW = 2
MAX_EVENTS = 12
EVENT_MIN = 60        # an event without an end time lasts an hour
DEADLINE_MIN = 30     # a deadline with a time: the half hour before it
MAX_TITLE = 60
MAX_PHRASE = 80
MAX_DETAILS = 800
CAL_BASE = "https://calendar.google.com/calendar/render"

# a time range: "2-3pm", "11 am to 1 pm", "between 2 and 4pm", "10:00–11:30"
_R12 = re.compile(r"\b(?P<b>between\s+)?(?P<h1>\d{1,2})(?::(?P<m1>[0-5]\d))?(?:\s*(?P<ap1>[ap])\.?m\.?)?\s*"
                  r"(?P<sep>-|–|—|to|until|till|and)\s*(?P<h2>\d{1,2})(?::(?P<m2>[0-5]\d))?\s*(?P<ap2>[ap])\.?m\b",
                  re.I)
_R24 = re.compile(r"\b(?P<h1>[01]?\d|2[0-3]):(?P<m1>[0-5]\d)\s*(?:-|–|—|to|until|till)\s*"
                  r"(?P<h2>[01]?\d|2[0-3]):(?P<m2>[0-5]\d)\b", re.I)
# between the pieces of one date: "Thursday 5 Nov from 2-3pm", "at 2pm on Friday"
_JOIN = re.compile(r"[\s,]*(?:(?:at|on|from|@|between|around|-|–)[\s,]*)?", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_AMPM = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s*([ap])\.m\.", re.I)
# something to put in a calendar
_CUE = re.compile(
    r"\b(?:meet(?:ing|up)?s?|calls?|catch[\s-]?up|chat|sync|appointments?|appt|interviews?|lunch|dinner|breakfast|"
    r"brunch|coffee|drinks|visit|session|review|demo|workshop|class|lesson|rehearsal|practice|game|party|event|"
    r"webinar|conference|presentation|walk-?through|viewing|inspection|pick[\s-]?up|drop[\s-]?off|dentist|doctor|"
    r"vet|flight|reservation|booking|booked|scheduled|schedule|reschedul\w*|moved\s+to|pushed\s+to|postponed|"
    r"available|free|works?\s+for\s+you|does\s+\w+(?:\s+\w+)?\s+work|how\s+about|what\s+about|can\s+we|could\s+we|"
    r"shall\s+we|let'?s|join\s+us|see\s+you|slot|invite|invitation|kick-?off|stand-?up|1:1|one-on-one|ceremony|"
    r"concert|recital|training|orientation|hearing|exam|tour|zoom|huddle)\b", re.I)
# the title word for a Slack row (no subject): "Lunch with Jordan"
_NOUNS = {"meeting": "Meeting", "meet": "Meeting", "meetup": "Meetup", "call": "Call", "calls": "Call",
          "catch up": "Catch-up", "catch-up": "Catch-up", "catchup": "Catch-up", "chat": "Chat", "sync": "Sync",
          "appointment": "Appointment", "appt": "Appointment", "interview": "Interview", "lunch": "Lunch",
          "dinner": "Dinner", "breakfast": "Breakfast", "brunch": "Brunch", "coffee": "Coffee", "drinks": "Drinks",
          "visit": "Visit", "session": "Session", "review": "Review", "demo": "Demo", "workshop": "Workshop",
          "class": "Class", "lesson": "Lesson", "rehearsal": "Rehearsal", "practice": "Practice", "game": "Game",
          "party": "Party", "webinar": "Webinar", "presentation": "Presentation", "walkthrough": "Walkthrough",
          "walk-through": "Walkthrough", "viewing": "Viewing", "inspection": "Inspection", "dentist": "Dentist",
          "doctor": "Doctor", "vet": "Vet", "flight": "Flight", "interviews": "Interview", "kickoff": "Kickoff",
          "kick-off": "Kickoff", "standup": "Stand-up", "stand-up": "Stand-up", "1:1": "1:1", "tour": "Tour",
          "training": "Training", "huddle": "Huddle"}
# the sentence is about something that already happened
_PAST = re.compile(r"\b(?:yesterday|ago|last\s+(?:week|month|time|night|mon|tue|wed|thu|fri|sat|sun)\w*|earlier\s+"
                   r"today|discussed|talked|spoke|attended|went|happened|missed|had\s+(?:a|the|our)|thanks?\s+(?:you\s+)?"
                   r"for\s+(?:the|joining|coming|meeting))\b", re.I)
# a question asking the owner for a time ("What time works on Thursday?"): not a proposal unless it names a time
_ASK = re.compile(r"^\W*(?:what|when|which)\b|\bwhat\s+time\b|\bwhen\s+(?:are|is|would|could|can|will|do)\b", re.I)
_SHORT_WD = {"sat", "sun", "wed", "mon", "tue", "thu", "fri", "tues", "thur", "thurs"}
_NTH_NOT = re.compile(r"\s+(?:floor|place|time|round|quarter|edition|grade|year|row|step|item|point|attempt)\b", re.I)


def _clip(text, n):
    return rules._clip(str(text or ""), n)


def _hm(h, m):
    return f"{h:02d}:{m:02d}"


def _range(m, is24):
    """("HH:MM", "HH:MM") of a range match, or None."""
    g = m.groupdict()
    h1, m1, h2, m2 = int(g["h1"]), int(g["m1"] or 0), int(g["h2"]), int(g["m2"] or 0)
    if is24:
        a, b = _hm(h1, m1), _hm(h2, m2)
        return (a, b) if b > a else None
    if g["sep"].lower() == "and" and not g["b"]:
        return None
    if not (1 <= h1 <= 12 and 1 <= h2 <= 12):
        return None
    pm2 = g["ap2"].lower() == "p"
    e = h2 % 12 + (12 if pm2 else 0)
    if g["ap1"]:
        s = h1 % 12 + (12 if g["ap1"].lower() == "p" else 0)
    else:
        s = h1 % 12 + (12 if pm2 else 0)
        if s * 60 + m1 >= e * 60 + m2:  # "11-1pm": 11 am
            s = h1 % 12
    a, b = _hm(s, m1), _hm(e, m2)
    return (a, b) if b > a else None


def _tokens(sentence):
    """Date/time tokens of a sentence in order: (start, end, kind, value): kind "date" (a _PIECE date match),
    "time" ("HH:MM") or "range" (("HH:MM", "HH:MM"))."""
    spans, toks = [], []
    for rx, is24 in ((_R12, False), (_R24, True)):
        for m in rx.finditer(sentence):
            if any(m.start() < e and s < m.end() for s, e in spans):
                continue
            r = _range(m, is24)
            if r:
                spans.append((m.start(), m.end()))
                toks.append((m.start(), m.end(), "range", r))
    for p in facts._PIECE.finditer(sentence):
        if any(p.start() < e and s < p.end() for s, e in spans):
            continue
        g = p.groupdict()
        if g["wd"] and g["wd_n"].lower() in _SHORT_WD and not (
                g["wd_n"][0].isupper() and (p.start() > 0 or re.match(r"\.?,?\s+\d", sentence[p.end():]))):
            continue  # "sat", "sun" as words; "Sat" first in a sentence only with a date or time after it
        if g["nth"] and _NTH_NOT.match(sentence, p.end()):
            continue  # "the 3rd floor"
        if g["rel"] and g["rel"].lower() not in ("today", "tonight", "tomorrow"):
            continue  # "EOD" is a deadline word, not an event
        if facts._is_time(p):
            t = facts._time_of(p)
            if t:
                toks.append((p.start(), p.end(), "time", t))
        else:
            toks.append((p.start(), p.end(), "date", p))
    toks.sort(key=lambda t: t[0])
    return toks


def _groups(sentence, toks):
    """Adjacent tokens (joined by spaces, commas, "at", "on", "from") as groups; a group with only a time joins
    the date group before it in the same sentence when that one has no time."""
    groups = []
    for t in toks:
        if groups and _JOIN.fullmatch(sentence[groups[-1][-1][1]:t[0]]):
            groups[-1].append(t)
        else:
            groups.append([t])
    out = []
    for g in groups:
        has_date = any(t[2] == "date" for t in g)
        if not has_date and out and not any(t[2] != "date" for t in out[-1]):
            out[-1] = out[-1] + g
            continue
        out.append(g)
    return out


def find(text, sent):
    """Concrete dates in one message. text: the latest incoming message (subject and snippet, or the chat
    message); sent: the date it was sent in the owner's time zone. Returns up to MAX_PER_ROW
    [{kind "event"|"deadline", date (date), start, end ("HH:MM" or None = all day), phrase (verbatim, clipped),
    cue (the event word, or "")}]. Rules: per sentence; a deadline (facts.due: "due Friday 6 Nov at 5pm", "by
    Thursday") with a date counts; otherwise the sentence needs an event word (meet, call, lunch, appointment,
    "does Tuesday work", "how about" ...) and a date (weekday, "3 Nov", "the 5th", ISO, today/tomorrow) with or
    without a time or range. A time without a date, "next Friday" (unclear week), sentences about the past and
    questions asking the owner for a time ("What time works on Thursday?") don't count."""
    text = _AMPM.sub(lambda m: m.group(1) + m.group(2) + "m", str(text or ""))
    out = []
    for sentence in _SENTENCE.split(text):
        sentence = sentence.strip()
        if not sentence or len(out) >= MAX_PER_ROW:
            continue
        due = facts.due(sentence, sent)
        if due and due.get("date"):
            d = dt.date.fromisoformat(due["date"])
            start = end = None
            if due.get("time"):
                t = dt.datetime.combine(d, dt.time.fromisoformat(due["time"]))
                st = max(t - dt.timedelta(minutes=DEADLINE_MIN), dt.datetime.combine(d, dt.time(0, 0)))
                start, end = f"{st:%H:%M}", due["time"] if due["time"] != "00:00" else None
                if end is None:
                    start = None
            out.append({"kind": "deadline", "date": d, "start": start, "end": end,
                        "phrase": _clip(due["phrase"], MAX_PHRASE), "cue": ""})
            continue
        cue = _CUE.search(sentence)
        if not cue or _PAST.search(sentence):
            continue
        asks = bool(_ASK.search(sentence))
        for g in _groups(sentence, _tokens(sentence)):
            dates = sorted((t[3] for t in g if t[2] == "date"), key=facts._rank)
            if not dates:
                continue
            d = facts._date_of(dates[0], sent)
            if d is None:
                continue
            rng = next((t[3] for t in g if t[2] == "range"), None)
            tm = next((t[3] for t in g if t[2] == "time"), None)
            if rng:
                start, end = rng
            elif tm:
                st = dt.datetime.combine(d, dt.time.fromisoformat(tm))
                en = st + dt.timedelta(minutes=EVENT_MIN)
                start, end = tm, ("24:00" if en.date() != d else f"{en:%H:%M}")
            else:
                if asks:
                    continue  # "When on Friday?" asks the owner; no proposal yet
                if dates[0].group("rel") and dates[0].group("rel").lower() == "tonight":
                    continue
                start = end = None
            out.append({"kind": "event", "date": d, "start": start, "end": end,
                        "phrase": _clip(sentence[g[0][0]:g[-1][1]], MAX_PHRASE),
                        "cue": " ".join(cue.group(0).lower().split())})
            if len(out) >= MAX_PER_ROW:
                break
    return out[:MAX_PER_ROW]


# --- the calendar the owner already has -------------------------------------------------------------------------
def calendar_items(events, tz, day):
    """The owner's events from day to the end of the read window as [{title, date, start, end (None = all day)}]
    for the already-on-calendar check. events: [(event, area_id)] from events.list (cancelled, free and declined
    events left out, as in the schedule)."""
    out = []
    for ev, _ in events:
        if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
            continue
        if rules._self_status(ev) == "declined":
            continue
        start, all_day = rules._when(ev.get("start"), tz)
        end, _ = rules._when(ev.get("end"), tz)
        if start is None:
            continue
        title = (ev.get("summary") or "").strip()
        if all_day:
            last = end - dt.timedelta(days=1) if isinstance(end, dt.date) and end > start else start
            d = start
            while d <= last and (d - start).days < LOOKAHEAD_DAYS:
                out.append({"title": title, "date": d, "start": None, "end": None})
                d += dt.timedelta(days=1)
            continue
        if end is None or end < start:
            end = start
        e_hm = "24:00" if end.date() != start.date() else f"{end:%H:%M}"
        out.append({"title": title, "date": start.date(), "start": f"{start:%H:%M}", "end": e_hm})
    return out


def on_calendar(cand, cal, title):
    """True when the owner's calendar already has it: an event on the same day that overlaps in time (both
    timed) or shares a title word (memory.words: no common words)."""
    tw = set(memory.words(title))
    for e in cal:
        if e["date"] != cand["date"]:
            continue
        if cand["start"] and e["start"] and cand["start"] < (e["end"] or e["start"]) and e["start"] < cand["end"]:
            return True
        if tw & set(memory.words(e["title"])):
            return True
    return False


def _subject(text):
    s = str(text or "")
    while True:
        m = rules._REPLY_SUBJECT.match(s)
        if not m:
            break
        s = s[m.end():]
    s = s.strip().strip('"').strip()
    return "" if s.startswith("…") else s


def title_for(cand, row):
    """The event's title: the mail subject (without Re:/Fwd:), else "<event word> with <who>"; deadlines are
    "Due: <subject>" or "Deadline from <who>"."""
    subj = _subject(row.get("subject")) if row.get("via") == "mail" or row.get("source") == "Mail" else ""
    who = str(row.get("who") or "").strip()
    if cand["kind"] == "deadline":
        return _clip("Due: " + subj if subj else "Deadline from " + (who or "a message"), MAX_TITLE)
    if subj:
        return _clip(subj, MAX_TITLE)
    noun = _NOUNS.get(cand.get("cue") or "", "Meeting")
    return _clip(noun + (" with " + who if who else ""), MAX_TITLE)


def possible_events(rows, cal, day):
    """The brief's "Possible events": rows: the reply rows with {id, key, who, source, via, area, subject, text
    (the latest incoming message), sent (date), account (email or None)}; cal: calendar_items(); day: the brief's
    date. Dates before day or more than MAX_AHEAD_DAYS out, already on the calendar, or found twice are left
    out. Returns up to MAX_EVENTS items in date order."""
    out, seen = [], set()
    for r in rows:
        for c in find(r.get("text"), r["sent"]):
            if c["date"] < day or (c["date"] - day).days > MAX_AHEAD_DAYS:
                continue
            title = title_for(c, r)
            if on_calendar(c, cal, title):
                continue
            sig = (c["date"], c["start"], title.lower())
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"key": rules.row_key(f"pe:{r.get('key')}:{c['date']}:{c['start']}:{c['kind']}"),
                        "row": r["id"], "row_key": r.get("key"), "who": _clip(r.get("who"), 60),
                        "source": r.get("source") or "", "via": r.get("via") or "", "area": r.get("area"),
                        "title": title, "kind": c["kind"], "date": c["date"].isoformat(), "start_hhmm": c["start"],
                        "end_hhmm": c["end"], "all_day": c["start"] is None, "phrase": c["phrase"],
                        "account": r.get("account") or None, "link": r.get("link") or None})
    out.sort(key=lambda p: (p["date"], p["start_hhmm"] or "", p["row"]))
    out = out[:MAX_EVENTS]
    for i, p in enumerate(out, 1):
        p["id"] = f"p{i}"
    return out


# --- the page's fields --------------------------------------------------------------------------------------
def _stamp(d, hhmm):
    if hhmm == "24:00":
        return f"{d + dt.timedelta(days=1):%Y%m%d}T000000"
    return f"{d:%Y%m%d}T{hhmm.replace(':', '')}00"


def calendar_url(p, tz_name, account=None):
    """The prefilled Google Calendar page for a possible event (opened by the owner, who saves it): text,
    dates (local times read in ctz, the owner's time zone; all-day = the day and the next), ctz, details
    (who, where, the phrase as written) and authuser (the Google account the row came from)."""
    d = dt.date.fromisoformat(p["date"])
    if p.get("start_hhmm") and p.get("end_hhmm"):
        dates = _stamp(d, p["start_hhmm"]) + "/" + _stamp(d, p["end_hhmm"])
    else:
        dates = f"{d:%Y%m%d}/{d + dt.timedelta(days=1):%Y%m%d}"
    via = p.get("via")
    src = "Slack" if via == "slack" else "email" if via == "mail" else (p.get("source") or "a message")
    details = f"From {p.get('who') or 'someone'} ({src}): \"{p.get('phrase') or ''}\"\nFound by Callsheet. " \
              "Check the details before you save."
    if isinstance(p.get("link"), str) and p["link"].startswith("https://mail.google.com/"):
        details += "\n" + p["link"]
    params = [("action", "TEMPLATE"), ("text", p.get("title") or "Event"), ("dates", dates),
              ("ctz", tz_name or "UTC"), ("details", details[:MAX_DETAILS])]
    if account:
        params.append(("authuser", account))
    return CAL_BASE + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/@")


def for_page(items, tz_name, fmt, day):
    """Cached possible events with the page's fields: when (display, in the owner's time format), today, and
    url (calendar_url). The account address is only in the url."""
    out = []
    for p in items or []:
        try:
            d = dt.date.fromisoformat(p["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if d < day:
            continue
        label = "Today" if d == day else "Tomorrow" if d == day + dt.timedelta(days=1) else f"{d:%a} {d.day} {d:%b}"
        if p.get("start_hhmm") and p.get("end_hhmm"):
            when = (label + " · due " + rules.clock(p["end_hhmm"], fmt)) if p.get("kind") == "deadline" else \
                label + " · " + rules.clock(p["start_hhmm"], fmt) + "–" + rules.clock(p["end_hhmm"], fmt)
        else:
            when = label + (" · due" if p.get("kind") == "deadline" else " · all day")
        q = {k: v for k, v in p.items() if k not in ("account", "link")}
        q.update(when=when, today=d == day, url=calendar_url(p, tz_name, p.get("account")))
        out.append(q)
    return out
