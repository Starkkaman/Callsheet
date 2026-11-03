"""0.8.0 Coming up: a rules-only horizon over the next 60 days (pure; no AI, nothing reaches the writer).

Sources: calendar events read 60 days ahead (all-day, multi-day, the Birthdays calendar), invites (calendar
invites not answered, and invites found only in email), project milestones and due dates, and dated notes. Each
item gets a kind (travel, big event, deadline, renewal, birthday, invite, unverified, event) by rules, and the
heads-up timing table below decides when it is a "Heads up today". The owner's marks (Got it, Remind me a week
before, Not big, Big) are stored per item key in callsheet.db's meta (`horizon_marks`).

| Kind | Heads up at (days before) |
| travel | 14, 2, 1 |  big event | 60, 30, 14, 2, 1 |  deadline | 14, 7, 2, 0 |  renewal | 14, 3 |  birthday | 7, 0 |
| invite not answered | from arrival until answered; once a day when < 7 days away |
| unverified (email-only) | no timed heads up; listed with its mark |
"""
import datetime as dt
import re

WINDOW_DAYS = 60
WEEKS_DAYS = 14
LIST_MAX = 5
MAX_ITEMS = 300
MARK_KEEP_DAYS = 120
ACTIONS = ("got", "week", "notbig", "big", "undo")

LEADS = {"travel": (14, 2, 1), "big": (60, 30, 14, 2, 1), "deadline": (14, 7, 2, 0), "renewal": (14, 3),
         "birthday": (7, 0)}
KIND_TEXT = {"travel": "Travel", "big": "Big event", "deadline": "Deadline", "renewal": "Renewal",
             "birthday": "Birthday", "invite": "Invite", "unverified": "Invite in email", "event": "Event",
             "milestone": "Milestone"}

TRAVEL = re.compile(r"(?:\b(?:flight|fly(?:ing)?\s+to|trip|travel(?:ling|ing)?|hotel|check[-\s]?in|airbnb|"
                    r"train\s+to|itinerary|boarding|departure|layover)\b|✈)", re.I)
RENEWAL = re.compile(r"\b(?:renew(?:s|al)?|expir(?:es|y|ation)|subscription|insurance|payment\s+due|bill\s+due|"
                     r"annual\s+fee)\b", re.I)
DEADLINE = re.compile(r"\b(?:deadline|due|submit|submission|filing|tax(?:es)?\s+(?:day|return))\b", re.I)
BIG = re.compile(r"\b(?:conference|wedding|show|offsite|off-site|festival|summit|launch|premiere|retreat|"
                 r"graduation|expo|tour|concert|gala)\b", re.I)
BIRTHDAY = re.compile(r"\bbirthday\b", re.I)


def _date(v):
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.datetime):
        return v.date()
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _local_date(v, tz):
    s = str(v or "")
    if "T" in s:
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=tz)).astimezone(tz).date()
    return _date(s)


def classify_event(ev):
    """The kind of a calendar event (compact form from the sync): birthday, travel, renewal, deadline, big or
    event (plain: not listed), and whether it is big."""
    title = ev.get("title") or ""
    if ev.get("holiday"):
        return "event", False
    if ev.get("birthday") or (ev.get("all_day") and BIRTHDAY.search(title) and ev.get("days", 1) <= 1):
        return "birthday", False
    if TRAVEL.search(title):
        return "travel", True
    if RENEWAL.search(title):
        return "renewal", False
    if DEADLINE.search(title):
        return "deadline", False
    big = bool(BIG.search(title)) or (ev.get("all_day") and ev.get("days", 1) > 1) or \
        (ev.get("attendees") or 0) >= 5 or bool(ev.get("external"))
    if ev.get("all_day") and ev.get("days", 1) <= 1 and not BIG.search(title):
        big = (ev.get("attendees") or 0) >= 5 or bool(ev.get("external"))
    return ("big", True) if big else ("event", False)


def items(events, invites, milestones, notes, today, tz):
    """Every horizon candidate: [{key, title, kind, big, date, end_date, src}] in the window."""
    out, last = [], today + dt.timedelta(days=WINDOW_DAYS)
    for ev in events or []:
        d = _local_date(ev.get("start"), tz)
        if d is None or not today <= d <= last:
            continue
        kind, big = classify_event(ev)
        e = _local_date(ev.get("end"), tz)
        if ev.get("all_day") and e:
            e = e - dt.timedelta(days=1)
        out.append({"key": ev.get("key") or "", "title": ev.get("title") or "(No title)", "kind": kind, "big": big,
                    "date": d, "end_date": e if e and e > d else None, "src": "From your calendar"})
    for inv in invites or []:
        d = _local_date(inv.get("start"), tz)
        if d is None or not today <= d <= last or inv.get("status") == "cancelled":
            continue
        if inv.get("kind") == "email":
            out.append({"key": inv["key"], "title": inv.get("title") or "", "kind": "unverified", "big": False,
                        "date": d, "end_date": None, "src": "From email · not on a calendar Callsheet reads"})
        elif inv.get("status") == "needs_action":
            out.append({"key": inv["key"], "title": inv.get("title") or "", "kind": "invite", "big": False,
                        "date": d, "end_date": None, "src": "Not answered yet"})
    for m in milestones or []:
        d = _date(m.get("date"))
        if d is None or not today <= d <= last:
            continue
        out.append({"key": m.get("key") or "", "title": m.get("title") or "", "kind": "deadline", "big": True,
                    "date": d, "end_date": None, "src": "Project " + (m.get("project") or "")})
    for n in notes or []:
        d = _date(n.get("date"))
        if d is None or not today <= d <= last:
            continue
        kind = "travel" if TRAVEL.search(n.get("title") or "") else "renewal" if RENEWAL.search(
            n.get("title") or "") else "birthday" if BIRTHDAY.search(n.get("title") or "") else "deadline"
        out.append({"key": n.get("key") or "", "title": n.get("title") or "", "kind": kind, "big": kind == "travel",
                    "date": d, "end_date": None, "src": "From your notes"})
    # one item per key (the same invite on two calendars)
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: (x["date"], x["title"])):
        if it["key"] and it["key"] in seen:
            continue
        seen.add(it["key"])
        uniq.append(it)
    return uniq[:MAX_ITEMS]


