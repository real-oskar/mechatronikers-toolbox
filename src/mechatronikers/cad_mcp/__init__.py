"""CAD MCP server: feature tools, rebuild, validation after every build, export (docs/05).

Runs inside the session sandbox, offline. Geometry is executed in the build runner.
"""

from mechatronikers.cad_mcp.validation import validate

__all__ = ["validate"]
