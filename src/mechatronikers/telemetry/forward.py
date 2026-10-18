"""Agent events to the host collector (docs/08): POST /ingest, with a local fallback.

The collector API runs in `session watch`. If it is not reachable, events go straight into the
same event store, so nothing is lost; the collector de-duplicates by emitter and sequence number.
`/ingest` takes host processes only: they send this host's key (`local_api.ingest_key`, #146).
"""

import sys
import time
from typing import TextIO

import httpx

from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.local_api import ingest_headers

RETRY_AFTER_S = 30.0


class CollectorStream:
    """A text stream for `EventEmitter`: each line is also sent to the collector."""

    def __init__(
        self,
        url: str,
        fallback: Collector | None = None,
        *,
        key: str,
        echo: TextIO | None = None,
        timeout_s: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/") + "/ingest"
        self.fallback = fallback
        self.echo = echo
        self._client = httpx.Client(
            timeout=timeout_s, transport=transport, headers=ingest_headers(key)
        )
        self._buffer = ""
        self._down_until = 0.0

    def write(self, text: str) -> int:
        if self.echo is not None:
            self.echo.write(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._send(line)
        return len(text)

    def flush(self) -> None:
        if self.echo is not None:
            self.echo.flush()

    def _send(self, line: str) -> None:
        if time.monotonic() >= self._down_until:
            try:
                self._client.post(self.url, content=line + "\n").raise_for_status()
                return
            except httpx.HTTPError as exc:
                self._down_until = time.monotonic() + RETRY_AFTER_S
                why = (
                    f"refused the events ({exc.response.status_code}; another --var-dir?)"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else "not reachable"
                )
                print(f"collector API at {self.url} {why}; storing events locally", file=sys.stderr)
        if self.fallback is not None:
            self.fallback.ingest_line(line, source="agent")

    def close(self) -> None:
        self._client.close()
