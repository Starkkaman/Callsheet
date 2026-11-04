"""Callsheet 0.8.2 People: one person across email, Slack, Signal and Drive (docs/people.md).

Tables in callsheet.db (0600, backed up; additive: CREATE TABLE IF NOT EXISTS):
- people: id, display name, aliases, state (suggested | confirmed | me | merged), VIP flag, area, merged_into.
- person_handles: one identifier each (email: mail, calendar, Drive owners and VIPs; slack: user id; signal: the
  store's salted sender id, never a number), UNIQUE (kind, value), with a label, where it came from and a count.
- merge_proposals: "Same person?" pairs with their signals and the owner's answer (open | merged | different | later).
- people_audit: every merge, split and "different" answer (the History list; Split undoes a merge).

Rules (design 4 and 6.1):
- Signals only propose. Nothing merges without the owner pressing Same person.
- The owner ("me", id "me") gets identifiers only from connected accounts: the Google accounts, Gmail send-as
  addresses read with the owner's button, each Slack workspace's own user id, and the linked Signal account. Message
  content (names, signatures, "I'm Isaac's other email") never adds to "me", and no proposal or merge involves "me".
- Role and automated addresses and first-name-only matches never propose; neither do names the owner uses.
"""
import collections
import hashlib
import json
import re
import threading
import time
import unicodedata

import memory
import rules

KINDS = ("email", "slack", "signal")
STATES = ("suggested", "confirmed", "me", "merged")
ME = "me"
NAME_MAX = 80
ALIAS_MAX = 12
MAX_PEOPLE = 3000          # people a seed may create in all
SEED_DAYS = 365            # contacts from the last year of the index
SIG_DAYS = 180             # signatures read from the last 6 months of mail ...
SIG_MAX = 2000             # ... at most this many messages
SIG_LINES = 12
LATER_S = 7 * 86400
SEED_EVERY_S = 6 * 3600    # GET api/people refreshes the people when the last seed is older than this
OPEN_SCORE = 0.8
PAIR_SCORE = 0.5
MAX_OPEN = 50
AUDIT_SHOW = 50
LIST_MAX = 300

WEIGHTS = {"slack_email": 1.0, "email_same": 1.0, "signature": 0.9, "name_domain": 0.7, "name": 0.5,
           "slack_name": 0.5, "signal_name": 0.5, "unique_name": 0.3}
# a signature is text the sender wrote: when either person is a VIP or has a verified handle (the owner's VIP list,
# a connected account, Slack or Signal), it counts as medium ("Good") at most and the card says so
SIG_CAP = 0.6
VERIFIED = frozenset(("owner", "google", "slack", "signal"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'suggested',
  vip INTEGER NOT NULL DEFAULT 0, area TEXT NOT NULL DEFAULT '', merged_into TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS person_handles (
  person TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
  verified TEXT NOT NULL DEFAULT 'inferred', source TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0,
  extra TEXT NOT NULL DEFAULT '{}', added REAL NOT NULL, UNIQUE (kind, value));
CREATE INDEX IF NOT EXISTS person_handles_person ON person_handles (person);
CREATE TABLE IF NOT EXISTS merge_proposals (
  id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL, signals TEXT NOT NULL DEFAULT '[]', score REAL NOT NULL,
  by TEXT NOT NULL DEFAULT 'rules', state TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL, decided REAL,
  until REAL, UNIQUE (a, b));
CREATE TABLE IF NOT EXISTS people_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, action TEXT NOT NULL, a TEXT NOT NULL DEFAULT '',
  b TEXT NOT NULL DEFAULT '', names TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}', undone REAL);
"""

ROLE = re.compile(r"^(?:info|team|hello|hi|support|help|billing|accounts?|admin|office|sales|contact|enquir(?:y|ies)|"
                  r"inquir(?:y|ies)|hr|jobs|careers|press|media|marketing|news|newsletters?|orders?|bookings?|"
                  r"reception|service|customerservice|feedback|invoices?|payments?|finance|legal|security|it|"
                  r"webmaster|abuse|all|everyone|staff|studio|mail|post)$", re.I)
PUBLIC = memory.PUBLIC_DOMAINS | frozenset(("hotmail.co.uk", "yahoo.co.uk", "outlook.co.uk", "btinternet.com",
                                            "live.co.uk", "icloud.de", "t-online.de", "orange.fr", "free.fr"))
_SECOND = frozenset(("co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go"))
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QUOTE = re.compile(r"^\s*(?:>|on\b.{0,200}\bwrote:\s*$|-{2,}\s*original message|-{2,}\s*forwarded message|"
                    r"from:\s|sent:\s|begin forwarded message)", re.I)
_HEADERISH = re.compile(r"\b(?:wrote:|from:|to:|cc:|bcc:|sent:|reply-to:)", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


class PeopleError(ValueError):
    """A request the owner can fix (HTTP 400): the message is a plain sentence."""


class Gone(LookupError):
    """Unknown id (HTTP 404)."""


def clean(text, n=NAME_MAX):
    text = re.sub(r"\s+", " ", _CTRL.sub(" ", str(text if text is not None else ""))).strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def norm_name(s):
    """Folded name for matching: accents, case, quotes and dots off; "Nair, Priya" reads as "priya nair"."""
    s = unicodedata.normalize("NFKD", clean(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"[\"'’`.()\[\]<>]", " ", s)
    if s.count(",") == 1 and "@" not in s:
        last, first = s.split(",")
        s = first + " " + last
    return " ".join(re.sub(r"[,;:]", " ", s).casefold().split())


def full_name(s):
    """The folded name when it is a full name (two or more words of two letters or more), else ""."""
    n = norm_name(s)
    parts = n.split()
    if len(parts) < 2 or any(len(p) < 2 or not re.fullmatch(r"[a-z][a-z-]*", p) for p in parts):
        return ""
    return n


def canon_email(e):
    """The address as Gmail delivers it: case off, "+tag" off, Gmail's dots off, googlemail.com = gmail.com."""
    e = str(e or "").strip().lower()
    if "@" not in e:
        return e
    local, dom = e.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if dom == "googlemail.com":
        dom = "gmail.com"
    if dom == "gmail.com":
        local = local.replace(".", "")
    return local + "@" + dom


def domain_family(domain):
    """acme.com, acme.co.uk and mail.acme.com are one family ("acme"); public mail domains have none."""
    d = str(domain or "").lower().strip(".")
    if not d or d in PUBLIC:
        return ""
    parts = d.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


def is_role(addr):
    local = str(addr or "").split("@", 1)[0]
    return bool(ROLE.match(local) or rules.AUTOMATED_SENDER.search(local))


def _pid(seed):
    return "p" + hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:11]


