# Ledger (0.16.0, rework stage 2: read once, keep a ledger)

## The idea
Each stored message (0.15.0 `messages.db`) is read **once** by the local model, with its thread around it, and what it
asks, promises or plans goes into a ledger of items. Items are updated, never re-guessed: a later message that moves,
cancels or answers something changes the item and adds a history line. Clashes are checked on every change against the
whole calendar and the whole ledger, on any date. Nothing is dropped silently: whatever the model gave that the code
can't use is kept with the reason, and an item the code is unsure of is shown as "likely" with its source.

## Files
- `callsheet/app/ledger.py`: tables, items, merge, statuses and history, sections (Needs you, Now and next, Coming up,
  Clean up), the read side (`Ledger`).
- `callsheet/app/extract.py`: the prompt, strict schema, the checks on the answer (`normalize`, `resolve_when`), the
  `Extractor` (one message per request) and the background `Runner`.
- `callsheet/app/conflicts.py`: clashes, follow-ups, sweeps and phone alerts (`Alerter`).
- `callsheet/app/heads.py` (0.21.0): the rules that don't wait for the model: security alerts and invites to answer
  (see "Security alerts and invites" below), and the Reports copy.
- `tools/replay_ledger.py`: replays an acceptance set through the real code and scores it.

## Tables (in `messages.db`)
- `ledger_items`: `type` (ask, commitment, event, cleanup), `subtype`, `who` (the person key; `owner` for the owner) and
  `owed` (who owes whom: an ask is owed by the owner to `who`, an owner commitment by the owner to the people in the
  thread), `what`, `purpose`, `status`, the time (`start_ts`, `end_ts`, `all_day`, `start_date`, `end_date`, `time` for
  an undated time, `date_resolved`, `approx`, `tz`, the original `phrase`, `candidates` for "Tue or Wed"), `says`
  (attribution: "Dana says"), `confidence` (sure, likely, unsure), `never_hide`, `thread`, `cal_ref` (the calendar
  event it repeats), `parent` (a hotel inside a trip), `predecessor` (the ask a commitment answers), `merged_into`,
  `checks` (fields the code rejected or changed, with the reason), `created_at`, `updated_at`.
- `ledger_sources`: every message behind an item (`created`, `merged`, `updated`).
- `ledger_history`: every change (status from and to, the message, the fields, a note).
- `ledger_conflicts`: two sides (item or calendar event), the slot, `overlap` or `tight`, confidence, state
  (open, resolved with why), the alert state; 0.22.x `entries`: the owner's trip's grouped clash lists the calendar entries
  during it (JSON ids, time order; b is the first), '' for a clash between two sides.
- `ledger_followups`: "Marco hasn't heard from you": the item, whom to tell, why, when it's due, cleared by the outgoing
  message that tells them; 0.22.0 `event`: the calendar entry that still holds a plan the owner moved (FUP-04).
- `ledger_reads`: one row per message read: state (done, failed, skipped), tries, seconds, items made, drops (the
  rejected parts and why).

Statuses: open, accepted, tentative, claimed, waiting_on_them, autopay, moved, postponed, declined, done, cancelled.
"changed" and "updated" from the model go to history only (0.22.0: kept even with no field or note).

## Reading a message (`extract.py`)
1. The message, up to 8 earlier messages of its thread (500 characters each), the people, the date and weekday with
   "tomorrow" spelled out, and up to 12 open ledger items of the same people or thread (`L12 | type | who | what | when`),
   plus (0.22.0) up to 4 from other threads that this message names: a person's open item when the message names
   them ("Marco is still waiting"), and the owner's own commitments to the people in it or naming them.
