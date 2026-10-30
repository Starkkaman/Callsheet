# Step 4: Callsheet 0.4.0 backend contract (Slack, smarter writer, writing instructions, "doesn't need a reply", new since)

Scope: server, API and tests. The page (`callsheet/app/static/**`) is built against this contract later.
Earlier contracts still hold: `docs/step2-google.md` (Google, API conventions) and `docs/step3-writer.md` (writer).
Conventions unchanged: relative URLs, JSON bodies, mutating requests need `X-Callsheet: 1`, errors are
`{"error": "<plain sentence>"}` with 400 (fixable input), 404 (unknown id), 409 (conflict), 502 (Slack said no).

## Hard rules for Slack
- Read-only by construction: `slack.py` sends GET/POST only to these Web API methods: `auth.test`, `team.info`,
  `users.info`, `users.list`, `conversations.list`, `conversations.history`, `conversations.replies`,
  `conversations.info`. Anything else raises `NotAllowed` before any network I/O (tested). No Slack SDK.
- The app manifest asks for user scopes only: `channels:history channels:read groups:history groups:read
  im:history im:read mpim:history mpim:read users:read team:read`. No `chat:write`, no other write scope, no bot.
- The user token (`xoxp-…`) is stored in the database like other secrets, never returned (only `token_hint`, the
  last 4 characters) and never logged. Base URL override for tests only: env `CALLSHEET_SLACK_API_BASE`
  (default `https://slack.com/api`).
- Rate limits: HTTP 429 → wait `Retry-After` seconds (default 1 s, then 2, 4; at most 3 tries per request, never
  past the sync deadline); at most `SLACK_MAX_CALLS` (120) Slack requests per workspace per sync. When the cap or the
  deadline is hit, the rest is skipped and Heads up says so.

## Slack API routes
- `GET api/slack/manifest` → `{manifest: {…}, manifest_json: "<compact JSON string>", manifest_yaml: "<YAML text>",
  create_url: "https://api.slack.com/apps?new_app=1&manifest_json=<urlencoded manifest_json>", scopes: [..]}`.
