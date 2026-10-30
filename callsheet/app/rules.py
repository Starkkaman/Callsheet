"""Plain rules that turn calendar events and mail threads into the Daily Brief. No network, no AI.

Everything here is a pure function of its inputs so it can be tested directly.
"""
import datetime as dt
import email.utils
import hashlib
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
# Gmail's category tabs: never "needs a reply" (even when the thread is in the inbox), unless from a VIP
NO_REPLY_CATEGORIES = ("CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CATEGORY_SOCIAL")
SLACK_WAIT_H = 3          # "Waiting Nh" from 3 hours on
SLACK_DM_MAX_S = 14 * 86400
MAX_TOP = 5
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


def clock(value, fmt="24h"):
    """A display time from a datetime/time or an "HH:MM" string (also "24:00", the end of the day).
    fmt "24h": "14:30". fmt "12h": "9:30 am", "2:30 pm", whole hours as "11 am"; noon "12 pm", midnight
    "12 am"."""
    if isinstance(value, str):
        try:
            h, m = (int(x) for x in value.split(":"))
        except ValueError:
            return value
    else:
        h, m = value.hour, value.minute
    if fmt != "12h":
        return f"{h:02d}:{m:02d}"
    h %= 24
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def time_format(settings):
    return "24h" if (settings or {}).get("time_format") == "24h" else "12h"


def flag_text(flags, fmt):
    """The schedule flag line from structured flags (cached as machine values, worded per time format)."""
    out = []
    for f in flags:
        if f.get("kind") == "not_accepted":
            out.append("Not accepted yet")
        elif f.get("kind") == "overlap":
            out.append(f"Overlaps {clock(f['at'], fmt)} {f['title']}")
    return " · ".join(out)


def render_schedule(rows, fmt):
    """Display fields of cached schedule rows in the owner's time format: time, end and the flag text come
    from the machine fields start_hhmm, end_hhmm and flags. Rows from older caches are left as they are."""
    out = []
    for r in rows:
        r = dict(r)
        if r.get("start_hhmm"):
            r["time"] = clock(r["start_hhmm"], fmt)
            r["end"] = clock(r["end_hhmm"], fmt)
            if r.get("flags"):
                r["flag"] = {"text": flag_text(r["flags"], fmt)}
        out.append(r)
    return out


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


def overlaps(a, b):
    """The clash rule: two timed events overlap when each starts before the other ends. a, b: {start, end}
    (datetimes, or "HH:MM" strings of one day, which compare the same way)."""
    return a["start"] < b["end"] and b["start"] < a["end"]


def unanswered(msgs):
    """msgs: [(time, from_owner)] of one thread or chat in time order. The messages by others after the owner's
    last message: {"first": time of the first of them (None when there are none), "chases": how many}."""
    last_mine = max((i for i, (_, mine) in enumerate(msgs) if mine), default=-1)
    after = [t for t, mine in msgs[last_mine + 1:] if not mine]
    return {"first": after[0] if after else None, "chases": len(after)}


def build_schedule(items, tz, day, fmt="24h"):
    """items: [(event, area_id)] from events.list for `day`. Returns (rows, all_day_titles). Rows carry the
    machine fields start_hhmm / end_hhmm ("HH:MM", end "24:00" at midnight) and flags ([{kind: not_accepted}
    | {kind: overlap, at: "HH:MM", title}]), and the display fields time, end and flag.text in fmt."""
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
        timed.append({"start": start, "end": end, "title": title, "area": area, "ev": ev, "key": key})
    timed.sort(key=lambda e: (e["start"], e["end"]))

    first = next((e for e in timed if _others(e["ev"])), timed[0] if timed else None)
    rows = []
    for e in timed:
        shown = max(e["start"], day_start)
        start_s, end_s = hhmm(shown), hhmm(e["end"]) if e["end"] < day_end else "24:00"
        row = {"time": clock(start_s, fmt), "end": clock(end_s, fmt), "start_hhmm": start_s, "end_hhmm": end_s,
               "title": e["title"], "short": _clip(e["title"], 24), "area": e["area"]}
        flags = []
        if _self_status(e["ev"]) == "needsAction":
            flags.append({"kind": "not_accepted"})
        clash = next((o for o in timed if o is not e and overlaps(e, o)), None)
        if clash is not None:
            row["clash"] = True
            flags.append({"kind": "overlap", "at": hhmm(max(clash["start"], day_start)),
                          "title": _clip(clash["title"], 40)})
        if flags:
            row["flags"] = flags
            row["flag"] = {"text": flag_text(flags, fmt)}
        note = _note(e["ev"])
        if note:
            row["note"] = note
        if e is first:
            row["first"] = True
        row["_key"] = f"cal:{e['key'][0]}:{e['key'][1]}"
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