2. One request, `chat_template_kwargs.enable_thinking = false`, the background slot, strict `json_schema`, at most 700
   tokens out; the prompt shrinks to the call's token cap (`time_cap`) by trimming the thread first.
   0.22.0 the read tier (`read_tier`), decided by code before the request, never a skip:
   - **full** (the prompt above): people writing to the owner, the owner's own messages, every source but Gmail; for chats
     (Signal, Slack, texts: `CHAT_SOURCES`) it adds `CHAT_RULE`: banter, reactions, thanks, emoji, captions, memes,
     settled plans, recaps, small delays, belated wishes and questions to others make no item. Always an item, with
     its time, however short (0.22.x, after round 3 lost S-MOVE-07, S-COMMIT-05 and S-BDAY-05): a new ask to the owner;
     someone asking to move a plan ("can we do 11 instead?": a new ask with the new time, the plan moves when the owner
     agrees; a plain change moves the listed plan); the owner's own promise (a commitment, besides any update); a birthday
     named in the chat (an event, subtype birthday, besides any ask); 0.22.x after the real Signal set: the owner's yes to an ask (a
     commitment, the ask done) and days away the owner announces (an all-day event, not an ask); plans among others the owner isn't
     part of and a shared link or forward make no item (never a clean-up); a group ask to everyone or with no @mention
     of the owner and no reply to the owner is at most likely; 0.22.x after the local model on the real Signal set: where someone is or
     what they did ("omw"), a maybe or wish with no promise, what the owner already did, and an ask or plan already listed
     asked again (an update) make no item. Each with a contrast example. A prompt rule
     only: the code keeps whatever the model returns, and only a live run measures it (0.22.x: except the group rule,
     which code now applies too, see 3);
   - **narrow** (0.22.x: the full prompt, narrowed in code by `extract.narrow_answer`; the separate `NARROW_SYSTEM`
     prompt is gone, it lost the "sender" rule and the trip parent): Gmail mail that is `bulk` (List-Unsubscribe or
     Precedence), from an automated sender (`people.AUTOMATED_SENDER`: no-reply, notifications, alerts ...; role
     addresses like info@ or bookings@ are not), or in a Gmail category tab (Promotions, Social, Updates, Forums).
     `extract.noise_keeps` keeps Clean up lines, never-hidden items (security, codes, invites), events `bill`,
     `delivery`, `trip`, `flight`, `lodging`, `appointment`, `birthday`, asks `bill`, `receivable`, `payment`,
     `document`, `invite`, `security`, an ask to sign (`SIGN_RE` on its text and the subject; "sign up/in" is not),
     and any ask from a person's own address (`person_address`: not bulk, automated or role); everything else
     (marketing asks, commitments, sales events) is left out **with the reason** in `ledger_reads.drops` (`item`,
     "kind: what"), with any update on it, and named on the mail's Clean up line;
   - back to **full** even when bulk or tagged, unless the address is automated: a known person or contact (`person:`
     key), a thread the owner wrote in, or a personal (not role) address that greets the owner by name at the top and asks a
     question ("Hi Jordan, could you send the PO?");
   - the tier shows on each item as a `read` check ("narrow (bulk, promotions)", "full (a known person, bulk)") and in
     `ledger_reads.note`; a narrow read that keeps nothing (no open item of this sender or thread shown, no security
     alert or invite) lists the mail in Clean up with "nothing to keep from this mail", so nothing disappears; when
     items were left out the Clean up line's `items` check names their kinds ("ask (reply), commitment, event").
