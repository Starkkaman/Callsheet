"""Callsheet's memory (0.5.0): the owner's notes to Callsheet, kept as one Markdown file in the app's data
directory (`$CALLSHEET_DATA/memory.md`, 0600) that the owner can read and edit in Setup.

The file has a section per kind of entry, so later builds can add to the same memory:

    # Callsheet memory
    (a short explanation)

    ## Notes            notes from the notepad menu ("Note for Callsheet")
    ## Profile          reserved for 0.6's profile (free text, kept as it is, not read yet)
    ## Answers          reserved for 0.6's interview answers (same entry format as notes, read like notes)

An entry is a level-3 heading, its metadata as "- key: value" lines, a blank line, then the note's text:

    ### Jordan Lee · 2026-10-30
    - people: Jordan Lee
    - emails: jordan@twinlakes.example
    - topics: volunteer, rota
    - area: Twin Lakes
    - created: 2026-10-30
    - from: reply
    - ref: 3f9a2c41d0e8b7a6
    - id: n7c21e0f4

    Jordan's volunteer requests can wait until Sunday.

The metadata maps one to one onto YAML front matter, so an Obsidian export is one file per entry. The writer
gets only the entries relevant to today's data (relevant()), in the owner's <notes> block (writer.py).
Pure apart from the file I/O in Memory. No network.
"""
import datetime as dt
import os
import pathlib
import re
import secrets
import threading

FILENAME = "memory.md"
MAX_FILE = 20000          # characters in the whole file (Setup's textarea)
MAX_TEXT = 500            # characters in one note's text
MAX_ENTRIES = 200
MAX_VALUE = 300           # characters in one metadata value
MAX_LIST = 12             # items in a list value (people, emails, domains, topics)
NOTES_CAP = 1500          # characters of note text sent to the writer per brief
SENT_TEXT = 400           # one note's text as sent (longer ones are clipped)
SECTIONS = ("Notes", "Profile", "Answers")
ENTRY_SECTIONS = ("Notes", "Answers")
LIST_KEYS = ("people", "emails", "domains", "topics")
KEYS = LIST_KEYS + ("area", "created", "from", "ref", "id")
ALIASES = {"person": "people", "email": "emails", "domain": "domains", "topic": "topics", "date": "created",
           "source": "from"}
FROM_KINDS = ("reply", "event", "briefing", "heads-up", "possible event", "setup", "interview")

HEADER = """# Callsheet memory

What you told Callsheet. Before each brief it reads the notes that fit the day's mail, chat and calendar
(the people, their email domains, the area and the topic words) and gives them to the writer as your notes.
Edit freely. Each note starts with "### " and a title, then "- key: value" lines, a blank line and the note.
Keys: people, emails, domains, topics (comma-separated), area, created (YYYY-MM-DD), from, ref, id.
A note without people, emails, domains or topics applies to its whole area; one without any keys, to every day.
"""
TEMPLATE = HEADER + """
## Notes

## Profile

## Answers
"""

# public mail providers: a shared domain says nothing about the sender, so notes don't match on it
PUBLIC_DOMAINS = frozenset((
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "gmx.de", "web.de", "mail.com", "zoho.com", "fastmail.com", "hey.com", "yandex.com", "qq.com"))
STOP = frozenset("""a about above after again all also am an and any are as at be been before being below between
both but by can could did do does doing done down during each few for from further had has have having he her here
hers him his how i if in into is it its just me more most my no nor not now of off on once only or other our out
over own re fwd fw same she should so some such than that the their them then there these they this those through
to too under until up very was we were what when where which while who whom why will with would you your yours
please thanks thank hi hello hey dear regards update updates question quick reminder follow following call meeting
today tomorrow tonight week weekend next last monday tuesday wednesday thursday friday saturday sunday mon tue wed
thu fri sat sun january february march april may june july august september october november december jan feb mar
apr jun jul aug sep sept oct nov dec new info re: fyi""".split())

