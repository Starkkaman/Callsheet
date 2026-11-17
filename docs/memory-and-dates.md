# 0.5.0: notes to Callsheet

Status (0.21.0): the 0.5.0 possible events (`dates.py`) and the notes for AI sorting went with the rework; the
ledger keeps dates now (`docs/ledger.md`).

## Notes to Callsheet (memory)

### The file
`$CALLSHEET_DATA/memory.md` (`/data/memory.md` in the add-on), mode 0600, written atomically (temp file,
`os.replace`). It does not exist until the first note; until then it reads as the template. Plain Markdown, so
the owner can edit it in Setup → Delivery → Memory (or by hand). There are three sections, and later versions add
to the same file:

```markdown
# Callsheet memory
(a short explanation)

## Notes
### Priya Nair · 2026-10-30
- people: Priya Nair
- emails: priya@acme.example
- domains: acme.example
- topics: budget, sign-off
- area: Work
- created: 2026-10-30
- from: reply
- ref: 3f9a2c41d0e8b7a6
- id: n7c21e0f4

Budget questions can wait until Tuesday; keep replies short.

## Profile
(0.6: free text, kept as it is and not read yet)

## Answers
(0.6: interview answers, same entry format as notes, read like notes)
```

- Entry: a `### ` heading (the title), `- key: value` lines, a blank line, then the text (500 characters at
  most). Keys: `people`, `emails`, `domains`, `topics` (comma-separated lists, 12 at most), `area` (area name),
  `created` (YYYY-MM-DD), `from` (reply, event, briefing, heads-up, possible event, 0.8.1 chat), `ref` (the brief item the
  note was made on), `id` (`n` + hex). Accepted aliases: person, email, domain, topic, date, source.
- Limits: 20,000 characters, 200 entries, no control characters, valid emails, domains, dates and ids,
  unique ids. Errors are plain sentences with the line number. A file with an error isn't read for the
  writer, and Callsheet won't change it until it is fixed.
- Text lines that start with `#` are indented by one space when Callsheet writes them, so a note can't
  start a new section or entry.
- Obsidian export: each entry maps one to one onto a note with YAML front matter (the key lines are the
  front matter, the title is the file name, and the text is the body).

### Relevance (memory.relevant)
For each brief, the writer gets only the notes that apply to the rows and events it is sent:
- email = a row's sender address: 8 points
- person = a row's sender name, or one word of it for a one-word name; a full name in a subject or event
  title also counts: 6 points
- domain = the sender's domain: 4 points. Public mail domains (gmail.com and similar) never match, so a
  note whose only key is a public domain doesn't apply.
- topic = every word of the topic is in a subject or event title: 2 points each, up to 3 topics
- A note with none of those keys applies when its area has a row or event today (+1 point); a note with no
  keys at all applies every day.

Notes are ranked by score, newest first on ties. Each note's text is clipped to 400 characters, and notes are
added until about 1,500 characters in total.

### To the writer
`data["notes"]` = `[{text, about {people, area, topics}, applies_to [row/event ids], written}]` is taken out of the data
and sent in its own block after the facts: `<data>…</data>`, then `<facts>…</facts>`, then `<notes>…</notes>`.
The block is JSON with `<` and `>` escaped, so a mail that contains `</notes>` or `<data>` stays inside `<data>`.

`writer.NOTES_RULE`, a fixed sentence, follows the facts sentence only when notes are sent. Without notes,
every prompt was byte-identical to 0.4.1's up to 0.5.1; since 0.5.3 (chat safety, `docs/chat-safety-0.5.3.md`) the
fixed texts say "message" for "email" and the pinned shape is 0.5.3's. With notes the prompt differs only by
`NOTES_RULE` and the `<notes>` block (`tests/test_release_050_shape.py`). Both writers (Claude and local) and the writer evaluation
(`Syncer.writer_data`) get the notes.

### API (mutations need X-Callsheet: 1)
- `GET api/memory` returns `{text, notes, max, note_max, error, exists, rule, cap}`
- `PATCH api/memory` takes `{text}` (the whole file, validated)
- `POST api/memory/reset` takes `{confirm: true}` and restores the template
- `POST api/memory/notes` takes `{kind: reply|event|para|heads|pe, id, ref, text, item?}`. The metadata
  (people, sender address and domain, topics, area) comes from the cached brief and the row refs, never from
  the page. 0.8.1: `{kind: "chat", chat, turn, token, text, people, topics, area, confirm?}` saves a Chat Save
  card (one-time token, owner's Save only; see docs/chat.md "Remember").
- `PATCH api/memory/notes/<id>` takes `{text}`; `DELETE api/memory/notes/<id>` deletes the note
- The brief's `notes` field is `{ref: {id, text}}`, so the notepad marks items that have a note.
