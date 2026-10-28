# Callsheet

Callsheet puts your **Daily Brief** in the Home Assistant sidebar: today's schedule (with clashes and
events that are not on a calendar), messages that need a reply, heads-ups, and progress per project,
grouped by area (Personal and one per employer).

Callsheet only reads and drafts. It never sends anything on your behalf.

## What it does today (0.1.0)

- Opens as a sidebar page through Ingress (**Open Web UI**, or turn on **Show in sidebar**).
- Shows the Daily Brief with **sample data**. A "Sample data" tag in the header says so.
- Three looks (Bold, Clean, Easy read) and three modes (Auto, Light, Dark), under **Look** in the header.
  Auto follows your device's light or dark setting. The choice is saved in this browser.
- Works offline: fonts are bundled, the page makes no requests outside Home Assistant.
- No options, no accounts, nothing stored.

## What's next

1. Setup: you, your areas, VIPs, brief and check-in times, delivery.
2. Read-only connections, starting with a personal Gmail account and calendar.
3. A real morning brief at 06:30 with check-ins at 12:30 and 17:00, stored on this machine.
4. A choice of writer: Claude, or a local OpenAI-compatible model on your network.

## Troubleshooting

- The page is blank or unstyled: restart the app and reload the page. Check the **Log** tab.
- The app only answers the Home Assistant ingress proxy. It has no port of its own on purpose.
