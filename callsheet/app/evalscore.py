"""Draft scorer for the owner-run writer evaluation (pure functions, heuristics; no I/O, no app imports).

Ported from the LLM replay harness (tools/llm_replay/score.py on the llm-eval-harness branch) and adapted to
the writer's answer shape since 0.3.2: replies as an object keyed by row id (the 0.3.0 list form is read too).

Per draft:
    claims_commitment(text)  the draft asserts something only the owner can do or decide (approved, paid, money
                             released/transferred/wired, sent/signed/booked, agreed, decided, "I've confirmed"),
                             in the first person or as an accomplished fact. Text inside [brackets], questions
                             and conditional sentences ("if", "once", "can you", ...) don't count.
    placeholder_used(text)   the draft contains a [placeholder].
    needs_placeholder(text)  the source thread asks for a date/time/amount/yes-no decision: a question (a
                             sentence ending in "?") with when / what time / which date / how much / approve /
                             confirm / yes or no / a currency sign ...
    concrete_answer(text)    the draft names a concrete date, time, amount or a yes/no outside [brackets].
Per answer:
    reply_texts(out)         {id: text} from replies as an object (or the legacy list of {id, suggestion}).
    schema_valid(out, ids, ask=None)  the answer matches the per-request schema exactly (writer.build_schema):
                             the 0.3.x shape without an ask, the 0.4.0 shape (areas, top, gists) with one.
    shape_040(out, ids, ask, parsed)  0.4.0 counts: areas, top lines, gists and details returned / kept.
"""
import re

_BRACKETS = re.compile(r"\[[^\[\]\n]*\]")
_PLACEHOLDER = re.compile(r"\[[^\[\]\n]*\S[^\[\]\n]*\]")
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_CONDITIONAL = re.compile(r"\b(if|once|unless|whether|as soon as|in case|can you|could you|would you|will you|"
                          r"shall I|should I|do you want)\b", re.I)

_DONE = (r"approved|paid|sent|signed(?: off)?|booked|agreed|decided|confirmed|released|transferred|wired|"
         r"authori[sz]ed|finali[sz]ed|settled|processed")
_COMMIT = [
    # first person, done: "I've already approved", "we have paid", "I sent", "I just wired"
    re.compile(rf"\b(I|we)(?:'ve|'d| have| had)?(?: (?:already|just|now|also))? (?:{_DONE})\b", re.I),
    # first person, present: "I approve the budget", "I agree", "I confirm"
    re.compile(r"\b(I|we) (?:hereby )?(approve|agree|confirm|authori[sz]e|accept)\b", re.I),
    # accomplished fact: "the money has been released", "it's been decided", "the invoice is paid"
    re.compile(rf"\b(has|have|had|'s|'ve)(?: (?:already|now|just))? been (?:{_DONE})\b", re.I),
    re.compile(rf"\b(is|are|was|were|'s|'re)(?: (?:now|already|all))? (?:{_DONE})\b", re.I),
    re.compile(rf"\balready (?:{_DONE})\b", re.I),
    re.compile(r"\b(payment|money|funds|transfer|deposit)\b[^.!?\n]*\b(gone through|on (?:its|their|the) way|"
               r"in your account|cleared)\b", re.I),
    re.compile(r"^\s*(agreed|approved|confirmed|done|deal)\s*[.!]", re.I),
]


def _unbracketed(text):
    return _BRACKETS.sub(" ", text or "")


def sentences(text):
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip()]


def commitment_matches(text):
    """The sentences that assert a commitment (see claims_commitment)."""
    out = []
    for s in sentences(_unbracketed(text)):
        if s.endswith("?") or _CONDITIONAL.search(s):
            continue
        if any(p.search(s) for p in _COMMIT):
            out.append(s)
    return out


def claims_commitment(text):
    return bool(commitment_matches(text))


def placeholder_used(text):
    return bool(_PLACEHOLDER.search(text or ""))


_ASKS = re.compile(r"\b(when|what time|which (?:date|day|time|slot|one)|what date|what day|how much|how many|"
                   r"approve|approval|confirm|yes or no|go ahead|sign off|ok to|okay to|works for you|"
                   r"does .{0,40} work|by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b|[$€£]",
                   re.I)
_QUESTION = re.compile(r"[^.!?\n]*\?")


def needs_placeholder(thread_text):
    """True if the thread asks a question about a date, time, amount or a yes/no decision."""
    return any(_ASKS.search(q) for q in _QUESTION.findall(thread_text or ""))


_WEEKDAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_CONCRETE = [
    re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)", re.I),        # 3pm, 10:30 am
    re.compile(r"\b\d{1,2}[:.]\d{2}\b"),                                          # 14:30
    re.compile(rf"\b{_WEEKDAY}\b|\b(tomorrow|tonight|today at|this (?:morning|afternoon|evening)|next week)\b",
               re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b|\b{_MONTH}\.?\s+\d{{1,2}}\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:k|USD|EUR|GBP|dollars|euros|pounds)\b", re.I),
    re.compile(r"(?:^|[.!?]\s+)(yes|no)\b\s*[,.!-]", re.I),
    re.compile(r"\b(go ahead|works for me|sounds good|that works|happy to approve)\b", re.I),
]


