"""0.10.0 connectors: safe RSS/Atom parsing and the HTML page scan (pure: no I/O, never raises on junk).

XML safety (defusedxml isn't allowed, so stdlib `xml.etree` with the dangerous parts refused up front):
- a document holding `<!DOCTYPE` or `<!ENTITY` anywhere is refused before parsing (feeds don't need them; this blocks
  entity expansion bombs and external entities); so is one over MAX_BYTES;
- the text is decoded first (BOM, then the XML declaration's encoding when it's a known one, else UTF-8 with
  replacement), characters XML 1.0 forbids are dropped, and the declaration is removed, so expat only ever sees a
  clean Unicode string;
- parsing is incremental (XMLPullParser) and stops past MAX_ELEMENTS elements or MAX_DEPTH levels.
Formats: Atom 1.0 (YouTube's channel feeds included: yt:videoId, media:group/media:description), RSS 2.0 (and 0.9x),
RSS 1.0 (RDF). Entries past `max_entries` are not read. Text fields come back raw (HTML in summaries is turned into
text by connectors.py with htmltext's hidden-text strip); dates come back as UTC epoch seconds or None.

The page scan (`page_links`) reads only <link>, <meta> and <title> in an HTML page (html.parser) for the YouTube
channel id (canonical, og:url, itemprop=identifier) and for feed autodiscovery (<link rel="alternate"
type="application/rss+xml|atom+xml">).
"""
import datetime as dt
import email.utils
import html.parser
import re
import xml.etree.ElementTree as ET

MAX_BYTES = 2 * 1024 * 1024
MAX_ELEMENTS = 2000
MAX_DEPTH = 32
MAX_ENTRIES = 50
FIELD_MAX = 20000          # raw characters kept per text field (connectors.py clips further)

ATOM = "http://www.w3.org/2005/Atom"
_ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "us-ascii": "ascii", "ascii": "ascii", "iso-8859-1": "latin-1",
              "latin-1": "latin-1", "latin1": "latin-1", "windows-1252": "cp1252", "cp1252": "cp1252",
              "iso-8859-15": "iso8859-15", "utf-16": "utf-16", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be"}
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_DECL = re.compile(r"^\s*<\?xml[^>]{0,300}\?>", re.I)
_DANGER = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.I)


class FeedError(ValueError):
    """Not a feed Callsheet will read (a plain sentence)."""


class Feed:
    def __init__(self, kind):
        self.kind = kind            # atom | rss | rdf
        self.title = ""
        self.links = []             # [(rel, href, type)]
        self.author = ""
        self.yt_channel = ""        # yt:channelId (YouTube feeds)
        self.entries = []
        self.truncated = False      # entries past max_entries were skipped


class Entry:
    def __init__(self):
        self.id = ""
        self.title = ""
        self.links = []             # [(rel, href, type)]
        self.published = None       # epoch seconds (UTC) or None
        self.updated = None
        self.author = ""
        self.summary = ""           # raw (may be HTML)
        self.summary_type = "html"  # html | text | xhtml
        self.video_id = ""          # yt:videoId
        self.categories = []


def decode(body):
    """bytes -> the document as a clean str (FeedError when it's unsafe or not text)."""
    if not isinstance(body, (bytes, bytearray)):
        body = str(body or "").encode("utf-8", "replace")
    if len(body) > MAX_BYTES:
        raise FeedError("The feed is too big.")
    b = bytes(body)
    enc = None
    if b.startswith(b"\xef\xbb\xbf"):
        b, enc = b[3:], "utf-8"
    elif b.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    else:
        m = re.match(rb"\s*<\?xml[^>]{0,300}?encoding\s*=\s*[\"']([A-Za-z0-9._-]{1,40})[\"']", b)
        if m:
            enc = _ENCODINGS.get(m.group(1).decode("ascii").lower())
    text = b.decode(enc or "utf-8", "replace")
    if _DANGER.search(text):
        raise FeedError("The feed has a DOCTYPE or ENTITY part, which Callsheet doesn't read.")
    text = _DECL.sub("", text.lstrip("﻿"), count=1)
    return _BAD_XML_CHARS.sub("", text)


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag):
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _text(el):
    if el is None:
        return ""
    return "".join(el.itertext())[:FIELD_MAX].strip()


