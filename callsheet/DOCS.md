# Callsheet

Callsheet is a personal assistant in the Home Assistant sidebar. It keeps the messages from your mail, calendar,
Signal, Slack and texts on this box, reads each new one once with your local model, and shows what needs you: clashes,
asks waiting on you, what's coming up, and what you can clear. **Talk it through** goes through your day with you, and
**Chat** answers questions about your own messages.

**Nothing is sent from here except an RSVP you press.** Callsheet reads your accounts and creates only two things in
them, each only when you press: an event in your Google Calendar (**Add to calendar**) and a draft in your Gmail Drafts
(**Put in Gmail Drafts**). It answers a calendar invite only when you press **Yes**, **No** or **Maybe** on it (Google
tells the organizer). It never sends, replies, deletes or changes anything else in your accounts.

## What it does

- Opens as a sidebar page through Ingress (**Open Web UI**, or turn on **Show in sidebar**).
- Reads your own Google accounts (Gmail, Calendar, and optionally Drive and Contacts), Slack, Signal and your phone's
  texts, read-only, as the messages arrive: Signal at once, Gmail every 2 minutes, Slack every 3, Calendar every 15.
  Every message is kept, received and sent, in the message store on this box. **Setup › Connections › Sources** shows
  each source and how fresh it is, and says plainly when one stops moving.
- Your local model reads each new message once, with its conversation, and keeps a list of what people ask you, what
  you promised, and the plans, trips, bills and dates in them. A later message that moves or answers something updates
  the same item. Anything Callsheet isn't sure of is shown as "likely" with the message it came from, never hidden.
- **Today**: **Now and next** (clashes, soonest first, and today's calendar), **Needs you** (asks and your own
  promises, oldest first, with the source and how long each has waited), **Coming up** (the next 30 days, from your
  calendar and your messages, each with how far out it is) and **Clean up** (newsletters, receipts and chatter you can
  clear at once). Security alerts and calendar invites are never hidden.
- **Replies** lists the asks with more context; **Agenda** shows your calendar; **Projects** collects what you're
  working on; **Chat** answers questions and changes settings when you ask.
- Phone notifications for clashes and asks from your VIPs, and "Your brief is ready" after each brief time.
- Until an account is connected, the page shows sample data with a "Sample data" tag.

## Today

- **Clashes** say how long until they start and what each side is for, with buttons to reply (opens Gmail, Signal,
  Slack or Messages; Callsheet sends nothing), open both, or dismiss. Under 24 hours a clash turns red. After you say
  yes to one side, the card says who hasn't heard from you yet; it clears itself when Callsheet sees your message.
- **Open any item** to see where you stand: did you reply, did you say yes, and the conversation itself, with the ask
  marked in ochre and your yes in teal. A clash shows both conversations. **Chat about this** opens Chat about just
  that item.
- **Done** and **Note…** on any item or batch: "not for me", "junk", "remind me tomorrow" or your own words. "Not for
  me" and "junk" mark the items done.
- **Add to calendar** on Coming up and in an item's view, once you allow it (see below).

## Talk it through

**Talk it through** on Today opens the morning brief: the assistant (Bruce, unless you rename it) goes through your day one step at a time (your day,
clashes, what needs you, coming up, bills and trips, your channels and reports when something stands out, clean up).
Each step is a few spoken-style sentences and one picture, with Back and Next, quick replies and a reply field.
Callsheet picks the steps and the items itself, from your messages, calendar and the preferences you set in Chat; the
local model only writes the words. Every name, number and time it says is checked against that step's messages, and
anything it can't find stays marked as the assistant's words. A reply at a step is a Chat turn about that step's items,
kept in that day's "Morning brief" chat. The brief is written after each brief time (Setup › Your day).

## Set up your Google client (once)

Callsheet uses your own Google Cloud OAuth client, so no one else's server sees your mail. This takes
about 10 minutes.

1. Open <https://console.cloud.google.com/> with the Google account you want to own the client and
   create a new project, for example "Callsheet".
2. Go to **APIs & Services > Library** and enable **Gmail API**, then **Google Calendar API**.
3. Go to **Google Auth Platform** (the OAuth consent screen) and click **Get started**. Enter an app name
   (for example "Callsheet") and your email, choose audience **External**, add your email as contact,
   and accept the policy.
