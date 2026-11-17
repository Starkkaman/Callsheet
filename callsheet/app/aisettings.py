"""Setup › AI in one place (0.21.0 rework, approved 2026-11-12).

One model server (the writer's `local.base_url` and key), two jobs that each pick a model from that server's own
list (GET {base}/models, the owner's saved server only), and a "Use one model for everything" switch:
  - Chat: the chat's local model (meta `chat`.local.model), or a Claude model when a key is saved;
  - Background: reading each message into the ledger, the brief, Get to know me and project suggestions (meta
    `sorting`.model, mirrored into the writer's `local.model`, which Get to know me and Projects read).
Claude is a fallback in Advanced: off unless the owner saves a key and turns it on.

What went (approved): the extra presets and the per-call Careful/Fast mix, Chat's own server ("Same server and
model as the brief writer"), Shadow week and Check the sorting (with the sorter). `migrate` maps settings saved by
earlier versions onto this shape once (meta `ai`.v); nothing is lost: what it changed is kept in `ai`.before (API
keys never copied there) and said in `ai`.notes. stdlib only.
"""
import copy
import threading
import time
import urllib.parse

import background
import chat
import writer
from store import InputError, Store, key_hint

VERSION = 1
MODELS_TIMEOUT = 8          # seconds: GET /models is small; a server that doesn't answer by then is reported
MODELS_MAX = 200
STATES = {"loaded": "loaded", "unloaded": "unloaded", "loading": "loading", "not-loaded": "unloaded"}

_LIST = {}                  # base_url -> the last model list {ok, at, models, message} (memory only)
_LIST_LOCK = threading.Lock()


def _meta(store, key):
    v = store._get_meta(key, None)  # noqa: SLF001
    return dict(v) if isinstance(v, dict) else {}


def _norm(url):
    return str(url or "").strip().rstrip("/")


def _host(url):
    try:
        p = urllib.parse.urlsplit(url)
        return p.hostname + (f":{p.port}" if p.port else "") if p.hostname else url
    except ValueError:
        return "another server"


