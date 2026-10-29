# Callsheet

Callsheet puts your **Daily Brief** in the Home Assistant sidebar: today's schedule (with clashes and
invitations you haven't answered), mail that needs a reply, heads-ups, and progress per project,
grouped by area (Personal and one per employer).

Callsheet only reads. It never sends, replies, deletes or changes anything in your accounts.

## What it does today (0.2.0)

- Opens as a sidebar page through Ingress (**Open Web UI**, or turn on **Show in sidebar**).
- Connects your own Google accounts (Gmail and Google Calendar), read-only, through a Google Cloud
  OAuth client that you create yourself. Each account belongs to an area.
- Builds the brief with plain rules (no AI yet):
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

## Your data

- Everything is stored on this Home Assistant machine in the app's data folder (`callsheet.db`, readable
  only by the app): your settings, areas, VIPs, the Google client secret, the account sign-in tokens and
  the latest brief. Nothing is sent anywhere else.
- Home Assistant backups of this app include that file, so treat backups as private.
- The only outbound traffic is to Google's sign-in and read-only Gmail and Calendar APIs.

## Troubleshooting

- The page is blank or unstyled: restart the app and reload the page. Check the **Log** tab.
- "That sign-in code was already used or expired": start the connection again and paste the new address.
- "Tick both Gmail and Calendar on Google's consent screen": start again and tick both boxes.
- An account says it needs to be connected again: Google ended the sign-in (password change, access
  removed, or the client still in Testing mode). Connect it again.
- "Google said no access": check that both APIs are enabled in your Google Cloud project (step 2).
- The app only answers the Home Assistant ingress proxy. It has no port of its own on purpose.
