# 0.5.1: Signal

Callsheet reads Signal as a **linked device** (like Signal Desktop) through
[signal-cli](https://github.com/AsamK/signal-cli). Signal messages become reply rows like Slack's: direct chats
where the other person spoke last, and group chats where you were mentioned or quoted. Callsheet never sends a
Signal message, reaction, read receipt or typing note.

## For the owner

### Link Signal
1. Open Callsheet → Setup → Areas. Under **Signal**, press **Link Signal**. A QR code appears (it is valid for
   about 5 minutes; press Link Signal again for a new one).
2. On your phone, open Signal → **Settings → Linked devices** and tap **+** (Link a new device).
3. Scan the code. The phone lists the new device as **Callsheet**. The Signal section then shows your number
   (masked, e.g. `+•••••••••67`), when it was linked and the time of the last message received.

Only messages that arrive after linking are received. Older messages can be imported (below).

Keep it working:
- Signal removes a linked device that hasn't been online for **45 days**: keep the app running.
- Update Callsheet **at least every 2 months**: Signal stops accepting old signal-cli versions.
- If the phone removes the device, the Signal section says so; link again.

### Choose what shows up
- **Signal rows go to**: the area whose brief gets the Signal rows (Personal by default). That area must read chat.
- **Ignore chats**: tap a chat to stop its rows (and its Heads up line). Tap again to undo.
- VIPs work by name, the "doesn't need a reply" menu works per chat and sender, and Done hides a row until a newer
  message arrives, as for Slack.

### Unlink
Setup → Areas → Signal → **Unlink**, then confirm. Callsheet stops signal-cli, deletes its Signal keys
(`/data/signal-cli`) and every message it received live. Imported history stays until you press **Delete
imported history**. On your phone, also remove "Callsheet" under Settings → Linked devices.

### Import older messages (Mac)
Signal Desktop can export your chats as text:
1. Open Signal Desktop on the Mac (it must be linked and up to date).
2. Signal → **Settings → Chats → Export chat history**, then **Export**. Leave **Include media** off (Callsheet
   keeps only text), press **Continue** and choose a folder (macOS may ask for permission).
3. When it says "Export complete", press **Show in Finder**. The folder holds `main.jsonl`.
4. In Callsheet: Setup → Areas → Signal → **Older messages**, choose `main.jsonl` as the **Export file** (up to
   200 MB) and press **Upload**. The file is uploaded in pieces and read as it arrives.
5. Tick the chats you want and press **Import N chats** (the button shows the number). **Discard** drops the rest.

`main.jsonl` also contains your Signal keys and phone numbers: Callsheet skips them while reading and never keeps
them. Delete the export folder when you are done.

The [signal-export](https://github.com/carderne/signal-export) tool writes one folder per chat with a
`data.json`; choose that file and type the **Chat name** (its folder name) before uploading.

Imported messages are history only: they never become reply rows and are not removed after 30 days. They are
marked imported and stay until you delete them.

## How it works

### Packaging
`callsheet/Dockerfile` installs from Alpine 3.24's community repository (x86_64 and aarch64):

```dockerfile
ENV JAVA_OPTS="-Xmx256m -XX:+UseSerialGC"
RUN grep -q '/community$' /etc/apk/repositories \
        || sed -n 's#/main$#/community#p' /etc/apk/repositories >> /etc/apk/repositories \
    && apk add --no-cache tzdata openjdk25-jre-headless 'signal-cli>=0.14.9' 'java-libsignal-client>=0.103.0' \
    && test -f /usr/lib/libsignal_jni.so && signal-cli --version \
    && pip install --no-cache-dir anthropic==2.3.0
```

The build stops if libsignal's JNI library is missing or signal-cli can't start. `config.yaml` lists
`signal-cli` and `signal-cli/**` under `backup_exclude`, so Home Assistant backups never hold the Signal keys (after
a restore, link again). `CALLSHEET_SIGNAL_CLI` overrides the binary (the tests use `tests/fake_signal_cli.py`).

### The process (`app/signalcli.py`)
One long-running process, JSON-RPC over stdin/stdout, multi-account mode (no `-a`):

```
signal-cli --config /data/signal-cli --trust-new-identities=always --disable-send-log --scrub-log \
  jsonRpc --receive-mode=manual --ignore-attachments --ignore-stories --ignore-avatars --ignore-stickers
```

- `/data/signal-cli` is created 0700. The process gets its own session; stop = close stdin, wait 10 s, terminate,
  kill. A crash restarts it with backoff (5 s up to 10 min; reset after 10 minutes of running).
- **Allow-list**: `startLink`, `finishLink`, `subscribeReceive`, `unsubscribeReceive`, `listAccounts`,
  `listContacts`, `listGroups`. `Rpc.call` refuses anything else before a byte is written, so no send, receipt,
  typing, profile or group change can come from Callsheet.
- signal-cli's stderr is kept as at most 20 lines with numbers and UUIDs scrubbed; an authorization failure (the
  device was removed) marks Signal as unlinked.

Linking: `startLink` → `deviceLinkUri` (must start with `sgnl://linkdevice?`) → QR as a server-side SVG
(`app/qr.py` with the vendored Nayuki `qrcodegen.py` v1.8.0, MIT, unchanged; one path of 1×1 squares; the link
text is never in the page) → `finishLink {deviceLinkUri, deviceName: "Callsheet"}` in a thread, waiting up to
5 minutes → `listAccounts` / `subscribeReceive`. A timeout shows "The code expired".

### What is stored (`callsheet.db`, 0600)
- Meta: linked yes/no, the masked number, linked at, last message at, area, ignored chats, a random salt.
- `signal_chats`: id, kind (dm/group), title, last message time.
- `signal_messages`: chat id, sender id, sender name, time, text (at most 8,000 characters, mentions as
  `@Name`), outgoing, mentions me, quotes me, imported, expires at.
- Ids are salted HMAC-SHA256 hashes (16 hex): direct chats `d…` from the other person's UUID (or number),
  groups `g…` from the group id, senders `s…`; your own messages are `me`. No phone number, UUID, group id or key
  is stored. Attachments and stickers are never downloaded (one without text is kept as an empty message, so the
  chat still knows who spoke last); reactions, stories, calls and view-once messages are not stored.
- Live messages are deleted after **30 days**, disappearing ones when their timer runs out (`signal_prune`, when
  messages arrive, at most hourly, and at each sync); edits replace the text, remote deletes remove the message.

### Rows (`rules.signal_rows`)
- Direct chat: a row when the last message (within 14 days) is not yours. Group: messages after your last one that
  mention you or quote you.
- Reasons: VIP (sender name), Mentioned you, Replied to you, Direct question, Mentions a deadline, Waiting Nh.
- Source "Signal · DM" or "Signal · <group>"; the last 3 messages go to the writer inside `<data>`, with the facts
  (unanswered count, last message) and the wait/claim checks as for Slack.
- Heads up: "Busy Signal group: <title> (N messages in the last day)" for groups with 25+ messages in a day
  (at most 3).

### Import (`app/signal_import.py`)
A streamed line parser (lines up to 4 MB; longer ones are counted as bad):
- `main.jsonl` (Signal's backup format as JSON lines): account (your name), recipients (names: nickname, the
  phone's contact name, profile name, username, else a masked number), chats and `standardMessage` chat items
  with text. Contact ACIs become the same direct-chat ids as live messages. Mentions of someone not in the file
  are you. Disappearing messages are skipped.
- `data.json` (signal-export): `{date, sender, body}` lines; `"Me"` is you; dates without a zone are in your time
  zone; more than one other sender makes it a group.

### API (`app/signal_api.py`)
`GET/PATCH api/signal` (status; `area_id`, `ignored`), `POST api/signal/link`, `api/signal/link/cancel`,
`api/signal/unlink {confirm: true}`, `GET/DELETE api/signal/import`, `POST api/signal/import/start {name, size,
chat}`, `POST api/signal/import/<id>/chunk` (raw bytes, at most 8 MB; the only raw-body route in `server.py`),
`POST api/signal/import/<id>/finish`, `POST api/signal/import/commit {chats}`,
`POST api/signal/history/clear {confirm: true}`.

## Tests
`tests/test_signal.py` (fake signal-cli `tests/fake_signal_cli.py`, synthetic fixtures
`tests/fixtures/signal_main.jsonl` and `signal_export_data.json`): allow-list, link flow, expiry, refusal,
cancel, restart and clean stop, removal on the phone, QR SVG and the qrcodegen hash, envelopes, rows, retention
and unlink, the import parser (no key or number survives), the API end to end, and Dockerfile/config checks.
