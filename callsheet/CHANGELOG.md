# Changelog

## 0.5.1

- **Phone notifications through Home Assistant.** Setup → Delivery → Phone notifications lists the phones
  Home Assistant knows (its `notify.mobile_app_*` services, with names like "iPhone" or "Pixel 9a"); pick one
  or more. "At call times" sends a short note after each scheduled brief: how many replies and events, and
  the top item's sender and subject (or event title). "Urgent alerts between briefs" looks every 10 minutes
  between call times, without AI, at new inbox mail (headers and Gmail's short preview only) and at Slack DMs
  from your VIPs. It alerts for a VIP, for mail that says urgent, emergency or ASAP, or today together with a
  deadline, and for a sender saying something has been approved, paid or signed. Each message alerts once, at
  most 3 alerts an hour (you can change it; more are grouped into one "N more urgent messages" alert), never
  in quiet hours (default 22:00 to 07:00) and not on days off when check-ins skip them. A notification shows
  only who wrote and the subject, never the message text ("Show the subject in notifications" off: the
  sender's name only, as notification text passes through Apple's or Google's push service); tapping it opens Callsheet in the Home Assistant
  app. "Send a test notification" checks the setup, and problems (a phone Home Assistant no longer knows, no
  access) are shown in plain words. The app now asks Home Assistant for API access (`homeassistant_api`):
  Callsheet uses it only to list the phones and to send to them.
- **Signal.** Link Callsheet to your Signal account as a linked device (Setup → Areas → Signal: press Link
  Signal and scan the QR code with Signal → Settings → Linked devices on your phone). Signal messages become
  reply rows like Slack's: direct chats where the other person spoke last, and groups where you were mentioned or
  quoted, with VIPs, Direct question, deadlines and the wait time; busy groups get a Heads up line. Choose the
  area for Signal rows, ignore chats, and unlink (Callsheet deletes its Signal keys and received messages; remove
  the device on the phone too). The status shows your masked number and the last message, with reminders to keep
  the app running (Signal drops a linked device after 45 days offline) and to update at least every 2 months.
  Callsheet never sends Signal messages, reactions, read receipts or typing notes.
- **Signal history import.** Upload Signal Desktop's chat export (`main.jsonl`, up to 200 MB, in pieces) or a
  chat's `data.json` from signal-export, then pick the chats to keep. Only text, names and times are kept;
  keys and phone numbers in the file are skipped. Imported messages are marked as imported, never become reply
  rows, and stay until you delete them.
- Received Signal messages are kept for 30 days. The Signal keys (`/data/signal-cli`) are left out of Home
  Assistant backups.
- The image now includes signal-cli 0.14.9 with libsignal and a headless Java 25 runtime (from Alpine's community
  repository, amd64 and aarch64), so it is larger.

## 0.5.0

- **Writer evaluation with contenders.** The evaluation no longer compares fixed Gemma and Qwen setups: you
  define two to four contenders, each with a name, a model (from your local server's model list, or Claude
  when a Claude key is saved), thinking on or off, effort, a thinking budget, optional sampling (temperature,
  top p, top k; local models only) and a writing profile, saved for the next run. Compare each against each,
  or every contender against a baseline; the same model with different profiles works too. Blind pairs,
  names after submitting, the results per contender (OK or failed, answer format, time, tokens, empty
  suggestions, flagged drafts, picked better/worse/same) and Download work as before, as do the call-time
  rules, the "Loading model" retry and loading your everyday model again afterwards. Your old evaluation
  settings become three contenders (G0, Q-low, Q-med against Q-med); old runs still open and download.
- **Notes to Callsheet.** The notepad menu on a reply, event, briefing paragraph, heads-up or possible event
  now has "Note for Callsheet…": write a note (for example "Jordan's volunteer requests can wait until
  Sunday"), and the item shows that it has one. Notes are kept in one Markdown file on the box
  (`/data/memory.md`, readable only by the app). Setup → Delivery → Memory shows the whole file, and you can
  edit it there or clear it after a confirmation. Before each brief the writer gets only the notes that fit
  the day: the same sender, email domain, person, topic words or area. It gets about 1,500 characters at most,
  in their own block after the facts, with a fixed sentence saying they are your notes and that they never
  change the safety rules. Mail that pretends to be your notes stays plain mail. Both writers and the writer
  evaluation get them. Prompts without notes are unchanged.
- **Possible events.** When the latest message in a reply proposes a date ("Can we meet Tuesday at 2pm?",
  "lunch on 3 Nov", "between 3 and 4pm on Wednesday", "due Friday at 5pm"), the Agenda lists it under
  "Possible events". Each entry shows what, when (in your 12h/24h format) and who it's from, unless your
  calendar already has it. "Add to calendar" opens a filled-in Google Calendar page in the right account and
  time zone in a new tab, and nothing is saved until you save it there. Callsheet still only reads your
  calendar. Questions that ask you for a time, past events and unclear dates such as "next Friday" are left out.

## 0.4.1

- **Facts for the writer.** Callsheet now works out the numbers the writer used to guess: per reply row the
  days it has been waiting (since the first message after your last reply, in your time zone), how many
  messages came after your last reply and how many of them were follow-ups (mail, using your sent mail too,
  and Slack), and a deadline when the last message names a date, weekday or time; plus the clashes in time
  order, the oldest wait, what's due today and counts (open replies, VIPs, overdue: waiting longer than the
  reply age in settings, none when that is 0). They go to the writer in their own block after the data, and
  the writer is told to use them exactly. Mail that pretends to contain such facts stays plain mail.
  Both writers and the writer evaluation get them. The waiting days count to the brief's own date (the sync's
  clock in your time zone), so they can be more than the row's age when you never answered an earlier message.
  Each row also gets its wait as ready-made words, which the writer is told to use as written: "waiting 5
  days", "waiting 1 day", "waiting since today", and when their latest message is more recent than the first
  unanswered one e.g. "waiting 11 days, last nudge 8 days ago" or "waiting 3 days, last nudge today".
- **Sender's claims stay claims.** A new fixed rule right after the reply guidance: suggested replies never
  state as done anything an email says was approved, released, paid, signed, booked or agreed; they treat it as
  the sender's claim or leave a [placeholder]. Setup shows it with the other fixed text.
- **Wider draft check.** The "Check this draft" warning now also catches drafts that take a sender's claim as
  done ("I note the contract has been signed", "confirm the signed contract"), while requests for a signed or
  paid document, questions and conditions still don't count. Mail signature lines ("Sent from my phone") are
  ignored. Thanks for news about it counts too ("Thank you for the update on the signed contract"); a reminder
  or question about a signed copy, or a "paid plan", does not.
- **Wait on each reply.** Reply rows show how long they have waited, worked out by Callsheet's rules (with or
  without the writer), in place of the short age: compact in the list ("waiting 11d · nudge 8d"), in full in the
  detail ("waiting 11 days · last nudge 8 days ago"). The "Waiting" reason no longer repeats a day count.
