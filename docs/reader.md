# Reader (0.12.0)

The design's stage "Reader" (Wheatley design 2.4 D, there called 0.10.1; it ships as 0.12.0 because 0.10.1 was taken
by Reports). Code: `callsheet/app/reader.py`; tests: `tests/test_reader_0120.py`.

## What it does

For every item the sorter places where the owner sees it (`triage.SURFACED`: Needs you, Your day, What changed, For
your info, security), the reader sends **one request** to the owner's local model (the sorter's model setting, the
sorter's slot `triage.slot_for`) and stores a checked answer:

- `gist`: what this is (at most 160 characters);
- `why_you`: what they need from the owner, or why it matters (at most 160);
- `ctx`: the context ids it used (c1..c8, only this item's);
- `kind`: reply, meeting_request, meeting_change, bill_due, renewal, payment, security, service_alert, delivery, info,
  document, launch, update, news, video, other;
- `due`: `date` (YYYY-MM-DD or null), `date_s` (the sentence that says it), `amount_s` (the sentence that states an
  amount; the model never writes an amount);
- `importance`, `confidence`.

## Flow

1. The sorter places an item (`Sorter._batch`) and hands it to `Reader.enqueue` (surfaced groups only). The queue
   (`reader_queue`) keeps references: key, thread hash, index id, Gmail account and message ids, times. The text stays
   in memory (`Reader.srcs`); after a restart a sync gives it back (`Sorter.collect` → `Reader.catch_up`), or the index
   copy is read again. `catch_up` also queues surfaced items sorted before the reader existed (3 days back).
2. After the sorter's pass (`Sorter.tick` → `read_after`, only when the sorter has nothing more it could do now) the
   reader runs (`run_pass`), newest first, one item at a time.
3. The thread hash (mail: the message, the thread and the earlier messages' times; other items: the content hash)
   decides re-reading: the same hash is never read twice; a new one is.

## The prompt

A fixed system text (`SYSTEM_T`, the owner's names filled in) and a user message: today and the time zone, then one
escaped `<data>` block (`writer._block_json`: `<` and `>` escaped) with

- the item: who, via, sender kind, subject (defanged), when sent, its sentences `s1..s60` (the new text after quotes
  and signatures, links replaced by `[link]`, at most 4,000 characters), attachment names, and the two earlier
  messages of the thread ("who wrote what, when", 400 characters each);
- the context entries `c1..`: the person (People: name, VIP, known, area, last exchange date), up to 2 active projects
  (linked by the person's address or name, the sender's own domain, or the project's name in the subject and body),
  up to 3 calendar events within -14/+45 days (by attendee, project name or two subject words);

and, when notes apply, a separate trusted `<notes>` block (the owner's notes, `ownernotes.for_prompt`, at most 2, also
numbered c..). The fixed texts and `PROMPT_VERSION` are pinned by hash in the tests (`prompt_hash()`): change them and
bump the version.

Request: `stream` off, `max_tokens` 512, temperature 0, thinking off, strict `json_schema` (the ctx enum is this
item's ids, the sentence enums this item's sentences).

## The checks

Before anything is sent (`item_of`): `inject.rules_hit` (except the link rule), the sorter's `steer` and the reader's own
`aimed` rule (sentences addressed to an assistant, AI, bot or Callsheet; "summarise this as", "mark this as paid", "tell
the owner", "if you are an AI", "new instructions:") run over the subject, attachment names, the whole body including
the signature and quotes, and the earlier messages. A hit means **flagged**: no request; the page shows the subject and
"Not read: instructions". A link lure alone is read with the `lure` mark; links never reach the model.

After the answer (`parse`, then `check`):

- strict parse: exact keys, types, enums, the item's ids; anything else is invalid (counted; one more try on a later
  run, then the rules' line stays);
- `gist` and `why_you`: control, bidi and zero-width characters, links, markdown and HTML removed; dropped when inject,
  steer or `aimed` flags them, when they repeat a flagged sentence or say Callsheet (or "I") did something;
- claims: the owner's agreement or a decision stated as done (`writer.states_decision`, the sorter's claim rules, the
  reader's `owner_claim`) becomes "<sender> says: ..." (marked `claim`), or is dropped when that can't be said safely;
- grounding: every number, weekday, month, relative day word ("tomorrow", "next week") and capitalised name must
  appear in the item or a **cited** context entry; an amount of money must appear in the new message itself (not a
  quoted earlier one); relative day words only for a message sent today. An ungrounded text is dropped and counted
  (`ground_number`, `ground_money`, `ground_date`, `ground_name`);
- the due date counts only when an item sentence says that date, read from the message's own date ("Friday" in a
  Thursday message is the next day; "next Friday" may be either of the next two Fridays); the words that say it are
  kept as the phrase;
- the amount is copied by Callsheet from the cited sentence, as written, only when that sentence holds exactly one
  amount (currency symbols or codes on either side, any separators, "5k" only in a sentence about money); two amounts
  in one sentence show none.

## Load rules

- One at a time, the sorter's slot, after the sorter: `call_guard` (the room before a brief), `careful_guard`, the
  brief writer's sync, Chat first (`writer.LOCAL_LOCK` taken: the reader yields; mid-request `notify("lock")` raises
  `Busy`).
- Blocked windows (owner's time zone): 06:16–06:45, 12:16–12:45, 16:46–17:15, and the security audit's 01:00–04:00. A
  run never starts in one, stops before the next one starts, and leaves 60 s.
- Day caps: 200 items and 15 GPU minutes a day.
- In-flight budget (`writer.inflight`, shared with the sorter and Chat): requests count their estimated tokens
  (characters / 3.5 plus `max_tokens`) while in flight; background work never takes Callsheet's total over 56,000
  (Chat is counted, never refused).
- `writer.ContextFull` (the server's HTTP 500 "Context size has been exceeded", or the budget): wait 10 s and retry
  the same item once; a second time ends the run with the item still queued (nothing is lost). The sorter halves its
  batches for 15 minutes after one.

## Storage

`callsheet.db`:

- `reader_items`: key, thread hash, day, time, state (read, flagged, failed), the checked gist and why_you, kind,
  importance, confidence, context references (`{"t": person|project|event|note, "id": ...}`), due date and phrase, the
  amount as written, marks (claim, lure, hidden_text), flag, model, prompt version. Pruned after 7 days.
- `reader_queue`: references only.
- meta `reader` (on/off, windows) and `reader_day` (counts, drops, latencies: numbers only).

Logs are counts only.

## The page

`api/brief` carries `reading` (when reading is on): per reply row `state` read (gist, why, kind, importance, cards,
due, amount, marks), `not_read`, or `flagged` (subject, flag). The cards are built at view time from Callsheet's own
data (People, Projects, the index's calendar, the owner's notes), so a renamed project shows its new name and nothing
the model wrote reaches a card. `api/sorting` carries `reading`: counts, drops, p50/p95 latency and the schema-valid
rate for today.

- Replies rows: the gist is the row's summary; Comfortable rows show "why you" on the second line; a flagged row shows
  its subject and "Not read: instructions" in every density.
- The panel and One at a time: "What this is", "Why you", the labels (their words; a link that asks you to sign in or
  pay), Due and Amount ("From their email · check the amount in your account"), and the cards ("From Callsheet").
- Not read yet: the rules' line, with "Not read yet".

## The live check

`scratchpad/c0120/live_reader.py` (outside the repo, run by the test lead) sends a synthetic set through the real
server under the same rules and reports the schema-valid rate, flagged rate, grounding drops, latency p50/p95, ContextFull
count, Chat latency with and without the reader running, and the bars the fakes can't measure (the model's own
precision and recall on the gold set, claims, lures).
