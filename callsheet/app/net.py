"""0.10.0 connectors: the only code in Callsheet that reads the public web (owner approved 2026-11-04, CLAUDE.md).

Every request goes through `Net.get`, which checks the request before any network I/O and the answer while reading:
- GET only, `https` only, port 443 only, no user name or password in the address; the host must be exactly one of
  the hosts the caller passes (the hosts the owner added for that connector, IDNA-normalized; no wildcards, no IP
  literals, no single-label or local names such as `localhost`, `supervisor`, `*.local`).
- The name is resolved once (`getaddrinfo`); if ANY address is not a public unicast address (RFC 1918, loopback,
  link-local incl. 169.254.169.254, CGNAT 100.64/10, multicast, unspecified, reserved, unique-local fc00::/7, the
  Supervisor network 172.30.32.0/23, or an IPv4-mapped/6to4/NAT64/Teredo form of those) the request is refused. The
  connection then goes to the checked address itself, with SNI and Host set to the name and the certificate checked
  against the name, so a second DNS answer (rebinding) can't move it.
- Redirects are never followed automatically: a 301/302/303/307/308 to the same host over https is followed (at most
  MAX_REDIRECTS, each checked again); anything else is `Moved` (another host) or `Refused` (http, another port).
- Size: the answer is read in chunks up to the caller's cap (at most MAX_CAP); a bigger answer is `TooBig`, or, for
  an HTML page read with `truncate=True` (YouTube's channel page is ~2.7 MB, its canonical link sits near 0.8 MB), cut
  at the cap. gzip is the only content coding asked for and is decompressed with the same cap.
- Time: CONNECT_S to connect, TOTAL_S for the whole request (a watchdog shuts the socket at the deadline, so a server
  trickling bytes can't hold it open; when the deadline falls during the TCP connect or the TLS handshake it shuts the
  socket being set up and keeps watching until the request ends); at most CONCURRENT requests at once.
- Rate: a poll of a host waits HOST_GAP_S (15 min) after the last poll of that host; the owner's Check presses are
  limited to CHECKS_PER_WINDOW per host per HOST_GAP_S.
- Sent: `User-Agent: Callsheet/<version> (feed reader)`, `Accept`, `Accept-Encoding: gzip`, `Connection: close` and
  the caller's conditional headers (If-None-Match, If-Modified-Since) only. No cookies, auth, referrer or owner data.
- Content types: feeds, XML, HTML and plain text only; images, video, audio and everything else are refused (no
  thumbnails or media are ever fetched).
Errors are plain sentences (`NetError` and subclasses). Logs are the caller's (host, status, bytes, counts).
Tests inject `resolver` and `connect`; nothing here reads environment variables: http.client (not urllib.request) is
used, so HTTP(S)_PROXY, ALL_PROXY and NO_PROXY are never consulted, and the TLS context drops an SSLKEYLOGFILE the
environment may set. The certificate and the host name are always checked (CERT_REQUIRED, check_hostname).
"""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.parse
import zlib

PORT = 443
CONNECT_S = 5.0
TOTAL_S = 15.0
MAX_REDIRECTS = 2
HOST_GAP_S = 15 * 60
CHECKS_PER_WINDOW = 6
MAX_CAP = 2 * 1024 * 1024
CHUNK = 64 * 1024
CONCURRENT = 2
MAX_URL = 2000
REDIRECTS = (301, 302, 303, 307, 308)
ACCEPT = ("application/atom+xml, application/rss+xml, application/rdf+xml;q=0.9, application/xml;q=0.9, "
          "text/xml;q=0.9, text/html;q=0.8, text/plain;q=0.5")
TYPES = frozenset(("application/atom+xml", "application/rss+xml", "application/rdf+xml", "application/xml",
                   "text/xml", "text/html", "application/xhtml+xml", "text/plain", "application/x-rss+xml",
                   "application/feed+xml"))
_USER_AGENT = ["Callsheet (feed reader)"]
# blocked on top of `not is_global` (which already covers most): spelled out so the rule reads plainly
_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "172.30.32.0/23", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8", "2001:db8::/32", "100::/64"))
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_LOCAL_NAMES = re.compile(r"(?:^|\.)(?:localhost|local|localdomain|internal|intranet|lan|home|corp|private|"
                          r"home\.arpa|in-addr\.arpa|ip6\.arpa|onion|test|invalid|example)$")
