"""0.8.2 security: HTML mail and event bodies as the text a mail client would show, nothing more.

Text a reader can't see is a prompt-injection channel (instructions the owner never sees, but the AI reads), so
it is dropped before any message text is used. Standard library only (html.parser).

Dropped: <script>, <style>, <head>, <title> (as before), <template>, <noscript>, <iframe>, comments; and the text
inside elements hidden by the `hidden` attribute or by inline style or simple <style> rules: display:none,
visibility:hidden|collapse (a child may set visible again), opacity at or below 0.1 (multiplied down the tree),
mso-hide:all, a font size of 1.5px or less (inherited; em and % are relative), height/max-height or width/max-width
of 1px or less with overflow hidden, off-screen positions or margins (-500px or further) and text-indent, clip or
clip-path to nothing, transform scale(0) or a far negative translate, text colour transparent or near its
background (contrast below 1.2:1; the background is followed through bgcolor and background colours; a background
image makes it unknown and the colour is then not judged).

<style> limits: only top-level rules whose selectors are a tag, .class, #id or a combination of those (tag.class,
.a.b) are used, in specificity then source order; descendant/child/attribute/pseudo selectors and anything inside
@media or other @-blocks are ignored. Malformed HTML: implied ends for p, li, td/th, tr, dt/dd, option; </body> and
</html> are ignored (browsers keep later text in the body); an unclosed hidden element hides everything after it.
"""
import html.parser
import re

SKIP = ("script", "style", "head", "title")              # never text; not counted as hidden text
DROP = ("template", "noscript", "iframe")                 # text a client doesn't show; counted
BREAK = ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote")
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                  "track", "wbr", "keygen", "frame"))
P_CLOSERS = frozenset(("p", "div", "table", "ul", "ol", "dl", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
                       "pre", "form", "hr", "section", "article", "header", "footer", "nav", "aside", "fieldset",
                       "address", "figure", "main", "menu", "details"))
# start tag -> (open tags it closes, tags that stop the search)
IMPLIED = {"li": (("li",), ("ul", "ol", "menu", "table", "body")),
           "td": (("td", "th"), ("tr", "table", "body")), "th": (("td", "th"), ("tr", "table", "body")),
           "tr": (("tr", "td", "th"), ("table", "tbody", "thead", "tfoot", "body")),
           "dt": (("dt", "dd"), ("dl", "body")), "dd": (("dt", "dd"), ("dl", "body")),
           "option": (("option",), ("select", "datalist", "body")),
           "body": (("head",), ())}
P_STOP = frozenset(("div", "td", "th", "li", "table", "tr", "body", "html", "blockquote", "button", "dd", "dt"))
SCAN = 64                       # stack levels searched for a matching or implied end tag
MAX_CSS = 200_000               # characters of <style> text read
MAX_STYLE_BLOCKS = 50
MAX_RULES = 2000
MAX_CLASSES = 20
MAX_CHECKS = 2_000_000      # rule candidates checked per document
MAX_CACHE = 5000
MIN_FONT_PX = 1.5
MIN_OPACITY = 0.1
MIN_CONTRAST = 1.2
OFFSCREEN_PX = -500
ROOT_FONT_PX = 16.0

