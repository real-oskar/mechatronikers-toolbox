"""Tests that need real Git LFS files (reference STEP, fixtures)."""

import os
from pathlib import Path

import pytest

from mechatronikers.testbench.cases import LfsPointerError, ensure_materialized


def require_lfs(path: Path) -> Path:
    """The file, materialized. Skips without it (plain checkout), unless MT_REQUIRE_LFS is set:
    the CI job that pulls these files sets it, so a broken pull fails instead of skipping."""
    try:
        return ensure_materialized(path)
    except (LfsPointerError, FileNotFoundError) as exc:
        if os.environ.get("MT_REQUIRE_LFS"):
            raise
        pytest.skip(f"{path} not available (Git LFS not pulled): {exc}")
