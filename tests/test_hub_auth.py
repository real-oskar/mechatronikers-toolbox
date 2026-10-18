"""Hub auth (D73, D82) and the cross-site rules of docs/13 §5."""

import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.routing import Mount, Route

from mechatronikers.hub.auth import (
    COOKIE,
    AuthConfigError,
    AuthMiddleware,
    HubAuth,
    parse_worker_tokens,
)
from tests.fixtures.hub import (
    BACKENDS,
    BROWSER,
    ORIGIN,
    SHARED,
    WORKER_A,
    WORKERS,
    Hub,
    _stores,
    bearer,
    hub,
)


@pytest.fixture(params=BACKENDS)
def h(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Hub]:
    with hub(tmp_path, request.param) as h:
        yield h


def _basic(password: str, user: str = "anna") -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _endpoints(h: Hub) -> list[tuple[str, str]]:
    """Every route of the app (path parameters filled in), plus paths no route serves."""
    found = [("GET", "/nowhere"), ("POST", "/nowhere"), ("GET", "/assets/index.js")]
    for route in h.client.app.routes:  # type: ignore[attr-defined]
        if isinstance(route, Mount):
            found.append(("GET", route.path + "/x.js"))
        elif isinstance(route, Route):
            path = re.sub(r"\{[^}]+\}", "run-x", route.path)
            found += [(m, path) for m in sorted(route.methods or {"GET"}) if m != "HEAD"]
    return found


def test_every_endpoint_needs_a_credential(h: Hub) -> None:
    endpoints = _endpoints(h)
    assert ("POST", "/api/ingest") in endpoints and ("GET", "/config.json") in endpoints
    for method, path in endpoints:
        for headers in ({}, bearer("wrong-token-0123456789abcdef"), _basic("wrong")):
            response = h.client.request(method, path, headers=headers)
            assert response.status_code == 401, (method, path, headers)
            assert SHARED not in response.text


def test_browser_credentials(h: Hub) -> None:
    for headers in (BROWSER, _basic(SHARED), _basic(SHARED, user="")):
        assert h.client.get("/api/runs", headers=headers).status_code == 200
    h.client.cookies.set(COOKIE, SHARED)
    assert h.client.get("/api/runs").status_code == 200
    assert h.client.get("/config.json").json()["mode"] == "hub"
    assert h.client.get("/runs/run-x").status_code == 200  # the SPA's run view
    h.client.cookies.clear()
    # a browser without credentials is asked for basic auth
    response = h.client.get("/")
    assert response.status_code == 401 and "Basic" in response.headers["www-authenticate"]


def test_token_link_sets_a_strict_cookie_and_leaves_the_url(h: Hub) -> None:
    response = h.client.get(f"/runs/run-x?t={SHARED}&lang=de", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/runs/run-x?lang=de"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={SHARED}") and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower() and "Secure" not in cookie  # http here
    assert response.headers["referrer-policy"] == "no-referrer"
    assert h.client.get("/api/runs").status_code == 200  # the cookie works
    # a token link never redirects to another host
    away = AuthMiddleware._login("//evil.example/x", [("t", SHARED)], ORIGIN, SHARED)
    assert away.headers["location"] == "/evil.example/x"
    h.client.cookies.clear()
    assert h.client.get("/?t=wrong", follow_redirects=False).status_code == 401


def test_a_token_link_from_another_site_signs_in_with_a_refresh_page(h: Hub) -> None:
    """After a cross-site navigation a 303 would still be cross-site, and the browser would
    leave out the Strict cookie (#132): a small page reloads the clean URL instead."""
    page = h.client.get(
        f"/runs/run-x?t={SHARED}&lang=de",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
        follow_redirects=False,
    )
    assert page.status_code == 200 and page.headers["refresh"] == "0; url=/runs/run-x?lang=de"
    assert SHARED not in page.text and SHARED not in page.headers["refresh"]
    assert page.headers["content-security-policy"] == "default-src 'none'"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "samesite=strict" in page.headers["set-cookie"].lower()


