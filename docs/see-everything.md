# 0.8.0 "See everything"

Stage 0.8.0 of the 0.8 design (`~/project/daily-brief/understanding/design.md`, sections 5 and 6; mockup
`mockups/understanding-v1.html`, frames 1 and 3). Rules only: no AI, nothing new reaches the brief writer, and nothing
is answered, booked or sent.

## Server

- `ics.py`: a pure, capped iCalendar reader (256 KB, time zones incl. Windows names, recurrence first instances,
  METHOD/SEQUENCE, invisible-character and look-alike marks, defanged links).
- `gather.py`: calendar parts of Gmail messages, the invite merge (the Calendar API wins; an email can never cancel
  or move an event your calendar shows), invite rows, the calendar-not-read gaps, security heads-ups, Sorted for you,
  What changed snapshots, and the page wording (`invites_for_page`, `security_for_page`, `sorted_for_page`,
  `changes_for_page`).
- `horizon.py`: Coming up (kinds, the heads-up timing table, the owner's marks in `callsheet.db`'s meta).
- `sync.py` reads the calendars 60 days ahead and, per account, invite mail (new ones once, with `format=full` and the
  attachment GET for small calendar files) and the last day's promotions (headers only, 25 a sync; the rest counted).

## API

`GET api/brief` gains, for a live brief (the sample has none):

| Field | Shape |
|---|---|
| `invites` | `[{id, kind: calendar\|email, title, start, end, all_day, day, when, organizer, org_addr, account, area, sender: pass\|fail\|none, status, location, marks: [[code, text]], link, mail_link}]` |
| `cal_gaps` | `[{email, why: off\|none_ticked, text}]` |
| `security`, `security_n` | `[{id, who, subject, when, area, link, marks: [[code, text]], brand, lure}]`, the full count |
| `changed` | `[{kind, src, text, by}]` |
| `coming_up` | `{heads, weeks, months: [{key, title, kind, kind_text, d, m, when, rel, src, big, heads, mark, week}], n, list_max}` |
| `sorted` | `[{key: promos\|updates\|other, name, n, items: [{who, subject, when, link}], more}]` |

Reply rows may carry `uses_name` (a stranger using a VIP's display name) and `sender_fail` (a VIP's address that
Gmail rejected).

`POST api/horizon {key, action: got|week|notbig|big|undo}` returns `{ok, coming_up}`; 400 for an unknown action, 404
when the item isn't coming up any more.

## Page (app.js, app.css "0.8.0" sections)

- **Agenda**: the "calendar isn't read" hint (`gapHint`, button **Read that calendar** → Setup → Areas, the account's
  card focused), the timeline (email-only invites today as dashed pins in the messages column, never in the calendar
  column, so never a clash), **Invites to answer** (`invCard`: solid with a green bar for calendar invites, dashed
  for email-only ones, red dashed when the sender failed Gmail's check).
- **Setup → Areas**: the account card's warning (`gapCard`) with one button: **Choose calendars** opens the ticks;
  **Read its calendar** turns the account's or area's calendar on, then opens them. A calendar tick's whole row is its
  label (44 px on phones).
- **Briefing**: **Security heads-ups** after Top of the day, then By area, **What changed**, **Coming up** (Heads up
  today, Next 2 weeks and Next 2 months side by side on desktop; Got it, Remind me a week before, Not big on each item;
  Undo in the toast) and **Sorted for you** (count chips that open the list, 30 at a time, then Show all; "And N more
  in Gmail" when the server capped the list). At a glance gains **Answer: N invites**.
- Marks (`.mk8`): plain for facts, dashed for unverified, alert colour for sender doubts. Theme tokens only; Transit
  draws the Briefing sections as signs.

### Link rules

- Every link is one the server built: `rules.gmail_link` (Gmail) or a Calendar event's `htmlLink` kept by
  `gather.safe_cal_link`. The page builds no URL; `gLink(u, 'mail')` accepts only `https://mail.google.com`, and
  `gLink(u, 'cal')` only other `https://*.google.com` hosts.
- An email-only invite never gets a Calendar link; when Gmail's sender check failed (or it was cancelled) it has no
  answer link at all, only **Open the email**.
- ICS `URL`, `LOCATION` and join links are text (defanged by the server).

## Tests

`tests/test_see_080.py` and `tests/test_ics_fuzz_080.py` (server), `tests/test_see_page_080.py` (page: text only, no
URL built on the page, links only through `gLink`, unverified invites without a Calendar link and outside the
calendar column, `api/horizon`, tokens only; `gLink` run under node when installed).
