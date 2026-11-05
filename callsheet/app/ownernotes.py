"""0.9.1: the owner's notes in AI sorting (docs/sorting.md "Your notes").

Two layers:
- The model: `for_prompt` picks the owner's own saved notes (memory.md: Notes, Profile, Answers) that fit a batch's
  cards (memory.relevant) and triage sends them as the trusted <owner_notes> block in the system message. A note that
  reads like an instruction or a claim copied from a message (inject.instructions, writer.states_decision) is never
  sent, even when the owner saved it anyway. Nothing else can reach the block: chat proposals and Know me drafts are
  not in the file until the owner presses Save, and message text never is.
- The rules: `rule_of` reads a note that plainly says its sender doesn't need a reply (or is FYI, can wait, isn't
  important, should be ignored) as an owner rule, keyed by the note's own emails, people and domains and the
  addresses the owner typed in it, never by message content. `apply` uses the rules after the model, when the brief
  is read: it only demotes (Needs you -> For your info or Sorted), never hides and never promotes, and it never moves
  a security alert, an invite, flagged mail, or a VIP unless the note names that VIP. Notes with a condition
  ("unless", "except", "but", "if", ...) or an opposite wish ("urgent", "always needs a reply") make no rule: the
  model reads them.
Pure functions; no I/O.
"""
import re

import inject
import learn
import memory
import writer

FYI = "for_your_info"
SORTED = {"promo": "sorted_promos", "update": "sorted_updates", None: "probably_not_for_you"}
PROMPT_CAP = 1200           # characters of note text in one batch's <owner_notes> block
PROMPT_MAX = 8              # notes in one batch's block
DOUBT = learn.DOUBT - {"hidden_text"}   # a rule is keyed by a verified sender; hidden HTML text can't change who
SCOPES = ("item", "sender", "domain")
NOTE_ID = re.compile(r"n[0-9a-f]{6,12}")

_V = r"(?:need|needs|require|requires|want|wants|get|gets)"
_NOT = r"(?:does\s*n[o']?t|do\s*n[o']?t|did\s*n[o']?t|never|won'?t|will\s+not|no\s+longer|not)"
_FYI = re.compile(
    r"\b(?:" + _NOT + r"\s+(?:ever\s+|really\s+|usually\s+)?" + _V + r"\s+(?:a\s+|an\s+|any\s+|my\s+)?"
    r"(?:reply|replies|response|responses|answer|answers|follow[- ]?ups?)"
    r"|no\s+(?:reply|replies|response|answer)s?\s+(?:is\s+|are\s+)?(?:needed|required|necessary|expected)"
    r"|no\s+need\s+(?:for\s+me\s+)?to\s+(?:reply|respond|answer|write\s+back)"
    r"|nothing\s+(?:from|by|sent\s+by)\s+\S+(?:\s+\S+){0,4}?\s+(?:ever\s+)?needs?\s+(?:a\s+|an\s+|any\s+)?"
    r"(?:reply|response|answer)"
    r"|(?:can|could)\s+(?:always\s+|safely\s+)?wait"
    r"|not\s+urgent|never\s+urgent|low[- ]priority"
    r"|(?:just|only|purely|always)\s+(?:an?\s+)?fyi|fyi\s+only|(?:are|is)\s+(?:just\s+)?fyi"
    r"|(?:just|only|purely)\s+for\s+(?:my\s+)?info(?:rmation)?|for\s+(?:my\s+)?info(?:rmation)?\s+only)\b", re.I)
_SORTED = re.compile(
    r"\b(?:ignore|ignored|not\s+important|unimportant|never\s+important|(?:do\s*n[o']?t|never)\s+care"
    r"|irrelevant|not\s+relevant|not\s+for\s+me|nothing\s+to\s+do\s+with\s+me)\b", re.I)
_NEG = re.compile(r"\b(?:do\s*n[o']?t|never|not|stop|no\s+longer)\s+(?:ever\s+)?ignor", re.I)
_COND = re.compile(r"\b(?:unless|except|but|if|when|whenever|until|sometimes|usually|mostly|often|maybe|perhaps|"
                   r"apart\s+from|other\s+than)\b", re.I)
