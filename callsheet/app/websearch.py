"""0.14.0 web search: Chat searches the web through the owner's own SearXNG (owner approved 2026-11-06, CLAUDE.md
"Chat may search the web through the owner's SearXNG"). Isaac: "I don't want to have to press any extra buttons. It
should know when to search, or ask."

What is here (chat.py calls it; it is the only code that calls net.Searx and Net.get_page):
- Settings (Setup → AI → Web search): the SearXNG address, On/Off, the date it was added (the approval record, like
  `outbound_hosts`), the last Check. Off, or no address: Chat isn't offered the tools.
- The query check (deterministic, before anything leaves): `check_query`. Every word of the query must come from the
  owner's own messages in this chat (NFKC, zero-width characters out, case-folded, light stemming of plurals, -ing and
  -ed), from STOP, or from the owner's "Your words" glossary. Anything that looks private asks first even when the
  owner typed it: email addresses, web addresses, phone numbers, amounts, account/order/invoice numbers, long digit
  runs, words mixing alphabets, and the names of the owner's contacts (people.py). A query that fails is never sent:
  Chat asks "Search the web for '<query>'?" with the exact string, and only the owner's yes in the next message (a
  one-time token bound to the chat, the turn and that string; chat.py) sends it.
- One owner turn (`Turn`): at most MAX_SEARCHES searches and MAX_READS page reads, MAX_RESULTS results each, plus the
  daily caps (DAY_SEARCHES, DAY_READS). Results get ids (w1, w2, …); `read_result` takes only such an id, never an
  address, and reads the result's own URL through net.py's rules (Net.get_page). Page text: HTML → visible text
  (htmltext.py), PAGE_CHARS at most, inject-checked (inject.instructions plus STEER: "search for …", "open result
  4", "visit …"); after a page with planted instructions, no more pages are read in that turn.
Nothing here logs a query or a page: chat.py logs counts only.
"""
import datetime as dt
import re
import time
import unicodedata
import urllib.parse

import htmltext
import inject
import net
import people as people_mod
from store import InputError

KEY = "web_search"            # callsheet.db meta: the settings
USAGE = "web_search_usage"    # callsheet.db meta: today's counts (the daily caps)
GLOSSARY_KEY = "your_words"   # "Your words" (the owner's glossary), when Callsheet has one: [{term, meaning}] or text
MAX_SEARCHES = 5              # per owner turn
MAX_READS = 5                 # per owner turn
MAX_RESULTS = 10
DAY_SEARCHES = 100            # the daily caps' defaults (Setup › AI › Web search › Advanced; DAY_LIMITS)
DAY_READS = 100
DAY_LIMITS = (1, 1000)
PAGE_CHARS = 6000
QUERY_CHARS = 200
QUERY_WORDS = 16
TITLE_CHARS = 200
SNIPPET_CHARS = 400
ASK = "Search the web for '{q}'?"
BUSY = "Web search is busy, try again in a few minutes."
BACKOFF_S = 5 * 60           # after a 429, or no results with engines unresponsive (a CAPTCHA): no search for this long
EXAMPLE = "http://192.168.10.157:30053"   # the placeholder in Setup (the owner's SearXNG on the NAS)
WEB_TOOL = "web_search"
READ_TOOL = "read_result"
TOOLS = (WEB_TOOL, READ_TOOL)

WHY = {
    "words": "It has words you didn't use in this chat.",
    "email": "It has an email address in it.",
    "url": "It has a web address in it.",
    "phone": "It has a phone number in it.",
    "amount": "It has an amount of money in it.",
    "account": "It has an account, order or invoice number in it.",
    "digits": "It has a long number in it.",
    "contact": "It has the name of someone you know in it.",
    "script": "It mixes letters from different alphabets.",
}


