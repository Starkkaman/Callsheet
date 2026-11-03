"""0.7.0 GitHub, read-only (owner-approved 2026-11-03). The only code in Callsheet that talks to GitHub.

Every request goes through `request()`, which checks (method, URL) against an exact allowlist before any network
I/O: GET only, only to api.github.com, only these paths:
  /user/repos                      the repos the token can read (Setup's picker, the token check)
  /repos/{owner}/{repo}            one repo
  /repos/{owner}/{repo}/commits    commits in the scan window
  /repos/{owner}/{repo}/issues     issues (pull requests in this list are skipped)
  /repos/{owner}/{repo}/pulls      pull requests
Nothing writes: no POST, PATCH, PUT or DELETE anywhere, no other host, no redirects. Pages follow GitHub's `Link:
rel="next"` header (each next URL is checked again), at most MAX_PAGES per list. ETags are sent back as
If-None-Match (a 304 doesn't count against the rate limit and means "nothing new"). The rate limit headers are
read on every answer; when the limit is used up (or GitHub sends Retry-After) `RateLimited` says until when.
The token (a fine-grained, read-only personal access token) is sent only in the Authorization header to
api.github.com and is never logged, stored elsewhere or returned. Errors are plain sentences.
The base URL can be overridden for tests only: CALLSHEET_GITHUB_API_BASE.
"""
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15
MAX_PAGES = 10
PER_PAGE = 50
API_VERSION = "2022-11-28"
MAX_REPOS = 20
ETAG_MAX = 300
BODY_MAX = 4000

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
REPO = r"(?!\.{1,2}(?:$|/))[A-Za-z0-9._-]{1,100}"
REPO_RE = re.compile(rf"({OWNER})/({REPO})")
TOKEN_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,255}")
_PATHS = (re.compile(r"/user/repos"),
          re.compile(rf"/repos/{OWNER}/{REPO}"),
          re.compile(rf"/repos/{OWNER}/{REPO}/(?:commits|issues|pulls)"))
_QUERY_KEYS = {"per_page", "page", "since", "until", "state", "sort", "direction", "affiliation", "visibility"}

NO_TOKEN = "Add a GitHub token first: Setup, Projects, GitHub."
CLASSIC = ("That looks like a classic token. Make a fine-grained token instead (it starts with github_pat_), "
           "read-only: Contents, Issues and Pull requests set to Read.")
BAD_TOKEN = "That isn't a GitHub fine-grained token. It starts with github_pat_."
UNAUTHORIZED = "GitHub didn't accept the token. It may have expired: make a new read-only token and paste it in Setup."
UNREACHABLE = "Callsheet couldn't reach GitHub. Check this box's internet connection; Callsheet tries again later."
DOWN = "GitHub isn't answering properly right now. Callsheet tries again later."


def api_base():
    return os.environ.get("CALLSHEET_GITHUB_API_BASE", "https://api.github.com").rstrip("/")


class NotAllowed(Exception):
    """A request outside the allowlist. A programming error, never a user error."""


class GitHubError(Exception):
    """GitHub answered with an error or couldn't be reached. str(): a plain sentence. .status: HTTP status (0: no
    answer)."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    """The token's rate limit is used up (or GitHub asked to slow down). .until: epoch seconds to wait for."""

    def __init__(self, message, until, status=403):
        super().__init__(message, status)
        self.until = until


def allowed(method, url):
    """True when (method, url) is on the allowlist: GET, the API host, one of the five paths, known query keys."""
    if method != "GET" or not isinstance(url, str):
        return False
    base = api_base()
    if not url.startswith(base + "/"):
        return False
    rest = url[len(base):]
    path, _, query = rest.partition("?")
    if "#" in rest or "//" in path or "%" in path or not any(p.fullmatch(path) for p in _PATHS):
        return False
    for k, _v in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if k not in _QUERY_KEYS:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D401 (never follow a redirect)
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_token(token):
    """The token as stored, or ValueError with a plain sentence. Only fine-grained tokens (github_pat_...)."""
    token = str(token or "").strip()
    if re.fullmatch(r"gh[opsur]_[A-Za-z0-9]{20,255}", token):
        raise ValueError(CLASSIC)
    if not TOKEN_RE.fullmatch(token):
        raise ValueError(BAD_TOKEN)
    return token


