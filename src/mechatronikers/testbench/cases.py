"""Test cases (drawing + human STEP) and the built-in challenge set from data/ (docs/09)."""

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class LfsPointerError(RuntimeError):
    pass


class TestCase(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest class

    case_id: str
    drawing: Path
    reference_step: Path
    meta: dict[str, Any] = Field(default_factory=dict)


def ensure_materialized(path: Path) -> Path:
    """Fail clearly if a Git LFS file was checked out as a pointer only."""
    with path.open("rb") as f:
        if f.read(len(_LFS_POINTER)) == _LFS_POINTER:
            raise LfsPointerError(f"{path} is a Git LFS pointer; run `git lfs pull`")
    return path


def challenge_set(data_dir: str | Path = "data") -> list[TestCase]:
    """The sample parts: data/<Part>/<Part>_dwg.pdf + data/<Part>/<Part>.stp."""
    cases = []
    for folder in sorted(Path(data_dir).iterdir()):
        drawing = folder / f"{folder.name}_dwg.pdf"
        reference = folder / f"{folder.name}.stp"
        if folder.is_dir() and drawing.is_file() and reference.is_file():
            cases.append(TestCase(case_id=folder.name, drawing=drawing, reference_step=reference))
    return cases


def get_case(case_id: str, data_dir: str | Path = "data") -> TestCase:
    for case in challenge_set(data_dir):
        if case.case_id.lower() == case_id.lower():
            return case
    raise KeyError(f"unknown test case: {case_id}")