- `POST api/slack/connect {area_id, token}` → workspace object (below). Checks the token with `auth.test`, then
  `team.info` (name, domain; best effort) and reads the channel list. Re-connecting the same Slack team updates its
  token (and area) instead of adding a second copy. Errors (400): not `xoxp-…` ("Paste the User OAuth Token. It
  starts with xoxp-."), Slack refused it ("Slack didn't accept that token. Copy the User OAuth Token again."),
  missing scope ("The Slack app is missing a read permission (<scope>). Create it again from the manifest."),
  Slack unreachable (502).
- `PATCH api/slack/<id> {ignored: [channel ids]}` → workspace object. Replaces the ignore list (ids must be
  channels in the workspace's channel list; at most 500).
- `GET api/slack/<id>/channels` → `{channels: [{id, name, kind: "channel"|"private", ignored}], fetched_at,
  stale}`: public and private channels the owner is in, sorted by name. Read live from Slack; when that fails,
  the list saved at the last sync with `stale: true`.
- `DELETE api/slack/<id>` → `{ok: true}`. Deletes the token and the workspace's saved state. (Callsheet can't
  uninstall the app; DOCS.md tells the owner to remove it in Slack if wanted.)

Workspace object (also in `api/state` under each area as `slack: [...]`):
`{id, area_id, team_id, team_name, domain, user_id, user_name, token_hint, status: "connected"|"needs_reconnect"|
"error", error, channels_total, ignored: [channel ids], last_read_at}`.

`api/state` areas also gain `chat_available` (true when the area has a connected Slack workspace). `reads.chat`
already exists (default true); Slack is read only when `reads.chat` is true. Every area is in the state as before.

## Slack sync and rules
- Per workspace whose area reads chat: `conversations.list` (types public_channel, private_channel, mpim, im;
  exclude_archived; member channels only; pages capped), then:
  - DMs and group DMs: `conversations.history limit=10` (latest messages, no window), at most 40 conversations,
    most recently active first (`priority`, then `updated`, then `created`), deleted users and Slackbot skipped.
  - Channels (not ignored): `conversations.history oldest=<window start>`; window = since the workspace's last
    successful read (minus 5 min overlap), at most 24 h back.
  - Threads: `conversations.replies` for threads the owner posted in (seen in a read, kept 7 days, at most 20
    newest) and for unanswered @mentions in threads (kept 14 days).
  - `users.info` for senders not seen before (name and, when Slack gives it, email; cached per workspace).
  - Saved per workspace (ids and timestamps only, no message text): channel list, user names, last read time,
    pending mentions `[channel, ts, thread_ts]`, watched threads, the owner's last post time per channel.
- A row needs a reply when the last message is not from the owner and at least one reason applies:
  - candidates: DM/group DM where the other person spoke last (≤ 14 days old); a message with `<@owner>`
    not answered by the owner later in that thread (or, outside threads, later in that channel); the newest reply
    by someone else in a thread the owner posted in, after the owner's last post there.
  - reasons, in this order: `VIP` (sender's Slack email matches a VIP email, else display/real name equals the VIP
    name, case-insensitive), `Mentioned you`, `In your thread`, `Direct question` (a "?"), `Mentions a deadline`
    (same regex as mail), `Waiting Nh` (3 h or more; `Waiting N days` from 48 h).
  - one row per DM, per thread, or per top-level mention. Bot messages and join/leave messages are ignored.
- Row: `{id, key, who, area, source: "Slack · #name"|"Slack · DM"|"Slack · group DM", via: "slack", text:
  "\"<message ≤118>\"", short, age ("5h", "2d"), reasons, vip, action: "Open", link}`; link
  `https://<domain>.slack.com/archives/<channel>/p<ts without dot>` plus `?thread_ts=<thread_ts>&cid=<channel>` for
  thread messages. Slack markup is shown as text (`<@U1>` → `@Name`, `<#C1|x>` → `#x`, `<!here>` → `@here`, links
  → their label or address).
- `<!channel>`, `<!here>`, `<!everyone>` from others in read channels → Heads up
  `"@channel in #general: <text>"` (whole line ≤ 100 chars, at most 5 per workspace).
- Problems → Heads up `"Couldn't read Slack <team name>: <reason>"`; `invalid_auth`/`token_revoked`/`account_inactive`
  → status `needs_reconnect`.
- Sources line gains `", 1 Slack workspace"` when one or more is read.

## Brief JSON (`GET api/brief`) additions
- Schedule rows: `id` (`"s1"`, `"s2"`, … in brief order) and `key` (stable across the day's briefs).
- Reply rows: `id` (`"r1"`, … VIP first, then oldest), `key` (stable: source, thread and last message), `via:
  "mail"|"slack"`; `new: true` (see "New since"). Mail rows keep `source: "Mail"`.
- `top`: always present (list, ≤ 5, may be empty): `[{text, ref, kind: "reply"|"event"}]`, `ref` = a current row
  id. From the writer when it ran (`top_by: "writer"`), else rules-based (`top_by: "rules"`): VIP replies, then
  clashes (`ref` = the earlier event), then replies that mention a deadline.
- `summary` (writer on and ran): `{text (3–6 sentences, ≤ 1200 chars), by, at, areas: {area_id: text}, top: [...]}`;
  `summary.top` = `top` when the writer wrote it. Items whose `ref` is no longer a row are dropped when the brief
  is composed. No area summaries without the writer.
- `hidden`: number of rows hidden by "Doesn't need a reply" rules in this brief (0 when none).
- `previous_at`: `"HH:MM"` of the latest earlier brief today, or null for the day's first brief.

## Writer output (both writers)
Schema per request (`writer.build_schema(ids, ask)`): `{summary: string, areas: {<area id>: string minLength 1, …
all required, additionalProperties false}, top: array maxItems 5 of {text: string minLength 1, ref: enum of the
reply ids and schedule ids sent}, replies: {…as before}}`, all required. With no refs, `top` is `maxItems 0`.
Input gains `areas: [{id, name}]` (areas present in today's data) and schedule `id`; reply rows gain `source`.
Slack rows carry their last up to 3 messages `{from, date, text}` (text capped at 4000 chars, Slack markup as text).
Areas with the writer off stay out (as before). Parsing: unknown area ids and refs are dropped, top ≤ 5 without
duplicate refs, area texts ≤ 600 chars, top texts ≤ 200 chars. A missing or bad `areas`/`top` is not a failure.
The owner-run evaluation (`evaluate.py`) keeps the 0.3.x shape (summary + replies).

## Writing instructions (editable)
- Stored in the writer settings: `prompts: {summary, areas, drafts}`; only edited values are stored, the rest use
  the defaults, so better defaults reach owners who never edited them.
- `GET api/writer` → the writer object (as in `api/state.writer`), which now has `prompts: {summary, areas,
  drafts}` (the texts in use), `prompt_defaults: {…}`, `prompts_edited: {summary: bool, …}`, `prompt_max: 1500`.
- `PATCH api/writer {prompts: {summary?, areas?, drafts?}}`: text ≤ 1500 chars, no control characters except
  newline and tab; empty text = back to the default.
- `POST api/writer/prompts/reset {key: "summary"|"areas"|"drafts"|"all"}` → the writer object.
- System prompt = fixed intro + "Day summary" + "Area summaries" + "Suggested replies" (the three editable texts)
  + the fixed safety block, always last: the untrusted-`<data>` rule, the never-claim rule, the JSON instructions.
  The safety block can't be edited or removed; whatever an editable text says, it comes after it.

## Doesn't need a reply
- Gmail: a thread whose last message has `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS` or
  `CATEGORY_SOCIAL` is never a reply row, even in the inbox, unless the sender is a VIP.
- Rules `{id, kind: "thread"|"sender"|"domain", value (lowercase), source: "mail"|"slack", created (ISO time),
  label}` (`label`: what the UI shows, e.g. `"Priya Nair <priya@acme.example>"`, `"acme.example"`,
  `"Slack · #general thread"`). In `api/state` as `suppress: [...]`.
- `POST api/suppress {row_id, scope: "thread"|"sender"|"domain", key?}` → `{rule, hidden}`. The value comes from
  the row in the current brief: mail thread = Gmail thread id, sender = address, domain = the address's domain;
  Slack thread = `<channel>:<thread ts, or the message ts outside threads>`, sender = Slack user id, no domain
  (400). With `key`, a row whose key differs (the brief changed since the page loaded) → 409. The row disappears
  from the cached brief at once and `hidden` goes up. The same rule twice returns the existing rule.
- `DELETE api/suppress/<id>` → `{ok: true}` (rows come back at the next refresh).
- Applied in every sync for mail and Slack. VIPs win over sender and domain rules; a thread rule always applies.

## New since
- The server keeps, per day, the row keys of every brief written that day. A later brief marks schedule and reply
  rows whose key wasn't in any earlier brief that day with `new: true`, and has `previous_at` (the latest earlier
  brief's time). The day's first brief has no `new` marks and `previous_at: null`. The writer evaluation never
  changes this.

## Upgrade
Additive only: a new `slack` table (`CREATE TABLE IF NOT EXISTS`), new meta keys (`suppress`, `seen_day`,
`brief_refs`, writer `prompts`) read with defaults. A 0.3.4 database opens unchanged.
