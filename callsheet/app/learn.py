"""0.8.5 Learning (design.md section 7 (e), docs/learning.md): the owner's own corrections become priors per sender,
per person (People) and per domain.

- Only owner actions make an event: Done on a reply row, the brief's fixes (Needs a reply, Not for me, This is a
  promo) and Check the sorting's Right / Wrong. A message can't make one, however many arrive (S-L2).
- An event is labels and identities only: the action, the learned label, the item's key (a hash), the source kind,
  the sender id (mail address, Slack user id, Signal sender id), the domain, the People id and the sender's display
  name. Never message text (S-L1). Every value is checked against a pattern before it is stored.
- The sender comes from the verified fields the brief and the sorter already use (the From address, the API's user
  id, signal-cli's sender), never from a signature or the text; an item with a doubt mark (sender not verified,
  look-alike domain, uses the owner's or a VIP's name, hidden text) teaches nothing and gets no prior (S-L3).
- A prior: 2 or more events with the same label and none against it (weights: 1 up to FULL_DAYS old, 0.5 up to
  KEEP_DAYS, then the event is dropped). Sender and person events come first; the domain only when they don't
  disagree.
- Applied after the model's label (`apply_prior`, deterministic, read time): it may move a row between groups, but
  never out of Needs you when an override or the rules' sure reply put it there, never moves security or invites,
  and never hides anything (every row stays in exactly one section).
- Setup -> AI -> Sorting -> What it learned lists every sender, person and domain with its counts; Forget and Forget all
  delete the events."""
import re
import time

LEARNED = ("needs_reply", "fyi", "promo", "update", "not_for_me")
ACTIONS = ("done", "fix_needs_reply", "fix_not_for_me", "fix_promo", "check_right", "check_wrong")
TARGET = {"needs_reply": "needs_you", "fyi": "for_your_info", "promo": "sorted_promos", "update": "sorted_updates",
          "not_for_me": "probably_not_for_you"}
FIX_LABEL = {"needs_reply": "needs_reply", "not_for_me": "not_for_me", "promo": "promo"}
KIND_LABEL = {"needs_reply": "needs_reply", "fyi": "fyi", "promo": "promo", "automated_update": "update",
              "meeting_or_date": "fyi", "for_someone_else": "not_for_me"}   # security: nothing to learn
NAMES = {"needs_reply": "needs a reply", "fyi": "for your info", "promo": "a promo", "update": "an automated update",
         "not_for_me": "not for you"}
DOUBT = frozenset(("sender_not_verified", "lure", "uses_owner_name", "uses_vip_name", "hidden_text"))
FIXED = frozenset(("heads_up_security", "your_day"))     # security and invites never move
RANK = {"needs_you": 0, "heads_up_security": 0, "your_day": 0, "for_your_info": 1, "what_changed": 1,
        "sorted_promos": 2, "sorted_updates": 2, "probably_not_for_you": 2}
MIN_WEIGHT = 2.0
FULL_DAYS = 45
KEEP_DAYS = 90
SRCS = ("mail", "slack", "signal", "drive")