def pair_id(a, b):
    a, b = sorted((a, b))
    return "mp" + hashlib.sha1((a + "|" + b).encode()).hexdigest()[:10]


def signature_addresses(body):
    """Addresses listed in a message's signature: the last lines before any quoted part, skipping header-like lines
    ("On … wrote:", "From:"). Returns lower-case addresses in order."""
    lines = []
    for line in str(body or "").replace("\r", "").split("\n"):
        if _QUOTE.match(line):
            break
        lines.append(line)
    tail = [x for x in lines if x.strip()][-SIG_LINES:]
    out = []
    for x in tail:
        if _HEADERISH.search(x):
            continue
        for m in _EMAIL.findall(x):
            m = m.lower().strip(".")
            if m not in out:
                out.append(m)
    return out


# --- signals (pure) ----------------------------------------------------------------------------------------------
def _names(p):
    """[(full name, origin)] of a person: its name and aliases (mail), Slack real names, Signal contact names."""
    out = []
    kinds = {h["kind"] for h in p.get("handles") or []}
    base = "mail" if "email" in kinds or not kinds else "slack" if "slack" in kinds else "signal"
    for n in [p.get("name")] + list(p.get("aliases") or []):
        f = full_name(n)
        if f:
            out.append((f, base))
    for h in p.get("handles") or []:
        ex = h.get("extra") or {}
        for key, origin in (("real_name", "slack"), ("contact", "signal")):
            f = full_name(ex.get(key))
            if f and h["kind"] == origin:
                out.append((f, origin))
    return out


def _emails(p):
    return [h["value"] for h in p.get("handles") or [] if h["kind"] == "email"]


def _shown(n):
    return " ".join(w.capitalize() if w.islower() else w for w in n.split())


def signals(a, b, owner_names=(), signatures=(), name_count=None):
    """[{code, weight, why}] for two people, strongest first. a, b: {id, name, aliases, handles: [{kind, value, extra}]}.
    owner_names: folded names the owner uses (they never count); signatures: {(from address, listed address)};
    name_count: {full name: number of people with it}. Every why is a short sentence built here."""
    out = []
    ea, eb = _emails(a), _emails(b)
    ca = {canon_email(e): e for e in ea}
    for e in eb:
        if canon_email(e) in ca and ca[canon_email(e)] != e:
            out.append(("email_same", f"{clean(ca[canon_email(e)])} and {clean(e)} are one address written two ways."))
            break
    for x, y in ((a, b), (b, a)):
        ex = set(_emails(y))
        for h in x.get("handles") or []:
            pe = str((h.get("extra") or {}).get("email") or "").lower()
            if h["kind"] == "slack" and pe and pe in ex:
                out.append(("slack_email", f"Their Slack profile email is {clean(pe)}."))
                break
    sig = set(signatures or ())
    sender = ""
    for x, y in ((ea, eb), (eb, ea)):
        hit = next(((f, t) for f in x for t in y if (f, t) in sig), None)
        if hit:
            sender = clean(hit[0])
            out.append(("signature", f"The signature in mail from {sender} lists {clean(hit[1])}."))
            break
    own = set(owner_names or ())
    na, nb = _names(a), _names(b)
    shared = [(n, oa, ob) for n, oa in na for m, ob in nb if n == m and n not in own]
    if shared:
        best = None
        for n, oa, ob in shared:
            if "signal" in (oa, ob):
                cand = ("signal_name", f"Their Signal contact name is {_shown(n)}.")
            elif "slack" in (oa, ob):
                cand = ("slack_name", f"Their Slack name is {_shown(n)}.")
            else:
                fa = {domain_family(e.rsplit("@", 1)[-1]) for e in ea} - {""}
                fb = {domain_family(e.rsplit("@", 1)[-1]) for e in eb} - {""}
                if fa & fb:
                    da = next(e.rsplit("@", 1)[1] for e in ea if domain_family(e.rsplit("@", 1)[1]) in fb)
                    db = next(e.rsplit("@", 1)[1] for e in eb if domain_family(e.rsplit("@", 1)[1]) in fa)
                    where = f"at {da}" if da == db else f"at {da} and {db}"
                    cand = ("name_domain", f"Same full name, {_shown(n)}, {where}.")
                else:
                    cand = ("name", f"Same full name, {_shown(n)}, at different addresses.")
            if best is None or WEIGHTS[cand[0]] > WEIGHTS[best[0]]:
                best = cand
        out.append(best)
        if name_count is not None and all(name_count.get(n, 0) <= 2 for n, _, _ in shared[:1]):
            out.append(("unique_name", "No one else you know has this name."))
    guarded = protected(a) or protected(b)
    vip = bool(a.get("vip") or b.get("vip"))
    seen, res = set(), []
    for code, why in out:
        if code not in seen:
            seen.add(code)
            s = {"code": code, "weight": WEIGHTS[code], "why": why}
            if code == "signature":
                s["sender"] = sender
                if guarded:  # medium at most: the sender wrote it
                    s.update(weight=SIG_CAP, capped=True)
            if code == "unique_name" and vip:  # never opens a card into a VIP
                s["no_open"] = True
            res.append(s)
    res.sort(key=lambda s: -s["weight"])
    return res


