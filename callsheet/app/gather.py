"""0.8.0 "See everything": rules-only gathering for the brief. No AI, and nothing new reaches the writer.

- Invites in mail: text/calendar parts and .ics/.vcs attachments (read with the attachment GET when small enough),
  parsed by ics.py, merged per UID by SEQUENCE and METHOD, and dropped when the calendar already has the event (the
  Calendar API's version always wins). They are "Unverified invite · from email": never in the schedule, never in a
  clash, never "booked"; "Sender not verified" (and no answer link) when Gmail's check failed.
- Invites to answer: calendar events in the 60-day read where the owner's status is needsAction.
- The "calendar isn't read" warning for an account whose mail is read but whose calendar isn't.
- Security heads-ups and Sorted for you (Promos, Updates, No reply needed): counted and listed, never dropped.
- What changed: calendar events new, moved, renamed or cancelled since the last brief of an earlier day.

Pure functions; the sync (sync.py) does the reading. Everything shown is text (the page uses textContent).
"""
import base64
import binascii
import codecs
import datetime as dt
import email.utils
import hashlib
import html
import quopri
import re
import urllib.parse

import ics
import rules

LOOKAHEAD_DAYS = 60            # the calendars are read this far ahead (was dates.LOOKAHEAD_DAYS = 14)
MAX_ATTACHMENT = ics.MAX_BYTES  # a calendar attachment larger than this is not fetched
MAX_PARTS = 60
MAX_PART_DEPTH = 12
MAX_INVITE_MAILS = 25           # invite mails looked at per account per sync (new ones; seen ones are cached)
MAX_UID_LOOKUPS = 10            # "is it on your calendar?" GETs per sync
MAX_INVITES = 40
INVITE_QUERY = ('newer_than:60d -in:sent -in:drafts -in:chats (filename:ics OR filename:vcs OR subject:invitation '
                'OR subject:invite OR subject:"updated invitation" OR subject:canceled OR subject:cancelled)')
PROMO_QUERY = "in:inbox category:promotions newer_than:1d"
MAX_PROMO_READS = 25            # promo threads read (headers only) per account per sync; the rest are counted
SORTED_WINDOW_S = 86400         # Sorted for you: the last 24 hours
SECURITY_WINDOW_S = 3 * 86400   # security heads-ups: the last 3 days
MAX_SECURITY = 10
MAX_SORTED = 200                # per bucket (the count is exact; the list is capped with "and N more")
MAX_CHANGED = 12
CAL_MIMES = ("text/calendar", "application/ics", "text/x-vcalendar", "application/x-ics", "text/x-calendar")
CAL_EXT = (".ics", ".vcs", ".ical", ".icalendar")
LINK_HOSTS = ("www.google.com", "calendar.google.com")

MARKS = {
    "unverified": "Unverified invite · from email",
    "event_file": "Unverified event · from email",
    "not_answered": "Invite · not answered",
    "other_calendar": "On a calendar Callsheet doesn't read",
    "sender_fail": "Sender not verified",
    "sender_none": "Sender not checked by Gmail",
    "cancelled": "Cancelled",
    "updated": "Updated",
    "uses_your_address": "Uses your address; not from your accounts",
    "look_alike": "Look-alike letters",
    "disagree": "Another email disagrees",
    "tz_guess": "Time zone guessed",
    "floating": "No time zone; shown in yours",
    "repeats": "Repeats",
    "repeat_unknown": "Repeats (see the email for the dates)",
}


# --- mail parts -----------------------------------------------------------------------------------------------------
def _hdrs(part):
    return {h.get("name", "").lower(): h.get("value", "") for h in part.get("headers") or [] if isinstance(h, dict)}


def _ct_param(ct, name):
    m = re.search(r";\s*" + name + r"\s*=\s*\"?([^\";]+)\"?", ct or "", re.I)
    return m.group(1).strip() if m else ""


