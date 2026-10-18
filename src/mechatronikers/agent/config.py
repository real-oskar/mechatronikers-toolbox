"""Agent configuration from the environment and an optional `.env` file (never committed)."""

import os
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Run language: the language of the texts the model writes for reviewers (#54, D78).
Language = Literal["de", "en"]
LANGUAGES: tuple[Language, ...] = get_args(Language)


def parse_language(value: str) -> Language:
    lang = value.strip().lower()
    if lang not in LANGUAGES:
        raise ValueError(f"unknown run language {value!r}: use one of {', '.join(LANGUAGES)}")
    return lang


def run_language(flag: str | None = None) -> Language:
    """Language of a new run: `--lang`, else MT_LANG, else en. MT_LANG is read only here, when a
    run starts without the flag, so a bad value never stops `resume` or `spend`."""
    return parse_language(flag or os.environ.get("MT_LANG") or "en")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader: KEY=VALUE lines; existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AgentConfig(BaseModel):
    model: str  # model id at the provider, e.g. from https://openrouter.ai/models (needs images)
    # `init_chat_model` provider (#59); "openai" with no base URL is OpenRouter's OpenAI API.
    model_provider: str = "openai"
    base_url: str | None = None  # None: OpenRouter for "openai", else the provider's default
    api_key: str = Field(default="", repr=False)  # OPENROUTER_API_KEY, sent only to OpenRouter
    budget_run: float = 1.0  # credits (USD on OpenRouter) one run may spend
    budget_total: float = 5.0  # credits all runs together may spend (ledger file)
    ledger: Path = Path("var/agent-spend.json")
    # Fallback prices per million tokens, used when the provider reports no cost.
    price_input: float = 3.0
    price_output: float = 15.0
    max_fix_iterations: int = 3
    # A model request that hangs must end as an error, never block a run (#145): the client's
    # timeout per attempt and its retries; ModelClient also bounds the whole call. Real calls
    # take long: up to 779 s (non-streaming, 65.5k output tokens) in the 2026-10-17 comparison.
    model_timeout_s: float = 900.0
    model_max_retries: int = 2
    # Sent as max_tokens when set; also the output a timed-out attempt is charged with.
    max_output_tokens: int | None = None
    tool_timeout_s: float = 600.0  # one MCP tool call into the sandbox (builds take up to 120 s)
    temperature: float = 0.0
    lang: Language = "en"  # set for a new run with run_language(); a resumed run has its own

    @model_validator(mode="after")
    def _openrouter_by_default(self) -> "AgentConfig":
        if self.base_url is None and self.model_provider == "openai":
            self.base_url = OPENROUTER_URL
        return self

    @property
    def openrouter(self) -> bool:
        """OpenRouter's OpenAI-compatible API: it gets OPENROUTER_API_KEY and reports the cost."""
        host = urlparse(self.base_url or "").hostname
        return self.model_provider == "openai" and host == "openrouter.ai"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        load_dotenv()
        env = os.environ
        if "MT_MODEL" not in env:
            raise ValueError("set MT_MODEL (a model id with image support) in .env")
        return cls(
            model=env["MT_MODEL"],
            # init_chat_model normalises provider names the same way
            model_provider=env.get("MT_MODEL_PROVIDER", "").strip().lower().replace("-", "_")
            or "openai",
            base_url=env.get("MT_BASE_URL") or None,
            api_key=env.get("OPENROUTER_API_KEY", ""),
            budget_run=float(env.get("MT_BUDGET_RUN", "1.0")),
            budget_total=float(env.get("MT_BUDGET_TOTAL", "5.0")),
            price_input=float(env.get("MT_PRICE_INPUT", "3.0")),
            price_output=float(env.get("MT_PRICE_OUTPUT", "15.0")),
            max_fix_iterations=int(env.get("MT_MAX_FIX_ITERATIONS", "3")),
            model_timeout_s=float(env.get("MT_MODEL_TIMEOUT_S", "900")),
            model_max_retries=int(env.get("MT_MODEL_MAX_RETRIES", "2")),
            max_output_tokens=int(env["MT_MODEL_MAX_OUTPUT_TOKENS"])
            if env.get("MT_MODEL_MAX_OUTPUT_TOKENS")
            else None,
            tool_timeout_s=float(env.get("MT_TOOL_TIMEOUT_S", "600")),
        )
