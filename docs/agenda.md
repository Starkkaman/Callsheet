# Agenda, answering invites, and suggested projects (0.21.0)

Design approved 2026-11-13 ("Agenda & Projects answers"). Server: `agenda.py`, `rsvp.py`, `suggest.py`. Client: `T_agenda` and its `ag*`
helpers, `rsvpBox`, `pjSuggested` and `pjFiles` in `static/app.js`; CSS under "0.21.0 Agenda" in `static/app.css`.

## What the Agenda shows
One list of a day's (or week's, or month's) things. Each one is drawn from:
- **On your calendar**: stored calendar events (solid box).
- **Only in a message**: ledger items with a time that aren't on the calendar (dashed box), with where they came from
  and "Add to calendar".
- **You said**: the owner's own promises (teal box with a right edge). A promise with a date and no time reads
  **"By 5 pm"**.
- **Asks you**: someone's ask with a time (amber tag).
- **Clashes**: the ledger's clashes in place, both sides side by side, with Open both, the reply link (for example
  "Message Ana") and Dismiss. Red when it's under 24 hours away.
- **Free**: the free stretches between things.

Views:
- **Day**: a time column and the boxes; beside it "You said · with times", "Invites waiting" and (when there are any)
  "Only in messages".
- **Week**: seven columns, Monday to Sunday, with a summary line (clashes, things only in messages, promises, the
  most free day). Past days stay, dimmed and marked "done". A day's header opens it as a Day. On a phone the days stack.
- **Month**: a 30-day strip, then the month grid with **dots only** (filled = on the calendar, ring = only in a
  message, triangle = clash, red triangle = clash under 24 hours). Pressing a day shows that day's rows below it and
  "Open the day". "At a glance" counts the month.

Past items stay visible, dimmed. Invites the owner said No to are hidden. Times are 12-hour; every label comes from
the server, and the page draws text only (`h()`, `textContent`). URLs are relative.

## Answering invites (RSVP)
Approved 2026-11-13 (answer invites). Yes, No and Maybe appear on a calendar invite in the Agenda's
"Invites waiting", on Today's Needs-you invite rows and in the item view (and on a day row whose invite isn't in
"Invites waiting", so the owner can change a Maybe).
- They show the owner's current answer ("Not answered", "Yes", "No", "Maybe"); the pressed one is marked.
- Pressing one sends `POST api/agenda/rsvp` `{item | event, answer: yes|no|maybe, via: agenda|today|item}` with the
  `X-Callsheet` header. The server reads the event (`events.get`) and makes the one allowed call: `PATCH
  .../events/<id>?sendUpdates=all` with `attendees` only, the owner's own entry's `responseStatus` changed
  (`google.check_rsvp_body`). Google tells the organizer; nothing else is sent.
- The result is said in place and in a toast ("Answered Yes. Google told the organizer."). An error is said plainly
  (a 403 is never retried) and nothing changes.
- Without "Allow adding events" (`calendar.events`) for that account, the keys are off and the row says how to allow
  it: Setup › Connections.
- Mail, messages, feeds and tool results can never offer, fill or press it; Chat has no RSVP tool.

## Add to calendar
The Agenda's dashed rows use Today's "Add to calendar" card (`calwrite.py`). When the message gave a start time and no
end, the card asks **Ends** (`ask_end`); the length sent is the time from start to end (an end before the start is
the next day). Only Add writes, one event per item.

## Projects
- **Suggested** (one card, owner-accepted only): projects that Find projects drafted (state `suggested`) and topics
  with 5 or more messages with the same people (`suggest.py`), on a **Personal** and a **Professional** side. Add makes
  the project (`POST api/projects/<id>/accept` for a drafted one, `POST api/projects/suggested/<key>/add` for a
  message one); "Not a project" is remembered. Nothing becomes a project until the owner presses Add.
- **Files · Drive**: on an open project, the Drive files whose names or people match it (`GET api/projects/more`),
  each with Open (Google's own page, read-only).
- **Slipping** and **Weekly review** are the existing views (renamed from "Slipped").

## API
- `GET api/agenda?view=day|week|month&date=YYYY-MM-DD[&sel=YYYY-MM-DD]`: `{view, date, today, legend, can_add,
  cal_targets, header, day | week | month, said, invites, only_msgs}`.
- `POST api/agenda/rsvp`: above.
- `GET api/projects/more`: `{suggested: {personal, professional}, files: {project id: [...]}}`.
- `POST api/projects/suggested/<key>/add` and `.../not`.

Tests: `tests/test_rsvp_0210.py`.
