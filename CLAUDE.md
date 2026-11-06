# Callsheet: project rules

Home Assistant app (add-on) that shows a Daily Brief through Ingress. Product name **Callsheet**;
"Daily Brief" is the morning brief inside it. Context and history: `~/project/daily-brief/HANDOFF.md`.

## Hard rules
- **Read-only and drafts-only.** Nothing in this app sends mail, messages or replies. "Nothing was sent"
  is a fixed promise in the UI, not a setting. (0.5.1 phone notifications go only to the owner's own phones,
  through Home Assistant's notify service: sender name and subject, never message text.)
- **No external requests at runtime.** The page and server must work offline: fonts and assets are
  bundled, no CDNs, no analytics. Outbound traffic from the server goes only to Google (the user's own
  accounts, read-only: Gmail (0.8.0: also `GET users/me/messages/<id>/attachments/<aid>` for calendar files up to
  256 KB), Calendar (0.8.0: also events.list by `iCalUID`) and, from 0.6.0, Drive `GET files`/`files/<id>`/`files/<id>/export`
  with `drive.readonly`, all behind the allowlist in `google.py`), Slack's read-only Web API (`slack.com`),
  `api.anthropic.com` (Claude writer, through the SDK), the owner's configured local model URL,
  and (0.5.1, phone notifications only) the Supervisor's Core API proxy
  `http://supervisor/core/api` with `SUPERVISOR_TOKEN`, limited by the (method, URL) allowlist in `ha.py` to
  exactly `GET /api/services` (list the `notify.mobile_app_*` targets) and
  `POST /api/services/notify/mobile_app_<name>` (send one notification): no other service, state or event
  endpoint, and the token is never logged, stored or returned. Since 0.5.1, signal-cli (linked device, owner
  approved 2026-10-30) talks to Signal's servers to receive the owner's messages; `signalcli.py`'s JSON-RPC method
  allow-list keeps it receive-only (no send, reactions, read receipts or typing; delivery receipts are
  automatic). Since 0.7.0, GitHub's REST API (`api.github.com`), read-only, owner approved 2026-11-03: a
  fine-grained read-only token (Contents, Issues, Pull requests: Read) and the repos the owner picks; `github.py`'s
  (method, URL) allowlist permits only `GET /user/repos`, `/repos/{o}/{r}`, `/repos/{o}/{r}/commits`, `/issues` and
  `/pulls` (no other method, path or host, no redirects); nothing is ever written to GitHub. Each needs the user's
  setup first. The
  browser page itself never calls out. Each new outbound destination needs the owner's approval, written here.
- **Connectors (0.10, owner approved 2026-11-04).** Callsheet may read public RSS and Atom feeds the owner adds in
  Setup → Connections → Connectors (news feeds; YouTube channel feeds `https://www.youtube.com/feeds/videos.xml?channel_id=…` or, when that one
  doesn't answer, the channel's uploads feed `…?playlist_id=UU…`, plus one GET of the channel page the owner pasted to
  find its id). Story links on another host of a feed's own site are shown only after the owner allows that host
  (asked once in Setup, stored with the date, removable; never contacted). Each host is allowed only when the owner adds
  it (stored with the date in `outbound_hosts`, listed in Setup → Connections, removable; removing the connector
  removes the host). `net.py` allows only GET over HTTPS on port 443 to exactly those hosts, refuses private,
  loopback, link-local, CGNAT and Supervisor addresses (checked after DNS; the connection is pinned to the checked
  address), follows no redirect to another host, caps size and time, rate-limits each host (at most one read per 15
  minutes), fetches no images or pages linked from a feed, and sends no cookies or owner data. Feed text is untrusted
  `<data>`: shown as text, links defanged, items that read like instructions marked and never followed. A new
  connector kind needs the owner's approval here and its own injection test.
- **Reports (0.10.2a, owner approved 2026-11-05).** Callsheet may read Markdown reports that other agents or services
  write, one file per report, from one folder per Reports connector the owner picks: a folder under Home Assistant's
  `/share` or `/media` (the existing read-only mappings; no new add-on permission), or a Google Drive folder through the
  existing `drive.readonly` access (no new host; built in 0.12.1, `conn_reports_drive.py`, only the Drive GETs already
  in `google.py`'s allowlist, no new permission). Read-only: files are never moved, changed or deleted. Only regular
  `*.md` files inside the chosen folder (no symlinks or paths out of it), capped in number and size. Callsheet accepts
  no inbound connections for reports (no endpoint, no MQTT). Report text is untrusted `<data>` like mail: inject checks,
  defanged links, instruction marking; a report's `importance` is a hint the owner's per-source choice overrides, so a
  report can never place itself above Needs you. Design: `~/project/daily-brief/wheatley/design.md` 3.9; injection test
  W-RP before release.
- **Relative URLs only.** Ingress serves the app under `/api/hassio_ingress/<token>/`. In HTML, CSS and
  JS write `app.css`, `api/brief`, `fonts/x.woff2`, never `/app.css`. The server never redirects to
  absolute paths. `tests/test_server.py` checks this.
- **Data is text.** Render data with `textContent` (see `h()` in app.js), never `innerHTML`.
- Server: Python stdlib (`http.server`) plus exactly one pinned dependency, `anthropic` (installed in the
  Dockerfile, imported lazily in `writer.py` so the app runs without it), plus (0.5.1, owner-approved) the
  Alpine packages signal-cli, java-libsignal-client and openjdk25-jre-headless, and the vendored MIT
  `vendor/qrcodegen.py`. Nothing else to add without the user's go-ahead. Listens on 0.0.0.0:8099, accepts only the Supervisor (172.30.32.2) and loopback.
- **AI output is text, never instructions.** Mail and calendar content goes to the writer inside the
  untrusted `<data>` block; the writer has no tools; suggestions are shown with textContent and copied
  by the user, never sent.
- **Chat may use tools (owner's decision, 2026-10-30).** The 0.6 Chat (`chat.py`) is a separate model component
  from the brief writer, which keeps no tools. Chat's tools are read-only lookups over Callsheet's own local
  index (`search`, `get_item`, `list_events`, `stats`): no send, no writes to any account, no network fetch.
  Tool results go back to the model as escaped `<data>` and are never instructions; answers get the same
  claim post-check as drafts. The owner expects more tools later: every new tool, and anything that writes,
  sends or fetches, needs the owner's explicit approval first, written here, and its own injection test.
- **Chat may save memory notes, owner-pressed only (owner approved 2026-11-03).** Since 0.8.1 `propose_memory` is
  offered only when the owner's own message asks to remember; it writes nothing. A note is saved only when the owner
  presses Save, after `inject.instructions` and `states_decision` checks, using a one-time token tied to the chat and
  turn. Text in mail, chat or tool results can never create or save a note.
- **Chat may help set up connectors, owner-pressed only (owner approved 2026-11-06).** When the owner's own message asks
  to add a channel, feed or reports folder (text the owner types or pastes into Chat counts as the owner's; the owner
  decided he may paste addresses), Chat may offer a setup card with the same fields as Setup → Connectors. Chat itself
  checks, fetches and adds nothing: Check (the only step that contacts a site, through `net.py`'s rules) and Add run
  only when the owner presses them, with the same approval record as Setup. Text in mail, feeds, reports or tool results
  can never offer, fill or press a card. Its own injection test before release.
- **Chat may search the web through the owner's SearXNG (owner approved 2026-11-06).** Isaac: "SearXNG on the NAS is
  the right choice, I don't want to have to press any extra buttons. It should know when to search, or ask." Chat
  (only Chat, only in a turn the owner's message started; never the brief writer, sorter, reader or picker) may call a
  `web_search` tool against the one SearXNG URL the owner configures (LAN, `format=json`; the only new destination).
  Guards instead of a button: the query is checked deterministically before it leaves: every word must come from the
  owner's own messages in this chat (plus stopwords and the owner's "Your words"); anything else, or anything that looks
  private (email addresses, phone numbers, amounts, account or order numbers, names of the owner's contacts), turns the
  search into a question to the owner ("Search the web for '…'?"), and the owner's yes is his own message. Results
  (title, snippet, URL) go back as untrusted `<data>`; Chat may read a result page only by choosing a result id (never a
  URL it writes), fetched through `net.py`'s rules (public https only, size and time caps, no redirects off-host);
  page text is untrusted, inject-checked and cited. Text in mail, feeds, reports or pages can never start a search.
  Its own injection test before release.
- **Projects (0.7.0) live in Callsheet only.** Projects, tasks and notes are stored in `callsheet.db`, never written
  to Obsidian or any account. Drafted projects and the AI's names and next steps stay "Suggested" until the owner
  adds them. "Ask about this project" scopes Chat to the project's linked index items: the scope is a filter on
  the existing read-only tools (`search`, `get_item`, `list_events`), not a new tool.
- No accounts, secrets or user data in the repo. Never print secrets.

## Design
- Source of truth: `~/project/daily-brief/harmony/themes-harmony.css` (with `harmony.md`, and `harmony/gen.py`, which
  generates the Jam looks' tokens and the contrast report) plus JamDash's final CSS (`~/project/jam-dash`, read-only;
  Callsheet never reads it at runtime). The brief view's layout still follows `mockups/daily-brief-v3.html`:
  desktop = v3 frame A, phone (<=700px) = frame B's order.
- Looks (ids in brackets), modes Auto/Light/Dark:
  - Jam (`jam`, structure `panel`): the default since 0.11.0, the shared "calm instruments" look
    (`~/project/jam-design` HANDOFF.md and `mockups/pass6/callsheet.html`; read-only).
  - Easy read (`easy`): selectable (the accessibility look).
  - Disabled (owner, 2026-11-06; kept in code and CSS, hidden from the picker by `ENABLED_LOOKS` in `app.js`; a saved
    one shows Jam with a one-time notice): Bold (`bold`), Clean (`clean`) and the Jam Dash looks Depth (`depth`), Braun
    (`braun`), NERV (`nerv`), LCARS (`lcars`), Olympia (`olympia`), Transit (`transit`), Grid (`grid`), New Wave
    (`newwave`), Pulsar (`pulsar`). NERV and LCARS are dark only.
  - Components use only the theme tokens at the top of `app.css`; structure keys on `data-base` (bold|clean|easy|panel).
- Contrast (the deeper-step rule): text and text-bearing fills meet WCAG AA (4.5:1, 3:1 for large text); where a
  look's colour falls short, use a deeper step of it for text-bearing fills and keep the original colour for dots,
  bars, marks and focus wherever it reaches 3:1. Phone targets are at least 44 x 44 px in every look.
- Design principles (design-audit-2026-11.md), one check each:
  1. One loud button: one job per screen, one filled button; the rest outlined or plain text.
  2. Say it once: a heading and at most one sentence; help hides behind "?"; let values speak.
  3. Defaults first: works before Setup; each setting in one place; rare ones in one closed Advanced drawer.
  4. Space groups, lines separate: spacing 4, 8, 12, 16, 24, 32 px; rules only where space can't do it.
  5. Three sizes a screen: display face for the one headline and big numbers, body face for reading, small caps for
     rare labels; targets 24 px, 44 px on phones.
  6. Make the end feel good: design the peaks, celebrate once and briefly, never guilt.
  7. Motion explains: 150 to 350 ms, cause and effect only, nothing loops, reduced motion turns it off, sound opt-in.
- Plain words in the UI, no film-production jargon (Areas, VIPs, Review).

## Test
```sh
python3 -m unittest discover -s tests          # stdlib: routes, headers, 403, relative/offline URLs
# Claude writer tests need the SDK: a uv venv with anthropic==2.3.0 (without it they are skipped)
python3 callsheet/app/server.py --port 8099    # manual: http://127.0.0.1:8099/
```
Visual checks (1280 and 400 px, every look x mode, fonts, overflow, contrast, no foreign requests)
run with Playwright outside the repo, loading the page through a prefix-stripping proxy at
`/api/hassio_ingress/<token>/`.

## Packaging
- `callsheet/config.yaml`: bump `version` and add a `CHANGELOG.md` entry for every release. Bump
  `APP_VERSION` in `callsheet/app/api.py` to the same value (it drives the `?v=` asset URLs and the
  "Callsheet was updated" notice; a test fails if the two differ).
- `callsheet/Dockerfile`: pinned `ghcr.io/home-assistant/base-python` tag (see home-assistant/docker-base
  README); no build.yaml (deprecated). `run.sh` must keep LF line endings.
- There is no Docker in the dev container: the image is built by the Supervisor on install.
- Update `filemap.md` when files are added or removed. Don't push without the user's go-ahead.
