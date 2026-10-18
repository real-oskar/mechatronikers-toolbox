"""Guards for the host's local HTTP APIs: `session watch` and the circuit dev server (#146, D103).

They listen on 127.0.0.1 only, but every web page the user opens can still send requests there
(a sandbox's review page too), and a DNS-rebinding page can even read the answers. Three rules:

- **Loopback `Host` only** on every request: a rebinding page's requests carry its own host name.
- **No cross-site browser request changes state:** a non-GET request whose `Origin` is not this
  server, or whose `Sec-Fetch-Site` is not `same-origin`/`none`, is refused. Host processes (the
  run drivers, scripts) send neither header and are not affected.
- **`/ingest` needs this host's key**, because what it stores is trusted as a host process's
  events (D86): the run drivers send `<var>/ingest.key` in a header, with a non-simple content
  type, so a browser could not even send it without a CORS preflight, which is never granted.
"""

import hmac
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

INGEST_KEY_FILE = "ingest.key"
INGEST_KEY_HEADER = "X-MT-Ingest-Key"
INGEST_CONTENT_TYPE = "application/x-ndjson"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def ingest_key(var_dir: Path) -> str:
    """This host's ingest key, `<var>/ingest.key` (0600), created on first use. `session watch`
    and the run drivers find the same key because they share the var directory, as they already
    do for `state.db`."""
    var_dir.mkdir(parents=True, exist_ok=True)
    path = var_dir / INGEST_KEY_FILE
    if not path.exists() and not path.is_symlink():
        # Written under a temporary name, then linked into place: a reader never sees a partial
        # key, and of two concurrent creators the first one wins.
        tmp = var_dir / f".{INGEST_KEY_FILE}.{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{path} must be a regular file of this user (not a link)")
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(0o600)
    key = path.read_text().strip()
    if len(key) < 32:
        raise ValueError(f"{path} holds no valid ingest key; delete it to create a new one")
    return key


def host_name(header: str | None) -> str | None:
    """`127.0.0.1` of `127.0.0.1:8765`, `[::1]` of `[::1]:8765`; None for a malformed header."""
    if not header:
        return None
    if header.startswith("["):
        name, close, rest = header.partition("]")
        if not close:
            return None
        name += close
    else:
        name, sep, port = header.partition(":")
        rest = sep + port
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return None
    return name.lower()


def cross_site(headers: Headers) -> bool:
    """The browser says the request comes from another page than this server's own."""
    origin = headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.netloc != headers.get("host", ""):
            return True
    site = headers.get("sec-fetch-site")
    return site is not None and site not in ("same-origin", "none")


def from_own_page(headers: Headers) -> bool:
    """Stricter than "not cross-site", for routes only this server's own page may call (the run
    table's shelve, #52): the browser must say so, with `Origin` or `Sec-Fetch-Site:
    same-origin`. A request with neither header (a script) is refused; the CLI is there for that."""
    if cross_site(headers):
        return False
    return headers.get("origin") is not None or headers.get("sec-fetch-site") == "same-origin"


class LocalOnly:
    """ASGI middleware for a local API: loopback `Host` on every request, and no state change
    from a cross-site browser request (see the module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            if host_name(headers.get("host")) not in LOOPBACK_HOSTS:
                await PlainTextResponse("Invalid host header", 400)(scope, receive, send)
                return
            unsafe = scope["type"] == "websocket" or scope["method"] not in _SAFE_METHODS
            if unsafe and cross_site(headers):
                await PlainTextResponse("Cross-site request refused", 403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def refuse_ingest(request: Request, key: str | None) -> JSONResponse | None:
    """Why an `/ingest` request is refused, or None: it needs the content type and this host's
    key (constant-time compare). Without a configured key nothing is accepted."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != INGEST_CONTENT_TYPE:
        return JSONResponse({"detail": f"expected {INGEST_CONTENT_TYPE}"}, status_code=415)
    sent = request.headers.get(INGEST_KEY_HEADER)
    if sent is None:
        return JSONResponse({"detail": f"missing {INGEST_KEY_HEADER}"}, status_code=401)
    if key is None or not hmac.compare_digest(sent.encode(), key.encode()):
        return JSONResponse({"detail": "wrong ingest key"}, status_code=403)
    return None


def ingest_headers(key: str) -> dict[str, str]:
    """What a run driver sends with each `/ingest` request."""
    return {INGEST_KEY_HEADER: key, "Content-Type": INGEST_CONTENT_TYPE}