_NAMED = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
          "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "yellow": "#ffff00", "navy": "#000080",
          "maroon": "#800000", "purple": "#800080", "teal": "#008080", "olive": "#808000", "lime": "#00ff00",
          "aqua": "#00ffff", "cyan": "#00ffff", "fuchsia": "#ff00ff", "magenta": "#ff00ff", "orange": "#ffa500",
          "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
          "gainsboro": "#dcdcdc", "whitesmoke": "#f5f5f5", "snow": "#fffafa", "ivory": "#fffff0",
          "ghostwhite": "#f8f8ff", "floralwhite": "#fffaf0", "azure": "#f0ffff", "mintcream": "#f5fffa",
          "aliceblue": "#f0f8ff", "seashell": "#fff5ee", "linen": "#faf0e6", "oldlace": "#fdf5e6",
          "honeydew": "#f0fff0", "lavenderblush": "#fff0f5", "beige": "#f5f5dc", "cornsilk": "#fff8dc"}
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n]?|\\(.)", re.S)
_LENGTH = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))(px|pt|em|rem|%|ex|ch|pc|in|cm|mm|q|vw|vh)?$")
_COLOR_TOKEN = re.compile(r"#[0-9a-f]{3,8}\b|(?:rgba?|hsla?)\([^)]*\)|[a-z]+")
_SIMPLE_SEL = re.compile(r"^([a-z][a-z0-9]*)?((?:[.#][\w-]+)*)$")
_SCALE0 = re.compile(r"scale[xyz]?\(\s*[+-]?0*\.?0+\s*[,)]|scale\(\s*[^,)]+,\s*[+-]?0*\.?0+\s*\)")
_TRANSLATE = re.compile(r"translate(?:[xy]|3d)?\(\s*([^)]*)\)")


def _unescape_css(s):
    def rep(m):
        if m.group(1):
            try:
                return chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return m.group(2)
    return _CSS_ESCAPE.sub(rep, s) if "\\" in s else s


def declarations(text):
    """{property: value} from a CSS declaration list, lowercased, comments, escapes and !important removed."""
    out = {}
    text = _unescape_css(_CSS_COMMENT.sub(" ", str(text or ""))).lower()
    for part in text.split(";"):
        name, sep, value = part.partition(":")
        if not sep:
            continue
        name = name.strip()
        value = value.replace("!important", "").strip()
        if name:
            out[name] = value
    return out


def _px(value, font_px=ROOT_FONT_PX, pct_base=None):
    """A length in px, or None. % uses pct_base (None: unknown)."""
    m = _LENGTH.match(value.strip()) if value else None
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or ""
    if unit in ("", "px"):
        return n if unit or n == 0 else None
    if unit == "pt":
        return n * 4 / 3
    if unit in ("em", "ch", "ex"):
        return n * font_px * (0.5 if unit != "em" else 1)
    if unit == "rem":
        return n * ROOT_FONT_PX
    if unit == "%":
        return n * pct_base / 100 if pct_base is not None else None
    if unit == "pc":
        return n * 16
    if unit == "in":
        return n * 96
    if unit == "cm":
        return n * 96 / 2.54
    if unit == "mm":
        return n * 96 / 25.4
    if unit == "q":
        return n * 96 / 101.6
    return None  # vw/vh: unknown