# --- settings -----------------------------------------------------------------------------------------------------
def load(store):
    saved = store._get_meta(KEY, {})  # noqa: SLF001 (web search's own meta key)
    saved = saved if isinstance(saved, dict) else {}
    s = {"base_url": "", "on": False, "added": None, "checked": None, "check_ok": None,
         "day_searches": DAY_SEARCHES, "day_reads": DAY_READS}
    s.update({k: v for k, v in saved.items() if k in s})
    for k, d in (("day_searches", DAY_SEARCHES), ("day_reads", DAY_READS)):
        v = s[k]
        s[k] = v if isinstance(v, int) and not isinstance(v, bool) and DAY_LIMITS[0] <= v <= DAY_LIMITS[1] else d
    if not isinstance(s["base_url"], str):
        s["base_url"] = ""
    s["on"] = bool(s["on"] and s["base_url"])
    return s


def configured(store):
    """The SearXNG address when web search is on, else "" (then Chat isn't offered the tools)."""
    s = load(store)
    return s["base_url"] if s["on"] and s["base_url"] else ""


def usage(store, today):
    u = store._get_meta(USAGE, {})  # noqa: SLF001
    u = u if isinstance(u, dict) and u.get("day") == today else {"day": today, "searches": 0, "reads": 0}
    return {"day": today, "searches": int(u.get("searches") or 0), "reads": int(u.get("reads") or 0)}


def public(store, today):
    s = load(store)
    u = usage(store, today)
    return dict(s, configured=bool(s["on"] and s["base_url"]), today={"searches": u["searches"], "reads": u["reads"]},
                caps={"searches": MAX_SEARCHES, "reads": MAX_READS, "results": MAX_RESULTS,
                      "day_searches": s["day_searches"], "day_reads": s["day_reads"], "day_limits": list(DAY_LIMITS)})


def update(store, patch, today):
    """{base_url, on}: a new address is stored with today's date (the approval record); "" removes it (and turns
    web search off). On needs an address."""
    if not isinstance(patch, dict):
        raise InputError("Send the web search settings as a JSON object.")
    with store.lock:
        s = load(store)
        for k, v in patch.items():
            if k == "base_url":
                if v in (None, ""):
                    s.update(base_url="", on=False, added=None, checked=None, check_ok=None)
                    continue
                if not isinstance(v, str):
                    raise InputError("The SearXNG address must be text.")
                try:
                    b = net.searx_base(v)
                except ValueError as e:
                    raise InputError(str(e)) from None
                if b != s["base_url"]:
                    s.update(base_url=b, added=today, checked=None, check_ok=None)
            elif k == "on":
                if not isinstance(v, bool):
                    raise InputError("on must be true or false.")
                s["on"] = v
            elif k in ("day_searches", "day_reads"):   # 0.14.0: the daily caps (Advanced)
                if not isinstance(v, int) or isinstance(v, bool) or not DAY_LIMITS[0] <= v <= DAY_LIMITS[1]:
                    raise InputError(f"The daily limit must be a whole number from {DAY_LIMITS[0]} to {DAY_LIMITS[1]:,}.")
                s[k] = v
            else:
                raise InputError(f"The web search settings can't change {str(k)[:40]}.")
        if s["on"] and not s["base_url"]:
            raise InputError("Add your SearXNG address first.")
        store._set_meta(KEY, s)  # noqa: SLF001
    return public(store, today)


def run_check(store, searx, today):
    """The owner's Check: one GET <base>/search?q=test&format=json. {ok, text, results}."""
    s = load(store)
    if not s["base_url"]:
        raise InputError("Add your SearXNG address first.")
    try:
        data = searx.search(s["base_url"], "test")
        n = len(results(data))
        out = {"ok": True, "results": n,
               "text": f"SearXNG answered with {n} result{'' if n == 1 else 's'}." if n else
               "SearXNG answered, with no results for “test”. " + ("Its search engines are busy: try again in a few "
                                                                  "minutes." if unresponsive(data) else
                                                                  "Check its search engines.")}
    except net.RateLimited:
        out = {"ok": False, "results": 0, "text": BUSY}
    except net.NetError as e:
        out = {"ok": False, "results": 0, "text": str(e)}
    with store.lock:
        cur = load(store)
        if cur["base_url"] == s["base_url"]:
            cur.update(checked=today, check_ok=out["ok"])
            store._set_meta(KEY, cur)  # noqa: SLF001
    return out


