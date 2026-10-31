# 0.5.0: notes to Callsheet and possible events

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
  `created` (YYYY-MM-DD), `from` (reply, event, briefing, heads-up, possible event), `ref` (the brief item the
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
every prompt was byte-identical to 0.4.1's up to 0.5.1; since 0.5.2 (chat safety, `docs/chat-safety-0.5.2.md`) the
fixed texts say "message" for "email" and the pinned shape is 0.5.2's. With notes the prompt differs only by
`NOTES_RULE` and the `<notes>` block (`tests/test_release_050_shape.py`). Both writers (Claude and local) and the writer evaluation
(`Syncer.writer_data`) get the notes.

### API (mutations need X-Callsheet: 1)
- `GET api/memory` returns `{text, notes, max, note_max, error, exists, rule, cap}`
- `PATCH api/memory` takes `{text}` (the whole file, validated)
- `POST api/memory/reset` takes `{confirm: true}` and restores the template
- `POST api/memory/notes` takes `{kind: reply|event|para|heads|pe, id, ref, text, item?}`. The metadata
  (people, sender address and domain, topics, area) comes from the cached brief and the row refs, never from
  the page.
- `PATCH api/memory/notes/<id>` takes `{text}`; `DELETE api/memory/notes/<id>` deletes the note
- The brief's `notes` field is `{ref: {id, text}}`, so the notepad marks items that have a note.

## Possible events (dates.py)

### Finding dates
Dates are found deterministically in each reply row's latest incoming message (Gmail subject and snippet, or
the Slack message), one sentence at a time, with at most 2 per message:
- A deadline (`facts.due`: "due Friday 6 Nov at 5pm", "by Thursday") that has a date counts. With a time it
  becomes the 30 minutes before that time; otherwise it is all day.
- Otherwise the sentence needs an event word (meet, call, lunch, coffee, appointment, "does Tuesday work",
  "how about", …) and a date: a weekday, "3 Nov", "the 5th", an ISO date, today or tomorrow. A time or range
  can come with it ("at 2pm", "9:30-10:15", "between 3 and 4pm"). A single time means 60 minutes, and no time
  means all day.
- These are left out:
  - a time with no date
  - "next Friday" (unclear which week)
  - "tonight" without a time
  - sentences about the past (yesterday, ago, last …, discussed, "thanks for the …")
  - questions asking the owner for a time ("What time works on Thursday?") when no time is proposed
  - dates before the brief's day or more than 120 days out
  - duplicates
- "Coffee Tuesday or Wednesday at 10am?" gives both days, each as proposed.

### Skipped when already on the calendar
The sync reads the calendar 14 days ahead in the same request; only today's events go into the schedule. A
candidate is skipped when the calendar has an event on the same day that overlaps it (both timed) or that
shares a title word. Cancelled events, free (transparent) events and events the owner declined don't count.

### On the page
The Agenda has a "Possible events" group (dashed cards) showing:
- the title (the mail subject, or "Lunch with Priya"; "Due: <subject>" or "Deadline from <who>")
- when ("Tue 3 Nov · 2 pm–3 pm", "Fri 6 Nov · due 5 pm", "· all day"; 12h/24h per the setting)
- the area, the source row (opens the reply) and the phrase as written
- "Add to calendar"

The link is built on the server like the Gmail links:
`https://calendar.google.com/calendar/render?action=TEMPLATE&text=…&dates=…&ctz=<owner tz>&details=…&authuser=<account>`
- Dates are local `YYYYMMDDTHHMMSS/…`, read in `ctz`. All-day dates are `YYYYMMDD/next day`, and 24:00 becomes
  the next day at 00:00.
- `details` contains who, the source and the phrase; it adds the Gmail link only when that is a
  mail.google.com URL.
- `authuser` is the mail account's address. Slack rows use the first account of the row's area.

The page opens the link only if it is https (`safeHttps`), with `target=_blank` and `rel="noopener noreferrer"`.
Callsheet has no calendar write scope: the owner saves the event on Google's page.
