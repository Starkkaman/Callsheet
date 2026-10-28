# Callsheet

Callsheet is a Home Assistant app (add-on) that gives you a **Daily Brief** every morning: today's
schedule across calendars (with clashes and things that are not on a calendar yet), messages that
need a reply, heads-ups, and project progress grouped by area (Personal plus one per employer).

It reads and drafts only. It never sends anything for you.

## Status

**Step 1 (0.1.0): app skeleton with sample data.** The page opens in the Home Assistant sidebar and
shows the approved Daily Brief design with example data. There are no accounts, no options and no
real data yet. See [callsheet/DOCS.md](callsheet/DOCS.md) for what comes next.

## Install

Needs Home Assistant OS or Supervised (apps are not available on Home Assistant Container or Core).

1. In Home Assistant go to **Settings** > **Apps** (called **Add-ons** in older versions) and open the
   app store (**Install app**, or **Add-on store**).
2. In the top-right **⋮** menu select **Repositories**.
3. Add `https://github.com/Starkkaman/callsheet` and select **Add**.
4. Find **Callsheet** in the store, select **Install**, then **Start**, then **Open Web UI**.
   Turn on **Show in sidebar** to keep it one click away.

Home Assistant clones the repository with Git, so it has to be reachable from your Home Assistant
machine (a private GitHub repository is not).

## Layout

- `repository.yaml`: the app repository for Home Assistant.
- `callsheet/`: the app (config, Dockerfile, docs) and `callsheet/app/` (Python server and web page).
- `filemap.md`: one line per file.

## Develop

```sh
python3 callsheet/app/server.py --port 8099      # then open http://127.0.0.1:8099/
python3 -m unittest discover -s tests            # server + ingress-safety tests (stdlib only)
```

## Fonts

Bundled in `callsheet/app/static/fonts/`, each under the SIL Open Font License 1.1 (licence texts next
to the fonts): Londrina Solid, Jost, IBM Plex Sans, Atkinson Hyperlegible.