def _no_keys(d):
    """A copy of a saved settings dict without API keys (for `ai`.before)."""
    if isinstance(d, dict):
        return {k: ("(removed)" if k in ("api_key", "key") and v else _no_keys(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_no_keys(x) for x in d]
    return d


def settings(store):
    """The saved `ai` meta with defaults: {v, one_model, notes}."""
    a = _meta(store, "ai")
    return {"v": a.get("v") or 0, "one_model": a.get("one_model", True) is not False,
            "notes": [str(n)[:300] for n in a.get("notes") or [] if isinstance(n, str)][:6]}


def _save(store, **kw):
    a = _meta(store, "ai")
    a.update(kw)
    a["v"] = VERSION   # any saved choice is in the new shape: nothing left to migrate
    store._set_meta("ai", a)  # noqa: SLF001


# --- migration -----------------------------------------------------------------------------------------------------
def migrate(store, clock=time.time):
    """Maps settings saved before the one-server design onto it, once. Returns the notes (empty when nothing moved
    or it already ran). Idempotent; never turns Claude on unless the owner had chosen Claude and saved a key. When
    nothing needs moving (a new install, or an older one already in this shape) it writes nothing at all, so opening
    an older install leaves its database as it was."""
    with store.lock:
        if settings(store)["v"] >= VERSION:
            return []
        w_raw, c_raw, s_raw = _meta(store, "writer"), _meta(store, "chat"), _meta(store, "sorting")
        if not (w_raw or c_raw or s_raw):   # a new install: nothing to map
            return []
        before = {k: _no_keys(v) for k, v in (("writer", w_raw), ("chat", c_raw), ("sorting", s_raw)) if v}
        notes = []
        w = store.writer_config()
        w0 = copy.deepcopy(w)
        wl = w["local"]
        had_server = bool(wl["base_url"])
        # 1. One server. Chat's own server (and a background server, if one was ever saved) become the one server
        #    when none is saved; else the saved one wins and Chat keeps its model on it.
        c_loc = c_raw.get("local") if isinstance(c_raw.get("local"), dict) else {}
        own = _norm(c_loc.get("base_url"))
        for other, key, who in ((own, c_loc.get("api_key"), "Chat"), (_norm(s_raw.get("base_url")), s_raw.get("api_key"), "Background work")):
            if not other:
                continue
            try:
                other = Store._check_base_url(other)  # noqa: SLF001
            except InputError:
                notes.append(f"{who} had a server address Callsheet couldn't read; it now uses your model server.")
                continue
            if not wl["base_url"]:
                wl["base_url"] = other
                wl["api_key"] = key if isinstance(key, str) and key.strip() else wl["api_key"]
                notes.append(f"{who}'s own server ({_host(other)}) is now your model server.")
            elif _norm(wl["base_url"]) != other:
                notes.append(f"{who} had its own server ({_host(other)}); it now uses your model server "
                             f"({_host(wl['base_url'])}).")
            elif not wl["api_key"] and isinstance(key, str) and key.strip():
                wl["api_key"] = key
        # 2. Chat's model: its own one, else the one it followed (the writer's), else it follows Background.
        chat_model = str(c_loc.get("model") or "").strip() if own else ""
        chat_model = chat_model or str(wl.get("model") or "").strip()
        # 3. Who writes: Claude becomes the fallback in Advanced (only when a key is saved: the owner had turned it on) and
        #    the writer follows the server. "Off" with a server saved was the owner's choice and stays (picking a
        #    model or the server in Setup › AI turns it back on); "off" for want of a server follows the moved one.
        if w["provider"] == "claude":
            if w["claude"]["api_key"]:
                w["fallback_to_claude"] = True
                notes.append("Claude wrote your briefs before; your model server does now, with Claude as the "
                             "fallback (AI › Advanced).")
            w["provider"] = "local" if wl["base_url"] else "off"
        elif w["provider"] == "off" and wl["base_url"] and not had_server:
            w["provider"] = "local"
        # 4. Presets, the per-call mix, Shadow week and the sorter's modes are gone (kept in `ai`.before).
        if w_raw.get("presets") or w_raw.get("call_presets"):
            careful = [p.get("model") for p in w_raw.get("presets") or [] if isinstance(p, dict)
                       and p.get("id") not in ("fast", None) and p.get("model")]
            notes.append("Presets are gone: one model per job now." +
                         (f" Your Careful preset used {str(careful[0])[:80]}." if careful else ""))
        bg = background.background_model(store)   # what Background ran then ("" when none was saved)
        c = chat.load_settings(store)
        c0 = copy.deepcopy(c)
        c["local"].update(base_url="", api_key=None)
        if own or (chat_model and chat_model != bg):   # Chat keeps the model it used (else it follows Background)
            c["local"]["model"] = chat_model
        one = c["provider"] == "local" and (not chat_model or chat_model == bg)
        if w == w0 and c == c0 and not notes and one:
            return []   # already in the new shape: write nothing
        if w != w0 or notes:
            store._set_meta("writer", w)  # noqa: SLF001 (writer_config's shape: unknown old keys drop out)
        if c != c0:
            store._set_meta("chat", dict(c, v062=True))  # noqa: SLF001
        _save(store, one_model=one, notes=notes, before=before, migrated_at=round(clock()))
        return notes


# --- the model list ------------------------------------------------------------------------------------------------
def _entry(e):
    if isinstance(e, str):
        e = {"id": e}
    if not isinstance(e, dict):
        return None
    mid = str(e.get("id") or e.get("name") or e.get("model") or "").strip()[:120]
    if not mid or not background.MODEL.fullmatch(mid):
        return None
    st = e.get("status")
    raw = st.get("value") if isinstance(st, dict) else st if isinstance(st, str) else e.get("state")
    return {"id": mid, "ctx": writer.ctx_of_entry(e), "state": STATES.get(str(raw or "").lower())}


def parse_models(res):
    """A GET /models answer ({data: [...]} or {models: [...]}) -> [{id, ctx, state}] (state: loaded, unloaded,
    loading, or None when the server doesn't say); None when it isn't an OpenAI-compatible list."""
    items = res.get("data") if isinstance(res, dict) else None
    if not isinstance(items, list):
        items = res.get("models") if isinstance(res, dict) else None
    if not isinstance(items, list):
        return None
    out, seen = [], set()
    for e in items:
        m = _entry(e)
        if m and m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
    return out[:MODELS_MAX]


def list_models(store, clock=time.time, fetch=None):
    """{ok, at, models, message}: asks the owner's saved model server (and only it) for its models, with a short
    timeout. fetch(base, key) replaces the request (tests). The answer is remembered for the page."""
    loc = store.writer_config()["local"]
    base, key = loc["base_url"], loc["api_key"]
    at = round(clock())
    if not base:
        return {"ok": False, "at": at, "models": [], "message": "Add your model server's address first."}
    try:
        res = fetch(base, key) if fetch else writer._local("GET", base, "/models", key, timeout=MODELS_TIMEOUT)  # noqa: SLF001
        models = parse_models(res)
        if models is None:
            out = {"ok": False, "models": [], "message": "Your model server answered, but not like an "
                                                         "OpenAI-compatible server (no model list)."}
        elif not models:
            out = {"ok": True, "models": [], "message": "Connected, but the server lists no models."}
        else:
            n = len(models)
            out = {"ok": True, "models": models, "message": f"Connected · {n} model{'' if n == 1 else 's'}"}
    except writer.LocalTimeout:
        out = {"ok": False, "models": [], "message": f"Your model server didn't answer within {MODELS_TIMEOUT} "
                                                     "seconds."}
    except writer.WriterError as e:
        msg = str(e).replace("Couldn't reach the local model. Check the server address.",
                             "Couldn't reach your model server. Check the address and that it's running.")
        out = {"ok": False, "models": [], "message": msg}
    out["at"] = at
    with _LIST_LOCK:
        _LIST[_norm(base)] = out
    return out


def cached_list(base):
    with _LIST_LOCK:
        hit = _LIST.get(_norm(base))
    return dict(hit) if hit else None


def reset_cache():
    with _LIST_LOCK:
        _LIST.clear()


# --- the page's view and its changes -------------------------------------------------------------------------------
def view(store, bg):
    """GET api/ai: the server (key hint only), the two jobs, one model, Claude in Advanced, the last model list and
    the migration notes. Never holds an API key."""
    w = store.writer_config()
    c = chat.load_settings(store)
    eff = chat.effective_local(store, c)
    a = settings(store)
    bgv = bg.view()
    bg_model = bgv["settings"]["model"]
    chat_local = c["provider"] == "local"
    one = one_now(store, bg)
    cap = (bgv.get("status") or {}).get("capacity") or {}
    return {"server": {"base_url": w["local"]["base_url"], "key_hint": key_hint(w["local"]["api_key"])},
            "one_model": one,
            "chat": {"provider": c["provider"], "model": eff["model"], "claude_model": c["claude"]["model"],
                     "thinking": bool(c["claude" if not chat_local else "local"]["thinking"]),
                     "max_tokens": c["local"]["max_tokens"], "timeout": c["local"]["timeout"],
                     "id_slot": c["local"]["id_slot"], "tools": c["local"]["tools"],
                     "limits": {k: list(v) for k, v in chat.LOCAL_LIMITS.items()}},
            "background": {"model": bg_model, "per_hour": cap.get("per_hour"), "ctx": cap.get("ctx"),
                           "ctx_from": cap.get("ctx_from"), "ctx_size": bgv["settings"]["ctx_size"],
                           "ctx_default": bgv["settings"]["ctx_default"], "ctx_limits": bgv["settings"]["ctx_limits"],
                           "extra_body": bgv["settings"]["extra_body"], "extra_keys": bgv["settings"]["extra_keys"],
                           "health": bgv.get("local_health")},
            "claude": {"key_hint": key_hint(w["claude"]["api_key"]), "model": w["claude"]["model"],
                       "fallback": bool(w["fallback_to_claude"] and w["claude"]["api_key"]),
                       "models": [[k, v[0]] for k, v in writer.MODELS.items()]},
            "models": cached_list(w["local"]["base_url"]),
            "notes": a["notes"]}


def one_now(store, bg):
    """Use one model for everything, as it is now: the switch is on, Chat is on the server and uses the Background
    model (a change made elsewhere, e.g. by an older API call, shows the switch off rather than hide a mismatch)."""
    c = chat.load_settings(store)
    return bool(settings(store)["one_model"] and c["provider"] == "local"
                and chat.effective_local(store, c)["model"] == bg.settings()["model"])


def _model(v):
    if not isinstance(v, str) or not background.MODEL.fullmatch(v.strip()):
        raise InputError("Pick a model from your model server's list.")
    return v.strip()


def _set_background(store, bg, model):
    try:
        bg.update_settings({"model": model})
    except background.InputError as e:
        raise InputError(str(e)) from None
    w = store.writer_config()
    if w["local"]["model"] != model:   # Get to know me and project suggestions follow Background
        store.update_writer({"local": {"model": model}})


def _set_chat(store, patch):
    try:
        chat.update_settings(store, patch)
    except chat.InputError as e:
        raise InputError(str(e)) from None


KEYS = ("base_url", "api_key", "chat_model", "chat_claude", "background_model", "one_model", "notes_seen")


def update(store, bg, patch):
    """PATCH api/ai: base_url, api_key ("" removes it), chat_model (a local model), chat_claude (a Claude model id;
    needs a saved key), background_model, one_model (true: Chat uses the Background model and follows it),
    notes_seen (clears the upgrade notes). Each key is checked before anything is saved. Returns view()."""
    if not isinstance(patch, dict) or not patch:
        raise InputError("Send the AI settings as a JSON object.")
    for k in patch:
        if k not in KEYS:
            raise InputError(f"Setup › AI can't change {str(k)[:40]}.")
    with store.lock:
        w = store.writer_config()
        one = patch.get("one_model", one_now(store, bg))
        if not isinstance(one, bool):
            raise InputError("one_model must be true or false.")
        bgm = _model(patch["background_model"]) if "background_model" in patch else None
        cm = _model(patch["chat_model"]) if "chat_model" in patch else None
        cc = patch.get("chat_claude")
        if cc is not None and cc not in writer.MODELS:
            raise InputError("Pick Claude Opus 5.5, Sonnet 5.5 or Haiku 5.5.")
        if cc and not w["claude"]["api_key"]:
            raise InputError("Save a Claude key in AI › Advanced first.")
        if "notes_seen" in patch and patch["notes_seen"] is not True:
            raise InputError("notes_seen must be true.")
        server = {}
        if "base_url" in patch:
            server["base_url"] = patch["base_url"]
        if "api_key" in patch:
            server["api_key"] = patch["api_key"] or ""
        if server:
            store.update_writer({"local": server})
        if server or bgm or cm or "one_model" in patch:   # a choice here means: use the model server
            base = store.writer_config()["local"]["base_url"]
            store.update_writer({"provider": "local" if base else "off"})
        if bgm:
            _set_background(store, bg, bgm)
        if cc:
            _set_chat(store, {"provider": "claude", "claude": {"model": cc}})
            one = False
        if cm:
            _set_chat(store, {"provider": "local", "local": {"model": cm}})
        if "one_model" in patch or bgm:
            if one:   # Chat uses the Background model (and the switch turns Chat back to the server)
                _set_chat(store, {"provider": "local", "local": {"model": bg.settings()["model"]}})
        if cm and "one_model" not in patch and cm != bg.settings()["model"]:
            one = False
        _save(store, one_model=one, **({"notes": []} if patch.get("notes_seen") else {}))
    return view(store, bg)
