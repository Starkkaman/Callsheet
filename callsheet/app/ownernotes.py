"""The owner's saved notes that may guide a model request (0.9.1, kept for Draft a reply in the rework): `for_prompt`
picks the owner's own saved notes (memory.md: Notes, Profile, Answers) that fit the rows (memory.relevant). A note that
reads like an instruction or a claim copied from a message (inject.instructions, writer.states_decision) is never
sent, even when the owner saved it anyway (`flagged`, `usable`). Message text never reaches the notes: chat proposals
are not in the file until the owner presses Save. The 0.9.1 demote-only sorting rules went with the sorter.
Pure functions; no I/O.
"""
import re
import inject
import memory
import writer
PROMPT_CAP = 1200           # characters of note text in one batch's <owner_notes> block
PROMPT_MAX = 8              # notes in one batch's block
NOTE_ID = re.compile(r"n[0-9a-f]{6,12}")
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

