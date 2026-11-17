"""The deadline rule Projects' Slipped list uses (0.4.1, kept in the rework): `due` finds a deadline phrase in a
message and works out its date from the day it was sent. Pure: no network, no AI. The 0.4.1 writer facts (waiting
times, clashes, the facts block) are retired with the rules brief."""
import datetime as dt
import re

import rules


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


