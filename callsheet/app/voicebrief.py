"""0.24.3 the morning brief for listening: GET /v1/brief/today?for=voice (docs/voice-api.md; owner 2026-11-18).

Built by plain code from the same plan as the screen brief (brief2.Planner: the same steps, the same promotion and
noise filters, the same counts), never by the model, so it is ready at any time and the same store gives the same
words. Each step: a stable id (its kind and the day), its title, `say` (written for listening: sentences under 20
words; no markdown, emoji, links or parentheses; times as words, "six thirty in the evening", "at noon"; first names;
money rounded, "about forty dollars", except a bill's own amount), its word count, at most three items with their ids
and labels, `more` (how many it didn't name, said as "And four more.") and at most one question.
Read-only: nothing here writes. Labels come from the ledger (who and what): message words, so untrusted data.
"""
import datetime as dt
import hashlib
import json
import re
import unicodedata

SAY_MAX = 3              # items a step names
SENTENCE_MAX = 19        # words in one sentence (under 20)
LABEL_WORDS = 12         # words in one item's label
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
        "twelve": "twelfth", "twenty": "twentieth", "thirty": "thirtieth"}
_DAYS = {"mon": "Monday", "tue": "Tuesday", "tues": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
         "thur": "Thursday", "thurs": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "may": "May", "jun": "June",
           "jul": "July", "aug": "August", "sep": "September", "sept": "September", "oct": "October",
           "nov": "November", "dec": "December"}
_CUR = {"$": "dollars", "€": "euros", "£": "pounds"}
_COMPANY = re.compile(r"\b(inc|llc|ltd|co|corp|company|rentals?|studios?|bank|group|team|support|services?|store|"
                      r"shop|airlines?|air|energy|insurance|clinic|pharmacy|school|labs?|media|health|billing|"
                      r"payments?|hotel|travel|club|council|city|county|the)\b", re.I)
QUESTIONS = {"clashes": "Which one do you want to keep?",
             "needs": "Should I snooze any of these until tomorrow morning?",
             "mail": "Want to hear more about one of these?"}


def words(n):
    """0 to 999,999,999 in words ("one thousand two hundred forty")."""
    n = int(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + words(n % 100))
    for size, name in ((10 ** 6, "million"), (1000, "thousand")):
        if n >= size:
            return words(n // size) + " " + name + ("" if n % size == 0 else " " + words(n % size))
    return str(n)


def ordinal(n):
    w = words(n).split()
    last = w[-1]
    w[-1] = _ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return " ".join(w)


def spoken_time(h, m, ampm):
    """"6:30 pm" -> "six thirty in the evening"; 12 pm -> "noon"; 12 am -> "midnight"."""
    h, m, ampm = int(h), int(m or 0), (ampm or "").lower().replace(".", "")
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if m == 0 and h == 12:
        return "noon"
    if m == 0 and h in (0, 24):
        return "midnight"
    h12 = h % 12 or 12
    mins = "" if m == 0 else " oh " + _ONES[m] if m < 10 else " " + words(m)
    part = "in the morning" if h < 12 else "in the afternoon" if h < 17 else "in the evening"
    return f"{words(h12)}{mins} {part}"


def _round_money(v):
    """About: two significant figures (84 -> 85 under a hundred: the nearest five)."""
    if v < 10:
        return max(1, round(v))
    if v < 100:
        return int(5 * round(v / 5))
    digits = len(str(int(v))) - 2
    return int(round(v, -digits))


def money_words(sym, num, exact):
    unit = _CUR.get(sym, "dollars")
    v = float(num.replace(",", ""))
    if exact:
        whole, cents = int(v), int(round((v - int(v)) * 100))
        out = f"{words(whole)} {unit if whole != 1 else unit[:-1]}"
        return out + (f" and {words(cents)} cents" if cents else "")
    r = _round_money(v)
    if 1000 < r < 10000 and r % 1000 and r % 100 == 0:     # 1,200 -> "twelve hundred"
        return f"about {words(r // 100)} hundred {unit}"
    return f"about {words(r)} {unit if r != 1 else unit[:-1]}"


_URL = re.compile(r"\b(?:https?://|www\.)\S+|\S+@\S+\.\w+", re.I)
_MONEY = re.compile(r"([$€£])\s?(\d[\d,]*(?:\.\d{1,2})?)")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)(?![a-z])", re.I)
_TIME24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_DAY = re.compile(r"\b(" + "|".join(sorted(_DAYS, key=len, reverse=True)) + r")\b\.?", re.I)
_UNITS = {"h": "hours", "hr": "hours", "hrs": "hours", "min": "minutes", "mins": "minutes", "d": "days",
          "wk": "weeks", "wks": "weeks"}


