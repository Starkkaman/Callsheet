"""0.10.0 YouTube channel connector (owner approved 2026-11-04; docs/connectors.md).

The owner pastes a channel address or @handle. Check reads the channel page once (www.youtube.com, same host) to find
the channel id, then the channel's public feed https://www.youtube.com/feeds/videos.xml?channel_id=<id>. The poller
reads only that feed. No API key, no cookies, no thumbnails or images, nothing about the owner.

Links are built by Callsheet from the 11-character video id (https://www.youtube.com/watch?v=<id>), never copied
from the feed. Shorts: the feed has no duration, so a video counts as a Short when the feed's own link for it is a
/shorts/ address; if YouTube stops marking them, Shorts show as ordinary videos (Skip Shorts then skips nothing).
"""
import re
import urllib.parse

import connectors as C
import feedxml
import net

HOST = "www.youtube.com"
SITE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
HANDLE = re.compile(r"[\w.-]{3,30}")
PAGE_CAP = 1536 * 1024          # the channel page is large; Callsheet reads its first 1.5 MB (the head is enough)
FEED_CAP = 1024 * 1024


MISSING = "YouTube isn't offering a feed for this channel right now."


def feed_of(channel_id):
    return f"https://{HOST}/feeds/videos.xml?channel_id={channel_id}"


def uploads_of(channel_id):
    """The channel's uploads playlist feed (UC… -> UU…): the same videos when the channel feed doesn't answer."""
    return f"https://{HOST}/feeds/videos.xml?playlist_id=UU{channel_id[2:]}"


def watch_link(video_id):
    return f"https://{HOST}/watch?v={video_id}" if VIDEO_ID.fullmatch(video_id or "") else ""


def parse_address(text):
    """("channel", "UC…") or ("page", "/@handle" | "/c/name" | "/user/name"); InputError for anything else."""
    t = str(text or "").strip()
    if t.startswith("@"):
        if not HANDLE.fullmatch(t[1:]):
            raise C.InputError("That doesn't look like a YouTube handle. It looks like @kurzgesagt.")
        return "page", "/@" + urllib.parse.quote(t[1:])
    if CHANNEL_ID.fullmatch(t):
        return "channel", t
    if "://" not in t:
        if re.match(r"(?:www\.|m\.)?youtube\.com(?:/|$)", t, re.I):
            t = "https://" + t
        elif HANDLE.fullmatch(t) and "." not in t:
            return "page", "/@" + urllib.parse.quote(t)
        else:
            raise C.InputError("Paste a YouTube channel address or @handle, e.g. youtube.com/@kurzgesagt.")
    try:
        p = urllib.parse.urlsplit(t)
    except ValueError:
        raise C.InputError("That isn't a YouTube channel address.") from None
    if p.scheme.lower() not in ("https", "http") or (p.hostname or "").lower() not in SITE_HOSTS:
        raise C.InputError("That isn't a YouTube address. Paste a channel address like youtube.com/@kurzgesagt.")
    if p.port not in (None, 443, 80):
        raise C.InputError("That isn't a YouTube channel address.")
    parts = [urllib.parse.unquote(x) for x in p.path.split("/") if x]
    q = urllib.parse.parse_qs(p.query)
    if parts[:2] == ["feeds", "videos.xml"]:
        cid = (q.get("channel_id") or [""])[0]
        if CHANNEL_ID.fullmatch(cid):
            return "channel", cid
        raise C.InputError("That feed address has no channel id in it.")
    if not parts:
        raise C.InputError("That's YouTube's home page. Paste a channel's address, like youtube.com/@kurzgesagt.")
    if parts[0] == "channel" and len(parts) > 1 and CHANNEL_ID.fullmatch(parts[1]):
        return "channel", parts[1]
    if parts[0].startswith("@") and HANDLE.fullmatch(parts[0][1:]):
        return "page", "/@" + urllib.parse.quote(parts[0][1:])
    if parts[0] in ("c", "user") and len(parts) > 1 and HANDLE.fullmatch(parts[1]):
        return "page", f"/{parts[0]}/" + urllib.parse.quote(parts[1])
    if parts[0] in ("watch", "shorts", "live", "playlist", "embed") or p.hostname.lower() == "youtu.be":
        raise C.InputError("That's a video or playlist. Paste the channel's address (open the channel, then copy "
                           "its address).")
    raise C.InputError("That isn't a YouTube channel address. It looks like youtube.com/@kurzgesagt.")


