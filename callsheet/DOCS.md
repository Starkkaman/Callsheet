# Callsheet

Callsheet puts your **Daily Brief** in the Home Assistant sidebar: today's schedule (with clashes and
invitations you haven't answered), mail that needs a reply, heads-ups, and progress per project,
grouped by area (Personal and one per employer).

Callsheet only reads. It never sends, replies, deletes or changes anything in your accounts.

## What it does today (0.3.4)

- Opens as a sidebar page through Ingress (**Open Web UI**, or turn on **Show in sidebar**).
- Connects your own Google accounts (Gmail and Google Calendar), read-only, through a Google Cloud
  OAuth client that you create yourself. Each account belongs to an area.
- Builds the brief with plain rules:
  - **Schedule**: today's events from the calendars you pick. Free, cancelled and declined events are
    left out. Invitations you haven't answered and overlapping events are flagged. All-day events go to
    Heads up.
  - **Needs a reply**: inbox threads from the last 14 days where the last message is not from you and it
    comes from one of your VIPs, or is addressed to you and asks a question (you in To), mentions a
    deadline or has waited longer than your reply age (2 days by default; you in To or Cc). Newsletters,
    mailing lists, auto-replies and no-reply senders are left out unless the sender is a VIP.
  - **Heads up**: all-day events, and accounts Callsheet couldn't read.
  - **Projects** still shows sample data.
- Reads your accounts at the morning brief and check-in times (06:30, 12:30 and 17:00 by default, in your
  time zone; check-ins are skipped on days off), when the app starts if the last read is older than
  30 minutes, and when you ask for it.
- Optionally an **AI writer** (Claude or a model on your own network) adds a short summary at the top and
  a suggested reply per "Needs a reply" row. The rules still decide what is in the brief.
- Until an account is connected, the page shows sample data with a "Sample data" tag.

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

To connect the same account again (for example after "needs to be connected again"), repeat these
steps; Callsheet updates the existing account. Removing an account also withdraws Callsheet's access at
Google. You can check or remove access any time at <https://myaccount.google.com/permissions>.

## AI writer (optional)

Off by default: the brief then looks exactly as in 0.2.0. Turn it on in **Setup → Delivery → Writer**.
After each read (scheduled or **Refresh**) the writer gets the brief's facts (schedule, heads-ups, the
replies due) plus, with **Suggest replies** on, the last up to 3 messages of at most 8 threads that need a
reply (VIPs first, then the oldest). Each message is sent without its quoted history ("On … wrote:" and
the quoted lines below it, Outlook reply headers; forwarded mail is kept whole), signature, "Sent from
my …" footer and long legal disclaimer, and is cut at 4,000 characters. Mail bodies are not stored; only the suggestions are.
It adds:

- a summary band under the date ("Written by Claude Opus 5.5" or "Written by Local · <model>");
- a **Suggested reply** under each reply row it could help with: open it, **Copy**, then paste the text
  into your reply in Gmail. Placeholders in [square brackets] are facts only you know. Nothing is sent.
- The writer is told never to say that something was approved, paid, sent, signed, booked, agreed or
  decided, and to leave a [placeholder] instead. Mail can still talk it into it, so Callsheet checks each
  draft: one that states an approval, payment or agreement as done gets the line "Check this draft: it
  states an approval, payment or agreement you may not have made." above it. The draft is still only a
  draft.

If the writer fails, the brief still shows (rules only) and Heads up says "The AI writer didn't run" and
why. Setup shows the last run with its token counts. Per area, the **AI writer** chip in step 2 decides
whether that area's events and mail go to the writer at all (on by default); with it off, the area's
items still show in the brief, without suggestions.

### Claude

1. Create an API key at <https://console.anthropic.com> (Settings → API keys).
2. Paste it under **Claude**, **Save key**, pick a model and select **Claude (cloud)**.

API use is billed by Anthropic per token, **separately from any Claude subscription** (Pro, Max): a
subscription does not cover it. Prices per million tokens (input / output, USD): Opus 5.5 4 / 20,
Sonnet 5.5 2 / 10, Haiku 5.5 0.10 / 0.50. Callsheet never switches model for you. The key is stored in
`callsheet.db` and only its last 4 characters are ever shown. **Test key** sends a one-line request.

### Local model

Any OpenAI-compatible server on your network works: llama.cpp (`llama-server`), Ollama, LM Studio.

