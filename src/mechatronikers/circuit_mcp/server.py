"""circuit-mcp server: schematic PDF → netlist for the agent (docs/12).

Deterministic and offline. The analysis proposes components, nets and pin roles from the vector
geometry and the text layer; the model (or a reviewer) confirms or corrects them from rendered
crops; checks and exports are tool calls.
"""

from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from mechatronikers.circuit_mcp.session import (
    CircuitSession,
    ComponentUpdate,
    ExportFormat,
    ExportResult,
)
from mechatronikers.circuit_mcp.simulate import SimulationResult
from mechatronikers.spec.circuit import (
    CIRCUIT_SPEC_VERSION,
    BBox,
    Component,
    ErcReport,
    NetSummary,
    PinRole,
)
from mechatronikers.spec.poi import ReviewItem
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.instrument import instrument


def create_server(
    emitter: EventEmitter,
    input_root: Path | None = None,
    artifacts_dir: Path | None = None,
    session: CircuitSession | None = None,
) -> FastMCP:
    server = FastMCP(
        "circuit-mcp",
        instructions=(
            "Turn a schematic PDF into a netlist: load_schematic, then list_components and "
            "list_review_items. Confirm uncertain parts from render_component images and correct "
            "them with update_component (kind, value, pin roles). check_circuit runs electrical "
            "rule checks; export_netlist writes SPICE (.cir, opens in LTspice), an LTspice "
            "schematic keeping the sheet layout (.asc) and a KiCad netlist (.net). Printed values "
            "are authoritative: never invent a value, leave it for review instead."
        ),
        stateless_http=True,
        log_level="WARNING",
        # The session gateway authenticates every request; the container is unreachable otherwise.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    state = session or CircuitSession(
        input_root or Path("/session/input"),
        artifacts_dir or Path("/session/artifacts"),
        emitter,
    )

    def tool(fn: Any) -> Any:
        return server.tool()(instrument(emitter, "circuit-mcp")(fn))

    def image_tool(fn: Any) -> Any:
        # Images are returned as MCP image content, not as structured JSON output.
        return server.tool(structured_output=False)(instrument(emitter, "circuit-mcp")(fn))

    @tool
    def circuit_ping() -> dict[str, str]:
        """Health check: returns the server name and circuit spec version."""
        return {"server": "circuit-mcp", "circuit_spec_version": CIRCUIT_SPEC_VERSION}

    @tool
    async def load_schematic(path: str) -> dict[str, Any]:
        """Load a schematic PDF (path relative to the session input folder) and analyse it.
        Returns the schematic_id and an overview: components, nets, review items, checks."""
        schematic_id, spec = await anyio.to_thread.run_sync(lambda: state.load(path))
        report = state.check(schematic_id)
        return {
            "schematic_id": schematic_id,
            "sheet_size": spec.sheet_size,
            "components": len(spec.components),
            "nets": len(spec.nets),
            "directives": spec.directives,
            "review_items": len(spec.review_items),
            "checks_passed": report.passed,
            "failed_checks": [c.id for c in report.checks if not c.passed],
            "stats": spec.stats.model_dump(),
        }

    @tool
    def list_components(schematic_id: str) -> list[Component]:
        """Components with kind, value, terminals (pin number, role, net) and status."""
        return state.get(schematic_id).components

    @tool
    def list_nets(schematic_id: str) -> list[NetSummary]:
        """Nets with their name and the terminals they connect."""
        return [
            NetSummary(id=n.id, name=n.name, terminals=n.terminals, wires=len(n.segments))
            for n in state.get(schematic_id).nets
        ]

    @tool
    def list_review_items(schematic_id: str, status: str | None = "open") -> list[ReviewItem]:
        """Review items (what the analysis is unsure about), by default the open ones."""
        items = state.get(schematic_id).review_items
        return [r for r in items if status is None or r.status == status]

    @image_tool
    async def render_schematic_region(schematic_id: str, bbox: BBox, dpi: float = 200) -> Image:
        """Image of a sheet region (x0, y0, x1, y1 in sheet mm, top-left origin)."""
        rendered = await anyio.to_thread.run_sync(lambda: state.render(schematic_id, bbox, dpi))
        return Image(data=rendered.png, format="png")

    @image_tool
    async def render_component(schematic_id: str, refdes: str, dpi: float = 400) -> Image:
        """Image of one component's symbol with its surroundings, to confirm its kind and pins."""
        rendered = await anyio.to_thread.run_sync(
            lambda: state.render_component(schematic_id, refdes, dpi)
        )
        return Image(data=rendered.png, format="png")

    @tool
    def update_component(
        schematic_id: str,
        refdes: str,
        kind: str | None = None,
        value: str | None = None,
        params: str | None = None,
        polarity: Literal["npn", "pnp", "n", "p"] | None = None,
        footprint: str | None = None,
        roles: dict[str, PinRole] | None = None,
        pins: dict[str, str] | None = None,
    ) -> Component:
        """Correct a component: kind, value exactly as printed, source parameters, transistor
        polarity, footprint, pin roles (terminal id → role, e.g. {"Q1.2": "B"}) or pin numbers.
        Only the given fields change."""
        update = ComponentUpdate.model_validate(
            {
                "kind": kind,
                "value": value,
                "params": params,
                "polarity": polarity,
                "footprint": footprint,
                "roles": roles,
                "pins": pins,
            }
        )
        return state.update_component(schematic_id, refdes, update)

    @tool
    def set_net_name(schematic_id: str, net_id: str, name: str) -> NetSummary:
        """Name a net (e.g. a label the analysis could not place)."""
        n = state.set_net_name(schematic_id, net_id, name)
        return NetSummary(id=n.id, name=n.name, terminals=n.terminals, wires=len(n.segments))

    @tool
    def check_circuit(schematic_id: str) -> ErcReport:
        """Electrical rule checks: unique designators, terminal counts, floating pins, ground,
        values, pin roles, open blocking review items; advisory: models needed."""
        return state.check(schematic_id)

    @tool
    def export_netlist(
        schematic_id: str,
        formats: list[ExportFormat] | None = None,
    ) -> ExportResult:
        """Write the netlist files as artifacts: spice (.cir), ltspice (.asc, sheet layout kept),
        kicad-netlist (.net), circuit-json. Returns artifact references, notes on what the files
        leave out, and the check report."""
        return state.export(schematic_id, list(formats or ["spice", "ltspice", "kicad-netlist"]))

    @tool
    async def simulate_op(schematic_id: str) -> SimulationResult:
        """DC operating point of the netlist with ngspice (time-limited, offline): node voltages
        and source currents. Refused while parts are missing from the netlist; fails with the
        ngspice error when a model (.model/.subckt) is not provided."""
        return await anyio.to_thread.run_sync(lambda: state.simulate(schematic_id))

    return server
