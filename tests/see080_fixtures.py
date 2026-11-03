"""0.8.0 test fixtures: calendar files in the formats Google, Outlook, Zoom and Teams send, and Gmail messages
(format=full) that carry them. Made-up people and domains only."""
import base64
import json


def b64(text):
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def cal(*vevents, method="REQUEST", prodid="-//Google Inc//Google Calendar 70.9054//EN", extra=""):
    body = "\r\n".join(["BEGIN:VCALENDAR", f"PRODID:{prodid}", "VERSION:2.0"] +
                       ([f"METHOD:{method}"] if method else []) + ([extra] if extra else []) + list(vevents) +
                       ["END:VCALENDAR"])
    return body + "\r\n"


def vevent(uid="abc123@google.com", start="DTSTART:20261104T193000Z", end="DTEND:20261104T201500Z",
           summary="Edit review", organizer="ORGANIZER;CN=Dana Lee:mailto:dana@studio.example", seq=0, extra=()):
    lines = ["BEGIN:VEVENT", start, end, "DTSTAMP:20261103T120000Z", organizer, f"UID:{uid}",
             "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN=me@example.com:"
             "mailto:me@example.com", f"SEQUENCE:{seq}", "STATUS:CONFIRMED", f"SUMMARY:{summary}"] + list(extra) + \
        ["END:VEVENT"]
    return "\r\n".join(x for x in lines if x)


GOOGLE = cal(vevent(extra=("DESCRIPTION:Join with Google Meet: https://meet.google.com/abc-defg-hij",
                           "LOCATION:")))

OUTLOOK = cal(
    "\r\n".join(["BEGIN:VTIMEZONE", "TZID:Pacific Standard Time", "BEGIN:STANDARD", "DTSTART:16010101T020000",
                 "TZOFFSETFROM:-0700", "TZOFFSETTO:-0800", "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11",
                 "END:STANDARD", "BEGIN:DAYLIGHT", "DTSTART:16010101T020000", "TZOFFSETFROM:-0800",
                 "TZOFFSETTO:-0700", "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3", "END:DAYLIGHT",
                 "END:VTIMEZONE"]),
    vevent(uid="040000008200E00074C5B7101A82E00800000000", summary="Budget sync",
           start="DTSTART;TZID=Pacific Standard Time:20261105T140000",
           end="DTEND;TZID=Pacific Standard Time:20261105T150000",
           organizer="ORGANIZER;CN=Sam Ortiz:mailto:sam@agency.example",
           extra=("LOCATION;LANGUAGE=en-US:Microsoft Teams Meeting",
                  "X-ALT-DESC;FMTTYPE=text/html:<html><script>alert(1)</script><img src=https://t.example/x></html>",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:REMINDER", "TRIGGER;RELATED=START:-PT15M",
                  "END:VALARM")),
    prodid="Microsoft Exchange Server 2010")

ZOOM = cal(vevent(uid="ZOOM8423@zoom.us", summary="Zoom catch-up",
                  start="DTSTART;TZID=America/Los_Angeles:20261106T090000",
                  end="DTEND;TZID=America/Los_Angeles:20261106T093000",
                  organizer="ORGANIZER;CN=Kim:mailto:kim@vendor.example",
                  extra=("LOCATION:https://us02web.zoom.us/j/8423",)),
           method="PUBLISH", prodid="-//zoom.us//iCalendar Event//EN")

TEAMS = cal(vevent(uid="teams-77@outlook.example", summary="Weekly check-in",
                   start="DTSTART;TZID=W. Europe Standard Time:20261109T170000",
                   end="DTEND;TZID=W. Europe Standard Time:20261109T173000",
                   organizer="ORGANIZER;CN=Ana Ruiz:mailto:ana@partner.example",
                   extra=("X-MICROSOFT-SKYPETEAMSMEETINGURL:https://teams.microsoft.com/l/meetup-join/x",
                          "LOCATION:Microsoft Teams Meeting")),
            prodid="Microsoft Exchange Server 2010")

AUTH_PASS = "mx.google.com; dkim=pass header.i=@studio.example; spf=pass smtp.mailfrom=studio.example; " \
            "dmarc=pass (p=NONE) header.from=studio.example"
AUTH_FAIL = "mx.google.com; dkim=fail header.i=@studio.example; spf=fail smtp.mailfrom=studio.example; " \
            "dmarc=fail (p=NONE) header.from=studio.example"


def message(mid, ics_parts, sender="Dana Lee <dana@studio.example>", auth=AUTH_PASS, ts_ms=1793700000000,
            labels=("INBOX",), subject="Invitation: Edit review"):
    """A Gmail message (format=full). ics_parts: [(text or None, {"attachmentId", "size", "mime", "filename",
    "charset", "cte"})]."""
    parts = [{"partId": "0", "mimeType": "text/plain", "headers": [], "body": {"size": 5, "data": b64("hello")}}]
    for i, (text, o) in enumerate(ics_parts, 1):
        mime = o.get("mime", "text/calendar")
        ct = f"{mime}; charset={o.get('charset', 'UTF-8')}; method={o.get('method', 'REQUEST')}"
        hd = [{"name": "Content-Type", "value": ct}]
        if o.get("cte"):
            hd.append({"name": "Content-Transfer-Encoding", "value": o["cte"]})
        body = {"size": o.get("size", len(text or ""))}
        if text is not None:
            body["data"] = b64(text)
        if o.get("attachmentId"):
            body["attachmentId"] = o["attachmentId"]
        parts.append({"partId": str(i), "mimeType": mime, "filename": o.get("filename", ""), "headers": hd,
                      "body": body})
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "me@example.com"},
               {"name": "Subject", "value": subject}]
    if auth:
        headers.insert(0, {"name": "Authentication-Results", "value": auth})
    return {"id": mid, "threadId": "t" + mid, "internalDate": str(ts_ms), "labelIds": list(labels),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


def walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk_strings(v)


BAD = ("<script", "javascript:", "data:", "<img", "‮", "​", "⁦", "onerror")


def assert_safe(tc, obj, allow_links=("link", "mail_link")):
    """No markup, script, data: URI or invisible character anywhere in the output; links only in Callsheet's own
    link fields (Gmail/Calendar on Google's hosts)."""
    for s in walk_strings({k: v for k, v in obj.items() if k not in allow_links} if isinstance(obj, dict) else obj):
        low = s.lower()
        for b in BAD:
            tc.assertNotIn(b, low, s[:80])
        tc.assertNotRegex(low, r"https?://", s[:80])
    if isinstance(obj, dict):
        for k in allow_links:
            if obj.get(k):
                tc.assertRegex(obj[k], r"^https://(mail|calendar|www)\.google\.com/")
    json.dumps(obj)