_EMAIL = re.compile(r"[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+")
_DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}")
_WORD = re.compile(r"[a-z0-9][a-z0-9'&-]*", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_META = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")
_ID = re.compile(r"n[0-9a-f]{6,12}")
_REF = re.compile(r"[\w:.-]{1,80}")


class MemoryError_(ValueError):
    """The memory file (or a note) isn't valid. The message is a plain sentence for the owner."""


def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def words(text):
    """Topic words of a text: lower-case words of 3+ characters that aren't common words, plural "s" dropped."""
    out = []
    for w in _WORD.findall(str(text or "").lower()):
        w = w.strip("'-&")
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in out:
            out.append(w)
    return out


def _norm_name(s):
    return " ".join(str(s or "").lower().split())


# --- parsing and validation ---------------------------------------------------------------------------------
def parse(text):
    """The memory file as {"sections": {name: {"entries": [...], "text": raw}}, "entries": [all entries in Notes
    and Answers]}. Each entry: {section, title, line, meta {key: value or list}, text}. Raises MemoryError_ with the
    first problem (line numbers start at 1)."""
    if not isinstance(text, str):
        raise MemoryError_("Send the memory as text.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_FILE:
        raise MemoryError_(f"The memory is {len(text):,} characters; the most it can hold is {MAX_FILE:,}. "
                           "Delete some notes.")
    m = _CTRL.search(text)
    if m:
        raise MemoryError_(f"Line {text.count(chr(10), 0, m.start()) + 1} has a control character. Remove it.")
    sections, entries, cur_sec, cur = {}, [], None, None

    def close():
        if cur is None:
            return
        body = "\n".join(cur.pop("_lines")).strip()
        if not body:
            raise MemoryError_(f"The note \"{_clip(cur['title'], 40)}\" (line {cur['line']}) has no text. Write the "
                               "note under its key lines, or delete the heading.")
        if len(body) > MAX_TEXT:
            raise MemoryError_(f"The note \"{_clip(cur['title'], 40)}\" (line {cur['line']}) is {len(body)} characters;"
                               f" a note can have {MAX_TEXT}.")
        cur["text"] = body
        entries.append(cur)

    for n, line in enumerate(text.split("\n"), 1):
        if line.startswith("## "):
            close()
            cur = None
            cur_sec = line[3:].strip()
            sections.setdefault(cur_sec, {"entries": [], "line": n})
            continue
        if line.startswith("### ") and cur_sec in ENTRY_SECTIONS:
            close()
            title = line[4:].strip()
            if not title:
                raise MemoryError_(f"Line {n}: give the note a title after \"### \".")
            cur = {"section": cur_sec, "title": title[:120], "line": n, "meta": {}, "_lines": [], "_body": False}
            continue
        if cur is None:
            continue
        if not cur["_body"]:
            mm = _META.match(line)
            if mm:
                key = mm.group(1).strip().lower()
                key = ALIASES.get(key, key)
                if key not in KEYS:
                    raise MemoryError_(f"Line {n}: \"{_clip(mm.group(1), 30)}\" isn't a key Callsheet knows. Use "
                                       + ", ".join(KEYS) + ".")
                cur["meta"][key] = _value(key, mm.group(2), n)
                continue
            if not line.strip():
                if cur["meta"] or cur["_lines"]:
                    cur["_body"] = True
                continue
            cur["_body"] = True
        cur["_lines"].append(line)
    close()
    if len(entries) > MAX_ENTRIES:
        raise MemoryError_(f"The memory has {len(entries)} notes; it can hold {MAX_ENTRIES}. Delete some.")
    ids = set()
    for e in entries:
        nid = e["meta"].get("id")
        if nid:
            if nid in ids:
                raise MemoryError_(f"Line {e['line']}: two notes have the id {nid}. Delete one of the id lines.")
            ids.add(nid)
        sections.setdefault(e["section"], {"entries": [], "line": 0})["entries"].append(e)
    for e in entries:
        e.pop("_body", None)
    return {"sections": sections, "entries": entries}


def _value(key, raw, n):
    raw = raw.strip()
    if len(raw) > MAX_VALUE:
        raise MemoryError_(f"Line {n}: the {key} line is too long ({MAX_VALUE} characters at most).")
    if key in LIST_KEYS:
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if len(items) > MAX_LIST:
            raise MemoryError_(f"Line {n}: at most {MAX_LIST} {key}.")
        if key == "emails":
            items = [x.lower().strip("<>") for x in items]
            bad = next((x for x in items if not _EMAIL.fullmatch(x)), None)
            if bad:
                raise MemoryError_(f"Line {n}: \"{_clip(bad, 40)}\" doesn't look like an email address.")
        if key == "domains":
            items = [x.lower().lstrip("@") for x in items]
            bad = next((x for x in items if not _DOMAIN.fullmatch(x)), None)
            if bad:
                raise MemoryError_(f"Line {n}: \"{_clip(bad, 40)}\" doesn't look like a domain (e.g. example.com).")
        if key == "topics":
            items = [x.lower() for x in items]
        return items
    if key == "created" and raw:
        try:
            dt.date.fromisoformat(raw)
        except ValueError:
            raise MemoryError_(f"Line {n}: write the created date as YYYY-MM-DD.") from None
    if key == "id" and raw and not _ID.fullmatch(raw):
        raise MemoryError_(f"Line {n}: an id looks like n1a2b3c4 (or delete the line).")
    if key == "ref" and raw and not _REF.fullmatch(raw):
        raise MemoryError_(f"Line {n}: the ref line can only have letters, digits and the marks : . - _ "
                           "(or delete the line).")
    return raw


def entry_out(e):
    """The public view of an entry (api/memory, the brief's note marks)."""
    m = e["meta"]
    return {"id": m.get("id") or None, "section": e["section"], "title": e["title"], "text": e["text"],
            "people": m.get("people") or [], "emails": m.get("emails") or [], "domains": m.get("domains") or [],
            "topics": m.get("topics") or [], "area": m.get("area") or "", "created": m.get("created") or "",
            "from": m.get("from") or "", "ref": m.get("ref") or ""}


# --- writing ------------------------------------------------------------------------------------------------
def _line_safe(s, n=MAX_VALUE):
    """One metadata value: one line, no commas inside list items handled by the caller."""
    return _clip(str(s or "").replace("\n", " "), n)


def render_entry(title, meta, text):
    lines = ["### " + _line_safe(title, 120)]
    for k in KEYS:
        v = meta.get(k)
        if not v:
            continue
        if k in LIST_KEYS:
            v = ", ".join(_line_safe(x, 80).replace(",", " ") for x in v[:MAX_LIST])
        lines.append(f"- {k}: {_line_safe(v)}")
    body = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    # a line in the text that looks like a heading would start a new section or note: indent it by a space
    body = "\n".join(" " + ln if ln.startswith("#") else ln for ln in body.split("\n"))
    return "\n".join(lines) + "\n\n" + body + "\n"


def _insert(text, section, block):
    """text with block added at the end of the section (the section is added at the end when missing)."""
    lines = text.rstrip("\n").split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## ") and ln[3:].strip() == section), None)
    if start is None:
        return "\n".join(lines) + "\n\n## " + section + "\n\n" + block
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    head = lines[:end]
    while head and not head[-1].strip():
        head.pop()
    tail = lines[end:]
    return "\n".join(head) + "\n\n" + block + ("\n" + "\n".join(tail) + "\n" if tail else "")


def _entry_span(text, nid):
    """(first line index, end line index) of the entry with id nid, or None."""
    lines = text.split("\n")
    for e in parse(text)["entries"]:
        if e["meta"].get("id") == nid:
            start = e["line"] - 1
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("### ", "## "))), len(lines))
            return start, end
    return None


