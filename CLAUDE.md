# Callsheet: project rules

Home Assistant app (add-on) that shows a Daily Brief through Ingress. Product name **Callsheet**;
"Daily Brief" is the morning brief inside it. Context and history: `~/project/daily-brief/HANDOFF.md`.

## Hard rules
- **Read-only and drafts-only.** Nothing in this app sends mail, messages or replies. "Nothing was sent"
  is a fixed promise in the UI, not a setting. (0.5.1 phone notifications go only to the owner's own phones,
  through Home Assistant's notify service: sender name and subject, never message text.)
- **No external requests at runtime.** The page and server must work offline: fonts and assets are
  bundled, no CDNs, no analytics. Outbound traffic from the server goes only to Google (the user's own
  accounts, read-only), `api.anthropic.com` (Claude writer, through the SDK), the owner's configured
  local model URL, and (0.5.1, phone notifications only) the Supervisor's Core API proxy
  `http://supervisor/core/api` with `SUPERVISOR_TOKEN`, limited by the (method, URL) allowlist in `ha.py` to
  exactly `GET /api/services` (list the `notify.mobile_app_*` targets) and
  `POST /api/services/notify/mobile_app_<name>` (send one notification): no other service, state or event
  endpoint, and the token is never logged, stored or returned. Since 0.5.1, signal-cli (linked device, owner
  approved 2026-10-30) talks to Signal's servers to receive the owner's messages; `signalcli.py`'s JSON-RPC method
  allow-list keeps it receive-only (no send, reactions, read receipts or typing; delivery receipts are
  automatic). Each needs the user's setup first. The
  browser page itself never calls out.
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
- No accounts, secrets or user data in the repo. Never print secrets.

## Design
- Source of truth: `~/project/daily-brief/mockups/daily-brief-v3.html` (brief view) and
  `mockups/themes.css`, approved 2026-10-28. Desktop = v3 frame A, phone (<=700px) = frame B's order.
- Looks: Bold, Clean, Easy read; modes Auto/Light/Dark. Components use only the theme tokens at the top
  of `app.css` (copied verbatim from themes.css; keep them in sync if the design changes).
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