def glossary(store):
    """The owner's "Your words" (terms and meanings) as text, when Callsheet has them (empty until that ships)."""
    g = store._get_meta(GLOSSARY_KEY, [])  # noqa: SLF001
    out = []
    for e in g if isinstance(g, list) else []:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict):
            out += [str(e.get(k) or "") for k in ("term", "meaning")]
    return [x[:300] for x in out if x][:500]


def contact_names(people):
    """The full names of the owner's contacts (people.py), each a tuple of folded words."""
    out = set()
    if people is None:
        return out
    try:
        d = people.directory()
    except Exception:  # noqa: BLE001 (no people yet: the private patterns still apply)
        return out
    for p in d.persons.values():
        for n in [p.get("name")] + list(p.get("aliases") or []) + list(p.get("names") or []):
            f = people_mod.full_name(n)
            if f:
                out.add(tuple(words(f)))
    return out


# --- the query check --------------------------------------------------------------------------------------------
_HIDDEN = re.compile("[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮"
                     "⁠-⁤⁦-⁯ㅤ︀-️﻿ﾠ\U000e0000-\U000e007f]")
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
STOP = frozenset("""a an the and or but if then so to of in on at by for with from as into onto about over under than
that this these those it its it's is are was were be been being am do does did done doing have has had having i me my
we us our you your he him his she her they them their there here who whom which what when where why how all any each
every some no not nor only own same too very can could may might must shall should will would just now also please s
vs versus via per near between after before during while""".split())


def clean_query(q):
    """The query as it will be sent and shown: NFKC, hidden characters out, one line, clipped."""
    q = unicodedata.normalize("NFKC", str(q or ""))
    q = _HIDDEN.sub("", q)
    q = re.sub(r"[\x00-\x1f\x7f]", " ", q)
    q = " ".join(q.split())
    return q[:QUERY_CHARS].strip()


def fold(text):
    return clean_query_text(text).casefold().replace("’", "'").replace("‘", "'")


def clean_query_text(text):
    t = unicodedata.normalize("NFKC", str(text or ""))
    return _HIDDEN.sub("", t)


def words(text):
    return _WORD.findall(fold(text))


def stem(w):
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            w = w[: -len(suf)]
            if suf in ("ing", "ed") and len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
                w = w[:-1]   # running -> run, planned -> plan
            break
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]   # hike, hiking -> hik
    return w


def vocabulary(texts):
    """The folded words and their stems of the owner's texts."""
    out = set()
    for t in texts:
        for w in words(t):
            out.add(w)
            out.add(stem(w))
    return out


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\b[\w.+-]+\s*(?:\(at\)|\[at\]|\sat\s)\s*[\w-]+\s*(?:\(dot\)|\[dot\]|\.)\s*[a-z]{2,}\b", re.I)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:[a-z]{2,24})(?:/|\b)(?!['\w])", re.I)
_PHONE = re.compile(r"(?<![\w])\+?\(?\d[\d\s().-]{5,}\d(?![\w])")
_CODES = r"(?:usd|eur|gbp|chf|jpy|cad|aud|nzd|sek|nok|dkk|pln|inr|cny|rmb|btc|eth)"
_CURRENCY = re.compile(r"[$€£¥₹₽¢₩₪₫₱฿₴₦]|\b" + _CODES + r"\s*\d|\d\s*" + _CODES + r"\b|"
                       r"\d\s*(?:dollars?|euros?|pounds?|bucks|quid|grand|cents?)\b", re.I)