def gmail_link(account_email, thread_id, message_id=""):
    """Direct link to a message in the right Gmail account, in the format Gmail's own view links use:
    /mail/u/?authuser=<email>#all/thread-f:<id>|msg-f:<id>, with the API's hex ids written in decimal.
    (/mail/?authuser= is ignored and /u/<email>/ shows "account temporarily unavailable" when several
    accounts are signed in; checked by the owner 2026-10-29.)"""
    base = "https://mail.google.com/mail/u/?authuser=" + urllib.parse.quote(account_email, safe="@.")
    try:
        link = base + "#all/thread-f:" + str(int(str(thread_id), 16))
    except ValueError:
        return base
    try:
        return link + "|msg-f:" + str(int(str(message_id), 16)) if message_id else link
    except ValueError:
        return link


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
    if not vip and any(lb in NO_REPLY_CATEGORIES for lb in last.get("labelIds") or []):
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
    link = gmail_link(account_email, thread.get("id", ""), last.get("id", ""))
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    return {"who": _clip(who, 60), "area": area, "source": "Mail", "via": "mail", "text": text,
            "short": _clip(text, 40), "age": f"{age_days}d" if age_days >= 1 else f"{max(1, int(age_s // 3600))}h",
            "reasons": reasons, "vip": vip, "action": "Open", "link": link, "_age_s": age_s,
            "_last": int(last.get("internalDate") or 0) / 1000,
            "_sup": {"source": "mail", "thread": str(thread.get("id", "")).lower(), "sender": addr,
                     "domain": domain or None,
                     "labels": {"thread": "Mail · " + _clip(subject or snippet or "this thread", 60),
                                "sender": (f"{name.strip()} <{addr}>" if name.strip() else addr),
                                "domain": domain}},
            "_key": "mail:" + str(thread.get("id", "")) + ":" + str(last.get("id", "")),
            # for the writer's facts (never stored): who wrote since the owner's last message (SENT label or
            # the owner's address), and the last message's text to look for a deadline in
            "_facts": dict(unanswered([(_ms(m) / 1000, _from_owner(m, me)) for m in sorted(msgs, key=_ms)]),
                           last=_ms(last) / 1000, text=subject + "\n" + snippet)}


def _ms(msg):
    return int(msg.get("internalDate") or 0)


def _from_owner(msg, me):
    if "SENT" in (msg.get("labelIds") or []):
        return True
    return email.utils.parseaddr(_headers(msg).get("from", ""))[1].lower() == me


def sort_replies(rows):
    rows.sort(key=lambda r: (not r["vip"], -r["_age_s"]))
    for r in rows:
        r.pop("_age_s", None)
    return rows


def row_key(source):
    """A stable, opaque row key (the same thread/event and last message give the same key all day)."""
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:16]


# --- doesn't need a reply ------------------------------------------------------
def suppressed(sup, vip, rules_list):
    """True when a "Doesn't need a reply" rule hides this row. sup: the row's {source, thread, sender, domain}.
    A thread rule always applies; VIPs win over sender and domain rules."""
    for r in rules_list or []:
        if r.get("source") != sup.get("source"):
            continue
        kind, value = r.get("kind"), r.get("value")
        if kind == "thread" and value and value == sup.get("thread"):
            return True
        if vip:
            continue
        if kind == "sender" and value and value == sup.get("sender"):
            return True
        if kind == "domain" and value and sup.get("domain") and (sup["domain"] == value
                                                                 or sup["domain"].endswith("." + value)):
            return True
    return False