_OPPOSE = re.compile(r"\b(?:urgent|important|asap|right\s+away|immediately|priority|always\s+" + _V +
                     r"|" + _V + r"\s+(?:a\s+|an\s+)?(?:reply|response|answer))\b", re.I)
_KINDS = re.compile(r"\b(?:newsletters?|promos?|promotions?|promotional|marketing|offers?|deals?|sales|digests?|"
                    r"updates?|notifications?|receipts?|automated|automatic|bulk|mailing\s+lists?|"
                    r"announcements?|no-?reply)\b", re.I)
_ITEM = re.compile(r"^\W*(?:this|it)\b(?!\s+(?:sender|person|company|domain|address|list|newsletter|kind|type|sort))"
                   r"|\b(?:this|that)\s+(?:e-?mail|message|mail|thread|one)\b", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])([a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})\b", re.I)
_DOMAIN = re.compile(r"(?<![\w@.-])@?((?:[a-z0-9-]+\.)+(?:com|org|net|io|co|uk|de|fr|nl|eu|us|ca|au|app|dev|ai|edu|"
                     r"gov|info|biz|me|tv|ly|xyz|ch|se|no|dk|fi|es|it|ie|be|at|nz|in|jp))\b(?![\w@-])", re.I)


def flagged(text):
    """Does a note read like an instruction or a claim copied from a message? Those never guide sorting."""
    t = str(text or "")
    try:
        return bool(inject.instructions(t)) or bool(writer.states_decision(t))
    except Exception:  # noqa: BLE001 (a check that fails keeps the note out)
        return True


def usable(entries):
    """The owner's saved notes that may guide sorting: every entry of memory.md (Notes, Profile, Answers: only what
    the owner saved) with text, except the ones `flagged`."""
    return [e for e in entries or () if str(e.get("text") or "").strip() and not flagged(e.get("text"))]


def for_prompt(entries, rows, directory=None, cap=PROMPT_CAP):
    """The <owner_notes> block's notes for one batch: [{note: "n1", text, about, applies_to [card ids], written}],
    the notes memory.relevant picks for the cards (rows: [{id: card id, who, email, domain, area, subject,
    person}]), PROMPT_MAX at most. Returns (notes, {"n1": the note's id in memory.md or ""})."""
    picked = memory.relevant(usable(entries), rows, [], cap=cap, people=directory, ids=True)[:PROMPT_MAX]
    notes, ids = [], {}
    for i, n in enumerate(picked, 1):
        nid = n.pop("id", "") or ""
        notes.append(dict({"note": f"n{i}"}, **n))
        ids[f"n{i}"] = nid if NOTE_ID.fullmatch(nid) else ""
    return notes, ids


def level_of(text):
    """"fyi" (doesn't need a reply, can wait, FYI), "sorted" (ignore, not important) or None (no plain rule: a
    condition, an opposite wish, a negated "ignore", or nothing of the kind)."""
    t = " ".join(str(text or "").split())
    if not t or _COND.search(t) or _NEG.search(t):
        return None
    fyi, srt = list(_FYI.finditer(t)), list(_SORTED.finditer(t))
    if not fyi and not srt:
        return None
    rest = t
    for m in sorted(fyi + srt, key=lambda m: -m.start()):
        rest = rest[:m.start()] + " " + rest[m.end():]
    if _OPPOSE.search(rest):
        return None
    return "sorted" if srt else "fyi"


