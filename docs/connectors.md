# Connectors (0.10.0, Reports 0.10.2a)

`callsheet/app/connectors.py`, `conn_youtube.py`, `conn_news.py`, `net.py`, `feedxml.py`. Design:
`~/project/daily-brief/wheatley/design.md` (connectors, `net.py`, safety 4.1, stage 0.10.0). Owner decisions
2026-11-04: connectors may read public feeds the owner adds (CLAUDE.md, "Connectors"); the first kinds are YouTube
channels (Kurzgesagt as the example) with a per-channel Skip Shorts (default off) and news feeds; a "What to watch
for" list (Always tell me / If it's big / Never). No AI is involved in 0.10.0.

## The rule

Each host is contacted only after the owner added it: Check reads the address the owner pasted (and, for a YouTube
@handle, the channel page once), Add writes the host to `outbound_hosts` with the date. Removing the connector removes
the host (a host shared by two connectors stays until both are gone). Setup lists the hosts under **Places Callsheet
contacts**. Nothing about the owner is sent; no images, thumbnails or linked pages are ever fetched.

## The flow

1. **Check** (`POST api/connectors/check {kind, address, via?}`): the kind parses the address and reads through
   `Ctx.get` (purpose `check`: a few per host per 15 minutes). The answer is a draft kept in memory for 15 minutes
   under a one-time token: `{stage: "found", token, title, label, latest (3 titles, flagged marked), host, every,
   hostline, note, duplicate, shorts_seen}`, or for a news site whose feed is on another host `{stage: "other_host",
   token, host, feed_url}` (nothing on that host was contacted; the page asks the owner to check it, sending `via`).
2. **Add** (`POST api/connectors {token, label?}`): one transaction writes the connector and its host; the items the
   Check already read are stored (no second request) and the read counts as the host's first poll.
3. **The poller** (thread `callsheet-connectors`, every 60 s): reads each due, unpaused connector whose host is past
   its 15-minute gap; schedule `every_s` x 0.9 to 1.1; conditional GET (ETag, Last-Modified) unless the index has no
   items for it (after a wipe it reads everything again); errors back off 1 h, 3 h, 12 h; Retry-After up to 24 h.
4. **Items** go to index.db with `source` = the kind (`youtube`, `news`), `account` = the connector id, `source_id`
   = `yt:<videoId>` or `news:<sha16>`: the index's own injection check (`instr`) and hidden mark apply, so Chat finds
   them as untrusted data. At most 500 per connector and 30 days. Triage never sorts them.
5. **The Briefing** (`brief.feeds`): the newest items of the last 48 hours from unpaused connectors, at most 10, newest
   first, `{title, label, when, url, flagged, marks}`; absent when the owner has no connectors.

## net.py

