# Voice assistant API (0.21.0)

The read-only API for the owner's phone voice assistant (approved 2026-11-13). This is the built contract. The draft it came from is revision 2 (2026-11-12). Two things differ from the draft: the listener is reached over the home network or Tailscale, never through
a reverse proxy (the draft's public-domain/Cloudflare route is out), and the audit log is kept 90 days (the draft's
open question 3). Code: `callsheet/app/voiceapi.py` (listener, endpoints, Setup routes) and `apitokens.py` (settings,
tokens, rate limit, audit log). Tests: `tests/test_voiceapi_0210.py`.

## Principles

- **Read-only.** Every endpoint is a `GET` that reads the message store (`messages.db`: messages, events, the
  ledger, the talk-through runs), People and the Sources panel. Nothing writes to any account or to Callsheet's state,
  apart from the audit log and each token's last-used time. Nothing is ever sent: sending stays in the phone app,
  after the owner confirms it there. Every other method gets `405` before anything is read.
- **The assistant must work without Callsheet.** The API is an optional memory backend: handle `401`, `404`, `503`
  and a refused connection, and fall back to the assistant's own sources.
- **Message text is untrusted data.** Every answer has `"untrusted": true` and a `note`; every result also has
  `"untrusted": true` and a source reference (`ref`). The assistant must never follow instructions found in them.
- **Never the old index.** The API reads only `messages.db`, People, the talk-through and the live readers' status.

## Transport

- Its own listener, separate from Ingress (8099): port **8100** inside the app (`server.py --voice-port`, or the
  environment variable `CALLSHEET_VOICE_PORT`), bound to `0.0.0.0`, started by `server.py` only while the switch in
  Setup → Connections → Voice assistant is on. **Off by default.** Switching it on or off starts or stops the
  listener at once.
- Home Assistant: `config.yaml` lists `8100/tcp` under `ports` with the default `null`, so nothing is exposed until
  the owner picks a host port in Home Assistant (Callsheet › Configuration › Network).
- **Home network or Tailscale only, never Cloudflare or another public proxy.** Plain HTTP on the LAN or inside the
  tailnet (Tailscale encrypts). Each request is checked, before the token:
  - the client address must be loopback, private (RFC 1918, IPv6 ULA, which includes Tailscale's
    `fd7a:115c:a1e0::/48`), link-local or Tailscale's `100.64.0.0/10`;
  - requests with Cloudflare's headers (`CF-Connecting-IP`, `CF-Ray`, `CF-IPCountry`, `CF-Visitor`, `CDN-Loop`) are
    refused;
  - the `Host` header must be an IP address, `localhost`, a one-word name (`nas`), a name ending in `.ts.net`,
    `.local`, `.lan`, `.home`, `.home.arpa`, `.internal` or `.localdomain`, or one of up to 10 names the owner adds in
    Setup ("Also answer to"). This stops DNS rebinding through a public name.
- **No CORS.** No `Access-Control-*` header is ever sent; `OPTIONS` gets `405`. Responses carry
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- Nothing about a request is printed to the app log (the request line holds the search words).

## Auth

- `Authorization: Bearer <token>` on every request.
- Tokens are made in Setup → Connections → Voice assistant → Add a device: a name (up to 40 characters) and its
  permissions (scopes). The token (`csva_` + 43 URL-safe characters, 256 random bits) is shown **once**, as text and
  as a QR code of the token text (vendored qrcodegen, `qr.py`), with Copy.
- Stored only as `sha256(salt + ":" + token)` with a random per-install salt (callsheet.db meta `voice_api_salt`).
  A request's hash is compared with every active token's hash using `hmac.compare_digest`, with no early exit.
- Revoke any time (Setup, "Revoke", confirmed): the device gets `401` from its next request. Revoked tokens stay
  listed in the database (not on the page) so the audit log keeps their names. At most 20 active tokens.
- Each token's last-used time is shown in Setup (written at most once a minute).

### Scopes