def color(value):
    """(r, g, b, alpha) for a CSS colour, "transparent" as alpha 0, None when unknown."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v == "transparent":
        return (0, 0, 0, 0.0)
    v = _NAMED.get(v, v)
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4) and all(c in "0123456789abcdef" for c in h):
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8) and all(c in "0123456789abcdef" for c in h):
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
        return None
    m = re.match(r"^rgba?\(([^)]*)\)$", v)
    if not m:
        return None
    parts = [p for p in re.split(r"[\s,/]+", m.group(1).strip()) if p]
    if len(parts) not in (3, 4):
        return None
    try:
        rgb = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
        a = 1.0
        if len(parts) == 4:
            a = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
    except ValueError:
        return None
    return tuple(max(0, min(255, round(c))) for c in rgb) + (max(0.0, min(1.0, a)),)


def _luminance(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(fg, bg):
    """WCAG contrast ratio of an (r, g, b, a) text colour over an opaque (r, g, b) background."""
    a = fg[3]
    mixed = tuple(a * f + (1 - a) * b for f, b in zip(fg[:3], bg[:3]))
    l1, l2 = _luminance(mixed), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _background(value, parent):
    """The new background colour (r, g, b) for a background/background-color value; None = unknown."""
    if "url(" in value or "gradient(" in value or "image(" in value or "var(" in value:
        return None
    if value in ("none", "transparent", "initial", "inherit", "unset", ""):
        return parent
    c = color(value)
    if c is None:
        for tok in _COLOR_TOKEN.findall(value):
            c = color(tok)
            if c is not None:
                break
        else:
            return None
    if c[3] < 0.5:
        return parent
    return c[:3]


# --- <style> rules ------------------------------------------------------------------------------------------
def _top_rules(css):
    """[(selector text, declaration text)] of the top-level rules; @-blocks and @-statements are skipped."""
    out, i, n = [], 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j]
        semi = head.rfind(";")
        if head.lstrip().startswith("@") and semi >= 0:   # "@import x; .a" -> keep what follows the statement
            head = head[semi + 1:]
        depth, k = 1, j + 1
        while k < n and depth:
            c = css[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            k += 1
        if not head.strip().startswith("@"):
            out.append((head.strip(), css[j + 1:k - 1]))
        i = k
    return out


def style_rules(src):
    """An index {"tag"|"class"|"id": {key: [(specificity, order, tag, classes, id, declarations)]}} of the simple
    rules in the document's <style> blocks (read with str.find, so a hostile body stays linear)."""
    low = src.lower()
    css, pos, blocks = [], 0, 0
    total = 0
    while blocks < MAX_STYLE_BLOCKS and total < MAX_CSS:
        a = low.find("<style", pos)
        if a < 0:
            break
        b = low.find(">", a)
        if b < 0:
            break
        e = low.find("</style", b)
        if e < 0:
            e = len(src)
        chunk = src[b + 1:e][:MAX_CSS - total]
        css.append(chunk)
        total += len(chunk)
        blocks += 1
        pos = e + 1
    index = {"tag": {}, "class": {}, "id": {}, "count": 0, "seen": {}}
    if not css:
        return index
    text = _unescape_css(_CSS_COMMENT.sub(" ", "\n".join(css))).lower()
    text = text.replace("<!--", " ").replace("-->", " ")
    order = 0
    for sels, body in _top_rules(text):
        decls = None
        for sel in sels.split(","):
            if index["count"] >= MAX_RULES:
                return index
            m = _SIMPLE_SEL.match(sel.strip())
            if not m or not sel.strip():
                continue
            tag = m.group(1)
            ids = re.findall(r"#([\w-]+)", m.group(2))
            classes = frozenset(re.findall(r"\.([\w-]+)", m.group(2)))
            if len(ids) > 1:
                continue
            if decls is None:
                decls = declarations(body)
            if not decls:
                break
            sig = (tag, classes, ids[0] if ids else None)
            if sig in index["seen"]:   # same selector again: later declarations win, one rule
                index["seen"][sig][5].update(decls)
                continue
            spec = (len(ids), len(classes), 1 if tag else 0)
            rule = (spec, order, tag, classes, ids[0] if ids else None, dict(decls))
            index["seen"][sig] = rule
            order += 1
            index["count"] += 1
            if ids:
                index["id"].setdefault(ids[0], []).append(rule)
            elif classes:
                index["class"].setdefault(min(classes), []).append(rule)
            else:
                index["tag"].setdefault(tag, []).append(rule)
    return index


def _matched(index, tag, classes, el_id):
    """The merged declarations of the rules matching an element, or None when the document's rule-matching budget
    (MAX_CHECKS) is spent (the caller then treats the element as hidden: only hostile documents get there)."""
    if not index["count"]:
        return {}
    key = (tag, classes, el_id)
    cache = index.setdefault("cache", {})
    if key in cache:
        return cache[key]
    found = list(index["tag"].get(tag, ()))
    for c in classes:
        found += index["class"].get(c, ())
    if el_id:
        found += index["id"].get(el_id, ())
    index["checks"] = index.get("checks", 0) + len(found) + 1
    if index["checks"] > MAX_CHECKS:
        return None
    out = []
    for rule in found:
        _, _, rtag, rclasses, rid, _ = rule
        if (rtag is None or rtag == tag) and rclasses <= classes and (rid is None or rid == el_id):
            out.append(rule)
    out.sort(key=lambda r: (r[0], r[1]))
    d = {}
    for rule in out:
        d.update(rule[5])
    if len(cache) < MAX_CACHE:
        cache[key] = d
    return d