def leads_for(item, mark):
    """The heads-up lead points (days before) for an item after the owner's mark, or () for none."""
    kind = item["kind"]
    action = (mark or {}).get("action")
    if kind in ("unverified", "event") and action != "big":
        return (7,) if action == "week" and kind == "event" else ()
    if action == "notbig":
        return ()
    if action == "big" or (item.get("big") and kind not in LEADS):
        base = LEADS["big"]
    else:
        base = LEADS.get(kind, ())
    if mark and (mark.get("week") or action == "week"):
        base = tuple(sorted(set(base) | {7}, reverse=True))
    return base


def lead_point(item, mark, today):
    """The current lead point (a string key), or None when no heads up is due today. Invites not answered: every
    day when less than 7 days away ("d:<date>"), else once from arrival ("arrival")."""
    days = (item["date"] - today).days
    if days < 0:
        return None
    if item["kind"] == "invite" and (mark or {}).get("action") != "notbig":
        return f"d:{today.isoformat()}" if days < 7 else "arrival"
    due = [lead for lead in leads_for(item, mark) if days <= lead]
    return f"l:{min(due)}" if due else None


def _when(item, today):
    d, e = item["date"], item.get("end_date")
    days = (d - today).days
    rel = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days < 14 else \
        f"in {days // 7} weeks"
    if e and e != d:
        span = f"{d:%a} {d.day}" + (f" {d:%b}" if e.month != d.month else "") + f" to {e:%a} {e.day} {e:%b}"
    else:
        span = f"{d:%a} {d.day} {d:%b}"
    return span, rel


def build(cands, marks, today):
    """{"heads": [...], "weeks": [...], "months": [...], "n": {...}}. Every candidate except plain events lands in
    exactly one list (nothing is dropped; Not big moves an item out of the heads ups, not out of the lists)."""
    heads, weeks, months = [], [], []
    for it in cands:
        mark = (marks or {}).get(it["key"]) if it["key"] else None
        action = (mark or {}).get("action")
        kind = it["kind"]
        if kind == "event" and action not in ("big", "week"):
            continue
        point = lead_point(it, mark, today)
        quiet = bool(point and mark and mark.get("got") == point)
        span, rel = _when(it, today)
        big = (it.get("big") or action == "big") and action != "notbig"
        row = {"key": it["key"], "title": it["title"], "kind": kind, "kind_text": KIND_TEXT.get(kind, "Event"),
               "date": it["date"].isoformat(), "d": str(it["date"].day), "m": f"{it['date']:%b}",
               "when": span, "rel": rel, "src": it["src"], "big": bool(big), "days": (it["date"] - today).days,
               "mark": action, "week": bool(mark and (mark.get("week") or action == "week"))}
        if point and not quiet:
            row["heads"] = True
            heads.append(row)
        elif row["days"] <= WEEKS_DAYS:
            weeks.append(row)
        else:
            months.append(row)
    return {"heads": heads, "weeks": weeks, "months": months,
            "n": {"heads": len(heads), "weeks": len(weeks), "months": len(months)}, "list_max": LIST_MAX}


def apply_mark(marks, key, action, cands, today):
    """The owner's action on an item (Got it, Remind me a week before, Not big, Big, undo). Returns the new marks.
    Got it records the current lead point, so the item is quiet until the next one."""
    if action not in ACTIONS or not isinstance(key, str) or not key or len(key) > 200:
        raise ValueError("unknown action")
    marks = {k: dict(v) for k, v in (marks or {}).items() if isinstance(v, dict)}
    it = next((c for c in cands if c["key"] == key), None)
    if it is None:
        raise KeyError(key)
    cur = marks.get(key, {})
    if action == "undo":
        marks.pop(key, None)
    elif action == "got":
        cur = dict(cur, got=lead_point(it, cur, today), at=today.isoformat())
        cur.setdefault("action", "got")
        marks[key] = cur
    elif action == "week":
        marks[key] = dict(cur, action=cur.get("action") if cur.get("action") in ("big", "notbig") else "week",
                          week=True, at=today.isoformat())
    else:
        marks[key] = dict(cur, action=action, at=today.isoformat())
    # forget marks for items long gone
    cutoff = (today - dt.timedelta(days=MARK_KEEP_DAYS)).isoformat()
    return {k: v for k, v in marks.items() if str(v.get("at") or "") >= cutoff}
