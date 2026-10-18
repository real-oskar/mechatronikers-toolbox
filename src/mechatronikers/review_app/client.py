"""The run drivers' client for the review API (D62, D75): open a gate, wait for its decision.

`base_url` is the run base `R` of docs/13: the session gateway's `/api` in mode 1
(`gateway_review_url`), `/api/runs/<run_id>` at the hub in mode 2 (#76). Gates and decisions are
the same models either way, so a driver switches modes by its base URL and token only. The agent
and the circuit run reach gates only through this client.
"""

import asyncio
import sys
from types import TracebackType

import httpx

from mechatronikers.review_app.trust import warn_if_untrusted
from mechatronikers.spec.review import GateDecision, GateRequest, GateState
from mechatronikers.telemetry import EventEmitter

POLL_S = 2.0
TIMEOUT_S = 10.0
OPEN_ATTEMPTS = 6  # with doubling backoff from 1 s: about half a minute of outage
BACKOFF_S = 1.0
# The run is gone or no longer this driver's (token revoked, run deleted): waiting cannot help.
FINAL_STATUS = (401, 403, 404, 410)


def _retryable(exc: httpx.HTTPError) -> bool:
    """Connection problems and server errors pass; a refused request does not."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return True


def gateway_review_url(gateway_url: str) -> str:
    """The review API's base behind a session gateway (mode 1)."""
    return f"{gateway_url.rstrip('/')}/api"


def review_access(
    gateway_url: str,
    gateway_token: str,
    review_url: str | None = None,
    review_token: str | None = None,
) -> tuple[str, str]:
    """Base URL and token of a run's review API: the session gateway's with the gateway token by
    default, else `review_url` (the hub, mode 2) with its own token. The gateway token never
    leaves the worker host (docs/13, auth), so it is never sent to another base."""
    if review_url is None:
        if review_token is not None:
            raise ValueError("a review token needs the review URL it belongs to")
        return gateway_review_url(gateway_url), gateway_token
    if not review_token:
        raise ValueError("a review URL needs its own token; the gateway token stays on the host")
    return review_url, review_token


class GateClient:
    """Gates of one run, through the review API at `base_url` with a bearer token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        poll_s: float = POLL_S,
        timeout: float = TIMEOUT_S,
        open_attempts: int = OPEN_ATTEMPTS,
        backoff_s: float = BACKOFF_S,
        transport: httpx.AsyncBaseTransport | None = None,
        emitter: EventEmitter | None = None,
    ) -> None:
        """`emitter` gets `agent.decision_untrusted` for a decision read in a run whose builds
        ran without sandbox isolation (D95)."""
        self.base_url = base_url.rstrip("/")
        self.emitter = emitter
        self.poll_s = poll_s
        self.open_attempts = max(1, open_attempts)
        self.backoff_s = backoff_s
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "GateClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def open(self, request: GateRequest) -> GateState:
        """Open a gate. Idempotent (D80): a restarted driver gets the stored state back, so a
        short outage of the review API is retried with backoff; a refused request is not."""
        delay = self.backoff_s
        for attempt in range(1, self.open_attempts + 1):
            try:
                response = await self._client.post(
                    "/review/gates",
                    content=request.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if attempt == self.open_attempts or not _retryable(exc):
                    raise
                print(f"review API not reachable ({exc}); retrying in {delay:g} s", file=sys.stderr)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return GateState.model_validate(response.json())
        raise AssertionError("unreachable")

    async def get(self, gate_id: str) -> GateState:
        response = await self._client.get(f"/review/gates/{gate_id}")
        response.raise_for_status()
        return GateState.model_validate(response.json())

    async def gates(self) -> list[GateState]:
        response = await self._client.get("/review/gates")
        response.raise_for_status()
        return [GateState.model_validate(g) for g in response.json()]

    async def submit(self, decision: GateDecision) -> GateState:
        """Record a decision (the terminal's `agent gate`; reviewers use the review UI)."""
        response = await self._client.post(
            f"/review/gates/{decision.gate_id}/decision",
            content=decision.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return GateState.model_validate(response.json())

    async def wait(self, gate_id: str) -> GateDecision:
        """Poll an open gate until it is decided. A gate can wait for hours: outages and other
        errors are reported and polling goes on, except when the run is gone or no longer this
        driver's (401, 403, 404, 410), which raises the HTTPStatusError."""
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                state = await self.get(gate_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in FINAL_STATUS:
                    raise
                print(f"review API not reachable ({exc}); retrying", file=sys.stderr)
                continue
            except httpx.HTTPError as exc:
                print(f"review API not reachable ({exc}); retrying", file=sys.stderr)
                continue
            if state.decision is not None:
                warn_if_untrusted(state, self.emitter)  # the run goes on, flagged (D95)
                return state.decision

    async def decide(self, request: GateRequest) -> GateDecision:
        """Open the gate and wait for the reviewer's decision (the agent's `ReviewClient`)."""
        state = await self.open(request)
        if state.decision is not None:
            warn_if_untrusted(state, self.emitter)
            return state.decision
        return await self.wait(request.id)