def check_repo(name):
    name = str(name or "").strip().strip("/")
    m = REPO_RE.fullmatch(name)
    if not m:
        raise ValueError("Write the repository as owner/name, e.g. northwind/billing.")
    return name


def _reset_at(headers, now):
    ra = headers.get("Retry-After")
    if ra and str(ra).strip().isdigit():
        return now + int(str(ra).strip())
    reset = headers.get("X-RateLimit-Reset")
    if reset and str(reset).strip().isdigit():
        return float(reset)
    return now + 60


def _hhmm(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M UTC")


def _next_link(link):
    for part in str(link or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"', part)
        if m and "next" in m.group(2).split():
            return m.group(1)
    return None


class Client:
    """One token's requests. etags: a dict {url: etag} the caller keeps between runs (bounded here)."""

    def __init__(self, token, etags=None, clock=None, opener=None):
        self.token = check_token(token)
        self.etags = etags if etags is not None else {}
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc).timestamp())
        self.opener = opener or _OPENER
        self.remaining = None
        self.reset = None
        self.requests = 0

    def request(self, url, what="this repository"):
        """GET one allowlisted URL. Returns (json or None for 304 Not Modified, next page URL or None)."""
        if not allowed("GET", url):
            raise NotAllowed("GET " + url.split("?")[0])
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": "Callsheet (read-only)", "Authorization": "Bearer " + self.token}
        tag = self.etags.get(url)
        if tag:
            headers["If-None-Match"] = tag
        req = urllib.request.Request(url, headers=headers, method="GET")
        self.requests += 1
        now = self.clock()
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                self._limits(r.headers)
                raw = r.read(5_000_000)
                etag = r.headers.get("ETag")
                nxt = _next_link(r.headers.get("Link"))
        except urllib.error.HTTPError as e:
            self._limits(e.headers or {})
            if e.code == 304:
                return None, None
            raise self._error(e.code, e.headers or {}, now, what) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise GitHubError(UNREACHABLE, 0) from None
        if etag:
            if len(self.etags) >= ETAG_MAX and url not in self.etags:
                self.etags.pop(next(iter(self.etags)))
            self.etags[url] = etag
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            raise GitHubError(DOWN, 200) from None
        if nxt and not allowed("GET", nxt):
            nxt = None
        return data, nxt

    def _limits(self, headers):
        rem = headers.get("X-RateLimit-Remaining")
        if rem is not None and str(rem).strip().isdigit():
            self.remaining = int(str(rem).strip())
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None and str(reset).strip().isdigit():
            self.reset = float(reset)

    def _error(self, code, headers, now, what):
        if code == 401:
            return GitHubError(UNAUTHORIZED, code)
        if code in (403, 429) and (str(headers.get("X-RateLimit-Remaining", "")).strip() == "0"
                                   or headers.get("Retry-After") or code == 429):
            until = _reset_at(headers, now)
            return RateLimited(f"GitHub's limit for this token is used up. Callsheet waits until {_hhmm(until)}.",
                               until, code)
        if code == 403:
            return GitHubError(f"The token can't read {what}. Give it read access to Contents, Issues and Pull "
                               "requests for this repository.", code)
        if code == 404:
            return GitHubError(f"GitHub can't find {what}, or the token can't see it.", code)
        if code == 422:
            return GitHubError(f"GitHub didn't accept Callsheet's question about {what}.", code)
        return GitHubError(DOWN, code)

    def pages(self, url, what="this repository", max_pages=MAX_PAGES):
        """Yields each page's list (a 304 ends the walk: nothing new). Stops early when the rate limit runs low."""
        n = 0
        while url and n < max_pages:
            data, url = self.request(url, what)
            n += 1
            if data is None:
                return
            yield data if isinstance(data, list) else []
            if self.remaining is not None and self.remaining < 5:
                raise RateLimited(f"GitHub's limit for this token is almost used up. Callsheet waits until "
                                  f"{_hhmm(self.reset or self.clock() + 3600)}.", self.reset or self.clock() + 3600)

    # the five reads ---------------------------------------------------------------------------------------------
    def user_repos(self, max_pages=4):
        url = api_base() + "/user/repos?" + urllib.parse.urlencode({"per_page": 100, "sort": "updated",
                                                                    "affiliation": "owner,collaborator,organization_member"})
        out = []
        for page in self.pages(url, "your repositories", max_pages):
            for r in page:
                if isinstance(r, dict) and isinstance(r.get("full_name"), str) and REPO_RE.fullmatch(r["full_name"]):
                    out.append({"name": r["full_name"], "private": bool(r.get("private")),
                                "updated": str(r.get("pushed_at") or r.get("updated_at") or "")[:10]})
        return out

    def repo(self, name):
        name = check_repo(name)
        data, _ = self.request(api_base() + "/repos/" + name, name)
        return data

    def list_url(self, name, kind, since_iso=None):
        name = check_repo(name)
        if kind not in ("commits", "issues", "pulls"):
            raise NotAllowed(kind)
        q = {"per_page": PER_PAGE}
        if kind == "commits" and since_iso:
            q["since"] = since_iso
        if kind == "issues":
            q.update(state="all", sort="updated", direction="desc")
            if since_iso:
                q["since"] = since_iso
        if kind == "pulls":
            q.update(state="all", sort="updated", direction="desc")
        return api_base() + f"/repos/{name}/{kind}?" + urllib.parse.urlencode(q)


