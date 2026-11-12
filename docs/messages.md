# Message store and live readers (0.15.0)

Stage 1 of the rework (see the rework plan): one store for every message, both directions, kept current in the
background, with each source's freshness shown in Setup › Connections. Nothing reads the store yet except Chat's
`search` and `get_item` tools; the ledger (stage 2) will read it.

Code: `callsheet/app/msgstore.py` (store, `ChatView`), `callsheet/app/live.py` (readers, freshness rows).
Tests: `tests/test_messages_0150.py` (in-process fakes; no network).

## The store: `/data/messages.db`

SQLite, WAL, file mode 0600, **included in Home Assistant backups** (not in `backup_exclude`). Python stdlib only.
All writes go through one writer thread (`MessageStore.write(fn)` runs `fn` in `BEGIN IMMEDIATE … COMMIT` on that
thread and waits); reads use a separate connection. Nothing is ever deleted: retention is "keep everything".

| Table | What it holds |
| --- | --- |
| `messages` | One row per message. `source` (gmail, slack, signal), `account` (Gmail address, Slack team id, own Signal number), `thread`, `msg_id` (unique per source+account), `direction` (`in`/`out`), `sender`, `sender_name`, `recipients` (JSON), `ts`, `subject`, `text` (plain text, quoted history kept, capped at 200,000 characters), `labels` (JSON: Gmail labels, Slack/Signal channel kind), `channel` (Slack "#name"/"DM with X", Signal group name), `link`, `attachments` (JSON name/type/size only), `people` (searchable names and handles), `hash` (of subject+text), `ingested_at`, `updated_at`, `deleted_at`. |
| `revisions` | Earlier versions: `message`, `at`, `kind` (`edit`/`delete`), the old `subject`, `text`, `hash`. An edit updates the message and keeps the old text here; a delete sets `deleted_at` and keeps the text. |
| `events` | Calendar events: `account`, `calendar`, `event_id` (unique per account+calendar), `ical_uid`, `start`/`end` (as Google sent them), `tz`, `start_ts`/`end_ts`, `all_day`, `title`, `location`, `attendees` (JSON), `status` (cancelled kept), `updated`, `link`, `hash`. |
| `cursors` | Per source+account resume points (JSON): Gmail history id and backfill page, Calendar sync time, Slack per-conversation newest ts and active threads. |
| `freshness` | Per source+account: `last_attempt`, `last_success`, `last_new`, `newest_ts`, `errors` (in a row), `last_error`, `error_at`, `backlog`. |
| `meta` | Settings such as `backfill_days`. |
| `messages_fts` | FTS5 over subject, text and people (external content, kept by triggers). If FTS5 is missing, search falls back to `LIKE`. |

Upserts are idempotent: the same message twice changes nothing; a changed hash is an edit (revision kept); changed
labels are updated without a revision.

## Readers (`live.py`)

Each reader runs on its own background thread (started by `server.py` unless `--no-scheduler`), with backoff of
interval × 2^errors up to one hour. A pass that leaves work over (backfill, rate limit) runs again soon.

- **Gmail**, per connected account, every 2 minutes. First run: saves the profile's history id, then backfills the
  last 90 days (Setup › Advanced › Message history: 7, 30, 90, 180 or 365 days), newest first, 100 ids a page, at
  most 300 `messages.get format=full` a pass, paced 0.1 s apart; resumable. After that `users.history.list` from the
  saved id (added, deleted, label changes). If the id has expired (404) it reads a time window with `messages.list
  after:` and takes a fresh history id. SENT label (or own address as sender) = `out`. Drafts and spam are skipped.
  Text: `text/plain` preferred, else HTML through `htmltext`, else the snippet. New allow-list line:
  `GET /gmail/v1/users/me/history` (the existing `/gmail/v1/users/me/` prefix already covered it; no new scope, so
  no re-consent).
- **Calendar**, every 15 minutes: the last 30 days and the next 400, every calendar Callsheet already reads; a full
  pass daily, otherwise only events changed since the last pass (`updatedMin`, `showDeleted`). Cancellations kept.
- **Slack**, every 3 minutes per workspace: `conversations.history` for each conversation (14 days the first time),
  plus `conversations.replies` for threads with a reply in the last 3 days (at most 20 a pass), at most 60 requests
  a pass and under Slack's rate limits (a capped pass is backlog, not an error). Own user id = `out`. Ignored
  conversations are skipped.
- **Signal**: continuous. `signalcli.SignalService` hands each received envelope to `SignalIngest.on_envelope`:
  incoming messages, messages sent from the owner's phone (sync) as `out`, group id and name, edits, remote deletes,
  attachments as name and type only. Handles are kept as the number or uuid.

## Freshness

`GET api/sources` returns `{sources: [...], chat_search, store: {messages, events, bytes}, settings: {backfill_days,
choices}}`. Each row: `key`, `name`, `state`, `word`, `line`, `warn`. `PATCH api/sources/settings {backfill_days}`
changes the backfill length (only the listed choices).

| State | Word | When |
| --- | --- | --- |
| live | Live | Signal receiving |
| current | Current | checked within 3 intervals, nothing waiting |
| catching_up | Catching up | backfill or a capped pass still has work |
| starting | Starting | no successful pass yet, no error |
| behind | Behind / Not receiving | no successful check for 3 intervals (+1 min); Signal not running |
| error | Needs attention | 3 failures in a row, or the first pass failed |

Chat's search (the older `index.db`, `max(indexed_at)`, and `sorting_ix.indexed_at`) has its own row: behind after
6 hours, with a warning like "Chat's search is 8 days behind. Chat also searches the new message store …".

The Sources panel is first in Setup › Connections: one row per source, a shape and a word (✓ current/live,
◐ catching up, ○ starting/off, ▲ behind/needs attention), the "Checked … · read up to …" line, and a dashed box with
the warning when a row is bad. Rendered with `textContent` only.

## Chat merge (`ChatView`)

`api._chat_init` wraps the index in `msgstore.ChatView(index, store)`. `search` asks both; rows from the store newer
than the index's newest item for that source come first, then index rows, then older store rows; duplicates (same
source and source id, or a Signal message with the same timestamp) keep the index's row. Store ids are offset by
1,000,000,000 so `get_item` reads the right one. Searches with `ids`, `area` or `offset`, or for a source the store
doesn't hold, go to the index only. Untrusted text is flagged the same way as the index's rows.

## Disk use (estimate)

Measured with synthetic text: about 14 KB per Gmail message with 6 KB of text (the full-text index roughly doubles
it), 0.6 KB per Signal message, 0.9 KB per Slack message. At Isaac's volume (70–94 Gmail threads per sync across four
accounts, about 400 Signal messages in 9 days, about 1,500 Slack messages in 2 weeks):

| Source | Per year | Size |
| --- | --- | --- |
| Gmail | about 30,000 messages | 300–600 MB |
| Signal | about 16,000 | 10–25 MB |
| Slack | about 39,000 | 35–80 MB |
| Calendar | a few thousand events | under 5 MB |

About 0.4–0.7 GB a year, up to about 1.5 GB if most mail is long newsletters with quoted history.
