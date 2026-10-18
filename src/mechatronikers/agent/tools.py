"""Calls into the sandbox's MCP servers (drawing-mcp, cad-mcp) via langchain-mcp-adapters."""

import asyncio
import json
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

Block = dict[str, Any]


class ToolError(RuntimeError):
    pass


TOOL_TIMEOUT_S = 600.0


class SandboxTools:
    def __init__(
        self, connections: dict[str, dict[str, Any]], *, timeout_s: float = TOOL_TIMEOUT_S
    ) -> None:
        self.client = MultiServerMCPClient(connections)  # type: ignore[arg-type]
        self.tools: dict[str, BaseTool] = {}
        self.timeout_s = timeout_s  # a tool call that hangs fails the step instead (#145)

    async def start(self) -> None:
        try:
            tools = await asyncio.wait_for(self.client.get_tools(), self.timeout_s)
        except TimeoutError as exc:
            listing = f"MCP servers did not list their tools in {self.timeout_s:g} s"
            raise ToolError(listing) from exc
        self.tools = {t.name: t for t in tools}

    async def blocks(self, name: str, args: dict[str, Any]) -> list[Block]:
        if name not in self.tools:
            raise ToolError(f"tool {name!r} not available; have {sorted(self.tools)}")
        try:
            out = await asyncio.wait_for(self.tools[name].ainvoke(args), self.timeout_s)
        except TimeoutError as exc:
            raise ToolError(f"{name}: no answer in {self.timeout_s:g} s") from exc
        parts: list[Block] = out if isinstance(out, list) else [{"type": "text", "text": str(out)}]
        for part in parts:
            text = part.get("text", "")
            if part.get("type") == "text" and isinstance(text, str) and text.startswith("Error"):
                raise ToolError(f"{name}: {text}")
        return parts

    async def json(self, name: str, args: dict[str, Any]) -> Any:
        """Tool result as JSON; several text blocks (a returned list) become a list."""
        texts = [b["text"] for b in await self.blocks(name, args) if b.get("type") == "text"]
        values = [json.loads(t) for t in texts]
        return values[0] if len(values) == 1 else values
