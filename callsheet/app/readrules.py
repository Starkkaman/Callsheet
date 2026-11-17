"""The deterministic reading rules the ledger's read uses (extract.py), kept from the 0.12 reader (reader.py, retired
in the rework) unchanged: the 0.12.2 date rules (relative dates from the message date, cancelled or moved, weekday vs
date, end of week or month, numeric dates in the owner's date order, invite times into the owner's zone) with the date
pieces from the 0.4.1 facts, the amount rules (totals over line items and subtotals, no ranges, no savings, copied from
one sentence), signatures without "-- " cut, the sentence caps, and the window helpers for the owner's quiet times
for the model server (settings model_quiet_times) that background requests stay out of. See docs/ledger.md.
"""
import datetime as dt
import re
import zoneinfo

import background

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


SENT_MAX = 60              # sentences numbered s1..s60
SENT_CHARS = 400
# money: a currency with a number, either way round ("$1,234.56", "1.234,56 €", "EUR 500", "USD 5k"); a bare "5k" only
# in a sentence about money. Shown as written: no currency or locale is assumed.
_CUR = r"(?:US\$|CA?\$|AU?\$|NZ\$|HK\$|S\$|R\$|[$€£¥₹₩₺]|\b(?:USD|EUR|GBP|CAD|AUD|NZD|JPY|INR|CHF|SEK|NOK|DKK|PLN|CZK|MXN|BRL|ZAR)\b)"
_NUM = r"\d{1,3}(?:[,.  ' ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_MULT = r"(?:\s?(?:k|K|m|M|bn)\b|\s(?:million|thousand)\b)?"
MONEY = re.compile(r"(?:" + _CUR + r"\s?(?:" + _NUM + r")" + _MULT + r"|(?<![\w.,])(?:" + _NUM + r")" + _MULT +
                   r"\s?" + _CUR + r")")
_BARE_K = re.compile(r"(?<![\w.,$€£])\d+(?:[.,]\d+)?\s?[kK]\b")
_MONEY_CUE = re.compile(r"\b(?:budget|cost|costs|price|priced|pay|paid|payment|invoice|fee|fees|quote|quoted|total|"
                        r"deposit|balance|owe|owed|amount|charge|charged|refund|bill|billed|rate|salary)\b", re.I)
# 0.12.2 amounts: a range is shown whole or not at all (here: not at all), never one end; line items, subtotals and
# savings are not the amount due; when the message states a total, only the total is shown.
_RV = r"(?:" + _CUR + r"\s?)?\d+(?:[.,' ]\d+)*(?:\s?(?:k|K|m|M|bn)\b)?(?:\s?" + _CUR + r")?"
_RANGE = re.compile(r"(?<![\w.,])(?P<a>" + _RV + r")\s*(?:-|–|—|to)\s*(?P<b>" + _RV + r")(?:\s*(?:dollars|euros|pounds|bucks))?"
                    r"|\bbetween\s+(?P<c>" + _RV + r")\s+and\s+(?P<d>" + _RV + r")(?:\s*(?P<w>dollars|euros|pounds|bucks))?",
                    re.I)
_MONEYISH = re.compile(_CUR + r"|\d\s?(?:k|K|m|M|bn)\b|\b(?:dollars|euros|pounds|bucks)\b")
_LINE = re.compile(r"\b(?:sub-?\s?total|line\s+items?|item\s+\d+|each|per|apiece|VAT|unit\s+price|qty|quantity)\b|"
                   r"\b\d+\s?[x×]\s|\b[x×]\s?\d+\b|\d\s?×", re.I)
_TOTAL_W = re.compile(r"(?<!sub-)(?<!sub )\b(?:total|amount\s+due|balance\s+due|to\s+pay)\b", re.I)
_PART = re.compile(r"\b(?:deposit|instal+ments?|down\s+payment|first\s+payment)\b", re.I)
_NOT_DUE = re.compile(r"\b(?:save[ds]?|saving|discount(?:ed)?|off\s+(?:the|your)|\d\s?%\s+off)\b", re.I)
def money_range(sentence):
    """A range of money in the sentence ("$1–2k", "2-3k" about money, "5–7k EUR", "between 500 and 800 dollars")."""
    s = str(sentence or "")
    for m in _RANGE.finditer(s):
        span = m.group(0)
        if _MONEYISH.search(span) or (_MONEY_CUE.search(s) and re.search(r"\d\s?[kKmM]\b", span)):
            return True
    return False

