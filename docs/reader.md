# Reader (0.12.0; field counts and gold set v2 in 0.12.2)

The design's stage "Reader" (Wheatley design 2.4 D, there called 0.10.1; it ships as 0.12.0 because 0.10.1 was taken
by Reports). Code: `callsheet/app/reader.py`; tests: `tests/test_reader_0120.py`,
`tests/test_reader_0122.py` (field counts, gold set v2).

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
the owner", "if you are an AI", "new instructions:"; "tell/let <owner's name> know" only when what is to be told settles
something: paid, approved, agreed, safe, no action needed; "can you let <owner> know the venue changed?" is read) run over the subject, attachment names, the whole body including
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
- `reader_counts` (0.12.2): day, field, outcome, n. Kept 30 days (`COUNT_DAYS`, pruned with the items).

Logs are counts only.

## Field counts (0.12.2)

For the test lead, per day (the owner's time zone) and per field the reader can show, how often it was shown and,
when not, why (numbers only, never text). Fields (`FIELDS`): `gist`, `why_you`, `due`, `amount`, `ctx` (the cards).
Each answer gives one outcome per field; a flagged item gives `flagged` for every field; an unusable answer gives
`schema_invalid` for every field, once per try (an item tried twice counts twice). Outcomes (`OUTCOMES`):

- every field: `shown`, `flagged`, `schema_invalid`;
- `gist`, `why_you`: `empty` (nothing left after cleaning), `instructions`, `repeated` (repeats a flagged sentence),
  `actions` (says Callsheet did something), `claim`, `ground_number`, `ground_money`, `ground_date`, `ground_name`;
- `due`: `not_given` (the model gave none), `ground_date` (no item sentence says that date);
- `amount`: `not_given`, `amount_none` (the cited sentence holds no amount), `amount_ambiguous` (more than one);
- `ctx`: `not_given` (entries offered, none cited), `no_context` (none offered). A cited card can still be left out
  at view time (`_cards`) when its record is gone.

"Copy the numbers" (Setup › AI, `GET api/sorting/numbers`) carries them as `reader_counts`:
`{"2026-11-05": {"gist": {"shown": 40, "ground_name": 2}, ...}, ...}`, newest day first. When a day is over, one log
line gives that day's counts, like the sorter's rescue count: `Callsheet reader: 2026-11-05: gist 40 shown, 2 not
(ground_name 2); why_you ...; due ...; amount ...; ctx ...`.

## The setting

Setup › AI › Read each message: On (the default) or Off (`PATCH api/reading/settings` `{"on": bool}`, meta `reader`).
Off sends no reader requests and the rows keep the sorting's reason. Reading also needs sorting to be on.

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

`--gold gold_v2.jsonl` (0.12.2) uses the gold set v2 instead of the 12 built-in items and reports precision and recall
per field (due date, amount) with 95% Wilson intervals, overall and per category, plus the field outcomes.

## Gold set v2 (0.12.2)

`tests/fixtures/c0122/gold_v2.jsonl` (the same file sits next to the live check as `scratchpad/c0120/gold_v2.jsonl`;
`gen_gold_v2.py` there builds both): 107 synthetic messages, one JSON object per line: `id`, `cat`, `from`,
`subject`, `body`, `sent` (with the owner's offset), `tz` (the owner's time zone), `gold` (`date`, `date_also`,
`amount`), two fake answers (`good`: what a careful model would answer; `trap`: the tempting wrong one, null = same
as good; each `{date, date_in, amount_in}`, the fake citing the sentence that holds that text), `expect` (the checks'
output for both answers) and an optional `note`.

| category | n | hard cases |
|---|---|---|
| currency | 16 | USD, CAD, EUR, GBP, "CA$", "C$", "US$", "US$ 45", codes after the number, 1.234,56 € |
| range | 9 | "$1–2k", "between 500 and 800 dollars", "$1,000-$1,500", "2-3k", "5–7k EUR", "up to $2,000" |
| deadline | 14 | "by EOW", "end of month", "by the 15th", "the 15th of December", "the 1st", "30/11", "in two weeks" |
| timezone | 9 | an invite at 09:00 ET for a Pacific owner, CET/EST times on the owner's previous day, AoE, a late-evening message |
| cancel_reschedule | 12 | cancelled (no due date) vs moved (the new date wins), "was 13 Nov", "postponed to next Tuesday" |
| quoted | 8 | the older amount under "On … wrote:", "> " lines, Outlook "Original Message", a forwarded header |
| signature | 8 | amounts, dates and names in signatures with and without "-- " |
| two_amounts | 8 | two or three amounts in one sentence, the total next to the deposit |
| totals | 10 | totals vs line items, subtotal/VAT/total, per-seat prices |
| control | 13 | no date or amount, "5k" run vs budget, room numbers, times |

`test_reader_0122.GoldV2` runs every item through `item_of`, `parse` and `check` with both fakes and pins the output
item by item; the careful fake must keep precision >= 0.95. What the fake run shows (dates / amounts):

- careful fake: precision 56/56 and 36/36; recall 56/68 (0.82, 95% CI 0.72–0.90) and 36/42 (0.86, 0.72–0.93). Lost
  by the checks: dates no sentence names ("EOW", "end of month", "in two weeks", "30/11"), owner-local dates of
  invites across midnight, "by Monday" said on a Monday (read as that same day), amounts only in quoted mail, and a
  clear total in a sentence with more amounts.
- trap fake: precision 28/44 (0.64, 0.49–0.76) and 22/33 (0.67, 0.50–0.80). What the checks can't catch: a cancelled
  or moved date the sentence still names, a weekday read alone ("Tue 10 Nov" also gives this Tuesday), a line item or
  subtotal in its own sentence, an amount in a signature without "-- " or under a forwarded header, and the bare end
  of a range ("2-3k", "5–7k EUR"). Older amounts in quoted mail never reach the model.