_SENDER = re.compile(r"[a-z0-9._%+'-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}|[a-z0-9][a-z0-9_:+.=/-]{2,90}")
_DOMAIN = re.compile(r"(?=.{4,190}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,24}")
_PERSON = re.compile(r"p[0-9a-f]{11}")
_KEY = re.compile(r"[0-9a-f]{16}")
_ENTRY = re.compile(r"([spd]):(.{1,254})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, action TEXT NOT NULL, label TEXT NOT NULL,
  key TEXT NOT NULL DEFAULT '', src TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '', person TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', cid INTEGER);
CREATE INDEX IF NOT EXISTS learn_events_sender ON learn_events (sender);
CREATE INDEX IF NOT EXISTS learn_events_domain ON learn_events (domain);
CREATE INDEX IF NOT EXISTS learn_events_person ON learn_events (person);
CREATE INDEX IF NOT EXISTS learn_events_key ON learn_events (key, action);
"""


def name_part(s, n=60):
    """A display name: letters, digits, spaces, dots, hyphens and apostrophes only, clipped."""
    s = re.sub(r"[^\w .'-]", " ", str(s or ""), flags=re.UNICODE)
    return " ".join(s.split())[:n].strip()


def clean_sender(v):
    v = str(v or "").strip().lower()
    return v if len(v) <= 254 and _SENDER.fullmatch(v) else ""


def clean_domain(v):
    v = str(v or "").strip().lower().rstrip(".")
    return v if _DOMAIN.fullmatch(v) else ""


def weight(at, now):
    age = (now - float(at)) / 86400
    return 1.0 if age <= FULL_DAYS else 0.5 if age <= KEEP_DAYS else 0.0


def decide(tally):
    """{label: weight} -> the prior's label or None: MIN_WEIGHT or more for one label and nothing against it."""
    live = {k: w for k, w in tally.items() if w > 0}
    if len(live) != 1:
        return None
    (label, w), = live.items()
    return label if w >= MIN_WEIGHT else None


def apply_prior(group, row, prior):
    """The prior after the model's label (pure). row: the sorting row (overrides, marks, pre, strength). Returns
    (group, how): how is None (no prior, or it doesn't apply: security, invites, a doubt mark), "agrees", "prior"
    (moved) or "prior_blocked" (it would have taken the row out of Needs you past an override or the rules' sure
    reply, so it didn't)."""
    if not prior or prior.get("label") not in TARGET or group in FIXED or group not in RANK:
        return group, None
    if DOUBT & set(row.get("marks") or ()):
        return group, None
    target = TARGET[prior["label"]]
    if target == group:
        return group, "agrees"
    if RANK[target] > RANK[group] and (row.get("overrides") or (row.get("pre") == "reply"
                                                                and row.get("strength") == "strong")):
        return group, "prior_blocked"
    return target, "prior"


class Learner:
    def __init__(self, store, people=None, clock=time.time):
        self.store, self.people, self.clock = store, people, clock
        with store.lock:
            store.db.executescript(_SCHEMA)

    @property
    def db(self):
        return self.store.db

    # identities -------------------------------------------------------------------------------------------------------
    def _own_domains(self):
        out = set()
        try:
            for a in self.store.accounts():
                if a.get("email") and "@" in a["email"]:
                    out.add(a["email"].rsplit("@", 1)[1].lower())
        except Exception:  # noqa: BLE001
            pass
        return out

    def _public(self):
        try:
            import people as people_mod
            return people_mod.PUBLIC
        except Exception:  # noqa: BLE001
            return frozenset()

    def _person(self, src, sender):
        if self.people is None or not sender:
            return "", ""
        kind = {"mail": "email", "drive": "email", "slack": "slack", "signal": "signal"}.get(src)
        try:
            p = self.people.directory().person_of(kind, sender) if kind else None
        except Exception:  # noqa: BLE001 (People is optional)
            p = None
        if not p or p.get("state") == "me" or not _PERSON.fullmatch(str(p.get("id") or "")):
            return "", ""
        return p["id"], name_part(p.get("name"))

    def ident(self, src, sender, name="", domain=None):
        """{src, sender, domain, person, name} from verified fields, or None when there's nothing to learn by."""
        src = src if src in SRCS else ""
        sender = clean_sender(sender)
        if not src or not sender:
            return None
        dom = ""
        if src in ("mail", "drive") and "@" in sender:
            dom = clean_domain(domain if domain else sender.rsplit("@", 1)[1])
            if dom in self._public() or dom in self._own_domains():
                dom = ""
        pid, pname = self._person(src, sender)
        return {"src": src, "sender": sender, "domain": dom, "person": pid, "name": pname or name_part(name)}

    # events -----------------------------------------------------------------------------------------------------------
    def record(self, action, label, ident, key="", marks=(), cid=None):
        """One owner action. Returns the event id, or None when there's nothing to learn (no identity, a doubt
        mark, a label outside LEARNED)."""
        if action not in ACTIONS or label not in LEARNED or not ident or DOUBT & set(marks or ()):
            return None
        key = key if isinstance(key, str) and _KEY.fullmatch(key) else ""
        cid = int(cid) if isinstance(cid, int) else None
        with self.store.lock:
            cur = self.db.execute("INSERT INTO learn_events (at, action, label, key, src, sender, domain, person, name, "
                                  "cid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (float(self.clock()), action, label, key, ident["src"], clean_sender(ident["sender"]),
                                   clean_domain(ident.get("domain")),
                                   ident.get("person") if _PERSON.fullmatch(ident.get("person") or "") else "",
                                   name_part(ident.get("name")), cid))
            return cur.lastrowid

    def unrecord(self, key=None, action=None, cid=None):
        """Undo: the events of one correction (cid) or one action on one item (key, action)."""
        with self.store.lock:
            if cid is not None:
                return self.db.execute("DELETE FROM learn_events WHERE cid=?", (int(cid),)).rowcount
            if key and action in ACTIONS:
                return self.db.execute("DELETE FROM learn_events WHERE key=? AND action=?", (key, action)).rowcount
        return 0

    def prune(self):
        with self.store.lock:
            return self.db.execute("DELETE FROM learn_events WHERE at<?",
                                   (float(self.clock()) - KEEP_DAYS * 86400,)).rowcount

    # priors -----------------------------------------------------------------------------------------------------------
    def _tally(self, where, args):
        now = float(self.clock())
        with self.store.lock:
            rs = self.db.execute(f"SELECT label, at FROM learn_events WHERE {where}", args).fetchall()
        t = {}
        for r in rs:
            t[r["label"]] = t.get(r["label"], 0.0) + weight(r["at"], now)
        return {k: v for k, v in t.items() if v > 0}

    def prior(self, ident):
        """{label, by: person|sender|domain, n, name} or None. Sender and person events first; the domain only when
        those don't disagree with it."""
        if not ident or not ident.get("sender"):
            return None
        conds, args = ["sender=?"], [ident["sender"]]
        if ident.get("person"):
            conds.append("person=?")
            args.append(ident["person"])
        spec = self._tally(" OR ".join(conds), args)
        lab = decide(spec)
        if lab:
            return {"label": lab, "by": "person" if ident.get("person") else "sender", "n": int(spec[lab] + 0.5),
                    "name": ident.get("name") or ""}
        if ident.get("domain"):
            dom = self._tally("domain=?", [ident["domain"]])
            lab = decide(dom)
            if lab and set(spec) <= {lab}:
                return {"label": lab, "by": "domain", "n": int(dom[lab] + 0.5), "name": ident["domain"]}
        return None

    # What it learned --------------------------------------------------------------------------------------------------
    def learned(self):
        """Every sender, person and domain with its counts per label and its prior (if any). Newest first."""
        self.prune()
        now = float(self.clock())
        with self.store.lock:
            rs = self.db.execute("SELECT * FROM learn_events ORDER BY at DESC").fetchall()
        ent = {}

        def add(eid, kind, name, r):
            e = ent.setdefault(eid, {"id": eid, "kind": kind, "name": name, "src": r["src"], "counts": {},
                                     "weights": {}, "last": r["at"]})
            e["counts"][r["label"]] = e["counts"].get(r["label"], 0) + 1
            e["weights"][r["label"]] = e["weights"].get(r["label"], 0.0) + weight(r["at"], now)
            if not e["name"] and name:
                e["name"] = name
        for r in rs:
            if r["person"]:
                add("p:" + r["person"], "person", r["name"], r)
            elif r["sender"]:
                add("s:" + r["sender"], "sender", r["name"] or r["sender"], r)
            if r["domain"]:
                add("d:" + r["domain"], "domain", r["domain"], r)
        out = []
        for e in ent.values():
            if e["kind"] == "sender":
                e["detail"] = e["id"][2:] if e["name"] != e["id"][2:] else ""
            else:
                e["detail"] = ""
            if e["kind"] == "person":
                e["name"] = e["name"] or "Someone in People"
            e["prior"] = decide(e.pop("weights"))
            e["counts"] = [{"label": k, "n": e["counts"][k]} for k in LEARNED if k in e["counts"]]
            out.append(e)
        out.sort(key=lambda e: (e["prior"] is None, -e["last"]))
        return out

    def forget(self, eid):
        """Forget one sender, person or domain: its events lose that identity (and go when nothing is left)."""
        m = _ENTRY.fullmatch(str(eid or ""))
        if not m:
            return 0
        kind, v = m.groups()
        with self.store.lock:
            if kind == "d":
                n = self.db.execute("UPDATE learn_events SET domain='' WHERE domain=?", (v,)).rowcount
            elif kind == "p":
                n = self.db.execute("UPDATE learn_events SET person='', sender='', name='' WHERE person=?",
                                    (v,)).rowcount
            else:
                n = self.db.execute("UPDATE learn_events SET sender='', name='' WHERE sender=? AND person=''",
                                    (v,)).rowcount
            self.db.execute("DELETE FROM learn_events WHERE sender='' AND person='' AND domain=''")
        return n

    def forget_all(self):
        with self.store.lock:
            return self.db.execute("DELETE FROM learn_events").rowcount

    def count(self):
        with self.store.lock:
            return self.db.execute("SELECT COUNT(*) FROM learn_events").fetchone()[0]
