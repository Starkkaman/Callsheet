"""What is left of the 0.1 sync after the rework (approved 2026-11-12): the Google token refresh, the lock the
owner-run Know me scan and the urgent check share, and the owner's notes (memory.md). Reading now happens live
(live.py into messages.db); the rules brief, the reply rows and the call-time sync are retired.
"""
import threading
import time

import google
import memory


class Syncer:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self.memory = memory.Memory(store.dir)  # the owner's notes (0.5.0), shared with api.App
        self.lock = threading.Lock()
        self.writing = None    # kept for the callers that ask (nothing writes the old brief any more)
        self.people = None     # 0.8.2: people.People (set by api.App)

    def _token(self, acc, client, force=False):
        """The account's access token, refreshed through Google's token endpoint when it is close to expiry."""
        now = self.clock()
        if not force and acc.get("access_token") and acc.get("expires_at", 0) - 60 > now:
            return acc["access_token"]
        if not client:
            raise google.GoogleError("The Google client ID and secret are missing. Add them in Settings.")
        if not acc.get("refresh_token"):
            raise google.GoogleError("Google asked to sign in again.", 400, "invalid_grant")
        res = google.refresh(client, acc["refresh_token"])
        if not res.get("access_token"):
            raise google.GoogleError("Google didn't send an access token.")
        acc["access_token"] = res["access_token"]
        acc["expires_at"] = now + float(res.get("expires_in", 3600))
        self.store.set_tokens(acc["id"], acc["access_token"], acc["expires_at"], res.get("refresh_token"))
        return acc["access_token"]