def amount_kind(sentence):
    """"total", "line" (line item or subtotal), "not_due" (a saving or discount) or "" for a sentence with money."""
    s = str(sentence or "")
    if _TOTAL_W.search(s):
        return "total"
    if _LINE.search(s):
        return "line"
    if _NOT_DUE.search(s):
        return "not_due"
    return ""

def pick_amount(sents, sid):
    """(amount or None, outcome) for the cited sentence among the item's sentences ({sid: text})."""
    if sid not in sents:
        return None, "not_given"
    s = sents[sid]
    if money_range(s):
        return None, "amount_range"
    found = amounts(s)
    if not found:
        return None, "amount_none"
    if len(found) > 1:
        return None, "amount_ambiguous"
    kind = amount_kind(s)
    if kind == "line":
        return None, "amount_line_item"
    if kind == "not_due":
        return None, "amount_not_due"
    totals = {k: amounts(v)[0] for k, v in sents.items() if amount_kind(v) == "total" and len(amounts(v)) == 1
              and not money_range(v)}
    if totals:
        if sid not in totals:
            return None, "amount_not_total"
        if len({_digits(a) for a in totals.values()}) > 1 or any(_PART.search(v) and amounts(v) for v in sents.values()):
            return None, "amount_ambiguous"          # two totals, or a deposit next to the total
    return found[0][:40], "shown"

# 0.12.2 signatures without "-- " and forwarded headers the quote cut leaves: never a source of amounts or names.
_CLOSING = re.compile(r"^\s*(?:thanks|thank\s+you|many\s+thanks|thx|best|best\s+wishes|all\s+the\s+best|regards|"
                      r"kind\s+regards|best\s+regards|warm\s+regards|warmly|cheers|sincerely|yours(?:\s+sincerely|"
                      r"\s+truly)?|take\s+care)\s*[,.!]?\s*$", re.I)
_CONTACT = re.compile(r"\+?\d[\d ()-]{7,}\d|\b(?:tel|phone|mobile|mob|fax|vat\s+no|reg(?:istered)?\.?\s+no|company\s+no|"
                      r"ltd|llc|inc|gmbh|plc|limited)\b|\s[|·]\s|[\w.+-]+@[\w-]+\.[\w.]+|\bwww\.", re.I)
_NAME_LINE = re.compile(r"^\s*[^\W\d_][\w'’.-]*(?:\s+[^\W\d_][\w'’.-]*){0,3}\s*$")   # "Dana", "Marcus Webb"
_FWD = re.compile(r"^\s*From:\s.+\n(?:.*\n){0,2}?\s*(?:Sent|Date):\s", re.M)
def strip_signature(text):
    """The text without a signature block written without "-- ": from a closing line ("Thanks,", "Best,") with at most
    8 short lines after it, the first a name, or a last paragraph of short contact lines (phone, company, "|" or "·" separators) with
    no sentence ending; also a lone "--" line and below, and a forwarded "From: … Sent:" header and below."""
    t = str(text or "")
    m = _FWD.search(t)
    if m and m.start() > 0:
        t = t[:m.start()]
    m = re.search(r"(?m)^--\s*$", t)
    if m and m.start() > 0:
        t = t[:m.start()]
    lines = t.rstrip().split("\n")
    for i, ln in enumerate(lines):
        after = [x for x in lines[i + 1:] if x.strip()]
        if i and _CLOSING.match(ln) and after and len(after) <= 8 and all(len(x) <= 80 for x in after) and \
                _NAME_LINE.match(after[0]) and any(x.strip() for x in lines[:i]):
            return "\n".join(lines[:i]).rstrip()
    paras = re.split(r"\n[ \t]*\n", t.rstrip())
    if len(paras) > 1:
        last = [x for x in paras[-1].split("\n") if x.strip()]
        if last and len(last) <= 6 and all(len(x) <= 90 and not re.search(r"[.?!]\s*$", x) for x in last) and \
                any(_CONTACT.search(x) for x in last):
            return "\n\n".join(paras[:-1]).rstrip()
    return t.rstrip()

