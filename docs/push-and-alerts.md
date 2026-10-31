# Phone notifications and urgent alerts (0.5.1)

Callsheet sends short notifications to the owner's own phones through Home Assistant's companion app
(`notify.mobile_app_*`). Nothing is sent to anyone else; the read-only and drafts-only rules are unchanged.

## Home Assistant access (`ha.py`)
- `config.yaml` has `homeassistant_api: true`; the Supervisor then sets `SUPERVISOR_TOKEN` and proxies the Core
  API at `http://supervisor/core/api`.
- Every request goes through `ha.request()`, which checks a (method, URL) allowlist before any network I/O:
  `GET <core>/services` (list notify targets) and `POST <core>/services/notify/mobile_app_<name>` with
  `<name>` matching `[a-z0-9_]{1,64}`. Query strings, fragments, `%`-escapes, `..`, credentials in the URL, other
  hosts and every other path (states, events, templates, other services, other notify services) raise
  `NotAllowed`. Redirects are not followed.
- The token is read from the environment per request and only put in the `Authorization` header. It is never
  logged, stored, returned or put in an error. Without it, `available()` is false and nothing is requested.
- Errors in plain words: 400/404 on a notify call = the phone is gone ("Home Assistant doesn't know Pixel 9a
  (notify.mobile_app_pixel_9a) any more…"); 401/403 = no access (restart or reinstall the app); 5xx; network;
  timeout.
- Friendly names come from the service id: `mobile_app_isaac_ipad` → "Isaac iPad", `mobile_app_pixel_9a` →
  "Pixel 9a", `mobile_app_iphone` → "iPhone".
- `CALLSHEET_HA_API_BASE` (tests only) points at a fake Core API (`tests/fake_ha.py`).

## Deep link
Both companion apps open a relative path inside the app when the notification is tapped: iOS reads
`data.url`, Android reads `data.clickAction`. Callsheet sets both to `/hassio/ingress/<app slug>`, the Home
Assistant frontend's page for an app's Ingress UI (it works whether or not the sidebar entry is shown). The slug
(`<repository hash>_callsheet`, or `local_callsheet`) is not in the environment, but the Supervisor names the
container's host after it with `_` written as `-`, so `push.app_slug()` reads `HOSTNAME` (`a1b2c3d4-callsheet` →
`a1b2c3d4_callsheet`). `CALLSHEET_APP_SLUG` overrides it. When neither fits, the link is left out and a tap opens
the Home Assistant app's start page.

## Settings (`meta` key `push`, `store.push_config` / `update_push`)
| key | default | rule |
|---|---|---|
| `targets` | `[]` | up to 5 `mobile_app_*` ids, duplicates dropped; not checked against Home Assistant on save |
| `at_call_times` | false | bool |
| `urgent` | false | bool |
| `quiet_start`, `quiet_end` | `22:00`, `07:00` | `HH:MM`; start == end means no quiet hours; may cross midnight |
| `max_per_hour` | 3 | 1 to 10 |
| `show_subject` | true | bool; off = the sender's name only: "New urgent message from Priya", and the summary's top item without subject or event title |

## After a scheduled brief
The scheduler calls `Notifier.after_brief()` after a sync it started for a call time (not the start-up sync and
not Refresh). It moves the light check's marker to now and clears held alerts (the brief covers them). With
"At call times" on, it sends the cached brief (if written in the last 15 minutes) as:
- title: the brief's kind ("Midday check-in"); message: "4 to reply, 3 events today. Top: Dana Lee, Budget
  sign-off" (at most 120 characters). The top item is the first of `rules.rules_top`: a mail row shows the sender
  and its subject (never a quoted snippet), a Slack row shows "Slack", a clash shows the event time and title.
  The writer's wording and gists are never used.
- `data.tag = "callsheet-brief"`, so a newer summary replaces the older one. Quiet hours don't apply: the owner
  chose the call times.