# --- Slack ---------------------------------------------------------------------
_SLACK_LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
_BROADCAST = re.compile(r"<!(channel|here|everyone)(?:\|[^>]*)?>")


def slack_text(text, users):
    """Slack markup as plain text: <@U1> -> @Name, <#C1|general> -> #general, <!here> -> @here, links -> their
    label (or the address), &amp; &lt; &gt; decoded."""
    def sub(m):
        target, label = m.group(1), m.group(2)
        if target.startswith("@"):
            u = users.get(target[1:]) or {}
            return "@" + (label or u.get("name") or target[1:])
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            word = target[1:].split("^")[0]
            return "@" + (label.lstrip("@") if label else word)
        return label or target
    out = _SLACK_LINK.sub(sub, text or "")
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def slack_link(domain, channel, ts, root=None):
    base = f"https://{domain or 'app'}.slack.com/archives/{channel}/p{str(ts).replace('.', '')}"
    if root and root != ts:
        base += "?" + urllib.parse.urlencode({"thread_ts": root, "cid": channel})
    return base


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _real(m):
    if not isinstance(m, dict) or m.get("bot_id") or not m.get("user") or not m.get("ts"):
        return False
    return m.get("subtype") in (None, "thread_broadcast", "file_share", "me_message")


def _waiting(age_s):
    h = int(age_s // 3600)
    if h < SLACK_WAIT_H:
        return None
    return f"Waiting {h}h" if h < 48 else f"Waiting {h // 24} days"


def slack_rows(data, ws, area, now_ts, vips, tz=None, fmt="24h"):
    """Needs-a-reply rows and Heads up lines from one workspace's read (slack.read_workspace). Pure.
    vips: {lowercase email: name}. Returns (rows, heads_up)."""
    me, users, convs = data["me"], data.get("users") or {}, data.get("convs") or {}
    domain, tz = data.get("domain") or ws.get("domain") or "", tz or dt.timezone.utc
    vip_names = {n.strip().lower(): n for n in vips.values() if n}
    cands = {}

    def who(uid):
        return (users.get(uid) or {}).get("name") or uid

    def is_vip(uid):
        u = users.get(uid) or {}
        if u.get("email"):
            return u["email"].lower() in vips
        return any(n and n.strip().lower() in vip_names for n in (u.get("name"), u.get("real_name")))

    def add(key, ch, msg, ctx, root=None, mentioned=False, in_thread=False, facts=None):
        c = cands.get(key)
        if c is None or _f(msg["ts"]) >= _f(c["msg"]["ts"]):
            cands[key] = {"ch": ch, "msg": msg, "ctx": ctx, "root": root,
                          "facts": facts or {"first": _f(msg["ts"]), "chases": 1},
                          "mentioned": mentioned or bool(c and c["mentioned"]),
                          "in_thread": in_thread or bool(c and c["in_thread"])}
        else:
            c["mentioned"] |= mentioned
            c["in_thread"] |= in_thread

    for ch, msgs in (data.get("dm") or {}).items():
        real = [m for m in msgs if _real(m) and not (m.get("thread_ts") and m.get("thread_ts") != m["ts"])]
        if real and real[-1]["user"] != me and now_ts - _f(real[-1]["ts"]) <= SLACK_DM_MAX_S:
            add((ch,), ch, real[-1], real[-3:], facts=unanswered([(_f(m["ts"]), m["user"] == me) for m in real]))
    threads = data.get("threads") or {}
    for (ch, root), msgs in threads.items():
        real = [m for m in msgs if _real(m)]
        if not real or ch not in convs:
            continue
        mine = [_f(m["ts"]) for m in real if m["user"] == me]
        my_last = max(mine, default=0.0)
        after = [m for m in real if m["user"] != me and _f(m["ts"]) > my_last]
        if not after:
            continue
        is_thread = len(msgs) > 1 or bool(real[0].get("thread_ts"))
        if not is_thread and _f((data.get("owner_last") or {}).get(ch)) > _f(after[-1]["ts"]):
            continue  # a plain mention answered later in the channel
        unanswered_mention = any(mentions_me(m, me) for m in after)
        if not (mine and is_thread) and not unanswered_mention:
            continue
        ctx = real[-3:]
        add((ch, root), ch, after[-1], ctx, root if is_thread else None, mentioned=unanswered_mention,
            in_thread=bool(mine) and is_thread, facts=unanswered([(_f(m["ts"]), m["user"] == me) for m in real]))
    owner_last = data.get("owner_last") or {}
    pending = {(p[0], p[1]) for p in data.get("mentions") or []}  # unanswered mentions from earlier reads
    for ch, msgs in (data.get("chan") or {}).items():
        for i, m in enumerate(msgs):
            if not _real(m) or m["user"] == me or not mentions_me(m, me):
                continue
            if _f(m["ts"]) < data.get("window_start", 0) and (ch, m["ts"]) not in pending:
                continue
            root = m.get("thread_ts") or m["ts"]
            if (ch, root) in threads or _f(owner_last.get(ch)) > _f(m["ts"]):
                continue
            add((ch, root), ch, m, [x for x in msgs[: i + 1] if _real(x)][-3:], None, mentioned=True)

    rows = []
    for key, c in cands.items():
        conv = convs.get(c["ch"]) or {}
        m, uid = c["msg"], c["msg"]["user"]
        text = " ".join(slack_text(m.get("text") or "", users).split())
        age_s = max(0.0, now_ts - _f(m["ts"]))
        vip = is_vip(uid)
        reasons = (["VIP"] if vip else []) + (["Mentioned you"] if c["mentioned"] else []) \
            + (["In your thread"] if c["in_thread"] else [])
        if "?" in text:
            reasons.append("Direct question")
        if DEADLINE.search(text):
            reasons.append("Mentions a deadline")
        wait = _waiting(age_s)
        if wait:
            reasons.append(wait)
        if not reasons:
            continue
        kind = conv.get("kind")
        source = "Slack · DM" if kind == "dm" else "Slack · group DM" if kind == "group" else \
            "Slack · #" + (conv.get("name") or c["ch"])
        quote = '"' + _clip(text or "(no text)", 118) + '"'
        root = c["root"]
        thread_value = f"{c['ch']}:{root or m['ts']}".lower()
        ctx = [{"from": who(x["user"]),
                "date": f"{dt.datetime.fromtimestamp(_f(x['ts']), tz):%a %d %b} "
                        + clock(dt.datetime.fromtimestamp(_f(x["ts"]), tz), fmt),
                "text": slack_text(x.get("text") or "", users)} for x in c["ctx"]]
        rows.append({
            "who": _clip(who(uid), 60), "area": area, "source": source, "via": "slack", "text": quote,
            "short": _clip(quote, 40),
            "age": f"{int(age_s // 86400)}d" if age_s >= 86400 else f"{max(1, int(age_s // 3600))}h",
            "reasons": reasons, "vip": vip, "action": "Open", "link": slack_link(domain, c["ch"], m["ts"], root),
            "_age_s": age_s, "_last": _f(m["ts"]),
            "_done": f"slack:{ws.get('team_id', '')}:{c['ch']}:{root or m['ts']}",
            "_sup": {"source": "slack", "thread": thread_value, "sender": uid.lower(), "domain": None,
                     "labels": {"thread": source + (" thread" if root else " message"),
                                "sender": f"{who(uid)} (Slack)"}},
            "_key": f"slack:{ws.get('team_id', '')}:{c['ch']}:{root or ''}:{m['ts']}",
            "_messages": ctx,
            "_facts": dict(c["facts"], last=_f(m["ts"]), text=text)})
    heads = []
    for ch, msgs in (data.get("chan") or {}).items():
        conv = convs.get(ch) or {}
        for m in msgs:
            if len(heads) >= 5:
                break
            if not _real(m) or m["user"] == me or _f(m["ts"]) < data.get("window_start", 0):
                continue
            b = _BROADCAST.search(m.get("text") or "")
            if not b:
                continue
            body = " ".join(slack_text(_BROADCAST.sub("", m.get("text") or ""), users).split())
            where = "#" + conv["name"] if conv.get("kind") in ("channel", "private") else "a group DM"
            heads.append(_clip(f"@{b.group(1)} in {where}: {body}", 100))
    return rows, heads


def mentions_me(m, me):
    return isinstance(m.get("text"), str) and f"<@{me}" in m["text"]


# --- top of the day ------------------------------------------------------------
def _row_line(r):
    """A reply row in a top line: its gist summary when it has one (the writer's, or the fallback: the
    subject or quoted message), else its text."""
    g = r.get("gist")
    return g["summary"] if isinstance(g, dict) and isinstance(g.get("summary"), str) and g["summary"].strip() \
        else r.get("text") or ""


def rules_top(schedule, replies):
    """Top of the day, chosen by the rules (the writer only words it): up to 5 items, VIP replies, then
    clashes (the earlier event), then replies that mention a deadline, then the next reply rows in order.
    [{text, ref, kind}] with the rules' own text (used when the writer gave none)."""
    out, seen = [], set()

    def put(text, ref, kind):
        if ref and ref not in seen and len(out) < MAX_TOP:
            seen.add(ref)
            out.append({"text": _clip(text, 200), "ref": ref, "kind": kind})
    for r in replies:
        if r.get("vip"):
            put(f"Reply to {r['who']}: {_row_line(r)}", r.get("id"), "reply")
    clash_end = ""
    for e in schedule:
        start = e.get("start_hhmm") or e["time"]
        if e.get("clash") and start >= clash_end:  # one item per clash: the earlier event
            clash_end = e.get("end_hhmm") or e.get("end") or start
            put(f"Clash at {e['time']}: {e['title']}. {(e.get('flag') or {}).get('text', '')}".strip(),
                e.get("id"), "event")
    for r in replies:
        if "Mentions a deadline" in (r.get("reasons") or []):
            put(f"Deadline from {r['who']}: {_row_line(r)}", r.get("id"), "reply")
    for r in replies:
        put(f"Reply to {r['who']}: {_row_line(r)}", r.get("id"), "reply")
    return out


def top_texts(raw):
    """The writer's top wording from a cached summary: {ref: text}, from a list of {text, ref} (as stored) or
    an object keyed by ref."""
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    out = {}
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, dict) and isinstance(t.get("ref"), str) and isinstance(t.get("text"), str) \
                and t["text"].strip():
            out.setdefault(t["ref"], t["text"])
    return out


