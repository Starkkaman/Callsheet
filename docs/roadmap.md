# Callsheet roadmap (from the owner's wishes, 2026-10-29)

Hard rules stay: read-only, drafts-only (nothing is ever sent, accepted or deleted), the writer has no tools,
real data stays on HA + the LAN model server.

## 0.4.0: Slack, smarter writer, cleaner "Needs a reply" (building now, layout-independent first)
- **Slack** (one workspace, every channel the owner is in): internal Slack app from a manifest, read-only user
  token pasted per area. Needs a reply = DMs/group DMs where the other person spoke last, @mentions, replies in
  threads the owner posted in, VIPs; @channel/@here → Heads up; per-channel ignore list.
- **Writer**: a more detailed day summary (3–6 sentences), one summary per area, and "Top of the day" (≤5
  ranked items, each pointing at a real row). The owner can edit the writing instructions in Setup (summary
  style, area summary style, draft style), with Reset; the safety rules are fixed and always added.
- **Needs a reply only when a reply is needed**: Gmail Promotions/Updates/Forums/Social labels are excluded even
  in Workspace inboxes; each row gets "Doesn't need a reply" → this thread / always for this sender / always
  for this domain (e.g. work-order systems). The list is editable in Setup.
- **Layout**: the approved calmer brief (Top of the day, By area, one merged list, folded sections, check-in
  "New since", density), unless the owner picks one of the new layout mockups instead (then that one).

- **12/24-hour time format** (setting in About you; default 12-hour for the owner), used everywhere incl. the
  writer's summaries.
- **Prompt profiles**: writing instructions are named profiles (Default + the owner's copies); one is active.
- **Evaluation, generalised** (after the backend above): the owner defines 2–4 contenders (name, model from the
  server's model list, thinking on/off, effort, budget, optional sampling, prompt profile), saved for re-runs;
  head-to-head as each-vs-each or all-vs-a-baseline; same model with different profiles, or different models;
  then reload the everyday model; blind pairs, names after submitting; Download as before.

## 0.5.0: memory, dates, Signal, push
- **Notes to Callsheet**: on any row or event, "Note for Callsheet" (e.g. "Jordan's volunteer requests can wait
  until Sunday"). Notes live in a Markdown memory file in the app's storage, editable in Setup; the writer gets
  the notes relevant to the people, areas and topics in today's data.
- **Dates found in mail and chat** → "Possible events" in the schedule, each with **Add to calendar**: a
  prefilled Google Calendar page in the right account, which the owner saves (no calendar write access).
- **Signal**: signal-cli as a linked device (QR code, like Signal Desktop), running locally; new messages from
  the moment of linking; read-only.
- **Phone push** via the HA app at call times, plus **urgent alerts** between briefs (a light check every
  ~10 min for VIP or urgent messages).
- **Model per call time** (see daily-brief/model-per-call-time.md), decided after the writer evaluation.

## Later: planning assistant
- Access to the owner's notes (which app: to decide), projects and timelines from notes, tasks and repos
  (real "Projects" section), reminders of things that slipped, live updates.
