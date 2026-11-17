# Today (0.18.0, rework stage 4)

Today is made from the ledger (`docs/ledger.md`), its clashes and follow-ups, the calendar and the message store.
`callsheet/app/today.py` shapes everything the page draws, labels included (12-hour times, "in 3 days"), so `app.js`
only draws text with `h()`. Design: the approved v4 Today design (4 rounds).

## The groups (`GET api/today`)
- **Now and next.** Open clashes whose time hasn't passed, soonest first. Under 24 h (or under way) a clash is `red`
  (red edge, big red time, a red key with dark text and no orange beside it); under a week `amber` (▲); later `far`.
  A clash under 48 h or with a follow-up shows both sides (what each is FOR, one line each, the source and the age);
  a later one is one line that opens the clash. Side order: calendar, your promise, then the asks. Keys: Reply to X
  (the oldest open ask, or the person a follow-up is about: a link to the message in Gmail or Slack, `sgnl://` for
  Signal, `sms:` for texts), Open both, Remind me at… (with a follow-up), Dismiss. Only the first red clash (else the
  first clash) has a filled key. The card shows the push state (sent, held, failed). Below: the newest thing that
  cleared itself today ("Cleared 8:55 am · you wrote to Ana") and today's calendar with free gaps of an hour or more.
- **Security (0.21.0, heads.py).** Above the clashes in Now and next: every open security alert (sign-in alerts,
  password resets, new devices, codes; `rules.security_alert` on every stored Gmail message and text), newest first,
  as a card with a red edge, a lock, the word "Security", who, the subject, the source, the look-alike mark ("Not
  from Google: the address only looks like it") and Open in Gmail / See it / Done. The newest has the red key; no
  orange key on the screen then. Nothing but Done hides one: never in Clean up, never snoozed, never closed by the
  model. An alert more than 3 days old when Callsheet first sees it is kept as done.
- **Needs you.** Open asks and your own promises with no time (`You said`), oldest first, without clash sides or
  snoozed items: source icon, who, how long it has waited, one line of why. 4 rows on a computer, 3 on a phone.
  0.21.0: invites to answer are asks here ("RSVP: Team sync, Wed Nov 18, 2:00 pm") with Open in Calendar (Google's
  own event link) or Open in Gmail beside the row: calendar events where your answer is `needsAction` (one item per
  series, closed when you answer, it's cancelled or it's over) and invites in mail (Google's "Invitation: X @ <time>"
  subject or an `.ics` file; "the time is in the email" when there is none; a cancel closes it; an invite already on
  the calendar is the calendar's). Invites take part in clash checks (the invite stands in for its calendar entry)
  and never go to Clean up.
  0.22.0: what is late comes first after a VIP's asks: a past-due bill (an ask, with its amount and "5 days late"
  in red before the why) and any item whose deadline has passed (`ledger.late_of`), never dropped. A late invoice
  someone owes you is also a follow-up in Now and next ("Invoice #1043 · 15 days late: chase it ($2,400).", the
  days counted at each view) until a message says it's paid.
  0.22.0: a follow-up about a calendar entry that still has a plan the owner moved gets a reminder on the owner's phones 3 hours
  before the entry ("Kofi hasn’t heard about the change" / "The calendar still has it Wed 3:00 pm."; names and times
  only), unless the owner set one; when the entry moves, it clears ("Cleared 11:00 am · the calendar entry was updated").
- **Coming up.** Tomorrow to 29 days out, from ledger items with a time and from the calendar (an item matched to a
  calendar event shows once, as the event). A 30-day strip, This week / Next week / Later, "from email/text/Signal/
  Slack" marks. 0.21.0: "Add to calendar" (`calwrite.py`, owner-pressed only) once the account allows adding events.
- **From your channels, feeds and reports (0.21.0).** Below Coming up, quiet: the connectors' newest items
  (`connectors.brief_list`, `reports_list`) newest first, with the source's name and age and the marks as before
  ("Marked urgent by …", "Reads like instructions · shown as text"). 5 rows, then "n more". A feed item links
  through `cnLink` (none when flagged); a report is text only. Hidden when there are no connectors or nothing new.
- **Clean up.** Cleanup items grouped (newsletters, receipts, Slack chatter, other): a count, three samples, Done for the
  batch, Note…, See all (`GET api/today/cleanup`, with the notes so far).

## Item and clash views
`GET api/today/items/<id>` and `GET api/today/clashes/<id>`: a kicker, the title, how far off it is, **Where you
stand** (you said yes, no reply yet, you replied, on your calendar, waiting on them; from your outgoing messages), the
time with the phrase it came from and what the calendar has then, the keys, and the conversation(s): the newest 6
messages (the ask kept in view, "N earlier" for the rest), quoted text cut, the ask marked (ochre, "The ask", the
time phrase underlined) and your yes (teal, "Your yes"). A clash shows both threads. **Chat about this** starts a Chat
scoped to the item's threads (`POST api/chat/ask` with `item_id` or `clash_id`): the 0.7.0 project pattern, a filter
on Chat's existing read-only tools (`MessageStore.search` and `ChatView.search` take the scope's store ids), not a tool.