3. The answer is checked by code, never trusted:
   - unknown types, unknown ids and more than 8 items are dropped **with the reason** in `ledger_reads.drops`;
   - bad fields are kept as `checks` and lower the item to "likely"; an item without `what` takes the subject;
   - times are never dropped: the phrase is kept; a missing date is filled by the date rules from the phrase; a
     weekday that doesn't match the date follows the phrase; "tomorrow" written before 04:00 by a person means later today
     (0.22.x: not from an automated sender, a shop, carrier, bank or notification: a narrow read, or an automated or
     role Gmail address, `extract.machine_sender`; its "tomorrow" is the calendar's); a time in another zone ("1pm ET") is converted with DST; a time with no date stays undated; an overnight range ends
     the next day; a moved item keeps its length;
   - 0.22.0: a day part or EOD with a date and no time gets its hour (morning 9, afternoon 14, EOD 17), approx, even
     when the model didn't say approx; "the 3rd" written after the 3rd is next month; "this weekend" and "next week"
     with no date become those days; a length in the phrase or message ("2 hrs", not "in 2 hours") gives a missing
     end; a zone named after the clock time in the phrase ("1pm ET", "3pm UTC-5") applies when the model kept that
     clock time; the owner's own commitment in their own message is `accepted`; each change is listed in `checks`;
   - security alerts and invites are always `never_hide`;
   - 0.22.0: a bill or invoice that an incoming message calls past due ("past due", "overdue", "was due", "15 days
     late") is an `ask`, `never_hide`, whatever the model made of it (an event that has passed, a reminder to clean
     up, the owner's own to-do): subtype `bill` when the owner owes it, `receivable` when it's owed to the owner; a `past_due` check
     says so, and two readings of one bill in one answer are one item. "was due on the 3rd" stays this month;
   - 0.22.x after a local model on a set of real chats (most of its items from group messages not to the owner were
     "sure", nearly all false): an incoming group-chat message (Signal or texts with the `group` label, `extract.group_chat`) not
     addressed to the owner (`extract.addressed`: no @mention, which Signal capture labels `mentions_me`; no quote of the owner's
     message, `quotes_me`; not the owner's name or first name as a word; not within 30 minutes after the owner's own message, last in
     the chat) makes no new items but never-hidden ones and events (`group_keeps`; a call time or a gig posted to the group). The
     rest is left out **with the reason** in `ledger_reads.drops` and named on one Clean up line per group chat
     ("<chat>: not addressed to you", who `chat:<thread>`, so the next message's line merges into the open one); its
     updates still apply. DMs and Slack are not touched. The owner's own chat message makes no Clean up item
     (`OWN_CLEANUP_WHY`), and the owner's own chat message's open ask is the owner's question (`waiting_on_them`) whoever the model
     names. 0.22.x after round 6 (the owner's own messages made the most false items: status lines, things already done):
     from the owner's own chat message only the owner's question (`waiting_on_them`) and the owner's open commitments and events are kept
     (`extract.own_keeps`); an event at the time of the owner's commitment in the same answer is the same plan; the rest is
     left out with the reason and named on the chat's Clean up line ("<chat>: left out of your own messages", `OWN_WHY`);
     the owner's updates (done, declined, moved) always apply. A first-person cue filter ("I'll", "I can", a time the owner set) was
     measured and not taken: it cost recall for no fewer false items.
     Each item carries `quote`, the new message's own words it comes from (schema and prompt); an item whose quote
     isn't in the message after case, space and punctuation are evened out (`extract.quote_found`, "..." parts each
     found) is left out with `QUOTE_WHY` (cleanups and never-hidden items aren't checked; an answer without the field
     isn't either). `MAX_TOKENS` 1600 (was 700: the local model pretty-prints its JSON, and 9 of 261 synthetic reads were cut,
     most of them the owner's short yes, with the update in them); a cut answer keeps its whole items and the updates with a
     whole id and status (`extract.salvage`; not a cut move), and the read lists `CUT_WHY`. Two items of one answer with the same type, person, subtype and time and nearly the same words
     (`ledger.similar` >= 0.8, `extract.same_twins`) are one: the twin is left out with its reason, its updates move to
     the first;
   - 0.20.0 facts for the visual blocks (`docs/visuals.md`): `amount_from` is the sentence the model cites for an
     amount, word for word. Code finds that sentence in the message (subject first; quoted mail and a mail signature
     left out) and copies the amount by the 0.12.2 rules (`readrules.pick_amount`: one amount in the sentence, no range,
     no line item, subtotal or saving, and the total when the message states one), into `amount` as written, with
     `currency` (an ISO code, or `$` for a bare dollar sign). The model's own words never become the amount; anything
     else is a check ("more than one amount", "the cited sentence isn't in the message" ...). `stage` is a package's
     progress (`ordered`, `shipped`, `out_for_delivery`, `delivered`: only forward, and only when the message has the
     words for it) or `due` for a to-do with a deadline (only with a date and a due word; the date is the item's
     `when`). Updates carry both, so a "shipped" mail moves the order on. Older stores get the columns on start.
4. In one write transaction: new items (merged with the same item from another source or the same thread: same start
   within 15 minutes, or overlapping and similar words; never all-day with timed), updates by id, clashes, follow-ups,
   the read row. Then the phone alerts. 0.22.0: in a message that asks ("?", "are you free", "can you"), an event of
   the same person at the asked time is the ask's plan: it joins the ask (kept as merged, with a history line; its
   end fills the ask's), so it neither shows twice nor clashes with the ask's rivals; a plan with a task ("Prepro Tue
   2pm, bring a lens list") stays two. A timed item that repeats a calendar entry and gives no end takes the entry's.
   0.22.x: two deliveries are one package only when they name the same order or tracking number, or (when the mails
   give none to compare) the same item, in its own words rather than the shop's or the delivery words, on dates that
   meet; never by the thread or the shop alone, so two orders in one shop thread stay two (`ledger.same_delivery`). An
   appointment links to the calendar entry that holds its start (from 15 minutes before to the entry's end, so a
   12:30-1:30 block holds a 1:00 visit) and shares a word with it: the title against the item's words, the provider
   (`who_name`) or the mail's subject; weekday, month and time words don't count (`ledger.cal_match`).

A message that fails is retried up to 3 times, then listed as failed. New messages first (2 hours), then the backfill
(30 days, meta `ledger_backfill_days`).

## The runner
A background thread every few seconds. It doesn't call the model when: the ledger is off (meta `ledger_enabled` = 0),
there is no local model server, no background model (none picked and the server lists none), the server is backing
off, one of the owner's quiet times for the model server (settings `model_quiet_times`, Setup › AI › Advanced; none
on a new install) is on or starts within a minute, the call guard says no (day caps, Careful), another background call holds the lock, or Chat is
waiting (Chat preempts). Server faults stop the pass and back off; a full context waits and retries.

## Clashes (`conflicts.py`)
- Any two timed items, or a timed item and a timed calendar event, on any date: `overlap` (sure when both are firm) or
  `tight` (likely): under an hour between calls or shoots, under 2.5 hours next to travel.
- Whole days never make a hard time clash (holidays, birthdays, offsites, other people's trips, plain all-day blocks, a
  "next week" commitment). A whole day makes a soft `away` clash (likely, "you may be away or off that day") with a
  timed ledger item only when it takes the owner's day: a busy all-day calendar entry for time off or out of office
  (Google's `outOfOffice` type, or OOO / time off / PTO / vacation / leave in the title, unless marked free:
  `transparency` "transparent"), a calendar hold ("HOLD: ..."), a calendar trip of more than one day, the owner's trip
  of more than one day (the owner's own words, or a booking that came by mail), or the owner's own all-day event ("at the lab
  all day"). Never against the calendar's own timed entries, except (0.22.x) the owner's trip or stay of more than a
  day against a busy timed entry during it, whatever its attendees (a meeting, a dentist visit, a haircut; owner's
  rule 2026-11-13); not a free (transparent) entry, an all-day one, one the owner declined, or the trip's own travel entry.
  It is ONE clash per trip that lists each such entry by id (`ledger_conflicts.entries`, time order; the test
  lead's ruling), likely unless every item in it is sure (ruling M4: the trip, not tentative, and each entry; a
  tentative entry, the owner's maybe or a likely item standing for an entry makes it likely). Ruling M3: the owner's time-off or
  "out" block of more than a day groups the same way (`conflicts.out_block`: a gig, job or travel of the owner's by its
  subtype, or time off / out of office by its words; the owner's own word or a booking by mail), with the open invites during
  it as entries; a friend's block, a one-day block or an ask doesn't. An item that stands for its entry (an invite, a mail that repeats it) counts as the entry; an entry
  that clears (declined in Calendar or on its invite, cancelled, moved out, made free) leaves the list without a clash
  of its own, and the clash resolves ("entries cleared") when none is left. Today shows one row ("Your trip to
  Tromsø overlaps 3 entries") with the entries, their 12-hour times, when it is open; clashes per entry from before
  the ruling fold into it ("grouped"). The Agenda (the trip and a box per entry with its day and time, "Open"), the
  reply draft's clash facts and Chat's clash list (`ledger_list` conflicts: every entry with its time, `entries` the
  count) show every entry, not only the first (`today._clash_sides`). An answer the owner presses in Callsheet
  (`rsvp.py`) re-checks the trips at once (`conflicts.regroup_event`): a No takes the entry off the list, a Yes or
  Maybe keeps or puts it on, without waiting for the sweep.
- 0.22.x: an ask for the owner's presence or time ("can someone be there Thursday 2:40?", "are you free", "be around") makes a soft
  `away` clash (likely, "your calendar holds that day") with an all-day calendar entry that may take the day: a
  tentative one (its status, or the owner's tentative RSVP) or one of more than a day.
- A calendar entry marked free (`transparency` "transparent") never clashes, timed or all-day.
- A whole-day item that repeats a calendar entry (`cal_ref`) takes the entry's exact days.
- Not clashes: an item and itself, its trip, its predecessor, the calendar event it repeats, two items of one person in
  one thread, two people in one thread naming the same time, two items made by one message, and an item whose message
  names the other side's person; 0.22.0: the owner's commitment and another person's item in the same thread at the same
  time, and the owner's yes to an ask and what came with the ask (its flight). 0.22.x after a round on a real mail set
  (the extra clashes were security alerts against appointments): a security alert holds no time
  (`NO_BLOCK`); an item from the owner's own chat message clashes only when it is the owner's commitment or a whole day of the owner's
  (`conflicts.chat_clashes`, `OWN_CHAT_KINDS`); the owner's event or question in a chat stands for the other side's plan.
  Measured and not taken (`SAME_CHAT`, `CHAT_LIKELY`): no clash between two items of one chat, or for a likely chat
  item; both broke rulings R8/M4 and lost likely clashes the synthetic set expects.
- When the owner says yes to one side, the clash carries to the owner's commitment; when a side moves away, is declined or
  cancelled, the clash is resolved with why.

## Follow-ups
Raised when the owner answers one side of a clash (the other person hasn't heard) or when the model reports a promise to
tell someone (`tell`, `due`), and (0.22.0) when the owner's own outgoing message promises to answer ("let me check and get back
to you tomorrow"): a follow-up on the commitment it made, due when it is. Cleared by an outgoing message to that person, on any source, that answers, updates or
closes the item (a message to them about something else leaves it open); a promise is kept by the owner's answer to its
ask or a change to that conversation's items; never by the message that raised it.

0.22.0, three more raised by code (`conflicts.after_message`, on the owner's outgoing messages), each until the owner writes to
that person about the item:
- **A no that someone else is waiting on.** The owner declines one person ("Can't Monday, sorry" to Ines: her ask declined,
  or still open in that conversation) and another person's open ask is about the same plan at the same time (it
  names the declined person, "Ines mentioned you might do pickups Monday", or has the same words without being its
  rival in a clash): a follow-up on that ask ("You said no to Ines for Mon Mar 8; Tess still waits to hear about it.").
- **A calendar entry that still holds a moved plan.** The owner agrees to move a plan ("Thursday 3pm works. I'll update
  everyone"; the conversation asks to move it) and a calendar entry in the next 30 days, with the person the owner wrote to
  and the same words, is still at the old time: each attendee the owner didn't write to, and who wasn't in that
  conversation or on its messages, gets a follow-up on the moved item ("The calendar still has Review w/ Lena + Kofi
  on Wed 3 pm; Kofi hasn't heard about the change."), due 3 hours before the entry (or the new time, if sooner),
  with the entry kept (`ledger_followups.event`). Today sets a reminder at that time. The sweep clears it when the
  entry moves to the new time, is cancelled or gone, or the item closes.
- **A yes in one conversation that another asked about.** The owner confirms a job with a date to one person ("Confirming
  Mar 24-25" to Dana) and someone in another conversation has an open ask about those days (its own time or the dates
  its message names) that is about the owner's booking ("booked", "hold", "free", "available") or shares the job's words:
  a follow-up on that ask ("You confirmed Brightwater doc shoot (Wed Mar 24 – Thu Mar 25) with Dana; Theo asked and
  hasn't heard.").

## Late (0.22.0)
An item whose deadline has passed is never dropped: a bill, payment, invoice, deadline, document, reply or decision,
a due stage, a past-due bill, or a commitment with a due word in its phrase ("by Friday", "EOD"), still open, shows in
Needs you as late (`ledger.late_of`: days by local date; "15 days past due" with no date counts from its message;
`late` in the API). A past plan (a party, a gig, a flight) is not late. Autopay and postponed items never are.
Money owed to the owner that is late (a `receivable` ask) gets a follow-up with nobody to tell ("15 days late: chase it");
the owner's own message naming its invoice number ("a nudge on invoice #1043", any thread) makes it `waiting_on_them`, the
follow-up staying. A message that says it's paid ("Payment received for invoice #1043"; the same invoice number, or
without one the same amount or conversation; never "not been paid") marks an open bill or invoice done, which clears
the chase. The runner's sweep chases invoices that became late since they were read.

## Trips (0.22.0, acceptance Rule C)
A trip is one item. Its flights, cars and trains (`ledger.TRIP_LEGS`, or an event without a subtype whose words say
so: "flight", "→", "rental car") and a hotel stay given as whole days are merged into the open trip whose days they
fall on (`merged_into` and `parent`), in either order: legs in the same message, legs read before the trip, legs read
after it. Each leg is kept in the trip's history ("flight YHZ→TOS, Thu Mar 18, 7:05 am") and never shown as a second
item. A hotel check-in with its time stays its own item under the trip (`parent`). A trip the model gave a time (its
first flight's) becomes the whole days from its start to its last leg in that message. A flight on other days stays
its own item.

## Security alerts and invites (0.21.0, `heads.py`)
- **Security alerts.** `rules.security_alert` (the brief's rule, no auth headers in `messages.db`) on every stored
  Gmail message and text: one item per message, `subtype` `security`, `never_hide`, confidence sure. When the model
  reads the message, its first new item becomes the security item (its words kept) and the rest join it; when
  `Heads.scan` (a Runner hook, every pass, model or not) saw it first, the model's items join the rule's item. Its
  section is `security` (Today's Now and next), never Clean up; a snooze never applies; the model never sees it in its
  open items and a status update from the model is dropped (listed: "a security alert closes only by your Done"). An
  alert more than 3 days old when first seen is kept as done.
- **Invites.** Calendar events with your answer `needsAction` (the next 60 days) become one `ask` per series
  (`subtype` `invite`, `thread` `invite:cal:<iCalUID>`, `cal_ref` the event); answered, cancelled or past ones close.
  Invites in mail (`heads.invite_of`: Google's subject or a `text/calendar`/`.ics` attachment) become the same kind of
  ask unless the calendar has it; "Updated invitation" changes an open one (alone it makes nothing: the model reads
  the change); "Canceled event" cancels it. The model's own new items from an invite mail join the invite. Invites go
  through `conflicts.check_item`; an open invite stands in for its calendar entry (no second clash).
- **Reports.** `Heads.copy_reports` copies the Reports connectors' stored reports (`connectors.report_items`) into
  `messages.db` as source `reports` (sender `report:<connector>`), so the extractor reads them; a report that reads
  like instructions, or from a removed source, is never copied. Chat's message search leaves them out (Chat reads
  reports from the index).

## Phone alerts
Through 0.5.1 push (`Notifier._send`): new clashes ("Clash in 5 h: Wed Mar 3, 10 am" / "Dana (Signal) and Marco (email)
both want this time.") and asks from VIPs. Names, sources and times only, never message text. Quiet hours hold an alert
until they end, except when a VIP is involved and `vip_through_quiet` is on (default). The hourly rate limit applies.
Settings: `clashes`, `vip_asks`, `vip_through_quiet` (all on by default). 0.21.0: a new security alert (one that
arrived within the hour) with the push setting `urgent` on: "Security alert" / "From Google (email). Open Callsheet to
see it." Never the subject (it can hold a code); quiet hours and the rate limit apply.

## API (read by Today since 0.18.0: `docs/today.md`)
- `GET api/ledger`: `{now_next, needs_you, coming_up, clean_up, waiting, conflicts, follow_ups, reads, counts}`.
- `GET api/ledger/items/<id>`: the item with its sources, history, clashes and follow-ups.
- `GET api/ledger/rejected`: every read with drops (message, time, what was dropped and why); items keep their
  own `checks` (the "no silent drops" list).

## Replay (`tools/replay_ledger.py`)
Runs an acceptance set through the real `Extractor`, ledger and clash code on a temporary database with a simulated
clock. Extractors: `oracle` (answers from the expectations, should score 100%), `bad` (a careless model), `live` (the
owner's local model server; key from `LOCAL_MODEL_KEY`). It scores per the acceptance README (sure and likely apart),
prints a JSON summary (`--out`), and can write the failures (`--detail`, mode 0600, refused inside the repo). Live runs
honour the model server's quiet times (`--quiet-times "01:00-04:00,..."`, none by default) by the real clock in `--tz` (default the TZ environment variable, else UTC; `--on-block wait|stop`) and stop
on a server fault.

Scoring notes (where the scorer reads the set, not the product): expectations may name another case's expectation
(updates, clashes, follow-ups), so matches are looked up across the replay group; `same_item` expectations share one
item; "confirmed" and "scheduled" count as `accepted`; an initial status and a final status reached in the same message
both count; an answered ask the set places by where it ends (`final_status` set) may be checked at the end; a clash with
an item that repeats a calendar entry counts as the clash with that entry. Scorer 2 (`SCORER_VERSION`, written into
every summary with the cases' `set_version`; every model is scored with the same version): an automated sender
(`p:auto`) matches only the sender of one of the case's automated messages or the case's declared `auto_sender`, never
the owner or a person of the case; "Kofi Mensah" is `p:kofi` only when Kofi is the only Kofi among the case's people;
an expectation may give allowed `types` (and a `section` list) for ambiguous wording, and counts as an ask when `ask` is
one of them. Scorer 2.1 (`SCORER_VERSION = "2.1"`, for set v1.1): the `section` list is paired with `types` by
position (an item matching as an ask must be in the ask's section, one matching as an event in the event's); an ask's
`done` update with `into` passes only when the ask's own item is done or merged into that expectation's item, and an
ask still open after the owner's agreement also counts as a false item (`open_after_agreement`); the run stops with a
set error (exit 2) on an `auto_sender` that isn't the sender of one of the case's automated messages (or is the owner
or a person of the case), on a `section` list whose length differs from `types`, and on a misused `into`. 0.22.0: "changed" and "updated" are one update kind; a security alert matches a `never_hide` expectation whatever its type, and its `security`
section counts for the expected one (never Clean up, as the README says); an item that repeats a calendar entry the
case links as a dedupe is not a false item (Today shows one row). Not scored yet: the 193 `facts_only` cases
(no message text was stored before stage 1, so there is nothing to replay; they need the device's 30-day backfill) and
the 6 `calendar` cases (calendar-only expectations for the stage-4 Coming-up view and RSVP-pending invites, which the
ledger doesn't turn into items yet). Scorer 2.2 (set v1.3, real-mail v1.0) keeps 2.1's rules and feeds the extractor
what live mail feeds it: the sender's display name (`sender_name`), the account, cc, Gmail's labelIds and category,
`bulk` (or the List-Unsubscribe / Precedence headers), the sets' `automated_sender` / `role_sender` flags
(`read_tier(automated=, role=)`), an optional `contacts.json` next to the cases (or `--contacts`) whose handles take
the known-person read tier, and on calendar entries the owner's RSVP (`rsvp` / `responseStatus`, the owner's `self` attendee
entry), `transparency` and `event_type`; 0.22.x: a chat message's `mentions_owner` / `quotes_owner` become the
`mentions_me` / `quotes_me` labels live Signal capture sets (the group-chat rule; not in the prompt). Scorer 2.4 adds two-annotator scoring (`--second-labels B.jsonl`: one line per
case, the cases' schema; works with `--extractor raw`): the two labellings' items pair by type (or overlapping types),
who and `after`; REQUIRED items (both give them) count for recall, OPTIONAL ones (one gives them) are neither a miss
nor a false item (the ones produced are counted for information), FALSE = a model item outside the union; clashes the
same way; confidence, section and time are scored on required items only. The summary's `two_annotator` block gives
the required count (`weak_test` under 30), required recall per kind, union precision, optional produced and the
per-field agreement between the two labellings; 2.3's scores and bars are unchanged.

```sh
LOCAL_MODEL_KEY=... python3 tools/replay_ledger.py --extractor live --base http://HOST:PORT/v1 --model MODEL \
  --cases path/to/acceptance/synthetic/cases.jsonl --out syn.json --detail /tmp/syn_fail.json
```
The real set's `calendar.json` next to `cases.jsonl` is picked up automatically. 0.22.0: `--raw-out FILE` (live; mode
0600, refused inside the repo) keeps each answer of the model; `--extractor raw --raw FILE` re-runs the reading code
on them offline (close, not exact: the answers' `L12` ids are the live run's). 0.22.x: exact with `--ids-out FILE`
on a raw replay with the code the answers were read with (each group's ledger ids by created message, type and words)
and `--raw-ids FILE` on the re-score, which maps each `L<n>` to this replay's item (or `L0` when it was left out).
Scorer 2.4 also lists each extra clash's and false item's sides by kind (type, status, confidence, own, chat), no words.

## Retired rules
The 0.12 reader's held facts and its silent drops, and the 0.13 picker's steer filter, never applied to the ledger;
the reader, the picker and the sorter are gone (approved 2026-11-12). Their date and amount rules live on in
`readrules.py` (`docs/readrules.md`).
