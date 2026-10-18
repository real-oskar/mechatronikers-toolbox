"""The configured vision model reads text crops of a raster sheet (raster path, T12: E3 and E4).

Crops come from drawing-mcp's offline text detector, so the model sees small crops, never the
whole sheet. Every call goes through `ModelClient.ask` (budget check, cost recorded in the
ledger, model events). Before each call the reader reserves the call's cost and makes the call
only if the reservation still fits the run budget. The cost is not known before the answer, so
the reservation is an estimate: the first call is priced pessimistically (images billed by
tiles), later calls at the highest ratio of reported to estimated cost seen so far, with
headroom. A model much pricier than the configured fallback prices can still pass the cap by
part of one call. A dry run makes no calls and adds up the estimates instead.
"""

import asyncio
import base64
import math
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from mechatronikers.agent import prompts
from mechatronikers.agent.budget import BudgetExceededError
from mechatronikers.agent.config import AgentConfig
from mechatronikers.agent.llm import ModelClient, extract_json, text_block

T = TypeVar("T")

BATCH = 24  # crops per call, as the label reading of the vector path
MAX_TEXT = 200  # characters kept of one reading (model output is untrusted)

# Token estimate per call. Image tokens differ by provider: Anthropic-style billing counts
# pixels (w × h / 750), OpenAI-style counts 512-px tiles (85 + 170 per tile, at least 255 for a
# small crop). The pixel count is the estimate; the tile count is kept as a pessimistic bound.
PIXELS_PER_TOKEN = 750
MAX_LONG_SIDE = 1568
TOKENS_PER_CROP_TEXT = 6  # the "crop c12" line before each image
TOKENS_CALL_OVERHEAD = 40  # instruction line and message framing
TOKENS_OUT_PER_CROP = 10  # "c12": "47nF",
TOKENS_OUT_BASE = 12
RESERVE_MARGIN = 1.5  # headroom over the highest reported/estimated cost ratio seen so far


@dataclass(frozen=True)
class Crop:
    id: str
    png: bytes
    size: tuple[int, int]  # px


class CropReading(BaseModel):
    id: str
    text: str | None = None  # None: not read (unreadable, or the model skipped it)
    lettering: bool = True  # False: the model saw no text (line art, symbol parts)
    answered: bool = True  # False: no answer for this crop, even after a retry


class CallEstimate(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_in_tiles: int = 0  # same call billed by tiles
    credits: float = 0.0
    credits_tiles: float = 0.0

    def __add__(self, other: "CallEstimate") -> "CallEstimate":
        a, b = self.model_dump(), other.model_dump()
        return CallEstimate(**{k: a[k] + b[k] for k in a})


@dataclass
class ReadOutcome:
    readings: dict[str, CropReading]
    credits: float  # spent (real run) or estimated (dry run)
    calls: int
    estimate: CallEstimate = field(default_factory=CallEstimate)
    dry_run: bool = False


def image_tokens(width: int, height: int) -> int:
    """Pixel-based image tokens (images larger than the provider limit are scaled down)."""
    scale = min(1.0, MAX_LONG_SIDE / max(width, height, 1))
    return max(1, math.ceil(width * scale * height * scale / PIXELS_PER_TOKEN))


def tile_tokens(width: int, height: int) -> int:
    """Tile-based image tokens (fit into 2048 px, short side at most 768 px, 512-px tiles)."""
    scale = min(1.0, 2048 / max(width, height, 1))
    w, h = width * scale, height * scale
    scale = min(1.0, 768 / max(1.0, min(w, h)))
    w, h = w * scale, h * scale
    return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)


def _text_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.5)


def estimate_call(crops: Sequence[Crop], config: AgentConfig, system: str) -> CallEstimate:
    base = _text_tokens(system) + TOKENS_CALL_OVERHEAD + TOKENS_PER_CROP_TEXT * len(crops)
    tokens_in = base + sum(image_tokens(*c.size) for c in crops)
    tiles_in = base + sum(tile_tokens(*c.size) for c in crops)
    tokens_out = TOKENS_OUT_BASE + TOKENS_OUT_PER_CROP * len(crops)
    out_cost = tokens_out * config.price_output

    return CallEstimate(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_in_tiles=tiles_in,
        credits=(tokens_in * config.price_input + out_cost) / 1e6,
        credits_tiles=(tiles_in * config.price_input + out_cost) / 1e6,
    )


def _image(crop: Crop) -> dict[str, Any]:
    data = base64.b64encode(crop.png).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


_DOMAIN_HINT = {
    "part": "Sheet type: mechanical drawing (dimensions, tolerances, notes, title block).",
    "circuit": "Sheet type: electronic schematic (designators, values, net and pin names).",
}