_HOSTNAME = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


def set_version(version):
    """api.App sets the app version once, for the User-Agent."""
    _USER_AGENT[0] = f"Callsheet/{version} (feed reader)"


def user_agent():
    return _USER_AGENT[0]


class NetError(Exception):
    """A plain sentence for the owner. .why: a short code (refused, moved, toobig, timeout, http, unreachable,
    type, rate, bad); .status: the HTTP status (0: no answer)."""
    why = "error"

    def __init__(self, message, status=0, why=None):
        super().__init__(message)
        self.status = status
        if why:
            self.why = why


class Refused(NetError):
    why = "refused"


class Moved(NetError):
    """A redirect to another host (or the feed autodiscovery found one): .host is that host, .url the address."""
    why = "moved"

    def __init__(self, message, host, url, status=0):
        super().__init__(message, status)
        self.host, self.url = host, url


class TooBig(NetError):
    why = "toobig"


class Timeout(NetError):
    why = "timeout"


class RateLimited(NetError):
    """Wait before asking this host again. .wait: seconds."""
    why = "rate"

    def __init__(self, message, wait, status=0):
        super().__init__(message, status)
        self.wait = max(0.0, float(wait))


class Answer:
    def __init__(self, status, headers, body, url, truncated=False):
        self.status, self.headers, self.body, self.url, self.truncated = status, headers, body, url, truncated

    @property
    def not_modified(self):
        return self.status == 304

    @property
    def ctype(self):
        return (self.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


# --- names and addresses ---------------------------------------------------------------------------------------
def normalize_host(name, allow_test_names=False):
    """The host as Callsheet stores and compares it: lower case, IDNA (punycode), no trailing dot. ValueError (a
    plain sentence) for an IP literal, a local or single-label name, or anything that isn't a host name."""
    name = str(name or "").strip().rstrip(".")
    if not name or len(name) > 253 or any(c in name for c in " \t\r\n/\\@:?#%"):
        raise ValueError("That isn't a web address Callsheet can read.")
    try:
        ipaddress.ip_address(name.strip("[]"))
        raise ValueError("Use the site's name, not a number address.")
    except ValueError as e:
        if "number address" in str(e):
            raise
    try:
        name = name.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ValueError("That site name can't be read.") from None
    if re.fullmatch(r"[0-9.]+", name) or re.fullmatch(r"0x[0-9a-f.x]+", name):
        raise ValueError("Use the site's name, not a number address.")
    if not _HOSTNAME.fullmatch(name):
        raise ValueError("That isn't a web address Callsheet can read.")
    if _LOCAL_NAMES.search(name) and not (allow_test_names and re.search(r"\.(?:example|test)$", name)):
        raise ValueError("Callsheet reads only public sites, not names on your own network.")
    if name in ("supervisor", "homeassistant", "hassio"):
        raise ValueError("Callsheet reads only public sites, not names on your own network.")
    return name


def blocked(ip):
    """Why this address may not be contacted ("" when it's a public unicast address)."""
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return "not an address"
    if a.version == 6:
        if a.ipv4_mapped is not None:
            return blocked(a.ipv4_mapped) or ""
        if a.sixtofour is not None:
            return blocked(a.sixtofour) or "6to4"
        if a.teredo is not None:
            return "teredo"
        for net in _NAT64:
            if a in net:
                return blocked(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)) or ""
    for net in _BLOCKED:
        if a.version == net.version and a in net:
            return "private or reserved"
    if a.is_multicast or a.is_unspecified or a.is_loopback or a.is_link_local or a.is_reserved:
        return "private or reserved"
    if not a.is_global:
        return "private or reserved"
    return ""