1. Start the server so Home Assistant can reach it, for example `http://192.168.10.157:8080/v1`.
2. Under **Local model**, enter the **Server URL** (ending in `/v1`), the **Model** exactly as the server
   lists it (for example `gemma-4-26b-a4b-it-qat-q4_0`) and an API key only if the server needs one.
3. **Test connection** lists the models the server offers (`GET /models`; it doesn't load a model).
4. Select **Local model**.

Callsheet asks for JSON that matches a schema made for each run (a summary and one suggested reply per
thread it sent), with **thinking off** and room for a 4,096-token answer. **Thinking** (under Local model)
turns the model's thinking on with a budget of 1,536 tokens on top; it is slower, and with Gemma 4 the
answers were not better, so off is recommended. Callsheet reads only the answer; if a thinking model puts
its answer in the thinking part by mistake, Callsheet uses it there. **Advanced** has the answer's max
tokens, the server's context size (32,768 by default; if a big brief would not fit, Callsheet sends fewer
or shorter messages) and the timeout (300 seconds). The first run after the server starts can take about
a minute while the model loads; if the server says it is still loading, Callsheet tries once more 10
seconds later. With **If the local model fails, use Claude instead** on (off by default), a failed local
run is sent to Claude if a Claude key is saved; off keeps your data on your network.

### Writer evaluation (optional, for comparing local models)

Off by default. Under Setup, Delivery, Local model, open **Advanced** and turn on **Show the evaluation
tools**, then press **Writer evaluation**. When the switch is off the page is hidden and Callsheet refuses
its requests.

- **Run evaluation** reads your mail and calendar the same read-only way a refresh does and builds the same
  writer input, but it never changes your brief, the last refresh or the last writer run. It only runs
  when you press the button; it is refused within 10 minutes before or after a brief or check-in, while a
  refresh runs, while another evaluation runs, or when no local model server is set. While it runs,
  **Refresh** is refused (scheduled briefs are kept apart by the 10-minute rule).
- The data goes only to your local model server, one request at a time: **Gemma** with thinking off (as
  the brief uses it), **Qwen** thinking a little (`reasoning_effort` low, 2,048-token budget) and thinking
  more (medium, 4,096), both with temperature 1.0, top_p 0.95 and top_k 20, each with up to 450 seconds, then one tiny request so Gemma is loaded again. If a
  brief comes within 10 minutes before a setup starts, the run stops early. A run can take up to about half
  an hour. Under the page's **Advanced** you can change the model names if your server calls them
  differently.
- When it has finished, **Open** a run: two comparisons (Gemma against Qwen medium, Qwen low against Qwen
  medium) show the summary and each draft as A and B, in a random order. Pick **A is better**, **B is
  better** or **Same** for each and press **Submit choices**; only then does the page show which setup was
  A or B and a table of numbers per setup.
- Results are kept in the app's data folder under `writer-eval/` (the last 5 runs, readable only by the
  app). **Download results** saves a file with the data that was sent, each setup's answer and numbers and
  your choices. It contains your real mail text: keep it private.

### App options

The app's **Configuration** tab in Home Assistant has two optional fields, `writer_local_url` and
`writer_local_model`. When both are filled and the writer was never changed in Callsheet's setup, the app
turns on the local writer with that server and model at start-up. Once you change the writer in setup,
setup wins and these options are ignored. Empty fields change nothing.

## Your data

- Everything is stored on this Home Assistant machine in the app's data folder (`callsheet.db`, readable
  only by the app): your settings, areas, VIPs, the Google client secret, the account sign-in tokens and
  the latest brief. Nothing is sent anywhere else. If you use the writer evaluation, its last 5 runs
  (including the mail text that was sent to your local model) are kept in `writer-eval/` too.
- Home Assistant backups of this app include that file, so treat backups as private.
- The only outbound traffic is to Google's sign-in and read-only Gmail and Calendar APIs, and, when you
  turn on the AI writer, to `api.anthropic.com` (Claude) or the local model server you entered.

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
- "The local model server was still loading the model": it was still loading after the retry. Refresh
  again in a minute.
- "Couldn't reach the local model": check the Server URL and that the server is running; **Test
  connection** should list your model.
- "The Anthropic API key was refused": create a new key and save it again.
- The app only answers the Home Assistant ingress proxy. It has no port of its own on purpose.
