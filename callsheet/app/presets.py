"""Writer presets per call time (0.6.1): pure helpers, no network.

A preset is a named set of local-writer options: model, thinking, effort, extra chat-template options (kwargs),
thinking budget, max tokens, timeout and server slot. The built-in "Fast" preset is the owner's existing local
writer settings (writer.local: model, thinking, reasoning_budget, max_tokens, timeout, id_slot, plus effort and
kwargs), so an upgraded store behaves exactly as before. The owner's own presets are stored in writer.presets.
writer.call_presets maps each call kind (morning, midday, afternoon, refresh) to a preset id; every kind defaults
to "fast", and an id that no longer exists reads as "fast". The urgent light check (push.py) never uses the
writer. Presets apply to the local writer only; Claude stays a separate choice.
"""
import re

import writer

FAST = "fast"
FAST_NAME = "Fast"
MAX_PRESETS = 4  # the owner's own, besides Fast
NAME_MAX = 40
MODEL_MAX = 120
EFFORTS = ("low", "medium", "high")
KWARGS_MAX = 4
KWARG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
KWARG_TEXT_MAX = 40
PRESET_ID = r"fast|pre_[0-9a-f]{8}"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# call kinds in table order: (id, label). "refresh" is the Refresh button and the start-up catch-up sync.
CALL_KINDS = (("morning", "Morning brief"), ("midday", "Midday check-in"), ("afternoon", "Afternoon check-in"),
              ("refresh", "Refresh"))
KIND_IDS = tuple(k for k, _ in CALL_KINDS)
URGENT_NOTE = "The urgent check between call times never uses the writer."

# the preset fields besides id and name, and where Fast keeps them in writer.local
FIELDS = ("model", "thinking", "effort", "kwargs", "reasoning_budget", "max_tokens", "timeout", "id_slot")
NUMBER_NAMES = {"reasoning_budget": "The thinking budget", "max_tokens": "Max tokens",
                "timeout": "The timeout (seconds)"}

# The built-in suggestion the owner can add with one click (the blind ranking's first on quality, 2026-11).
# max_tokens + budget = 6144 tokens of answer room; ~180 s per brief on the owner's server, so 900 s timeout.
SUGGESTION = {"label": "Careful (Swift, thinking)", "name": "Careful", "model": "Swift-1.5-Qwen3.8-27B-Q3_K_M",
              "thinking": True, "effort": None, "kwargs": {}, "reasoning_budget": 2048, "max_tokens": 4096,
              "timeout": 900}

RELOAD_MAX_TOKENS = 8
WRITING = "Writing the summary…"
FELL_BACK_TIMEOUT = "Written by the fast model: the careful one didn't answer in time."
FELL_BACK = "Written by the fast model: the careful one didn't work."


class Invalid(ValueError):
    """A preset or mapping the owner can fix; the message is a plain sentence."""


def default_mapping():
    return {k: FAST for k in KIND_IDS}


# --- reading the stored settings -----------------------------------------------------------
def fast(loc):
    """The built-in Fast preset from writer.local (the owner's current settings)."""
    opts = writer.local_options(loc)
    return {"id": FAST, "name": FAST_NAME, "builtin": True, "model": (loc or {}).get("model") or "",
            "thinking": opts["thinking"], "effort": _effort(loc), "kwargs": _kwargs(loc),
            "reasoning_budget": opts["reasoning_budget"], "max_tokens": opts["max_tokens"],
            "timeout": opts["timeout"], "id_slot": opts["id_slot"]}


def _effort(p):
    v = (p or {}).get("effort")
    return v if v in EFFORTS else None


def _kwargs(p):
    v = (p or {}).get("kwargs")
    try:
        return check_kwargs(v) if v else {}
    except Invalid:
        return {}


def _own(p, loc):
    """One stored preset, read leniently (bad values fall back to Fast's)."""
    base = writer.local_options(loc)
    opts = writer.local_options(dict(base, **{k: p.get(k) for k in ("thinking", "reasoning_budget", "max_tokens",
                                                                     "timeout", "id_slot") if k in p}))
    if "id_slot" in p and p["id_slot"] is None:
        opts["id_slot"] = None
    return {"id": p["id"], "name": str(p.get("name") or "Preset")[:NAME_MAX], "builtin": False,
            "model": str(p.get("model") or "")[:MODEL_MAX], "thinking": opts["thinking"], "effort": _effort(p),
            "kwargs": _kwargs(p), "reasoning_budget": opts["reasoning_budget"], "max_tokens": opts["max_tokens"],
            "timeout": opts["timeout"], "id_slot": opts["id_slot"]}


def stored(w):
    """The owner's own presets as stored (well-formed ones only)."""
    v = w.get("presets")
    return [p for p in v if isinstance(p, dict) and isinstance(p.get("id"), str)
            and re.fullmatch(PRESET_ID, p["id"]) and p["id"] != FAST] if isinstance(v, list) else []


def all_presets(w):
    """Fast first, then the owner's presets."""
    return [fast(w.get("local"))] + [_own(p, w.get("local")) for p in stored(w)]


