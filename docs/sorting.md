# AI sorting (0.8.4, 0.9.0)

`callsheet/app/triage.py`. Design: `~/project/daily-brief/understanding/design.md` (sections 3, 6, 7, 9). The card,
schema, prompt and merge rules are the ones the 0.8.3 screen measured (`triage083.py`). 0.9.0: prompt `t083.2` with
its addressing cues, the `t083.3` to_owner rule and the dates-only fact policy (below).

## What happens

1. **Each refresh** (`sync.py`, after the rules brief is cached): every Gmail thread the sync read (also the ones the
   rules drop today), the **live Slack and Signal messages it read** (`slack_sources`, `signal_sources`: keyed exactly
   like the brief's Slack and Signal rows, so their labels find the rows; the sender and "from the owner" come from
   user ids and signal-cli's outgoing flag, never the text; ignored Signal chats are left out; the sorter gets the
   workspace id and team id only, never the token) and the index items (mail, Slack, Signal, Drive) indexed since the
   last look and at most 3 days old become cards. Each card gets its **rules pre-label** right away (stored, status
   `rules`); new or changed items (by content hash) go into **the work queue** (below). No model is called inside a
   sync.
2. **The AI pass** (the sorter thread, every 30 s): batches of 12 cards, one request each, to the owner's local model
   server (the writer's address), with the model from the sorting settings (Gemma by default), `id_slot` 0,
   temperature 0, thinking off, a strict per-request JSON schema (each card's own sentence ids are the only allowed
   `s` values) and the fixed system text. Cards go inside the escaped `<data>` block (`writer._block_json`).
3. **The merge** (`place`, deterministic): overrides first (security -> Heads up; invites -> Your day; VIP, known
   person writing to the owner, owner named in To -> never demoted; flagged text -> the model's label is ignored),
   then strong rules replies, then the model's label with its evidence. Every card lands in exactly one group
   (`never_hides`); a model answer that's missing, malformed or off-schema leaves that card to the rules (status
   `fallback`).
4. **The night backfill** reads the index newest first from a stored `(ts, id)` cursor inside the owner's window.

## The work queue (`Queue`, table `triage_queue`)

- **Restart-safe**: one row per item in callsheet.db, newest first: key, source kind, origin (`sync` or `index`), index
  id, dedupe id, content hash, message time, queued time. **References only, never text**: the text comes from the
  live read in memory or, for index items, from the index when the batch is built. After a restart a live item waits
  for the next sync to read it again (that sync re-attaches the text without queueing it twice); one that never comes
  back leaves after `STALE_S` = 3 days (its rules label stays). A run with only such items ends with `waiting`.
- **Dedupe**: one row per key; the same message seen through the live read and the index (same `dup` id: the Gmail
  message id, Slack workspace + channel + ts, Signal chat + sender + ts) is sorted once. The live copy wins (it has
  the headers): it replaces a queued index copy, and an index copy is skipped when the live one is queued or labelled.
- **Cap**: `MAX_PENDING` = 2,000; beyond it the oldest leave first (their rules labels stay).

## Budget and timing (`Sorter.call_guard`, `_run`, `backfill_step`)

| Rule | Value |
|---|---|
| A run | at most `RUN_S` = 180 s of model time; each request's timeout is the room left |
| Before a call time | the run ends at least `END_BEFORE_S` = 10 min before; no start with < 20 s of room |
| After a call time | no start until `AFTER_CALL_S` = 10 min after (the brief writer goes first) |
| Sync or brief writer | never while `syncer.lock` is held or `syncer.writing` is set (Swift's brief) |
| Chat | requests never wait for `writer.LOCAL_LOCK`: when it's taken the sorter yields (`Busy`) |
| Measured pace | Gemma, 12-card batches: 21-36 cards a minute (`MEASURED_CPM`); a batch is planned at `EST_BATCH_S` = 30 s until measured |
| Daily cap | `day_cards` 1,200 cards and `day_gpu_min` 60 model minutes (1,200 / 21 = 57); then "N items waiting" |
| Night backfill | window 01:30-04:30 (configurable, may cross midnight), 30 days back, 7 nights at most; each night tick runs batches back to back for up to `NIGHT_RUN_S` (= `RUN_S`), so a 3-hour window reads about 3,800-6,500 cards; pauses when the server is busy, yields the rest of the night after "Loading" twice or when its batches take 3x the measured median |
| Resuming | the `(ts, id)` cursor and the nights used are stored in meta after every batch: a night that ends (window over, restart, unhealthy server) goes on from the cursor the next night, until the 30 days are read or the nights run out |

Log lines are counts only (cards, batches, rules-only, tokens, ms, cards/min, waiting).

Server faults (0.8.5, `docs/server-faults.md`): garbage or a llama.cpp internal error from the server stops the run
(reason `unhealthy`, the batch stays queued with its rules labels); while the server is in its 15-minute backoff
`run_pass` and the night step return `unhealthy` without sending. Setup → AI → Sorting shows "Local model unhealthy
since ..." with **Check again** (`POST api/sorting/model-check`).

## Modes (settings in meta `sorting`)

- `shadow` (default; `shadow_since` is the first day): labels are computed and stored and shown only on
  **Check the sorting** (0.9.0: Setup → AI → Sorting links to it). `api/brief` is byte-identical to sorting off.
- `on` (the owner's choice; never automatic): `brief.sorting` = per reply row: group, why code, rules label and
  codes, AI label and codes, overrides, marks and the quoted evidence sentence (from memory, defanged, never a
  flagged sentence). The page shows a mark on demoted rows and **Why is this here?** with three fixes.
  `brief.sorting.sections` (`sections`, pure) groups the reply rows: **Needs you** (needs_you, your_day, security;
  every override; rows without a label yet), **For your info** (for_your_info, what_changed; folded after 5) and
  **Sorted for you** (parts promos, updates, not_for_you: counts, each opening its list). Every row id is in exactly
  one section, so nothing is hidden. Ranked within each: VIP and security overrides, other overrides, needs-a-reply
  (decision, deadline, request, question), then the rules' order. Shadow mode adds nothing (byte-identical brief).
- `off`: nothing is collected or sent.

Other settings: `model`, `batch` (4-16), `day_cards`, `day_gpu_min`, `backfill {on, start, end, days, nights}`.
gpt-oss models get `reasoning_effort: low`.

## Storage (callsheet.db)

- `triage_labels` (key = the brief row key for live mail, else a hash of the index id): src, hash, day, status,
  pre_kind, strength, evidence codes, overrides, marks, ai_kind, to_owner, ai_evidence, evidence_s, ask, confidence,
  facts (type, sentence id, date, time, who, status, verified, claim, headsup), group, why, model, prompt, index id,
  checked. Every value is checked against its enum or pattern (`label_row`): no free text. Pruned after 60 days.
- `triage_corrections`: the owner's fixes (key, action, the labels at the time, model, prompt), for 0.8.5.
- `review_counts`: (week, model, prompt, label, truth, n): the Check page's counts only.
- Message text lives only in the in-memory card cache (for the Check page and Why), never on disk.

## Check the sorting

`GET api/sorting` gives up to 10 items a day (AI labels from today, one of each label in turn), the counts and the
settings. `POST api/sorting/check {key, right, label}` adds one to the counts and marks the label checked.
`GET api/sorting/numbers` is counts only (week, model, prompt, per-label right/wrong, confusion, security misses).
"Ready to turn on" (shown, never acted on): 70+ checks, needs_reply 85%+ right, promo 95%+, no security misses.

## API

`GET api/sorting`, `PATCH api/sorting/settings`, `POST api/sorting/check`, `GET api/sorting/numbers`,
`POST api/sorting/fix {key, action: needs_reply|not_for_me|promo}` (409 unless sorting is on),
`DELETE api/sorting/fix/<id>` (Undo). `api/state` has a `sorting` summary for Setup home (0.9.0: the AI card's
"Sorting ..." line).

## Where it lives (0.9.0)

- **Setup → AI → Sorting** (`sortingBlock`, through 0.9.0's `sortingSlot`): the health line and **Check again**
  (`lmHealth`), the settings (`sortSettings`, its model, items a day and night reading behind its own closed
  Advanced), the **Check the sorting** link (with today's count) and **What it learned** (`srLearned`). It loads
  `GET api/sorting` when the place opens (again after a minute).
- **#sorting** is the Check the sorting page only (the items to check, the week's counts, Copy the numbers);
  Back goes to Setup → AI.
- The settings index (`SETTINGS`: Find a setting and Find or ask) has AI sorting, the sorting model and limits, the
  local model's health, Check the sorting and What it learned.
- When sorting is on, One at a time walks the sections in order (Needs you, For your info, Sorted for you;
  `srSplit`), Start your day counts Needs you, and the One at a time card shows the row's sorting mark.

## Prompt t083.2 and its cues (0.9.0)

- `SYSTEM_T` is the screen's `t083.2` text; `system_text(owner, first, aliases)` is byte-identical to the harness
  (`screen_hash` = `ecfcfcc5b3abafa4`, pinned in `tests/test_sorting_090.py`; `prompt_hash` = the template plus
  `PROMPT_VERSION`). Gemma (`GEMMA`) is the default model; Shadow week the default mode.
- `addressing_cues` (deterministic, from headers and text, never the model) adds to each card: `you_copied` (live
  mail with the owner only in Cc/Bcc; index mail has `rcpt_known` false, so never), `greeting_to` (owner, others,
  none), `mentions` (who is @-mentioned), `vocative_to` ("Isaac," / "Zac:" at a sentence start) and `owner_named`
  (the owner's first or last name anywhere in the text). Same values as the screen's cards on all 171 hard items.

## to_owner rule t083.3 (`copied_only`, after the model, before `place`)

When the owner is only on Cc/Bcc (`you_copied`) AND no greeting, vocative, @-mention or group question addresses
the owner AND `owner_named` is false, the model's label becomes `to_owner: no`; `needs_reply` and `fyi` become
`for_someone_else` (promo, automated_update and meeting_or_date keep their kind). Never when an override holds (VIP,
security, invite, owner named in To, flagged text) or the sender is a VIP. The row's why is `rules:copied_only`.
Offline on the screen's hard set (171 items x 3 Gemma answers, `tests/fixtures/sorting_hard083.json`): to_owner
0.807 (414/513) before and after; the rule applies to 7 items (Bcc, two others in To), where Gemma already said no.
The misses left are mostly promos written in a personal tone, reply-all noise and group chatter.

## Facts: dates only (0.9.x)

Only the model's `date` and `deadline` facts are used (`FACT_USED`): each points at a sentence id and is checked
against the dates `dates.find` reads in that sentence (`verified`); only verified ones can become a heads-up
(`used_dates` lists the usable ones). Decisions and commitments are stored as labels with status
`held`: never shown, never in a heads-up (a commitment from someone else stays `sender_says`).

## Also in 0.8.4

- `rules.SECURITY`: declined cards ("card ending 2290 was declined"), declined or failed transactions, payments that
  didn't go through.
- `dates.find`: a commitment or deadline sentence confirms a date without an event word (kind `deadline`); not in
  quoted history (">", "On ... wrote:", header lines) or after a signature ("--", "Sent from my").
- `index.py`: a `hidden` column (the mail's HTML had hidden text), `hidden_text: true` on its rows and Chat chips
  ("had hidden text"); for the sorter it's a mark and a flagged override.

## Tests

`tests/test_sorting_084.py`, `tests/test_sorting_084_queue.py` (the queue, live Slack/Signal, sections),
`tests/test_sorting_090.py` (prompt t083.2 pin, cues, t083.3 on the hard set offline, dates only, throughput, night
resume; `tests/fixtures/sorting_hard083.json`). Fakes and recorded answers only: no model is ever called.