def channel_from_page(body):
    """(channel id, channel name) from the channel page's head: the canonical link, og:url, the page's own RSS link
    or its identifier meta; ("", "") when none says it."""
    info = feedxml.page_links(body)
    cid = ""
    for href in [info["canonical"], info["og_url"]] + [f[0] for f in info["feeds"]]:
        try:
            p = urllib.parse.urlsplit(href)
        except ValueError:
            continue
        if (p.hostname or "").lower() not in SITE_HOSTS:
            continue
        m = re.fullmatch(r"/channel/(UC[A-Za-z0-9_-]{22})/?", p.path)
        q = urllib.parse.parse_qs(p.query).get("channel_id") or [""]
        if m:
            cid = m.group(1)
        elif p.path == "/feeds/videos.xml" and CHANNEL_ID.fullmatch(q[0]):
            cid = q[0]
        if cid:
            break
    if not cid and CHANNEL_ID.fullmatch(info["identifier"] or ""):
        cid = info["identifier"]
    name = info["og_title"] or re.sub(r"\s*-\s*YouTube\s*$", "", info["title"] or "")
    return cid, C.clean(name, single_line=True)[:200]


def _video_id(e):
    if VIDEO_ID.fullmatch(e.video_id or ""):
        return e.video_id
    m = re.fullmatch(r"yt:video:([A-Za-z0-9_-]{11})", e.id or "")
    return m.group(1) if m else ""


def is_short(e, vid):
    for _rel, href, _type in e.links:
        try:
            p = urllib.parse.urlsplit(href)
        except ValueError:
            continue
        if (p.hostname or "").lower() in SITE_HOSTS and p.path.rstrip("/") == f"/shorts/{vid}":
            return True
    return False


class YouTube:
    kind = "youtube"
    noun = "YouTube channel"
    item_kind = "video"
    every_s = C.DEFAULT_EVERY
    feed_cap = FEED_CAP
    missing = MISSING

    @staticmethod
    def feed_url(cfg):
        return feed_of(cfg["channel_id"])

    @staticmethod
    def feed_urls(cfg):
        """The channel feed, then the uploads playlist feed (read when the first doesn't answer; checked live
        2026-11-05: both answer with the same 15 entries and the same /shorts/ links)."""
        return [feed_of(cfg["channel_id"]), uploads_of(cfg["channel_id"])]

    def items(self, cfg, feed, now, allow_test_names=False):
        out, seen = [], set()
        for e in feed.entries:
            vid = _video_id(e)
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title, summary, hidden, keys = C.entry_text(e.title, e.summary, e.summary_type)
            title = title or "Untitled video"
            out.append({"source": self.kind, "connector_id": cfg.get("id", ""), "id": "yt:" + vid,
                        "kind": "short" if is_short(e, vid) else "video", "title": title,
                        "published": C.when_ok(e.published or e.updated, now),
                        "author": C.clip(C.clean(e.author or feed.title or "", True), 120),
                        "url": watch_link(vid), "summary": summary, "hidden_text": hidden,
                        "flagged": bool(keys), "instr": keys})
        return out

    def check(self, ctx, address, via=None):
        how, val = parse_address(address)
        name, note, page_read = "", None, False
        if how == "page":
            try:
                a = ctx.get(f"https://{HOST}{val}", [HOST], PAGE_CAP, truncate=True)
            except net.Moved as e:
                raise C.InputError(f"YouTube sent Callsheet to {e.host} instead of the channel (often a cookie "
                                   "page). Paste the channel's /channel/UC… address instead.") from None
            except net.NetError as e:
                if e.status == 404:
                    raise C.InputError("YouTube has no channel at that address.") from None
                raise
            page_read = True
            val, name = channel_from_page(a.body)
            if not val:
                raise C.InputError("Callsheet couldn't find the channel id on that page. Paste the channel's "
                                   "/channel/UC… address instead.")
        cid = val
        feed, items, feed_i = None, None, 0
        for feed_i, url in enumerate(self.feed_urls({"channel_id": cid})):   # the channel feed, then uploads
            try:
                feed = feedxml.parse(ctx.get(url, [HOST], FEED_CAP).body)
                break
            except net.RateLimited:
                raise
            except (net.NetError, feedxml.FeedError):
                continue
        if feed is None:
            if not page_read:
                raise C.InputError(MISSING + " Check the address, or try again later.")
            feed_i = 0
            note = MISSING + " You can add it anyway: Setup shows when Callsheet reads it, with Try again."
        latest, shorts = [], 0
        if feed is not None:
            items = self.items({"channel_id": cid}, feed, ctx.c.clock(), ctx.allow_test_names)
            name = name or re.sub(r"^Uploads from\s+", "", C.clean(feed.title, True))
            shorts = sum(1 for it in items if it["kind"] == "short")
            for it in sorted(items, key=lambda x: -x["published"])[:3]:
                latest.append({"title": it["title"], "when": C._iso(it["published"]), "kind": it["kind"],
                               "flagged": it["flagged"]})
        return {"kind": self.kind, "title": C.clip(name, C.TITLE_MAX) or "YouTube channel", "latest": latest,
                "host": HOST, "link_hosts": [HOST], "config": {"channel_id": cid, "feed_url": feed_of(cid),
                                                               "skip_shorts": False},
                "items": items, "note": note, "shorts_seen": shorts, "feed_i": feed_i}
