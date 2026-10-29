# Step 2: Google accounts (own OAuth client), real Schedule and Needs a reply

Decided 2026-10-28 (user): option 1, each Callsheet owner uses their **own Google Cloud OAuth client**
and Callsheet calls Google's REST APIs directly. HA is only the remote access path to the page.
HA calendar entities may become an optional extra source later (not in this step). No AI writer yet:
plain rules build the brief.

## Hard rules
- Read-only. Scopes: `openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly`.
  The Google client code only issues GET requests to Gmail/Calendar; the only POSTs allowed go to the token and
  revoke endpoints. A test enforces this.
- Python stdlib only (urllib, sqlite3, zoneinfo, threading). Image adds `tzdata` via apk.
- Secrets (client secret, refresh/access tokens) live only in `/data/callsheet.db` (file mode 0600), are never
  returned by the API (only hints such as the last 6 characters of the client id), and are never logged.
- Data dir: env `CALLSHEET_DATA` (default `/data`). Google base URLs overridable only by env for tests:
  `CALLSHEET_GOOGLE_AUTH_BASE`, `CALLSHEET_GOOGLE_TOKEN_URL`, `CALLSHEET_GOOGLE_API_BASE`.
- Mutating requests (POST/PATCH/DELETE) must carry header `X-Callsheet: 1`, else 403 (simple CSRF guard).
  All URLs relative (ingress). JSON bodies, JSON responses, errors as `{"error": "<plain sentence>"}` with 4xx/5xx.

## OAuth flow (Desktop app client, loopback redirect, paste-back)
1. Owner creates a Google Cloud project, enables Gmail API + Google Calendar API, OAuth consent screen External,
   **publishes it to "In production"** (Testing mode expires refresh tokens after 7 days), creates an OAuth client of
   type **Desktop app**, pastes client ID + secret into Callsheet. (Workspace accounts: their admin may need to allow
   the client ID.)
2. `POST api/accounts/start {area_id}` → server makes state + PKCE (S256) verifier, keeps them in memory for 15 min,
   returns `{flow_id, auth_url}`. auth_url = `https://accounts.google.com/o/oauth2/v2/auth` with client_id,
   `redirect_uri=http://127.0.0.1:8765/`, response_type=code, scope (above), access_type=offline, prompt=consent,
   include_granted_scopes=true, state, code_challenge, code_challenge_method=S256.
3. Owner approves; the browser lands on an unreachable `http://127.0.0.1:8765/?state=…&code=…&scope=…` page.
   Owner copies that address and pastes it into Callsheet.
4. `POST api/accounts/finish {flow_id, pasted}` → accepts the full URL or a bare code; checks state; exchanges the
   code at the token endpoint (with the same redirect_uri and code_verifier); reads the email from the id_token payload
   (or `gmail/v1/users/me/profile`); stores the account under the area; fetches its calendar list (primary selected by
   default); returns the account object. Re-connecting an email that exists updates its tokens.
   Errors in plain words: wrong/expired flow, state mismatch, Google said invalid_grant ("That sign-in code was already
   used or expired. Start again."), missing scopes ("Tick both Gmail and Calendar on Google's consent screen.").

## API (all relative)
- `GET api/brief` → brief JSON in the shape of `sample_brief.json`. Each section object or list gains provenance:
  top-level `"sample"` stays (true only when nothing is connected), plus `"sections": {"schedule": "live|sample",
  "replies": "live|sample", "headsup": "live|sample", "projects": "sample"}`. When live, `areas` comes from the
  configured areas. `written_at` = last sync time; `next_checkin` = next enabled call time.
- `GET api/state` →
  `{google_client: {configured, client_id_hint}, areas: [{id, name, colour (1-4), reads: {calendar, mail, chat, tasks},
  fixed (true for Personal), accounts: [{id, email, status: "connected|needs_reconnect|error", error, services:
  {calendar, mail}, calendars: [{id, summary, primary, selected}]}]}], vips: [{id, name, email, area_id}],
  settings: {name, timezone, workdays: ["mon",…], reply_age_days, call_times: [{id: "morning|midday|afternoon",
  label, time "HH:MM", enabled}], skip_checkins_days_off, look, mode}, last_sync: {at, ok, errors: [plain strings]},
  setup_done}`
- `POST api/google-client {client_id, client_secret}` / `DELETE api/google-client`
- `POST api/areas {name, colour}` → area; `PATCH api/areas/<id> {name?, colour?, reads?}`; `DELETE api/areas/<id>`
  (Personal cannot be deleted; deleting an area deletes its accounts and revokes their tokens).
- `POST api/accounts/start`, `POST api/accounts/finish` (above); `PATCH api/accounts/<id> {services?, calendars?:
  [selected calendar ids]}`; `DELETE api/accounts/<id>` (revokes at Google, best effort).
- `POST api/vips {name, email, area_id}` → vip; `DELETE api/vips/<id>`
- `PATCH api/settings {…any settings keys}`
- `POST api/sync` → runs a sync now (blocking, max ~60 s), returns `last_sync`.

## Sync and rules (rules only, no AI)
- Scheduler thread: runs a sync at each enabled call time in the configured timezone (skip check-ins on days off
  when set), plus once at start-up if the last sync is older than 30 min. Brief cached in the DB.
- Calendar: for each selected calendar of accounts whose area reads calendar: `events.list` today 00:00–24:00 local,
  singleEvents=true, orderBy=startTime. Skip cancelled, transparent ("free") and declined events.
  Schedule rows: time, title, area, note (location or first line of description, ≤80 chars), flags:
  "Not accepted yet" when own responseStatus is needsAction; "Overlaps HH:MM <title>" for clashes (timed events that
  overlap another non-free timed event). All-day events go to Heads up ("All day: <title>").
  First meeting = first timed event with at least one other attendee, else first timed event.
- Gmail (areas that read mail): `threads.list` q=`in:inbox -category:promotions -category:social -category:forums
  newer_than:14d`, maxResults 50; `threads.get` format=metadata, metadataHeaders From/To/Cc/Subject/Date/Message-ID (+ list headers).
  A thread needs a reply when the last message is not from the account itself and at least one of:
  the last message's snippet or subject contains a question mark ("Direct question"); unanswered for ≥
  reply_age_days ("Waiting N days"); snippet or subject mentions a deadline (regex: deadline, due, by
  (mon|tue|…|tomorrow|today|eod|end of day|\d)) ("Mentions a deadline"); sender is a VIP ("VIP").
  Row: who (display name), area, source "Mail", subject or snippet quote, age ("3d", "18h"), reasons, link
  `https://mail.google.com/mail/u/?authuser=<email>#search/rfc822msgid:<Message-ID>` (fallback `#all/<threadId>`). Sorted VIP first, then age.
- Heads up: all-day events, plus sync problems ("Couldn't read <email>: <reason>").
- Token refresh on 401/expiry; invalid_grant → account status needs_reconnect, brief shows it in Heads up.
