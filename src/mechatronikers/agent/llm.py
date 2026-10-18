"""All model calls go through `ModelClient.ask`: budget check, call, cost record, events."""

import asyncio
import hashlib
import json
import math
import re
import time
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from mechatronikers.agent.budget import BudgetGuard
from mechatronikers.agent.config import AgentConfig
from mechatronikers.telemetry import EventEmitter

Block = dict[str, Any]

# Fallback when a provider reports no token counts at all, so the budget guard still counts the
# call: about 4 characters per token, and an image at the upper end of what providers bill.
_CHARS_PER_TOKEN = 4
_TOKENS_PER_IMAGE = 1600


# Output a timed-out attempt is charged with when MT_MODEL_MAX_OUTPUT_TOKENS is not set: the
# largest answer seen in the 2026-10-17 comparison (Gemini 3.8 Flash, 65.5k tokens), rounded up.
TIMEOUT_OUTPUT_TOKENS = 65_536
# Margin per attempt on the outer bound for the client's back-off between retries (the OpenAI
# and Anthropic SDKs wait at most 8 s).
_BACKOFF_S = 30.0


class ModelTimeoutError(RuntimeError):
    """A model call got no answer in time (#145); the step fails, the run ends with an error."""


def _is_timeout(exc: BaseException) -> bool:
    """asyncio's and httpx's timeouts, and a provider SDK's own (e.g. openai.APITimeoutError)
    without importing the SDK."""
    return isinstance(exc, TimeoutError | httpx.TimeoutException) or "Timeout" in type(exc).__name__


def create_model(config: AgentConfig) -> BaseChatModel:
    """The configured chat model via `init_chat_model` (#59). Default: OpenRouter's
    OpenAI-compatible API with OPENROUTER_API_KEY and the cost in the usage report. Other
    providers read their own key variable (e.g. ANTHROPIC_API_KEY) and need their LangChain
    integration package; their cost is estimated from token counts."""
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        # ChatOpenAI (OpenRouter) and the other integrations take both names (#145)
        "timeout": config.model_timeout_s,
        "max_retries": config.model_max_retries,
    }
    if config.max_output_tokens:
        kwargs["max_tokens"] = config.max_output_tokens
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.openrouter:
        if not config.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set (.env)")
        kwargs |= {"api_key": config.api_key, "extra_body": {"usage": {"include": True}}}
    model: BaseChatModel = init_chat_model(
        config.model, model_provider=config.model_provider, **kwargs
    )
    return model


def image_block(block: Block) -> Block:
    """MCP image content (as delivered by langchain-mcp-adapters) → OpenAI image_url block."""
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{block.get('mime_type', 'image/png')};base64,{block['base64']}"
        },
    }