## Replies
`GET api/today/replies`: every open ask (clash sides too), oldest first, with a bar for how long each has waited. The
detail adds context: your calendar on the days asked about, a call with them today, a later message from them.

## Draft a reply (0.21.0, `replydraft.py`)
On an open ask, in the item view and in the Replies detail, **Draft a reply** (it replaced "Draft with Chat"; Chat
about this's "Draft a reply to <name>" does the same, and on a clash it opens that ask and drafts it there). Plain
code gathers the thread (the ask marked, newest 12 messages, each cut to 1,200 characters), what is asked and when,
the calendar on the asked days and the 3 after (free or the events), open clashes with the item, the active writing
profile's rules and reply texts, and the owner's saved notes that fit this person (`ownernotes.for_prompt`). One
request on Chat's path (Chat's server and slot, `writer._post(preempt=True)` inside Chat's activity mark, so
background work holds back); message text goes in as escaped `<data>`. Then the old suggestions' check: a draft that
states an approval, payment or agreement as done gets `writer.WARNING`; when the asked message itself claims
something was done (`triage.claims`) and the draft repeats a done-claim, it is held (not shown, with why).

The draft is kept per item in messages.db `ledger_drafts` (state `writing`, `ready`, `held` or `failed`, the text, the
warning, the claim, the asked message, how many times written), replaced by **Write again**. A `writing` row older than
15 minutes with no thread (a restart) reads as failed. `GET api/today/items/<id>` carries it as `draft` ({can, state,
text, warning, claim, why, kind: gmail|app|other, app, reply_all}) and `draft_sug` (the Chat suggestion that means
Draft a reply). The page shows the text in an editable box (`value`, never HTML) with **Copy** (outlined); for email
**Put in Gmail Drafts** (the one orange key while it shows; `POST api/today/items/<id>/gmail {text, reply_all}` →
`gdrafts.reply_stored`: account and ids from the store, To/Cc/Subject from the message's own headers read from Gmail,
Reply-To when present, else From; Cc only with Reply all ticked); for Signal, Slack and texts **Copy** and **Open in
<app>** (the app's own link). States drawn: none, writing (polled every 1.5 s), ready, saved to Gmail Drafts, held,
failed, and a refused (403) save said plainly.

## Presses (nothing is ever sent from here)
- `POST api/today/done {items}` and `api/today/undone {items}`: the ledger status with history (`kind` owner); a clash
  with a closed item resolves.
- `POST api/today/dismiss {clash}`: the clash is resolved as dismissed, its follow-ups and reminders end.
- `POST api/today/notes {items, chips, text}`: a note to the assistant kept in `today_notes` and in each item's history.
  `not_for_me` and `junk` mark the items done (the 0.8.5 learner they fed went with the sorter);
  `remind_tomorrow` hides them until 9:00 am tomorrow and then reminds you.
- `POST api/today/remind {followup|item|clash, at}`: `noon`, `5pm`, `tomorrow` (9:00 am), `HH:MM` or a local date-time.
  `today_reminders`; one open reminder per target.
- `POST api/today/items/<id>/draft`: write (again) a suggested reply to this ask (above); local model only.
- `POST api/today/items/<id>/gmail {text, reply_all?}`: the owner's Put in Gmail Drafts under that reply.

## Reminders
The ledger runner calls `Today.tick` every minute: a due reminder whose follow-up already cleared is dropped;
otherwise it goes to the owner's phones through `conflicts.Alerter.remind` (the 0.5.1 push, quiet hours and the hourly
cap; `reminders` in the push settings, on by default) with names, sources and times only, never message text:
"Marco still doesn't know you're busy Thu 7:00 pm" / "You told Dana yes (Signal) at 9:02 am. Marco (email) hasn't
heard from you."

## Gone
The old brief (Today → Old brief, 0.21.0) and the old replies list (Replies → Old replies list, with One at a time)
are gone: Talk it through and Draft a reply replace them.
