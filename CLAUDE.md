# Callsheet: project rules

Home Assistant app (add-on) that shows a Daily Brief through Ingress. Product name **Callsheet**;
"Daily Brief" is the morning brief inside it. Context and history: `~/project/daily-brief/HANDOFF.md`.

## Hard rules
- **Read-only and drafts-only.** Nothing in this app sends mail, messages or replies. "Nothing was sent"
  is a fixed promise in the UI, not a setting.
- **No external requests at runtime.** The page and server must work offline: fonts and assets are
  bundled, no CDNs, no analytics. Outbound traffic from the server goes only to Google (the user's own
  accounts, read-only), `api.anthropic.com` (Claude writer, through the SDK) and the owner's configured
  local model URL; each needs the user's setup first. The browser page itself never calls out.
- **Relative URLs only.** Ingress serves the app under `/api/hassio_ingress/<token>/`. In HTML, CSS and
  JS write `app.css`, `api/brief`, `fonts/x.woff2`, never `/app.css`. The server never redirects to
  absolute paths. `tests/test_server.py` checks this.
- **Data is text.** Render data with `textContent` (see `h()` in app.js), never `innerHTML`.
- Server: Python stdlib (`http.server`) plus exactly one pinned dependency, `anthropic` (installed in the
  Dockerfile, imported lazily in `writer.py` so the app runs without it). Nothing else to add without the
  user's go-ahead. Listens on 0.0.0.0:8099, accepts only the Supervisor (172.30.32.2) and loopback.
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
