"""MCP connection settings for the agent (langchain-mcp-adapters MultiServerMCPClient)."""

from typing import Any


def mcp_connections(base_url: str, token: str) -> dict[str, dict[str, Any]]:
    """Connections for the MCP servers behind a session gateway at `base_url`."""
    headers = {"Authorization": f"Bearer {token}"}
    base = base_url.rstrip("/")
    return {
        name: {"transport": "streamable_http", "url": f"{base}/{name}/mcp", "headers": headers}
        for name in ("drawing", "cad", "circuit")
    }