- **"This message says something was done."** When the latest message from the other person says something was
  approved, paid, signed, released and so on (not a question or an "once it's paid"), the row's detail shows "This
  message says something was approved, paid, signed or released. Check before replying." and the row in the list
  gets a small "!". Callsheet's own rules decide this, with or without the AI writer, and even when the suggested
  reply is fine. Mail: the subject (unless it is a "Re:" or "Fwd:") and the preview, plus the message text when
  the writer reads it; Slack: the last message. Your own messages never count. Only the yes/no is kept, and it is
  not sent to the writer.
- **Default writing profile.** The Default keeps the 0.4.0 texts, with one new lead sentence: the day summary
  leads with the oldest wait (with its day count), then the clashes in time order, then what's due when. A
  more tuned wording was measured too and left out because its replies repeated sender claims more often. Your
  own profiles are unchanged.

## 0.4.0

Server and API for this release; the page that uses them follows.

- **Slack (read-only).** Connect a Slack workspace per area with a user token (`xoxp-`) from a Slack app
  you create from Callsheet's manifest (read scopes only, no write scopes). Callsheet calls only Slack's
  read methods (auth.test, team.info, users.info, users.list, conversations.list/history/replies/info),
  waits when Slack asks it to slow down, and caps the requests per read. **Needs a reply** gains Slack rows:
  DMs and group DMs where the other person spoke last, @mentions you haven't answered, and new replies in
  threads you're in, with the same reasons as mail and VIPs matched by email (else by name). @channel,
  @here and @everyone go to Heads up. Channels can be ignored. Areas get a **chat** read switch.