def rule_of(entry, directory=None):
    """One note as an owner rule, or None. {id, text, level, scope (item|sender|domain), ref, emails, pids,
    domains, kinds_only}. Scope: "this email/message/one" (or a note starting "This ...") is the item it was written
    on (its ref); addresses the owner typed in the note, else its emails and people, are senders; else a domain the
    owner typed, else its domains (never a public mail domain). Topic-only and area-only notes make no rule."""
    text = str(entry.get("text") or "")
    level = level_of(text)
    if level is None or flagged(text):
        return None
    m = entry.get("meta") or {}
    typed = [x.lower() for x in _EMAIL.findall(text)]
    emails = typed or [str(x).lower() for x in m.get("emails") or [] if "@" in str(x)]
    t_dom = [x.lower().lstrip("@") for x in _DOMAIN.findall(_EMAIL.sub(" ", text))]
    domains = t_dom or [str(x).lower() for x in m.get("domains") or []]
    domains = [d for d in domains if d not in memory.PUBLIC_DOMAINS]
    pids = set()
    if directory is not None and not typed:
        try:
            pids = set(directory.note_people(emails, m.get("people") or []))
        except Exception:  # noqa: BLE001
            pids = set()
    ref = str(m.get("ref") or "")
    if _ITEM.search(text) and ref and not typed and not t_dom:
        scope = "item"
    elif emails or pids:
        scope = "sender"
    elif domains:
        scope = "domain"
    else:
        return None
    nid = str(m.get("id") or "")
    return {"id": nid if NOTE_ID.fullmatch(nid) else "", "text": text if len(text) <= 300 else text[:299] + "…",
            "level": level, "scope": scope, "ref": ref, "emails": sorted(set(emails)), "pids": sorted(pids),
            "domains": sorted(set(domains)), "kinds_only": bool(_KINDS.search(text)),
            "written": str(m.get("created") or "")}


def rules_of(entries, directory=None):
    """The owner rules of all notes, the most specific first (item, sender, domain), newest first within each."""
    out = [r for r in (rule_of(e, directory) for e in entries or ()) if r]
    out.sort(key=lambda r: r["written"], reverse=True)
    out.sort(key=lambda r: SCOPES.index(r["scope"]))
    return out


def _bulkish(row):
    return (row.get("pre") in ("promo", "update") or row.get("ai") in ("promo", "automated_update")
            or bool({"bcc_or_list", "automated"} & set(row.get("codes") or ())))


def _kind(row):
    if row.get("ai") == "promo" or (row.get("ai") is None and row.get("pre") == "promo"):
        return "promo"
    if row.get("ai") == "automated_update" or (row.get("ai") is None and row.get("pre") == "update"):
        return "update"
    return None


def _names_sender(rule, ident):
    return bool(ident) and (ident.get("sender") in rule["emails"] or
                            bool(ident.get("person") and ident["person"] in rule["pids"]))


def _matches(rule, row, ident, rid):
    if rule["kinds_only"] and not _bulkish(row):
        return False
    if rule["scope"] == "item":
        return bool(rule["ref"]) and rule["ref"] in (rid, row.get("key"))
    if rule["scope"] == "sender":
        return _names_sender(rule, ident)
    dom = (ident or {}).get("domain") or ""
    return bool(dom) and any(dom == d or dom.endswith("." + d) for d in rule["domains"])


def apply(group, row, ident, rules, rid=None):
    """(group, note) after the model's (and the rules') group, for one brief row. note is None (no rule fits) or
    {id, text, scope, level, how}: how "note" (moved by the note), "agrees" (already there or lower: notes never
    promote) or "blocked" (with `reason`: security, invite, flagged, vip, rules). rid: the brief row id (item
    rules). row: the sorting row (overrides, marks, pre, strength, ai, codes). Never hides: the row keeps a
    section, only a lower one."""
    if group not in learn.RANK or DOUBT & set(row.get("marks") or ()):
        return group, None
    rule = next((r for r in rules or () if _matches(r, row, ident, rid)), None)
    if rule is None:
        return group, None
    note = {"id": rule["id"], "text": rule["text"], "scope": rule["scope"], "level": rule["level"]}
    target = FYI if rule["level"] == "fyi" else SORTED[_kind(row)]
    shown = "needs_you" if row.get("overrides") else group   # an override keeps a row in Needs you (triage.section_of)
    if learn.RANK[target] <= learn.RANK[shown]:
        return group, dict(note, how="agrees")
    ov = set(row.get("overrides") or ())
    reason = ("security" if group == "heads_up_security" or "security" in ov or row.get("ai") == "security" else
              "invite" if group == "your_day" or "invite" in ov else
              "flagged" if "flagged" in ov else
              "vip" if "vip" in ov and not (rule["scope"] != "domain" and _names_sender(rule, ident)) else
              "rules" if rule["scope"] == "domain" and (ov & {"known_to", "owner_named"} or
                                                        (row.get("pre") == "reply" and row.get("strength") == "strong"))
              else None)
    if reason:
        return group, dict(note, how="blocked", reason=reason)
    return target, dict(note, how="note")
