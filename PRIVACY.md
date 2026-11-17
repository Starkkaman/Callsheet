# Callsheet privacy policy

Last updated: 13 November 2026

Callsheet is a self-hosted Home Assistant app. Each person runs their own copy on their own Home Assistant
machine, with their own Google Cloud OAuth client. There is no Callsheet server, company or cloud service.

**Nothing is sent from here except an RSVP you press.** Callsheet sends no email, Signal, Slack or text message. It
creates exactly two kinds of things in your accounts, each only when you press: an event in your Google Calendar
(**Add to calendar**) and a draft in your Gmail Drafts (**Put in Gmail Drafts**, or **Confirm** on Chat's draft card).
It answers a calendar invite only when you press **Yes**, **No** or **Maybe** on it; Google then tells that event's
organizer, and nothing else is sent. Everything else it does with your accounts is reading.

## What Callsheet reads
When you connect a Google account, Callsheet asks Google for read-only access (the two create permissions below are
asked for only when you press their Allow button):
- `gmail.readonly`: to read your mail, received and sent, into the message store on your Home Assistant machine
  (below), so Callsheet can show what needs you, what you promised and what's coming up.
- `calendar.readonly`: to read your events and flag clashes on any date.
- `openid` and `email`: to know which account you connected.
- `drive.readonly` (only if you choose "Grant Drive access" in Get to know me): to read the text of your
  Docs, Sheets, Slides and text files into the message store. Callsheet never changes, shares or deletes files.

- `calendar.events` (only if you press "Allow adding events" for an account in Setup → Connections, 0.21.0): to add
  an event to your calendar when you press Add on one item. Nothing else is written: Callsheet never changes or
  deletes events, never invites anyone (no guests; Google is told not to send updates), and never touches calendar
  settings or sharing. Each added event carries a private marker with the item's number so it is never added twice.
- `gmail.drafts.create` (only if you press "Allow drafts in Gmail" for an account in Setup → Connections, 0.21.0):
  to save a suggested reply as a draft in your Gmail Drafts when you press "Put in Gmail Drafts" or confirm Chat's
  draft card (and one "Callsheet test draft — safe to delete" when you press "Test drafts"). The draft holds the reply
  text you saw, addressed to the person you're replying to, in the same conversation. It stays in your Drafts until
  you send or delete it yourself.

- `contacts.readonly` (only if you press "Allow contacts" for an account in Setup → Connections, 0.21.0): to read
  your Google Contacts (names, email addresses, phone numbers, birthdays, organisations, nicknames) so that a Signal
  number, a text's number, an email address and a Slack user who are one person show as that person, and so that
  their birthdays appear in Coming up. Callsheet reads only your own contacts list (not "other contacts"), every 6
  hours and when you press Sync now. It never creates, changes or deletes a contact. The contacts are stored in
  Callsheet's database on your Home Assistant machine (and its backups), never sent anywhere, and forgotten when you
  disconnect the account.

Callsheet never sends, deletes, moves or changes email (it can only add a draft you asked for, and never changes or
deletes a draft). It never changes or deletes calendar events, and creates one only when you press Add (above).

When you link Signal (optional), Callsheet becomes a linked device of your Signal account, like Signal Desktop,
using the open-source signal-cli program inside the app. It keeps only the text of messages that arrive after
linking, the sender's name, the chat's name and the time, plus whether a message mentions or quotes you; it does
not keep attachments, stickers, reactions, stories, calls or view-once messages, and it deletes disappearing
messages when their timer runs out. Phone numbers, Signal ids and group ids are stored only as salted hashes;
your own number only in masked form. The message text is kept in the message store (below). If you import
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
- Callsheet's AI runs on the model server you set up on your own network: each new message is read there once,
  with its conversation, and the morning brief and Chat's answers are written there. Message text goes to no other
  model unless you save a Claude API key (Setup → Advanced) and choose Claude, and then only what that request needs.
- Draft a reply (0.21.0) runs only when you press it, on your local model: the conversation, what is asked, your
  calendar on those days, your writing instructions and your saved notes about that person go to the local model
  server you set up (on your network) and nowhere else. The suggested reply is kept with the item on your Home
  Assistant machine until you ask for a new one; it is never sent.
- The message store keeps the text of every message Callsheet reads, received and sent (Gmail, Signal, Slack and
  texts), your calendar events, and the Drive files and Obsidian notes you tick in Get to know me, in its own file
  (`messages.db`) on your Home Assistant machine. It is included in Home Assistant backups, and nothing is deleted
  from it by default. It never leaves the machine as a whole: Chat, the brief and the voice assistant API use only
  what a request needs. Get to know me keeps nothing in Memory unless you save or answer it; turning a Drive or notes
  source off removes what was read from it. An older index file (`index.db`) from before 0.21.0 is read once into
  the store and then left on disk, unused, until you clear it in Setup › Advanced › Old data from earlier versions
  (which removes only what you tick, after asking).
- Chat (0.6.0) keeps your questions and its answers on your Home Assistant machine for 30 days, and you
  can delete them at any time. With a local model, questions and search results stay on your network. If you
  pick Claude for chat, each question, the recent conversation and the search results it needs are
  sent to Anthropic to answer it. Chats are never added to Callsheet's memory or your brief.
- Projects (0.7.0) keeps your projects, tasks, notes and slipped marks on your Home Assistant machine only; they
  are never written to Obsidian or any account. If you ask for suggestions, the model you use gets only a
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
  Items are kept for 30 days on your Home Assistant machine. For a YouTube channel the feed is the channel's feed or, when that
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
- **Voice assistant API (0.21.0, off by default).** When you switch it on (Setup → Connections → Voice assistant),
  devices you give a token can read, over your home network or a VPN only, what that token allows: your stored
  messages (search and threads), your list of asks and dates, calendar events, people and the handles you use with
  them, today's morning brief and the sources' status. That data then goes to the device and the assistant behind it
  (for example a voice assistant server on your NAS and the speech or language model it uses); Callsheet doesn't
  control what that assistant does with it. Nothing can be changed or sent through it. Tokens are stored only as
  salted hashes; revoke one and that device stops at once. The call log keeps time, device name, endpoint, status
  and result count for 90 days, never search words or message text. Never expose it to the internet (no public
  proxy).

## Your control
- Disconnect an account in Callsheet's setup (Areas): Callsheet deletes its tokens and data and revokes the
  access at Google.
- You can also remove access at any time at https://myaccount.google.com/permissions.
- Unlink Signal in Callsheet's setup (Areas): Callsheet deletes its Signal keys and the messages it received;
  imported history stays until you delete it there. On your phone, also remove "Callsheet" under Signal →
  Settings → Linked devices.
- Turn the voice assistant API off, or revoke a device, in Setup → Connections → Voice assistant.
- Uninstalling the app deletes all of its stored data.

Callsheet's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact
Questions: open an issue at https://github.com/Starkkaman/callsheet/issues.
