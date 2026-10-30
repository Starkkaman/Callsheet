"""Facts for the writer (0.4.1): numbers the rules compute from the owner's mail and calendar, so the writer
only words them (waiting time, unanswered messages and follow-ups, clashes, what's due today). Pure: no network,
no AI.

The facts go to the writer in their own <facts> block after the <data> block (writer.build_payload), built only
here; every text from mail or calendars in them is clipped, and build_payload escapes < and > as in <data>.
"""
import datetime as dt
import re

import rules

MAX_WHO = 60
MAX_TITLE = 60
MAX_WHAT = 80
MAX_PHRASE = 60
DUE_GAP = 20         # characters allowed between the deadline word and its date or time (same clause)
MONTHS = {m: i for i, ms in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), 1) for m in ms}
_DAYNAMES = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2, "thursday": 3,
             "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_WD = "|".join(sorted(_DAYNAMES, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)"
# one date or time piece; named groups say which kind
_PIECE = re.compile(
    r"(?P<iso>\b\d{4}-\d{2}-\d{2}\b)"
    r"|(?P<md>\b(?P<md_m>" + _MON + r")\.?\s+(?P<md_d>\d{1,2})" + _ORD + r"?\b)"
    r"|(?P<dm>\b(?:the\s+)?(?P<dm_d>\d{1,2})" + _ORD + r"?\s+(?:of\s+)?(?P<dm_m>" + _MON + r")\b)"
    r"|(?P<wd>\b(?P<wd_next>next\s+)?(?:this\s+)?(?P<wd_n>" + _WD + r")\b)"
    r"|(?P<rel>\b(?:today|tonight|tomorrow|eod|end\s+of\s+(?:the\s+)?day)\b)"
    r"|(?P<nth>\b(?:the\s+)?(?P<nth_d>\d{1,2})" + _ORD + r"\b)"
    r"|(?P<hm>\b(?P<hm_h>\d{1,2})(?::(?P<hm_m>\d{2}))?\s*(?P<hm_ap>[ap])\.?m\b)"
    r"|(?P<h24>\b(?P<h24_h>[01]?\d|2[0-3]):(?P<h24_m>[0-5]\d)\b)"
    r"|(?P<noon>\b(?:noon|midday|midnight)\b)",
    re.I)
_GAP_STOP = re.compile(r"[.!?;\n]")
_JOIN = re.compile(r"[\s,]*(?:(?:at|on|by|@)[\s,]*)?")  # between the pieces of one deadline: "Friday, 6 Nov at 5pm"


def _clip(text, n):
    return rules._clip(str(text or ""), n)


def _date_of(piece, sent):
    """The ISO date a date piece means, read from the message's own date (sent: a date); None when unclear."""
    g = piece.groupdict()
    try:
        if g["iso"]:
            return dt.date.fromisoformat(g["iso"])
        if g["md"] or g["dm"]:
            mon = MONTHS[(g["md_m"] or g["dm_m"]).lower()]
            day = int(g["md_d"] or g["dm_d"])
            d = dt.date(sent.year, mon, day)
            if d < sent - dt.timedelta(days=60):  # "3 January" written in December
                d = dt.date(sent.year + 1, mon, day)
            return d
        if g["wd"]:
            if g["wd_next"]:
                return None  # "next Friday": this week's or next week's, people differ
            return sent + dt.timedelta(days=(_DAYNAMES[g["wd_n"].lower()] - sent.weekday()) % 7)
        if g["rel"]:
            return sent + dt.timedelta(days=1) if g["rel"].lower() == "tomorrow" else sent
        if g["nth"]:
            day = int(g["nth_d"])
            y, m = sent.year, sent.month
            if day < sent.day:
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return dt.date(y, m, day)
    except ValueError:  # 30 February, 0th
        return None
    return None


