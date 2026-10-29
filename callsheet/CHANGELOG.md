# Changelog

## 0.3.1

- "Open in Gmail" links open the message directly in the right account, in Gmail's own link format
  (`/mail/u/?authuser=<email>#all/thread-f:<id>|msg-f:<id>`), so they work when several Google accounts
  are signed in to one browser (they used to show Gmail's "account temporarily unavailable" page).

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