def _xhtml(el):
    """An Atom type="xhtml" element back to HTML (local names, only the attributes the hidden-text check reads)."""
    out = []

    def walk(e, depth):
        if depth > MAX_DEPTH:
            return
        name = _local(e.tag)
        attrs = "".join(f' {k}="{_esc(v)}"' for k, v in e.attrib.items()
                        if _local(k) in ("style", "hidden", "class", "id", "bgcolor", "color", "width", "height"))
        out.append(f"<{name}{attrs}>")
        out.append(_esc(e.text or ""))
        for c in e:
            walk(c, depth + 1)
            out.append(_esc(c.tail or ""))
        out.append(f"</{name}>")
    for c in el:
        walk(c, 0)
        out.append(_esc(c.tail or ""))
    return ("".join(out) or _esc(el.text or ""))[:FIELD_MAX]


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def when(value):
    """RFC 3339 / ISO 8601 (Atom, dc:date) or RFC 822 (RSS) -> UTC epoch seconds, or None."""
    v = str(value or "").strip()[:80]
    if not v:
        return None
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?)?\s*"
                       r"(Z|z|[+-]\d{2}:?\d{2})?", v)
    try:
        if iso:
            y, mo, d, hh, mm, ss, tz = iso.groups()
            t = dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
            off = 0
            if tz and tz not in ("Z", "z"):
                sign = 1 if tz[0] == "+" else -1
                digits = tz[1:].replace(":", "")
                off = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
            return t.replace(tzinfo=dt.timezone.utc).timestamp() - off
        t = email.utils.parsedate_to_datetime(v)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def parse(body, max_entries=MAX_ENTRIES):
    """The feed in `body` (bytes) as a Feed; FeedError for anything that isn't a safe RSS/Atom feed."""
    text = decode(body)
    parser = ET.XMLPullParser(events=("start", "end"))
    count, depth, root = 0, 0, None
    try:
        for i in range(0, len(text), 65536):
            parser.feed(text[i:i + 65536])
            for ev, el in parser.read_events():
                if ev == "start":
                    count += 1
                    depth += 1
                    if root is None:
                        root = el
                    if count > MAX_ELEMENTS:
                        raise FeedError("The feed has too many parts.")
                    if depth > MAX_DEPTH:
                        raise FeedError("The feed is nested too deeply.")
                else:
                    depth -= 1
        parser.close()
        for _ev, _el in parser.read_events():
            pass
    except ET.ParseError:
        raise FeedError("That isn't a readable feed (the XML is broken).") from None
    if root is None:
        raise FeedError("That isn't a feed.")
    name = _local(root.tag).lower()
    if name == "feed":
        return _atom(root, max_entries)
    if name == "rss":
        return _rss(root, max_entries)
    if name == "rdf":
        return _rdf(root, max_entries)
    raise FeedError("That isn't an RSS or Atom feed.")


def looks_like_feed(body):
    """A cheap test before parsing: is the first element <rss>, <feed> or <rdf:RDF>?"""
    head = bytes(body[:4096]) if isinstance(body, (bytes, bytearray)) else str(body)[:4096].encode()
    head = re.sub(rb"<\?[^>]*\?>|<!--.*?-->", b"", head, flags=re.S).lstrip(b"\xef\xbb\xbf \t\r\n")
    return bool(re.match(rb"<(?:rss|feed|(?:\w+:)?RDF)\b", head))


def _links(el):
    out = []
    for c in el:
        if _local(c.tag) == "link":
            href = (c.get("href") or "").strip() or _text(c)
            if href:
                out.append(((c.get("rel") or "alternate").strip().lower(), href[:2000],
                            (c.get("type") or "").strip().lower()))
    return out


def _atom(root, max_entries):
    f = Feed("atom")
    for c in root:
        n = _local(c.tag)
        if n == "title":
            f.title = _text(c)
        elif n == "author" and not f.author:
            f.author = _text(next((x for x in c if _local(x.tag) == "name"), None))
        elif n == "channelId":
            f.yt_channel = _text(c)
        elif n == "entry":
            if len(f.entries) >= max_entries:
                f.truncated = True
                continue
            f.entries.append(_atom_entry(c))
    f.links = _links(root)
    return f


def _atom_entry(el):
    e = Entry()
    have_summary = False
    for c in el:
        n = _local(c.tag)
        if n == "id":
            e.id = _text(c)
        elif n == "title":
            e.title = _text(c)
        elif n == "published":
            e.published = when(_text(c))
        elif n == "updated":
            e.updated = when(_text(c))
        elif n == "author" and not e.author:
            e.author = _text(next((x for x in c if _local(x.tag) == "name"), None))
        elif n == "videoId":
            e.video_id = _text(c)
        elif n == "summary" and _ns(c.tag) == ATOM:      # the summary wins over the full content
            _set_summary(e, c)
            have_summary = True
        elif n == "content" and _ns(c.tag) == ATOM and not have_summary:
            _set_summary(e, c)
        elif n == "group":   # media:group (YouTube)
            for g in c:
                if _local(g.tag) == "description" and not e.summary:
                    e.summary, e.summary_type = _text(g), "text"
        elif n == "category":
            term = (c.get("term") or "").strip()
            if term:
                e.categories.append(term[:80])
    e.links = _links(el)
    if e.published is None:
        e.published = e.updated
    return e


def _set_summary(e, c):
    t = (c.get("type") or "text").strip().lower()
    if t == "xhtml":
        e.summary, e.summary_type = _xhtml(c), "html"
    elif t in ("html", "text/html"):
        e.summary, e.summary_type = _text(c), "html"
    else:
        e.summary, e.summary_type = _text(c), "text"


