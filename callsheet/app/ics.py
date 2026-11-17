"""0.8.0 "See everything": a small, pure iCalendar (RFC 5545) reader for invites found in mail.

No network, no AI, no imports beyond the standard library. It reads only what the Agenda needs (METHOD, VEVENT UID,
SEQUENCE, RECURRENCE-ID, DTSTART/DTEND/DURATION with TZID, UTC or floating times, RRULE for the next few instances,
EXDATE, SUMMARY, ORGANIZER, the owner's ATTENDEE PARTSTAT, LOCATION, URL, STATUS) and ignores everything else
(DESCRIPTION, X-ALT-DESC HTML, ATTACH, VALARM, ...). Text is cleaned (controls, bidi overrides and zero-width
characters removed, look-alike letters marked) and links are defanged: nothing from an ICS file is ever a link.

Limits keep a hostile file cheap: MAX_BYTES of text, MAX_LINES lines, MAX_DEPTH nested components, MAX_EVENTS
VEVENTs, MAX_VALUE characters per value, a capped attendee count and MAX_RRULE_STEPS recurrence steps.
"""
import datetime as dt
import re
import unicodedata
import zoneinfo

MAX_BYTES = 256 * 1024    # the attachment cap too (gather.MAX_ATTACHMENT)
MAX_LINES = 20000
MAX_DEPTH = 6
MAX_COMPONENTS = 400
MAX_EVENTS = 20
MAX_VALUE = 4000
MAX_ATTENDEES = 1000      # counting stops here
MAX_EXDATES = 200
MAX_RRULE_STEPS = 1000
MAX_INSTANCES = 3
MAX_TITLE = 200
MAX_NAME = 80
YEAR_MIN, YEAR_MAX = 1990, 2100

INVITE_METHODS = ("REQUEST", "PUBLISH", "ADD")      # an event to answer or to note
CANCEL_METHODS = ("CANCEL",)
REPLY_METHODS = ("REPLY", "COUNTER", "REFRESH", "DECLINECOUNTER")  # answers about an invite: never an invite

# Windows (Outlook, Teams) time zone names -> IANA, the common ones
WINDOWS_TZ = {
    "pacific standard time": "America/Los_Angeles", "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix", "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York", "us eastern standard time": "America/Indiana/Indianapolis",
    "alaskan standard time": "America/Anchorage", "hawaiian standard time": "Pacific/Honolulu",
    "atlantic standard time": "America/Halifax", "newfoundland standard time": "America/St_Johns",
    "gmt standard time": "Europe/London", "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin", "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest", "central european standard time": "Europe/Warsaw",
    "e. europe standard time": "Europe/Chisinau", "fle standard time": "Europe/Kiev", "gtb standard time":
    "Europe/Bucharest", "russian standard time": "Europe/Moscow", "israel standard time": "Asia/Jerusalem",
    "india standard time": "Asia/Kolkata", "china standard time": "Asia/Shanghai", "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul", "singapore standard time": "Asia/Singapore",
    "aus eastern standard time": "Australia/Sydney", "e. australia standard time": "Australia/Brisbane",
    "cen. australia standard time": "Australia/Adelaide", "w. australia standard time": "Australia/Perth",
    "new zealand standard time": "Pacific/Auckland", "sa pacific standard time": "America/Bogota",
    "e. south america standard time": "America/Sao_Paulo", "mexico standard time": "America/Mexico_City",
    "central standard time (mexico)": "America/Mexico_City", "south africa standard time": "Africa/Johannesburg",
    "arabian standard time": "Asia/Dubai", "utc": "UTC", "coordinated universal time": "UTC",
}

