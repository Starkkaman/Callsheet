"""Plain rules that turn calendar events and mail threads into the Daily Brief. No network, no AI.

Everything here is a pure function of its inputs so it can be tested directly.
"""
import datetime as dt
import email.utils
import html
import re
import urllib.parse
import zoneinfo

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEADLINE = re.compile(r"\b(?:deadline|due|by\s+(?:mon(?:day)?|tues?(?:day)?|wed(?:nesday)?|thu(?:rs?)?(?:day)?|"
                      r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|tomorrow|today|eod|end\s+of\s+day)\b|by\s+\d)",
                      re.I)
AUTOMATED_SENDER = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|notifications?|mailer-daemon|"
                              r"postmaster|alerts?|bounces?)(?:$|[._+-])", re.I)
SECTIONS_SAMPLE = {"schedule": "sample", "replies": "sample", "headsup": "sample", "projects": "sample"}
SECTIONS_LIVE = {"schedule": "live", "replies": "live", "headsup": "live", "projects": "sample"}


def tzinfo(settings):
    try:
        return zoneinfo.ZoneInfo(settings.get("timezone") or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def day_bounds(day, tz):
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz)
    return start, end


def _clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def hhmm(d):
    return f"{d:%H:%M}"


# --- call times --------------------------------------------------------------
def _calls(settings):
    out = []
    for c in settings.get("call_times", []):
        if c.get("enabled"):
            h, m = (int(x) for x in c["time"].split(":"))
            out.append((dt.time(h, m), c))
    return sorted(out, key=lambda x: x[0])


def is_day_off(settings, day):
    return WEEKDAYS[day.weekday()] not in settings.get("workdays", WEEKDAYS)


def next_call(settings, after):
    """First enabled call time strictly after `after` (aware datetime). On days off, check-ins
    (midday, afternoon) are skipped when skip_checkins_days_off is set; the morning brief still runs.
    Returns (datetime, call) or (None, None)."""
    tz = tzinfo(settings)
    after = after.astimezone(tz)
    calls = _calls(settings)
    for i in range(9):
        day = after.date() + dt.timedelta(days=i)
        off = settings.get("skip_checkins_days_off") and is_day_off(settings, day)
        for t, c in calls:
            if off and c["id"] != "morning":
                continue
            when = dt.datetime.combine(day, t, tzinfo=tz)
            if when > after:
                return when, c
    return None, None


def kind_for(settings, at):
    """Label of the latest enabled call time at or before `at` (local), e.g. "Midday check-in"."""
    at = at.astimezone(tzinfo(settings))
    kind = "Morning brief"
    for t, c in _calls(settings):
        if t <= at.time().replace(second=0, microsecond=0):
            kind = c.get("label") or kind
    return kind


# --- calendar ----------------------------------------------------------------
def _when(part, tz):
    """(aware datetime, False) for timed, (date, True) for all-day, (None, None) if unreadable."""
    part = part or {}
    try:
        if part.get("dateTime"):
            d = dt.datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=zoneinfo.ZoneInfo(part["timeZone"]) if part.get("timeZone") else tz)
            return d.astimezone(tz), False
        if part.get("date"):
            return dt.date.fromisoformat(part["date"]), True
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        pass
    return None, None


def _self_status(ev):
    for a in ev.get("attendees") or []:
        if a.get("self"):
            return a.get("responseStatus")
    return None


def _others(ev):
    return [a for a in ev.get("attendees") or [] if not a.get("self") and not a.get("resource")]


def _note(ev):
    if ev.get("location"):
        return _clip(ev["location"], 80)
    desc = ev.get("description") or ""
    desc = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", desc)
    desc = html.unescape(re.sub(r"<[^>]+>", "", desc))
    for line in desc.splitlines():
        if line.strip():
            return _clip(line, 80)
    return None


def build_schedule(items, tz, day):
    """items: [(event, area_id)] from events.list for `day`. Returns (rows, all_day_titles)."""
    day_start, day_end = day_bounds(day, tz)
    timed, all_day, seen = [], [], set()
    for ev, area in items:
        if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
            continue
        if _self_status(ev) == "declined":
            continue
        start, is_all_day = _when(ev.get("start"), tz)
        end, _ = _when(ev.get("end"), tz)
        if start is None:
            continue
        key = (ev.get("iCalUID") or ev.get("id"), str(start))
        if key in seen:  # the same event on two of your calendars
            continue
        seen.add(key)
        title = (ev.get("summary") or "").strip() or "(No title)"
        if is_all_day:
            all_day.append(title)
            continue
        if end is None or end < start:
            end = start
        if end <= day_start or start >= day_end:
            continue
        timed.append({"start": start, "end": end, "title": title, "area": area, "ev": ev})
    timed.sort(key=lambda e: (e["start"], e["end"]))

    first = next((e for e in timed if _others(e["ev"])), timed[0] if timed else None)
    rows = []
    for e in timed:
        shown = max(e["start"], day_start)
        row = {"time": hhmm(shown), "end": hhmm(e["end"]) if e["end"] < day_end else "24:00",
               "title": e["title"], "short": _clip(e["title"], 24), "area": e["area"]}
        flags = []
        if _self_status(e["ev"]) == "needsAction":
            flags.append("Not accepted yet")
        clash = next((o for o in timed if o is not e and e["start"] < o["end"] and o["start"] < e["end"]), None)
        if clash is not None:
            row["clash"] = True
            flags.append(f"Overlaps {hhmm(max(clash['start'], day_start))} {_clip(clash['title'], 40)}")
        if flags:
            row["flag"] = {"text": " · ".join(flags)}
        note = _note(e["ev"])
        if note:
            row["note"] = note
        if e is first:
            row["first"] = True
        rows.append(row)
    return rows, all_day


