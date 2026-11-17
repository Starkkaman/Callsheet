"""0.22.0 area colours (mockup areas-v1, owner approved 2026-11-16): the palette of 10, each with a Light and a Dark
shade that meets WCAG AA as text on the page, the panel and the pressed panel, and as a fill under its own text
colour; a free colour ("More colours") checked in both themes with a deeper or lighter shade that passes, and a
warning when it sits close to a state colour. Pure: no I/O.

Areas saved before 0.22.0 kept a number 1 to 4; LEGACY maps it to the first four keys (today's colours, unchanged)."""
import colorsys
import re

# [key, name, Light shade, Dark shade]. The first four are the colours areas had before (1 to 4). No orange, red,
# amber or green: those mean now, alert, warning and done.
PALETTE = (
    ("blue", "Blue", "#4a6396", "#8098c8"), ("teal", "Teal", "#2c6d7d", "#5ea3b3"),
    ("violet", "Violet", "#6f52a3", "#a58bd3"), ("walnut", "Walnut", "#6b4428", "#c09a74"),
    ("pine", "Pine", "#2a6f58", "#44a081"), ("plum", "Plum", "#8c4c8c", "#c373c3"),
    ("berry", "Berry", "#a93b69", "#d07299"), ("indigo", "Indigo", "#535ab9", "#848ad6"),
    ("olive", "Olive", "#65672c", "#929540"), ("slate", "Slate", "#556576", "#7d92a8"))
KEYS = tuple(p[0] for p in PALETTE)
BY_KEY = {p[0]: p for p in PALETTE}
LEGACY = {1: "blue", 2: "teal", 3: "violet", 4: "walnut"}
# the theme tokens (app.css): the text colour on an area fill, and the page, panel and pressed panel
LIGHT = {"on": "#f7f4ee", "bgs": ("#ece7dd", "#f7f4ee", "#e8e2d5")}
DARK = {"on": "#10141d", "bgs": ("#10141d", "#161c28", "#202838")}
AA = 4.5
# state colours an area must not look like (app.css: --warn, --alert, --ok, --accent)
STATES = (("amber", "warnings", "#f2c14e"), ("red", "alerts", "#b3261b"), ("green", "done", "#3f8a1f"),
          ("orange", "the one thing to do now", "#ec7c22"))
# words people use for colours the palette doesn't have: the nearest swatches (Chat offers these, never a state colour)
NEAREST = {"green": ("pine", "olive"), "red": ("berry", "plum"), "orange": ("walnut", "berry"),
           "amber": ("olive", "walnut"), "yellow": ("olive", "walnut"), "purple": ("violet", "plum"),
           "pink": ("berry", "plum"), "brown": ("walnut", "olive"), "grey": ("slate",), "gray": ("slate",),
           "navy": ("indigo", "blue"), "turquoise": ("teal",), "cyan": ("teal",), "lavender": ("violet",)}
STATE_WORDS = ("green", "red", "orange", "amber", "yellow")
HEX = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")


class Invalid(ValueError):
    pass


def norm_hex(v):
    m = HEX.fullmatch(str(v or "").strip())
    if not m:
        raise Invalid("A colour is a hex code like #2c6d7d.")
    h = m.group(1).lower()
    return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h))


def _lum(h):
    def f(c):
        c /= 255
        return c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return .2126 * f(int(h[1:3], 16)) + .7152 * f(int(h[3:5], 16)) + .0722 * f(int(h[5:7], 16))


def ratio(a, b):
    x, y = _lum(a), _lum(b)
    return (max(x, y) + .05) / (min(x, y) + .05)


def worst(c, theme):
    """The lowest contrast of the colour as text on the page, panel and pressed panel, and as a fill under its text."""
    return min([ratio(c, b) for b in theme["bgs"]] + [ratio(c, theme["on"])])


def passes(c, dark):
    return worst(c, DARK if dark else LIGHT) >= AA


def _hls(h):
    return colorsys.rgb_to_hls(int(h[1:3], 16) / 255, int(h[3:5], 16) / 255, int(h[5:7], 16) / 255)


def _hex(hh, ll, ss):
    r, g, b = colorsys.hls_to_rgb(hh, ll, ss)
    return "#" + "".join(f"{round(max(0, min(1, v)) * 255):02x}" for v in (r, g, b))