# characters that change how text looks without being visible: bidi overrides/isolates, zero-width, BOM, soft hyphen
_INVISIBLE = re.compile("[­؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁯"
                        "ㅤ﻿ﾠ\U000e0000-\U000e007f]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SCHEME_URL = re.compile(r"\b([a-z][a-z0-9+.-]{1,20})://([^\s<>\"']+)", re.I)
_WWW = re.compile(r"\b(www\.[^\s<>\"']+)", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_DATETIME = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)")
_DURATION = re.compile(r"([+-])?P(?:(\d{1,6})W)?(?:(\d{1,6})D)?(?:T(?:(\d{1,6})H)?(?:(\d{1,6})M)?(?:(\d{1,6})S)?)?$")
_OFFSET = re.compile(r"([+-])(\d{2})(\d{2})(\d{2})?$")
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class _Bad(Exception):
    """A value that can't be read; the event (or the file) is left out."""


# --- text ---------------------------------------------------------------------------------------------------------
def _scripts(text):
    """The letter scripts used in text (LATIN, CYRILLIC, GREEK, ...), from the Unicode names."""
    out = set()
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            out.add(name.split(" ")[0] if name else "?")
    return out


def clean(text, n=MAX_TITLE):
    """(display text, marked): controls, bidi overrides and zero-width characters removed, whitespace collapsed,
    clipped to n. marked is True when invisible characters were removed or the letters mix scripts (Latin with
    Cyrillic or Greek: look-alike letters), so the page can say so."""
    text = str(text or "")
    stripped = _INVISIBLE.sub("", text)
    marked = stripped != text
    stripped = _CONTROL.sub(" ", stripped)
    stripped = " ".join(stripped.split())
    sc = _scripts(stripped)
    if "LATIN" in sc and sc & {"CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE"}:
        marked = True
    if len(stripped) > n:
        stripped = stripped[: n - 1].rstrip() + "…"
    return stripped, marked


def defang(text):
    """Links become plain text that can't be clicked or pasted by accident: hxxps://host[.]example/path."""
    def host(h):
        return h.replace(".", "[.]")

    def sub(m):
        scheme, rest = m.group(1), m.group(2)
        hp, sep, path = rest.partition("/")
        s = scheme.lower().replace("http", "hxxp").replace("ftp", "fxp")
        return f"{s}://{host(hp)}{sep}{path}"

    text = _SCHEME_URL.sub(sub, str(text or ""))
    text = _WWW.sub(lambda m: host(m.group(1).split("/", 1)[0]) + ("/" + m.group(1).split("/", 1)[1]
                                                                     if "/" in m.group(1) else ""), text)
    return text


def _unescape(value):
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


# --- lines --------------------------------------------------------------------------------------------------------
def _decode(raw):
    if isinstance(raw, bytes):
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", errors="replace")
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _content_line(line):
    """'NAME;P=V;Q="a:b":value' -> (NAME, {P: V, Q: a:b}, value), or None for a line that isn't one."""
    m = re.match(r"([A-Za-z0-9-]{1,64})", line)
    if not m:
        return None
    name, i, params = m.group(1).upper(), m.end(), {}
    while i < len(line) and line[i] == ";":
        j = i + 1
        k = line.find("=", j)
        if k < 0:
            return None
        pname = line[j:k].upper()
        i = k + 1
        vals = []
        while True:
            if i < len(line) and line[i] == '"':
                e = line.find('"', i + 1)
                if e < 0:
                    return None
                vals.append(line[i + 1:e])
                i = e + 1
            else:
                e = i
                while e < len(line) and line[e] not in ";:,":
                    e += 1
                vals.append(line[i:e])
                i = e
            if i < len(line) and line[i] == ",":
                i += 1
                continue
            break
        if len(params) < 20:
            params[pname[:40]] = ",".join(vals)[:200]
    if i >= len(line) or line[i] != ":":
        return None
    return name, params, line[i + 1:i + 1 + MAX_VALUE]


# --- dates --------------------------------------------------------------------------------------------------------
def _zone(tzid, vtz, default_tz):
    """(tzinfo, guessed): an IANA name, a Windows name, a "/vendor/.../Area/City" path, the file's own VTIMEZONE
    offset (a fixed-offset guess), else the owner's zone (a guess)."""
    name = (tzid or "").strip().strip('"')
    if not name:
        return default_tz, False
    cands = [name, WINDOWS_TZ.get(name.lower())]
    if "/" in name:
        parts = [p for p in name.split("/") if p]
        cands += ["/".join(parts[-3:]), "/".join(parts[-2:])]
    for c in cands:
        if c and re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", c) and ".." not in c:
            try:
                return zoneinfo.ZoneInfo(c), False
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                pass
    off = vtz.get(name)
    if off is not None:
        return dt.timezone(off), True
    return default_tz, True


def _offset(value):
    m = _OFFSET.match(value.strip())
    if not m:
        raise _Bad("offset")
    h, mi = int(m.group(2)), int(m.group(3))
    if h > 18 or mi > 59:
        raise _Bad("offset")
    d = dt.timedelta(hours=h, minutes=mi)
    return -d if m.group(1) == "-" else d


def _when(value, params, vtz, default_tz):
    """(datetime or date, all_day, floating, tz_guess); raises _Bad for anything unreadable or out of range."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        m = _DATE.fullmatch(value)
        if not m:
            raise _Bad("date")
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise _Bad("date") from None
        if not YEAR_MIN <= d.year <= YEAR_MAX:
            raise _Bad("year")
        return d, True, False, False
    m = _DATETIME.fullmatch(value)
    if not m:
        raise _Bad("datetime")
    try:
        naive = dt.datetime(*(int(m.group(k)) for k in range(1, 7)))
    except ValueError:
        raise _Bad("datetime") from None
    if not YEAR_MIN <= naive.year <= YEAR_MAX:
        raise _Bad("year")
    if m.group(7):
        return naive.replace(tzinfo=dt.timezone.utc), False, False, False
    if params.get("TZID"):
        tz, guess = _zone(params["TZID"], vtz, default_tz)
        return naive.replace(tzinfo=tz), False, False, guess
    return naive.replace(tzinfo=default_tz), False, True, False


def _duration(value):
    m = _DURATION.match(value.strip().upper())
    if not m or not any(m.group(k) for k in range(2, 7)):
        raise _Bad("duration")
    w, d, h, mi, s = (int(m.group(k) or 0) for k in range(2, 7))
    days = w * 7 + d
    if days > 3660:
        raise _Bad("duration")
    out = dt.timedelta(days=days, hours=h, minutes=mi, seconds=s)
    if m.group(1) == "-":
        raise _Bad("duration")
    return out


def _iso(v):
    return v.isoformat() if isinstance(v, dt.datetime) else v.isoformat()


# --- recurrence ---------------------------------------------------------------------------------------------------
def _rrule(value):
    rule = {}
    for part in value.split(";")[:20]:
        k, _, v = part.partition("=")
        rule[k.strip().upper()] = v.strip().upper()
    return rule


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    if not YEAR_MIN <= y <= YEAR_MAX:
        raise _Bad("year")
    try:
        return d.replace(year=y, month=m + 1)
    except ValueError:  # 31 Feb: that month is skipped (RFC 5545)
        return None


def instances(ev, window_start, window_end, limit=MAX_INSTANCES):
    """Up to `limit` (start, end) pairs of a parsed event that end after window_start and start before window_end
    (aware datetimes; all-day events count from the owner's midnight). The next instances of a supported RRULE
    (DAILY, WEEKLY with BYDAY, MONTHLY, YEARLY; INTERVAL, COUNT, UNTIL, EXDATE), skipping ahead arithmetically so a
    series from long ago is cheap; any other rule gives the first instance only. At most MAX_RRULE_STEPS steps."""
    start, end = ev["_start"], ev["_end"]
    length = end - start
    tz = window_start.tzinfo

    def aware(v):
        if isinstance(v, dt.datetime):
            return v
        return dt.datetime.combine(v, dt.time(0, 0), tzinfo=tz)

    def hit(s):
        return aware(s + length) > window_start and aware(s) < window_end if length else \
            aware(s) >= window_start and aware(s) < window_end

    rule = ev.get("_rrule")
    if not rule or ev.get("repeat_unsupported"):
        return [(start, end)]
    freq = rule.get("FREQ")
    try:
        interval = max(1, min(1000, int(rule.get("INTERVAL") or 1)))
        count = max(1, min(10_000_000, int(rule["COUNT"]))) if rule.get("COUNT") else None
    except ValueError:
        return [(start, end)]
    until = None
    if rule.get("UNTIL"):
        try:
            until = _when(rule["UNTIL"], {}, {}, tz)[0]
        except _Bad:
            until = None
    ex = ev.get("_exdates") or set()
    # wall-clock arithmetic: keep the local time across DST changes (America/Los_Angeles 9:00 stays 9:00)
    zone = start.tzinfo if isinstance(start, dt.datetime) else None
    base = start.replace(tzinfo=None) if zone else start
    target = aware(window_start).astimezone(zone).replace(tzinfo=None) if zone else \
        aware(window_start).date()

    def mk(naive):
        return naive.replace(tzinfo=zone) if zone else naive

    out, steps = [], 0
    if freq in ("DAILY", "WEEKLY"):
        weekly = freq == "WEEKLY"
        period = interval * (7 if weekly else 1)
        byday = sorted({_WEEKDAYS[x] for x in (rule.get("BYDAY") or "").split(",") if x in _WEEKDAYS}) \
            if weekly else [0]
        if weekly and not byday:
            byday = [base.weekday()]
        anchor0 = base - dt.timedelta(days=base.weekday()) if weekly else base
        # skip whole periods before the window; `seen` counts the instances skipped (for COUNT)
        k = max(0, (target - anchor0).days // period - 1)
        seen = (k * len(byday) - sum(1 for d in byday if d < base.weekday())) if weekly and k else k
        if count is not None and seen >= count:
            return []
        p = k
        while steps < MAX_RRULE_STEPS and len(out) < limit:
            anchor = anchor0 + dt.timedelta(days=p * period)
            p += 1
            stop = False
            for d in byday:
                steps += 1
                c = anchor + dt.timedelta(days=d) if weekly else anchor
                if c < base:
                    continue
                if count is not None and seen >= count:
                    stop = True
                    break
                s = mk(c)
                if until is not None and aware(s) > aware(until):
                    stop = True
                    break
                seen += 1
                if aware(s) >= window_end:
                    stop = True
                    break
                if _iso(s) not in ex and hit(s):
                    out.append((s, s + length))
                    if len(out) >= limit:
                        break
            if stop:
                break
        return out
    if freq in ("MONTHLY", "YEARLY"):
        months = interval * (12 if freq == "YEARLY" else 1)
        gap = (target.year - base.year) * 12 + (target.month - base.month)
        k = max(0, gap // months - 1)
        if count is not None:
            k = min(k, max(0, count - 1))
        seen, p = k, k
        while steps < MAX_RRULE_STEPS and len(out) < limit:
            steps += 1
            try:
                c = _add_months(base, p * months)
            except _Bad:
                break
            p += 1
            if c is None:
                continue
            if count is not None and seen >= count:
                break
            s = mk(c)
            if until is not None and aware(s) > aware(until):
                break
            seen += 1
            if aware(s) >= window_end:
                break
            if _iso(s) not in ex and hit(s):
                out.append((s, s + length))
        return out
    return [(start, end)]


# --- the parser ---------------------------------------------------------------------------------------------------
def parse(raw, owner=(), default_tz=dt.timezone.utc, now=None, horizon_days=60):
    """Reads one text/calendar part or .ics file. owner: the owner's verified addresses (for ATTENDEE PARTSTAT and
    the "uses your address" mark). Returns {"method", "events": [...], "skipped": n, "error": None | "too_big" |
    "too_deep" | "not_ics"}. Each event (JSON-ready): uid, seq, method, status ("confirmed" | "cancelled" |
    "tentative"), recurrence_id, title, title_marked, start/end (ISO), all_day, floating, tz_guess, repeats,
    repeat_unsupported, instances ([[start, end] ISO] in the window from now when now is given), organizer {name,
    addr, marked, owner_addr}, attendees (count), partstat (the owner's, or None), location, url (defanged text),
    flags (["end_before_start", ...]). Private keys starting with "_" are dropped before returning."""
    out = {"method": "", "events": [], "skipped": 0, "error": None}
    if raw is None:
        out["error"] = "not_ics"
        return out
    if len(raw) > MAX_BYTES:
        out["error"] = "too_big"
        return out
    text = _decode(raw)
    if "BEGIN:VCALENDAR" not in text.upper():
        out["error"] = "not_ics"
        return out
    text = re.sub(r"\r\n[ \t]|\n[ \t]|\r[ \t]", "", text)
    lines = re.split(r"\r\n|\n|\r", text)
    if len(lines) > MAX_LINES:
        out["error"] = "too_big"
        return out
    owner = {a.lower() for a in owner if a}
    stack, n_comp, events, vtz = [], 0, [], {}
    cur_tz, cur_ev = None, None
    for line in lines:
        if not line:
            continue
        parsed = _content_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            comp = value.strip().upper()[:40]
            n_comp += 1
            if len(stack) >= MAX_DEPTH:
                out["error"] = "too_deep"
                out["events"] = []
                return out
            if n_comp > MAX_COMPONENTS:
                break
            stack.append(comp)
            if comp == "VEVENT" and len(stack) == 2 and stack[0] == "VCALENDAR":
                if len(events) >= MAX_EVENTS:
                    out["skipped"] += 1
                    cur_ev = None
                else:
                    cur_ev = {"props": {}, "attendees": 0, "partstat": None, "exdates": [], "owner_org": False}
                    events.append(cur_ev)
            elif comp == "VTIMEZONE" and len(stack) == 2:
                cur_tz = {"id": None, "off": None}
            continue
        if name == "END":
            comp = value.strip().upper()[:40]
            if stack and stack[-1] == comp:
                stack.pop()
                if comp == "VEVENT" and len(stack) == 1:
                    cur_ev = None
                elif comp == "VTIMEZONE" and len(stack) == 1 and cur_tz:
                    if cur_tz["id"] and cur_tz["off"] is not None and len(vtz) < 50:
                        vtz[cur_tz["id"]] = cur_tz["off"]
                    cur_tz = None
            continue
        if not stack:
            continue
        if len(stack) == 1 and stack[0] == "VCALENDAR" and name == "METHOD" and not out["method"]:
            out["method"] = value.strip().upper()[:20]
            continue
        if cur_tz is not None and len(stack) >= 2 and stack[1] == "VTIMEZONE":
            if len(stack) == 2 and name == "TZID":
                cur_tz["id"] = value.strip()[:100]
            elif len(stack) == 3 and stack[2] == "STANDARD" and name == "TZOFFSETTO" and cur_tz["off"] is None:
                try:
                    cur_tz["off"] = _offset(value)
                except _Bad:
                    pass
            continue
        if cur_ev is None or len(stack) != 2 or stack[1] != "VEVENT":
            continue  # VALARM and anything nested: ignored
        if name == "ATTENDEE":
            if cur_ev["attendees"] < MAX_ATTENDEES:
                cur_ev["attendees"] += 1
                addr = value.strip()
                if addr.lower().startswith("mailto:"):
                    addr = addr[7:]
                if addr.strip().lower() in owner and cur_ev["partstat"] is None:
                    cur_ev["partstat"] = (params.get("PARTSTAT") or "NEEDS-ACTION").upper()[:20]
            continue
        if name == "EXDATE":
            if len(cur_ev["exdates"]) < MAX_EXDATES:
                cur_ev["exdates"].append((value, params))
            continue
        if name not in cur_ev["props"]:
            cur_ev["props"][name] = (value, params)
    out["events"] = [e for e in (_event(ev, out["method"], vtz, default_tz, owner, now, horizon_days)
                                 for ev in events) if e]
    out["skipped"] += len(events) - len(out["events"])
    return out


def _event(ev, method, vtz, default_tz, owner, now, horizon_days):
    p = ev["props"]
    if "DTSTART" not in p:
        return None
    try:
        start, all_day, floating, guess = _when(p["DTSTART"][0], p["DTSTART"][1], vtz, default_tz)
        flags = []
        end = None
        if "DTEND" in p:
            end, e_all, _, g2 = _when(p["DTEND"][0], p["DTEND"][1], vtz, default_tz)
            guess = guess or g2
            if e_all != all_day:
                end = None
                flags.append("mixed_end")
        elif "DURATION" in p:
            end = start + _duration(p["DURATION"][0])
        if end is None:
            end = start + dt.timedelta(days=1) if all_day else start
        if end < start:
            end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(0))
            flags.append("end_before_start")
        if (end - start) > dt.timedelta(days=366):
            raise _Bad("too long")
    except _Bad:
        return None
    uid = clean(p.get("UID", ("", {}))[0], 300)[0]
    if not uid:
        return None
    try:
        seq = max(0, min(1_000_000, int((p.get("SEQUENCE") or ("0", {}))[0].strip() or 0)))
    except ValueError:
        seq = 0
    rid = None
    if "RECURRENCE-ID" in p:
        try:
            rid = _iso(_when(p["RECURRENCE-ID"][0], p["RECURRENCE-ID"][1], vtz, default_tz)[0])
        except _Bad:
            return None
    title, t_marked = clean(_unescape(p.get("SUMMARY", ("", {}))[0]), MAX_TITLE)
    org_name, org_addr, org_marked = "", "", False
    if "ORGANIZER" in p:
        v, params = p["ORGANIZER"]
        addr = v.strip()
        if addr.lower().startswith("mailto:"):
            addr = addr[7:]
        addr_clean, a_marked = clean(addr, 254)
        org_addr = addr_clean.lower() if _EMAIL.fullmatch(addr_clean) else ""
        org_marked = a_marked or (bool(addr_clean) and not org_addr)
        org_name, n_marked = clean(_unescape(params.get("CN", "")), MAX_NAME)
        org_marked = org_marked or n_marked
        if not org_addr and addr_clean and not org_name:
            org_name = clean(addr_clean, MAX_NAME)[0]
    status = (p.get("STATUS", ("", {}))[0].strip().upper())
    rule = _rrule(p["RRULE"][0]) if "RRULE" in p else None
    unsupported = False
    if rule:
        freq = rule.get("FREQ")
        if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or \
                any(k in rule for k in ("BYSETPOS", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYHOUR", "BYMINUTE",
                                        "BYSECOND", "BYMONTH")) or \
                (freq in ("MONTHLY", "YEARLY") and "BYDAY" in rule) or \
                (freq == "DAILY" and "BYDAY" in rule):
            unsupported = True
    exdates = set()
    for value, params in ev["exdates"]:
        for v in value.split(",")[:MAX_EXDATES]:
            try:
                exdates.add(_iso(_when(v, params, vtz, default_tz)[0]))
            except _Bad:
                pass
    m = (method or "").upper()
    cancelled = m in CANCEL_METHODS or status == "CANCELLED"
    out = {"uid": uid, "seq": seq, "method": m, "recurrence_id": rid,
           "status": "cancelled" if cancelled else "tentative" if status == "TENTATIVE" else "confirmed",
           "title": title or "(No title)", "title_marked": t_marked,
           "start": _iso(start), "end": _iso(end), "all_day": all_day, "floating": floating, "tz_guess": guess,
           "repeats": bool(rule), "repeat_unsupported": unsupported,
           "organizer": {"name": org_name, "addr": org_addr, "marked": org_marked,
                         "owner_addr": bool(org_addr and org_addr in owner)},
           "attendees": ev["attendees"], "partstat": ev["partstat"],
           "location": defang(clean(_unescape(p.get("LOCATION", ("", {}))[0]), 160)[0]),
           "url": defang(clean(p.get("URL", ("", {}))[0], 200)[0]),
           "flags": flags,
           "_start": start, "_end": end, "_rrule": rule, "_exdates": exdates}
    if now is not None:
        ws = now - dt.timedelta(days=1)
        we = now + dt.timedelta(days=horizon_days)
        out["instances"] = [[_iso(s), _iso(e)] for s, e in instances(out, ws, we)]
    else:
        out["instances"] = [[out["start"], out["end"]]]
    for k in [k for k in out if k.startswith("_")]:
        del out[k]
    return out