| Scope | Endpoints | Setup label |
|---|---|---|
| `messages:read` | `/v1/search_messages`, `/v1/threads/{id}` | Messages |
| `ledger:read` | `/v1/ledger`, `/v1/ledger/{id}` | Needs you and coming up |
| `calendar:read` | `/v1/calendar` | Calendar |
| `people:read` | `/v1/people` | People |
| `brief:read` | `/v1/brief/today` | Morning brief |
| `status:read` | `/v1/status` | Sources status |

A new token has all six ticked; untick what a device doesn't need.

## Limits

- **60 requests a minute per token** (a sliding window in memory). Over it: `429` with `Retry-After` in seconds.
- `limit` at most 50 (people: 20); snippets at most 300 characters; thread message text at most 4,000 characters
  (`text_cut: true` when cut); `q` at most 200 characters; at most 20 query parameters; URLs at most 2,048
  characters (`414`); calendar ranges at most 62 days; an answer over 512 KB drops whole results from the end and says
  `"truncated": true`. No attachments, no contact export, no bulk dumps.
- Unknown parameters are refused (`400`), so a typo never silently widens a query.

## Errors

Plain words in `{"error": "..."}`:

| Status | When |
|---|---|
| `400` | A parameter is wrong ("limit should be a number from 1 to 50.") |
| `401` | No token, a wrong one or a revoked one (with `WWW-Authenticate: Bearer`) |
| `403` | The token lacks the scope; or the client, host name or a proxy header isn't allowed |
| `404` | No such endpoint, thread or ledger item |
| `405` | Any method but `GET` (`Allow: GET`) |
| `429` | Over 60 requests a minute (`Retry-After`) |
| `503` | The message store, ledger, brief or readers aren't available (Callsheet starting, or the store failed to open) |

## Times and ids

