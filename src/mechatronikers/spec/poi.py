"""Points of interest (POIs): review items and their provenance, shared by every run kind (D75).

Part runs point at dimensions and features, circuit runs at components and nets (refdes and net
ids in `feature_ids`, D67). Re-exported from `spec.part` for older imports.
"""

from typing import Literal

from pydantic import BaseModel, Field

Vec3 = tuple[float, float, float]
Status = Literal["auto_ok", "needs_review", "approved", "edited", "rejected"]


class SourceRef(BaseModel):
    """Where a value was found on the drawing."""

    sheet: int = 0
    view_id: str | None = None  # "front", "section-A-A", "detail-A" …
    bbox: tuple[float, float, float, float]  # sheet mm: x0, y0, x1, y1
    input_path: Literal["vector", "raster"] = "vector"
    crop_sha256: str | None = None  # hash of the rendered crop shown to the model


class ModelRef(BaseModel):
    face_ids: list[int] = Field(default_factory=list)
    point: Vec3 | None = None


class ReviewAction(BaseModel):
    at: str  # RFC 3339 UTC
    by: str
    action: Literal["approved", "edited", "rejected", "resolved", "note"]
    old_value: float | str | None = None
    new_value: float | str | None = None
    comment: str | None = None


class ReviewItem(BaseModel):
    id: str
    kind: Literal[
        "assumption",
        "mismatch",
        "low_confidence",
        "unreadable",
        "unsupported",
        "validation_failure",
        "user_note",
        "possibly_missing",  # drawing evidence that no planned feature explains
        "undimensioned_feature",  # visible in a view, but the drawing gives no values (D93)
    ]
    severity: Literal["info", "warning", "blocking"] = "warning"
    message: str
    reason: str
    dimension_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)
    sheet_ref: SourceRef | None = None
    model_ref: ModelRef | None = None
    status: Literal["open", "approved", "edited", "rejected", "resolved"] = "open"
    history: list[ReviewAction] = Field(default_factory=list)