def _time_of(piece):
    """"HH:MM" (24 h) of a time piece, or None."""
    g = piece.groupdict()
    if g["hm"]:
        h, m = int(g["hm_h"]), int(g["hm_m"] or 0)
        if not 1 <= h <= 12 or m > 59:
            return None
        h = h % 12 + (12 if g["hm_ap"].lower() == "p" else 0)
        return f"{h:02d}:{m:02d}"
    if g["h24"]:
        return f"{int(g['h24_h']):02d}:{g['h24_m']}"
    if g["noon"]:
        return "00:00" if g["noon"].lower() == "midnight" else "12:00"
    return None


def _rank(piece):
    return next(i for i, k in enumerate(("iso", "md", "dm", "nth", "rel", "wd")) if piece.group(k))


def _is_time(piece):
    return bool(piece.group("hm") or piece.group("h24") or piece.group("noon"))


def due(text, sent):
    """A deadline in a message: the first place the deadline rule (rules.DEADLINE) matches that is followed by
    a date, weekday or time in the same clause. text: the message text; sent: the date it was sent (owner's
    time zone). Returns {phrase (verbatim, whitespace collapsed, clipped), date (ISO, when it can be worked out;
    a time alone means the day it was sent), time ("HH:MM", when given)} or None."""
    text = str(text or "")
    for m in rules.DEADLINE.finditer(text):
        word = m.group(0)
        pos = m.start() + 2 if word[:2].lower() == "by" else m.end()  # "by Friday": the date is in the match
        pieces = []
        while len(pieces) < 3:
            p = _PIECE.search(text, pos)
            if p is None:
                break
            gap = text[pos:p.start()]
            if pieces:  # the next piece of the same deadline follows at once
                ok = _JOIN.fullmatch(gap) is not None
            elif word[:2].lower() == "by":  # "by Friday", "by 5pm": right after "by"
                ok = not gap.strip()
            else:  # "the deadline for the report is Friday": same clause, close by
                ok = len(gap) <= DUE_GAP and not _GAP_STOP.search(gap)
            if not ok:
                break
            pieces.append(p)
            pos = p.end()
        if not pieces:
            continue
        dates = sorted((p for p in pieces if not _is_time(p)), key=_rank)  # "Friday 6 Nov": the date wins
        date = _date_of(dates[0], sent) if dates else None
        time = next((t for t in (_time_of(p) for p in pieces if _is_time(p)) if t), None)
        if date is None and time is not None and all(_is_time(p) for p in pieces):
            date = sent
        out = {"phrase": _clip(text[m.start():pos], MAX_PHRASE)}
        if date is not None:
            out["date"] = date.isoformat()
        if time is not None:
            out["time"] = time
        return out
    return None


def clashes(schedule, fmt="24h"):
    """Every pair of overlapping events (rules.overlaps on start_hhmm / end_hhmm, as build_schedule flags
    clashes) in time order: [{start (when the overlap begins, display), a_title, a_area, b_title, b_area}],
    a = the event that starts first. schedule: writer-input rows {start_hhmm, end_hhmm, title, area (name)}."""
    timed = [e for e in schedule if e.get("start_hhmm") and e.get("end_hhmm")]
    timed.sort(key=lambda e: (e["start_hhmm"], e["end_hhmm"]))
    pairs = []
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if rules.overlaps({"start": a["start_hhmm"], "end": a["end_hhmm"]},
                              {"start": b["start_hhmm"], "end": b["end_hhmm"]}):
                pairs.append((max(a["start_hhmm"], b["start_hhmm"]), a["start_hhmm"], b["start_hhmm"], i, a, b))
    pairs.sort(key=lambda p: p[:4])
    return [{"start": rules.clock(at, fmt), "a_title": _clip(a.get("title"), MAX_TITLE),
             "a_area": _clip(a.get("area"), MAX_TITLE), "b_title": _clip(b.get("title"), MAX_TITLE),
             "b_area": _clip(b.get("area"), MAX_TITLE)} for at, _, _, _, a, b in pairs]