def mapping(w):
    """{kind: preset id} for every call kind; missing or unknown ids read as Fast."""
    ids = {p["id"] for p in all_presets(w)}
    saved = w.get("call_presets") if isinstance(w.get("call_presets"), dict) else {}
    return {k: saved.get(k) if saved.get(k) in ids else FAST for k in KIND_IDS}


def for_kind(w, kind):
    """The preset a call kind uses (unknown kinds, e.g. None, are a Refresh)."""
    kind = kind if kind in KIND_IDS else "refresh"
    pid = mapping(w)[kind]
    return next(p for p in all_presets(w) if p["id"] == pid)


def is_slow(w, kind):
    """True when the local writer would use another preset than Fast for this call kind."""
    return w.get("provider") == "local" and for_kind(w, kind)["id"] != FAST


def options(loc, preset):
    """writer options for one preset: the server's context from writer.local, the rest from the preset.
    effort and kwargs are only present when set, so Fast without them sends exactly the 0.6.0 body."""
    opts = dict(writer.local_options(loc), thinking=preset["thinking"], reasoning_budget=preset["reasoning_budget"],
                max_tokens=preset["max_tokens"], timeout=preset["timeout"], id_slot=preset["id_slot"])
    if preset.get("effort"):
        opts["effort"] = preset["effort"]
    if preset.get("kwargs"):
        opts["kwargs"] = dict(preset["kwargs"])
    return opts


def reload_body(model, opts=None):
    """The tiny request that loads the Fast model again after another preset ran (max_tokens 8, thinking off)."""
    body = {"model": model, "stream": False, "max_tokens": RELOAD_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Reply with OK."}]}
    return writer.with_slot(body, opts or {})


# --- validation ------------------------------------------------------------------------------
def check_name(name, others):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX or _CTRL.search(name):
        raise Invalid(f"A preset name must be 1 to {NAME_MAX} characters.")
    name = name.strip()
    if name.lower() == FAST_NAME.lower() or any(name.lower() == str(o.get("name", "")).lower() for o in others):
        raise Invalid("There's already a preset with that name.")
    return name


def check_model(v):
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > MODEL_MAX or _CTRL.search(v):
        raise Invalid(f"The model must be the server's model id, at most {MODEL_MAX} characters.")
    return v.strip()


def check_kwargs(v):
    """Extra chat-template options (llama.cpp chat_template_kwargs): at most 4 keys with true/false, whole-number
    or short text values. enable_thinking comes from the Thinking switch."""
    if v is None:
        return {}
    if not isinstance(v, dict) or len(v) > KWARGS_MAX:
        raise Invalid(f"Template options must be an object with at most {KWARGS_MAX} keys.")
    out = {}
    for k, x in v.items():
        if not isinstance(k, str) or not KWARG_KEY.fullmatch(k):
            raise Invalid("Template option names are letters, digits and _ (at most 40).")
        if k == "enable_thinking":
            raise Invalid("Use the Thinking switch instead of enable_thinking.")
        if isinstance(x, bool) or (isinstance(x, int) and abs(x) < 10 ** 9):
            out[k] = x
        elif isinstance(x, str) and len(x) <= KWARG_TEXT_MAX and not _CTRL.search(x):
            out[k] = x
        else:
            raise Invalid(f"Template option values are true/false, whole numbers or text up to {KWARG_TEXT_MAX} "
                          "characters.")
    return out


def check_fields(patch, p):
    """Apply the preset fields in patch to p (a dict with FIELDS) and return p. Raises Invalid."""
    for k, v in patch.items():
        if k == "model":
            p["model"] = check_model(v)
        elif k == "thinking":
            if not isinstance(v, bool):
                raise Invalid("thinking must be true or false.")
            p["thinking"] = v
        elif k == "effort":
            if v not in (None, "") + EFFORTS:
                raise Invalid("Effort must be low, medium, high or empty.")
            p["effort"] = v or None
        elif k == "kwargs":
            p["kwargs"] = check_kwargs(v)
        elif k == "id_slot":
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= writer.ID_SLOT_MAX):
                raise Invalid(f"The server slot must be a whole number from 0 to {writer.ID_SLOT_MAX}, or empty.")
            p["id_slot"] = v
        elif k in NUMBER_NAMES:
            lo, hi = writer.LOCAL_LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise Invalid(f"{NUMBER_NAMES[k]} must be a whole number from {lo:,} to {hi:,}.")
            p[k] = v
        elif k not in ("name", "suggestion"):
            raise Invalid(f"Presets can't change {str(k)[:40]}.")
    return p


def check_mapping(val, ids):
    if not isinstance(val, dict):
        raise Invalid("call_presets maps morning, midday, afternoon and refresh to a preset.")
    out = {}
    for k, v in val.items():
        if k not in KIND_IDS:
            raise Invalid("Call kinds are morning, midday, afternoon and refresh. The urgent check never uses the "
                          "writer.")
        if v not in ids:
            raise Invalid("That preset doesn't exist.")
        out[k] = v
    return out


def in_use(w, pid):
    """The labels of the call kinds that use preset pid."""
    m = mapping(w)
    return [label for k, label in CALL_KINDS if m[k] == pid]