def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def extract_json(text: str) -> Any:
    """First JSON object or array in a model answer (tolerates ```json fences and prose)."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    starts = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in model answer")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(candidate[min(starts) :])
    return value


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class ModelClient:
    def __init__(
        self, model: BaseChatModel, config: AgentConfig, budget: BudgetGuard, emitter: EventEmitter
    ) -> None:
        self.model = model
        self.config = config
        self.budget = budget
        self.emitter = emitter

    def _cost(
        self, message: AIMessage, system: str, content: list[Block]
    ) -> tuple[float, int, int, bool]:
        usage = message.usage_metadata
        if usage is not None:
            tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
        else:  # no token counts: estimate them, the guard must not see a free call
            text = len(system) + sum(len(str(b.get("text", ""))) for b in content)
            images = sum(1 for b in content if b.get("type") == "image_url")
            tokens_in = text // _CHARS_PER_TOKEN + images * _TOKENS_PER_IMAGE
            tokens_out = len(str(message.content)) // _CHARS_PER_TOKEN
        # OpenRouter reports the cost of the call; other providers report tokens only
        reported = (message.response_metadata.get("token_usage") or {}).get("cost")
        if isinstance(reported, int | float):
            return float(reported), tokens_in, tokens_out, True
        estimate = (
            tokens_in * self.config.price_input + tokens_out * self.config.price_output
        ) / 1e6
        return estimate, tokens_in, tokens_out, False

    def _ledger_events(self) -> None:
        """A spend ledger restored from its backup, or unreadable, is a run event (docs/08)."""
        for issue in self.budget.take_issues():
            self.emitter.emit(issue.type, issue.payload, level=issue.level)

    async def ask(self, step: str, system: str, content: list[Block]) -> str:
        """One model call for a graph step. Raises BudgetExceededError before calling if a
        limit is reached or the spend ledger cannot be read."""
        try:
            self.budget.check()
        finally:
            self._ledger_events()
        images = sum(1 for b in content if b.get("type") == "image_url")
        self.emitter.emit(
            "model.request",
            {
                "step": step,
                "model": self.config.model,
                "prompt": prompt_hash(system),
                "images": images,
            },
        )
        start = time.monotonic()
        messages = [SystemMessage(content=system), HumanMessage(content=content)]  # type: ignore[arg-type]
        try:
            # The client's own timeout and retries come first; this bounds the whole call even
            # for a provider that ignores them or a connection that stalls (#145).
            answer = await asyncio.wait_for(
                self.model.ainvoke(messages),
                timeout=self.deadline_s,
            )
        except Exception as exc:
            if not _is_timeout(exc):
                raise
            raise self._timed_out(step, system, content, exc, start) from exc
        assert isinstance(answer, AIMessage)
        cost, tokens_in, tokens_out, reported = self._cost(answer, system, content)
        self.budget.record(cost)
        self._ledger_events()
        self.emitter.emit(
            "model.response",
            {
                "step": step,
                "model": self.config.model,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cost": round(cost, 6),
                "cost_reported": reported,
                "run_spent": round(self.budget.run_spent, 6),
            },
            duration_ms=(time.monotonic() - start) * 1000,
        )
        return answer.text if isinstance(answer.text, str) else str(answer.content)

    @property
    def deadline_s(self) -> float:
        """The outer bound: every allowed attempt in full, plus the client's back-off between
        them, so it never cuts an attempt the client still allows."""
        attempts = self.config.model_max_retries + 1
        timeout = self.config.model_timeout_s
        return attempts * (timeout + min(_BACKOFF_S, timeout))

    def _timed_out(
        self, step: str, system: str, content: list[Block], exc: BaseException, start: float
    ) -> ModelTimeoutError:
        """`model.error` for the timed-out call. Every attempt that started may be billed in
        full, so each is charged its input (estimated from the prompt) plus the output ceiling
        (`max_output_tokens`, else TIMEOUT_OUTPUT_TOKENS)."""
        elapsed = time.monotonic() - start
        text = len(system) + sum(len(str(b.get("text", ""))) for b in content)
        images = sum(1 for b in content if b.get("type") == "image_url")
        tokens_in = text // _CHARS_PER_TOKEN + images * _TOKENS_PER_IMAGE
        tokens_out = self.config.max_output_tokens or TIMEOUT_OUTPUT_TOKENS
        allowed = self.config.model_max_retries + 1
        attempts = min(allowed, max(1, math.ceil(elapsed / self.config.model_timeout_s)))
        per_attempt = tokens_in * self.config.price_input + tokens_out * self.config.price_output
        cost = attempts * per_attempt / 1e6
        self.budget.record(cost)
        self._ledger_events()
        detail = str(exc) or f"no answer after {elapsed:.0f} s"
        error = f"no answer from {self.config.model} ({type(exc).__name__}: {detail})"
        self.emitter.emit(
            "model.error",
            {
                "step": step,
                "model": self.config.model,
                "error": error,
                "timeout": True,
                "timeout_s": self.config.model_timeout_s,
                "attempts": attempts,
                "input_tokens": tokens_in,  # estimated, per attempt
                "output_tokens": tokens_out,  # the ceiling, per attempt
                "cost": round(cost, 6),
                "cost_reported": False,  # a conservative estimate: each attempt billed in full
                "run_spent": round(self.budget.run_spent, 6),
            },
            level="error",
            duration_ms=elapsed * 1000,
        )
        return ModelTimeoutError(f"{step}: {error}")