# --- the brief ---------------------------------------------------------------
def date_fields(day):
    return {"date": day.isoformat(), "date_long": f"{day:%A} {day.day} {day:%b}",
            "date_short": f"{day:%a} {day.day} {day:%b}"}


def fallback_gist(row):
    """A reply row's gist without the writer: its text (the subject, or the quoted message), no details."""
    return {"summary": row.get("text") or "", "details": []}


def with_gist(row, writer_on=True):
    """The row with a gist: the writer's (only while the writer is on), else the fallback."""
    g = row.get("gist")
    if not (writer_on and isinstance(g, dict) and isinstance(g.get("summary"), str) and g["summary"].strip()
            and isinstance(g.get("details"), list)):
        g = fallback_gist(row)
    return dict(row, gist=g)


def done_list(rows, tz, fmt, writer_on=True):
    """The brief's "Done today": [{id, who, summary, done_at (display)}], most recently done first."""
    out = []
    for r in rows or []:
        try:
            at = dt.datetime.fromisoformat(r["done_at"]).astimezone(tz)
        except (KeyError, TypeError, ValueError):
            continue
        out.append((at, {"id": r.get("id"), "who": r.get("who") or "",
                         "summary": with_gist(r, writer_on)["gist"]["summary"], "done_at": clock(at, fmt)}))
    out.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in out]


