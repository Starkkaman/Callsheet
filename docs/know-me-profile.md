# Know me, stage 3b: the drafted profile and the questions (0.6.0)

Code: `callsheet/app/know_profile.py` (named so it doesn't shadow Python's `profile` module), routes in the
"0.6.0 profile" section of `api.py`, UI in the "0.6.0 profile" sections of `app.js`/`app.css` (`#profile`,
`#profile-questions`), tests in `tests/test_profile_060.py`. Mockup: `mockups/knowme-v2.html` frames 3 to 5.

## Signal as an index source
`sources.SignalSource` reads the 0.5.1 Signal store (`signal_messages` joined to `signal_chats`, live and
imported messages) into the index: kind `message`/`sent`, title "Signal with X" / "Signal group X", people the
sender's name, body the text, area from `signal_meta()`, ignored chats left out. It does nothing when the store's
tables are missing or empty (`signal_ready`). Item ids are `<chat>:<sender>:<ts>` from the store's salted hashes, so
no number or uuid reaches the index. Scans filter by message time (the window); the 30-minute refresh also takes
messages whose `received_at` is after its last read, so history imported after a scan is indexed too. Messages
deleted from the store (ignored chats, deleted imports, unlinking, the 30-day limit) leave the index at the end of a
pass (`delete_missing`). Tested against the real store in `tests/test_release_060.py`.

## The draft (deterministic, no AI)
`build_draft` reads `Index.rows` (metadata only, never bodies) for the last year (calendar: 120 days back, 60 ahead):
- **People**: by email (mail, calendar) or name (Slack, Signal; merged into the email person with the same name).
  Counts: heard from, wrote to, threads, Slack/Signal messages, meetings; main area; automated senders
  (`rules.AUTOMATED_SENDER`) are never people. Top 12 by 2·wrote + heard + 2·meetings.
- **Reply habits**: an owner message answers the incoming ones since the owner's last message in that thread/DM
  (capped at 14 days); median per person and per area; the busiest two-hour window of sent messages; senders
  with 10+ emails and no reply; Slack channels with 50+ messages where the owner posted at most twice.
- **Recurring**: same title 3+ times, seen in the last 45 days: Mon–Fri, every day, weekly ("Mondays"), every
  other week, monthly by date ("1st of each month") or weekday ("First Monday of each month"), plus the time.
- **VIP candidates**: two-way and quick (5+ emails written, 3+ replies, median at most 4 hours) or 70%+ answered.
- **Projects**: words and word pairs shared by subject lines of threads the owner wrote in (and Obsidian note
  titles), minus stopwords, names and area names; overlapping groups merged; Active if seen in 21 days.
  Confidence by thread count (8+ Sure, 5+ Fairly sure). Abbreviations seen in 4+ subjects become questions.

## The AI pass (optional, suggestions only)
"Suggest roles and project names" and the questions use the configured writer (local first; Claude when
chosen or as the writer's fallback): no tools, the JSON schema as response format, thinking off, the owner's
timeout, one retry, count-only log lines. Refused (HTTP 409) within 10 minutes of a call time
(`Evaluator.near_call`), while a sync runs, or while another profile job runs. The system text is a fixed intro
(`SUGGEST_INTRO` / `ASK_INTRO`), then `SAFETY` and the schema; the user message is a fixed ask and the
aggregates as JSON in `<data>` with `<` and `>` escaped (`writer._block_json`). The model sees names, email
domains (not addresses), counts and at most 4 clipped subject lines per person or project. Answers are checked:
only keys of this draft, plain short strings, anything with a link or markup dropped.

## The memory boundary
The draft, the suggestions, the questions and the owner's removals are in `index.db` meta (`profile`), deleted
with the index. memory.md changes only on the owner's click: Save or Accept on a person, project, recurring
event or habit writes a **Profile** entry (`from: profile`, `ref: <key>`; people, emails and area come from the
draft on the server, never from the page or the model); an answer writes an **Answers** entry (`from: interview`,
text "Callsheet asked: ...\nAnswer: ..."; metadata from the gap) that `memory.relevant()` matches. Saving again
replaces the entry (`Memory.replace`); Remove deletes it.

## Questions
`gaps` lists what Callsheet couldn't work out: people without a role, people across areas, fast replies but
few, senders never answered, unexplained abbreviations, stalled projects. Up to 8 become questions (the AI words
them; area questions always offer the owner's areas). Without the AI, or when it fails, fixed rules word them.
Answered gaps are not asked again ("Ask me again").

## API
`GET api/profile`, `POST api/profile/build`, `POST api/profile/suggest`, `POST api/profile/questions`,
`POST api/profile/questions/<g...>` `{pick}|{text}|{skip}`, `POST|DELETE api/profile/(people|projects|items)/<key>`,
`POST .../<key>/restore`, `DELETE api/profile/(people|projects)/<key>/suggestion`,
`POST api/profile/people/<key>/vip {vip}`.
