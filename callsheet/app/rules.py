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


# --- call times --------------------------------------------------------------
def _calls(settings):
    out = []
    for c in settings.get("call_times", []):
        if c.get("enabled"):
            h, m = (int(x) for x in c["time"].split(":"))
            out.append((dt.time(h, m), c))
    return sorted(out, key=lambda x: x[0])


def is_day_off(settings, day):
    """Not one of the workdays. 0.6.2: only the urgent light check (push.check_due, with skip_checkins_days_off)
    still uses this; call times have their own days (call_days)."""
    return WEEKDAYS[day.weekday()] not in settings.get("workdays", WEEKDAYS)


def call_days(settings, c):
    """0.6.2: the weekdays a call time runs on: its own "days" (Store.settings fills them in), else the rule before
    0.6.2 (the morning brief every day, check-ins on workdays when skip_checkins_days_off is set)."""
    days = c.get("days")
    if isinstance(days, list) and days:
        return days
    if c.get("id") != "morning" and settings.get("skip_checkins_days_off"):
        return settings.get("workdays", WEEKDAYS)
    return WEEKDAYS


def next_call(settings, after):
    """First enabled call time strictly after `after` (aware datetime), on one of its days (call_days) in the
    settings' time zone. Returns (datetime, call) or (None, None)."""
    tz = tzinfo(settings)
    after = after.astimezone(tz)
    calls = _calls(settings)
    for i in range(9):
        day = after.date() + dt.timedelta(days=i)
        for t, c in calls:
            if WEEKDAYS[day.weekday()] not in call_days(settings, c):
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


def overlaps(a, b):
    """The clash rule: two timed events overlap when each starts before the other ends. a, b: {start, end}
    (datetimes, or "HH:MM" strings of one day, which compare the same way)."""
    return a["start"] < b["end"] and b["start"] < a["end"]


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


# --- 0.8.0: sender checks and security heads-ups -------------------------------------------------------------
# Security, bank and 2FA mail always surfaces (a heads-up that no rule, category or suppression hides).
SECURITY = re.compile(
    r"\b(?:verification\s+code|security\s+code|one[-\s]?time\s+(?:pass)?code|one[-\s]?time\s+password|\bOTP\b|"
    r"2-?step|two[-\s]?(?:step|factor)|\b2FA\b|sign[-\s]?in\s+attempt|new\s+sign[-\s]?in|signed\s+in\s+(?:to|on|from)|"
    r"new\s+(?:device|login|log[-\s]?in)|login\s+attempt|unusual\s+(?:sign[-\s]?in|activity|login)|"
    r"suspicious\s+(?:activity|sign[-\s]?in|login|transaction)|password\s+(?:reset|change[ds]?|was\s+changed)|"
    r"reset\s+your\s+password|security\s+alert|critical\s+security|account\s+(?:locked|suspended|disabled|"
    r"compromised|on\s+hold)|payment\s+(?:failed|declined|was\s+declined|unsuccessful)|card\s+(?:was\s+)?declined|"
    # 0.8.4 (the 0.8.3 screen's bank miss): "card ending 2290 was declined", "transaction declined", "payment
    # method has been declined", "your payment didn't go through", "we couldn't process your payment"
    r"(?:card|payment|transaction|purchase|charge)(?:\s+method)?\s+(?:(?:ending|that\s+ends)\s+(?:in\s+|with\s+)?"
    r"[x*\u2022.\d\s-]{2,14}?\s+)?(?:was\s+|has\s+been\s+|got\s+|is\s+)?(?:declined|failed|refused)|"
    r"(?:payment|transaction|charge)\s+(?:didn'?t|did\s+not|couldn'?t|could\s+not)\s+go\s+through|"
    r"(?:couldn'?t|could\s+not|were\s+unable\s+to|unable\s+to)\s+process\s+your\s+(?:payment|card)|"
    r"\bfraud|overdra(?:wn|ft)|insufficient\s+funds|confirm\s+it'?s\s+you|is\s+your\s+(?:verification\s+)?code)",
    re.I)
# Senders whose security mail is genuine when the address is theirs (or a subdomain) and Gmail checked it.
SECURITY_DOMAINS = {
    "google.com": "Google", "accounts.google.com": "Google", "apple.com": "Apple", "microsoft.com": "Microsoft",
    "live.com": "Microsoft", "paypal.com": "PayPal", "amazon.com": "Amazon", "github.com": "GitHub",
    "slack.com": "Slack", "stripe.com": "Stripe", "wise.com": "Wise", "chase.com": "Chase",
    "bankofamerica.com": "Bank of America", "wellsfargo.com": "Wells Fargo", "capitalone.com": "Capital One",
    "americanexpress.com": "American Express", "citi.com": "Citi", "discover.com": "Discover",
    "usbank.com": "U.S. Bank", "schwab.com": "Schwab", "fidelity.com": "Fidelity", "venmo.com": "Venmo",
    "coinbase.com": "Coinbase", "dropbox.com": "Dropbox", "facebookmail.com": "Facebook", "instagram.com": "Instagram"}