def compose_brief(sample, settings, areas, cache, live, now, writer_on=False):
    """sample: the bundled sample brief. cache: the brief saved by the last sync (or None).
    live: at least one account is connected. now: aware datetime. writer_on: show the AI writer's
    summary and suggestions from the cache (hidden once the writer is turned off). Top of the day: the
    writer's (its refs still rows) when shown, else the rules' list."""
    if not live:
        brief = dict(sample)
        brief["sample"] = True
        brief["sections"] = dict(SECTIONS_SAMPLE)
        brief["replies"] = [r if "gist" in r else with_gist(r) for r in brief.get("replies") or []]
        brief.setdefault("done", [])
        return brief
    tz = tzinfo(settings)
    fmt = time_format(settings)
    now = now.astimezone(tz)
    nxt, _ = next_call(settings, now)
    brief = {"sample": False, "sections": dict(SECTIONS_LIVE), "time_format": fmt,
             "areas": [{"id": a["id"], "name": a["name"], "colour": a["colour"]} for a in areas],
             "next_checkin": clock(nxt, fmt) if nxt else None,
             "projects": sample.get("projects", [])}
    if cache:
        written = dt.datetime.fromisoformat(cache["written_at"]).astimezone(tz)
        brief.update(date_fields(dt.date.fromisoformat(cache["date"])))
        sources = cache.get("sources", "")
        if cache.get("sources_tail"):
            sources = f"Written {clock(written, fmt)} {cache['sources_tail']}"
        brief.update(kind=cache.get("kind") or "Morning brief", written_at=clock(written, fmt),
                     schedule=render_schedule(cache.get("schedule", []), fmt), replies=cache.get("replies", []),
                     heads_up=cache.get("heads_up", []), sources=sources, hidden=int(cache.get("hidden") or 0))
        prev = cache.get("previous_at")
        brief["previous_at"] = clock(dt.datetime.fromisoformat(prev).astimezone(tz), fmt) if prev else None
        if not writer_on:
            brief["replies"] = [{k: v for k, v in r.items() if k not in ("suggestion", "suggestion_warning")}
                                for r in brief["replies"]]
        brief["replies"] = [with_gist(r, writer_on) for r in brief["replies"]]
        # the rules choose and order the rows; the writer's wording replaces the text of the refs it wrote
        brief["top"] = rules_top(brief["schedule"], brief["replies"])
        brief["top_by"] = "rules"
        summary = cache.get("summary")
        if writer_on and isinstance(summary, dict) and summary.get("text"):
            at = dt.datetime.fromisoformat(summary["at"]).astimezone(tz) if summary.get("at") else None
            brief["summary"] = {"text": summary["text"], "by": summary.get("by") or "",
                                "at": clock(at, fmt) if at else None,
                                "areas": {k: v for k, v in (summary.get("areas") or {}).items()
                                          if isinstance(v, str) and any(a["id"] == k for a in areas)}}
            words = top_texts(summary.get("top"))
            if any(t["ref"] in words for t in brief["top"]):
                brief["top"] = [dict(t, text=_clip(" ".join(words[t["ref"]].split()), 200)) if t["ref"] in words
                                else t for t in brief["top"]]
                brief["top_by"] = "writer"
            brief["summary"]["top"] = brief["top"] if brief["top_by"] == "writer" else []
            brief["sections"]["summary"] = "live"
        brief["done"] = done_list(cache.get("done"), tz, fmt, writer_on)
    else:
        brief.update(date_fields(now.date()))
        brief.update(kind=kind_for(settings, now), written_at=None, schedule=[], replies=[],
                     heads_up=["Callsheet hasn't read your accounts yet. It reads them at the next check-in."],
                     sources="Not read yet", top=[], top_by="rules", hidden=0, previous_at=None, done=[])
    return brief