def amounts(sentence):
    """The amounts of money in one sentence, as written (trimmed)."""
    s = str(sentence or "")
    out = [m.group(0).strip() for m in MONEY.finditer(s)]
    if _MONEY_CUE.search(s):
        taken = [m.span() for m in MONEY.finditer(s)]
        for m in _BARE_K.finditer(s):
            if not any(a <= m.start() < b for a, b in taken):
                out.append(m.group(0).strip())
    return out

# --- 0.12.2 dates: what a sentence says is due, deterministically --------------------------------------------------------
# A date is read as a "mention": the date pieces written next to each other ("Tue 10 Nov 2026", "Friday, 6 Nov",
# "close of business Thursday"). Rules (the acceptance rules' follow-ups, docs/readrules.md):
# - a weekday written with a date must be that date's weekday, else the mention says nothing;
# - a weekday alone is the next one after the message date; said on that weekday it is a week on, unless the sentence
#   says "today"; "next Friday" is either of the next two (people differ);
# - "EOW"/"end of (the) week": that week's Friday; "EOM"/"end of (the) month": the month's last day; "the 15th": the
#   next 15th on or after the message date; "30/11": by the owner's date order (d/m or m/d), or when only one reading
#   is a date; "03/04" without a date order says nothing; numeric dates count only after "on", "by", "due" and the like;
# - a time with a time zone next to the date ("09:00 ET", "4pm PT", "23:59 AoE") moves the date into the owner's zone;
# - cancelled, called off, gone, won't happen: the clause's dates are never due; moved, rescheduled, postponed: the
#   dates before the move word, after "was", "instead of", and after "from" in a move, are old, the ones after it are the new ones. An
#   old or cancelled date is never due anywhere in the item.
_EXTRA = re.compile(
    r"(?P<eow>\b(?:EOW|end\s+of\s+(?:the\s+)?week)\b)"
    r"|(?P<eom>\b(?:EOM|end\s+of\s+(?:the\s+)?month|month[- ]end)\b)"
    r"|(?P<num>(?<![\d/.,:])(?P<n1>\d{1,2})(?P<nsep>[/.])(?P<n2>\d{1,2})(?:(?P=nsep)(?P<n3>\d{4}|\d{2}))?(?![\d/]|\.\d))",
    re.I)
_NUM_CUE = re.compile(r"\b(?:on|by|due|until|till|before|after|from|close[sd]?|deadline|date|dated|expires?|ends?)\s*:?\s*$",
                      re.I)
_DATE_KINDS = ("iso", "md", "dm", "wd", "rel", "nth", "eow", "eom", "num")
_TIME = r"(?:\d{1,2}(?::[0-5]\d)?\s*[aApP]\.?[mM]\.?|\b(?:[01]?\d|2[0-3]):[0-5]\d)"
_TZ_NAMES = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York", "Eastern": "America/New_York",
             "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago", "Central": "America/Chicago",
             "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver", "Mountain": "America/Denver",
             "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
             "Pacific": "America/Los_Angeles", "GMT": "UTC", "UTC": "UTC", "BST": "Europe/London",
             "CET": "Europe/Paris", "CEST": "Europe/Paris", "AoE": "-12"}
_TZ_AT = re.compile(r"(?P<t>" + _TIME + r")(?:\s*(?:-|–|—|to)\s*" + _TIME + r")?\s*\(?\s*(?P<z>" +
                    "|".join(sorted(_TZ_NAMES, key=len, reverse=True)) + r")\b")
_JOIN_GAP = re.compile(r"[\s,]*(?:\d{4}\b)?[\s,]*(?:(?:at|on|by|@|the)[\s,]+)?", re.I)
_CANCEL = re.compile(r"\b(?:cancel+(?:ed|ing|ation)?|called\s+off|call(?:ing)?\s+off|(?:is|are|'s|’s)\s+off\b|"
                     r"won['’]t\s+happen|will\s+not\s+happen|(?:is|are)\s+gone|no\s+longer|void(?:ed)?|scrapped|"
                     r"not\s+going\s+ahead)\b", re.I)
