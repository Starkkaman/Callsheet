# Changelog

## 0.4.0

Server and API for this release; the page that uses them follows.

- **Slack (read-only).** Connect a Slack workspace per area with a user token (`xoxp-`) from a Slack app
  you create from Callsheet's manifest (read scopes only, no write scopes). Callsheet calls only Slack's
  read methods (auth.test, team.info, users.info, users.list, conversations.list/history/replies/info),
  waits when Slack asks it to slow down, and caps the requests per read. **Needs a reply** gains Slack rows:
  DMs and group DMs where the other person spoke last, @mentions you haven't answered, and new replies in
  threads you're in, with the same reasons as mail and VIPs matched by email (else by name). @channel,
  @here and @everyone go to Heads up. Channels can be ignored. Areas get a **chat** read switch.
- **Smarter writer.** A summary per area and up to 5 **Top of the day** items that point at rows in the
  brief; the day summary is 3–6 sentences. Without the writer, Top of the day comes from the rules (VIPs,
  clashes, deadlines). Slack rows go to the writer with their last 3 messages.
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
