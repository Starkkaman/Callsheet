"""0.21.0 Suggested projects and Drive files on a project (Agenda & Projects answers, approved 2026-11-13).

- Suggested projects: 5 or more messages over the last 90 days about the same topic (an email subject, a Slack
  channel) or, in a chat, the same people coming back to the same word, family chats included. Split into a Personal
  side (texts, Signal, free-mail addresses, Personal/Family/Home areas) and a Professional side (Slack, work
  addresses, Work areas). Only suggestions: a project is made only when the owner presses Add (projects.create), and
  "Not a project" is remembered. They live in Callsheet only (callsheet.db), never in any account.
- Files: Drive documents the library keeps (messages.db `docs`, source "drive") link to a project on their own when
  the file's title shares a word of the project's name, or the file's owner is one of the project's people. Shown
  with the file's own Drive link (https on docs.google.com or drive.google.com only).
Rules only: no model, nothing is sent.
"""
import collections
import json
import re
import time
import urllib.parse

import rules
import today as T
from store import Conflict, InputError, NotFound

DAYS = 90
MIN_MSGS = 5
MAX_SIDE = 6
MAX_FILES = 8
NAME_MAX = 60
SOURCES = ("gmail", "slack", "signal", "sms")
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com", "live.com",
             "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "msn.com"}
PROMO = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "SPAM", "TRASH")
PERSONAL_AREAS = ("personal", "family", "home", "house", "kids", "health", "friends")
STOP = set("""about after again also another back been before being could does doing done from have here just like
make many more most much need only other over really said same should some still such than that their them then there
these they thing think this those through want well were what when where which while will with would your yours you're
thanks thank hello please today tomorrow tonight yesterday morning evening night week weekend later soon sure okay yeah
going gonna great good love sorry maybe time call meeting meet update updates project projects notes plan plans team
work email message chat file files reply fwd re""".split())
KIND = {"doc": "Doc", "sheet": "Sheet", "slides": "Slides", "pdf": "PDF"}
SCHEMA = """CREATE TABLE IF NOT EXISTS project_suggestion_marks (
  key TEXT PRIMARY KEY, state TEXT NOT NULL, pid TEXT NOT NULL DEFAULT '', at REAL NOT NULL)"""
_RE = re.compile(r"^\s*((re|fw|fwd|aw|sv)\s*(\[\d+\])?\s*:\s*)+", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")


def _words(text, n=4):
    return {w.lower().strip("'-") for w in _WORD.findall(str(text or "")) if len(w) >= n} - STOP


def _subject(s):
    s = _RE.sub("", " ".join(str(s or "").split()))
    return s.strip(" -:·")


def _domain(addr):
    a = str(addr or "").strip().lower()
    return a.rsplit("@", 1)[1] if "@" in a else ""


def _key(*parts):
    import hashlib
    return "s" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]


def safe_drive_link(url):
    try:
        u = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return None
    if u.scheme == "https" and u.hostname in ("docs.google.com", "drive.google.com") and not u.username:
        return u.geturl()
    return None