def _rss_item(el):
    e = Entry()
    guid = ""
    for c in el:
        n, ns = _local(c.tag), _ns(c.tag)
        if n == "title" and not e.title:
            e.title = _text(c)
        elif n == "link" and not ns:
            href = _text(c)
            if href:
                e.links.append(("alternate", href[:2000], ""))
        elif n == "guid":
            guid = _text(c)
            if (c.get("isPermaLink") or "true").lower() == "true" and guid.lower().startswith("https://"):
                e.links.append(("guid", guid[:2000], ""))
        elif n in ("pubDate", "date", "published", "issued") and e.published is None:
            e.published = when(_text(c))
        elif n == "updated" and e.updated is None:
            e.updated = when(_text(c))
        elif n == "description" and not ns and not e.summary:
            e.summary, e.summary_type = _text(c), "html"
        elif n == "encoded" and not e.summary:      # content:encoded
            e.summary, e.summary_type = _text(c), "html"
        elif n in ("creator", "author") and not e.author:
            e.author = _text(c)
        elif n == "category":
            v = _text(c)
            if v:
                e.categories.append(v[:80])
    e.id = guid or (e.links[0][1] if e.links else "") or e.title
    if e.published is None:
        e.published = e.updated
    return e


def _rss(root, max_entries):
    f = Feed("rss")
    ch = next((c for c in root if _local(c.tag) == "channel"), None)
    if ch is None:
        raise FeedError("That RSS feed has no channel.")
    for c in ch:
        n, ns = _local(c.tag), _ns(c.tag)
        if n == "title" and not f.title:
            f.title = _text(c)
        elif n == "link" and not ns:
            href = _text(c)
            if href:
                f.links.append(("alternate", href[:2000], ""))
        elif n == "link" and ns == ATOM:
            href = (c.get("href") or "").strip()
            if href:
                f.links.append(((c.get("rel") or "alternate").lower(), href[:2000], (c.get("type") or "").lower()))
        elif n == "item":
            if len(f.entries) >= max_entries:
                f.truncated = True
                continue
            f.entries.append(_rss_item(c))
    return f


def _rdf(root, max_entries):
    f = Feed("rdf")
    for c in root:
        n = _local(c.tag)
        if n == "channel":
            for x in c:
                if _local(x.tag) == "title" and not f.title:
                    f.title = _text(x)
                elif _local(x.tag) == "link":
                    href = _text(x)
                    if href:
                        f.links.append(("alternate", href[:2000], ""))
        elif n == "item":
            if len(f.entries) >= max_entries:
                f.truncated = True
                continue
            f.entries.append(_rss_item(c))
    return f


# --- HTML pages: the head's <link>, <meta> and <title> only ------------------------------------------------------
class _Head(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.metas, self.title, self._in_title, self.n = [], [], "", False, 0

    def handle_starttag(self, tag, attrs):
        self.n += 1
        if self.n > 200000:
            raise _Stop()
        a = {k.lower(): (v or "") for k, v in attrs[:30] if isinstance(k, str)}
        if tag == "link" and len(self.links) < 400:
            self.links.append(a)
        elif tag == "meta" and len(self.metas) < 400:
            self.metas.append(a)
        elif tag == "title" and not self.title:
            self._in_title = True

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 500:
            self.title += data


class _Stop(Exception):
    pass


def page_links(body):
    """What an HTML page says about itself: {"title", "canonical", "og_url", "og_title", "identifier",
    "feeds": [(href, type, title)]} (raw strings; callers check hosts). Never raises."""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body[:MAX_BYTES]).decode("utf-8", "replace")
    else:
        text = str(body or "")[:MAX_BYTES]
    p = _Head()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 (keep what was read)
        pass
    out = {"title": re.sub(r"\s+", " ", p.title).strip()[:300], "canonical": "", "og_url": "", "og_title": "",
           "identifier": "", "feeds": []}
    for a in p.links:
        rel = set(a.get("rel", "").lower().split())
        href = a.get("href", "").strip()[:2000]
        if "canonical" in rel and href and not out["canonical"]:
            out["canonical"] = href
        if "alternate" in rel and href and a.get("type", "").lower().strip() in (
                "application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json"):
            if a.get("type", "").lower().strip() != "application/feed+json" and len(out["feeds"]) < 20:
                out["feeds"].append((href, a.get("type", "").lower().strip(), a.get("title", "")[:200]))
    for a in p.metas:
        prop = (a.get("property") or a.get("name") or "").lower()
        if prop == "og:url" and not out["og_url"]:
            out["og_url"] = a.get("content", "").strip()[:2000]
        elif prop == "og:title" and not out["og_title"]:
            out["og_title"] = a.get("content", "").strip()[:300]
        if (a.get("itemprop") or "").lower() in ("identifier", "channelid") and not out["identifier"]:
            out["identifier"] = a.get("content", "").strip()[:100]
    return out
