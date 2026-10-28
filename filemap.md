# File map

- `README.md`: what Callsheet is, status, install steps, dev commands.
- `CLAUDE.md`: project rules (read-only, offline, relative URLs), design source, test commands.
- `filemap.md`: this file.
- `.gitignore`: Python caches, venvs, logs, local screenshots.
- `repository.yaml`: Home Assistant app repository (name, url, maintainer).
- `callsheet/config.yaml`: app manifest (slug callsheet, 0.1.0, ingress on 8099, sidebar icon/title).
- `callsheet/Dockerfile`: image from the HA base-python image; copies app/ and run.sh.
- `callsheet/run.sh`: container start script (bashio log line, starts server.py on 8099).
- `callsheet/DOCS.md`: user documentation shown in Home Assistant (today and next).
- `callsheet/CHANGELOG.md`: release notes per version.
- `callsheet/app/server.py`: stdlib web server: static files, `api/brief`, cache headers, client allowlist.
- `callsheet/app/sample_brief.json`: sample Daily Brief data (v3 example, `"sample": true`).
- `callsheet/app/static/index.html`: page shell: header (wordmark, sample tag, look picker) and main.
- `callsheet/app/static/app.css`: theme tokens (from themes.css) + Daily Brief styles, phone under 700px.
- `callsheet/app/static/app.js`: fetches `api/brief`, renders the brief, look/mode, area filter, tabs.
- `callsheet/app/static/fonts/`: Londrina Solid, Jost (ttf), IBM Plex Sans, Atkinson Hyperlegible (woff2, latin).
- `callsheet/app/static/fonts/OFL-*.txt`: SIL OFL 1.1 licence text for each font family.
- `tests/test_server.py`: stdlib unittest: routes, headers, 403 for other clients, relative and offline URLs.
