"""Event schema and emitter. Inside sandboxes, events go to stdout as NDJSON (docs/08)."""

import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

EVENT_SCHEMA = "mechatronikers.event/1"

Level = Literal["debug", "info", "warning", "error"]
# Where the hub's forwarder read a line (docs/13 §1, D86): a host process or a sandbox container.
Source = Literal["host", "container"]


def utc_now() -> str:
    """RFC 3339 UTC timestamp with microseconds."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TextSink(Protocol):
    """Where events go: stdout, a file, or a forwarder to the collector."""

    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


class ArtifactRef(BaseModel):
    path: str
    sha256: str


class Event(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default=EVENT_SCHEMA, alias="schema")
    ts: str
    mono_ns: int
    recv_ts: str | None = None  # set by the collector
    # Set by the hub's ingest from the channel the forwarder names, never taken from the line
    # itself; state-driving events count only from "host" (docs/13 §1). Unset in mode 1.
    source: Source | None = None
    seq: int
    emitter_id: str | None = None  # one per emitter instance; seq increases per emitter
    session_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    component: str
    type: str
    level: Level = "info"
    duration_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True)


class EventEmitter:
    """Writes one event per line. Sequence numbers are per emitter and strictly increasing."""

    def __init__(
        self,
        component: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        stream: TextSink | None = None,
    ) -> None:
        self.component = component
        self.session_id = session_id
        self.run_id = run_id
        self._stream = stream
        self.emitter_id = uuid.uuid4().hex[:12]
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def stream(self) -> TextSink:
        return self._stream or sys.stdout

    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        level: Level = "info",
        duration_ms: float | None = None,
        artifacts: list[ArtifactRef] | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                ts=utc_now(),
                mono_ns=time.monotonic_ns(),
                seq=self._seq,
                emitter_id=self.emitter_id,
                session_id=self.session_id,
                run_id=self.run_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                component=self.component,
                type=type,
                level=level,
                duration_ms=duration_ms,
                payload=payload or {},
                artifacts=artifacts or [],
            )
            self.stream.write(event.to_json() + "\n")
            self.stream.flush()
        return event
