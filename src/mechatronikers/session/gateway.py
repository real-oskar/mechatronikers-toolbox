"""Per-session gateway: Caddy in front of the session container, token-protected (docs/06, 07)."""

import hashlib
import hmac
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

GATEWAY_IMAGE = "docker.io/library/caddy:2.8.4-alpine"
GATEWAY_PORT = 8080
TOKEN_COOKIE = "mt_token"
# The host collector API (`session watch`), as seen from the gateway container. On macOS (Podman
# VM) host.containers.internal reaches the host's 127.0.0.1 (D61).
COLLECTOR_PORT = 8765  # `session watch --port` default
DEFAULT_COLLECTOR = f"host.containers.internal:{COLLECTOR_PORT}"
# Rootless Linux (pasta) does not map host loopback, so there the collector also listens on a Unix
# socket whose directory is mounted read-only into the gateway (D83, #53).
COLLECTOR_SOCKET = "collector.sock"
GATEWAY_SOCKET_DIR = "/run/mt-collector"
TRANSPORT_ENV = "MT_COLLECTOR_TRANSPORT"  # "unix" or "tcp"; default by platform
SOCKET_DIR_ENV = "MT_COLLECTOR_SOCKET_DIR"  # host directory that holds only the socket
_SESSION_ID = re.compile(r"^ses-[a-z0-9]{6,32}$")
_COLLECTOR = re.compile(r"^(?:[A-Za-z0-9.-]+:\d{1,5}|unix//[A-Za-z0-9._/-]+)$")


@dataclass(frozen=True)
class CollectorRoute:
    """How the gateway reaches the host collector: a Caddy upstream, plus the host directory with
    the collector socket that is mounted into the gateway (None for TCP)."""

    upstream: str
    socket_dir: Path | None = None

    @property
    def socket(self) -> Path | None:
        """The socket `session watch` listens on (host path)."""
        return self.socket_dir / COLLECTOR_SOCKET if self.socket_dir else None


def collector_route(
    var_dir: Path, environ: Mapping[str, str] | None = None, platform: str | None = None
) -> CollectorRoute:
    """Unix socket on Linux, `host.containers.internal` elsewhere; `MT_COLLECTOR_TRANSPORT`
    and `MT_COLLECTOR_SOCKET_DIR` override (tests, other setups).

    The socket lives in `<var>/run/collector/` by default, so `session watch` and `session start`
    agree on it as long as they share the var directory (as they already do for `state.db`).
    """
    env = os.environ if environ is None else environ
    transport = env.get(TRANSPORT_ENV) or (
        "unix" if (platform or sys.platform).startswith("linux") else "tcp"
    )
    if transport == "tcp":
        return CollectorRoute(DEFAULT_COLLECTOR)
    if transport != "unix":
        raise ValueError(f"{TRANSPORT_ENV} must be 'unix' or 'tcp', not {transport!r}")
    # absolute(), not resolve(): a symlinked socket directory must stay visible to the check below
    socket_dir = Path(env.get(SOCKET_DIR_ENV) or var_dir / "run" / "collector").absolute()
    return CollectorRoute(f"unix/{GATEWAY_SOCKET_DIR}/{COLLECTOR_SOCKET}", socket_dir)


def ensure_socket_dir(path: Path) -> None:
    """Create the socket directory (0700) if missing, then check it (see `check_socket_dir`)."""
    try:
        path.mkdir(mode=0o700, parents=True)
        path.chmod(0o700)  # created here: the umask must not change the mode
    except FileExistsError:
        pass
    check_socket_dir(path)


