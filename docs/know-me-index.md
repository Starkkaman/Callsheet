# 0.6.0 "Know me": the local index (stage 3a, no AI)

Callsheet reads the owner's history once (and then every 30 minutes, incrementally) into a private full-text
index on the Home Assistant box. 3a builds the index, the scan and the Setup page; nothing in 0.6.0a gives the
index to the writer (3b/3c will, through the interface at the end).

## Storage
`/data/index.db` (mode 0600, WAL), separate from `callsheet.db`, rebuildable, so `config.yaml` has
`backup_exclude: ["index.db*"]`. Schema version 1 (`index.py`):

- `items(id, source, account, source_id, kind, thread, title, people, area, ts, url, snippet, body, hash,
  indexed_at)`, `UNIQUE(source, account, source_id)`. `body` is plain text (writer helpers strip HTML and
  quoted replies), capped at 8,000 characters; `people` is "; "-joined names/addresses/hints.
- 0.6.0 chat: `items.instr` / `items.instr_v`: planted-instruction flags (hashes only, see `chat.md`), added in place
  to existing databases; `Index._row` returns `instructions: true|false`.
- `items_fts`: FTS5 external-content table over `title, people, body` (`unicode61 remove_diacritics 2`),
  kept in step by triggers. Ranking: bm25 with weights title 8, people 4, body 1.
- `cursors(source, account, key, value)`: resumable page tokens/progress per target (written in the same
  transaction as the items of that step, so a restart resumes exactly).
- `meta(key, value)`: settings (`vault`, `sources` on/off), the job, refresh times, granted Google scopes.

Upserts are keyed by (source, account, source_id) and skip unchanged rows (content hash), so rescans are
idempotent. Turning a source off, changing the vault, or removing an account/workspace (orphan prune every
10 minutes) deletes its items.

## Sources (read-only; `sources.py`)
| key | reads | allowlist |
|---|---|---|
| gmail | `messages.list q=after:X [before:Y] -in:chats -in:drafts`, `messages.get format=full` (text parts only, no attachments), 0.25 s pacing | already allowed (Gmail GET) |
| calendar | the calendars picked in Setup, `events.list` pages, window up to now+365 days | already allowed |
| slack | `conversations.list` (non-ignored), `conversations.history` in the window, `conversations.replies` (max 2,000 threads), `users.list` for names; `slack.py` caps and Retry-After respected | unchanged (all existing read methods) |
| drive | `files.list` (modified in window, not trashed, Docs/Sheets/Slides/text/markdown/CSV; PDF skipped), Docs/Slides `export text/plain`, Sheets `export text/csv` (capped), text files `alt=media` (≤1 MB) | NEW: `GET /drive/v3/files` and `GET /drive/v3/files/<id>[/export]` (id `[A-Za-z0-9_-]{1,200}`; `generateIds`/`trash` refused) |
| obsidian | `*.md` under the vault (front matter, H1/stem title, tags, `[[links]]` as people/topic hints); skips dot folders (`.obsidian/`), attachments, symlinks, files > 1 MB; whole vault regardless of window | none (local, `map: share:ro, media:ro`) |

Drive needs `drive.readonly`. Granted scopes come from the token refresh response (`scope`), cached for an
hour; Setup's "Grant Drive access" runs the normal paste flow with `include_granted_scopes=true` and
`login_hint`, so existing logins keep working and nothing else is re-asked. Until granted, Drive targets
show "Grant Drive access first" and are skipped.

Errors: 429 / rate reasons → wait and retry (60 s, or 1 h for daily quota), backoff doubling to 1 h;
`accessNotConfigured` / `insufficientPermissions` / 404 → that target is skipped with a plain sentence.

## Scan (`scan.py`)
Windows: 30 days, 90 days, 1 year, 5 years, Entire history. Before starting, `estimate` counts (Gmail
`resultSizeEstimate`/`messagesTotal`, events, files, notes; Slack can't be counted) and words it:

- "About 48,000 emails. May take about 6 hours and use about 900 MB on this box."
- "This can take hours. Briefs still run on time: the scan pauses around call times. You can pause, stop and resume."
- "Slack doesn't say how many messages there are, so Slack isn't in this estimate."
- "This box has only 2 GB free; the index may need about 4 GB."

One background thread (nice 10), round robin over targets, one step at a time; progress per target
(done/total, rate, ETA), pause/resume/cancel, state in `meta` so it survives restarts; pauses from 10 minutes
before to 10 minutes after each call time and while a sync runs ("Waiting for the 12:30 pm Midday brief: …").
After a scan, a refresh job re-reads each target from its last run minus 2 days every 30 minutes.

## API (`api.py`, section "0.6.0 index")
`GET api/index/status` · `GET|PATCH api/index/settings` (`{vault, sources:{key:bool}}`) ·
`POST api/index/estimate {window, sources}` · `POST api/index/scan {window, sources}` ·
`POST api/index/pause|resume|cancel` · `GET api/index/search?q=&source=&account=&area=&people=&from=&to=&limit=&offset=`
(dates YYYY-MM-DD in the owner's time zone, `to` inclusive) · `GET api/index/item/<id>` ·
`DELETE api/index {confirm: wipe_token}` (token from status, rotates) · `POST api/index/drive/check` ·
`POST api/index/drive/start {account_id}` → `{flow_id, auth_url}` · `POST api/index/drive/finish {flow_id, pasted}` → `{account, granted}`.
All behind the same Supervisor/loopback check as every other route.

## Interface for 3b/3c (Python, `index.Index`)
```python
ix = index.Index(data_dir)                         # opens/creates index.db (0600)
ix.search(q="", source=None, account=None, area=None, people=None, since=None, until=None, limit=20, offset=0)
# -> [{id, source, account, kind, thread, title, people, area, ts, date, url, snippet, highlight}]
#    highlight = [[text, is_match], ...]; q="" -> newest first; source may be "gmail,slack" or a list; limit <= 100
#    0.6.0: several terms and 0 hits on the first page -> one retry with OR and every word a prefix; rows get relaxed: true
ix.get(item_id)   # -> the row plus body, snippet, source_id; None if gone
ix.stats()        # -> {total, bytes, oldest, newest, sources: {src: {items, years: {YYYY: n}, accounts: {acc: n}}}}
ix.upsert(items) / ix.commit_step(items, source, account, key="scan", cursor=None, clear_cursor=False)
```
A new source (e.g. Signal on its branch) subclasses `sources.Source` with `key, label, unit`,
`targets(ctx)`, `estimate(ctx, tgt, since)` and `step(ctx, tgt, since, until, cursor) -> (items, cursor|None)`
and is added to `sources.all_sources()` (+ `scan.SECONDS_PER`/`BYTES_PER`/`UNIT_WORDS`).