def protected(p):
    """A VIP, or someone with a handle the owner or an account vouches for (not only seen in mail)."""
    return bool(p.get("vip")) or any(h.get("verified") in VERIFIED for h in p.get("handles") or [])


def score(sigs):
    left = 1.0
    for s in sigs:
        left *= 1 - s["weight"]
    return round(1 - left, 3)


def opens(sigs):
    """A proposal opens at a signal of 0.8 or more, or a combined 0.5 or more from two independent signals. A capped
    signature still opens its card (shown as medium, with the caution line); "no one else has this name" never
    counts for a VIP."""
    ev = [s for s in sigs or () if not s.get("no_open")]
    if not ev:
        return False
    return max(WEIGHTS[s["code"]] if s.get("capped") else s["weight"] for s in ev) >= OPEN_SCORE or \
        (len(ev) >= 2 and score(ev) >= PAIR_SCORE)


def caution(sigs):
    """The card's warning when a capped signature is the only evidence."""
    if len(sigs or ()) == 1 and sigs[0].get("code") == "signature" and sigs[0].get("capped"):
        return f"Only evidence: a signature in a message from {sigs[0].get('sender') or 'someone'}; check before merging."
    return ""


def strength(w):
    return "Strong" if w >= 0.9 else "Good" if w >= 0.6 else "Some"


# --- VIPs ------------------------------------------------------------------------------------------------------
class VipMap(dict):
    """{lower-case email: name} as rules.reply_row expects, plus the Slack user ids (`slack`) and Signal sender ids
    (`signal`) of VIP people, so a VIP is a person: every handle counts."""
    slack = frozenset()
    signal = frozenset()


def vip_map(store, people=None):
    v = VipMap({x["email"].lower(): x["name"] for x in store.vips()})
    if people is None:
        return v
    d = people.directory()
    slack, signal = set(), set()
    for pid, p in d.persons.items():
        if p["state"] == ME or not (p["vip"] or any(e in v for e in p["email"])):
            continue
        for e in p["email"]:
            v.setdefault(e, p["name"])
        slack |= p["slack"]
        signal |= p["signal"]
    v.slack, v.signal = frozenset(slack), frozenset(signal)
    return v


class Directory:
    """A read-only snapshot for lookups during a sync or a page view."""

    def __init__(self, persons, by_handle):
        self.persons, self.by_handle = persons, by_handle
        self.by_name = collections.defaultdict(set)
        for pid, p in persons.items():
            if p["state"] == ME:
                continue
            for n in [p["name"]] + p["aliases"]:
                if norm_name(n):
                    self.by_name[norm_name(n)].add(pid)

    def person_of(self, kind, value):
        pid = self.by_handle.get((kind, str(value or "").lower()))
        return self.persons.get(pid) if pid else None

    def note_people(self, emails=(), names=()):
        """Person ids a memory note is about: by any of its addresses, or a name or alias that only one person has
        (a one-word name only when it is someone's whole alias or the first name of exactly one person)."""
        ids = set()
        for e in emails or ():
            p = self.person_of("email", e)
            if p and p["state"] != ME:
                ids.add(p["id"])
        for n in names or ():
            k = norm_name(n)
            if not k:
                continue
            hit = self.by_name.get(k) or set()
            if not hit and " " not in k:
                hit = {pid for pid, p in self.persons.items() if p["state"] != ME and norm_name(p["name"]).split()[:1] == [k]}
            if len(hit) == 1:
                ids |= hit
        return ids

    def search_terms(self, text):
        """For chat's search(person=…): every name, alias and address of the person the text names (by name, alias or
        address), or [] when it names no one or several people."""
        k = norm_name(text)
        p = self.person_of("email", str(text or "").strip().lower()) if "@" in str(text or "") else None
        if p is None:
            hit = self.by_name.get(k) or set()
            p = self.persons.get(next(iter(hit))) if len(hit) == 1 else None
        if p is None or p["state"] == ME:
            return []
        terms = [p["name"]] + p["aliases"] + sorted(p["email"]) + sorted(p["names"])
        return list(dict.fromkeys(t for t in terms if t and len(t) >= 2))[:12]


