"""Input variants of the vector drawings for the extraction benchmark (docs/09).

Raster variants are rendered from the vector PDF with PDFium and degraded like a scan: contrast
loss, rotation and shear, blur, noise and JPEG artefacts. Geometric changes are affine only, so
every sheet position maps exactly into the image and the vector ground truth stays exact.
Generation is deterministic for a seed on the same library versions (PDFium anti-aliasing,
Pillow's blur/JPEG/zlib and numpy's random stream may differ between versions; the manifest
records them and a version change regenerates). The sampled parameters and the sheet-to-image
transform go into a manifest kept apart from the images, so extractors get only the pixels.
Output goes to an ignored folder (var/), never to data/.
"""

import hashlib
import io
import math
from functools import cache
from pathlib import Path
from typing import Literal

import numpy as np
import PIL
import pypdfium2 as pdfium
from numpy.typing import NDArray
from PIL import Image, ImageFilter
from pydantic import BaseModel, ConfigDict, Field

from mechatronikers.testbench.cases import ensure_materialized

GENERATOR_VERSION = 2  # 2: manifests apart from the images, library versions recorded
MM_PER_INCH = 25.4

Affine = tuple[float, float, float, float, float, float]  # x' = a·x + b·y + c, y' = d·x + e·y + f
IDENTITY: Affine = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


class VariantSpec(BaseModel):
    """How a variant is made. Ranges are sampled per case from the seed."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["vector", "raster"]
    dpi: float | None = Field(default=None, gt=0)
    max_rotation_deg: float = Field(default=0.0, ge=0)  # uniform in ±max
    max_shear: float = Field(default=0.0, ge=0)  # x' = x + shear·y, uniform in ±max
    contrast: float = Field(default=1.0, gt=0, le=1)  # share of the grey range kept
    blur_px: float = Field(default=0.0, ge=0)  # Gaussian standard deviation
    noise: float = Field(default=0.0, ge=0)  # Gaussian standard deviation in grey levels
    jpeg_quality: int | None = Field(default=None, ge=1, le=95)


# docs/09 "Input variants". Perspective and occlusion are not generated: perspective would make
# the sheet-to-image mapping non-affine, occlusion would remove ground truth (follow-up).
PRESETS: dict[str, VariantSpec] = {
    v.name: v
    for v in (
        VariantSpec(name="vector", kind="vector"),
        VariantSpec(name="raster-300", kind="raster", dpi=300),
        VariantSpec(name="raster-150", kind="raster", dpi=150),
        VariantSpec(name="raster-100", kind="raster", dpi=100),
        VariantSpec(
            name="scan-sim", kind="raster", dpi=200, max_rotation_deg=2.0, max_shear=0.01,
            contrast=0.75, blur_px=0.8, noise=6.0, jpeg_quality=70,
        ),
        VariantSpec(
            name="low-quality", kind="raster", dpi=72, max_rotation_deg=1.0, max_shear=0.005,
            contrast=0.6, blur_px=0.6, noise=10.0, jpeg_quality=30,
        ),
    )
}  # fmt: skip


@cache
def library_versions() -> dict[str, str]:
    """Versions that determine the pixels of a variant."""
    from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

    return {
        "pypdfium2": str(PYPDFIUM_INFO),
        "pdfium": str(PDFIUM_INFO),
        "pillow": PIL.__version__,
        "numpy": np.__version__,
    }


class VariantManifest(BaseModel):
    generator_version: int = GENERATOR_VERSION
    libraries: dict[str, str] = Field(default_factory=library_versions)
    case_id: str
    spec: VariantSpec
    seed: int  # derived per case and variant from the run seed
    source: str
    source_sha256: str
    sheet_size_mm: tuple[float, float]
    image: str | None = None  # image path relative to the variants folder (raster only)
    image_size_px: tuple[int, int] | None = None
    image_sha256: str | None = None
    sheet_to_image: Affine = IDENTITY  # sheet mm (top-left origin) → image px; identity = vector
    rotation_deg: float = 0.0  # sampled
    shear: float = 0.0  # sampled


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def variant_seed(seed: int, case_id: str, variant: str) -> int:
    """Stable per-case seed (Python's hash() is salted per process)."""
    digest = hashlib.sha256(f"{seed}/{case_id}/{variant}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _matrix(affine: Affine) -> NDArray[np.float64]:
    a, b, c, d, e, f = affine
    return np.array([[a, b, c], [d, e, f], [0.0, 0.0, 1.0]])


def _affine(m: NDArray[np.float64]) -> Affine:
    return (
        float(m[0, 0]), float(m[0, 1]), float(m[0, 2]),
        float(m[1, 0]), float(m[1, 1]), float(m[1, 2]),
    )  # fmt: skip


def image_to_sheet(manifest: VariantManifest) -> NDArray[np.float64]:
    """3×3 matrix mapping image px back to sheet mm."""
    inverse: NDArray[np.float64] = np.linalg.inv(_matrix(manifest.sheet_to_image))
    return inverse


def render_raster(
    pdf: Path, spec: VariantSpec, seed: int, page_index: int = 0
) -> tuple[Image.Image, Affine, float, float]:
    """Render and degrade one sheet. Returns (greyscale image, sheet-to-image transform, sampled
    rotation in degrees, sampled shear)."""
    if spec.kind != "raster" or spec.dpi is None:
        raise ValueError(f"variant {spec.name!r} is not a raster variant with a dpi")
    rng = np.random.default_rng(seed)
    page = pdfium.PdfDocument(str(pdf))[page_index]
    image = page.render(scale=spec.dpi / 72, grayscale=True).to_pil().convert("L")
    s = spec.dpi / MM_PER_INCH
    transform = np.diag([s, s, 1.0])

    paper = 255
    if spec.contrast < 1.0:
        low = 255 * (1 - spec.contrast) / 2
        grey = np.asarray(image, dtype=np.float32) * spec.contrast + low
        image = Image.fromarray(np.clip(np.rint(grey), 0, 255).astype(np.uint8))
        paper = round(low + 255 * spec.contrast)

    rotation = float(rng.uniform(-spec.max_rotation_deg, spec.max_rotation_deg))
    shear = float(rng.uniform(-spec.max_shear, spec.max_shear))
    if rotation or shear:
        t = math.radians(rotation)
        linear = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]]) @ np.array(
            [[1.0, shear], [0.0, 1.0]]
        )
        w, h = image.size
        corners = linear @ np.array([[0, w, w, 0], [0, 0, h, h]], dtype=np.float64)
        low_xy = corners.min(axis=1)
        size = np.ceil(corners.max(axis=1) - low_xy).astype(int)
        forward = np.eye(3)
        forward[:2, :2] = linear
        forward[:2, 2] = -low_xy  # keep the whole rotated sheet on the canvas
        inverse = np.linalg.inv(forward)
        image = image.transform(
            (int(size[0]), int(size[1])),
            Image.Transform.AFFINE,
            _affine(inverse),
            resample=Image.Resampling.BILINEAR,
            fillcolor=paper,
        )
        transform = forward @ transform

    if spec.blur_px > 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=spec.blur_px))
    if spec.noise > 0:
        grey = np.asarray(image, dtype=np.float32)
        grey = grey + rng.standard_normal(grey.shape, dtype=np.float32) * spec.noise
        image = Image.fromarray(np.clip(np.rint(grey), 0, 255).astype(np.uint8))
    if spec.jpeg_quality is not None:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=spec.jpeg_quality)
        buffer.seek(0)
        with Image.open(buffer) as decoded:
            image = decoded.convert("L")
    return image, _affine(transform), rotation, shear


