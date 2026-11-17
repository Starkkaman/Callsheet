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
| `messages` | One row per message. `source` (gmail, slack, signal; 0.16.1: sms, sms_live), `account` (Gmail address, Slack team id, own Signal number), `thread`, `msg_id` (unique per source+account), `direction` (`in`/`out`), `sender`, `sender_name`, `recipients` (JSON), `ts`, `subject`, `text` (plain text, quoted history kept, capped at 200,000 characters), `labels` (JSON: Gmail labels, Slack/Signal channel kind), `channel` (Slack "#name"/"DM with X", Signal group name), `link`, `attachments` (JSON name/type/size only), `people` (searchable names and handles), `hash` (of subject+text), `ingested_at`, `updated_at`, `deleted_at`. |
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

The Sources panel is first in Setup › Connections: one row per source, a shape and a word (✓ current/live,
◐ catching up, ○ starting/off, ▲ behind/needs attention), the "Checked … · read up to …" line, and a dashed box with
the warning when a row is bad. Rendered with `textContent` only.

## Texts (0.16.1, `texts.py`)

Two readers fill `messages.db` with the phone's texts. Tests: `tests/test_texts_0161.py`, fixtures in
`tests/fixtures/texts/`.

**Nightly backup (source `sms`, account `phone`).** The SMS Backup & Restore app writes `sms-*.xml` into a folder
under /share or /media, saved in Setup › Connections › Texts. 0.21.2: there is no default folder; an empty folder
means texts aren't set up and nothing is read, on a new install and on one from before that never saved a folder.
`calls-*.xml` and other names are ignored.
The folder follows the Reports rules (`conn_reports.check_folder`, read through a directory descriptor, no links,
hard links, pipes or odd names; a file younger than 60 s waits). Every 15 minutes the reader looks at the folder and
reads only files whose size or time changed, newest first, at most 3 a pass. Each file's result is kept in the
cursor `files` (read or bad, the reason, the backup time).

- Parsing: `xml.parsers.expat`, streamed in 1 MB pieces. A DOCTYPE or any entity declaration stops the file (no
  external entities, no entity expansion). Caps: 512 MB a file, 500,000 messages, 50,000 characters of text, 50
  attachments a message, depth 8. The `data` attribute (base64 pictures) is never kept.
- Emoji: the app writes them as two surrogate references (`&#55357;&#56832;`); they are joined into one character;
  a lone half becomes U+FFFD.
- SMS: `type` 1 = in, 2 = out; drafts, outbox and failed sends are skipped. MMS: `msg_box` 1/2, text parts kept,
  other parts as type and file name only, the sender from `addr type="137"`, a group's people from the other
  addresses. Dates in ms (MMS dates in seconds are scaled).
- Ids: a hash of kind, address, date, box and text, so the same message in the next night's file is the same row.
  Thread: `sms:<last 10 digits>` (or the lowercased name for short codes); channel "Texts with Dana" or "Group text:
  …". Labels: sms, mms or group.
- A malformed file is marked bad and named in the warning; messages read before the error are kept.

**Live (source `sms_live`, account = the sensor).** The owner picks the Companion app's *Last notification* sensor
and the apps to keep (Google Messages by default; Signal and WhatsApp optional). Every minute: `GET
/api/states/<sensor>`; only when it changed, `GET /api/history/period/<UTC time>?filter_entity_id=<sensor>` from the
last read (2 min overlap, one day the first time). Kept: allowed `package`, not ongoing, not a group summary, with
`android.title` (the sender) and `android.text` or the longer `android.bigText`. The same sender and text again
within 10 minutes is one message. These are the only two new Core API calls (`ha.py`'s allowlist, sensor ids
`sensor.*_last_notification` only); everything else stays refused. Home Assistant's history keeps state changes, so
the same text twice in a row may be missed live; the backup still has it.

**Link.** After each backup read, a live text from the last 30 days is linked to its backup copy when the sender
(name, or the number's last 10 digits, or "Name: text" in a group) and the text match within ±5 minutes; the live
row gets `same_as` = the backup row. Search, Chat, the ledger's queue and its sources skip linked live rows, so the
backup copy wins. If the ledger already read the live copy, the backup copy is marked read (`skipped`, "the same
text as live notification N") and takes over its ledger sources, so nothing is read twice.

**Sources rows.** "Texts · Nightly backup 03:10 · 1,204 messages" (warns when the newest backup is older than 36 h,
a file couldn't be read, or files were skipped) and "Texts (live) · Last notification 2 min ago" (warns when the
sensor is unavailable; a sensor Home Assistant doesn't have is a failed check with that reason). API: `GET/PATCH api/texts` (folder, sensor, apps), `GET api/texts/sensors`
(the phones from the notify list, each probed with `GET /api/states/sensor.<phone>_last_notification`).

## Chat's search (the library)

Since 0.21.0 the library (`library.py`) is the one searchable view of `messages.db`: Chat's tools, Find, Know me,
Projects, People and the connectors read there. The 0.6.0 index (`index.py`, `index.db`), its scan and the
`ChatView` merge are gone; an old `index.db` is imported once (`import_index`, `import_index_messages`) and then left
on disk, unused.

## Disk use (estimate)

Measured with synthetic text: about 14 KB per Gmail message with 6 KB of text (the full-text search tables roughly double
it), 0.6 KB per Signal message, 0.9 KB per Slack message. At a test install's volume (about 80 Gmail threads per sync across a few
accounts, a few hundred Signal messages and about a thousand Slack messages a week):

| Source | Per year | Size |
| --- | --- | --- |
| Gmail | about 30,000 messages | 300–600 MB |
| Signal | about 16,000 | 10–25 MB |
| Slack | about 39,000 | 35–80 MB |
| Calendar | a few thousand events | under 5 MB |

About 0.4–0.7 GB a year, up to about 1.5 GB if most mail is long newsletters with quoted history.