def fit(c, dark):
    """The colour itself when it passes AA in the theme, else the nearest deeper (Light) or lighter (Dark) shade that
    does (aiming at 4.6 for rounding), else None."""
    c = norm_hex(c)
    if passes(c, dark):
        return c
    hh, ll, ss = _hls(c)
    for _ in range(200):
        ll += .005 if dark else -.005
        if not 0 <= ll <= 1:
            return None
        h = _hex(hh, ll, ss)
        if worst(h, DARK if dark else LIGHT) >= 4.6:
            return h
    return None


def near_state(c):
    """(name, meaning) of the state colour this one sits close to (hue within 20 degrees, a strong colour and a
    similar lightness), else None. Walnut, a deep brown, is not an orange."""
    hh, ll, ss = _hls(norm_hex(c))
    if ss < .5:
        return None
    for name, meaning, s in STATES:
        sh, sl, _ = _hls(s)
        d = abs(hh - sh) * 360
        if min(d, 360 - d) < 20 and abs(ll - sl) < .2:
            return name, meaning
    return None


def check_hex(raw):
    """More colours: {hex, light, dark, light_ratio, dark_ratio, raw_light, raw_dark, warnings}. Raises Invalid when
    no shade passes in a theme (never saved)."""
    c = norm_hex(raw)
    lt, dk = fit(c, False), fit(c, True)
    if not lt or not dk:
        raise Invalid("No shade of that colour reads well in both themes. Try another.")
    warns = []
    if lt != c:
        warns.append(f"Too light to read in Light ({worst(c, LIGHT):.1f}:1; it needs 4.5:1). Callsheet would use {lt} "
                     "in Light.")
    if dk != c:
        warns.append(f"Too dark to read in Dark ({worst(c, DARK):.1f}:1; it needs 4.5:1). Callsheet would use {dk} "
                     "in Dark.")
    st = near_state(c)
    if st:
        warns.append(f"Close to the {st[0]} Callsheet uses for {st[1]}, so this area may look like one.")
    return {"hex": c, "light": lt, "dark": dk, "light_ratio": round(worst(lt, LIGHT), 2),
            "dark_ratio": round(worst(dk, DARK), 2), "raw_light": round(worst(c, LIGHT), 2),
            "raw_dark": round(worst(c, DARK), 2), "warnings": warns}


def clean(colour):
    """The stored colour: a palette key, or a free colour as "#rrggbb" (only when a shade passes in both themes).
    Accepts the old numbers 1 to 4 and palette names in any case."""
    if isinstance(colour, int) and not isinstance(colour, bool) and colour in LEGACY:
        return LEGACY[colour]
    if isinstance(colour, str):
        k = colour.strip().lower()
        if k in BY_KEY:
            return k
        if HEX.fullmatch(k):
            return check_hex(k)["hex"]
    raise Invalid("Pick one of the 10 colours, or a hex code like #2c6d7d.")


def info(colour):
    """{key, name, light, dark, custom} for a stored colour (an unknown or old value reads as its palette colour)."""
    if isinstance(colour, int) and not isinstance(colour, bool):
        colour = LEGACY.get(colour, "walnut")
    k = str(colour or "").strip().lower()
    if k in BY_KEY:
        p = BY_KEY[k]
        return {"key": k, "name": p[1], "light": p[2], "dark": p[3], "custom": False}
    try:
        c = check_hex(k)
    except Invalid:
        p = BY_KEY["walnut"]
        return {"key": "walnut", "name": p[1], "light": p[2], "dark": p[3], "custom": False}
    return {"key": c["hex"], "name": c["hex"], "light": c["light"], "dark": c["dark"], "custom": True}


def from_words(text):
    """A palette key, a hex code, or ("nearest", word, keys) for a colour word the palette doesn't have; None."""
    t = str(text or "").strip().lower()
    if t in BY_KEY:
        return t
    m = HEX.search(t) if "#" in t else None
    if m:
        return norm_hex(m.group(0))
    for w in re.findall(r"[a-z]+", t):
        if w in BY_KEY:
            return w
        if w in NEAREST:
            return ("nearest", w, NEAREST[w])
    return None


def first_free(used):
    return next((k for k in KEYS if k not in used), KEYS[len(used) % len(KEYS)])
