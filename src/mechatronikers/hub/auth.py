"""Hub auth (D73, D82) and the cross-site rules of docs/13 §5, as one ASGI middleware.

Every request needs a credential; nothing is public, not even `/config.json` or `/assets`.

- **Browsers** share one credential (`MT_HUB_SHARED_TOKEN`): a token link `/?t=<token>` sets an
  HttpOnly `SameSite=Strict` cookie and redirects to the same page without the token (from another
  site: a page that reloads itself there, since a redirect would still be cross-site), or HTTP
  basic auth with the token as password (any user name: it is no identity, D82). Scripts may send
  it as `Authorization: Bearer`.
- **Workers** (the forwarder of `session watch --hub`) send their own bearer token. Worker routes
  (`/api/ingest`, `/api/worker/*`) accept worker tokens only, never a browser credential, and
  browser routes accept no worker token (except `GET /api/health`).
- **Cross-site requests:** browsers attach cookies and cached basic-auth credentials to cross-site
  requests, so a state-changing request (`POST`, `PUT`, `PATCH`, `DELETE`) with such a credential
  needs proof that it comes from the hub's own pages (`Origin`, else `Sec-Fetch-Site:
  same-origin`, else `Referer`) and `Content-Type: application/json` (else 415). Any request whose
  `Origin` names another site is refused, bearer or not.

Tokens are compared as SHA-256 digests in constant time and never logged.
"""

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, quote, urlencode

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mechatronikers.hub.records import ID

COOKIE = "mt_hub"
SHARED_TOKEN_ENV = "MT_HUB_SHARED_TOKEN"
WORKER_TOKENS_ENV = "MT_HUB_WORKER_TOKENS"  # "name=token,name=token"
ORIGIN_ENV = "MT_HUB_ORIGIN"  # public origin, e.g. https://hub.example.org (default: Host header)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{24,256}$")  # secrets.token_urlsafe(32) and the like
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),  # the token link must not leak in a Referer
    (b"x-frame-options", b"DENY"),
]

Via = Literal["cookie", "basic", "bearer", "worker"]
_SIGNING_IN = '<!doctype html><meta charset="utf-8"><title>Signing in</title><p>Signing in…</p>'


class AuthConfigError(ValueError):
    """The hub's tokens are missing or too weak; the message never contains a token."""


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def check_token(token: str, what: str) -> str:
    if not _TOKEN.match(token):
        raise AuthConfigError(
            f"{what} must be 24 to 256 characters of A-Z, a-z, 0-9, '-' or '_' "
            "(python -m mechatronikers.hub token)"
        )
    return token


def parse_worker_tokens(value: str) -> dict[str, str]:
    """`name=token,name=token` → {name: token}. Names are worker ids (the store's id charset)."""
    workers: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, sep, token = item.partition("=")
        if not sep or not ID.match(name):
            raise AuthConfigError(
                f"{WORKER_TOKENS_ENV}: expected name=token, with a name of [A-Za-z0-9._-]"
            )
        if name in workers:
            raise AuthConfigError(f"{WORKER_TOKENS_ENV}: worker {name} is listed twice")
        workers[name] = check_token(token, f"the token of worker {name}")
    return workers