# --- the parser ---------------------------------------------------------------------------------------------
class _State:
    __slots__ = ("hidden", "quiet", "visible", "opacity", "font", "color", "bg")

    def __init__(self, hidden=False, quiet=False, visible=True, opacity=1.0, font=ROOT_FONT_PX,
                 color_=(0, 0, 0, 1.0), bg=(255, 255, 255)):
        self.hidden, self.quiet, self.visible, self.opacity = hidden, quiet, visible, opacity
        self.font, self.color, self.bg = font, color_, bg

    def child(self):
        return _State(self.hidden, self.quiet, self.visible, self.opacity, self.font, self.color, self.bg)

    def shows(self):
        if self.hidden or self.quiet or not self.visible or self.opacity <= MIN_OPACITY or self.font <= MIN_FONT_PX:
            return False
        if self.color is None or self.bg is None:
            return True
        if self.color[3] <= 0.05:
            return False
        return contrast(self.color, self.bg) >= MIN_CONTRAST


def _num(value):
    try:
        return float(value[:-1]) / 100 if value.endswith("%") else float(value)
    except ValueError:
        return None


def _font_px(value, parent):
    keywords = {"xx-small": 9, "x-small": 10, "small": 13, "medium": 16, "large": 18, "x-large": 24,
                "xx-large": 32, "xxx-large": 48}
    if value in keywords:
        return float(keywords[value])
    if value == "smaller":
        return parent / 1.2
    if value == "larger":
        return parent * 1.2
    px = _px(value, parent, pct_base=parent)
    return parent if px is None or px < 0 else px


def _font_shorthand_size(value):
    for tok in re.split(r"\s+", value):
        size = tok.split("/")[0]
        if _LENGTH.match(size) and (any(size.endswith(u) for u in ("px", "pt", "em", "rem", "%")) or
                                    re.fullmatch(r"[+-]?0*\.?0+", size)):
            return size
    return None


def _apply(state, d, attrs, parent):
    """Mutates state (a child of parent) with the element's declarations d and HTML attributes."""
    if "hidden" in attrs:
        state.hidden = True
    if attrs.get("bgcolor"):
        state.bg = _background(attrs["bgcolor"].lower(), parent.bg)
    if attrs.get("background"):
        state.bg = None
    if not d:
        return
    if d.get("display", "").startswith("none") or d.get("mso-hide") == "all":
        state.hidden = True
    vis = d.get("visibility")
    if vis in ("hidden", "collapse"):
        state.visible = False
    elif vis == "visible":
        state.visible = True
    if "opacity" in d:
        o = _num(d["opacity"])
        if o is not None:
            state.opacity = parent.opacity * max(0.0, min(1.0, o))
    size = d.get("font-size")
    if size is None and "font" in d:
        size = _font_shorthand_size(d["font"])
    if size is not None:
        state.font = _font_px(size, parent.font)
    if "color" in d:
        c = d["color"]
        if c in ("inherit", "currentcolor", "unset", "initial"):
            pass
        else:
            state.color = color(c)
    for prop in ("background", "background-color"):
        if prop in d:
            state.bg = _background(d[prop], parent.bg if prop == "background" else state.bg)
    if d.get("background-image", "none") not in ("none", "initial", "unset", ""):
        state.bg = None
    overflow = " ".join(d.get(p, "") for p in ("overflow", "overflow-x", "overflow-y"))
    if "hidden" in overflow or "clip" in overflow:
        for prop in ("height", "max-height", "width", "max-width"):
            px = _px(d.get(prop, ""), state.font)
            if px is not None and px <= 1:
                state.hidden = True
    for prop in ("left", "top", "right", "bottom", "margin-left", "margin-top", "margin-right", "margin-bottom",
                 "text-indent", "margin"):
        val = d.get(prop)
        if not val:
            continue
        for tok in val.split():
            px = _px(tok, state.font)
            if px is not None and px <= OFFSCREEN_PX:
                state.hidden = True
    clip = d.get("clip", "")
    if clip.startswith("rect("):
        vals = [_px(v, state.font) for v in re.split(r"[\s,]+", clip[5:].rstrip(")").strip()) if v]
        if len(vals) == 4 and None not in vals and (vals[2] <= vals[0] or vals[1] <= vals[3]):
            state.hidden = True
    cp = d.get("clip-path", "")
    m = re.match(r"inset\(\s*([\d.]+)%", cp)
    if (m and float(m.group(1)) >= 50) or re.match(r"circle\(\s*0*\.?0+(?:px|%)?\s*[)a]", cp):
        state.hidden = True
    tf = d.get("transform", "")
    if tf and tf != "none":
        if _SCALE0.search(tf):
            state.hidden = True
        for args in _TRANSLATE.findall(tf):
            for tok in args.split(","):
                px = _px(tok.strip(), state.font)
                if px is not None and px <= OFFSCREEN_PX:
                    state.hidden = True