GET only, `https://` on port 443, host names only (no IP literals, no local names such as `.local`, `.lan`,
`supervisor`), and the host must be one of the connector's `outbound_hosts`. DNS is resolved once per request; any
answer that is private, loopback, link-local, CGNAT (100.64/10), the Supervisor network (172.30.32/23), multicast,
reserved, documentation, IPv4-mapped/6to4/NAT64 of those, or Teredo refuses the request; the connection goes to the
checked address with SNI and Host set to the name (no second lookup, so DNS rebinding can't swap the address).
TLS 1.2 or later with the default certificate check. Redirects: at most 2, same host only, never to http; a move to
another host is an error ("This feed moved to …. Add it again"). Caps: 2 MB (YouTube's channel page is read up to
1.5 MB and cut: its head is enough), gzip decompression bounded by the cap, connect 5 s, 15 s per request enforced by a
watchdog that shuts the socket (slow-drip servers), two requests at a time. Content types: feeds, XML, HTML and text
only. Sent: `User-Agent: Callsheet/<version> (feed reader)`, `Accept`, `Accept-Encoding: gzip`, `Connection: close`
and the conditional headers; never cookies, auth, referrer or anything about the owner. Logs: host, status, bytes and
counts, never titles or text.

## Feeds

`feedxml.py` uses stdlib `xml.etree` (no defusedxml): a DOCTYPE or ENTITY anywhere refuses the feed before parsing
(XML bombs, external entities), with at most 2,000 elements, depth 32 and 50 entries. Atom, RSS 2.0 and RSS 1.0 are
read; Atom `summary` wins over `content`; YouTube's `media:description` is the summary.

## YouTube

Addresses: `@handle`, `youtube.com/@handle[/videos]`, `/c/name`, `/user/name`, `/channel/UC…`, the feed address, a bare
handle. Videos, Shorts, playlists and youtu.be links are refused with a plain sentence. The channel id comes from the
page head (canonical link, og:url, the page's RSS link, then `itemprop=identifier`); the feed is
`https://www.youtube.com/feeds/videos.xml?channel_id=<id>`, read every 2 hours; when it doesn't answer (403, 404,
410, 5xx or not a feed), the channel's uploads playlist feed `…/feeds/videos.xml?playlist_id=UU<id without UC>` (the same
videos) is tried: on Check at once, by the poller at its next read after the host's 15-minute gap, and the poller keeps
the address that works. When both fail, Setup shows "YouTube isn't offering a feed for this channel right now." with
**Try again** (`POST api/connectors/<id>/read`, one read, refused within the host's 15-minute gap); then 1 h, 3 h, 12 h. Links are built as
`https://www.youtube.com/watch?v=<11-character id>`; the feed's own links are used only to spot Shorts.

**Shorts** count as Shorts when the feed's link for the video is a `/shorts/<id>` address. The feed has no duration,
so if YouTube stops marking them, Shorts show as videos and Skip Shorts skips nothing.

**Checked live on 2026-11-04** (one manual read, outside the tests): the channel page for @kurzgesagt answered 200
(2.7 MB; canonical link, RSS link, og:title and identifier at about 828 KB) and the channel id resolved; the feed
address answered **404** (a Google error page) at that moment. Check therefore degrades: when the page gave the
channel but the feed doesn't answer, the owner sees "YouTube didn't send the channel's feed just now" and can still
add it (Setup then shows the status with Try again). The feed fixture in the tests is synthetic, in the documented
shape.

**Checked live again on 2026-11-05** (one read of each form, outside the tests): both
`?channel_id=UCsXVk37bltHxD1rDPwtNM8Q` and `?playlist_id=UUsXVk37bltHxD1rDPwtNM8Q` answered 200 (`text/xml`, about
57 KB, 15 entries each, the uploads feed titled "Uploads from …"), and 9 of the 15 entries linked to `/shorts/<id>`,
so Skip Shorts works on today's feeds.

## News

A feed address, or a site address: Callsheet reads the page and takes the first feed it announces on the same site
(same host or its `www.` sibling); a feed on another host is offered to the owner (stage `other_host`). One move to
the `www.`/bare sibling is followed on Check; any other move is shown. Story links are kept only when they are https
on the feed's host (or, for a feed the site pointed to, that site), relative links resolved; anything else has no
link. Read every hour by default.

**Links on another host of the same site** (a feed on `feeds.example.com` whose stories are on `www.example.com` or
`news.example.com`): Setup asks once, in the connector's row, "Stories in <name> link to <host>. Show those links?"
(at most 3 hosts). **Show links** writes the host to `outbound_hosts` with the date and `how = 'link'` (listed under
the connector's Advanced, removable; never contacted: net.py reads only the `owner` hosts, and Places Callsheet
contacts lists only those); the next read stores the links. **Keep hidden** records the answer and Callsheet doesn't
ask again. Callsheet never allows a host by itself, and only a host it is asking about can be allowed. "Same site"
is the registrable domain, approximated without the public suffix list (the last two labels, three under a listed
suffix such as `co.uk`, `com.au` or a shared host like `github.io`, `substack.com`), so two sites on one suffix are
never offered. Shown links are checked against the allowed hosts again at display time.

## Text safety

Feed text is someone else's words. inject's rules (link lures included) read each entry's ORIGINAL title and
summary, links intact, before the links are rewritten (`entry_text`); the flag goes with the item to index.db
(`instr`, re-applied on each read), and the page shows the defanged form. Titles and summaries lose control, bidi-override, zero-width and private-use
characters; HTML is read with `htmltext` (hidden text dropped and marked "Had hidden text"); links in the text are
defanged (`shop.example/x (from a feed; not opened)`); titles are capped at 200 characters and summaries at 1,500; a
date in the future becomes "now". `inject.item_instructions` flags items that read like instructions: they show the
mark "Reads like instructions · shown as text" and get no link. The page renders everything with textContent and
checks each link again (`cnLink`). The connector's name is the owner's label.

## Security notes

Reviewed 2026-11-05, item by item in `tests/test_net_security_0100.py` (with `test_connectors_0100.py`):
- **Add-on network.** `config.yaml` has no `host_network` (and no `ports`: Ingress only), so Callsheet runs on Home
  Assistant's internal bridge network (`hassio`, 172.30.32.0/23) and reaches the internet through the host's NAT,
  which also routes to the owner's LAN. The private-range blocking below is what keeps a feed name off the Supervisor
  (172.30.32.2), the other add-ons, the Docker bridges (172.16/12) and the LAN (10/8, 172.16/12, 192.168/16, ULA,
  link-local). If `host_network: true` is ever set, the same blocking also guards the host's own loopback services:
  keep it, and keep this note true (a test reads `config.yaml`).
- **Addresses** (checked after DNS, every answer, every hop): loopback, unspecified, RFC 1918, CGNAT, link-local
  (incl. 169.254.169.254 and fd00:ec2::254), ULA, multicast, broadcast, reserved and documentation ranges, and the
  IPv4-mapped, IPv4-compatible, 6to4 and NAT64 (64:ff9b::/96, 64:ff9b:1::/48) forms of those, plus Teredo. Number
  hosts (decimal, octal, hex, short forms, bracketed IPv6) are refused before DNS.
- **Names** are compared as lowercase IDNA A-labels without the trailing dot, so a homograph is another host. User
  names, backslashes, percent-encoded or full-width dots, ports other than 443, `http:`, whitespace and control
  characters are refused; the fragment is never sent; non-ASCII in the path or query is sent percent-encoded.
- **Transport**: `http.client` only, never a urllib opener, so `HTTP(S)_PROXY`, `ALL_PROXY` and `NO_PROXY` are not
  read; TLS 1.2+, `CERT_REQUIRED` and `check_hostname`; `SSLKEYLOGFILE` ignored; the socket is dialled to the checked
  address (no second lookup) with SNI and Host set to the name; no cookie jar, no Referer, a fixed User-Agent.
- **Time**: the 15 s deadline holds even when a server stalls the TCP connect or the TLS handshake (the watchdog
  shuts the socket being set up and keeps watching until the request ends; fixed in this review, before it a
  deadline that fell inside the handshake left nothing to shut and a header drip could run on).
- **Payloads**: only gzip is asked for and accepted (any other `Content-Encoding` is refused) and it is inflated in
  bounded steps up to the cap; DOCTYPE/ENTITY refused (billion laughs, XXE). Feed text that reaches Chat goes through
  `chat.data_block` (`<` and `>` escaped), so a CDATA `</data>` stays text and can't close the block.
- **Left as is**: Python's IDNA codec is IDNA 2003 (a few characters such as `ß` map differently from browsers);
  the host contacted is always the one listed under Places Callsheet contacts. `SSL_CERT_FILE`/`SSL_CERT_DIR` in
  the image's environment would change the trust store; the Dockerfile sets neither.

## Reports (0.10.2a)

`callsheet/app/conn_reports.py`, released as version 0.10.1. Owner approved 2026-11-05 (CLAUDE.md, "Reports"); design
`~/project/daily-brief/wheatley/design.md` 3.9 plus the test lead's additions of 2026-11-05. Another agent, service or
script writes one short Markdown digest per run into a folder; Callsheet reads the new files. A file hand-off: no
endpoint, no MQTT, no inbound connection and no outbound host. Callsheet never moves, changes or deletes a file.

**One folder = one source.** A Reports connector is one folder under `/share` or `/media` (Home Assistant's
read-only mappings), never `/share` or `/media` itself. Who a report is from comes from the folder: the owner names the
source in Setup, the Briefing and Chat use that name, and the file's `from:` is display text only. When it differs
from the source's name it shows as the mark "Says it's from …" (a file saying `from: Isaac` is never trusted).
Isaac's setup: the NAS exports one dataset, Home Assistant mounts it under `/share/reports`, and each producer gets
its own subfolder (`/share/reports/security-audit`).

**The format.** One file per report, named `<YYYY-MM-DD>-<topic>.md`, written under a temporary name (`*.tmp`) and then
renamed:
```
---
from: Security audit
title: Nightly audit, Nov 5
importance: normal
expires: 2026-11-07
---
2 findings, both low. 14 devices checked, all patched. (Plain Markdown, under about 300 words.)
```
- The header is read by hand, not as YAML: `key: value` lines only, `# comments`, quotes around a value allowed. No YAML
  features are honoured (anchors `&a`, aliases `*a`, tags `!!str`, lists, blocks, `{…}`): they stay plain text, so they
  never make a valid importance or date. A field given twice makes the file unreadable ("The header has title
  twice."): a file can't say two things. Unknown fields are ignored. At most 20 header lines of at most 1,000
  characters; `title` is required (cut at 200 characters), `from` is cut at 60.
- `importance`: `low`, `normal` (default) or `urgent`, a hint only (below). `expires`: a date; the report leaves the
  Briefing after that day (UTC). Default 3 days after the file's time, never more than 14 days; a date in the past
  means it isn't shown (it stays in Chat's index). Any other value makes the file unreadable.
- The body: read up to 4,000 characters, the rest cut with `[cut: Callsheet reads the first 4,000 characters]`.
- Unreadable files (no date in the name, no header, a bad value, not UTF-8, too big, a link…) are listed in Setup as
  "Couldn't read" with the file name (control characters shown as `?`) and a plain reason, never their text.

**The instruction for the other agent** (Setup shows it filled in with the folder and the source's name, with Copy;
it works as a Claude skill, a routine prompt or a system prompt line). It asks for a short digest, not the full
report:
```
After each run, write a short digest for Callsheet as one new file in <folder> named <YYYY-MM-DD>-<topic>.md (write it under a name ending in .tmp first, then rename it). Start the file with this header:
---
from: <name>
title: <one line>
importance: normal
expires: <YYYY-MM-DD, optional, at most 14 days ahead>
---
importance is low, normal or urgent. Then, in under about 300 words, plain facts first: the counts and the top items. Don't put passwords, keys, tokens, credentials, IP addresses or exploit details in it. Keep the full report where you keep it now; you may name it as plain text ("Full report: <name>"), not as a link.
```

**Reading** (every 15 minutes, `Reports.read`, and right after Add):
- The folder is opened once as a directory descriptor without following links (`O_DIRECTORY | O_NOFOLLOW`) after its
  real path was checked to be the folder the owner picked (a folder that became a link, or moved, is an error in
  Setup). Only names directly in it are looked at (no subfolders), at most 2,000 entries.
- Files: regular files only (`lstat`, then `open(O_NOFOLLOW | O_NONBLOCK)` relative to the folder and `fstat`: the same
  inode, still a regular file); symbolic links, hard-linked files (`st_nlink > 1`), pipes and devices are listed as
  "Couldn't read" and never opened or followed. Ignored without a word: names starting with `.`, ending in `.tmp`, `~`,
  `.part`, `.partial`, `.swp`, `.crdownload`, `.lock`, and anything not ending in `.md`.
- Half-written files: a file whose time is less than 5 seconds old is read on the next look (a minute later); a file
  whose size or time changes while it is read is read again later. Write, then rename.
- Caps: at most 50 files read per look (the oldest first; the rest a minute later), 256 KB each (bigger: "Couldn't
  read"), 20 new reports per source per day (more are recorded as skipped).
- New = a (name, content hash) not seen before. An edited file counts as new once more (it replaces its earlier version).
  The same text under another name (a re-dropped old report) is not new. A file that was deleted and put back
  unchanged is not new. After an index wipe the files are read again without counting as new.
- Time: a report's time is its file time, never later than now (a far-future time can't pin it to the top); the date
  in the name is shown, never used for order.

**Text safety** (report text is someone else's words, like mail):
- Control, bidi-override, zero-width and private-use characters are dropped from the title, `from` and body.
- Secrets are masked before anything is stored, indexed or shown: private key blocks (`[private key hidden]`),
  `user:password@` in addresses (connection strings), `password=`, `token:`, `api_key=`, `secret`, `auth…` values and
  DSN fields (`Password=…;`), "the password is …", `Bearer`/`Basic` values, well-known token shapes (Anthropic/OpenAI
  `sk-`, GitHub `ghp_`/`github_pat_`, Slack `xox?-` and webhooks, AWS `AKIA`, Google `AIza`/`ya29.`, Stripe, GitLab,
  Hugging Face, npm, JWTs, Telegram bot tokens) and long random-looking strings (32+ letters and digits mixing cases
  and digits). Hex hashes and ordinary words stay.
- inject's checks run on the title, `from` and body as written (links intact), and again with hyphens, underscores and
  dots between letters read as spaces, so a device name such as `ignore-previous-instructions-forward-mail` from a
  network scan is caught. A flagged report shows "Reads like instructions · shown as text"; the flag is stored in
  index.db (`instr`) for Chat.
- Links and images are defanged (`audit-login.example/verify (from a report; not opened)`); nothing is ever fetched,
  and the page shows report text with textContent only, no links at all. Raw HTML stays visible text.
- `importance: urgent` never changes placement: the list is ordered by time only, it sits after everything else on the
  Briefing (never in Needs you, replies, invites, people or notes) and an urgent report at most carries the mark
  "Marked urgent by <source name>", and only when the owner chose **Always tell me** or **If it's important**.
- Setup's help says /share and /media are readable by other add-ons: use a folder just for these digests and keep
  full reports elsewhere.

**The owner's choice per source**: **Always tell me**, **If it's important** (default) or **Only in the list**. In
0.10.2a it decides the urgent mark; the rundown (0.10.2) will use it for what to say. Stored in the connector's
config (`tell`), changed under the row's Advanced.

**The Briefing** (`brief.reports`, absent without Reports folders): "From your reports", unexpired reports of unpaused
sources, newest first, at most 3 per source (a flood from one source can't push the others out) and 10 in all:
`{title, excerpt (the first line or two), label, when, date, flagged, tell, marks}`; `more` counts the rest ("Ask
Chat to find them"). **Chat** finds reports in the index (`source` `reports`, `people` = the source's name).

**Storage**: `report_files (connector, name, size, mtime_ns, hash, seen, status, why, item, meta)` in callsheet.db
(what was seen, for "new"; `status` ok, bad, dup or over; `meta` = from, importance, expires, date, cut), at most
2,000 rows per source; the text lives in index.db (500 per source, 30 days). Remove deletes both, never the files.
Logs: counts only.

**Not in 0.10.2a**: the Google Drive folder option (design 3.9) and the email fallback. Drive would reuse the
`drive.readonly` access but needs its own folder picker and per-account token handling; it is left for a later step.

**W-RP** (`tests/test_reports_0102a.py`): planted instructions in the body and the header, link lures, device-name
injection, a fake `from: Isaac` and other owner claims (10/10 marked, never trusted), urgent-flag abuse (20/20 never
above Needs you), path tricks (`..`, links in and out, hard links, pipes, subfolders, non-`.md`, temp and dot files,
names with control characters, the folder swapped for a link), huge and many files, half-written and changing files,
malformed headers and a fuzz set of 240 malformed files, secret masking (22 patterns, 0 unmasked), expiry, replays
and time spoofing, and read-only (the folder tree is snapshotted around every read: 0 writes) with the network code
replaced by a trap (0 fetches).

## What to watch for

`interests` (text up to 80 characters, level `always`, `big` or `never`, up to 20). Links are dropped from a topic and
a topic that reads like an instruction is refused. 0.10.0 only stores the list (the rundown uses it from 0.10.2).

## From Chat (0.11.1)

Owner decisions 2026-11-06 (CLAUDE.md, "Chat may help set up connectors"). `chat.connect_intent` is a fixed gate on the
owner's own message ("add / follow / subscribe / hook up / connect / track / set up …" with a channel, feed, site,
reports folder or an address; a bare address right after such a message continues it). Text in quotes or after a `>`
line is someone else's and doesn't count. Only in those turns is `propose_connector {kind, address?, name?, tell?}`
offered to the model; anywhere else a call is ignored and counted in the log.

The card is built by Callsheet (`chat.build_card`), never from the model's text: the address is one the owner wrote
(the model only picks among them), a name only when all its words are the owner's, `tell` only from the fixed list.
`chat.card_address` applies the rules before anything is read (parsing only): https only, no user@host, port 443 only,
no number addresses, no home-network names, no hidden or bidi characters, no link shorteners (refused, never
followed), YouTube cards take only YouTube addresses, reports folders only under /share or /media. The host is shown
as its IDNA A-label (the form Check contacts) plus the Unicode form when they differ, and "new site" when it isn't in
`outbound_hosts`. At most one card per turn.

Check and Add are the endpoints below with `chat: {chat, turn, token}`: a one-time token bound to the chat and turn,
held in memory (never in the chat file), 30 minutes. Check checks the token, refuses the card's address rules again,
then runs Setup's check (`net.py` after DNS: a private address answers `stage: "refused", why: "home"`). Add accepts
only a draft that came from that card's own Check and uses the token up; the approval record is the same
`outbound_hosts` row as Setup. Remove (Undo) marks the card undone. Memory-note tokens and card tokens can't stand in
for each other. `chat.py` fetches nothing; the glue lives in `api.py`. Tests: `tests/test_chat_setup_0111.py`.

When to tell you (`always | important | list`, default `important`) is stored in each connector's config for every
kind; today only the Briefing list uses it (Always tell me first).

## API

`GET api/connectors` (connectors, hosts with dates, interests, choices) · `POST api/connectors/check` ·
`POST api/connectors` · `PATCH api/connectors/<id> {label | paused | every_s | skip_shorts}` ·
`DELETE api/connectors/<id>` · `DELETE api/connectors/hosts/<host>` (every connector on that host) ·
`POST api/connectors/<id>/read` (Try again) · `POST api/connectors/<id>/links {host, allow}` ·
`DELETE api/connectors/<id>/links/<host>` ·
`POST api/connectors/interests` · `PATCH|DELETE api/connectors/interests/<id>` ·
0.10.2a: `POST api/connectors/reports/folders {path}` (the folder picker: subfolder names, no file opened),
`POST api/connectors/check {kind: "reports", address: <folder>}`, `POST api/connectors {token, label?, tell}`,
`PATCH api/connectors/<id> {tell}`. 0.11.1: `tell` for every kind; `chat: {chat, turn, token}` on check and add from a
Chat card.

## Storage

callsheet.db (additive): `connectors (id, kind, label, config, every_s, added, paused, state)`,
`outbound_hosts (host, connector, added, how)` (`how`: `owner` = read, `link` = story links only), `interests (id, text, level, added)`, `report_files` (0.10.2a). Items live in index.db.

## A new kind

Needs the owner's approval in CLAUDE.md first, a class with `kind, noun, item_kind, every_s, feed_cap, check(),
feed_url(), items()`, its entry in `connectors.kinds()`, and its own injection test. A local kind (0.10.2a reports)
sets `local = True` and has `key(cfg)`, `hostline()`, `read(conns, row)`, `public()`, `forget()`, `brief_list()` and
its own `schema`; it writes no `outbound_hosts` and never goes through net.py.
