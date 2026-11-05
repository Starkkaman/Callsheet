# Connectors (0.10.0)

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

## What to watch for

`interests` (text up to 80 characters, level `always`, `big` or `never`, up to 20). Links are dropped from a topic and
a topic that reads like an instruction is refused. 0.10.0 only stores the list (the rundown uses it from 0.10.2).

## API

`GET api/connectors` (connectors, hosts with dates, interests, choices) · `POST api/connectors/check` ·
`POST api/connectors` · `PATCH api/connectors/<id> {label | paused | every_s | skip_shorts}` ·
`DELETE api/connectors/<id>` · `DELETE api/connectors/hosts/<host>` (every connector on that host) ·
`POST api/connectors/<id>/read` (Try again) · `POST api/connectors/<id>/links {host, allow}` ·
`DELETE api/connectors/<id>/links/<host>` ·
`POST api/connectors/interests` · `PATCH|DELETE api/connectors/interests/<id>`.

## Storage

callsheet.db (additive): `connectors (id, kind, label, config, every_s, added, paused, state)`,
`outbound_hosts (host, connector, added, how)` (`how`: `owner` = read, `link` = story links only), `interests (id, text, level, added)`. Items live in index.db.

## A new kind

Needs the owner's approval in CLAUDE.md first, a class with `kind, noun, item_kind, every_s, feed_cap, check(),
feed_url(), items()`, its entry in `connectors.kinds()`, and its own injection test.
