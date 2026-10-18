"""Wraps MCP tool functions so every call emits tool.called / tool.completed / tool.failed."""

import functools
import inspect
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic_core import to_jsonable_python

from mechatronikers.telemetry.events import EventEmitter

F = TypeVar("F", bound=Callable[..., Any])

_MAX_CHARS = 2000  # large payloads belong in artifacts, not events (docs/08)


def summarize(value: Any) -> Any:
    """JSON-safe, size-capped representation of tool inputs/outputs for events."""
    data = to_jsonable_python(value, fallback=repr)
    text = repr(data)
    if len(text) <= _MAX_CHARS:
        return data
    return {"truncated": True, "preview": text[:_MAX_CHARS], "chars": len(text)}


def instrument(emitter: EventEmitter, server: str) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        def called(kwargs: dict[str, Any]) -> tuple[str, float]:
            call_id = uuid.uuid4().hex[:12]
            emitter.emit(
                "tool.called",
                {
                    "server": server,
                    "tool": fn.__name__,
                    "call_id": call_id,
                    "input": summarize(kwargs),
                },
            )
            return call_id, time.monotonic()

        def completed(call_id: str, start: float, result: Any) -> None:
            emitter.emit(
                "tool.completed",
                {
                    "server": server,
                    "tool": fn.__name__,
                    "call_id": call_id,
                    "output": summarize(result),
                },
                duration_ms=(time.monotonic() - start) * 1000,
            )

        def failed(call_id: str, start: float, exc: Exception) -> None:
            emitter.emit(
                "tool.failed",
                {"server": server, "tool": fn.__name__, "call_id": call_id, "error": repr(exc)},
                level="error",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                call_id, start = called(kwargs)
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    failed(call_id, start, exc)
                    raise
                completed(call_id, start, result)
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            call_id, start = called(kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                failed(call_id, start, exc)
                raise
            completed(call_id, start, result)
            return result

        return wrapper  # type: ignore[return-value]

    return decorate
