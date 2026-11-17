# Callsheet

Callsheet is a Home Assistant app (add-on) that works as a personal assistant: it keeps your mail, calendar,
Signal, Slack and texts on your Home Assistant machine, reads each new message once with a local model, and shows
what needs you (clashes, asks waiting on you, what's coming up in the next 30 days, what you can clear). A morning
brief talks you through the day, and Chat answers questions about your own messages.

Nothing is sent from here except an RSVP you press. It reads; the only things it creates are a calendar event and a
Gmail draft, each only when you press, and it answers an invite only when you press Yes, No or Maybe on it.

## Status

Version 0.21.2. See [callsheet/DOCS.md](callsheet/DOCS.md) for setup and use, and
[callsheet/CHANGELOG.md](callsheet/CHANGELOG.md) for what changed.

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

## Develop

```sh
python3 callsheet/app/server.py --port 8099      # then open http://127.0.0.1:8099/
python3 -m unittest discover -s tests            # server + ingress-safety tests (stdlib only)
```

## Fonts

Bundled in `callsheet/app/static/fonts/`, each under the SIL Open Font License 1.1 (licence texts next
to the fonts): Jost (`jost.ttf`, `OFL-jost.txt`) and IBM Plex Mono (`ibm-plex-mono-400.woff2`,
`OFL-ibmplexmono.txt`).