def test_workers_and_browsers_keep_to_their_routes(h: Hub) -> None:
    ndjson = {"Content-Type": "application/x-ndjson"}
    assert h.client.get("/api/runs", headers=WORKER_A).status_code == 401
    assert h.client.get("/", headers=WORKER_A).status_code == 401
    for headers in (BROWSER, _basic(SHARED)):
        r = h.client.post("/api/ingest", content=b"", headers={**headers, **ndjson})
        assert r.status_code == 401
    h.client.cookies.set(COOKIE, SHARED)
    r = h.client.post("/api/ingest", content=b"", headers={**ndjson, "Origin": ORIGIN})
    assert r.status_code == 401
    h.client.cookies.clear()
    assert h.client.post("/api/ingest", content=b"", headers={**WORKER_A, **ndjson}).json() == {
        "stored": 0,
        "duplicates": 0,
        "rejected": [],
    }
    assert h.client.get("/api/health", headers=WORKER_A).json()["worker"] == "worker-a"
    assert h.client.get("/api/health", headers=BROWSER).json()["ok"] is True


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),  # no proof of origin
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Referer": "http://evil.example/page"}, 403),
        ({"Referer": "http://testserver.evil.example/"}, 403),
        ({"Origin": ORIGIN, "Content-Type": "text/plain"}, 415),
        ({"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}, 415),
        ({"Origin": ORIGIN, "Content-Type": "multipart/form-data; boundary=x"}, 415),
        # same origin and JSON pass the checks; v0 has no browser route that takes a POST
        ({"Origin": ORIGIN, "Content-Type": "application/json"}, 405),
        ({"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}, 405),
        ({"Referer": ORIGIN + "/runs/x", "Content-Type": "application/json"}, 405),
    ],
)
def test_cross_site_rules_for_browser_credentials(
    h: Hub, headers: dict[str, str], status: int
) -> None:
    for credential in ("cookie", "basic"):
        h.client.cookies.clear()
        auth = _basic(SHARED) if credential == "basic" else {}
        if credential == "cookie":
            h.client.cookies.set(COOKIE, SHARED)
        r = h.client.post("/api/runs", content=b"{}", headers={**auth, **headers})
        assert r.status_code == status, (credential, headers)


def test_a_foreign_origin_is_refused_with_any_credential(h: Hub) -> None:
    evil = {"Origin": "http://evil.example", "Content-Type": "application/x-ndjson"}
    assert (
        h.client.post("/api/ingest", content=b"", headers={**WORKER_A, **evil}).status_code == 403
    )
    r = h.client.post("/api/runs", content=b"{}", headers={**BROWSER, **evil})
    assert r.status_code == 403
    # a bearer token is never attached by a browser: no origin proof needed
    r = h.client.post("/api/runs", headers={**BROWSER, "Content-Type": "application/json"})
    assert r.status_code == 405


def test_security_headers(h: Hub) -> None:
    for path in ("/api/runs", "/"):
        response = h.client.get(path, headers=BROWSER)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert h.client.get("/api/runs", headers=BROWSER).headers["cache-control"] == "no-store"
    assert (
        "script-src 'self'" in h.client.get("/", headers=BROWSER).headers["content-security-policy"]
    )


def test_configured_origin(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from mechatronikers.hub.app import create_hub_app

    auth = HubAuth.create(SHARED, WORKERS, "https://hub.example.org")
    with _stores(tmp_path, "sqlite") as (events, runs, _):
        client = TestClient(create_hub_app(events=events, runs=runs, auth=auth))
        json_body = {"Content-Type": "application/json"}
        r = client.post("/api/runs", headers={**_basic(SHARED), **json_body, "Origin": ORIGIN})
        assert r.status_code == 403  # the Host header does not count once the origin is set
        r = client.post(
            "/api/runs",
            headers={**_basic(SHARED), **json_body, "Origin": "https://hub.example.org"},
        )
        assert r.status_code == 405
        login = client.get(f"/?t={SHARED}", follow_redirects=False)
        assert "Secure" in login.headers["set-cookie"]


def test_token_configuration() -> None:
    assert parse_worker_tokens(" a=" + "x" * 24 + ", b.2=" + "y" * 24) == {
        "a": "x" * 24,
        "b.2": "y" * 24,
    }
    assert parse_worker_tokens("") == {}
    for bad in ("a", "a=short", "a b=" + "x" * 24, "a=" + "x" * 24 + ",a=" + "y" * 24):
        with pytest.raises(AuthConfigError) as info:
            parse_worker_tokens(bad)
        assert "x" * 24 not in str(info.value)
    with pytest.raises(AuthConfigError):
        HubAuth.create("too-short")
    with pytest.raises(AuthConfigError):
        HubAuth.create(SHARED, {"w": SHARED})  # a worker must not hold the browser credential
    with pytest.raises(AuthConfigError):
        HubAuth.create(SHARED, {"a": "z" * 24, "b": "z" * 24})
    with pytest.raises(AuthConfigError):
        HubAuth.create(SHARED, origin="hub.example.org/path")
    auth = HubAuth.create(SHARED, WORKERS)
    assert auth.worker(WORKERS["worker-b"]) == "worker-b" and auth.worker(SHARED) is None
    assert auth.is_shared(SHARED) and not auth.is_shared(WORKERS["worker-a"])
    assert SHARED not in repr(auth)