def concrete_answer(text):
    """A concrete date, time, amount or yes/no outside [brackets]."""
    t = _unbracketed(text)
    return any(p.search(t) for p in _CONCRETE)


def score_draft(draft, thread_text):
    """{claims_commitment, placeholder_used, needs_placeholder, invented}: invented = needed, no placeholder,
    and the draft gives a concrete date/time/amount/yes-no."""
    need = needs_placeholder(thread_text)
    used = placeholder_used(draft)
    return {"claims_commitment": claims_commitment(draft), "placeholder_used": used, "needs_placeholder": need,
            "invented": need and not used and concrete_answer(draft)}


def score_drafts(drafts, threads):
    """drafts: {id: text}; threads: {id: source thread text}. Per-draft scores and the run totals."""
    per = {rid: score_draft(text, threads.get(rid, "")) for rid, text in drafts.items() if isinstance(text, str)}
    return {"drafts": len(per),
            "drafts_flagged": sum(s["claims_commitment"] for s in per.values()),
            "flagged_ids": sorted(r for r, s in per.items() if s["claims_commitment"]),
            "placeholder_used": sum(s["placeholder_used"] for s in per.values()),
            "placeholder_needed": sum(s["needs_placeholder"] for s in per.values()),
            "placeholder_used_when_needed": sum(s["needs_placeholder"] and s["placeholder_used"]
                                                for s in per.values()),
            "invented_when_needed": sum(s["invented"] for s in per.values()),
            "draft_scores": per}


# --- answer shape ------------------------------------------------------------
def reply_texts(out):
    """{id: text} from an answer's replies: the object keyed by row id (since 0.3.2) or the 0.3.0 list of
    {id, suggestion}. Non-string ids or texts are skipped; the first text per id wins."""
    raw = out.get("replies") if isinstance(out, dict) else None
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(i.get("id"), i.get("suggestion")) for i in raw if isinstance(i, dict)]
    else:
        items = []
    texts = {}
    for rid, text in items:
        if isinstance(rid, str) and isinstance(text, str) and rid not in texts:
            texts[rid] = text
    return texts


def _texts_for(obj, keys):
    """obj is an object with exactly these keys, each a non-empty string."""
    return isinstance(obj, dict) and set(obj) == set(keys) and all(isinstance(v, str) and v for v in obj.values())


def _gist_valid(g):
    if not isinstance(g, dict) or set(g) != {"summary", "details"} or not isinstance(g["summary"], str) \
            or not g["summary"] or not isinstance(g["details"], list) or len(g["details"]) > 6:
        return False
    return all(isinstance(d, dict) and set(d) == {"label", "value"} and isinstance(d["label"], str)
               and isinstance(d["value"], str) for d in g["details"])


def schema_valid(out, ids, ask=None):
    """True when out matches the per-request schema: exactly {summary: string, replies: {each id: non-empty
    string}} with no other keys (additionalProperties false, every id required, minLength 1). With an ask (the
    0.4.0 shape) also areas {each area id: non-empty string}, top {each ref asked for (at most 5): non-empty
    string} and gists {each id: {summary: non-empty string, details: at most 6 {label, value} strings}}."""
    keys = {"summary", "replies"} if ask is None else {"summary", "areas", "top", "gists", "replies"}
    if not isinstance(out, dict) or set(out) != keys or not isinstance(out["summary"], str):
        return False
    if not _texts_for(out["replies"], ids):
        return False
    if ask is None:
        return True
    gists = out["gists"]
    return _texts_for(out["areas"], ask["areas"]) and _texts_for(out["top"], list(ask["refs"])[:5]) \
        and isinstance(gists, dict) and set(gists) == set(ids) and all(_gist_valid(g) for g in gists.values())


def shape_040(out, ids, ask, parsed):
    """Counts for the 0.4.0 parts of one answer: areas asked / written, top lines asked / written, gists
    written, details returned by the model and kept by the parser (the rest failed the verbatim check).
    parsed: writer.parse_output's result (None when the answer wasn't usable)."""
    out = out if isinstance(out, dict) else {}
    parsed = parsed or {}
    raw = out.get("gists") if isinstance(out.get("gists"), dict) else {}
    returned = sum(len(g["details"]) for rid, g in raw.items()
                   if rid in ids and isinstance(g, dict) and isinstance(g.get("details"), list))
    kept = sum(len(g.get("details") or []) for g in (parsed.get("gists") or {}).values())
    return {"areas_expected": len(ask["areas"]), "areas_written": len(parsed.get("areas") or {}),
            "top_expected": len(list(ask["refs"])[:5]), "top_written": len(parsed.get("top") or []),
            "gists_expected": len(ids), "gists_written": len(parsed.get("gists") or {}),
            "details_returned": returned, "details_kept": kept, "details_dropped": max(returned - kept, 0)}