- **Smarter writer.** A summary per area and up to 5 **Top of the day** items; the day summary is 3–6
  sentences. The rules pick Top of the day (VIPs, clashes, deadlines, then the next replies) and the writer
  only words it; without the writer the rules' own lines show. Slack rows go to the writer with their last 3
  messages. The "never say it was approved or paid" rule follows the reply instructions directly again (as in
  0.3.5), and more drafts that tell someone to release or pay get the warning.
- **Writing profiles.** The writer's three instruction texts (day summary, area summaries, suggested
  replies) live in named profiles: a read-only Default plus up to 10 of your own (copy, rename, edit,
  delete). The safety rules are fixed and always come last, whatever a profile says.
- **Doesn't need a reply.** Gmail's Promotions, Updates, Forums and Social tabs no longer give reply rows
  (VIPs still do). Hide a thread, a sender or a mail domain from Needs a reply; the brief says how many
  rows are hidden, and rules can be removed again.
- **New since.** Rows that weren't in the day's earlier brief are marked new, and the brief says when the
  previous one was written.
- **Time format.** Times show as 12-hour ("9:30 am", "12 pm") by default, or 24-hour ("14:30") in
  settings. The writer uses the same format.
- **Email and message summaries.** With the writer on, each reply row it reads (mail and Slack) gets a
  one-line summary of what they want plus up to 6 exact details (dates, times, amounts, addresses,
  reference numbers, deadlines). A detail is only shown when it appears word for word in the messages the
  writer read, so invented facts are dropped. Without the writer the row shows its subject.
- **Two more writing sections.** Profiles gain **Global rules** (applied to everything the writer writes)
  and **Email summaries**. Your saved profiles get the Default texts for both. The Default texts for the day
  summary and suggested replies use the wording that did best in the writer evaluation, and two fixed style
  examples (one reply, one summary) are always added before the safety rules.
- **Done.** Mark a reply row done: it leaves Needs a reply, Top of the day and the counts and shows under
  **Done today**; **Undo** brings it back. A new message in that thread or chat brings it back by itself.
  Done marks are forgotten after 30 days.

## 0.3.6

- Bold look on phones: the torn-paper edge of a long section (e.g. Needs a reply) no longer cuts
  through its heading and first row; the edge now has a fixed depth.

## 0.3.5

- Local writer: more specific replies and day summaries, short placeholders like [day], each reply answers
  only its own thread (tested on synthetic data: placeholders where needed 61/65 vs 44/65, invented values
  2 vs 8; the safety rule still held). The Claude writer is unchanged.
- Local model: the safety sentence is repeated after the answer schema, matching the tested prompt shape.
- Local model: optional server slot (llama.cpp id_slot) in Advanced; off by default.

## 0.3.4