4. Under **Audience**, click **Publish app** so the publishing status is **In production**. Do not leave it
   in Testing: in Testing mode Google expires the sign-in after 7 days and Callsheet stops reading.
   You don't need to submit the app for verification for your own use.
5. Under **Clients**, click **Create client**, choose application type **Desktop app**, give it a name and
   click **Create**.
6. Copy the **Client ID** (it ends in `.apps.googleusercontent.com`) and the **Client secret**, and paste
   both into Callsheet where it asks for your Google client.
7. Google Workspace (work or school) accounts: the Workspace admin may need to allow this client. In the
   Admin console that is **Security > Access and data control > API controls > Manage third-party app
   access**, add the app by its client ID and mark it **Trusted**. Personal Gmail accounts don't need this.

## Connect an account

1. In Callsheet, pick the area the account belongs to (Personal, or an employer) and choose to connect a
   Google account. A Google sign-in page opens.
2. Choose the account. Google warns that it hasn't verified the app: that is expected for your own
   client. Click **Advanced**, then **Go to Callsheet (unsafe)**.
3. On the consent screen, tick **both** the Gmail and the Calendar boxes and click **Continue**.
4. Your browser then lands on a page that doesn't load, at an address starting with
   `http://127.0.0.1:8765/`. That is expected: nothing needs to run there.
5. Copy the **whole address** from the browser's address bar and paste it back into Callsheet, then finish.
   You have 15 minutes from step 1; after that, start again.
6. Callsheet shows the account with its calendars. Your main calendar is picked; pick any others you want
   on the schedule.

### Add to calendar (0.21.0, optional)

Callsheet can put an item from your messages on your calendar when you press **Add to calendar**. It needs one
more permission per account, which you add once:

1. In the Google Cloud console, open your project → **Google Auth Platform** (the OAuth consent screen) →
   **Data access** → **Add or remove scopes**. Add `https://www.googleapis.com/auth/calendar.events` (paste it under
   "Manually add scopes" if it isn't listed), press **Update**, then **Save**. The Google Calendar API is already on.
2. In Callsheet, Setup → Connections, press **Allow adding events** under the account. Open Google's page, allow
   it, and paste the address back as when you connected (Google's page asks only for this; the access you gave
   before stays as it is).
3. The account now says "Can add events · to <calendar>". If it has several calendars, pick where new events go.

Then "Add to calendar" on Today (Coming up) and in an item's view opens a small card: check the title, date, time
(empty for all day) and calendar, and press **Add**. Nobody is invited or emailed, and each item is added once.
Callsheet can't change or delete events: do that in Google Calendar. To take the permission back, remove Callsheet
at <https://myaccount.google.com/permissions> (that removes its other access too) or connect the account again
without ticking it.

To connect the same account again (for example after "needs to be connected again"), repeat these
steps; Callsheet updates the existing account. Removing an account also withdraws Callsheet's access at
Google. You can check or remove access any time at <https://myaccount.google.com/permissions>.

## Put in Gmail Drafts (0.21.0, optional)

Callsheet can save a suggested reply as a draft in your Gmail, so it waits for you in **Drafts** in the right
conversation. It never sends anything: you open the draft in Gmail, change it if you like, and send or delete it.

1. In the Google Cloud console, open your project → **Google Auth Platform** → **Data access** → **Add or remove
   scopes**. Add `https://www.googleapis.com/auth/gmail.drafts.create` (paste it under "Manually add scopes" if it
   isn't listed), press **Update**, then **Save**. The Gmail API is already on.
2. **Setup › Connections**, at the account: press **Allow drafts in Gmail**. Google's page asks only for "Compose new
   draft emails"; what the account allowed before stays as it is. Paste the address back as when you connected it.
3. Press **Test drafts** once. Callsheet saves one draft "Callsheet test draft — safe to delete" addressed to the
   account itself and says whether Google allowed it. Delete the test draft in Gmail when you like.
4. On a suggested Gmail reply (**Draft a reply** below), press **Put in Gmail Drafts**. When it's saved you see "In your Gmail Drafts ✓ · Open
   in Gmail". The draft goes to the person you're replying to, with "Re:" and the same subject, as a reply in the same
   conversation; when the message you're answering has a Reply-To address, the draft goes there (as in Gmail). With
   **Reply all** ticked (shown when others were on the message), the others go in Cc. Pressing again with the same
   text doesn't make a second draft; edit the text and press again for a
   new one (the earlier draft stays in Gmail until you delete it).
5. In **Chat**, after Chat drafts a reply, say "put this in my Gmail drafts". Chat shows a card with To, Subject and
   the text; the draft is saved only when you press **Confirm**.

If Google refuses ("Google didn't allow drafts with this permission: Callsheet can't save drafts on this account
yet"), Callsheet doesn't try again by itself. Google's documentation lists other permissions for drafts; a different
permission would need a new decision, so for now the account simply can't save drafts. A message in your mail that
says "create a draft to …" can never make Callsheet draft anything: only your own press or your own words in Chat do.

## Google Contacts (0.21.0, optional, read-only)

Callsheet can read your Google Contacts so that one person is one person everywhere: Dana's Signal number, her texts,
her Gmail address and her Slack account. Their asks, clashes and follow-ups then join up, and Today, Replies and Chat
show "Dana Ruiz" instead of a phone number.

1. In the Google Cloud console, open your project → **APIs & Services > Library** and enable the **People API**.
   Then **Google Auth Platform** → **Data access** → **Add or remove scopes**: add
   `https://www.googleapis.com/auth/contacts.readonly` (paste it under "Manually add scopes" if it isn't listed),
   press **Update**, then **Save**.
2. **Setup › Connections**, at the account: press **Allow contacts**. Google's page asks only for "See and download
   your contacts"; what the account allowed before stays. Paste the address back as when you connected it.
3. Callsheet reads the contacts at once, then every 6 hours (only what changed). The account says "412 contacts ·
   synced 2 h ago"; **Sync now** reads again. The Sources panel shows "Contacts · 412 people · Synced 2 h ago".

How people are matched: email addresses ignoring case (and, for gmail.com, dots and "+tags"); phone numbers in full
international form (a number without a country code takes your own Signal number's country, else your time zone's,
else +1); a Slack user by the email Slack gives; a Signal or text sender without a number by a full name only one
contact has (never a first name alone). Contacts come first, then the people Callsheet already knew (Setup → People).
Contacts' birthdays show in Coming up every year, unless your calendar already has them. Nothing is written to your
contacts, and they never leave Callsheet. Disconnecting the account forgets its contacts.

## Slack (optional, read-only)

Callsheet can read one or more Slack workspaces, each in an area, with a Slack app that you create in your
own workspace. It only reads: the app has no write permissions, and Callsheet only calls Slack's read
methods.

1. From Callsheet's Slack setup, open the link to create the app: it opens
   `https://api.slack.com/apps?new_app=1&manifest_json=…` with Callsheet's manifest filled in (you can
   also copy the manifest and paste it under **Create New App → From an app manifest**). The manifest
   asks for these user scopes only: `channels:history`, `channels:read`, `groups:history`, `groups:read`,
   `im:history`, `im:read`, `mpim:history`, `mpim:read`, `users:read`, `team:read`.
2. Pick your workspace and create the app, then **Install to Workspace** and allow it.
   **Admin approval:** many workspaces only let admins approve new apps. If Slack says the app needs
   approval, ask a workspace admin to approve it (or install it yourself if you are one); until then
   there is no token.
3. Under **OAuth & Permissions**, copy the **User OAuth Token** (it starts with `xoxp-`; not the bot
   token) and paste it into Callsheet for the area. Callsheet checks it with Slack and shows the
   workspace name. The token is stored like your other secrets and only its last characters are shown.

What Callsheet reads, every few minutes: the channels, DMs and group DMs the token can see, your own messages and
thread replies too, into the message store. Channels you don't want read can be ignored per workspace. When Slack
asks Callsheet to slow down, it waits as Slack says and reads the rest next time. Revoke the token in Slack (or remove
the workspace in Callsheet) to stop.

## Signal (optional, read-only)

Callsheet can read your Signal messages as a linked device, like Signal Desktop.

1. Setup → Connections → Signal → **Link Signal**. A QR code appears.
2. On your phone: Signal → Settings → Linked devices → **+**, and scan the code.
3. Messages are kept from then on, as they arrive: yours too (from your phone), and group messages with the group's
   name.

Callsheet never sends Signal messages, reactions, read receipts or typing notes. Like every linked device it
confirms delivery automatically. Signal unlinks a device that has been offline for 45 days, so keep the app
running, and update Callsheet at least every 2 months (older signal-cli versions stop working).

**Your history:** a newly linked device gets no past messages. In Signal Desktop on your computer, use Settings →
Chats → **Export chat history**, then in Callsheet upload the export's `main.jsonl` and pick the chats to keep.
Only text, names and times are kept. More in `docs/signal.md`.

## Texts (optional, read-only)

- **From the nightly backup.** If the SMS Backup & Restore app on your phone saves its file to a folder in Home
  Assistant's `share` or `media` folder, type that folder in Setup › Connections › Texts › Backup folder (for example
  `/share/sms-backup`) and press Save. Callsheet then reads it: texts you got and sent, group texts, and the words in
  picture messages. Only a new or changed file is read again. There is no default folder: until you save one, no
  backup is read. Clear the field and press Save to stop.
- **As they arrive.** Pick your phone's "Last notification" sensor (from the Home Assistant app on the phone) and
  Callsheet checks it every minute and keeps new texts from Google Messages (Signal and WhatsApp can be turned on
  too). No sensor is picked until you pick one; the list shows the phones your own Home Assistant has. Once the nightly backup has the same text, the backup's copy is used.
- The Sources panel warns when the backup is more than 36 hours old, a file couldn't be read, or the sensor is
  unavailable. Callsheet asks Home Assistant only for that one sensor and its recent history.

## Time format

Times show in 12-hour format by default ("9:30 am", "11 am", "12 pm" for noon, "12 am" for midnight).
Choose 24-hour ("14:30") in settings. The change shows at once; the brief and Chat use the same format.

## The local model

Callsheet's AI runs on a model server on your own network: any OpenAI-compatible server works (llama.cpp's
`llama-server`, Ollama, LM Studio). Chat always goes first; background reading waits while you chat.

1. Start the server so Home Assistant can reach it, for example `http://192.168.1.50:8080/v1`.
2. In **Setup › AI › Your model server**, enter its **Address** (ending in `/v1`) and a key only if the server asks
   for one, then press **Check**. Callsheet asks that server, and only that one, for its model list (`GET /models`,
   8 seconds at most; it doesn't load a model) and shows **Connected** with each model's context size and whether
   it's loaded, or **Can't reach it** with the reason and **Try again**.
3. In **Which model does what**, pick each job's model from the list: **Chat** (the Chat tab and Draft a reply) and
   **Background** (reading each message, the brief, Get to know me and project ideas). **Use one model for
   everything** (on by default) keeps one model loaded so nothing waits while the server swaps; with two different
   models Callsheet warns you and offers to use one for both. Until you pick one, background work uses the first
   model your server lists; with none listed it waits and Setup says **Pick a model**.

When the server sends garbage or an internal error, Callsheet leaves it alone for 15 minutes, then checks it with a
short answer; the jobs list shows "Not working right" with **Check again**. **AI › Advanced** (closed) has the context
size to use when the server doesn't say, **Quiet times for the model server** (times, in your time zone, when
background reading leaves the server alone, for example while it does other work; none unless you add them), Chat's longest answer, time limit, server slot and tool use, web search
limits, and **Claude**: your messages go only to your model server unless you save an Anthropic key there and turn
on **If your model server fails, use Claude** (a saved key also lets Chat use Claude). **Advanced › Writing
instructions and safety rules** has the texts Callsheet's writing follows. Callsheet's own safety rules always come
last, whatever your instructions say.

Upgrading from an older version moves your settings over once: Chat's own server becomes your model server when none
was saved (otherwise Chat uses yours), Chat keeps its model, Claude as the writer becomes the fallback, and presets
are set aside. Setup › AI says once what changed.

### App options

The app's **Configuration** tab in Home Assistant has two optional fields, `writer_local_url` and
`writer_local_model`. When both are filled and the writer was never changed in Callsheet's setup, the app
uses that local model server and model at start-up. Once you change the writer in setup,
setup wins and these options are ignored. Empty fields change nothing.

## Phone notifications (optional)

Setup → Your day → Phone notifications. Install the Home Assistant app on your phone and sign in: Home Assistant then
has a `notify.mobile_app_<phone>` service, and Callsheet lists it. Pick one or more phones, then turn on:

- **At call times**: "Your brief is ready" after each brief time; it opens the brief.
- **Urgent alerts between briefs**: a new clash and an ask from one of your VIPs, as they arrive. Alerts have names,
  sources and times, never message text; at most a few an hour (the rest are grouped into one alert). Quiet hours hold
  them, except for your VIPs (you can change that by asking Chat). A reminder you set ("remind me at noon") comes at
  that time.

A notification shows who wrote and the subject only. Tapping it opens Callsheet in the Home Assistant app.
Home Assistant delivers it through Apple's or Google's push service, so the sender's name and the subject pass
through that service. Turn off **Show the subject in notifications** to send the sender's name only ("New urgent
message from Priya"). Use **Send a test notification** to check; if Home Assistant no longer knows a phone,
open the Home Assistant app on it or pick another phone.

## Know me (optional)

Setup → AI → Memory → **Get to know me** lets Callsheet learn who you are from what it keeps. Its steps are shown at
the top of the page: Sources, Check, Questions, Done.

1. **Sources.** Tick the documents to read besides your messages:
   - **Drive**: in your Google Cloud project turn on the *Google Drive API* and add the
     `https://www.googleapis.com/auth/drive.readonly` scope to the consent screen. Then press "Grant Drive
     access" for each account and paste the address as when you connected it. Docs, Sheets, Slides and text files
     are read; PDFs are skipped.
   - **Obsidian**: sync the vault into Home Assistant's `share` or `media` folder (Syncthing, Samba, …) and type
     its path, for example `/share/Obsidian/My vault`. The app sees these folders read-only.
2. **Check.** Callsheet lists the people you write to (role, projects, area, VIP), your projects, recurring
   commitments and reply habits. Click a field to change it; accept or remove each row.
3. **Questions.** Up to 8 questions about what Callsheet couldn't work out, one at a time: tap an answer, write your
   own, Skip, or "That's enough for now".
4. **Done.** Shows what Callsheet now knows. **See it in Memory** opens Setup's Memory, where everything is in your
   own words; **Open Chat** opens the Chat tab.

Only what you save or answer goes into Memory (`memory.md`). Turning a source off removes what Callsheet read from it.

When you write or edit a note in Memory by hand and it reads like an instruction or a claim copied from a message
("the invoice has been paid"), Callsheet asks "… Save anyway?" first (0.8.2). It's only a warning.

## Draft a reply (0.21.0)

On an open ask (the item view from Today, or **Replies**), press **Draft a reply** (or Chat about this's "Draft a reply
to …"). Your local model writes a suggested reply from the conversation, what is asked and when, your calendar on
those days, any clash with it, your writing instructions and the notes you saved about that person. It runs on
Chat's model, so background reading waits while it writes. You need the local model set up (Setup › AI).

- The reply shows in a box you can edit, with **Copy** and **Write again**. It is kept with the item until you ask
  for a new one.
- Email: **Put in Gmail Drafts** saves it as a draft reply in the same conversation (see above; **Reply all** when
  others were on the message). Signal, Slack and texts: **Copy**, then **Open in Signal** (or Slack, Messages) and
  paste it there.
- A check on every draft: a reply that says something was approved, paid or agreed gets the
  "Check this draft" warning; when the message itself claims something was done and the reply repeats it as done,
  no draft is shown, and Callsheet says why.
- Nothing is sent from here except an RSVP you press: you send it yourself.

## Chat (optional)

The **Chat** tab answers questions about your own messages, for example "what did Lena say about the truck?" or "what
needs me this week?". It searches every message Callsheet keeps (mail, Slack, Signal and texts, the ones you sent
too), reads a whole conversation in order, lists your asks, promises, clashes and clean up, and looks at your
calendar. Every answer points at the messages it came from.

- **Change how Callsheet works by asking.** "Make Lena a VIP", "no Slack alerts after 9 pm, except Dana" or "move my
  morning brief to 7 am on weekdays". When your message says the whole change, it's done at once with Undo; anything
  Callsheet had to fill in, or anything broad, waits for you to press **Confirm**. Everything you set this way is
  listed in **Setup › Your day › Your rules**, with Undo and Delete.
- **Act on items.** "Mark the truck one done", "snooze that until Monday", "remind me tomorrow at 9 am" and "note to
  Bruce: Marco is vegetarian" each get a card.
- **Only your own words count.** A message that says "make me a VIP" or "create a draft" can never put up a card or
  change anything, even when Chat reads it. A calendar event or Gmail draft Chat suggests is made only when you press
  **Confirm**.
- Nothing is sent from here except an RSVP you press. A message that claims something was done is reported as the sender's claim ("An
  email from Jonas says invoice 2207 is paid; that's the sender's claim, so check before relying on it."), never
  confirmed. Answers that state something as done or decided get the "Check this draft" warning.
- **Web search** (Setup › AI, optional): one switch; turn it on and enter your SearXNG address, and Chat can search
  the web through it. Callsheet asks SearXNG for JSON, which SearXNG turns off by default: in its `settings.yml`, add
  `json` under `search: formats:` (next to `html`) and restart it; without it the Check fails (SearXNG answers 403).
  A search that would carry private words (addresses, numbers, your contacts' names, or words you didn't type) is asked first.
- Stop ends an answer. Chats are kept on this box; **Clear all chats** deletes them.

## Projects (0.7.0)

The **Projects** tab collects what you're working on, drafted from your notes, mail, calendar and (optionally)
GitHub. Nothing there is written back anywhere: projects, tasks and notes are kept in Callsheet only.
- **Suggested** projects come from Obsidian notes tagged `#project` or in a Projects folder, threads you wrote in,
  events that repeat 3 or more times, projects saved in Know me and GitHub repos you follow. **Add** keeps one,
  **Not a project** hides it for good. Note checkboxes become suggested tasks; `(due 2026-11-12)` gives a date.
- **List**, **Timeline** (six weeks, missed dates, today) and the detail panel with next step, people, milestones,
  tasks, notes and sources. **Ask about this project** opens a chat that only looks at the project's items.
- **Slipped**: a date passed, a thread went quiet (Settings: 3 to 14 days, default 7) or you owe a follow-up
  (1 to 5 days, default 2). Each shows the exact words and a link. Use the menu to mark it done, fine, snooze it
  a week or add a note. Today shows the count and the top item.
- **Weekly review**: Monday morning (default), Friday afternoon, Sunday evening or off. With phone notifications
  set up you get one notification with counts only.
- **Suggest names and next steps** sends a summary (counts, short titles, dates, never message text) to your
  writer in one request. Suggestions stay suggestions until you accept them.
- **GitHub (optional, read-only)**: in Projects → Settings (or Setup → Memory → Projects and GitHub) paste a
  fine-grained token with read-only Contents, Issues and Pull requests access and pick repos. Callsheet only
  reads (`GET`) repos, commits, issues and pull requests; the token is never shown again. **Forget GitHub**
  deletes the token and everything read from it.

## People (0.8.2)

Callsheet knows that two addresses, a Slack user and a Signal contact can be one person. Open **Setup → People**.

- **Same person?** shows two people and why Callsheet thinks they're one ("Their Slack profile email is …", "The
  signature in mail from … lists …"). Answer **Same person**, **Different people** (never asked again) or **Later**
  (asked again in a week). The same card shows on Get to know me and in a reply's panel when the sender is a likely
  match. Nothing is merged until you answer, and shared addresses (info@, team@) and first names alone are never
  suggested.
- **You** lists your own addresses: only your connected accounts add to it. **Read my Gmail "send as" addresses**
  adds Gmail's own list (read-only). **Names people use for you** marks mail that uses your name from an address
  that isn't yours. Nothing a message says can add an address to you.
- **Everyone**: search by name or address, turn **VIP** on or off for a person (all their addresses, Slack and Signal
  count), and **Split** a merge. **History** lists merges and splits. Merging into a VIP asks once more, with the
  evidence, before it merges; Split puts both people back exactly as they were.

## Voice assistant API (0.21.0, optional)

A read-only API so your phone voice assistant can search your messages, hear what needs you, look up the calendar,
find how you usually reach someone and read you today's morning brief. It is **off** until you switch it on in
**Setup → Connections → Voice assistant**. Read-only. Reached over your home network or a VPN; never expose it
to the internet.

1. In Home Assistant, open Callsheet › Configuration › Network and give `8100/tcp` a host port (it is empty, so not
   exposed, by default).
2. In Callsheet, switch Voice assistant on, then **Add a device**: give it a name and tick what it may read. Copy the
   token (or scan the QR code) into the assistant's settings right away: it is shown only once.
3. The assistant calls `http://<Home Assistant's address or VPN name>:<host port>/v1/...` with
   `Authorization: Bearer <token>`. Addresses outside your home network or VPN (private ranges and 100.64.0.0/10) are
   refused; if you use a host name that isn't an IP address, a one-word name or a VPN/`.local` name, add it under "Host names and recent
   calls".

Nothing can be changed or sent through it: only reads, 60 a minute per device. **Revoke** stops a device at once.
"Recent calls" shows when each device called and how many results it got (never what it searched for). The full
contract for the assistant's developer is `docs/voice-api.md` in the repository.

## Connectors (0.10.0, optional)

Public YouTube channels and news feeds you choose. Open **Setup → Connections → Connectors**.

- **Add a YouTube channel**: paste the channel's address (`youtube.com/@kurzgesagt`) or its @handle and press
  **Check**. Callsheet reads the channel's page once to find its id, then the channel's public feed, and shows the
  channel's name and three latest titles with "Callsheet will read www.youtube.com every 2 hours. Nothing about you is
  sent." Press **Add channel** to start. **Skip Shorts** (in the channel's **Advanced**, off by default) leaves out
  videos YouTube marks as Shorts in its feed.
- **Add a news feed**: paste a feed address, or a site's address and Callsheet looks for the feed the site announces.
  When a site's feed lives on another site, Callsheet tells you and checks that one only when you press it. News
  feeds are read every hour; story links open only on the feed's own site.
- Each connector has **Pause**, **Remove** and a closed **Advanced** part (how often it is read). **Places Callsheet
  contacts** lists every site connectors read, with the date you added it; removing a connector (or a place) stops
  the reading and deletes what was read from it.
- **What to watch for**: topics, each **Always tell me**, **If it's big** or **Never**, kept for the coming rundown.
- Today's **From your channels, feeds and reports** (below Coming up) lists the newest videos and stories. Feed text
  is someone else's words: links inside it are shown without being links, and an item that reads like instructions
  to an assistant is marked and has no link. Chat can find connector items. Details: `docs/connectors.md`.

## Theme

Callsheet has one look. Pick its theme under **Theme** in the header or in Setup › Your day › Appearance and sound:

- **Light** or **Dark** fix it; **Auto** follows your device's light or dark setting.
- The theme is saved on the Callsheet server and on this device (`callsheet.prefs` in the browser's storage). If you
  picked another look before (Easy read, Bold, Clean or one of the others), you now see the one look, with your
  Light, Dark or Auto kept.
- The fonts are bundled (latin letters only).

## Make it yours

Callsheet starts with plain defaults; change any of these whenever you like.

- **The assistant's name**: Bruce unless you change it (Setup › Your day › You, or say "call yourself Max" in Chat).
  Chat, Today and the brief use it. An install from before this setting keeps the name it had.
- **Your name**: Setup › Your day › You. The brief and Chat call you by it.
- **Time zone**: Setup › Your day › You (your browser's, until you pick one). Brief times, quiet hours and quiet
  times follow it.
- **Areas**: Setup › Connections. Each account belongs to an area (Personal, or an employer), and Today groups by it.
- **VIPs**: Setup › Your day › VIPs and replies, or in Chat ("make Dana a VIP"). Their asks go to the top and can alert you.
- **Quiet hours** for phone alerts: Setup › Your day › Phone, or in Chat ("no alerts after 9 pm until 7 am").
- **Brief times**: Setup › Your day › Brief times, or in Chat ("move my morning brief to 7 am").
- **Your model server and its models**: Setup › AI (see "The local model").
- **Quiet times for the model server**: Setup › AI › Advanced, or in Chat ("keep the model server quiet from 1 am to
  4 am"). None on a new install.
- **Extra request fields for a model**: Setup › AI › Advanced › Model server. A small JSON object, like
  `{"top_k": 20}`, added to every background request for the background model, for models that need one. Empty by
  default; only number fields from the list shown there are allowed.

A change you ask for in Chat shows a card. When your own message says exactly that change, it's applied at once with
**Undo**; otherwise it waits for your **Confirm**.

## Setup and finding things

- **Setup** has four places: **Connections** (Sources, accounts and areas, Slack, Signal, texts, connectors, the
  voice assistant), **Your day** (name, time zone, brief times and their days, phone alerts, VIPs, your rules, look),
  **AI** (your model server, which model does what, web search, Memory, Get to know me, People, writing style, and
  its own Advanced: context size, quiet times for the model server, Chat's limits, Claude as a fallback) and **Advanced** (closed until you open a part:
  Google sign-in key, alert limits, writing instructions and safety rules, memory file, message history, Signal
  history import, visuals, old data from earlier versions, Start over). Setup's home page sums
  up each place; **Find a setting** finds any of them.
- **Find or ask**: press **Ctrl+K**, **⌘K** or **/** (or **Find** in the header) and type a name, a setting or a
  question. **Enter** opens the first result; "Ask: …" sends the text to Chat. It only reads, copies and opens.
- Small **?** buttons show the longer help; Callsheet remembers on this device whether each one is open.

## Your data

- Everything is stored on this Home Assistant machine in the app's data folder, readable only by the app:
  `callsheet.db` (your settings, areas, VIPs, the Google client secret, the account sign-in tokens, the ledger and
  your rules) and `messages.db` (the message store: the text of every message Callsheet reads, received and sent,
  your calendar events, and the Drive files and notes you ticked in Know me). Nothing is deleted from the store.
- Home Assistant backups of this app include both files, so treat backups as private.
- The only outbound traffic is to Google (sign-in, Gmail, Calendar, Drive and Contacts, read-only, plus the two
  creates you press: one calendar event, one Gmail draft), Slack's read-only Web API (`slack.com`) when you connect a
  workspace, Signal's servers (to receive), your local model server, your SearXNG if you set up web search, the
  feeds you add, and `api.anthropic.com` only if you save a Claude key. Message text goes only to the model you use.
- An older `index.db` from before 0.21.0 is read once into the store and then left on disk, unused, until you clear it.
- **Old data from earlier versions** (Setup › Advanced): what the features that are gone left behind, in groups: the
  AI sorting's labels and corrections, what it learned, the reader, the "What you should know" picker, the writer
  evaluation and its saved runs, the old brief's caches, settings nothing uses any more (presets, Chat's own server,
  "needs a reply after N days", the sorter's and reader's modes), the copy of your AI settings from before 0.21.0,
  `index.db` (offered only once both of its one-time imports have finished; the counts copied are shown), and this
  browser's own leftovers. Each group shows exactly what it holds, with counts and sizes, and has a tick; nothing is
  ticked at first. **Clear** asks first ("Clear N items, X MB? This can't be undone."), then removes only what you
  ticked and frees the space. It never runs on its own, at an upgrade or from Chat, and it never touches your
  messages, the ledger, people, contacts, calendar adds, drafts, chats, memory notes, Know me answers, connectors,
  reports, sign-in tokens or current settings. With nothing left it says "Nothing left from earlier versions."
- The voice assistant API (off by default) gives devices you made a token for read-only access to the store, the
  calendar, people and today's brief, over your home network or a VPN only. Its call log keeps counts, never
  words, for 90 days.

## Troubleshooting

- The page is blank or unstyled: restart the app and reload the page. Check the **Log** tab.
- "Callsheet was updated. Reload to see the new version.": the page was opened before an update. Click
  **Reload**. The version that runs is shown at the bottom of Setup.
- "That sign-in code was already used or expired": start the connection again and paste the new address.
- "Tick both Gmail and Calendar on Google's consent screen": start again and tick both boxes.
- An account says it needs to be connected again: Google ended the sign-in (password change, access
  removed, or the client still in Testing mode). Connect it again.
- "Google said no access": check that both APIs are enabled in your Google Cloud project (step 2).
- "The local model ran out of room; try again": the answer didn't fit in its max tokens (usually with
  **Thinking** on). Turn Thinking off, or raise **Max tokens** under Advanced.
- "The local model server was still loading the model": it was still loading after the retry. Try
  again in a minute.
- "The local model isn't healthy right now": the model server sent garbage or an internal error. Callsheet leaves it
  alone for 15 minutes, then checks it with a short answer. Restart the model server if it keeps happening.
- "Couldn't reach the local model": check the Server URL and that the server is running; **Test
  connection** should list your model.
- "The Anthropic API key was refused": create a new key and save it again.
- The app only answers the Home Assistant ingress proxy. Its only other port is the voice assistant API, and only
  when you give it a host port.