class People:
    def __init__(self, store, index=None, clock=time.time):
        self.store, self.index, self.clock = store, index, clock
        self.lock = threading.RLock()
        self._ver = 0
        self._dir = None
        with store.lock:
            store.db.executescript(_SCHEMA)
            if store.db.execute("SELECT 1 FROM people WHERE id=?", (ME,)).fetchone() is None:
                now = clock()
                store.db.execute("INSERT INTO people (id, name, state, created, updated) VALUES (?,?,?,?,?)",
                                 (ME, "You", ME, now, now))

    # basics -----------------------------------------------------------------------------------------------------
    @property
    def db(self):
        return self.store.db

    def _touch(self):
        self._ver += 1
        self._dir = None

    def _row(self, pid):
        with self.store.lock:
            r = self.db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise Gone("That person isn't known any more. Reload the page.")
        return dict(r, aliases=json.loads(r["aliases"] or "[]"))

    def _root(self, pid):
        for _ in range(20):
            r = self._row(pid)
            if not r["merged_into"]:
                return r
            pid = r["merged_into"]
        return r

    def _handles(self, pid):
        with self.store.lock:
            rows = self.db.execute("SELECT * FROM person_handles WHERE person=? ORDER BY n DESC, added", (pid,)).fetchall()
        return [dict(r, extra=json.loads(r["extra"] or "{}")) for r in rows]

    def _audit(self, action, a="", b="", names="", detail=None):
        self.db.execute("INSERT INTO people_audit (at, action, a, b, names, detail) VALUES (?,?,?,?,?,?)",
                        (self.clock(), action, a, b, names, json.dumps(detail or {})))

    def owner_names(self):
        """Folded names the owner uses: the About you name and the names people use for the owner."""
        me = self._row(ME)
        return {norm_name(n) for n in [self.store.settings().get("name")] + me["aliases"] if norm_name(n)}

    # the owner: only from connected accounts ---------------------------------------------------------------------
    def refresh_me(self):
        """Rebuilds the owner's identifiers from the connected accounts only: Google account emails, the Gmail send-as
        addresses read with the owner's button (meta people_sendas), each Slack workspace's own user id and the linked
        Signal account. Returns the handles. Nothing from message content is ever read here."""
        want = {}
        accounts = self.store.accounts()
        for a in accounts:
            want[("email", a["email"].lower())] = (a["email"].lower(), "google", "account", {})
        known = {a["email"].lower() for a in accounts}
        for x in self.store._get_meta("people_sendas", []) or []:
            if isinstance(x, dict) and x.get("account") in known and isinstance(x.get("email"), str):
                want.setdefault(("email", x["email"].lower()), (x["email"].lower(), "google", "sendas", {}))
        for w in self.store.slack_workspaces():
            if w.get("user_id"):
                want[("slack", w["user_id"].lower())] = ("@" + clean(w.get("user_name") or "you", 40) + " · " +
                                                         clean(w.get("team_name") or "Slack", 40), "slack", "auth.test",
                                                         {"ws": w.get("team_name") or ""})
        if self.store.signal_meta().get("linked"):
            want[("signal", "me")] = ("Signal", "signal", "linked", {})
        now = self.clock()
        with self.store.lock:
            have = {(r["kind"], r["value"]) for r in self.db.execute(
                "SELECT kind, value FROM person_handles WHERE person=?", (ME,))}
            for k in have - set(want):
                self.db.execute("DELETE FROM person_handles WHERE person=? AND kind=? AND value=?", (ME,) + k)
            for (kind, value), (label, ver, src, extra) in want.items():
                self.db.execute(
                    "INSERT INTO person_handles (person, kind, value, label, verified, source, extra, added) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind, value) DO UPDATE SET person=excluded.person, "
                    "label=excluded.label, verified=excluded.verified, source=excluded.source, extra=excluded.extra",
                    (ME, kind, value, label, ver, src, json.dumps(extra), now))
            name = clean(self.store.settings().get("name") or "") or "You"
            self.db.execute("UPDATE people SET name=? WHERE id=?", (name, ME))
            # a proposal can never involve the owner, even one stored before an address became the owner's
            self.db.execute("UPDATE merge_proposals SET state='different' WHERE (a=? OR b=?) AND state!='different'",
                            (ME, ME))
        self._touch()
        return self._handles(ME)

    def me_keys(self):
        return {(h["kind"], h["value"]) for h in self._handles(ME)}

    def set_send_as(self, by_account):
        """by_account: {account email: [send-as addresses]} as Gmail's settings list them (read-only GET with
        gmail.readonly). Stored, then the owner record is rebuilt."""
        rows = [{"account": acc.lower(), "email": e.lower()} for acc, es in by_account.items() for e in es
                if isinstance(e, str) and _EMAIL.fullmatch(e)]
        # accounts that couldn't be read this time keep what was read before
        rows += [x for x in self.store._get_meta("people_sendas", []) or []
                 if isinstance(x, dict) and x.get("account") not in {a.lower() for a in by_account}]
        self.store._set_meta("people_sendas", rows[:50])
        return self.refresh_me()

    def my_names(self, names):
        """The names people use for the owner ("Isaac", "Zac"), typed by the owner. Names only: they mark mail that
        uses the owner's name; they never make anything the owner's."""
        if not isinstance(names, list) or len(names) > ALIAS_MAX or any(
                not isinstance(n, str) or not clean(n) or len(n) > 40 or "@" in n for n in names):
            raise PeopleError(f"Up to {ALIAS_MAX} names, each at most 40 characters and without @.")
        clean_names = list(dict.fromkeys(clean(n, 40) for n in names))
        with self.store.lock:
            self.db.execute("UPDATE people SET aliases=?, updated=? WHERE id=?", (json.dumps(clean_names), self.clock(), ME))
        self._touch()
        return clean_names

    # seeding: migration and refresh (adds people and handles; never merges) ---------------------------------------
    def _identities(self):
        """[(kind, value, name, label, source, n, extra, area, vip, verified)] from the VIPs, the Know me profile draft
        and the existing contacts (index: mail, calendar and Drive owners; Slack users the brief knows; Signal)."""
        out = []
        for v in self.store.vips():
            out.append(("email", v["email"].lower(), v["name"], v["email"].lower(), "vip", 0, {}, v["area_id"], True,
                        "owner"))
        if self.index is None:
            return out
        try:
            prof = (self.index.meta("profile") or {}).get("draft") or {}
        except Exception:  # noqa: BLE001
            prof = {}
        for x in prof.get("people") or []:
            for e in (x.get("emails") or [])[:3]:
                if isinstance(e, str) and "@" in e:
                    out.append(("email", e.lower(), x.get("name") or "", e.lower(), "profile",
                                int(x.get("heard") or 0) + int(x.get("wrote") or 0), {}, x.get("area") or "",
                                bool(x.get("vip")), "inferred"))
        since = self.clock() - SEED_DAYS * 86400
        mail = collections.defaultdict(lambda: {"n": 0, "wrote": 0, "heard": 0, "names": collections.Counter(),
                                                "areas": collections.Counter(), "src": set()})
        slack_names = collections.Counter()
        for r in self.index.rows(source=["gmail", "calendar", "drive", "slack"], since=since):
            ppl = _split(r.get("people"))
            if r["source"] == "slack":
                if ppl and ppl[0][0]:
                    slack_names[norm_name(ppl[0][0])] += 1
                continue
            sent = r["kind"] == "sent"
            for i, (name, email) in enumerate(ppl):
                if not email:
                    continue
                m = mail[email]
                m["n"] += 1
                m["src"].add(r["source"])
                if r["source"] == "gmail":
                    if sent and i > 0:
                        m["wrote"] += 1
                    elif i == 0 and not sent:
                        m["heard"] += 1
                if name and name.lower() != email:
                    m["names"][clean(name)] += 1
                if r.get("area"):
                    m["areas"][r["area"]] += 1
        for email, m in mail.items():
            if is_role(email):
                continue
            keep = m["wrote"] >= 1 or m["heard"] >= 2 or ("drive" in m["src"]) or \
                ("calendar" in m["src"] and m["n"] >= 2)
            if not keep:
                continue
            src = "drive" if m["src"] == {"drive"} else "calendar" if m["src"] == {"calendar"} else "mail"
            out.append(("email", email, m["names"].most_common(1)[0][0] if m["names"] else "", email, src, m["n"], {},
                        m["areas"].most_common(1)[0][0] if m["areas"] else "", False, "inferred"))
        for w in self.store.slack_workspaces():
            try:
                users = (self.store.slack_secret(w["id"]).get("state") or {}).get("users") or {}
            except Exception:  # noqa: BLE001
                users = {}
            for uid, u in users.items():
                if not isinstance(u, dict) or not uid or uid == w.get("user_id"):
                    continue
                name = clean(u.get("real_name") or u.get("name") or "")
                extra = {"ws": w.get("team_name") or "", "real_name": clean(u.get("real_name") or ""),
                         "email": str(u.get("email") or "").lower()[:254]}
                n = slack_names.get(norm_name(u.get("name") or ""), 0) or slack_names.get(norm_name(name), 0)
                out.append(("slack", uid.lower(), name, "@" + clean(u.get("name") or name or uid, 40) + " · " +
                            clean(w.get("team_name") or "Slack", 40), "slack", n, extra, w.get("area_id") or "",
                            False, "slack"))
        with self.store.lock:
            sig = self.db.execute(
                "SELECT sender, MAX(sender_name) AS name, COUNT(*) AS n FROM signal_messages WHERE outgoing=0 "
                "AND sender != 'me' GROUP BY sender").fetchall()
        area = self.store.signal_meta().get("area_id") or ""
        for r in sig:
            name = clean(r["name"] or "")
            out.append(("signal", str(r["sender"]).lower(), name, (name or "Someone") + " (Signal)", "signal", r["n"],
                        {"contact": name}, area, False, "signal"))
        return out

    def seed(self):
        """Adds people and identifiers from the VIPs, the profile draft and the existing contacts, then looks for
        "Same person?" pairs. Never merges, never adds to the owner, never renames a person the owner confirmed.
        Returns {"added": n, "proposals": n}."""
        with self.lock:
            self.refresh_me()
            mine = self.me_keys()
            now = self.clock()
            added = 0
            with self.store.lock:
                total = self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                for kind, value, name, label, src, n, extra, area, vip, ver in self._identities():
                    if not value or (kind, value) in mine:
                        continue
                    row = self.db.execute("SELECT person, n FROM person_handles WHERE kind=? AND value=?",
                                          (kind, value)).fetchone()
                    if row is not None:
                        self.db.execute("UPDATE person_handles SET n=MAX(n, ?), extra=CASE WHEN ?='{}' THEN extra "
                                        "ELSE ? END WHERE kind=? AND value=?", (int(n), json.dumps(extra),
                                                                                  json.dumps(extra), kind, value))
                        p = self.db.execute("SELECT * FROM people WHERE id=?", (row["person"],)).fetchone()
                        if p is not None and p["state"] == "suggested":
                            sets = {}
                            if vip and not p["vip"]:
                                sets["vip"] = 1
                            if name and (not p["name"] or p["name"] == value) and src in ("vip", "profile", "mail"):
                                sets["name"] = clean(name)
                            if area and not p["area"]:
                                sets["area"] = area
                            if sets:
                                self.db.execute("UPDATE people SET " + ", ".join(k + "=?" for k in sets) +
                                                ", updated=? WHERE id=?", list(sets.values()) + [now, p["id"]])
                        continue
                    if total >= MAX_PEOPLE:
                        continue
                    pid = _pid(kind + ":" + value)
                    self.db.execute("INSERT OR IGNORE INTO people (id, name, state, vip, area, created, updated) "
                                    "VALUES (?,?,?,?,?,?,?)", (pid, clean(name) or value if kind == "email" else
                                                               clean(name) or "Someone", "suggested", int(vip), area or "",
                                                               now, now))
                    self.db.execute("INSERT INTO person_handles (person, kind, value, label, verified, source, n, extra, "
                                    "added) VALUES (?,?,?,?,?,?,?,?,?)", (pid, kind, value, clean(label, 120), ver,
                                                                          src, int(n), json.dumps(extra), now))
                    added += 1
                    total += 1
                self.store._set_meta("people_seeded", now)
            self._touch()
            n = self.match()
            return {"added": added, "proposals": n}

    def maybe_seed(self):
        last = self.store._get_meta("people_seeded", 0) or 0
        if self.clock() - float(last) >= SEED_EVERY_S:
            return self.seed()
        return None

    # matching -------------------------------------------------------------------------------------------------------
    def _signatures(self, mine):
        """{(from address, listed address)} from recent mail signatures (index bodies); the owner's addresses are left
        out on both sides, so a signature can never point at the owner."""
        if self.index is None:
            return set()
        my_emails = {v for k, v in mine if k == "email"}
        out = set()
        try:
            rows = self.index.bodies("gmail", self.clock() - SIG_DAYS * 86400, SIG_MAX)
        except Exception:  # noqa: BLE001
            return out
        for r in rows:
            if r["kind"] == "sent":
                continue
            ppl = _split(r["people"])
            if not ppl or not ppl[0][1]:
                continue
            frm = ppl[0][1]
            if frm in my_emails or is_role(frm):
                continue
            listed = [x for x in signature_addresses(r["body"]) if x != frm and x not in my_emails and not is_role(x)]
            if 1 <= len(listed) <= 4:
                for x in listed:
                    out.add((frm, x))
        return out

    def _all(self):
        """{pid: {id, name, aliases, state, vip, area, handles}} for everyone but merged people."""
        with self.store.lock:
            ps = self.db.execute("SELECT * FROM people WHERE state != 'merged'").fetchall()
            hs = self.db.execute("SELECT * FROM person_handles").fetchall()
        out = {p["id"]: dict(p, aliases=json.loads(p["aliases"] or "[]"), handles=[]) for p in ps}
        for h in hs:
            if h["person"] in out:
                out[h["person"]]["handles"].append(dict(h, extra=json.loads(h["extra"] or "{}")))
        return out

    def match(self):
        """Opens "Same person?" proposals from the deterministic signals. Pairs the owner answered stay answered
        ("different" is never asked again; "later" waits a week). Returns the number of open proposals."""
        with self.lock:
            mine = self.me_keys()
            everyone = self._all()
            own = self.owner_names()
            sigs = self._signatures(mine)
            cands = {pid: p for pid, p in everyone.items() if p["state"] != ME and not self._role_only(p)}
            keys = collections.defaultdict(set)
            name_count = collections.Counter()
            for pid, p in cands.items():
                ns = {n for n, _ in _names(p)}
                for n in ns:
                    name_count[n] += 1
                    keys["n:" + n].add(pid)
                for h in p["handles"]:
                    if h["kind"] == "email":
                        keys["c:" + canon_email(h["value"])].add(pid)
                        keys["e:" + h["value"]].add(pid)
                    pe = str((h.get("extra") or {}).get("email") or "").lower()
                    if h["kind"] == "slack" and pe:
                        keys["e:" + pe].add(pid)
            by_email = {h["value"]: pid for pid, p in cands.items() for h in p["handles"] if h["kind"] == "email"}
            pairs = set()
            for k, ids in keys.items():
                if k.startswith("n:") and (k[2:] in own or len(ids) > 6):
                    continue
                ids = sorted(ids)
                for i, a in enumerate(ids):
                    for b in ids[i + 1:]:
                        pairs.add((a, b))
            for f, t in sigs:
                a, b = by_email.get(f), by_email.get(t)
                if a and b and a != b:
                    pairs.add(tuple(sorted((a, b))))
            now = self.clock()
            with self.store.lock:
                old = {(r["a"], r["b"]): dict(r) for r in self.db.execute("SELECT * FROM merge_proposals")}
                for a, b in pairs:
                    s = signals(cands[a], cands[b], own, sigs, name_count)
                    prev = old.get((a, b))
                    if not opens(s):
                        if prev and prev["state"] == "open" and prev["by"] == "rules":
                            self.db.execute("DELETE FROM merge_proposals WHERE id=?", (prev["id"],))
                        continue
                    if prev is None:
                        self.db.execute("INSERT INTO merge_proposals (id, a, b, signals, score, created) VALUES "
                                        "(?,?,?,?,?,?)", (pair_id(a, b), a, b, json.dumps(s), score(s), now))
                    elif prev["state"] in ("open", "later"):
                        self.db.execute("UPDATE merge_proposals SET signals=?, score=? WHERE id=?",
                                        (json.dumps(s), score(s), prev["id"]))
                n = self.db.execute("SELECT COUNT(*) FROM merge_proposals WHERE state='open' OR (state='later' AND "
                                    "until<=?)", (now,)).fetchone()[0]
            return n

    @staticmethod
    def _role_only(p):
        hs = p["handles"]
        return bool(hs) and all(h["kind"] == "email" and is_role(h["value"]) for h in hs)

    # the owner's answers --------------------------------------------------------------------------------------------
    def _proposal(self, prop_id):
        with self.store.lock:
            r = self.db.execute("SELECT * FROM merge_proposals WHERE id=?", (prop_id,)).fetchone()
        if r is None:
            raise Gone("That suggestion isn't open any more. Reload the page.")
        return dict(r, signals=json.loads(r["signals"] or "[]"))

    def decide(self, prop_id, answer):
        """The owner's answer to a "Same person?" card: same (merge), different (never asked again) or later (a week)."""
        if answer not in ("same", "different", "later"):
            raise PeopleError("Answer same, different or later.")
        with self.lock:
            p = self._proposal(prop_id)
            if p["state"] not in ("open", "later"):
                raise PeopleError("That suggestion was already answered.")
            if ME in (p["a"], p["b"]):
                raise PeopleError("Nothing is ever merged with you.")
            now = self.clock()
            if answer == "same":
                out = self.merge(p["a"], p["b"], prop_id)
                return out
            a, b = self._row(p["a"]), self._row(p["b"])
            with self.store.lock:
                if answer == "different":
                    self.db.execute("UPDATE merge_proposals SET state='different', decided=? WHERE id=?", (now, prop_id))
                    self._audit("different", a["id"], b["id"], f"{a['name']} and {b['name']}")
                else:
                    self.db.execute("UPDATE merge_proposals SET state='later', decided=?, until=? WHERE id=?",
                                    (now, now + LATER_S, prop_id))
            self._touch()
            return {"ok": True, "state": answer}

    def merge(self, a_id, b_id, prop_id=None):
        """Merges b into a (the owner pressed Same person). The owner record never merges. Every handle b had moves
        to a; the audit entry keeps what moved, so Split can put it back."""
        with self.lock:
            if ME in (a_id, b_id):
                raise PeopleError("Nothing is ever merged with you.")
            a, b = self._row(a_id), self._row(b_id)
            if a["merged_into"] or b["merged_into"] or a_id == b_id:
                raise PeopleError("One of them was already merged. Reload the page.")
            # the one the owner already shaped (VIP, confirmed, more handles) stays the person
            if (b["vip"], b["state"] == "confirmed", len(self._handles(b_id))) > \
                    (a["vip"], a["state"] == "confirmed", len(self._handles(a_id))):
                a, b = b, a
            moved = [[h["kind"], h["value"]] for h in self._handles(b["id"])]
            add = [n for n in [b["name"]] + b["aliases"] if n and norm_name(n) != norm_name(a["name"]) and
                   norm_name(n) not in {norm_name(x) for x in a["aliases"]} and "@" not in n]
            now = self.clock()
            detail = {"moved": moved, "aliases_added": add, "vip_from_b": bool(b["vip"] and not a["vip"]),
                      "area_from_b": b["area"] if b["area"] and not a["area"] else "", "a_state": a["state"],
                      "proposal": prop_id or ""}
            with self.store.lock:
                self.db.execute("UPDATE person_handles SET person=? WHERE person=?", (a["id"], b["id"]))
                self.db.execute("UPDATE people SET aliases=?, vip=?, area=?, state='confirmed', updated=? WHERE id=?",
                                (json.dumps((a["aliases"] + add)[:ALIAS_MAX]), int(a["vip"] or b["vip"]),
                                 a["area"] or b["area"], now, a["id"]))
                self.db.execute("UPDATE people SET state='merged', merged_into=?, updated=? WHERE id=?",
                                (a["id"], now, b["id"]))
                if prop_id:
                    self.db.execute("UPDATE merge_proposals SET state='merged', decided=? WHERE id=?", (now, prop_id))
                # other open suggestions about b now ask about a (or end, when they were about a and b)
                for r in self.db.execute("SELECT * FROM merge_proposals WHERE (a=? OR b=?) AND state IN ('open','later')",
                                         (b["id"], b["id"])).fetchall():
                    other = r["b"] if r["a"] == b["id"] else r["a"]
                    self.db.execute("DELETE FROM merge_proposals WHERE id=?", (r["id"],))
                    if other != a["id"]:
                        x, y = sorted((a["id"], other))
                        self.db.execute("INSERT OR IGNORE INTO merge_proposals (id, a, b, signals, score, by, state, "
                                        "created, until) VALUES (?,?,?,?,?,?,?,?,?)",
                                        (pair_id(x, y), x, y, r["signals"], r["score"], r["by"], r["state"],
                                         r["created"], r["until"]))
                self._audit("merge", a["id"], b["id"], f"{a['name']} and {b['name']}", detail)
            self._touch()
            return {"ok": True, "state": "same", "person": self.person(a["id"])}

    def split(self, audit_id):
        """Undoes one merge from the History: the handles that moved go back, the merged person returns, and the pair
        is remembered as different people."""
        with self.lock:
            with self.store.lock:
                e = self.db.execute("SELECT * FROM people_audit WHERE id=?", (int(audit_id),)).fetchone()
            if e is None or e["action"] != "merge":
                raise Gone("That merge isn't in the history. Reload the page.")
            if e["undone"]:
                raise PeopleError("Those two were already split.")
            d = json.loads(e["detail"] or "{}")
            a = self._root(e["a"])
            b = self._row(e["b"])
            now = self.clock()
            with self.store.lock:
                for kind, value in d.get("moved") or []:
                    self.db.execute("UPDATE person_handles SET person=? WHERE kind=? AND value=? AND person!=?",
                                    (b["id"], kind, value, ME))
                self.db.execute("UPDATE people SET state='confirmed', merged_into='', updated=? WHERE id=?", (now, b["id"]))
                drop = {norm_name(x) for x in d.get("aliases_added") or []}
                aliases = [x for x in a["aliases"] if norm_name(x) not in drop]
                vip = 0 if d.get("vip_from_b") else a["vip"]
                area = "" if d.get("area_from_b") and a["area"] == d["area_from_b"] else a["area"]
                self.db.execute("UPDATE people SET aliases=?, vip=?, area=?, updated=? WHERE id=?",
                                (json.dumps(aliases), vip, area, now, a["id"]))
                self.db.execute("UPDATE people_audit SET undone=? WHERE id=?", (now, e["id"]))
                x, y = sorted((e["a"], e["b"]))
                self.db.execute("INSERT INTO merge_proposals (id, a, b, signals, score, state, created, decided) VALUES "
                                "(?,?,?,?,?,?,?,?) ON CONFLICT(a, b) DO UPDATE SET state='different', decided=?",
                                (pair_id(x, y), x, y, "[]", 0, "different", now, now, now))
                self._audit("split", a["id"], b["id"], f"{a['name']} and {b['name']}", {"merge": e["id"]})
            self._touch()
            return {"ok": True, "people": [self.person(a["id"]), self.person(b["id"])]}

    def patch(self, pid, body):
        """The owner's edits to a person: name, vip, area. Turning VIP off also removes the person's addresses from
        the VIP list, so no handle of theirs still counts."""
        if pid == ME:
            raise PeopleError("Your own record comes from your accounts. Add the names people use for you instead.")
        if not isinstance(body, dict) or not body or set(body) - {"name", "vip", "area"}:
            raise PeopleError("Send name, vip or area.")
        with self.lock:
            p = self._root(pid)
            sets = {}
            if "name" in body:
                n = clean(body["name"]) if isinstance(body["name"], str) else ""
                if not n or "@" in n:
                    raise PeopleError("A name of at most 80 characters, without @.")
                sets["name"] = n
            if "vip" in body:
                if not isinstance(body["vip"], bool):
                    raise PeopleError("vip must be true or false.")
                sets["vip"] = int(body["vip"])
            if "area" in body:
                area = body["area"] if isinstance(body["area"], str) else None
                if area is None or (area and area not in {a["id"] for a in self.store.areas()}):
                    raise PeopleError("Pick an area.")
                sets["area"] = area
            with self.store.lock:
                self.db.execute("UPDATE people SET " + ", ".join(k + "=?" for k in sets) + ", state='confirmed', "
                                "updated=? WHERE id=?", list(sets.values()) + [self.clock(), p["id"]])
            if sets.get("vip") == 0:
                emails = {h["value"] for h in self._handles(p["id"]) if h["kind"] == "email"}
                for v in self.store.vips():
                    if v["email"].lower() in emails:
                        self.store.delete_vip(v["id"])
            self._touch()
            return self.person(p["id"])

    # views ---------------------------------------------------------------------------------------------------------
    def directory(self):
        with self.lock:
            if self._dir is not None:
                return self._dir
            persons, by_handle = {}, {}
            for pid, p in self._all().items():
                v = {"id": pid, "name": p["name"], "aliases": p["aliases"], "state": p["state"], "vip": bool(p["vip"]),
                     "area": p["area"], "email": set(), "slack": set(), "signal": set(), "names": set()}
                for h in p["handles"]:
                    if h["kind"] in KINDS:
                        v[h["kind"]].add(h["value"])
                    by_handle[(h["kind"], h["value"])] = pid
                    ex = h.get("extra") or {}
                    for k in ("real_name", "contact"):
                        if ex.get(k):
                            v["names"].add(ex[k])
                persons[pid] = v
            self._dir = Directory(persons, by_handle)
            return self._dir

    def _handle_view(self, h):
        unit = "email" if h["kind"] == "email" else "message"
        src = {"vip": "VIP list", "drive": "Google Drive", "calendar": "Calendar", "sendas": "Gmail “send as”",
               "account": "connected", "auth.test": "connected", "linked": "linked"}.get(h["source"], "")
        return {"kind": h["kind"], "label": h["label"] or (h["value"] if h["kind"] == "email" else ""),
                "count": (f"{h['n']:,} {unit}{'s' if h['n'] != 1 else ''}" if h["n"] else src)}

    def person(self, pid, areas=None):
        p = self._root(pid)
        areas = areas if areas is not None else {a["id"]: a["name"] for a in self.store.areas()}
        hs = self._handles(p["id"])
        with self.store.lock:
            merges = self.db.execute("SELECT id, b, names, at FROM people_audit WHERE action='merge' AND undone IS NULL "
                                     "AND a=? ORDER BY id DESC", (p["id"],)).fetchall()
        sub = " · ".join(x for x in ["VIP" if p["vip"] else "", areas.get(p["area"], ""),
                                     "New" if p["state"] == "suggested" and not p["vip"] else ""] if x)
        return {"id": p["id"], "name": p["name"], "aliases": p["aliases"], "state": p["state"], "vip": bool(p["vip"]),
                "area": p["area"], "sub": sub, "handles": [self._handle_view(h) for h in hs[:8]],
                "more": max(0, len(hs) - 8), "merges": [{"id": m["id"], "names": m["names"]} for m in merges],
                "n": sum(h["n"] for h in hs)}

    def _card(self, r, areas):
        sigs = json.loads(r["signals"] or "[]")
        return {"id": r["id"], "a": self.person(r["a"], areas), "b": self.person(r["b"], areas),
                "signals": [{"strength": strength(s["weight"]), "why": s["why"]} for s in sigs],
                "caution": caution(sigs), "by": r["by"]}

    def proposals(self, person=None, limit=MAX_OPEN):
        now = self.clock()
        areas = {a["id"]: a["name"] for a in self.store.areas()}
        with self.store.lock:
            q = "SELECT * FROM merge_proposals WHERE (state='open' OR (state='later' AND until<=?))"
            args = [now]
            if person:
                q += " AND (a=? OR b=?)"
                args += [person, person]
            rows = self.db.execute(q + " ORDER BY score DESC, created LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            try:
                out.append(self._card(r, areas))
            except Gone:
                continue
        return out

    def open_count(self):
        with self.store.lock:
            return self.db.execute("SELECT COUNT(*) FROM merge_proposals WHERE state='open' OR (state='later' AND "
                                   "until<=?)", (self.clock(),)).fetchone()[0]

    def audit(self):
        with self.store.lock:
            rows = self.db.execute("SELECT * FROM people_audit ORDER BY id DESC LIMIT ?", (AUDIT_SHOW,)).fetchall()
        words = {"merge": "Merged", "split": "Split", "different": "Kept apart"}
        return [{"id": r["id"], "action": r["action"], "text": f"{words.get(r['action'], r['action'])}: {r['names']}",
                 "at": r["at"], "can_split": r["action"] == "merge" and not r["undone"]} for r in rows]

    def me(self):
        me = self._row(ME)
        return {"name": me["name"], "names": me["aliases"],
                "handles": [self._handle_view(h) for h in self._handles(ME)],
                "send_as_read": bool(self.store._get_meta("people_sendas", None) is not None)}

    def view(self, q=""):
        """GET api/people: the owner record, open suggestions, everyone (searched by name or handle) and the history."""
        self.maybe_seed()
        k = norm_name(q)[:80]
        areas = {a["id"]: a["name"] for a in self.store.areas()}
        with self.store.lock:
            rows = self.db.execute("SELECT p.id, p.name, p.vip, p.state, (SELECT COALESCE(SUM(n),0) FROM person_handles "
                                   "h WHERE h.person=p.id) AS n FROM people p WHERE p.state IN ('suggested','confirmed')"
                                   " ORDER BY p.vip DESC, p.state='confirmed' DESC, n DESC, p.name").fetchall()
            total = len(rows)
            if k:
                hit = {r["person"] for r in self.db.execute("SELECT person FROM person_handles WHERE value LIKE ? OR "
                                                            "lower(label) LIKE ?", ("%" + k + "%", "%" + k + "%"))}
                rows = [r for r in rows if k in norm_name(r["name"]) or r["id"] in hit]
        people = [self.person(r["id"], areas) for r in rows[:LIST_MAX]]
        return {"me": self.me(), "proposals": self.proposals(), "people": people, "total": total,
                "shown": len(people), "audit": self.audit()}


def _split(text):
    """The index's people field as [(name, lower-case email)] (know_profile.split_people without the import)."""
    out = []
    for part in str(text or "").split("; "):
        part = part.strip()
        if not part or part.startswith("#"):
            continue
        m = re.match(r"^(.*?)\s*<([^<>\s]+@[^<>\s]+)>$", part)
        if m:
            out.append((m.group(1).strip().strip('"').strip(), m.group(2).lower()))
        elif re.fullmatch(r"[^@\s<>;,]+@[^@\s<>;,]+\.[^@\s<>;,]+", part):
            out.append(("", part.lower()))
        else:
            out.append((part, ""))
    return out