_MOVE = re.compile(r"\b(?:moved?|moves|moving|reschedul\w*|postpon\w*|pushed|push(?:ing)?\s+back|brought\s+forward|"
                   r"changed|new\s+(?:time|date|deadline)|(?:is|are|'s|’s)\s+now|now\s+(?:on|at|due)|delayed)\b|"
                   r"(?:^|[,:]\s*)now\b", re.I)
_OLD_BEFORE = re.compile(r"\b(?:was|were|originally|previously|instead\s+of|rather\s+than)\s*(?:on\s+|the\s+)?$", re.I)
_FROM_BEFORE = re.compile(r"\bfrom\s*(?:on\s+|the\s+)?$", re.I)     # "from Friday to Monday": old only in a move
_CLAUSE = re.compile(r";|\bbut\b|\(", re.I)
def _zone(name):
    if name == "-12":
        return dt.timezone(dt.timedelta(hours=-12))
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None

def _pieces(s):
    """Date and time pieces (facts' pieces plus EOW, EOM and numeric dates), in order, not overlapping."""
    ps = []
    for m in _PIECE.finditer(s):  # noqa: SLF001
        kind = next(k for k in ("iso", "md", "dm", "wd", "rel", "nth", "hm", "h24", "noon") if m.group(k))
        ps.append((m.start(), m.end(), kind, m))
    for m in _EXTRA.finditer(s):
        kind = next(k for k in ("eow", "eom", "num") if m.group(k))
        ps.append((m.start(), m.end(), kind, m))
    ps.sort(key=lambda p: (p[0], -(p[1] - p[0])))
    out, end = [], -1
    for p in ps:
        if p[0] >= end:
            out.append(p)
            end = p[1]
    return out

def _num_date(m, sent, order):
    """A numeric date ("30/11", "11/30/2026") by the owner's date order ("dmy", "mdy" or None)."""
    a, b = int(m.group("n1")), int(m.group("n2"))
    y = m.group("n3")
    reads = []
    for d, mo, o in ((a, b, "dmy"), (b, a, "mdy")):
        if order and o != order:
            continue
        try:
            x = dt.date(int(y) + (2000 if len(y) == 2 else 0) if y else sent.year, mo, d)
        except ValueError:
            continue
        if not y and x < sent - dt.timedelta(days=60):
            x = dt.date(x.year + 1, mo, d)
        reads.append(x)
    reads = set(reads)
    return reads.pop() if len(reads) == 1 else None

def _mentions(s, sent, order=None):
    """[(start, end, dates set)] for each run of date pieces; times stay out of mentions."""
    groups, cur = [], []
    for p in _pieces(s):
        if p[2] not in _DATE_KINDS:
            if cur:
                groups.append(cur)
                cur = []
            continue
        if cur and not _JOIN_GAP.fullmatch(s[cur[-1][1]:p[0]]):
            groups.append(cur)
            cur = []
        cur.append(p)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        dates = _resolve(g, s, sent, order)
        out.append((g[0][0], g[-1][1], dates))
    return out

def _resolve(g, s, sent, order):
    explicit, wds, rel = set(), [], []
    for a, b, kind, m in g:
        if kind == "num":
            if not _NUM_CUE.search(s[:a]) and len(g) == 1:
                return set()
            d = _num_date(m, sent, order)
            if d is None:
                return set()
            explicit.add(d)
        elif kind in ("iso", "md", "dm", "nth"):
            d = _date_of(m, sent)  # noqa: SLF001
            if d is None:
                return set()
            explicit.add(d)
        elif kind == "wd":
            wds.append((_DAYNAMES[m.group("wd_n").lower()], bool(m.group("wd_next"))))  # noqa: SLF001
        elif kind == "rel":
            rel.append(" ".join(m.group(0).lower().split()))
        elif kind == "eow":
            explicit.add(sent + dt.timedelta(days=(4 - sent.weekday()) % 7))
        elif kind == "eom":
            nxt = dt.date(sent.year + (sent.month == 12), sent.month % 12 + 1, 1)
            explicit.add(nxt - dt.timedelta(days=1))
    rel_d = {"today": sent, "tomorrow": sent + dt.timedelta(days=1)}
    fixed = {rel_d[r] for r in rel if r in rel_d}           # "tonight", "EOD", "end of day": a time, not a date
    if len(explicit) > 1 or len(fixed) > 1 or len({w for w, _ in wds}) > 1:
        return set()
    if explicit:
        d = next(iter(explicit))
        if any(w != d.weekday() for w, _ in wds) or (fixed and fixed != {d}):
            return set()                                    # "Tue 10 Nov" when 10 Nov is a Wednesday: nothing
        return {d}
    if wds:
        w, nxt = wds[0]
        ahead = (w - sent.weekday()) % 7
        if nxt:
            base = sent + dt.timedelta(days=ahead or 7)
            out = {base, base + dt.timedelta(days=7)}
        else:
            if ahead == 0 and not re.search(r"\btoday\b", s, re.I):
                ahead = 7                                   # "by Monday" said on a Monday: next Monday
            out = {sent + dt.timedelta(days=ahead)}
        if fixed and not fixed <= out:
            return set()
        return out if not fixed else fixed
    if rel and not fixed:
        return {sent} if any(r in ("tonight", "eod") or r.startswith("end of") for r in rel) else set()
    return fixed