@dataclass(frozen=True)
class HubAuth:
    """The hub's credentials as digests, and its public origin (None: from the Host header)."""

    shared: bytes
    workers: Mapping[str, bytes] = field(default_factory=dict)
    origin: str | None = None
    secrets_: tuple[str, ...] = field(default=(), repr=False)  # for the ingest secret scan

    @classmethod
    def create(
        cls, shared_token: str, workers: Mapping[str, str] | None = None, origin: str | None = None
    ) -> "HubAuth":
        shared = check_token(shared_token, SHARED_TOKEN_ENV)
        tokens = dict(workers or {})
        for name, token in tokens.items():
            check_token(token, f"the token of worker {name}")
            if hmac.compare_digest(_digest(token), _digest(shared)):
                raise AuthConfigError(f"worker {name} must not use the shared browser token")
        if len(set(tokens.values())) != len(tokens):
            raise AuthConfigError("two workers share one token")
        if origin is not None and not re.match(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$", origin):
            raise AuthConfigError(f"{ORIGIN_ENV} must look like https://host[:port]")
        return cls(
            shared=_digest(shared),
            workers={name: _digest(token) for name, token in tokens.items()},
            origin=origin,
            secrets_=(shared, *tokens.values()),
        )

    def is_shared(self, token: str) -> bool:
        return hmac.compare_digest(_digest(token), self.shared)

    def worker(self, token: str) -> str | None:
        digest = _digest(token)
        found = None
        for name, expected in self.workers.items():  # no early exit: same time for every name
            if hmac.compare_digest(digest, expected):
                found = name
        return found


def is_worker_route(path: str) -> bool:
    return path == "/api/ingest" or path.startswith("/api/worker/")


def _origin_of(scope: Scope, headers: Headers, configured: str | None) -> str:
    if configured:
        return configured
    scheme = scope.get("scheme", "http")
    return f"{scheme}://{headers.get('host', '')}"


def _bearer(headers: Headers) -> str | None:
    scheme, _, value = headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _basic_password(headers: Headers) -> str | None:
    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, sep, password = decoded.partition(":")
    return password if sep else None


def _cookie(headers: Headers) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == COOKIE:
            return value
    return None


def _denied(status: int, detail: str, *, basic: bool = False) -> Response:
    headers = {"WWW-Authenticate": 'Basic realm="mechatronikers hub", charset="UTF-8"'}
    return JSONResponse({"detail": detail}, status_code=status, headers=headers if basic else None)


def _same_origin(headers: Headers, origin: str) -> bool:
    if "origin" in headers:
        return headers["origin"] == origin
    if "sec-fetch-site" in headers:
        return headers["sec-fetch-site"] == "same-origin"
    referer = headers.get("referer")
    return referer is not None and (referer == origin or referer.startswith(origin + "/"))


class AuthMiddleware:
    """Authenticates every HTTP request and applies the cross-site rules (module docstring).
    Sets `request.state.worker` (worker routes) and `request.state.via`."""

    def __init__(self, app: ASGIApp, auth: HubAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response, via, worker = self._check(scope)
        if response is not None:
            await response(scope, receive, self._secured(send))
            return
        scope.setdefault("state", {})
        scope["state"]["via"] = via
        scope["state"]["worker"] = worker
        await self.app(scope, receive, self._secured(send))

    @staticmethod
    def _secured(send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in _SECURITY_HEADERS if n not in present]
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        return wrapped

    def _check(self, scope: Scope) -> tuple[Response | None, Via | None, str | None]:
        headers = Headers(scope=scope)
        path: str = scope["path"]
        method: str = scope["method"]
        origin = _origin_of(scope, headers, self.auth.origin)
        unsafe = method not in _SAFE_METHODS
        if unsafe and "origin" in headers and headers["origin"] != origin:
            return _denied(403, "cross-origin request refused"), None, None

        bearer = _bearer(headers)
        worker = self.auth.worker(bearer) if bearer else None
        if is_worker_route(path):
            if worker is None:
                return _denied(401, "a worker token is required"), None, None
            return None, "worker", worker
        if path == "/api/health" and worker is not None and not unsafe:
            return None, "worker", worker

        via: Via | None = None
        query = scope.get("query_string", b"").decode("latin-1")
        params = parse_qsl(query, keep_blank_values=True)
        link = next((v for k, v in params if k == "t"), None)
        if link is not None and method == "GET":
            if not self.auth.is_shared(link):
                return _denied(401, "invalid token link", basic=True), None, None
            cross_site = headers.get("sec-fetch-site") == "cross-site"
            return self._login(path, params, origin, link, cross_site), None, None
        if bearer is not None and self.auth.is_shared(bearer):
            via = "bearer"
        elif (password := _basic_password(headers)) is not None and self.auth.is_shared(password):
            via = "basic"
        elif (cookie := _cookie(headers)) is not None and self.auth.is_shared(cookie):
            via = "cookie"
        if via is None:
            return _denied(401, "authentication required", basic=True), None, None

        if unsafe and via in ("cookie", "basic"):
            # credentials a browser attaches by itself: prove the request comes from our pages
            if not _same_origin(headers, origin):
                return _denied(403, "cross-site request refused"), None, None
            media = headers.get("content-type", "").split(";")[0].strip().lower()
            if media != "application/json":
                return _denied(415, "Content-Type must be application/json"), None, None
        return None, via, None

    @staticmethod
    def _login(
        path: str,
        params: list[tuple[str, str]],
        origin: str,
        token: str,
        cross_site: bool = False,
    ) -> Response:
        """Token link: set the cookie, then show the same page without the token in the URL.

        After a navigation from another site (a link in mail or chat) the browser still treats a
        redirected request as cross-site and leaves out the `SameSite=Strict` cookie, so that
        case gets a small page whose `Refresh` header loads the clean URL (as the gateway, #132).
        """
        rest = urlencode([(k, v) for k, v in params if k != "t"])
        page = quote("/" + path.lstrip("/"), safe="/")  # "//host" would leave the hub
        target = page + (f"?{rest}" if rest else "")
        response: Response
        if cross_site:
            response = HTMLResponse(
                _SIGNING_IN,
                headers={
                    "Refresh": f"0; url={target}",
                    "Content-Security-Policy": "default-src 'none'",
                },
            )
        else:
            response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            path="/",
            httponly=True,
            samesite="strict",
            secure=origin.startswith("https://"),
        )
        return response