class CropReader:
    """Reads crops in batches with one model; `llm` is None only for a dry run."""

    def __init__(
        self,
        llm: ModelClient | None,
        config: AgentConfig,
        *,
        batch: int = BATCH,
        dry_run: bool = False,
    ) -> None:
        if llm is None and not dry_run:
            raise ValueError("a crop reader without a model can only do a dry run")
        self.llm = llm
        self.config = config
        self.batch = max(1, batch)
        self.dry_run = dry_run
        self.estimated = CallEstimate()  # all estimates so far (dry run: the whole cost)
        self._ratio: float | None = None  # highest reported/estimated cost of a call so far
        self._closers: list[Callable[[], None]] = []
        self._runner: asyncio.Runner | None = None

    def run(self, work: Coroutine[Any, Any, T]) -> T:
        """Run `work` on the reader's own event loop, the same one for every sheet and backend.
        Async model clients keep pooled connections bound to the loop that opened them, so a
        fresh loop per sheet (`asyncio.run`) would reuse a connection of a closed loop."""
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(work)

    def on_close(self, closer: Callable[[], None]) -> None:
        self._closers.append(closer)

    def close(self) -> None:
        """Release what the reader was built with (event loop, model event file)."""
        if self._runner is not None:
            self._runner.close()
            self._runner = None
        while self._closers:
            self._closers.pop()()

    @property
    def spent(self) -> float:
        return self.llm.budget.run_spent if self.llm is not None else 0.0

    def reserve(self, estimate: CallEstimate) -> float:
        """Credits set aside for the next call before it is made."""
        if self._ratio is None:  # nothing reported yet: assume tile billing, the dearer scheme
            return max(estimate.credits, estimate.credits_tiles)
        return estimate.credits * self._ratio * RESERVE_MARGIN

    def _check_budget(self, estimate: CallEstimate) -> None:
        if self.llm is None:
            return
        guard = self.llm.budget
        reserve = self.reserve(estimate)
        if guard.run_spent + reserve > guard.run_limit:
            raise BudgetExceededError(
                f"next call (reserving ~{reserve:.4f} credits) would pass the run budget: "
                f"{guard.run_spent:.4f} of {guard.run_limit:.4f} spent"
            )

    def _learn(self, estimate: CallEstimate, cost: float) -> None:
        ratio = cost / max(estimate.credits, 1e-12)
        self._ratio = ratio if self._ratio is None else max(self._ratio, ratio)

    async def _ask(self, crops: Sequence[Crop], domain: str, step: str) -> dict[str, Any]:
        """Crop id -> the model's answer (text, None or False), for the ids asked only."""
        assert self.llm is not None
        content = [
            text_block(_DOMAIN_HINT.get(domain, "")),
            text_block(f"Read these {len(crops)} crops; answer for every crop id."),
        ]
        for crop in crops:
            content += [text_block(f"crop {crop.id}"), _image(crop)]
        answer = extract_json(await self.llm.ask(step, prompts.READ_CROPS, content))
        wanted = {c.id for c in crops}
        return {k: v for k, v in answer.items() if k in wanted} if isinstance(answer, dict) else {}

    async def read(self, crops: Sequence[Crop], domain: str) -> ReadOutcome:
        """One reading per crop id. Crops the model skips are asked for once more."""
        readings: dict[str, CropReading] = {}
        estimate = CallEstimate()
        calls = 0
        before = self.spent

        async def run(batch: Sequence[Crop], step: str) -> None:
            nonlocal estimate, calls
            call = estimate_call(batch, self.config, prompts.READ_CROPS)
            estimate += call
            self.estimated += call
            if self.dry_run:
                return
            self._check_budget(call)
            calls += 1
            spent = self.spent
            try:
                answer = await self._ask(batch, domain, step)
            finally:
                if self.spent > spent:  # the call was paid even if its answer is unusable
                    self._learn(call, self.spent - spent)
            for crop_id, value in answer.items():
                plain = isinstance(value, str | int | float) and not isinstance(value, bool)
                text = str(value).strip()[:MAX_TEXT] if plain else ""
                readings[crop_id] = CropReading(
                    id=crop_id, text=text or None, lettering=value is not False
                )

        for i in range(0, len(crops), self.batch):
            await run(crops[i : i + self.batch], "read_crops")
        missing = [c for c in crops if c.id not in readings]
        if missing and not self.dry_run:
            for i in range(0, len(missing), self.batch):
                await run(missing[i : i + self.batch], "read_crops_retry")
        for crop in crops:
            readings.setdefault(crop.id, CropReading(id=crop.id, answered=False))
        credits = estimate.credits if self.dry_run else self.spent - before
        return ReadOutcome(
            readings=readings,
            credits=credits,
            calls=calls,
            estimate=estimate,
            dry_run=self.dry_run,
        )