# --- index items ---------------------------------------------------------------------------------------------------
def _ts(v):
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _login(u):
    return str(u.get("login") or "") if isinstance(u, dict) else ""


def commit_item(repo, c):
    sha = str(c.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return None
    info = c.get("commit") if isinstance(c.get("commit"), dict) else {}
    author = info.get("author") if isinstance(info.get("author"), dict) else {}
    msg = str(info.get("message") or "")
    people = [x for x in (author.get("name"), _login(c.get("author"))) if x]
    return {"source": "github", "account": repo, "source_id": f"{repo}:commit:{sha}", "kind": "commit",
            "thread": repo, "title": f"{repo}: {msg.splitlines()[0] if msg else sha[:7]}"[:300], "people": people,
            "ts": _ts(author.get("date")), "url": str(c.get("html_url") or ""), "body": msg[:BODY_MAX]}


def issue_item(repo, i, pr=False):
    n = i.get("number")
    if not isinstance(n, int) or (not pr and "pull_request" in i):
        return None
    state = "merged" if pr and i.get("merged_at") else str(i.get("state") or "open")
    kind = ("pr_" if pr else "issue_") + (state if state in ("open", "closed", "merged") else "open")
    people = [_login(i.get("user"))] + [_login(a) for a in i.get("assignees") or [] if isinstance(a, dict)]
    label = "PR" if pr else "Issue"
    return {"source": "github", "account": repo, "source_id": f"{repo}:{'pr' if pr else 'issue'}:{n}", "kind": kind,
            "thread": repo, "title": f"{repo} {label} #{n}: {i.get('title') or ''}"[:300],
            "people": [p for p in people if p], "ts": _ts(i.get("updated_at") or i.get("created_at")),
            "url": str(i.get("html_url") or ""), "body": f"{label} #{n} ({state}). " + str(i.get("body") or "")[:BODY_MAX]}


# --- the owner's GitHub settings (meta "github" in callsheet.db, 0600) ---------------------------------------------
META = "github"


def config(store):
    """{token (secret, never returned by the API), repos: ["owner/name"], login}. Read-only helper."""
    c = store._get_meta(META, None) or {}
    return {"token": c.get("token") or "", "repos": [r for r in c.get("repos") or [] if REPO_RE.fullmatch(str(r))],
            "checked_at": c.get("checked_at")}


def save_config(store, cfg):
    store._set_meta(META, {"token": cfg.get("token") or "", "repos": list(cfg.get("repos") or [])[:MAX_REPOS],
                           "checked_at": cfg.get("checked_at")})


def public(store):
    """What the page may see: whether a token is set (never the token), the followed repos."""
    c = config(store)
    return {"token_set": bool(c["token"]), "repos": c["repos"], "checked_at": c["checked_at"],
            "max_repos": MAX_REPOS}