def _shift(s, start, end, prev_end, next_start, dates, tz):
    """A time with a zone next to the mention ("at 07:00 CET", "23:59 AoE on"): the date in the owner's zone."""
    if tz is None or len(dates) != 1:
        return dates
    d = next(iter(dates))
    m = None
    for lo, hi in ((end, next_start), (prev_end, start)):
        for x in _TZ_AT.finditer(s, lo, hi):
            m = x
            break
        if m:
            break
    if not m:
        return dates
    src = _zone(_TZ_NAMES[m.group("z")])
    t = m.group("t").lower().replace(".", "").replace(" ", "")
    try:
        if t.endswith(("am", "pm")):
            hm = t[:-2].split(":")
            h, mi = int(hm[0]) % 12 + (12 if t.endswith("pm") else 0), int(hm[1]) if len(hm) > 1 else 0
        else:
            h, mi = (int(x) for x in t.split(":"))
        when = dt.datetime(d.year, d.month, d.day, h, mi, tzinfo=src)
    except (ValueError, TypeError):
        return set()
    return {when.astimezone(tz).date()} if src is not None else dates

def _read(sentence, sent, tz=None, order=None):
    """[(start, end, dates, "new"|"old")] for each mention that says a date."""
    s = str(sentence or "")
    ms = _mentions(s, sent, order)
    out = []
    bounds = [0] + [m.end() for m in _CLAUSE.finditer(s)] + [len(s) + 1]
    for i, (a, b, dates) in enumerate(ms):
        dates = _shift(s, a, b, ms[i - 1][1] if i else 0, ms[i + 1][0] if i + 1 < len(ms) else len(s), dates, tz)
        if not dates:
            continue
        lo = max(x for x in bounds if x <= a)
        hi = min(x for x in bounds if x > a)
        clause = s[lo:hi]
        mv = _MOVE.search(clause)
        before = s[max(lo, a - 24):a]
        if _OLD_BEFORE.search(before) or (mv and _FROM_BEFORE.search(before)):
            st = "old"
        elif mv:
            st = "old" if a - lo < mv.start() else "new"
        elif _CANCEL.search(clause) or (s[lo - 1:lo] == "(" and _CANCEL.search(s[:lo]) and not _MOVE.search(s)):
            st = "old"
        else:
            st = "new"
        out.append((a, b, dates, st))
    return out

def date_reading(sentence, sent, tz=None, order=None):
    """(due dates the sentence says, old or cancelled dates it names), read from the message's own date (sent) in the
    owner's zone (tz) and date order."""
    new, old = set(), set()
    for _a, _b, dates, st in _read(sentence, sent, tz, order):
        (new if st == "new" else old).update(dates)
    return new, old

def _digits(tok):
    return re.sub(r"[^\d]", "", tok)

def in_windows(t, windows):
    return any(background.in_window(t, st, en) for st, en in windows)

def seconds_to_window(now, windows):
    """Seconds from now (aware) until the next window starts (within a day)."""
    best = 86400.0
    for st, _ in windows:
        h, m = map(int, st.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now:
            start += dt.timedelta(days=1)
        best = min(best, (start - now).total_seconds())
    return best