# look-alike letters folded before comparing a domain with the brands above (digits, Cyrillic and Greek letters)
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t", "8": "b", "9": "g",
                             "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                             "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ⅼ": "l", "ο": "o", "α": "a", "ε": "e",
                             "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "υ": "u", "χ": "x"})


def auth_level(headers):
    """Gmail's verdict on the sender: "pass" | "fail" | "none". headers: [(name, value)] in message order, or a
    {lowercase name: value} dict. Only the topmost Authentication-Results (else ARC-Authentication-Results) from
    mx.google.com counts: a header further down could be written by the sender. "fail" when DMARC fails, or SPF
    fails (fail/softfail) and DMARC didn't pass; "pass" when DMARC passes, or SPF or DKIM passes with no failure."""
    if isinstance(headers, dict):
        items = [(k, v) for k, v in headers.items()]
    else:
        items = list(headers or [])
    res = None
    for want in ("authentication-results", "arc-authentication-results"):
        for name, value in items:
            if str(name).lower() == want:
                res = str(value)
                break
        if res is not None:
            break
    if res is None:
        return "none"
    v = " ".join(res.split()).lower()
    authserv = re.sub(r"^i=\d+;\s*", "", v).split(";", 1)[0].strip()
    if not authserv.startswith("mx.google.com"):
        return "none"

    def result(method):
        m = re.search(r"(?:^|[;\s])" + method + r"=([a-z]+)", v)
        return m.group(1) if m else None
    dmarc, spf, dkim = result("dmarc"), result("spf"), result("dkim")
    if dmarc == "pass":
        return "pass"
    if dmarc == "fail" or spf in ("fail", "softfail"):
        return "fail"
    if spf == "pass" or dkim == "pass":
        return "pass" if dkim != "fail" else "fail"
    return "none"


def _skeleton(domain):
    d = (domain or "").lower().strip(".")
    try:
        d = ".".join(lb.encode("ascii").decode("idna") if lb.startswith("xn--") else lb for lb in d.split("."))
    except (UnicodeError, ValueError):
        pass
    return d.translate(_CONFUSABLE).replace("rn", "m").replace("vv", "w")


def genuine_domain(domain):
    """The brand name when domain is one of SECURITY_DOMAINS or a subdomain of one, else None."""
    d = (domain or "").lower().strip(".")
    for known, brand in SECURITY_DOMAINS.items():
        if d == known or d.endswith("." + known):
            return brand
    return None


def look_alike(domain):
    """The brand a domain imitates without being it (paypa1.com, chase-secure-login.net, xn--pypal-4ve.com,
    google.com.account-check.io), else None."""
    d = (domain or "").lower().strip(".")
    if not d or genuine_domain(d):
        return None
    sk = _skeleton(d)
    labels = [x for x in re.split(r"[.-]", sk) if x]
    for known, brand in SECURITY_DOMAINS.items():
        name = known.split(".")[-2]
        if len(name) < 4:
            continue
        if sk == known or any(lb == name or (len(name) >= 5 and name in lb) for lb in labels) or \
                known in sk:
            return brand
    if d.startswith("xn--") or ".xn--" in d or any(ord(c) > 127 for c in d):
        return "a known sender"
    return None


def security_alert(hdr, addr, subject, snippet, auth="none"):
    """0.8.0: a security, bank or 2FA alert (the SECURITY words in the subject or snippet), whoever sent it:
    {"marks": [...], "brand": the genuine sender's name or None}, or None. Marks: "checked" (a known sender's own
    domain and Gmail checked it), "lure" (a look-alike of a known sender: never marked as that sender),
    "sender_fail" (Gmail couldn't confirm the sender). The alert surfaces in every case."""
    text = f"{subject}\n{snippet}"
    if not SECURITY.search(text):
        return None
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    marks, brand = [], None
    lure = look_alike(domain)
    if auth == "fail":
        marks.append("sender_fail")
    if lure:
        marks.append("lure")
    elif genuine_domain(domain) and auth == "pass":
        brand = genuine_domain(domain)
        marks.append("checked")
    return {"marks": marks, "brand": brand, "lure": lure}


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


_REPLY_SUBJECT = re.compile(r"\s*(?:re|fwd?|aw|wg|sv)\s*(?:\[\d+\])?\s*:", re.I)


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


# --- Signal (0.5.1) ------------------------------------------------------------


# --- top of the day ------------------------------------------------------------


# --- the brief ---------------------------------------------------------------
def date_fields(day):
    return {"date": day.isoformat(), "date_long": f"{day:%A} {day.day} {day:%b}",
            "date_short": f"{day:%a} {day.day} {day:%b}"}


NEAR_CALL_S = 600


def near_call(settings, now):
    """'<label> at HH:MM' when an enabled call time is within 10 minutes before or after now (aware), else None (from
    the retired writer evaluation; Know me's builds wait for it)."""
    nxt, call = next_call(settings, now - dt.timedelta(seconds=NEAR_CALL_S + 1))
    if nxt is not None and nxt <= now + dt.timedelta(seconds=NEAR_CALL_S):
        return f"{call.get('label') or 'A brief'} at {hhmm(nxt)}"
    return None
