# Callsheet privacy policy

Last updated: 30 October 2026

Callsheet is a self-hosted Home Assistant app. Each person runs their own copy on their own Home Assistant
machine, with their own Google Cloud OAuth client. There is no Callsheet server, company or cloud service.

## What Callsheet reads
When you connect a Google account, Callsheet asks Google for read-only access:
- `gmail.readonly`: to find email threads that may need your reply (sender, recipients, subject, date,
  a short snippet and list headers).
- `calendar.readonly`: to show today's events and flag clashes.
- `openid` and `email`: to know which account you connected.
- `drive.readonly` (only if you choose "Grant Drive access" in Get to know me): to read the text of your
  Docs, Sheets, Slides and text files into the local index. Callsheet never changes, shares or deletes files.

Callsheet never sends, deletes, moves or changes email, and never creates, changes or deletes calendar events.

When you link Signal (optional), Callsheet becomes a linked device of your Signal account, like Signal Desktop,
using the open-source signal-cli program inside the app. It keeps only the text of messages that arrive after
linking, the sender's name, the chat's name and the time, plus whether a message mentions or quotes you; it does
not keep attachments, stickers, reactions, stories, calls or view-once messages, and it deletes disappearing
messages when their timer runs out. Phone numbers, Signal ids and group ids are stored only as salted hashes;
your own number only in masked form. Messages received from Signal are deleted after 30 days. If you import
older messages from a Signal export file, only text, names and times are kept (the keys and phone numbers in the
file are skipped), and only for the chats you pick.

Callsheet never sends Signal messages, reactions, read receipts or typing notes. As a linked device, signal-cli
itself still talks to Signal's servers the way every Signal app does: it fetches your messages and keys, sends
delivery receipts for messages it receives, and answers the protocol's own session and retry messages.

## Where your data goes
- Sign-in tokens and the data Callsheet reads are stored only on your own Home Assistant machine, in the
  app's private storage. Home Assistant backups include them, except the Signal keys (after a restore, link
  Signal again).
- Callsheet talks only to Google's APIs, to GitHub's API when you add a token, and to Signal's servers through
  signal-cli when you link Signal, to read your data. It sends your data to no one else: no analytics,
  no tracking, no third parties.
- If you later turn on an AI writer (a cloud model such as Claude, or a local model on your network), only
  the items Callsheet has picked for your brief are sent to the writer you chose. This is off until you turn it on.
- Get to know me (0.6.0) keeps a full-text index of what it read (mail, events, Slack messages, Drive files,
  Obsidian notes) in its own file on your Home Assistant machine. It is left out of Home Assistant backups,
  and never leaves the machine as a whole: Chat sends the search results it uses to answer only when you pick
  Claude for it, and the brief writer never gets it. When you ask for suggestions or questions, the AI writer you
  chose gets only a summary from it: names, email domains, counts and short subject lines; nothing is kept in
  Memory unless you save or answer it. You can delete it from the page at any
  time; turning a source off or disconnecting an account removes what was read from it.
- Chat (0.6.0) keeps your questions and its answers on your Home Assistant machine for 30 days, and you
  can delete them at any time. With a local model, questions and search results stay on your network. If you
  pick Claude for chat, each question, the recent conversation and the index search results it needs are
  sent to Anthropic to answer it. Chats are never added to Callsheet's memory or your brief.
- Projects (0.7.0) keeps your projects, tasks, notes and slipped marks on your Home Assistant machine only; they
  are never written to Obsidian or any account. If you ask for suggestions, the AI writer you chose gets only a
  summary: counts, short titles, people's names and dates.
- GitHub (0.7.0, optional): with a fine-grained read-only token you provide, Callsheet reads (`GET` only) the
  repos you pick: repo details, commits, issues and pull requests, from `api.github.com`. It never writes to
  GitHub. The token is stored in the app's private storage and never shown or logged; "Forget GitHub" deletes it
  and what was read.
- Connectors (0.10.0, optional): Callsheet reads the public feeds you add in Setup → Connections → Connectors
  (YouTube channels and news feeds), with a plain `GET` over https, about every one or two hours. What is fetched:
  the feed itself, and once, when you add a YouTube channel by its @handle, the channel's public page to find its
  id. Nothing about you is sent: no account, cookies, sign-in, search words or your watch list; the only thing a
  site learns is that a Callsheet feed reader at your home's internet address read its feed. Callsheet never opens
  the stories or videos, and never downloads images or thumbnails. Each site is listed under **Places Callsheet
  contacts** with the date you added it; removing the connector stops the reading and deletes what was read.
  Items are kept for 30 days in the local index.

## Your control
- Disconnect an account in Callsheet's setup (Areas): Callsheet deletes its tokens and data and revokes the
  access at Google.
- You can also remove access at any time at https://myaccount.google.com/permissions.
- Unlink Signal in Callsheet's setup (Areas): Callsheet deletes its Signal keys and the messages it received;
  imported history stays until you delete it there. On your phone, also remove "Callsheet" under Signal →
  Settings → Linked devices.
- Uninstalling the app deletes all of its stored data.

Callsheet's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact
Questions: open an issue at https://github.com/Starkkaman/callsheet/issues.