class Suggest:
    def __init__(self, ms, projects, clock=time.time):
        self.ms, self.projects, self.clock = ms, projects, clock
        ms.write(lambda db: db.execute(SCHEMA))

    def _read(self, sql, args=()):
        return self.ms._read(sql, args)  # noqa: SLF001

    def _marks(self):
        return {r["key"]: dict(r) for r in self._read("SELECT * FROM project_suggestion_marks")}

    def _owner(self):
        try:
            return {a["email"].lower() for a in self.projects.store.accounts() if a.get("email")}
        except Exception:  # noqa: BLE001
            return set()

    def _areas(self):
        try:
            return {a["id"]: a["name"] for a in self.projects.store.areas()}
        except Exception:  # noqa: BLE001
            return {}

    # --- suggestions --------------------------------------------------------------------------------------------------
    def _clusters(self, now):
        rows = self._read("SELECT id, source, thread, direction, sender, sender_name, recipients, ts, subject, text, "
                          "labels, channel FROM messages WHERE deleted_at IS NULL AND ts >= ? AND ts <= ? AND source IN "
                          "('gmail','slack','signal','sms') ORDER BY ts", (now - DAYS * 86400, now + 3600))
        mine = self._owner()
        topics = collections.defaultdict(list)
        for r in rows:
            try:
                labels = json.loads(r["labels"] or "[]")
            except ValueError:
                labels = []
            if any(lb in PROMO for lb in labels if isinstance(lb, str)):
                continue
            if r["direction"] == "in" and rules.AUTOMATED_SENDER.search(str(r["sender"] or "").split("@")[0]):
                continue
            if r["source"] == "gmail":
                subj = _subject(r["subject"])
                if not _words(subj):
                    continue
                topics[("t", subj.lower())].append(r)
            elif r["source"] == "slack" and r["channel"] and not str(r["channel"]).startswith(("D", "@")):
                topics[("c", str(r["channel"]).lower())].append(r)
            else:
                topics[("p", r["source"] + ":" + (r["thread"] or r["sender"]))].append(r)
        out = []
        for (kind, k), msgs in topics.items():
            if len(msgs) < MIN_MSGS or not any(m["direction"] == "out" for m in msgs):
                continue
            people, addrs = collections.Counter(), set()
            for m in msgs:
                if m["direction"] == "in":
                    nm = T.first(m["sender_name"] or m["sender"]) or ""
                    if nm and m["sender"].lower() not in mine:
                        people[nm] += 1
                        addrs.add(m["sender"].lower())
                else:
                    try:
                        rec = json.loads(m["recipients"] or "[]")
                    except ValueError:
                        rec = []
                    for x in rec if isinstance(rec, list) else []:
                        a = x.get("address") if isinstance(x, dict) else x
                        n = x.get("name") if isinstance(x, dict) else ""
                        if a and str(a).lower() not in mine:
                            addrs.add(str(a).lower())
                            if n or "@" in str(a):
                                people[T.first(n or str(a).split("@")[0])] += 1
            names = [n for n, _ in people.most_common(4) if n]
            srcs = {m["source"] for m in msgs}
            if kind == "t":
                name = _subject(msgs[-1]["subject"])
                about = f"“{name}”"
            elif kind == "c":
                name = str(msgs[-1]["channel"]).lstrip("#").replace("-", " ").replace("_", " ").strip().capitalize()
                about = f"#{str(msgs[-1]['channel']).lstrip('#')}"
            else:      # a chat: the same people coming back to the same word, in 5+ messages
                cnt = collections.Counter()
                for m in msgs:
                    cnt.update(_words(m["text"], 5))
                if not cnt:
                    continue
                word, n = min(cnt.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))   # ties: the longer word
                if n < MIN_MSGS:
                    continue
                msgs = [m for m in msgs if word in _words(m["text"], 5)]
                name = word.capitalize() + (f" with {' and '.join(names[:2])}" if names else "")
                about = f"“{word}”"
            if not name:
                continue
            doms = {_domain(a) for a in addrs if "@" in a}
            if "slack" in srcs or (doms and doms - FREE_MAIL):
                side = "professional"
            else:
                side = "personal"
            who = (" with " + " and ".join(names[:2]) + (f" and {len(names) - 2} more" if len(names) > 2 else "")) \
                if names else ""
            src_words = sorted({T.SOURCES.get(s, ("", "Message"))[1] for s in srcs})
            out.append({"key": _key(kind, k), "name": name[:NAME_MAX], "side": side, "count": len(msgs),
                        "people": names, "why": f"{len(msgs)} messages{who} about {about}",
                        "sources": src_words, "last": max(m["ts"] for m in msgs)})
        return out

    def suggested(self):
        """{personal: [...], professional: [...]}: newest first, at most 6 a side, without the ones the owner added or said
        aren't projects, and without the names of projects the owner already has."""
        now = self.clock()
        marks = self._marks()
        have = [_words(p["name"]) for p in self.projects._rows(" WHERE state IN ('active','suggested','not')")]  # noqa: SLF001
        areas = self._areas()
        out = {"personal": [], "professional": []}
        for s in sorted(self._clusters(now), key=lambda s: -s["last"]):
            if s["key"] in marks:
                continue
            w = _words(s["name"])
            if w and any(h and (w <= h or h <= w) for h in have):
                continue
            side = out[s["side"]]
            if len(side) < MAX_SIDE:
                tz = self.projects._tz()  # noqa: SLF001
                s["ago"] = T.ago(s.pop("last"), now, tz)
                side.append(s)
        # the drafted ones from Find projects, sided by their area's name
        for p in self.projects._rows(" WHERE state='suggested' ORDER BY updated DESC LIMIT 12"):  # noqa: SLF001
            aname = (areas.get(p["area"]) or "").lower()
            side = "personal" if any(a in aname for a in PERSONAL_AREAS) else "professional"
            try:
                ppl = json.loads(p["people"] or "[]")
            except ValueError:
                ppl = []
            if len(out[side]) < MAX_SIDE:
                out[side].append({"key": p["id"], "pid": p["id"], "name": p["name"], "side": side, "count": None,
                                  "people": [T.first(x) for x in ppl[:4] if isinstance(x, str)],
                                  "why": "Found by “Find projects”", "sources": [], "ago": ""})
        return out

    def _find(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"s[0-9a-f]{12}", key):
            raise NotFound("There's no such suggestion.")
        for s in self._clusters(self.clock()):
            if s["key"] == key:
                return s
        raise NotFound("That suggestion has gone (its messages moved on).")

    def add(self, key, body=None):
        """The owner pressed Add on one suggestion: makes it a project (never on its own)."""
        if body not in (None, {}) and not isinstance(body, dict):
            raise InputError("Send a JSON object.")
        if key in self._marks():
            raise Conflict("You already answered this suggestion.")
        s = self._find(key)
        name = (body or {}).get("name") or s["name"]
        self.projects.create({"name": name, "people": s["people"][:12], "notes": s["why"] + "."})
        pid = self.projects._rows(" WHERE state='active' ORDER BY created DESC LIMIT 1")[0]["id"]  # noqa: SLF001
        self.ms.write(lambda db: db.execute("INSERT OR REPLACE INTO project_suggestion_marks VALUES (?,?,?,?)",
                                            (key, "added", pid, self.clock())))
        return {"ok": True, "pid": pid}

    def not_project(self, key, body=None):
        self._find(key)
        self.ms.write(lambda db: db.execute("INSERT OR REPLACE INTO project_suggestion_marks VALUES (?,?,?,?)",
                                            (key, "not", "", self.clock())))
        return {"ok": True}

    # --- Drive files on a project -------------------------------------------------------------------------------------
    def files(self, projects):
        """{pid: [{title, kind, link, ago, why}]}: Drive files linked by a shared name word or a project person."""
        rows = self._read("SELECT id, kind, title, people, area, ts, url FROM docs WHERE source='drive' "
                          "ORDER BY ts DESC LIMIT 2000")
        now = self.clock()
        tz = self.projects._tz()  # noqa: SLF001
        out = {}
        for p in projects:
            if p.get("state") != "active":
                continue
            w = _words(p["name"])
            ppl = {T.first(x).lower() for x in p.get("people") or [] if isinstance(x, str) and x.strip()}
            got = []
            for d in rows:
                link = safe_drive_link(d["url"])
                if not link:
                    continue
                why = ""
                if w and w & _words(d["title"]):
                    why = "Its name matches"
                else:
                    owners = {T.first(re.sub(r"\s*<[^>]*>", "", x)).lower() for x in str(d["people"] or "").split("; ")
                              if x.strip()}
                    if ppl and ppl & owners:
                        why = "Shared by " + next(iter(sorted(ppl & owners))).capitalize()
                if why:
                    got.append({"id": d["id"], "title": d["title"], "kind": KIND.get(d["kind"], "File"),
                                "link": link, "ago": T.ago(d["ts"], now, tz), "why": why})
                if len(got) >= MAX_FILES:
                    break
            if got:
                out[p["id"]] = got
        return out

    def view(self, _body=None):
        v = self.projects.view()
        return {"suggested": self.suggested(), "files": self.files(v["projects"])}