def _days(n):
    return "1 day" if n == 1 else f"{n} days"


def wait_text(waiting, last_nudge=None):
    """The ready-made wait wording of a row: "waiting 5 days", "waiting 1 day", "waiting since today" (0 days);
    with a different last_nudge "waiting 11 days, last nudge 8 days ago" / "..., last nudge 1 day ago" /
    "..., last nudge today"."""
    out = "waiting since today" if waiting == 0 else "waiting " + _days(waiting)
    if last_nudge is not None and last_nudge != waiting:
        out += ", last nudge " + ("today" if last_nudge == 0 else _days(last_nudge) + " ago")
    return out


def _display_date(ts, tz, fmt):
    d = dt.datetime.fromtimestamp(ts, tz)
    return f"{d:%a %d %b} " + rules.clock(d, fmt)


def build(replies, schedule, day, tz, fmt="24h", reply_age_days=0):
    """The facts block's content.
    replies: [{id, who, text (subject or quoted message), vip, facts: {first (epoch s of the first unanswered
    message, or None), count (messages by others since the owner's last one), last (epoch s of the last message),
    text (where to look for a deadline)}}] in brief order; schedule: writer-input rows (clashes()); day: the
    brief's date; tz: the owner's time zone. Each row gets unanswered_messages = count and follow_ups = count - 1,
    last_nudge_days when it differs from waiting_days, and wait = wait_text() of the two.
    waiting_days = calendar days in the owner's time zone from the first unanswered message to the brief's
    date; overdue = rows waiting more than reply_age_days (0 = no age rule, so overdue
    is 0, as for the "Waiting" reason)."""
    rows, today, oldest = {}, [], None
    vip = overdue = 0
    for r in replies:
        vip += bool(r.get("vip"))
        f = r.get("facts")
        if not isinstance(f, dict) or f.get("first") is None:
            continue
        first = dt.datetime.fromtimestamp(f["first"], tz)
        waiting = max(0, (day - first.date()).days)
        n = int(f.get("count") or 0)
        # unanswered_messages counts the first one too; follow_ups = the times they chased (one message = 0)
        row = {"waiting_days": waiting, "unanswered_messages": n, "follow_ups": max(0, n - 1),
               "first_unanswered": _display_date(f["first"], tz, fmt)}
        # days since their latest message, only when it differs from waiting_days ("last nudge 8 days ago")
        last_nudge = max(0, (day - dt.datetime.fromtimestamp(f.get("last") or f["first"], tz).date()).days)
        if last_nudge != waiting:
            row["last_nudge_days"] = last_nudge
        # the same as words, for the writer to use as written ("waiting 11 days, last nudge 8 days ago")
        row["wait"] = wait_text(waiting, last_nudge)
        d = due(f.get("text"), dt.datetime.fromtimestamp(f.get("last") or f["first"], tz).date())
        if d:
            row["due"] = {k: d[k] for k in ("phrase", "date") if k in d}
            if d.get("date") == day.isoformat():
                today.append((d.get("time") or "99:99", len(today), {
                    "ref": r["id"], "what": _clip(r.get("text"), MAX_WHAT),
                    "time": rules.clock(d["time"], fmt) if d.get("time") else None}))
        rows[r["id"]] = row
        if reply_age_days > 0 and waiting > reply_age_days:
            overdue += 1
        if oldest is None or (waiting, -f["first"]) > (oldest[0], -oldest[1]):
            oldest = (waiting, f["first"], {"ref": r["id"], "who": _clip(r.get("who"), MAX_WHO),
                                            "waiting_days": waiting})
    today.sort(key=lambda x: x[:2])
    return {"oldest_waiting": oldest[2] if oldest else None,
            "clashes": clashes(schedule, fmt),
            "due_today": [x[2] for x in today],
            "counts": {"open_replies": len(replies), "vip": vip, "overdue": overdue},
            "replies": rows}