# --- mail --------------------------------------------------------------------
def _headers(msg):
    return {h.get("name", "").lower(): h.get("value", "") for h in (msg.get("payload") or {}).get("headers", [])}


def is_automated(hdr, addr):
    """Bulk or machine-sent mail: mailing lists, newsletters, auto-replies, no-reply senders."""
    if hdr.get("list-id") or hdr.get("list-unsubscribe"):
        return True
    if hdr.get("precedence", "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = hdr.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return True
    return bool(AUTOMATED_SENDER.search(addr.split("@", 1)[0]))


def _addressed(hdr, names, me):
    return any(a.lower() == me for _, a in email.utils.getaddresses([hdr.get(n, "") for n in names]))


def reply_row(thread, account_email, area, now_ts, reply_age_days, vips):
    """One "Needs a reply" row for a threads.get(format=metadata) result, or None.
    vips: {lowercase email: name}. Bulk and automated mail is dropped unless the sender is a VIP.
    A question counts when you are in To; waiting and deadlines when you are in To or Cc."""
    msgs = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
    if not msgs:
        return None
    last = max(msgs, key=lambda m: int(m.get("internalDate") or 0))
    hdr = _headers(last)
    name, addr = email.utils.parseaddr(hdr.get("from", ""))
    addr = addr.lower()
    me = account_email.lower()
    if "SENT" in (last.get("labelIds") or []) or (addr and addr == me):
        return None
    vip = addr in vips
    if not vip and is_automated(hdr, addr):
        return None
    in_to = _addressed(hdr, ("to",), me)
    in_to_cc = in_to or _addressed(hdr, ("cc",), me)
    subject = " ".join(hdr.get("subject", "").split())
    snippet = " ".join(html.unescape(last.get("snippet") or "").split())
    age_s = max(0.0, now_ts - int(last.get("internalDate") or 0) / 1000)
    age_days = int(age_s // 86400)
    reasons = []
    if vip:
        reasons.append("VIP")
    if in_to and ("?" in snippet or "?" in subject):
        reasons.append("Direct question")
    if in_to_cc and (DEADLINE.search(subject) or DEADLINE.search(snippet)):
        reasons.append("Mentions a deadline")
    if in_to_cc and reply_age_days > 0 and age_days >= reply_age_days:
        reasons.append(f"Waiting {age_days} day{'s' if age_days != 1 else ''}")
    if not reasons:
        return None
    text = _clip(subject, 120) if subject else '"' + _clip(snippet, 118) + '"'
    who = name.strip() or vips.get(addr) or addr or "Unknown sender"
    link = ("https://mail.google.com/mail/u/" + urllib.parse.quote(account_email, safe="@.")
            + "/#all/" + urllib.parse.quote(str(thread.get("id", "")), safe=""))
    return {"who": _clip(who, 60), "area": area, "source": "Mail", "text": text, "short": _clip(text, 40),
            "age": f"{age_days}d" if age_days >= 1 else f"{max(1, int(age_s // 3600))}h",
            "reasons": reasons, "vip": vip, "action": "Open", "link": link, "_age_s": age_s}


def sort_replies(rows):
    rows.sort(key=lambda r: (not r["vip"], -r["_age_s"]))
    for r in rows:
        r.pop("_age_s", None)
    return rows


# --- the brief ---------------------------------------------------------------
def date_fields(day):
    return {"date": day.isoformat(), "date_long": f"{day:%A} {day.day} {day:%b}",
            "date_short": f"{day:%a} {day.day} {day:%b}"}


def compose_brief(sample, settings, areas, cache, live, now, writer_on=False):
    """sample: the bundled sample brief. cache: the brief saved by the last sync (or None).
    live: at least one account is connected. now: aware datetime. writer_on: show the AI writer's
    summary and suggestions from the cache (hidden once the writer is turned off)."""
    if not live:
        brief = dict(sample)
        brief["sample"] = True
        brief["sections"] = dict(SECTIONS_SAMPLE)
        return brief
    tz = tzinfo(settings)
    now = now.astimezone(tz)
    nxt, _ = next_call(settings, now)
    brief = {"sample": False, "sections": dict(SECTIONS_LIVE),
             "areas": [{"id": a["id"], "name": a["name"], "colour": a["colour"]} for a in areas],
             "next_checkin": hhmm(nxt) if nxt else None,
             "projects": sample.get("projects", [])}
    if cache:
        written = dt.datetime.fromisoformat(cache["written_at"]).astimezone(tz)
        brief.update(date_fields(dt.date.fromisoformat(cache["date"])))
        brief.update(kind=cache.get("kind") or "Morning brief", written_at=hhmm(written),
                     schedule=cache.get("schedule", []), replies=cache.get("replies", []),
                     heads_up=cache.get("heads_up", []), sources=cache.get("sources", ""))
        summary = cache.get("summary")
        if writer_on and isinstance(summary, dict) and summary.get("text"):
            at = dt.datetime.fromisoformat(summary["at"]).astimezone(tz) if summary.get("at") else None
            brief["summary"] = {"text": summary["text"], "by": summary.get("by") or "",
                                "at": hhmm(at) if at else None}
            brief["sections"]["summary"] = "live"
        if not writer_on:
            brief["replies"] = [{k: v for k, v in r.items() if k != "suggestion"} for r in brief["replies"]]
    else:
        brief.update(date_fields(now.date()))
        brief.update(kind=kind_for(settings, now), written_at=None, schedule=[], replies=[],
                     heads_up=["Callsheet hasn't read your accounts yet. It reads them at the next check-in."],
                     sources="Not read yet")
    return brief