- "Open in Gmail" links open the message directly in the right account, in Gmail's own link format
  (`/mail/u/?authuser=<email>#all/thread-f:<id>|msg-f:<id>`), so they work when several Google accounts
  are signed in to one browser (they used to show Gmail's "account temporarily unavailable" page).
  This is the 0.3.1 work: 0.3.1 was never released on its own and ships here for the first time.
- After an update the page no longer keeps the old version in Home Assistant's frame: the page asks for
  `app.css` and `app.js` with the app version in the address, and the server sends a content hash (ETag)
  so the browser checks each file instead of reusing an old copy.
- If the page is still older than the running app, a line at the top says "Callsheet was updated. Reload
  to see the new version." with a **Reload** button. Setup shows the version at the bottom.

## 0.3.3

- **Writer evaluation** (for the owner, off by default): compare local model setups on today's real brief
  data, blind. Turn on **Show the evaluation tools** under Setup, Delivery, Local model, **Advanced**, then
  open **Writer evaluation**.
- It only runs when you press **Run evaluation**: never on a schedule, and not within 10 minutes of a
  brief or check-in, while a refresh runs, or while another evaluation runs.
- While an evaluation runs, **Refresh** is refused until it has finished.
- It reads your mail and calendar the same read-only way a refresh does, and never changes the live brief,
  the last refresh or the last writer run.
- It sends the real brief data only to the local model server you configured: Gemma with thinking off (as
  the brief uses it), then Qwen with a little and with more thinking, then one tiny request so Gemma is
  loaded again. Model names can be changed under the evaluation's **Advanced**.
- You compare two pairs of answers as A and B without names, pick the better one, and only then see which
  setup wrote which and the numbers (valid JSON, tokens, time, empty or flagged drafts, placeholders).
- Results stay in the app's storage (the last 5 runs, readable only by the app) until you download them
  with **Download results** (one JSON line per setup, plus your choices).

## 0.3.2

- 0.3.1 (direct "Open in Gmail" links) is a separate branch that is not merged yet; this release is built
  on 0.3.0 and does not include it.
- Local model: thinking is off by default (it used all the room on big briefs, so the answer came back
  empty). A **Thinking** switch turns it on with a 1,536-token budget; off is recommended.
- Local model: the answer must match a JSON schema made for each run, with one suggestion per thread
  sent (no duplicates, none missing), and has a fixed 4,096 tokens of room. **Advanced** sets max tokens,
  the server's context size and the timeout. A brief too big for the context is sent with fewer or
  shorter messages instead.
- Claude writer: uses the same schema with replies keyed by row id, so both writers share one reader.
  Everything else about the Claude call is unchanged.
- Every flagged reply now gets a suggestion; the writer can no longer leave one blank.
- Mail sent to the writer leaves out quoted history, signatures, "Sent from my …" footers and long legal
  disclaimers: about half the prompt and half the wait on a local model.
- The writer is told never to state approvals, payments or decisions, and each draft is checked: one that
  says something was approved, paid, sent, signed, booked or agreed gets "Check this draft: it states an
  approval, payment or agreement you may not have made." above it.
- Local model: one retry 10 seconds later while the server is still loading the model; an answer put in
  the thinking channel by mistake is used; the log shows tokens, cache use and time per run (no content).

## 0.3.0

- Optional AI writer: a short summary at the top of the brief and a suggested reply per "Needs a reply"
  row, with Copy. The rules still decide what is in the brief; nothing is ever sent.
- Writers: Claude (Opus 5.5, Sonnet 5.5 or Haiku 5.5, your own Anthropic API key, billed separately from
  Claude subscriptions) or a local OpenAI-compatible server (llama.cpp, Ollama, LM Studio) with Test
  connection. Optional fallback from the local model to Claude (off by default).
- Setup step 5 has the Writer block (with the last run and its token counts); area cards get an
  "AI writer" chip to keep an area out of the writer's input.
- A writer failure never breaks the brief: Heads up says why it didn't run.
- App options `writer_local_url` and `writer_local_model` can turn on the local writer until it is
  changed in setup.
- Upgrades keep the 0.2.0 database as it is (only new settings are added); if the database can't be
  opened, the app stops with a log line instead of starting empty.
- Image adds the `anthropic` Python package (2.3.0, pinned).

## 0.2.0

- Connect your own Google accounts, read-only, with your own Google Cloud OAuth client (Desktop app,
  sign-in with PKCE and a pasted-back address). Scopes: Gmail read-only, Calendar read-only, email.
- Live Daily Brief from plain rules: today's schedule (unanswered invitations and overlaps flagged,
  all-day events in Heads up) and mail that needs a reply (question, deadline, VIP, waiting; only mail
  addressed to you, and no newsletters, mailing lists or no-reply senders unless from a VIP).
  Projects stay sample data.
- Areas, VIPs and settings (time zone, workdays, reply age, call times, look) stored on the machine in
  `callsheet.db` (mode 0600). Secrets are never shown or logged.
- Reads accounts at each call time, at start-up when the last read is older than 30 minutes, and on request.
  One account failing doesn't stop the others; problems show in Heads up.
- JSON API under `api/` for the page (state, Google client, areas, accounts, VIPs, settings, sync).
- Image adds `tzdata`.

## 0.1.0

- First version: Home Assistant app skeleton with an Ingress page.
- Daily Brief page from the approved design (v3), with sample data: schedule, needs a reply, heads up,
  projects by area.
- Look picker: Bold, Clean, Easy read; modes Auto, Light, Dark. Saved in the browser.
- Fonts bundled (Londrina Solid, Jost, IBM Plex Sans, Atkinson Hyperlegible; SIL OFL 1.1).
