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
  Items are kept for 30 days in the local index. For a YouTube channel the feed is the channel's feed or, when that
  one doesn't answer, its uploads feed on the same site. A story site you allow with **Show links** is only used to
  show links; Callsheet never contacts it.
  Callsheet runs on Home Assistant's internal bridge network, not your home network directly, and it refuses
  to read any address on your home network, Home Assistant's own network or a cloud metadata address, even when a
  feed's name points there.
- Web search in Chat (0.14.0, optional, off until you add your SearXNG in Setup → AI → Web search): Chat can
  search the web through your own SearXNG on your home network (Callsheet asks it only for a search, nothing
  else on it). The words searched are only words you wrote in that chat; when a
  search would include anything else (words from your mail or a web page, an email address, a phone number, an
  amount, an order or account number, a long number, a web address, or a contact's name), Chat first asks "Search
  the web for '…'?" with the exact words, and searches only after your yes. SearXNG sends those words on to the
  search engines it uses (for example Brave or Wikipedia).
  Chat may also open up to two result pages per question (only pages from its search results, never an address
  from your mail or the model). **This is a new kind of contact: any public website in the results, not only sites
  you added. Each site Chat opens sees your Home Assistant box's public internet address** and Callsheet's fixed
  user agent, and nothing else: no cookies, no sign-in, no referrer. Pages are read as text (no images or
  scripts), at most 1 MB, and are not kept beyond the chat. At most 5 searches and 5 pages per question, and 100
  searches and 100 pages a day (you can lower or raise the daily numbers in Setup); the log keeps counts only, never
  the words searched. Pressing a web source in an answer opens that page in your own browser, without a referrer. Turn
  it off in Setup, or remove the address, to stop it.
- **Show thinking (0.14.0).** With the switch on, the model's reasoning is shown in Chat's Thinking panel. With your
  local model it stays on your network (it comes from the same model server). With Claude, thinking happens at
  Anthropic like the rest of the answer. Callsheet keeps the reasoning only with the chat (deleted with it, at most 30
  days), never saves it as a note and never sends it back to a model.

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