_ACCOUNT = re.compile(r"\b(?:order|account|acct|invoice|inv|receipt|ref|reference|booking|confirmation|ticket|case|"
                      r"policy|iban|swift|bic|routing|sort\s+code|card|member(?:ship)?|customer|tracking|parcel|"
                      r"shipment|po|pnr|claim|contract|serial|license|licence|passport|ssn|tax|vat|bsn)\b"
                      r"\s*(?:#|no\.?|nr\.?|number|id|code)?\s*:?\s*#?(?!(?:19|20)\d\d\b)[a-z0-9-]*\d", re.I)
_MIXED_ID = re.compile(r"(?<![\w])(?=[\w-]*\d)(?=[\w-]*[^\W\d_])[\w-]*?(?:\d[\w-]*){4,}(?![\w])|#\s*\d{3,}")
_DIGITS = re.compile(r"\d{5,}|(?:\d[\s.,/-]?){7,}")


def _scripts(w):
    out = set()
    for c in w:
        if c.isalpha():
            try:
                out.add(unicodedata.name(c).split()[0])
            except ValueError:
                out.add("?")
    return out


def private(query, contacts=()):
    """The kind of private-looking thing in the query ("" when none): email, url, phone, amount, account, digits,
    contact, script."""
    q = clean_query_text(query)
    if _EMAIL.search(q) or "@" in q:
        return "email"
    if _URL.search(q):
        return "url"
    if _CURRENCY.search(q):
        return "amount"
    if _ACCOUNT.search(q) or _MIXED_ID.search(q):
        return "account"
    if _PHONE.search(q) and len(re.sub(r"\D", "", _PHONE.search(q).group(0))) >= 7:
        return "phone"
    if _DIGITS.search(q):
        return "digits"
    ws = words(q)
    have = set(ws) | {stem(w) for w in ws}
    for name in contacts:
        if name and all(w in have for w in name):
            return "contact"
    if any(len(_scripts(w)) > 1 for w in ws):
        return "script"
    return ""


def check_query(query, owner_texts, glossary_texts=(), contacts=(), approved=()):
    """{ok, query (as it will be sent), why (a WHY key or None), words (the words that aren't the owner's)}. ok only
    when nothing looks private and every word is the owner's (or STOP, or "Your words"); or when the owner said yes to
    exactly this query (approved: folded queries)."""
    q = clean_query(query)
    out = {"ok": False, "query": q, "why": None, "words": []}
    if not q:
        return out
    if fold(q) in {fold(a) for a in approved if a}:
        out["ok"] = True
        return out
    why = private(q, contacts)
    vocab = vocabulary(list(owner_texts) + list(glossary_texts))
    stray = [w for w in words(q) if w not in STOP and w not in vocab and stem(w) not in vocab]
    out["words"] = stray[:10]
    if why:
        out["why"] = why
    elif stray:
        out["why"] = "words"
    elif len(words(q)) > QUERY_WORDS:
        out["why"] = "words"
    else:
        out["ok"] = True
    return out


_YES_FIRST = frozenset("yes yeah yep yup y sure ok okay go please do search fine alright absolutely sounds of yea "
                       "aye correct right".split())
_YES_ALL = _YES_FIRST | frozenset("ahead it for that thanks thank you go good course please search sure the web "
                                  "one this just".split())


def yes_reply(text):
    """Is the owner's message a plain yes ("yes", "Yes please", "ok go ahead", "sure, search it")?"""
    ws = words(text)
    return bool(ws) and len(ws) <= 6 and ws[0] in _YES_FIRST and all(w in _YES_ALL for w in ws) \
        and not re.search(r"\b(?:no|not|don't|dont|never|stop|wait)\b", fold(text))


