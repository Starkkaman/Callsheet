"""0.10.0 News feed connector (owner approved 2026-11-04; docs/connectors.md).

The owner pastes a feed address (RSS, Atom or RSS 1.0) or a site address. For a site, Check reads that one page and
looks for the feed it announces (<link rel="alternate">). A feed on the same site (same host or its www. sibling)
is read at once; a feed on another host is shown to the owner first ("This site's feed is on feeds.example.net"),
who checks it as its own place. The poller reads only the feed address, every hour by default.

Story links: an entry's link is shown only when it is https on the feed's own site (or, for a feed the site pointed
to, that site); anything else gets no link. Callsheet never fetches the stories, images or anything else.
"""
import urllib.parse

import connectors as C
import feedxml
import net

FEED_CAP = 2 * 1024 * 1024
PAGE_CAP = 1024 * 1024


def _url(address):
    t = str(address or "").strip()
    if "://" not in t:
        t = "https://" + t
    p = urllib.parse.urlsplit(t)
    if p.scheme.lower() == "http":       # Callsheet reads only https; the same address over https
        t = urllib.parse.urlunsplit(("https",) + tuple(p)[1:])
    return t


def _sibling(a, b):
    return a == b or a == "www." + b or b == "www." + a


def story_href(e):
    """The entry's own link: the alternate (or untyped) link, else an RSS guid that is a web address."""
    for rel, href, _type in e.links:
        if rel in ("", "alternate") and href:
            return href
    if (e.id or "").startswith("https://"):
        return e.id
    return ""


class News:
    kind = "news"
    noun = "News feed"
    item_kind = "article"
    every_s = 3600
    feed_cap = FEED_CAP

    @staticmethod
    def feed_url(cfg):
        return cfg["feed_url"]

    def items(self, cfg, feed, now, allow_test_names=False):
        url = cfg["feed_url"]
        link_hosts = cfg.get("link_hosts") or [net.host_of(url, allow_test_names)]
        out, seen = [], set()
        for e in feed.entries:
            title, summary, hidden, keys = C.entry_text(e.title, e.summary, e.summary_type)
            href = story_href(e)
            key = e.id or href or (title + "\n" + str(e.published))
            iid = C.item_id("news", url, key)
            if iid in seen or not (title or summary):
                continue
            seen.add(iid)
            title = title or C.clip(summary.split("\n")[0], 120)
            link = C.story_link(href, url, link_hosts, allow_test_names)
            out.append({"source": self.kind, "connector_id": cfg.get("id", ""), "id": iid, "kind": "article",
                        "title": title, "published": C.when_ok(e.published or e.updated, now),
                        "author": C.clip(C.clean(e.author or "", True), 120),
                        "url": link, "link_offer": "" if link else C.link_offer(href, url, link_hosts, allow_test_names),
                        "summary": summary, "hidden_text": hidden, "flagged": bool(keys), "instr": keys})
        return out

    def _get(self, ctx, url, cap, hosts):
        """A Check read that follows one move to the www./bare sibling of the address the owner typed."""
        try:
            return ctx.get(url, hosts, cap, truncate=True)
        except net.Moved as e:
            first = net.host_of(url, ctx.allow_test_names)
            if not _sibling(first, e.host):
                raise C.InputError(f"That address moves to {e.host}. If you trust it, paste the {e.host} "
                                   "address instead.") from None
            return ctx.get(e.url, [e.host], cap, truncate=True)

    def check(self, ctx, address, via=None):
        try:
            url = _url(address)
            host = net.split_url(url, ctx.allow_test_names)[0]
        except (ValueError, net.NetError) as e:
            raise C.InputError(str(e) if isinstance(e, net.NetError) else
                               "That isn't a web address Callsheet can read.") from None
        a = self._get(ctx, url, FEED_CAP, [host])
        url = a.url
        host = net.host_of(url, ctx.allow_test_names)
        site = None
        if not feedxml.looks_like_feed(a.body):
            info = feedxml.page_links(a.body)
            if not info["feeds"]:
                raise C.InputError("Callsheet found no news feed on that page. Paste the feed's address (it often "
                                   "ends in /feed, /rss or .xml).")
            site = host
            other = None
            for href, _type, _title in info["feeds"]:
                try:
                    fu = urllib.parse.urljoin(url, href)
                    fh = net.split_url(fu, ctx.allow_test_names)[0]
                except (ValueError, net.NetError):
                    continue
                if _sibling(fh, host):
                    url = fu
                    break
                other = other or (fu, fh)
            else:
                if other is None:
                    raise C.InputError("That page's feed isn't an https address Callsheet can read.")
                return {"stage": "other_host", "kind": self.kind, "host": other[1], "feed_url": other[0],
                        "site_host": site, "title": C.clean(info["title"], True)[:120]}
            a = self._get(ctx, url, FEED_CAP, [net.host_of(url, ctx.allow_test_names)])
            url = a.url
            host = net.host_of(url, ctx.allow_test_names)
        if a.truncated:
            raise C.InputError("That feed is bigger than Callsheet reads (2 MB).")
        try:
            feed = feedxml.parse(a.body)
        except feedxml.FeedError as e:
            raise C.InputError(f"That isn't a feed Callsheet can read: {e}") from None
        link_hosts = [host]
        if via and via.get("stage") == "other_host" and via.get("host") == host and via.get("site_host"):
            link_hosts.append(via["site_host"])        # a site's feed on another host links to the site
        if site and site != host:
            link_hosts.append(site)
        cfg = {"feed_url": url, "link_hosts": link_hosts}
        items = self.items(cfg, feed, ctx.c.clock(), ctx.allow_test_names)
        offers = []
        for it in items:
            if it["link_offer"] and it["link_offer"] not in offers:
                offers.append(it["link_offer"])
        latest = [{"title": it["title"], "when": C._iso(it["published"]), "kind": it["kind"],
                   "flagged": it["flagged"]} for it in sorted(items, key=lambda x: -x["published"])[:3]]
        title = C.title_text(feed.title) or host
        return {"kind": self.kind, "title": title, "latest": latest, "host": host, "link_hosts": link_hosts,
                "config": {"feed_url": url}, "items": items, "link_offer": offers[:C.LINK_OFFERS],
                "note": None if items else
                "The feed is empty right now. Callsheet will show stories when they appear."}