## The light check (`Notifier.check`)
- Due (`push.check_due`) when urgent alerts are on with at least one phone, not a day off (when "Skip check-ins on
  days off" is on), outside quiet hours, more than 10 minutes from any enabled call time (before or after), and 10
  minutes after the last check or brief. The scheduler asks every tick (30 s) when no call time is due.
- Skipped while a sync or a writer evaluation holds the sync lock; the check never takes that lock itself, so
  Refresh never waits for it. Never runs the writer, a model or a sync.
- Looks back to the last check (at most 60 minutes; 10 minutes on the very first check).
- Mail, per connected account whose area reads mail: `threads.list` with
  `in:inbox -category:promotions -category:social -category:forums after:<epoch>` (10 at most), then
  `threads.get format=metadata` per thread (the existing read-only calls). Only the latest message counts, and
  only when it is newer than the look-back, in INBOX, not sent by the owner.
- Slack, per workspace whose area reads chat: one `conversations.history` (`oldest` = look-back, `limit` 5) per
  VIP's DM channel, at most 10 per workspace (the client's request cap). DM channel ids come from the brief's saved
  Slack state (`state.dms`, ids only); a new VIP DM shows up after the next brief. One alert per DM per check.
- Token refresh uses the sync's code (`Syncer._token`); account status and Slack state are not changed.

## Urgent rules (rules only)
`urgent_reason(vip, subject, snippet, inbound)`:
1. `vip`: the sender is a VIP (by email; Slack by email, else name, like the brief).
2. `urgent`: subject + snippet has "urgent"/"urgently", "emergency" or "asap"/"a.s.a.p." (whole words), or "today"
   together with a separate deadline: `rules.DEADLINE` ("due", "deadline", "by Friday", "by 5", "by end of day") or "today",
   "tonight", "EOD", "end of day", "this morning/afternoon/evening", "within the hour", "right away",
   "immediately", "by noon", "by 5pm". "today" alone or twice doesn't count; "Urgent: server down" does.
3. `claim`: `writer.states_decision` on the inbound text (the subject unless it is a reply or forward, plus the
   snippet): the sender says something was approved, paid, signed and so on (a pure text check).
Skipped unless VIP: automated or bulk mail (`rules.is_automated`), Gmail's category tabs. The owner's "Doesn't need
a reply" rules apply as in the brief (thread rules always; sender and domain rules not for VIPs).

## Dedupe, rate limit, batching (`push.plan`)
- Ids: `m:<account>:<message id>` and `s:<workspace>:<channel>:<ts>`; the last 500 are kept in `seen`.
- At most `max_per_hour` alerts in any rolling hour (`sent` keeps their times). With room for R and N new items:
  N ≤ R → N alerts; else R−1 alerts (VIP, then urgent, then claim; newest first) and one "K more urgent messages.
  Open Callsheet to see them." alert. With no room, the items are counted in `held` and go into the next batch;
  a scheduled brief clears `held`.

## Notification texts
- Urgent: title "VIP message", "Looks urgent" or "Sender says it's done"; message "Sender: Subject" ("(no subject)"
  when empty), Slack "Name sent you a Slack message"; with `show_subject` off, "New urgent message from Name".
  Setup says next to that switch that notification text passes through Apple's or Google's push service. Never the snippet or body. Title ≤ 40, message ≤ 120
  characters (clipped with "…").
- Urgent payloads add `data.priority: high`, `data.ttl: 0` (Android) and `data.push.interruption-level:
  time-sensitive` (iOS). All carry `data.group: callsheet`.

## State (`meta` key `push_state`)
`last_check` (epoch), `seen` (ids), `sent` (epochs within the hour), `held` (count), `last_sent` {at, kind},
`last_error` {at, text}. No subjects, names or message text.

## API
- `GET api/push` → `{settings, names, available, link, last_sent, last_error, every_min, max_targets}`;
  `api/state` also has `push` (the settings).
- `PATCH api/push` (any settings keys) → same; 400 with a plain sentence when invalid.
- `GET api/push/targets` → `{available, targets: [{id, name}], error}`; 502 with Home Assistant's problem in words.
- `POST api/push/test` → the GET result plus `{ok, message}`; 400 "Pick a phone first." or outside Home Assistant.

## Logs
Counts only: `Callsheet push: urgent, 1 of 2 phones, 1 failed`, `Callsheet urgent check: 6 threads, 1 Slack DMs,
2 urgent, 2 sent, 0 held, 0 problems`.