def _emoji_free(s):
    return "".join(ch for ch in s if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co")
                   and not 0xFE00 <= ord(ch) <= 0xFE0F and ch not in "‍⃣")


def spoken(text, exact_money=False):
    """Text made for listening: no links, addresses, emoji, markdown or parentheses; times, dates, money and
    numbers as words."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _URL.sub("", s)
    s = _emoji_free(s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", "").replace("”", "").replace('"', "")
    s = re.sub(r"(\d)\s*[–—-]\s*(\d)", r"\1 to \2", s)
    s = re.sub(r"\s*[–—]\s*", " to ", s) if _TIME.search(s) else re.sub(r"\s*[–—]\s*", ", ", s)
    s = re.sub(r"[(){}\[\]]", ", ", s)
    s = re.sub(r"[*_`#>|~<^\\]+", " ", s)
    s = re.sub(r"\s*·\s*([0-9]{1,2}(?::\d\d)?\s*[ap]\.?m)", r" at \1", s, flags=re.I)
    s = re.sub(r"\b(\d{1,2}|[A-Za-z]{3,}),?\s+(\d{1,2}(?::\d\d)?\s*[ap]\.?m)", lambda m: m.group(1) + (
        " " if m.group(1).lower() in ("at", "from", "to", "by", "until", "after", "before", "around") else " at ") +
        m.group(2), s, flags=re.I)
    s = re.sub(r"\s*[·•]\s*", ", ", s)
    s = _MONEY.sub(lambda m: money_words(m.group(1), m.group(2), exact_money), s)
    s = _TIME.sub(lambda m: spoken_time(m.group(1), m.group(2), m.group(3)), s)
    s = _TIME24.sub(lambda m: spoken_time(m.group(1), m.group(2), ""), s)
    s = _DATE.sub(lambda m: f"{_MONTHS[m.group(1)[:3].lower()] if m.group(1)[:4].lower() != 'sept' else 'September'} "
                            f"{ordinal(int(m.group(2)))}" if 1 <= int(m.group(2)) <= 31 else m.group(0), s)
    s = _DAY.sub(lambda m: _DAYS[m.group(1).lower()], s)
    s = re.sub(r"\b(" + "|".join(set(_DAYS.values())) + r") (" + "|".join(set(_MONTHS.values())) + r")\b", r"\1, \2", s)
    s = re.sub(r"\b(\d{1,4})\s*(hrs?|h|mins?|d|wks?)\b", lambda m: f"{m.group(1)} {_UNITS[m.group(2).lower()]}", s)
    s = re.sub(r"\b1 (hours|minutes|days|weeks)\b", lambda m: "1 " + m.group(1)[:-1], s)
    s = re.sub(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{1,6})(?![\w.,]\d)", lambda m: words(int(m.group(1).replace(",", ""))), s)
    s = re.sub(r"\bat (noon|midnight)\b", r"at \1", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:")
    return s


def _lower(s):
    w = s.split(" ", 1)
    return s if not w[0] or w[0] in ("Slack", "Signal", "Gmail", "YouTube", "GitHub") else w[0].lower() + s[len(w[0]):]


def cap_words(s, n):
    w = s.split()
    return s if len(w) <= n else " ".join(w[:n]).rstrip(",;:")


def sentence(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip(" ,;:")
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    w = s.split()
    if len(w) > SENTENCE_MAX:
        s = " ".join(w[:SENTENCE_MAX]).rstrip(",;:")
    return s if s[-1] in ".?!" else s + "."


def split(text):
    """One or more sentences (". " inside a line starts a new one), each under 20 words."""
    return [sentence(x) for x in re.split(r"(?<=\w)\.\s+", str(text or "")) if x.strip(" ,.")]


def sentences(say):
    return [x for x in re.split(r"(?<=[.?!])\s+", say.strip()) if x]


def word_count(say):
    return len(re.findall(r"[A-Za-z0-9']+", say))


class Names:
    """First names as the owner says them: a person People knows -> their one-word alias, else their first name; a
    company or a list stays whole; an address -> "someone"."""

    def __init__(self, directory=None):
        self.d = directory

    def __call__(self, name):
        name = re.sub(r"\s+", " ", str(name or "")).strip()
        if not name:
            return ""
        if "@" in name:
            return "someone"
        p = None
        if self.d is not None:
            try:
                p = self.d.persons.get(name)
            except Exception:  # noqa: BLE001
                p = None
        if p is not None and p.get("state") != "me":
            one = [a for a in p.get("aliases") or [] if a and " " not in a.strip() and "@" not in a]
            return one[0] if one else str(p.get("name") or name).split()[0]
        parts = name.split()
        if 2 <= len(parts) <= 3 and all(re.fullmatch(r"[A-Z][a-z'’-]+", x) for x in parts) and not _COMPANY.search(name):
            return parts[0]
        return name


def _id(ref):
    k, _, n = str(ref or "").partition(":")
    return {"item": "l", "clash": "c", "cal": "e"}.get(k, "") + n if k in ("item", "clash", "cal") and n.isdigit() \
        else None


class VoiceBrief:
    """build(plan) -> {date, steps_total, etag, steps}. plan: brief2.Planner.plan(now)."""

    def __init__(self, names=None):
        self.name = names or Names()

    def build(self, plan):
        day = plan["day"]
        bills = {f["ref"] for s in plan["steps"] if s["key"] == "money" for f in s["facts"] if f.get("amount")}
        steps = []
        for s in plan["steps"]:
            st = self.step(s, bills)
            if st is None:
                continue
            st["id"] = f"{s['key']}-{day}"
            steps.append(st)
        for i, st in enumerate(steps):
            st["n"] = i + 1
        out = {"date": day, "steps_total": len(steps),
               "steps": [{k: st[k] for k in ("id", "n", "title", "say", "words", "items", "more")} |
                         ({"question": st["question"]} if st.get("question") else {}) for st in steps]}
        out["etag"] = '"v' + hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:30] + '"'
        return out

    @staticmethod
    def _label(text, exact=False):
        return cap_words(spoken(text, exact), LABEL_WORDS)

    @staticmethod
    def _words(text, exact=False):
        return spoken(text, exact)

    def step(self, s, bills):
        k, f = s["key"], s["facts"]
        fn = getattr(self, "_" + k, None)
        if fn is None:
            return None
        intro, rows, total = fn(s, f, bills)
        said = rows[:SAY_MAX]
        more = max(0, total - len(said))
        parts = split(intro) + [x for line, _ref, _lab in said for x in split(line)]
        if more:
            parts.append(sentence(f"And {words(more)} more"))
        q = QUESTIONS.get(k) if said else None
        if q:
            parts.append(q)
        say = " ".join(p for p in parts if p)
        return {"title": spoken(s["title"]) or s["title"], "say": say, "words": word_count(say),
                "items": [{"item_id": _id(ref), "label": lab} for _l, ref, lab in said], "more": more,
                "question": q}

    # one function per step kind: (intro, [(sentence, ref, label)], how many there are) --------------------------------
    def _day(self, s, f, bills):
        ev = [x for x in f if "time" in x]
        if not ev:
            return "Nothing is on your calendar today", [], 0
        rows = []
        for x in ev:
            what = self._words(x["what"])
            rows.append((f"{what} at {spoken(x['time'])}", x.get("ref"), cap_words(f"{what}, {spoken(x['time'])}",
                                                                                   LABEL_WORDS)))
        n = len(ev)
        intro = f"You have {words(n)} {'thing' if n == 1 else 'things'} on your calendar today"
        free = next((x["free"] for x in f if "free" in x), "")
        if free and n <= SAY_MAX:
            rows[-1] = (rows[-1][0] + f". Then you're {spoken(free).lower()}", rows[-1][1], rows[-1][2])
        return intro, rows, n

    def _clashes(self, s, f, bills):
        n = s.get("count") or len(f)
        rows = []
        for x in f:
            who = " and ".join(self.name(w) for w in (x.get("who") or []) if w) or "two things"
            when = spoken(x.get("when") or "")
            word = "You may be double-booked" if "maybe" in str(x.get("word") or "").lower() else \
                "You're double-booked"
            sides = self._sides(x.get("line"))
            say = f"{word} {when}. {sides}" if sides else f"{word} {when}: {who}"
            rows.append((say, x.get("ref"), cap_words(f"{who}, {when}", LABEL_WORDS)))
        return f"{words(n)} {'thing needs' if n == 1 else 'things need'} you to choose", rows, n

    def _sides(self, line):
        """The two sides of a clash for listening ("Sam's move to the new place, or the pier pickups with Lena"),
        from the screen line "Sam: Sam's move to the new place · Lena: the pier pickups"; "" when it doesn't parse."""
        out = []
        for part in str(line or "").split(" · "):
            who, sep, what = part.partition(": ")
            if not sep or not what.strip():
                return ""
            name, what = self.name(who.strip()), self._words(what.strip())
            first = name.split()[0] if name else ""
            out.append(what if not first or first.lower() in what.lower() else f"{what} with {name}")
        return ", or ".join(out) if len(out) == 2 else ""

    def _happening(self, s, f, bills):
        n = s.get("count") or len(f)
        rows = []
        for x in f:
            what = self._words(x.get("what"))
            when = spoken(" ".join(y for y in (x.get("day"), x.get("when")) if y))
            rows.append((f"{what}, {when}" if when else what, x.get("ref"), cap_words(what, LABEL_WORDS)))
        return f"{words(n)} {'thing is' if n == 1 else 'things are'} happening, nothing to decide", rows, n

    def _needs(self, s, f, bills):
        n = s.get("count") or len(f)
        rows = []
        for x in f:
            who = self.name(x.get("who")) or "Someone"
            why = self._words(x.get("why"), x.get("ref") in bills)
            wait = spoken(x.get("waiting") or "")
            line = f"{who} has waited {wait} on {why}" if wait and why else f"{who}: {why}" if why else who
            if x.get("never_hide") and not x.get("why"):
                line = f"A security alert from {who}"
            rows.append((line, x.get("ref"), cap_words(f"{who}, {why}" if why else who, LABEL_WORDS)))
        return f"{words(n)} {'thing needs' if n == 1 else 'things need'} you", rows, n

    def _coming(self, s, f, bills):
        n = s.get("count") or len(f)
        rows = []
        for x in f:
            what = self._words(x.get("what"), x.get("ref") in bills)
            rel = spoken(str(x.get("when") or "").split(",")[0])
            rows.append((f"{rel}, {what}" if rel else what, x.get("ref"), cap_words(what, LABEL_WORDS)))
        return f"{words(n)} {'thing is' if n == 1 else 'things are'} coming up in the next thirty days", rows, n

    def _money(self, s, f, bills):
        rows, nb = [], 0
        for x in f:
            if "due" in x:
                nb += 1
                who = self.name(x.get("who")) or self._label(x.get("what"))
                amt = spoken(x["amount"], exact_money=True) if x.get("amount") else ""
                when = spoken(x.get("late") or ("due " + x["due"] if x.get("due") else ""))
                line = f"{who}, {amt}, {when}" if amt else f"{self._words(x.get('what'))}, {when}"
                rows.append((line.strip(" ,"), x.get("ref"), cap_words(f"{who} bill", LABEL_WORDS)))
            else:
                when = spoken(str(x.get("when") or "").split("–")[0].split(" to ")[0])
                what = self._words(x.get("what"))
                rows.append((f"{what}, {when}", x.get("ref"), cap_words(what, LABEL_WORDS)))
        nt = len(f) - nb
        bits = ([f"{words(nb)} {'bill' if nb == 1 else 'bills'} to pay"] if nb else []) + \
            ([f"{'a trip' if nt == 1 else words(nt) + ' trips'} coming up"] if nt else [])
        total = len(rows) + max(0, len([r for r in s.get("refs") or []]) - len(f))
        return " and ".join(bits) or "Bills and trips", rows, total

    def _mail(self, s, f, bills):
        rows = []
        for x in f:
            who = self.name(x.get("who")) or "someone"
            if "what" in x:
                what = self._words(x["what"], x.get("ref") in bills)
                rows.append((f"From {who}, {what}", x.get("ref"), cap_words(f"{who}, {what}", LABEL_WORDS)))
            else:
                rows.append((f"A message from {who}, open it when you're ready", x.get("ref"), f"Message from {who}"))
        return "From your mail", rows, s.get("count") or len(f)

    def _feeds(self, s, f, bills):
        rows = []
        for x in f:
            what = self._label(x.get("what"))
            src = self._label(x.get("source"))
            rows.append((f"{what}, from {src}" if src else what, None, what))
        return "From your channels, feeds and reports", rows, s.get("count") or len(f)

    def _cleanup(self, s, f, bills):
        tot = next((x["total"] for x in f if "total" in x), 0) or s.get("count") or 0
        kinds = [_lower(spoken(x["kind"])) for x in f if "kind" in x and x.get("count") and x["kind"] != "Other"]
        intro = f"{words(tot)} {'thing' if tot == 1 else 'things'} to clean up"
        if kinds:
            k = kinds[:SAY_MAX]
            intro += ", mostly " + (k[0] if len(k) == 1 else ", ".join(k[:-1]) + " and " + k[-1])
        return intro + ". Clear them in the app when you have a minute", [], 0


def today_iso(now, tz):
    return dt.datetime.fromtimestamp(now, tz).date().isoformat()
