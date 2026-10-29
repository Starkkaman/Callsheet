"""Connecting a Google account: Desktop-app OAuth with PKCE and a pasted-back loopback URL.

start() keeps state + PKCE verifier in memory for 15 minutes. finish() takes the address the
browser landed on (http://127.0.0.1:8765/?state=...&code=...) or a bare code, exchanges it, and
stores the account. Codes, tokens and verifiers are never logged or returned.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse

import google
from store import InputError

FLOW_TTL = 15 * 60


class FlowError(InputError):
    """A sign-in problem the user fixes by trying again (HTTP 400, plain sentence)."""


class OAuthFlows:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self.lock = threading.Lock()
        self.flows = {}

    def _prune(self):
        now = self.clock()
        for fid in [f for f, v in self.flows.items() if v["expires"] < now]:
            del self.flows[fid]

    def start(self, area_id):
        client = self.store.google_client()
        if not client:
            raise InputError("Add your Google client ID and secret first.")
        if not isinstance(area_id, str):
            raise InputError("Pick an area for this account.")
        self.store.area(area_id)  # NotFound if unknown
        verifier = secrets.token_urlsafe(64)  # 86 chars of [A-Za-z0-9_-], within PKCE's 43..128
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        flow_id = secrets.token_urlsafe(12)
        with self.lock:
            self._prune()
            self.flows[flow_id] = {"area_id": area_id, "state": state, "verifier": verifier,
                                   "expires": self.clock() + FLOW_TTL}
        return {"flow_id": flow_id, "auth_url": google.build_auth_url(client["client_id"], state, challenge)}

    def finish(self, flow_id, pasted):
        if not isinstance(flow_id, str) or not isinstance(pasted, str) or not pasted.strip():
            raise InputError("Paste the address from the page Google sent you to.")
        with self.lock:
            self._prune()
            flow = self.flows.get(flow_id)
        if flow is None:
            raise FlowError("This sign-in has expired or was not started here. Start again.")
        code, state, scope = parse_pasted(pasted)
        if state is not None and not secrets.compare_digest(state, flow["state"]):
            raise FlowError("That address belongs to a different sign-in. Paste the address from the sign-in "
                            "you just started, or start again.")
        if scope is not None:
            _check_scopes(scope)
        client = self.store.google_client()
        if not client:
            raise InputError("Add your Google client ID and secret first.")
        with self.lock:  # one exchange per flow: codes are single-use
            if self.flows.pop(flow_id, None) is None:
                raise FlowError("This sign-in has expired or was not started here. Start again.")
        try:
            tok = google.exchange_code(client, code, flow["verifier"])
        except google.GoogleError as e:
            if e.code == "invalid_grant":
                raise FlowError("That sign-in code was already used or expired. Start again.") from None
            if e.code in ("invalid_client", "unauthorized_client"):
                raise FlowError("Google doesn't accept the client ID and secret. Check them and start again.") \
                    from None
            raise FlowError(f"{e} Start again.") from None
        if "scope" in tok:
            try:
                _check_scopes(tok["scope"])
            except FlowError:
                google.revoke(tok.get("refresh_token") or tok.get("access_token"))
                raise
        access = tok.get("access_token")
        if not access:
            raise FlowError("Google didn't send an access token. Start again.")
        email = _email_from_id_token(tok.get("id_token"))
        if not email:
            try:
                email = google.gmail_profile(access).get("emailAddress")
            except google.GoogleError:
                email = None
        if not email:
            raise FlowError("Couldn't read the account's email address. Start again.")
        now = self.clock()
        account_id = self.store.save_account(flow["area_id"], email, tok.get("refresh_token"), access,
                                             now + float(tok.get("expires_in", 3600)), now)
        if not self.store.account_secret(account_id)["refresh_token"]:
            self.store.set_status(account_id, "needs_reconnect",
                                  "Google didn't give lasting access. Remove the account and connect it again.")
        try:
            items = google.calendar_list(access)
            self.store.set_calendar_list(account_id, [
                {"id": c.get("id"), "summary": c.get("summaryOverride") or c.get("summary"),
                 "primary": c.get("primary", False)} for c in items if c.get("id")])
        except google.GoogleError as e:
            self.store.set_status(account_id, "error", f"Couldn't read the calendar list: {e}")
        return self.store.account(account_id)


def parse_pasted(pasted):
    """Returns (code, state or None, scope or None). Accepts the full loopback URL, just its query
    string, or a bare code."""
    text = pasted.strip()
    if "code=" in text or "error=" in text or text.startswith(("http://", "https://")):
        query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
        qs = urllib.parse.parse_qs(query)
        if "error" in qs:
            if qs["error"][0] == "access_denied":
                raise FlowError("Sign-in was cancelled on Google's page. Start again.")
            raise FlowError("Google didn't finish the sign-in. Start again.")
        code = qs.get("code", [""])[0]
        if not code:
            raise FlowError("That address has no sign-in code. Copy the whole address from the browser's "
                            "address bar.")
        return code, qs.get("state", [None])[0], qs.get("scope", [None])[0]
    if any(c.isspace() for c in text) or len(text) > 2048:
        raise FlowError("That doesn't look like the address or code from Google. Copy the whole address.")
    return text, None, None


def _check_scopes(scope):
    granted = set(scope.split())
    if google.GMAIL_SCOPE not in granted or google.CALENDAR_SCOPE not in granted:
        raise FlowError("Tick both Gmail and Calendar on Google's consent screen.")


def _email_from_id_token(id_token):
    """The id_token comes straight from Google's token endpoint over TLS, so its payload is read
    without signature checking (OpenID Connect Core 3.1.3.7)."""
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        part = id_token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return None
    email = payload.get("email") if isinstance(payload, dict) else None
    return email if isinstance(email, str) and "@" in email else None