def new_id(text=""):
    while True:
        nid = "n" + secrets.token_hex(4)
        if nid not in text:
            return nid


class Memory:
    """The memory file on disk. One lock for every read-change-write."""

    def __init__(self, data_dir):
        self.path = pathlib.Path(data_dir) / FILENAME
        self.lock = threading.Lock()

    def read(self):
        """The file's text (the template when there is no file yet)."""
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return TEMPLATE
        except (OSError, UnicodeDecodeError) as e:
            raise MemoryError_("Callsheet couldn't read its memory file. Check the app log.") from e

    def _write(self, text):
        tmp = self.path.with_name(FILENAME + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def entries(self):
        """Every entry of Notes and Answers, or [] when the file can't be read or parsed (a brief never fails
        on its memory)."""
        try:
            return parse(self.read())["entries"]
        except MemoryError_:
            return []

    def view(self):
        text = self.read()
        try:
            entries, error = [entry_out(e) for e in parse(text)["entries"]], None
        except MemoryError_ as e:
            entries, error = [], str(e)
        return {"text": text, "notes": entries, "max": MAX_FILE, "note_max": MAX_TEXT, "error": error,
                "exists": self.path.exists()}

    def save(self, text):
        """Replaces the whole file after validating it. Returns view()."""
        with self.lock:
            if not isinstance(text, str):
                raise MemoryError_("Send the memory as text.")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            parse(text)
            if not text.strip():
                text = TEMPLATE
            self._write(text if text.endswith("\n") else text + "\n")
        return self.view()

    def reset(self):
        with self.lock:
            self._write(TEMPLATE)
        return self.view()

    def add(self, title, meta, text, section="Notes"):
        """Adds an entry (a new id is set). Returns the entry's public view."""
        text = _check_text(text)
        with self.lock:
            cur = self.read()
            parse(cur)  # the owner's file must be valid before Callsheet changes it
            meta = dict(meta, id=new_id(cur))
            out = _insert(cur, section, render_entry(title, meta, text))
            parsed = parse(out)
            self._write(out)
        return entry_out(next(e for e in parsed["entries"] if e["meta"].get("id") == meta["id"]))

    def update(self, nid, text):
        """Changes the text of the entry with id nid (its metadata stays). Returns its public view."""
        text = _check_text(text)
        with self.lock:
            cur = self.read()
            span = _entry_span(cur, nid)
            if span is None:
                raise LookupError("That note isn't in the memory any more. Reload the page.")
            e = next(x for x in parse(cur)["entries"] if x["meta"].get("id") == nid)
            lines = cur.split("\n")
            block = render_entry(e["title"], e["meta"], text).rstrip("\n").split("\n") + [""]
            out = "\n".join(lines[:span[0]] + block + lines[span[1]:])
            parse(out)
            self._write(out)
        return entry_out(next(x for x in parse(out)["entries"] if x["meta"].get("id") == nid))

    def delete(self, nid):
        with self.lock:
            cur = self.read()
            span = _entry_span(cur, nid)
            if span is None:
                raise LookupError("That note isn't in the memory any more. Reload the page.")
            lines = cur.split("\n")
            out = "\n".join(lines[:span[0]] + lines[span[1]:])
            out = re.sub(r"\n{3,}", "\n\n", out)
            parse(out)
            self._write(out)
        return {"ok": True}


def _check_text(text):
    if not isinstance(text, str) or not text.strip():
        raise MemoryError_("Write the note first.")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_TEXT:
        raise MemoryError_(f"A note can have {MAX_TEXT} characters; this one has {len(text)}.")
    if _CTRL.search(text):
        raise MemoryError_("The note has a control character. Remove it.")
    return text


# --- relevance ----------------------------------------------------------------------------------------------
def relevant(entries, rows, events, cap=NOTES_CAP):
    """The notes for today's writer input, most relevant first, their texts together at most cap characters.
    rows: [{id, who, email (mail sender, lower case, or ""), domain, area (name), subject}] (the reply rows sent);
    events: [{id, title, area (name)}] (the events sent). An entry applies when one of its specific keys matches:
    an email equals a row's sender, a domain equals a sender's domain (public mail domains never match), a
    person's name is a row's sender name (or one word of it, for a one-word name; a full name may also appear in
    a subject or event title), or a topic word (or every word of a topic phrase) is in a subject or event title.
    An entry with none of these keys applies when its area has a row or event today; one with no keys at all
    applies every day. Ranked by how specific the match is (email 8, person 6, domain 4, topic 2 each up to 3,
    area 1), newest first on ties. Returns [{text, about {people, area, topics}, applies_to [ids], written}]."""
    rows, events = list(rows or ()), list(events or ())
    areas_today = {_norm_name(x.get("area")) for x in rows + events if x.get("area")}
    items = [(x["id"], set(words(x.get("subject") if "subject" in x else x.get("title"))),
              _norm_name(x.get("subject") if "subject" in x else x.get("title")), x) for x in rows + events]
    picked = []
    for e in entries:
        m = e["meta"]
        emails = [x.lower() for x in m.get("emails") or []]
        all_domains = [x.lower() for x in m.get("domains") or []]
        domains = [x for x in all_domains if x not in PUBLIC_DOMAINS]
        people = [_norm_name(x) for x in m.get("people") or [] if _norm_name(x)]
        topics = [x for x in m.get("topics") or [] if words(x)]
        area = _norm_name(m.get("area"))
        score, hits = 0, []
        for rid, ws, low, x in items:
            s = 0
            if emails and x.get("email") and x["email"].lower() in emails:
                s += 8
            if people and x.get("who") is not None:
                who = _norm_name(x.get("who"))
                if any(p == who or (" " not in p and p in who.split()) for p in people):
                    s += 6
            if people and any(" " in p and p in low for p in people):
                s += 6
            if domains and x.get("domain") and x["domain"].lower() in domains:
                s += 4
            if topics:
                n = sum(1 for t in topics if set(words(t)) <= ws)
                s += 2 * min(n, 3)
            if s:
                score = max(score, s)
                hits.append(rid)
        specific = bool(emails or all_domains or people or topics)  # a public domain alone matches nothing
        if specific and not hits:
            continue
        if not specific:
            if area and area not in areas_today:
                continue
            if area:
                hits = [rid for rid, _, _, x in items if _norm_name(x.get("area")) == area]
        if area and area in areas_today:
            score += 1
        picked.append((score, m.get("created") or "", e, hits))
    picked.sort(key=lambda p: p[1], reverse=True)  # newest first on ties (stable sort below)
    picked.sort(key=lambda p: -p[0])
    out, used = [], 0
    for score, created, e, hits in picked:
        text = e["text"] if len(e["text"]) <= SENT_TEXT else e["text"][: SENT_TEXT - 1].rstrip() + "…"
        if used + len(text) > cap:
            continue
        used += len(text)
        m = e["meta"]
        about = {k: v for k, v in (("people", m.get("people") or []), ("area", m.get("area") or ""),
                                   ("topics", m.get("topics") or [])) if v}
        item = {"text": text, "about": about, "applies_to": hits[:10]}
        if created:
            item["written"] = created
        out.append(item)
    return out


# --- a note from a brief item ---------------------------------------------------------------------------------
def meta_for(kind, item, ref, now, area_name="", sender=None, domain=None, account_email=None):
    """The metadata and title of a new note on a brief item. kind: reply | event | para | heads | pe; item: the
    brief row (reply row, schedule row, possible event) or the heads-up text; sender/domain: a mail row's sender
    address and domain (None for Slack). Returns (title, meta)."""
    created = now.date().isoformat()
    meta = {"created": created, "ref": ref if isinstance(ref, str) and _REF.fullmatch(ref) else ""}
    if area_name:
        meta["area"] = area_name
    if kind in ("reply", "pe"):
        who = str(item.get("who") or "").strip()
        if who and who != sender:
            meta["people"] = [who]
        if sender and _EMAIL.fullmatch(sender):
            meta["emails"] = [sender.lower()]
        if domain and domain.lower() not in PUBLIC_DOMAINS and _DOMAIN.fullmatch(domain.lower()):
            meta["domains"] = [domain.lower()]
        meta["topics"] = words(re.sub(r"^\s*(?:re|fwd?|aw|wg|sv)\s*:\s*", "", str(item.get("text") or item.get("title")
                                                                                 or ""), flags=re.I))[:5]
        meta["from"] = "reply" if kind == "reply" else "possible event"
        title = who or sender or "Note"
    elif kind == "event":
        meta["topics"] = words(item.get("title"))[:5]
        meta["from"] = "event"
        title = str(item.get("title") or "Event")
    elif kind == "heads":
        meta["topics"] = words(item)[:5]
        meta["from"] = "heads-up"
        title = "Heads up"
    else:  # para: an area's summary; area only, so it applies to the area
        meta["from"] = "briefing"
        title = area_name or "Briefing"
    if not meta.get("topics"):
        meta.pop("topics", None)
    return _clip(title, 60) + " · " + created, meta
