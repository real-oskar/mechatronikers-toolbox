"""cad-mcp server: builds one part from structured feature tools (docs/05).

Every change is rebuilt and validated in the build runner (V1 geometry, V2 dimensions incl.
holes, V3 STEP round trip). Failing changes are rolled back and the report names the feature.
Free code is a fallback only (add_script_feature / run_script) and always creates a review item.
"""

import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mechatronikers.cad_mcp.part_session import (
    ArtifactRef,
    ChangeResult,
    DimIds3,
    ExportFormat,
    PartSession,
    PartState,
    ViewName,
)
from mechatronikers.runner.launcher import Isolation, run_build
from mechatronikers.spec import (
    SPEC_VERSION,
    Boss,
    BuildJob,
    BuildResult,
    Chamfer,
    Dimension,
    ExpectedGeometry,
    ExtrudeBase,
    Fillet,
    Hole,
    HolePattern,
    Pocket,
    RevolveBase,
    ScriptFeature,
    Slot,
)
from mechatronikers.spec.validation import Size3
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.instrument import instrument

MAX_SCRIPT_TIMEOUT_S = 120.0
T = TypeVar("T")


def create_server(
    emitter: EventEmitter,
    builds_dir: Path,
    isolation: Isolation,
    artifacts_dir: Path | None = None,
) -> FastMCP:
    server = FastMCP(
        "cad-mcp",
        instructions=(
            "Build ONE 3D part from structured feature tools, in mm, part frame Z up. "
            "Start with new_part, then exactly one base body (create_extrude_base or "
            "create_revolve_base), then holes, patterns, pockets, bosses, slots, fillets, "
            "chamfers. Every change is validated; a failing change is rolled back and the "
            "message names the feature. Use add_script_feature only for geometry the tools "
            "cannot express. Finish with validate_part, render_views and export."
        ),
        stateless_http=True,
        log_level="WARNING",
        # The session gateway authenticates every request; the container is unreachable otherwise.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    part = PartSession(
        builds_dir, artifacts_dir or builds_dir.parent / "artifacts", isolation, emitter
    )
    lock = threading.Lock()

    async def locked(fn: Callable[[], T]) -> T:
        def run() -> T:
            with lock:
                return fn()

        return await anyio.to_thread.run_sync(run)

    def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        return server.tool()(instrument(emitter, "cad-mcp")(fn))

    @tool
    def cad_ping() -> dict[str, str]:
        """Health check: returns the server name and spec version."""
        return {"server": "cad-mcp", "spec_version": SPEC_VERSION}

    @tool
    async def new_part(
        part_name: str,
        overall_size: Size3 | None = None,
        dimensions: list[Dimension] | None = None,
        overall_size_dims: DimIds3 | None = None,
    ) -> PartState:
        """Start a new part (discards the current one). `dimensions` = values read from the
        drawing, which features may reference by id. `overall_size_dims` = per axis (x, y, z)
        the id of the ONE printed dimension that spans the whole part, or null: the overall
        size is then taken from the printed values (preferred, checked by validate_part).
        `overall_size` = explicit extents instead (null per axis = not checked)."""

        def start() -> PartState:
            part.new_part(part_name, overall_size, dimensions, overall_size_dims=overall_size_dims)
            return part.state()

        return await locked(start)

    @tool
    async def get_part_state() -> PartState:
        """Current feature list, last build, bounding box and volume."""
        return await locked(part.state)

    @tool
    async def create_extrude_base(feature: ExtrudeBase) -> ChangeResult:
        """Base body: closed 2D profile on a placement plane, extruded along its normal."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def create_revolve_base(feature: RevolveBase) -> ChangeResult:
        """Turned base body: profile in the XZ half-plane (u = radius >= 0, v = z), about Z."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_hole(feature: Hole) -> ChangeResult:
        """Hole: placement origin on the entry face, normal pointing INTO the material.
        Threaded holes use the tap drill diameter (M5 -> 4.2) and set `thread`."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_hole_pattern(feature: HolePattern) -> ChangeResult:
        """Circular or linear pattern of a seed hole (the seed is the first instance)."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_pocket(feature: Pocket) -> ChangeResult:
        """Remove material: profile on a placement, cut `depth` along its normal (None: through)."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_boss(feature: Boss) -> ChangeResult:
        """Add material: profile on a placement, extruded `height` along the normal."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_slot(feature: Slot) -> ChangeResult:
        """Straight slot: centre-to-centre length along placement x_dir, width, depth."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_fillet(feature: Fillet) -> ChangeResult:
        """Round edges chosen by an edge selector (near_points / parallel / all)."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_chamfer(feature: Chamfer) -> ChangeResult:
        """Chamfer edges chosen by an edge selector; distance_2 empty = symmetric (45°)."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def add_script_feature(feature: ScriptFeature) -> ChangeResult:
        """FALLBACK ONLY: build123d code that modifies `part` (the solid so far) and leaves the
        result in `part`. Give a `reason`; a review item is created automatically."""
        return await locked(lambda: part.add_feature(feature))

    @tool
    async def update_feature(feature_id: str, changes: dict[str, Any]) -> ChangeResult:
        """Change fields of an existing feature (e.g. {"diameter": 12}); rebuilt and validated."""
        return await locked(lambda: part.update_feature(feature_id, changes))

    @tool
    async def delete_feature(feature_id: str) -> ChangeResult:
        """Remove a feature; the part is rebuilt and validated."""
        return await locked(lambda: part.delete_feature(feature_id))

    @tool
    async def validate_part() -> BuildResult:
        """All checks of the current build, including the overall size from new_part. Every
        change is already built and validated; render_views and export use this same build."""
        return await locked(part.validate)

    @tool
    async def render_views(views: list[ViewName]) -> list[ArtifactRef]:
        """Hidden-line SVG drawings (front/top/right/iso) of the part, to compare with the
        drawing. Returns artifact references."""
        return await locked(lambda: part.render_views(views))

    @tool
    async def export(formats: list[ExportFormat]) -> list[ArtifactRef]:
        """Export the last validated build (step-ap242, step-ap214, stl, obj, features-json,
        review-json). Returns artifact references (uri, sha256)."""
        return await locked(lambda: part.export(formats))

    @tool
    async def run_script(
        code: str,
        reason: str,
        expected: ExpectedGeometry | None = None,
        timeout_s: float = 60.0,
    ) -> BuildResult:
        """FALLBACK ONLY, outside the current part: run build123d code that assigns a whole
        solid to `result` in the isolated build runner and validate it."""
        job = BuildJob(
            build_id=f"build-{uuid.uuid4().hex[:12]}",
            kind="script",
            script=code,
            expected=expected,
            timeout_s=min(timeout_s, MAX_SCRIPT_TIMEOUT_S),
        )
        emitter.emit(
            "build.requested", {"build_id": job.build_id, "kind": "script", "reason": reason}
        )
        return await anyio.to_thread.run_sync(
            lambda: run_build(job, builds_dir, isolation=isolation, emitter=emitter)
        )

    return server