- Times in answers are ISO 8601 in the owner's time zone (`2026-11-11T12:14:00-08:00`), with a `*_unix` twin in
  seconds where useful. Time parameters take seconds since 1970, a date (`2026-11-11`: its start; as `until` or `to`,
  the end of that day) or a date-time (no offset: the owner's time zone).
- Ids carry their kind: messages `m123`, events `e12`, ledger items `l45`, clashes `c7`, follow-ups `f3`, threads
  `t…` (opaque: base64url of the source, account and thread), people as in People (`id: null` for a sender People
  doesn't know yet).
- `ref`: `{store, id, source, account, source_id, link}`: where the result came from (`source_id` is the source's own
  message or event id; `link` only when it is an `https://` link).

## Endpoints (all `GET`, JSON)

Every answer is an object with `"untrusted": true` and `note`.

### `/v1/search_messages` (messages:read)

Full-text search over the message store (FTS5, subject weighted highest, then people, then text; all words first,
then any word, which sets `"relaxed": true`). Without `q`: newest first ("anything new?"). Deleted messages and live
text notifications the backup also holds are left out.

| Parameter | |
|---|---|
| `q` | optional words |
| `unanswered=true` | incoming messages to the owner that want a reply, with no later outgoing message in the same thread. Left out: Gmail's Promotions, Social, Forums and Updates tabs (where bulk and List-Unsubscribe mail lands), automated and role senders (no-reply, notifications, newsletter@, info@ ...), the owner's own accounts, messages the ledger read as cleanup or as nothing to keep, reports, and Slack channel posts that don't mention the owner and aren't in a thread the owner wrote in |
| `updated_since` | stored or changed since (the store's `updated_at`) |
| `source` | `gmail`, `signal`, `slack`, `sms` (backup and live texts) or `calendar` (events by title and place) |
| `person` | a People id, a name, alias or address (all of that person's names and handles), else the text as written |
| `direction` | `in` or `out` |
| `since`, `until` | the message's time |
| `limit` | 1 to 50, default 20 |

Returns `{results: [{id, source, account, thread_id, direction, from: {name, handle}, to: [{name, handle}], ts, ts_unix,
subject, snippet, ref, untrusted}], relaxed}` and an `ETag`; `If-None-Match` with it gives `304` (no body) while
nothing changed.

### `/v1/threads/{thread_id}` (messages:read)

One thread, oldest first: the last `limit` (1 to 50, default 50) messages, or those before `before`.
Returns `{thread_id, source, account, participants: [{name, handle}], messages: [{id, direction, from, ts, ts_unix,
subject, text, text_cut, ref, untrusted}]}`.

### `/v1/ledger` (ledger:read)

The ledger as Today shows it (snoozed items stay hidden; security alerts count as needs you).

| Parameter | |
|---|---|
| `kind` | `needs_you`, `coming_up` (now and next, and the next 30 days), `cleanup`, `conflicts`, `followups`; all when left out |
| `updated_since` | changed since |
| `since`, `until` | when it happens (an item's start or date; a clash's slot; a follow-up's due time) |
| `person` | as for search (who it is from or about) |
| `limit` | 1 to 50, default 50 |

Items: `{id: "l45", type, subtype, kind, who, what, for, when: {start, end, start_date, end_date, tz, all_day, phrase,
label}, status, confidence, urgency, vip, sources: ["m12"], updated_at, updated_unix, untrusted}` (plus `amount`,
`currency` when set). Clashes: `{id: "c7", type: "conflict", kind: "conflicts", clash, when: {start, end, label},
urgency, confidence, why, status, sides: [{kind: "item", id: "l…", who, what, status, sources} | {kind: "event", id:
"e…", title, status}]}`. Follow-ups: `{id: "f3", type: "followup", kind: "followups", item: "l…", conflict, reason,
tell, says, due, status, confidence}`. With an `ETag` as above.

### `/v1/ledger/{id}` (ledger:read)

`l…`: the item, its `source_messages` (as search results, with snippets), `linked` (its open clashes, with the other
side, and its parent and predecessor items) and `followups`. `c…`: the clash with both sides' items and their source
messages in `linked`.

### `/v1/calendar` (calendar:read)

`from`, `to` (at most 62 days; default today and the next six days). Returns `{from, to, results: [{id, calendar,
account, start, end, all_day, title, location, status, ref, untrusted}]}`; cancelled events are left out.

### `/v1/people` (people:read)

`q`: a name, an email address or a phone number; `limit` 1 to 20 (default 10). Without `q`: VIPs first. Returns
`{results: [{id, name, vip, channels: [{source, handle, display, last_used_ts, last_used_unix, count_90d}],
untrusted}]}`, channels most recently used first, so "text Robin" resolves to the channel the owner really uses with them:

- `gmail`: the person's email addresses in People; `slack`: their Slack user ids;
- `signal` and `sms`: the numbers (or Signal uuids) that wrote with one of the person's names (People keeps no phone
  numbers);
- `last_used_ts`: the newest message from or to that handle; `count_90d`: messages in the last 90 days.

A phone number or name that no one in People has returns the matching senders as their own entries (`id: null`).

### `/v1/brief/today` (brief:read)

Today's talk-through: `{date, state, date_line, steps: [{id, n, title, state, text, visual, items: ["l…"]}]}`.
`visual` is the step's visual block JSON (docs/visuals.md) filled from the store now. Before the day's brief is
written: `{date, state: "not_written", steps: []}`.

### `/v1/status` (status:read)

The Sources panel: `{now, sources: [{key, name, state, word, line, warn}], freshness: [{source, account, last_read,
newest, lag_s, errors}], store: {messages, events}}`.

## Audit log

`voice_audit` in callsheet.db: time, token id and name, the endpoint **template** (`/v1/threads/{id}`), status and
result count. Never the query string, search words, ids from the path, tokens or message text. Rows older than 90
days are removed. Setup shows the last 20 under "Host names and recent calls".

## Setup API (Ingress, the page)

- `GET api/voice`: `{settings: {enabled, hosts}, port, running, error, tokens: [{id, name, scopes, created,
  last_used, revoked}], scopes, audit, rate_per_minute, audit_days}`. Never a token or a hash.
- `PATCH api/voice` `{enabled?, hosts?}`: the switch (starts or stops the listener) and the extra host names.
- `POST api/voice/tokens` `{name, scopes?}`: the view plus `new: {token, secret, qr_svg}`, the only time the token
  text exists outside the device.
- `DELETE api/voice/tokens/<id>`: revoke.