def split_url(url, allow_test_names=False):
    """(host, target) of an https address on port 443; Refused (a plain sentence) for anything else."""
    url = str(url or "").strip()
    if not url or len(url) > MAX_URL or re.search(r"[\x00-\x20\x7f]", url):
        raise Refused("That isn't a web address Callsheet can read.")
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        raise Refused("That isn't a web address Callsheet can read.") from None
    if p.scheme.lower() != "https":
        raise Refused("Callsheet reads only https:// addresses.")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise Refused("That address has a user name in it. Callsheet doesn't read those.")
    if port not in (None, PORT):
        raise Refused("Callsheet reads only the standard https port (443).")
    try:
        host = normalize_host(p.hostname or "", allow_test_names)
    except ValueError as e:
        raise Refused(str(e)) from None
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    target = path + ("?" + p.query if p.query else "")
    # non-ASCII (bidi, zero-width, letters) in the path or query is sent percent-encoded, never raw on the wire
    return host, re.sub(r"[^\x21-\x7e]+", lambda m: urllib.parse.quote(m.group(0), safe=""), target)


def host_of(url, allow_test_names=False):
    """The normalized host of an https address, or "" when it isn't one Callsheet would read."""
    try:
        return split_url(url, allow_test_names)[0]
    except Refused:
        return ""