def manifest_file(out_dir: Path, case_id: str, variant: str) -> Path:
    return out_dir / "manifests" / case_id / f"{variant}.json"


def prepare_variant(
    case_id: str, pdf: Path, spec: VariantSpec, out_dir: Path, seed: int = 0
) -> tuple[Path, VariantManifest]:
    """The input file for one case and variant, plus its manifest. Vector variants are the source
    PDF itself. Raster variants are generated into `out_dir/images/<case_id>/<variant>.png`, their
    manifest into `out_dir/manifests/<case_id>/<variant>.json` (for the scorer only); both are
    reused when spec, seed, source, generator and library versions are unchanged."""
    ensure_materialized(pdf)
    source_sha = _sha256(pdf)
    page = pdfium.PdfDocument(str(pdf))[0]
    width_pt, height_pt = page.get_size()
    sheet = (width_pt * MM_PER_INCH / 72, height_pt * MM_PER_INCH / 72)
    if spec.kind == "vector":
        return pdf, VariantManifest(
            case_id=case_id, spec=spec, seed=seed, source=str(pdf),
            source_sha256=source_sha, sheet_size_mm=sheet,
        )  # fmt: skip

    image_path = out_dir / "images" / case_id / f"{spec.name}.png"
    manifest_path = manifest_file(out_dir, case_id, spec.name)
    derived = variant_seed(seed, case_id, spec.name)
    if manifest_path.is_file() and image_path.is_file():
        cached = VariantManifest.model_validate_json(manifest_path.read_text())
        if (
            cached.generator_version == GENERATOR_VERSION
            and cached.spec == spec
            and cached.seed == derived
            and cached.source_sha256 == source_sha
            and cached.libraries == library_versions()
            and cached.image_sha256 == _sha256(image_path)
        ):
            return image_path, cached

    image, transform, rotation, shear = render_raster(pdf, spec, derived)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    assert spec.dpi is not None
    image.save(image_path, format="PNG", dpi=(spec.dpi, spec.dpi))
    manifest = VariantManifest(
        case_id=case_id,
        spec=spec,
        seed=derived,
        source=str(pdf),
        source_sha256=source_sha,
        sheet_size_mm=sheet,
        image=image_path.relative_to(out_dir).as_posix(),
        image_size_px=image.size,
        image_sha256=_sha256(image_path),
        sheet_to_image=transform,
        rotation_deg=round(rotation, 6),
        shear=round(shear, 8),
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2))
    return image_path, manifest