def check_socket_dir(path: Path) -> None:
    """The whole directory is mounted into the gateway, and whoever controls it controls what the
    gateway proxies to: it must be a real directory of this user, 0700, holding only the socket."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"collector socket directory {path} is not a directory (or is a symlink)")
    if info.st_uid != os.getuid():
        raise ValueError(f"collector socket directory {path} belongs to another user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(
            f"collector socket directory {path} has mode {stat.S_IMODE(info.st_mode):o}, not 700"
        )
    for entry in path.iterdir():
        if entry.name != COLLECTOR_SOCKET or not stat.S_ISSOCK(entry.lstat().st_mode):
            raise ValueError(f"collector socket directory {path} holds more than the socket")


# Shown for `?t=` in gateway log lines (the URI and the Referer would otherwise carry the token).
LOG_MASK = "REDACTED"
# Answer to a cross-site `?t=` navigation: it sets the cookie, then the `Refresh` header loads the
# token-free URL. A 303 would not do: the redirected request still counts as cross-site, so the
# browser leaves out the `SameSite=Strict` cookie and the gateway answers 401.
_SIGNING_IN = '<!doctype html><meta charset="utf-8"><title>Signing in</title><p>Signing in…</p>'


def caddyfile(
    token: str, upstream: str, session_id: str, collector: str = DEFAULT_COLLECTOR
) -> str:
    """Accepts the token as `?t=` (sets a cookie), as cookie, or as `Authorization: Bearer`.

    A browser that opens the review link (`?t=`, a top-level GET navigation) gets the cookie and
    goes on to the same URL without `t` (#116): a 303, or for a link opened from another site a
    page that reloads itself there (see `_SIGNING_IN`). Other `?t=` requests are served as before.
    The gateway's log lines show `t=REDACTED` and no Referer, so the token never reaches the
    collected events.

    `/events/history` and `/events/stream` reach the host collector for this session only: the
    session id is fixed here, so a token can never read another session's events. The token is
    stripped before the request leaves the gateway. `collector` is `host:port` or `unix//path`.
    """
    if not token.isalnum():
        raise ValueError("token must be alphanumeric")
    if not _SESSION_ID.match(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    if not _COLLECTOR.match(collector):
        raise ValueError(f"invalid collector address {collector!r}")
    return f"""{{
	admin off
	auto_https off
	log {{
		format filter {{
			wrap json
			fields {{
				request>uri query {{
					replace t {LOG_MASK}
				}}
				request>headers>Referer delete
			}}
		}}
	}}
}}

:{GATEWAY_PORT} {{
	@authed expression `{{http.request.uri.query.t}} == "{token}" || {{http.request.header.Authorization}} == "Bearer {token}" || {{http.request.cookie.{TOKEN_COOKIE}}} == "{token}"`
	handle @authed {{
		@first query t={token}
		header @first Set-Cookie "{TOKEN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
		@login {{
			method GET HEAD
			query t={token}
			header Sec-Fetch-Mode navigate
		}}
		handle @login {{
			route {{
				uri query -t
				uri path_regexp ^/+ /
				header Cache-Control no-store
				header Referrer-Policy no-referrer
				@cross_site header Sec-Fetch-Site cross-site
				header @cross_site Refresh "0; url={{uri}}"
				header @cross_site Content-Type "text/html; charset=utf-8"
				header @cross_site Content-Security-Policy "default-src 'none'"
				respond @cross_site `{_SIGNING_IN}` 200
				redir {{uri}} 303
			}}
		}}
		handle /events/history {{
			rewrite * /sessions/{session_id}/events?after={{http.request.uri.query.after}}
			reverse_proxy {collector} {{
				header_up -Authorization
				header_up -Cookie
			}}
		}}
		handle /events/stream {{
			rewrite * /sessions/{session_id}/stream?after={{http.request.uri.query.after}}
			reverse_proxy {collector} {{
				header_up -Authorization
				header_up -Cookie
				flush_interval -1
			}}
		}}
		handle {{
			reverse_proxy {upstream} {{
				header_up {GATEWAY_HEADER} {gateway_secret(token)}
				flush_interval -1
			}}
		}}
	}}
	handle {{
		respond "unauthorized" 401
	}}
}}
"""


# The gateway sets this header on every request it forwards to the session container, which
# accepts no request without it (D95): a request that did not pass the token check (e.g. from a
# build script inside the container) cannot reach the review API or the MCP servers.
GATEWAY_HEADER = "X-MT-Gateway"
GATEWAY_SECRET_ENV = "MT_GATEWAY_SECRET"


def gateway_secret(token: str) -> str:
    """The header value for a session: derived from its token, so a leaked value does not open
    the gateway, and nothing new needs storing."""
    return hmac.new(token.encode(), b"mt-gateway-header", hashlib.sha256).hexdigest()
