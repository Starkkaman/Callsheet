"""The fake Google of fake_google.py plus what the 0.6.0 Know me index reads: Gmail messages.list (q after:/before:,
pages, resultSizeEstimate) and messages.get(format=full), the profile's messagesTotal, Calendar events with pages,
and Drive files.list/get(alt=media)/export (only when the granted scope has drive.readonly). Stdlib only.

`inject` holds one-shot failures: (path prefix, HTTP status, reason) answered once each, in order, for the first
request whose path starts with the prefix (e.g. a 429 or a 403 userRateLimitExceeded).
"""
import base64
import json
import re
import urllib.parse

import fake_google

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


def message(mid, thread_id, sender, subject, body, when, to="me@example.com", cc=None, labels=("INBOX",),
            html_body=None):
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"data": _b64(body)}})
    if html_body is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": _b64(html_body)}})
    parts.append({"mimeType": "application/pdf", "filename": "invoice.pdf", "body": {"attachmentId": "att1", "size": 9}})
    return {"id": mid, "threadId": thread_id, "labelIds": list(labels), "snippet": (body or "")[:80],
            "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


class FakeGoogleIndex(fake_google.FakeGoogle):
    def __init__(self):
        super().__init__()
        self.messages = {}   # email -> [message]
        self.drive = {}      # email -> [file dict + "content"]
        self.inject = []
        self.page_cap = None

    def add_messages(self, email, msgs):
        self.messages.setdefault(email, []).extend(msgs)

    def add_file(self, email, fid, name, mime, modified, content="", owners=None, size=None):
        f = {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified,
             "owners": owners or [{"displayName": "Sam Rivera", "emailAddress": email}],
             "webViewLink": f"https://docs.google.com/document/d/{fid}/edit", "content": content}
        if size is not None:
            f["size"] = str(size)
        self.drive.setdefault(email, []).append(f)

    def _ours(self, path):
        return (path == "/gmail/v1/users/me/messages" or path.startswith("/gmail/v1/users/me/messages/")
                or path == "/gmail/v1/users/me/profile" or path.startswith("/drive/")
                or (path.startswith("/calendar/v3/calendars/") and path.endswith("/events")))

    def _handle(self, h):
        parts = urllib.parse.urlsplit(h.path)
        path = parts.path
        if h.command != "GET" or not self._ours(path):
            return super()._handle(h)
        query = urllib.parse.parse_qs(parts.query)
        with self.lock:
            self.requests.append((h.command, path, query))
            hit = next((x for x in self.inject if path.startswith(x[0])), None)
            if hit:
                self.inject.remove(hit)
        if hit:
            return self._send(h, hit[1], {"error": {"code": hit[1], "status": "RESOURCE_EXHAUSTED" if hit[1] == 429
                                                    else "PERMISSION_DENIED", "errors": [{"reason": hit[2]}]}})
        auth = h.headers.get("Authorization", "")
        email = self.access_tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
        if not email:
            return self._send(h, 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        q1 = {k: v[-1] for k, v in query.items()}
        if path == "/gmail/v1/users/me/profile":
            return self._send(h, 200, {"emailAddress": email, "messagesTotal": len(self.messages.get(email, [])),
                                       "threadsTotal": 1})
        if path == "/gmail/v1/users/me/messages":
            return self._list_messages(h, email, q1)
        if path.startswith("/gmail/v1/users/me/messages/"):
            mid = urllib.parse.unquote(path.rsplit("/", 1)[1])
            m = next((x for x in self.messages.get(email, []) if x["id"] == mid), None)
            return self._send(h, 200, m) if m else self._send(h, 404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if path.startswith("/calendar/"):
            return self._events(h, email, path, q1)
        return self._drive(h, email, path, q1)

    def _page(self, items, q1, size_key):
        size = int(q1.get(size_key) or 100)
        if self.page_cap:
            size = min(size, self.page_cap)
        start = int(q1.get("pageToken") or 0)
        chunk = items[start:start + size]
        nxt = str(start + size) if start + size < len(items) else None
        return chunk, nxt

    def _list_messages(self, h, email, q1):
        q = q1.get("q", "")
        after = re.search(r"after:(\d+)", q)
        before = re.search(r"before:(\d+)", q)
        msgs = sorted(self.messages.get(email, []), key=lambda m: -int(m["internalDate"]))
        if after:
            msgs = [m for m in msgs if int(m["internalDate"]) / 1000 > int(after.group(1))]
        if before:
            msgs = [m for m in msgs if int(m["internalDate"]) / 1000 < int(before.group(1))]
        chunk, nxt = self._page(msgs, q1, "maxResults")
        out = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in chunk], "resultSizeEstimate": len(msgs)}
        if nxt:
            out["nextPageToken"] = nxt
        return self._send(h, 200, out)

    def _events(self, h, email, path, q1):
        cal = urllib.parse.unquote(path[len("/calendar/v3/calendars/"):-len("/events")])
        items = self.users[email]["events"].get(cal, [])
        chunk, nxt = self._page(items, q1, "maxResults")
        out = {"items": chunk}
        if nxt:
            out["nextPageToken"] = nxt
        return self._send(h, 200, out)

    def _drive(self, h, email, path, q1):
        if DRIVE_SCOPE not in self.scope.split():
            return self._send(h, 403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                 "errors": [{"reason": "insufficientPermissions"}]}})
        files = self.drive.get(email, [])
        if path == "/drive/v3/files":
            m = re.search(r"modifiedTime > '([^']+)'", q1.get("q", ""))
            if m:
                files = [f for f in files if f["modifiedTime"] > m.group(1)]
            chunk, nxt = self._page(files, q1, "pageSize")
            out = {"files": [{k: v for k, v in f.items() if k != "content"} for f in chunk]}
            if nxt:
                out["nextPageToken"] = nxt
            return self._send(h, 200, out)
        rest = path[len("/drive/v3/files/"):]
        fid = rest.split("/")[0]
        f = next((x for x in files if x["id"] == fid), None)
        if not f:
            return self._send(h, 404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if rest.endswith("/export") or q1.get("alt") == "media":
            body = f["content"].encode()
            h.send_response(200)
            h.send_header("Content-Type", "text/plain; charset=utf-8")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return None
        return self._send(h, 200, {k: v for k, v in f.items() if k != "content"})