class _Text(html.parser.HTMLParser):
    def __init__(self, rules):
        super().__init__(convert_charrefs=True)
        self.out, self.rules, self.dropped = [], rules, False
        self.root = _State()
        self.stack = []  # [(tag, _State)]

    def _top(self):
        return self.stack[-1][1] if self.stack else self.root

    def _pop_to(self, names, stops=(), limit=SCAN):
        for i in range(len(self.stack) - 1, max(-1, len(self.stack) - 1 - limit), -1):
            t = self.stack[i][0]
            if t in names:
                del self.stack[i:]
                return True
            if t in stops:
                return False
        return False

    def handle_starttag(self, tag, attrs):
        if tag in IMPLIED:
            names, stops = IMPLIED[tag]
            self._pop_to(names, stops)
        if tag in P_CLOSERS:
            self._pop_to(("p",), P_STOP)
        parent = self._top()
        if tag in BREAK and parent.shows():
            self.out.append("\n")
        if tag in VOID:
            return
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        state = parent.child()
        if tag in SKIP:
            state.quiet = True
        elif tag in DROP:
            state.hidden = True
        if tag == "font" and a.get("color"):
            state.color = color(a["color"].lower())
        classes = frozenset(a.get("class", "").lower().split()[:MAX_CLASSES])
        d = _matched(self.rules, tag, classes, a.get("id", "").lower() or None)
        if d is None:
            state.hidden, d = True, {}
        else:
            d = dict(d)
        if a.get("style"):
            d.update(declarations(a["style"]))
        _apply(state, d, a, parent)
        self.stack.append((tag, state))

    def handle_startendtag(self, tag, attrs):
        if tag in BREAK and self._top().shows():
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("body", "html"):
            pass
        elif tag != "br":
            self._pop_to((tag,))
        if tag in BREAK and self._top().shows():
            self.out.append("\n")

    def handle_data(self, data):
        st = self._top()
        if st.shows():
            self.out.append(data)
        elif not st.quiet and not self.dropped and data.strip():
            self.dropped = True


def html_text(src):
    """(visible text, hidden_text) for an HTML body. hidden_text is True when non-blank text was dropped because
    a reader wouldn't see it (not counting <script>, <style>, <head>, <title> or comments)."""
    src = str(src or "")
    p = _Text(style_rules(src))
    try:
        p.feed(src)
        p.close()
    except Exception:  # malformed beyond the parser: keep what was read, never fall back to raw tag stripping
        return "".join(p.out), True
    return "".join(p.out), p.dropped


def html_to_text(src):
    return html_text(src)[0]


__all__ = ["html_text", "html_to_text", "declarations", "color", "contrast", "style_rules"]
