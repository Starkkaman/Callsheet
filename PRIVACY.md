# Callsheet privacy policy

Last updated: 29 October 2026

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

## Where your data goes
- Sign-in tokens and the data Callsheet reads are stored only on your own Home Assistant machine, in the
  app's private storage. Home Assistant backups include them.
- Callsheet talks only to Google's APIs, to read your data. It sends your data to no one else: no analytics,
  no tracking, no third parties.
- If you later turn on an AI writer (a cloud model such as Claude, or a local model on your network), only
  the items Callsheet has picked for your brief are sent to the writer you chose. This is off until you turn it on.
- Get to know me (0.6.0) keeps a full-text index of what it read (mail, events, Slack messages, Drive files,
  Obsidian notes) in its own file on your Home Assistant machine. It is left out of Home Assistant backups,
  never leaves the machine except for the search results Chat sends when you pick Claude for it, and is not sent to the brief writer. You can delete it from the page at any
  time; turning a source off or disconnecting an account removes what was read from it.
- Chat (0.6.0) keeps your questions and its answers on your Home Assistant machine for 30 days, and you
  can delete them at any time. With a local model, questions and search results stay on your network. If you
  pick Claude for chat, each question, the recent conversation and the index search results it needs are
  sent to Anthropic to answer it. Chats are never added to Callsheet's memory or your brief.

## Your control
- Disconnect an account in Callsheet's setup (Areas): Callsheet deletes its tokens and data and revokes the
  access at Google.
- You can also remove access at any time at https://myaccount.google.com/permissions.
- Uninstalling the app deletes all of its stored data.

Callsheet's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact
Questions: open an issue at https://github.com/Starkkaman/callsheet/issues.