def calendar_parts(payload):
    """The calendar parts of a Gmail message payload (format=full), in order: [{"mime", "filename", "data"
    (base64url text) or "attachment_id", "size", "charset", "encoding", "method"}]. At most MAX_PARTS parts are
    looked at, MAX_PART_DEPTH deep."""
    out, seen = [], [0]

    def walk(p, depth):
        if not isinstance(p, dict) or depth > MAX_PART_DEPTH or seen[0] >= MAX_PARTS:
            return
        seen[0] += 1
        mime = str(p.get("mimeType") or "").lower()
        fname = str(p.get("filename") or "")
        hd = _hdrs(p)
        ct = hd.get("content-type", "")
        is_cal = mime in CAL_MIMES or (fname.lower().endswith(CAL_EXT) and not mime.startswith("multipart/"))
        if is_cal:
            body = p.get("body") or {}
            try:
                size = int(body.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append({"mime": mime, "filename": fname[:120], "data": body.get("data"),
                        "attachment_id": body.get("attachmentId"), "size": size,
                        "charset": _ct_param(ct, "charset"), "method": _ct_param(ct, "method").upper()[:20],
                        "encoding": hd.get("content-transfer-encoding", "").strip().lower()})
        for c in p.get("parts") or []:
            walk(c, depth + 1)

    walk(payload or {}, 0)
    return out


def part_text(data_b64, charset="", encoding=""):
    """The text of a calendar part from Gmail's base64url body data (Gmail already undid the transfer encoding; a
    part still in base64 or quoted-printable is decoded once more). None when it can't be read or is too big."""
    if not data_b64 or len(data_b64) > MAX_ATTACHMENT * 4 // 3 + 8:
        return None
    try:
        raw = base64.urlsafe_b64decode(str(data_b64) + "=" * (-len(str(data_b64)) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) > MAX_ATTACHMENT:
        return None
    if b"BEGIN:VCALENDAR" not in raw.upper() and encoding in ("base64", "quoted-printable"):
        try:
            raw = base64.b64decode(raw, validate=False) if encoding == "base64" else quopri.decodestring(raw)
        except (binascii.Error, ValueError):
            return None
    cs = (charset or "utf-8").strip().lower()
    try:
        codecs.lookup(cs)
    except LookupError:
        cs = "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        cs = "utf-16"
    try:
        return raw.decode(cs, errors="replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", errors="replace")


# --- invites from mail ------------------------------------------------------------------------------------------
def message_invites(msg, account_email, owner, tz, now, fetch=None):
    """The invites in one Gmail message (format=full): [{"ev": ics event, "part": n, "inline": bool, ...mail}] where
    mail = {"acc_email", "msg_id", "thread_id", "ts", "auth", "from_addr", "sent"}. fetch(attachment_id) -> the
    attachment's base64url data, for a part without inline data whose size is within MAX_ATTACHMENT (None: not read).
    owner: the owner's verified addresses (the connected accounts)."""
    hd = rules.last_headers(msg)
    auth = rules.auth_level(hd)
    frm = email.utils.parseaddr(next((v for k, v in hd if k.lower() == "from"), ""))[1].lower()
    meta = {"acc_email": account_email, "msg_id": str(msg.get("id") or ""), "thread_id": str(msg.get("threadId") or ""),
            "ts": int(msg.get("internalDate") or 0) / 1000, "auth": auth, "from_addr": frm,
            "sent": "SENT" in (msg.get("labelIds") or [])}
    out = []
    for i, p in enumerate(calendar_parts(msg.get("payload"))):
        data = p["data"]
        if not data and p["attachment_id"] and fetch is not None and 0 < p["size"] <= MAX_ATTACHMENT:
            data = fetch(p["attachment_id"])
        text = part_text(data, p["charset"], p["encoding"]) if data else None
        if text is None:
            continue
        res = ics.parse(text, owner=owner, default_tz=tz, now=now, horizon_days=LOOKAHEAD_DAYS)
        method = res["method"] or p["method"]
        for ev in res["events"]:
            if not ev["method"] and method:
                ev = dict(ev, method=method, status="cancelled" if method == "CANCEL" else ev["status"])
            out.append(dict(meta, ev=ev, part=i, inline=bool(p["data"]) and p["mime"] == "text/calendar"))
    return out


def _same(a, b):
    return all(a[k] == b[k] for k in ("start", "end", "title", "status", "all_day"))


def merge_invites(found, calendar_uids=(), now=None):
    """found: message_invites() results from every account. One invite per (UID, RECURRENCE-ID):
    - answers about invites (REPLY, COUNTER, REFRESH, DECLINECOUNTER) are never invites;
    - the Calendar API's version always wins: a UID in calendar_uids is left out (an email can't move or cancel
      what the calendar shows);
    - within one mail, parts with the same UID that disagree keep the inline text/calendar part, marked "disagree";
    - across mails the highest SEQUENCE wins, then the newest mail; a CANCEL or update from another organiser than
      the invite's own doesn't count (marked "disagree");
    - a series shows its next instance; events that ended before now are left out.
    Returns [{"ev", mail fields, "marks": [codes]}] sorted by start."""
    cal = {u for u in calendar_uids if u}
    groups = {}
    for f in found:
        ev = f["ev"]
        if ev["method"] in ics.REPLY_METHODS or ev["uid"] in cal:
            continue
        groups.setdefault((ev["uid"], ev["recurrence_id"]), []).append(f)
    out = []
    for _, items in groups.items():
        marks = set()
        by_msg = {}
        for f in items:
            by_msg.setdefault(f["msg_id"], []).append(f)
        cands = []
        for mid, fs in by_msg.items():
            fs = sorted(fs, key=lambda f: (not f["inline"], f["part"]))
            if any(not _same(fs[0]["ev"], f["ev"]) for f in fs[1:]):
                marks.add("disagree")
            cands.append(fs[0])
        cands.sort(key=lambda f: (f["ev"]["seq"], f["ts"]))
        first_req = next((f for f in sorted(cands, key=lambda f: f["ts"]) if f["ev"]["status"] != "cancelled"), None)
        org = first_req["ev"]["organizer"]["addr"] if first_req else ""
        win = cands[-1]
        if org and win["ev"]["organizer"]["addr"] and win["ev"]["organizer"]["addr"] != org:
            marks.add("disagree")
            same_org = [f for f in cands if f["ev"]["organizer"]["addr"] in ("", org)]
            win = same_org[-1] if same_org else win
        if len(cands) > 1 and win["ev"]["status"] != "cancelled" and win["ev"]["seq"] > cands[0]["ev"]["seq"]:
            marks.add("updated")
        ev = win["ev"]
        inst = ev.get("instances") or []
        if now is not None:
            nxt = [x for x in inst if _end_dt(x[1], now.tzinfo) > now]
            if not nxt:
                continue
            ev = dict(ev, start=nxt[0][0], end=nxt[0][1])
        out.append(dict(win, ev=ev, marks=sorted(marks)))
    out.sort(key=lambda f: f["ev"]["start"])
    return out[:MAX_INVITES]


def _end_dt(value, tz):
    v = dt.datetime.fromisoformat(value) if "T" in value else dt.datetime.combine(
        dt.date.fromisoformat(value), dt.time(0, 0), tzinfo=tz)
    return v if v.tzinfo else v.replace(tzinfo=tz)


def email_invite_rows(merged, owner, areas_of):
    """The brief's invite rows for merged email invites (JSON for the cache). areas_of: {account email: area id}."""
    rows = []
    for f in merged:
        ev = f["ev"]
        marks = ["event_file" if ev["method"] in ("PUBLISH", "ADD") or not ev["method"] else "unverified"]
        marks.append({"pass": None, "fail": "sender_fail", "none": "sender_none"}[f["auth"]] or "")
        if ev["status"] == "cancelled":
            marks.append("cancelled")
        org = ev["organizer"]
        if org["owner_addr"] and not (f["sent"] or (f["auth"] == "pass" and f["from_addr"] in owner)):
            marks.append("uses_your_address")
        if org["marked"] or ev["title_marked"]:
            marks.append("look_alike")
        if ev["tz_guess"]:
            marks.append("tz_guess")
        if ev["floating"]:
            marks.append("floating")
        if ev["repeats"]:
            marks.append("repeat_unknown" if ev["repeat_unsupported"] else "repeats")
        marks += f.get("marks") or []
        link = rules.gmail_link(f["acc_email"], f["thread_id"], f["msg_id"]) if f["thread_id"] else None
        rows.append({
            "key": "inv:" + hashlib.sha256(f"{ev['uid']}|{ev['recurrence_id']}".encode()).hexdigest()[:16],
            "kind": "email", "title": ev["title"], "start": ev["start"], "end": ev["end"], "all_day": ev["all_day"],
            "organizer": org["name"], "org_addr": org["addr"], "account": f["acc_email"],
            "area": areas_of.get(f["acc_email"]), "sender": f["auth"], "status": ev["status"],
            "location": ev["location"], "marks": [m for m in dict.fromkeys(marks) if m],
            # "Answer in Gmail" only when Gmail didn't reject the sender; "Open the email" stays (Gmail's own view)
            "link": link if f["auth"] != "fail" and ev["status"] != "cancelled" else None,
            "mail_link": link, "found_at": f["ts"]})
    return rows


def safe_cal_link(url):
    """A Calendar event's htmlLink from the API, kept only when it is https on Google's own calendar hosts."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return None
    return url if p.scheme == "https" and p.hostname in LINK_HOSTS else None


def calendar_invite_rows(events, tz, now, other=False):
    """Invites to answer: events [(event, area id)] (the 60-day read) where the owner's status is needsAction and
    the event isn't cancelled or over. A series (one iCalUID) shows its next instance. other: the events came from
    the iCalUID lookup of a calendar Callsheet doesn't read (then answered ones are listed too)."""
    seen, rows = {}, []
    for ev, area in events:
        status = rules._self_status(ev)
        if ev.get("status") == "cancelled" or (status != "needsAction" and not other) or status == "declined":
            continue
        start, all_day = rules._when(ev.get("start"), tz)
        end, _ = rules._when(ev.get("end"), tz)
        if start is None:
            continue
        end = end if end is not None and end >= start else start
        end_dt = end if isinstance(end, dt.datetime) else dt.datetime.combine(end, dt.time(0, 0), tzinfo=tz)
        if end_dt <= now:
            continue
        uid = ev.get("iCalUID") or ev.get("id") or ""
        if uid in seen:
            seen[uid]["n"] += 1
            continue
        org = ev.get("organizer") or {}
        marks = ["not_answered"] if status == "needsAction" else []
        if other:
            marks.append("other_calendar")
        row = {"key": "cal:" + hashlib.sha256(uid.encode()).hexdigest()[:16], "kind": "calendar",
               "title": rules._clip((ev.get("summary") or "").strip() or "(No title)", 200),
               "start": start.isoformat(), "end": end.isoformat(), "all_day": bool(all_day),
               "organizer": rules._clip(org.get("displayName") or "", 80), "org_addr": (org.get("email") or "").lower(),
               "account": None, "area": area, "sender": "pass", "status": "needs_action" if status == "needsAction"
               else "answered", "location": ics.defang(rules._clip(ev.get("location") or "", 160)),
               "marks": marks, "link": safe_cal_link(ev.get("htmlLink")), "mail_link": None, "n": 1}
        seen[uid] = row
        rows.append(row)
    for r in rows:
        if r.pop("n") > 1:
            r["marks"].append("repeats")
    rows.sort(key=lambda r: r["start"])
    return rows


def compact_event(ev, cal, account_email, tz):
    """A calendar event in the compact form Coming up keeps (cached with the brief; no description, attendee names
    or location): {key, title, start, end, all_day, days, birthday, holiday, attendees, external}."""
    if ev.get("status") == "cancelled" or rules._self_status(ev) == "declined":
        return None
    start, all_day = rules._when(ev.get("start"), tz)
    end, _ = rules._when(ev.get("end"), tz)
    if start is None:
        return None
    end = end if end is not None and end >= start else start
    cid = str(cal.get("id") or "")
    csum = str(cal.get("summary") or "")
    birthday = ev.get("eventType") == "birthday" or "#contacts@" in cid or csum.strip().lower() == "birthdays"
    holiday = "#holiday@" in cid
    sd = start if all_day else start.date()
    ed = end if all_day else end.date()
    own = account_email.split("@")[-1].lower()
    atts = [a for a in (ev.get("attendees") or []) if isinstance(a, dict) and not a.get("resource")]
    external = any((a.get("email") or "").lower().rpartition("@")[2] not in ("", own) for a in atts
                   if not a.get("self"))
    uid = str(ev.get("iCalUID") or ev.get("id") or "")
    return {"key": "ev:" + hashlib.sha256((uid + "|" + start.isoformat()).encode()).hexdigest()[:16],
            "title": rules._clip((ev.get("summary") or "").strip() or "(No title)", 120),
            "start": start.isoformat(), "end": end.isoformat(), "all_day": bool(all_day),
            "days": max(1, (ed - sd).days), "birthday": bool(birthday), "holiday": bool(holiday),
            "attendees": len(atts), "external": bool(external)}


# --- accounts -------------------------------------------------------------------------------------------------------
def calendar_gaps(accounts, areas):
    """Accounts whose mail is read but whose calendar isn't: [{"email", "why"}]. why: "off" (the account's
    Calendar switch or the area's calendar reading is off) or "none_ticked" (no calendar ticked)."""
    out = []
    for a in accounts:
        area = areas.get(a.get("area_id")) or {}
        reads = area.get("reads") or {}
        svc = a.get("services") or {}
        if not (reads.get("mail") and svc.get("mail")):
            continue
        if not (reads.get("calendar") and svc.get("calendar")):
            out.append({"email": a.get("email"), "why": "off"})
        elif not any(c.get("selected") for c in a.get("calendars") or []):
            out.append({"email": a.get("email"), "why": "none_ticked"})
    return out


def gap_text(gap):
    return f"{gap['email']}: its calendar isn't read, so invites there show only from email."


# --- Sorted for you and security heads-ups ---------------------------------------------------------------------------
def mail_item(thread, account_email, area):
    """(header dict, compact item) for a thread's newest message (format=metadata), or (None, None)."""
    msgs = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
    if not msgs:
        return None, None
    last = max(msgs, key=lambda m: int(m.get("internalDate") or 0))
    hdr = rules._headers(last)
    name, addr = email.utils.parseaddr(hdr.get("from", ""))
    subject = " ".join(hdr.get("subject", "").split())
    who, _ = ics.clean(name.strip() or addr or "Unknown sender", 60)
    subj, _ = ics.clean(subject or html.unescape(last.get("snippet") or ""), 120)
    item = {"who": who, "subject": subj, "at": int(last.get("internalDate") or 0) / 1000, "area": area,
            "link": rules.gmail_link(account_email, thread.get("id", ""), last.get("id", "")),
            "_addr": addr.lower(), "_labels": list(last.get("labelIds") or []),
            "_snippet": " ".join(html.unescape(last.get("snippet") or "").split()),
            "_auth": rules.auth_level(rules.last_headers(last)), "_hdr": hdr,
            "_own": "SENT" in (last.get("labelIds") or []) or addr.lower() == account_email.lower()}
    return hdr, item


def sort_mail(items, now_ts, promo_counts=None):
    """items: mail_item() results not shown as reply rows (promotions included). Returns {"security": [...],
    "sorted": {"promos": {"n", "items"}, "updates": {...}, "other": {...}}}. Security alerts (rules.security_alert)
    always surface and are never counted in a bucket; every other item of the window lands in exactly one bucket.
    promo_counts: promotions counted but not read this time (their items say so), so the count stays exact."""
    sec, buckets = [], {"promos": [], "updates": [], "other": []}
    for it in items:
        if it is None or it["_own"]:
            continue
        alert = rules.security_alert(it["_hdr"], it["_addr"], it["subject"], it["_snippet"], it["_auth"])
        age = now_ts - it["at"]
        if alert is not None:
            if age <= SECURITY_WINDOW_S:
                marks = ["security"] + alert["marks"]
                sec.append(dict(_pub(it), marks=marks, brand=alert["brand"], lure=alert["lure"]))
            continue
        if age > SORTED_WINDOW_S:
            continue
        lb = it["_labels"]
        if "CATEGORY_PROMOTIONS" in lb:
            buckets["promos"].append(_pub(it))
        elif any(x in lb for x in ("CATEGORY_UPDATES", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")) or \
                rules.is_automated(it["_hdr"], it["_addr"]):
            buckets["updates"].append(_pub(it))
        else:
            buckets["other"].append(_pub(it))
    for extra in promo_counts or []:
        buckets["promos"].append(extra)
    sec.sort(key=lambda x: -x["at"])
    out = {}
    for k, v in buckets.items():
        v.sort(key=lambda x: -(x.get("at") or 0))
        out[k] = {"n": len(v), "items": v[:MAX_SORTED]}
    return {"security": sec[:MAX_SECURITY], "security_n": len(sec), "sorted": out}


def _pub(it):
    return {k: v for k, v in it.items() if not k.startswith("_")}


# --- What changed ---------------------------------------------------------------------------------------------------
def snapshot(cal_events, tz, now):
    """{"at": ISO, "end": ISO, "cals": {calendar key: {event key: [title, start, end]}}} of the events read
    (cal_events: {calendar key: [event]}), for What changed."""
    cals = {}
    for ck, evs in cal_events.items():
        d = {}
        for ev in evs:
            if ev.get("status") == "cancelled":
                continue
            s, _ = rules._when(ev.get("start"), tz)
            e, _ = rules._when(ev.get("end"), tz)
            if s is None:
                continue
            key = str(ev.get("id") or ev.get("iCalUID") or "")[:200]
            if key:
                d[key] = [rules._clip((ev.get("summary") or "").strip() or "(No title)", 120), s.isoformat(),
                          (e or s).isoformat()]
        cals[ck] = d
    return {"at": now.isoformat(timespec="seconds"),
            "end": (now + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat(timespec="seconds"), "cals": cals}


def _start(v, tz):
    return _end_dt(v, tz)


def changes(base, cur, tz, now):
    """What changed between two snapshots: [{"kind": new|moved|renamed|cancelled, "title", "old_title", "start",
    "end", "old_start", "old_end"}], upcoming events only, on calendars read both times; an event is "new" only when
    it starts inside the earlier window (not merely because the window moved on)."""
    if not base or not cur:
        return []
    out = []
    try:
        base_end = dt.datetime.fromisoformat(base["end"])
        cur_end = dt.datetime.fromisoformat(cur["end"])
    except (KeyError, TypeError, ValueError):
        return []
    for ck, now_evs in (cur.get("cals") or {}).items():
        before = (base.get("cals") or {}).get(ck)
        if before is None:
            continue
        for k, (title, s, e) in now_evs.items():
            sd = _start(s, tz)
            if sd < now:
                continue
            old = before.get(k)
            if old is None:
                if sd < base_end:
                    out.append({"kind": "new", "title": title, "start": s, "end": e})
                continue
            if old[1] != s or old[2] != e:
                out.append({"kind": "moved", "title": title, "start": s, "end": e, "old_start": old[1],
                            "old_end": old[2]})
            elif old[0] != title:
                out.append({"kind": "renamed", "title": title, "old_title": old[0], "start": s, "end": e})
        for k, (title, s, e) in before.items():
            if k in now_evs:
                continue
            sd = _start(s, tz)
            if now <= sd < cur_end:
                out.append({"kind": "cancelled", "title": title, "start": s, "end": e})
    out.sort(key=lambda c: c["start"])
    return out[:MAX_CHANGED]


# --- the page -----------------------------------------------------------------------------------------------------
def when_text(start, end, all_day, tz, fmt, today):
    """"Today · 11:30 am to 12:15 pm", "Tue 10 Nov · 9:00 to 10:00", "Fri 14 Nov · all day"."""
    try:
        s = _end_dt(start, tz).astimezone(tz) if not all_day else dt.date.fromisoformat(start[:10])
        e = _end_dt(end, tz).astimezone(tz) if not all_day else dt.date.fromisoformat(end[:10])
    except (TypeError, ValueError):
        return ""
    day = s.date() if isinstance(s, dt.datetime) else s
    if day == today:
        label = "Today"
    elif day == today + dt.timedelta(days=1):
        label = "Tomorrow"
    else:
        label = f"{day:%a} {day.day} {day:%b}"
    if all_day:
        n = (e - s).days
        return f"{label} · all day" + (f", {n} days" if n > 1 else "")
    t = rules.clock(s, fmt)
    if e > s:
        same = e.date() == s.date()
        t += " to " + (rules.clock(e, fmt) if same else f"{e:%a} {e.day} {e:%b} " + rules.clock(e, fmt))
    return f"{label} · {t}"


def invites_for_page(rows, tz, fmt, today):
    """Display fields for cached invite rows: when, day (ISO date), marks as [code, text]."""
    out = []
    for i, r in enumerate(rows or [], 1):
        if not isinstance(r, dict) or not r.get("start"):
            continue
        r = dict(r)
        r["id"] = f"i{i}"
        r["when"] = when_text(r["start"], r.get("end") or r["start"], r.get("all_day"), tz, fmt, today)
        try:
            r["day"] = (_end_dt(r["start"], tz).astimezone(tz).date() if not r.get("all_day")
                        else dt.date.fromisoformat(r["start"][:10])).isoformat()
        except ValueError:
            r["day"] = ""
        r["marks"] = [[m, MARKS[m]] for m in r.get("marks") or [] if m in MARKS]
        out.append(r)
    return out


def _at_text(ts, tz, fmt, today):
    try:
        d = dt.datetime.fromtimestamp(float(ts), tz)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    t = rules.clock(d, fmt)
    if d.date() == today:
        return t
    if d.date() == today - dt.timedelta(days=1):
        return "Yesterday " + t
    return f"{d:%a} {d.day} {d:%b}"


SECURITY_MARKS = {"security": "Security · always shown", "sender_fail": "Sender not verified",
                  "checked": "Sender checked by Gmail", "lure": "Not from {brand}: the address only looks like it"}


def security_for_page(items, tz, fmt, today):
    """Security heads-ups for the page: marks as [code, text]; a look-alike never names the brand as its sender."""
    out = []
    for i, it in enumerate(items or [], 1):
        if not isinstance(it, dict):
            continue
        r = dict(it, id=f"s{i}", when=_at_text(it.get("at"), tz, fmt, today))
        brand = it.get("lure") if isinstance(it.get("lure"), str) else it.get("brand") or "the company"
        r["marks"] = [[m, SECURITY_MARKS[m].format(brand=brand)] for m in it.get("marks") or [] if m in SECURITY_MARKS]
        out.append(r)
    return out


SORTED_NAMES = {"promos": "Promos", "updates": "Updates", "other": "No reply needed"}


def sorted_for_page(sorted_, tz, fmt, today):
    """Sorted for you: [{"key", "name", "n", "items", "more"}] (the count is exact; nothing is hidden: every item
    of the window is in one list, the list shows up to MAX_SORTED and says how many more)."""
    out = []
    for k in ("promos", "updates", "other"):
        b = (sorted_ or {}).get(k) or {}
        n = int(b.get("n") or 0)
        items = [dict(it, when=_at_text(it.get("at"), tz, fmt, today)) for it in (b.get("items") or [])
                 if isinstance(it, dict)]
        out.append({"key": k, "name": SORTED_NAMES[k], "n": n, "items": items, "more": max(0, n - len(items))})
    return out


def changes_for_page(items, tz, fmt, today):
    out = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        when = when_text(c["start"], c.get("end") or c["start"], "T" not in c["start"], tz, fmt, today)
        if c.get("kind") == "moved":
            old = when_text(c["old_start"], c.get("old_end") or c["old_start"], "T" not in c["old_start"], tz, fmt,
                            today)
            text = f"{c['title']} moved from {old} to {when}"
        elif c.get("kind") == "cancelled":
            text = f"{c['title']} ({when}) is no longer on your calendar"
        elif c.get("kind") == "renamed":
            text = f"{c.get('old_title')} is now called {c['title']} ({when})"
        elif c.get("kind") == "project":
            out.append(c)
            continue
        else:
            text = f"New: {c['title']} ({when})"
        out.append({"kind": c.get("kind"), "src": "cal", "text": text, "by": "Calendar"})
    return out