def _resolve(host):
    infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to an address that was already checked: SNI, Host and the certificate check use the name."""

    def __init__(self, host, ip, timeout, context):
        super().__init__(host, PORT, timeout=timeout, context=context)
        self._ip = ip
        self._raw = None        # the TCP socket during the TLS handshake (the watchdog can shut it)

    def connect(self):
        # an address, not a name: create_connection's getaddrinfo is a numeric parse, never a second DNS lookup
        sock = socket.create_connection((self._ip, PORT), self.timeout)
        self._raw = sock
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


_CONTEXT = None


def _context():
    global _CONTEXT
    if _CONTEXT is None:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED     # create_default_context's defaults, stated so they can't drift
        ctx.check_hostname = True
        if hasattr(ctx, "keylog_filename"):
            ctx.keylog_filename = None          # never write TLS secrets where SSLKEYLOGFILE points
        _CONTEXT = ctx
    return _CONTEXT


def _connect(host, ip, timeout):
    return _Pinned(host, ip, timeout, _context())


# --- the guarded GET -------------------------------------------------------------------------------------------
class Net:
    def __init__(self, clock=time.time, monotonic=time.monotonic, resolver=None, connect=None,
                 connect_s=CONNECT_S, total_s=TOTAL_S, host_gap_s=HOST_GAP_S, allow_test_names=False):
        self.clock, self.monotonic = clock, monotonic
        self.resolver = resolver or _resolve
        self.connect = connect or _connect
        self.connect_s, self.total_s, self.host_gap_s = connect_s, total_s, host_gap_s
        self.allow_test_names = allow_test_names
        self.lock = threading.Lock()
        self.sem = threading.BoundedSemaphore(CONCURRENT)
        self.last_poll = {}     # host -> clock() of the last poll
        self.checks = {}        # host -> [clock() of the owner's checks]

    # rate limits ----------------------------------------------------------------------------------------------
    def wait_s(self, host):
        """Seconds until this host may be polled again (0: now)."""
        with self.lock:
            last = self.last_poll.get(host)
        return 0.0 if last is None else max(0.0, last + self.host_gap_s - self.clock())

    def note_poll(self, host, at=None):
        """Counts a read of `host` as a poll (the owner's Add reuses the Check's answer as the first read)."""
        with self.lock:
            self.last_poll[host] = self.clock() if at is None else at

    def _take(self, host, purpose):
        now = self.clock()
        with self.lock:
            if purpose == "poll":
                last = self.last_poll.get(host)
                if last is not None and now - last < self.host_gap_s:
                    raise RateLimited(f"Callsheet read {host} less than 15 minutes ago. It reads it again later.",
                                      last + self.host_gap_s - now)
                self.last_poll[host] = now
            else:
                recent = [t for t in self.checks.get(host, []) if now - t < self.host_gap_s]
                if len(recent) >= CHECKS_PER_WINDOW:
                    raise RateLimited(f"That's a lot of checks of {host}. Wait a few minutes and try again.",
                                      recent[0] + self.host_gap_s - now)
                recent.append(now)
                self.checks[host] = recent

    # the request ----------------------------------------------------------------------------------------------
    def get(self, url, hosts, cap, purpose="poll", headers=None, truncate=False):
        """One guarded GET. `hosts`: the hosts this request may contact (exact names); `cap`: bytes;
        `purpose`: "poll" (the 15-minute host gap) or "check" (the owner's Check, a few per 15 minutes);
        `headers`: conditional headers only; `truncate`: cut an oversized answer at the cap instead of failing.
        Returns an Answer (status 200 or 304); raises NetError."""
        allowed = set()
        for h in hosts or ():
            try:
                allowed.add(normalize_host(h, self.allow_test_names))
            except ValueError:
                continue
        host, target = split_url(url, self.allow_test_names)
        if host not in allowed:
            raise Refused(f"Callsheet may not read {host}: you haven't added it.")
        cap = max(1024, min(int(cap), MAX_CAP))
        extra = {}
        for k, v in (headers or {}).items():
            if k.lower() in ("if-none-match", "if-modified-since") and isinstance(v, str) and v and \
                    len(v) < 300 and not re.search(r"[\r\n]", v):
                extra[k] = v
        self._take(host, purpose)
        if not self.sem.acquire(timeout=self.total_s):
            raise Timeout("Callsheet is busy reading other feeds. It tries again later.")
        try:
            deadline = self.monotonic() + self.total_s
            for _hop in range(MAX_REDIRECTS + 1):
                status, hdrs, body, truncated = self._once(host, target, cap, extra, truncate, deadline)
                if status in REDIRECTS:
                    loc = hdrs.get("location") or ""
                    nxt = urllib.parse.urljoin(f"https://{host}{target}", loc)
                    if urllib.parse.urlsplit(nxt).scheme.lower() != "https":
                        raise Refused("The site sent Callsheet to an http:// address. Callsheet reads only https.",
                                      status)
                    try:
                        nhost, ntarget = split_url(nxt, self.allow_test_names)
                    except Refused as e:
                        raise Refused("The site sent Callsheet somewhere it can't read. " + str(e), status) from None
                    if nhost != host:
                        raise Moved(f"This feed moved to {nhost}. Add it again to allow that host.", nhost, nxt,
                                    status)
                    target = ntarget
                    continue
                return Answer(status, hdrs, body, f"https://{host}{target}", truncated)
            raise NetError("The site redirected Callsheet too many times.", 0, "moved")
        finally:
            self.sem.release()

    def _once(self, host, target, cap, extra, truncate, deadline):
        try:
            ips = list(self.resolver(host))
        except (OSError, UnicodeError, ValueError):
            raise NetError(f"Callsheet couldn't find {host}. Check the address; it tries again later.",
                           0, "unreachable") from None
        if not ips:
            raise NetError(f"Callsheet couldn't find {host}.", 0, "unreachable")
        for ip in ips:
            if blocked(ip):
                raise Refused(f"{host} points to an address on a private network. Callsheet doesn't read those.")
        ip = ips[0]
        left = deadline - self.monotonic()
        if left <= 0:
            raise Timeout(f"{host} took too long to answer.")
        conn = self.connect(host, ip, min(self.connect_s, left))
        timer = _Watchdog(conn, left)
        try:
            conn.putrequest("GET", target, skip_accept_encoding=True)
            for k, v in (("User-Agent", user_agent()), ("Accept", ACCEPT), ("Accept-Encoding", "gzip"),
                         ("Connection", "close")) + tuple(extra.items()):
                conn.putheader(k, v)
            conn.endheaders()
            if conn.sock is not None:
                conn.sock.settimeout(max(0.05, min(self.connect_s, deadline - self.monotonic())))
            resp = conn.getresponse()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            if status in REDIRECTS or status == 304:
                return status, hdrs, b"", False
            if status == 429 or (status == 503 and hdrs.get("retry-after")):
                raise RateLimited(f"{host} asked Callsheet to slow down. It tries again later.",
                                  _retry_after(hdrs.get("retry-after"), self.clock()), status)
            if status != 200:
                raise NetError(_status_words(host, status), status, "http")
            ctype = (hdrs.get("content-type") or "").split(";", 1)[0].strip().lower()
            if ctype and ctype not in TYPES:
                raise NetError(f"{host} answered with something that isn't a feed or a page ({ctype or 'unknown'}).",
                               status, "type")
            coding = (hdrs.get("content-encoding") or "identity").strip().lower()
            if coding not in ("identity", "gzip", "x-gzip", ""):
                raise NetError(f"{host} answered in a form Callsheet doesn't read ({coding}).", status, "type")
            try:
                length = int(hdrs.get("content-length") or -1)
            except ValueError:
                length = -1
            if length > cap and not truncate and coding in ("identity", ""):
                raise TooBig(_too_big(host, cap), status)
            body, truncated = self._read(resp, cap, coding != "identity" and coding != "", truncate, deadline, host)
            return status, hdrs, body, truncated
        except NetError:
            raise
        except (socket.timeout, TimeoutError):
            raise Timeout(f"{host} took too long to answer. Callsheet tries again later.") from None
        except ssl.SSLError:
            raise NetError(f"{host}'s security certificate didn't check out. Callsheet didn't read it.",
                           0, "unreachable") from None
        except (OSError, http.client.HTTPException, ValueError):
            if self.monotonic() >= deadline - 0.01:
                raise Timeout(f"{host} took too long to answer. Callsheet tries again later.") from None
            raise NetError(f"Callsheet couldn't read {host}. It tries again later.", 0, "unreachable") from None
        finally:
            timer.cancel()
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _read(self, resp, cap, gz, truncate, deadline, host):
        out = bytearray()
        raw = 0
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        while True:
            if self.monotonic() > deadline:
                raise Timeout(f"{host} took too long to answer. Callsheet tries again later.")
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            raw += len(chunk)
            if dec is None:
                out += chunk
            else:
                buf = chunk
                while buf:
                    try:
                        part = dec.decompress(buf, cap + 1 - len(out))
                    except zlib.error:
                        raise NetError(f"{host}'s answer was damaged.", 200, "bad") from None
                    out += part
                    if len(out) > cap:
                        break
                    buf = dec.unconsumed_tail
                    if not part:
                        break
            if len(out) > cap or raw > cap:
                if truncate:
                    return bytes(out[:cap]), True
                raise TooBig(_too_big(host, cap), 200)
        return bytes(out), False


class _Watchdog:
    """The deadline: at `left` seconds it shuts the connection's socket so a blocked read returns. A server that
    stalls the TCP connect or the TLS handshake past the deadline would otherwise leave no socket to shut (http.client
    sets conn.sock only after the handshake) and then trickle the answer forever: so the socket being set up is shut
    too, and the watchdog keeps checking until there is a socket to shut or the request has ended."""

    def __init__(self, conn, left):
        self.conn, self.done = conn, threading.Event()
        t = threading.Thread(target=self._run, args=(max(0.0, left),), name="callsheet-net-deadline", daemon=True)
        t.start()

    def _run(self, left):
        if self.done.wait(left):
            return
        while not _kill(self.conn):
            if self.done.wait(0.05):
                return

    def cancel(self):
        self.done.set()


def _kill(conn):
    """Shut the connection's socket (and the one in its TLS handshake). True once the connection's socket was shut."""
    raw = getattr(conn, "_raw", None)
    sock = getattr(conn, "sock", None)
    for s in (raw, sock):
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    return sock is not None


def _retry_after(value, now, longest=24 * 3600):
    value = str(value or "").strip()
    if re.fullmatch(r"\d{1,9}", value):
        return min(float(value), longest)
    try:
        import email.utils
        when = email.utils.parsedate_to_datetime(value).timestamp()
        return min(max(0.0, when - now), longest)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 3600.0


def _too_big(host, cap):
    return f"{host}'s answer was bigger than {cap // 1024} KB, so Callsheet stopped reading it."


def _status_words(host, status):
    if status in (401, 403):
        return f"{host} didn't let Callsheet read that address ({status})."
    if status in (404, 410):
        return f"{host} says that address isn't there ({status}). Check it, or try again later."
    if status >= 500:
        return f"{host} isn't answering properly right now ({status}). Callsheet tries again later."
    return f"{host} answered with an error ({status})."