# --- results and pages ------------------------------------------------------------------------------------------
def _txt(v, n):
    t = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or ""))
    t = " ".join(_HIDDEN.sub("", t).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def host_of(url):
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
    return h


def unresponsive(data):
    """The engines SearXNG says didn't answer (CAPTCHA, too many requests, timeouts): [(engine, reason)]."""
    out = []
    for e in (data.get("unresponsive_engines") if isinstance(data, dict) else None) or []:
        if isinstance(e, (list, tuple)) and e:
            out.append((_txt(e[0], 40), _txt(e[1] if len(e) > 1 else "", 80)))
        elif isinstance(e, str):
            out.append((_txt(e, 40), ""))
    return out[:20]


def results(data):
    """[{title, snippet, url, host}] of a SearXNG JSON answer: http(s) results only, MAX_RESULTS at most."""
    out = []
    for r in (data.get("results") if isinstance(data, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if len(url) > net.MAX_URL or not re.match(r"^https?://", url, re.I) or re.search(r"[\x00-\x20\x7f]", url):
            continue
        host = host_of(url)
        if not host:
            continue
        out.append({"title": _txt(r.get("title"), TITLE_CHARS) or host, "snippet": _txt(r.get("content"), SNIPPET_CHARS),
                    "url": url, "host": host})
        if len(out) >= MAX_RESULTS:
            break
    return out


STEER = re.compile(
    r"\b(?:search(?:\s+the\s+web)?|google|look\s+up|query|bing)\s+(?:for\b|now\b|again\b|[\"'“‘])"
    r"|\b(?:now|then|next|also|first)\s*,?\s+(?:search|google|look\s+up)\b"
    r"|\b(?:open|read|fetch|visit|load|click|follow|browse)\s+(?:the\s+)?(?:(?:next|first|second|third|fourth|fifth|"
    r"last|other|\d+(?:st|nd|rd|th)?)\s+)?(?:result|link|page|url|site)s?\b"
    r"|\b(?:open|read|fetch|visit|load)\s+(?:result|link)\s*#?\s*\d+"
    r"|\b(?:visit|open|fetch|load|go\s+to|navigate\s+to|browse\s+to)\s+(?:https?://|www\.|[a-z0-9-]+\.[a-z]{2,}/)"
    r"|(?:^|[,:;]\s*)(?:google|bing)\s+\w"
    r"|\b(?:must|should|please|need\s+to)\s+(?:google|search|look\s+up|query|visit|open|fetch)\b",
    re.I)


def instructions(text):
    """The sentences of web text (a snippet or a page) that steer an assistant: inject.instructions plus STEER."""
    out = list(inject.instructions(text))
    for s in inject.sentences(text):
        if STEER.search(s) and s not in out:
            out.append(s)
    return out


def _charset(ctype_header):
    m = re.search(r"charset\s*=\s*\"?([\w.:-]{1,40})", ctype_header or "", re.I)
    return m.group(1) if m else "utf-8"


def page_text(answer):
    """(text, hidden_text, cut) of a fetched page: HTML as the visible text, plain text as is, PAGE_CHARS at most."""
    ctype = answer.ctype
    if ctype not in ("text/html", "application/xhtml+xml", "text/plain", ""):
        raise net.NetError("That result isn't a web page Callsheet reads.", 200, "type")
    try:
        raw = answer.body.decode(_charset(answer.headers.get("content-type")), errors="replace")
    except LookupError:
        raw = answer.body.decode("utf-8", errors="replace")
    if ctype == "text/plain":
        text, hidden = raw, False
    else:
        text, hidden = htmltext.html_text(raw)
    text = _HIDDEN.sub("", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*(?:\n\s*)+", "\n\n", text).strip()
    cut = len(text) > PAGE_CHARS or answer.truncated
    if len(text) > PAGE_CHARS:
        text = text[: PAGE_CHARS - 1].rstrip() + "…"
    return text, hidden, cut


# --- one owner turn ---------------------------------------------------------------------------------------------
class Web:
    """Chat's web search. api.App makes one with the connectors' Net (page reads) and a Searx (the owner's SearXNG)."""

    def __init__(self, store, net_=None, searx=None, clock=time.time):
        self.store, self.clock = store, clock
        self.net = net_ or net.Net(clock=clock)
        self.searx = searx or net.Searx()
        self.busy_until = 0.0     # after a 429 or a CAPTCHA'd answer (BACKOFF_S): searches wait, nothing is sent

    def busy(self):
        return self.clock() < self.busy_until

    def day_cap(self, kind):
        s = load(self.store)
        return s["day_searches"] if kind == "searches" else s["day_reads"]

    def back_off(self):
        self.busy_until = self.clock() + BACKOFF_S

    def today(self):
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date().isoformat()

    def take(self, kind):
        """Counts one search or page read against today's cap; False when the cap is reached."""
        cap = self.day_cap(kind)
        with self.store.lock:
            u = usage(self.store, self.today())
            if u[kind] >= cap:
                return False
            u[kind] += 1
            self.store._set_meta(USAGE, u)  # noqa: SLF001
        return True

    def turn(self, owner_texts, people=None, approved=None):
        """A Turn when web search is on, else None (the tools aren't offered)."""
        base = configured(self.store)
        if not base:
            return None
        return Turn(self, base, owner_texts, glossary(self.store), contact_names(people), approved)


class Turn:
    """The web tools of one owner turn. .asked: {query, why} when a query needs the owner's yes (the turn then ends
    with the question); .queries: what was searched; .results: id -> result; .flagged: planted sentences seen."""

    def __init__(self, web, base, owner_texts, glossary_texts=(), contacts=(), approved=None):
        self.web, self.base = web, base
        self.owner_texts = [str(t or "") for t in owner_texts]
        self.glossary = list(glossary_texts)
        self.contacts = set(contacts)
        self.approved = clean_query(approved) if approved else ""
        self.searches = self.reads = 0
        self.asked = None
        self.queries, self.read_ids, self.flagged = [], [], []
        self.results = {}
        self.refused_urls = 0
        self.blocked = False      # a page with planted instructions was read: no more pages this turn
        self.busy = False         # SearXNG was busy (429, CAPTCHA): no more searches this turn
        self.ignored = 0          # calls after the owner was asked, over a cap, or with an address

    def check(self, query):
        return check_query(query, self.owner_texts, self.glossary, self.contacts,
                           [self.approved] if self.approved else ())

    def search(self, args):
        if set(args) - {"query"} or not isinstance(args.get("query"), str):
            return {"error": "web_search takes one argument: query (text)."}, None
        if self.asked:
            self.ignored += 1
            return {"error": "Callsheet is asking the owner first. Don't search again or answer; wait for them."}, None
        if self.searches >= MAX_SEARCHES:
            self.ignored += 1
            return {"error": f"At most {MAX_SEARCHES} web searches per question. Answer from what you have."}, None
        c = self.check(args["query"])
        if not c["query"]:
            return {"error": "The query is empty."}, None
        if not c["ok"]:
            self.asked = {"query": c["query"], "why": c["why"]}
            return {"asked": True, "note": "Nothing was searched. Callsheet now asks the owner whether to search for "
                                           "exactly this; stop here."}, None
        if self.busy or self.web.busy():   # backing off: nothing is sent, no retry in this turn
            self.busy = True
            self.ignored += 1
            return {"error": BUSY + " Tell the owner that, and answer without the web."}, None
        if not self.web.take("searches"):
            self.ignored += 1
            return {"error": f"Callsheet has done its {self.web.day_cap('searches')} web searches for today. Answer without the web."}, None
        self.searches += 1
        self.queries.append(c["query"])
        label = "Searched the web for '" + c["query"] + "'"
        try:
            data = self.web.searx.search(self.base, c["query"])
        except net.RateLimited:
            self.busy = True
            self.web.back_off()
            return {"error": BUSY + " Tell the owner that, and answer without the web."}, label
        except net.NetError as e:
            return {"error": str(e)}, label
        found = results(data)
        if not found and unresponsive(data):   # e.g. a CAPTCHA at every engine that would have answered
            self.busy = True
            self.web.back_off()
            return {"error": BUSY + " Tell the owner that, and answer without the web."}, label
        out = []
        for r in found:
            rid = f"w{len(self.results) + 1}"
            flagged = instructions(r["title"] + ".\n" + r["snippet"])
            self.results[rid] = dict(r, id=rid, instructions=bool(flagged))
            self.flagged += flagged
            row = {"id": rid, "title": r["title"], "snippet": r["snippet"], "url": r["url"], "host": r["host"]}
            if flagged:
                row["instructions"] = True
            out.append(row)
        return {"query": c["query"], "results": out} if out else {"query": c["query"], "results": [],
                                                                  "note": "No results."}, label

    def read(self, args):
        if set(args) - {"id"}:
            return {"error": "read_result takes one argument: id (a result id like w3)."}, None
        rid = args.get("id")
        if isinstance(rid, bool) or not isinstance(rid, (str, int)):
            return {"error": "read_result takes a result id like w3."}, None
        rid = str(rid).strip().lower()
        if not re.fullmatch(r"w?\d{1,3}", rid):
            self.refused_urls += 1
            return {"error": "read_result takes only a result id like w3 from this question's web_search results, "
                             "never an address."}, None
        rid = rid if rid.startswith("w") else "w" + rid
        r = self.results.get(rid)
        if r is None:
            return {"error": f"There's no result {rid} in this question's searches."}, None
        if self.asked:
            self.ignored += 1
            return {"error": "Callsheet is asking the owner first. Wait for them."}, None
        if self.reads >= MAX_READS:
            self.ignored += 1
            return {"error": f"At most {MAX_READS} pages per question. Answer from what you have."}, None
        if self.blocked:
            self.ignored += 1
            return {"error": "A page in this question had instructions in it, so Callsheet reads no more pages for "
                             "this question."}, None
        if r["instructions"]:
            self.ignored += 1
            return {"error": "That result's text reads like instructions, so Callsheet doesn't open it."}, None
        if not self.web.take("reads"):
            self.ignored += 1
            return {"error": f"Callsheet has read its {self.web.day_cap('reads')} web pages for today."}, None
        self.reads += 1
        self.read_ids.append(rid)
        label = "Read " + r["host"]
        try:
            ans = self.web.net.get_page(r["url"])
            text, hidden, cut = page_text(ans)
        except net.Moved as e:
            return {"error": f"That page moved to another site ({e.host}); Callsheet doesn't follow it."}, label
        except net.NetError as e:
            return {"error": str(e)}, label
        flagged = instructions(text)
        if flagged:
            self.blocked = True
            self.flagged += flagged
            r["instructions"] = True
        out = {"id": rid, "title": r["title"], "host": r["host"], "text": text}
        if flagged:
            out["instructions"] = True
        if hidden:
            out["hidden_text"] = True
        if cut:
            out["cut"] = True
        return out, label

    def chip(self, rid):
        r = self.results.get(rid)
        if r is None:
            return None
        c = {"id": rid, "kind": "web", "title": r["title"], "host": r["host"], "url": r["url"]}
        if r["instructions"]:
            c["instructions"] = True
        return c


_WCITE = re.compile(r"\s?\[\s*w\d{1,3}(?:\s*[,;]\s*w\d{1,3})*\s*\]", re.I)


def cite(raw, turn, n=8):
    """(text without the [w3] markers, chips of the cited web results in order; when none is cited, the pages read)."""
    order = []

    def repl(m):
        for x in re.findall(r"w\d{1,3}", m.group(0), re.I):
            x = x.lower()
            if turn is not None and x in turn.results and x not in order:
                order.append(x)
        return ""
    text = _WCITE.sub(repl, raw or "")
    if turn is None:
        return text, []
    if not order:
        order = list(turn.read_ids)
    return text, [c for c in (turn.chip(x) for x in order) if c][:n]
