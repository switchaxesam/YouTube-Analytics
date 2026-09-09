"""Thumbnail analysis: local image features, then optional Claude vision.

Same split as :mod:`titles`, for the same reason. Colour, contrast, and edge
density are measured from the pixels — free, instant, and objective. What the
image *depicts* needs a vision model, costs money per image, and is a
judgement.

The local half matters more than it sounds. A thumbnail is chosen at roughly
210×118 on a desktop homepage and smaller on mobile, so the failure mode that
actually loses clicks is *illegibility at size*, not taste. Contrast and edge
density measure exactly that, and they measure it the same way every time.

Analyses are cached on ``(video_id, thumbnail_url)`` forever. A YouTube
thumbnail URL is immutable — replacing the image mints a new URL — so a cached
analysis can never go stale, and re-analysing would be pure waste.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

import httpx
import numpy as np
from PIL import Image, ImageFilter, ImageStat
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import thumbnail_cache_dir
from ..models import ThumbnailAnalysis, Video

log = logging.getLogger(__name__)

#: Analysis is done at this width. Full-resolution thumbnails add processing
#: time without changing any of the measurements meaningfully.
ANALYSIS_WIDTH = 640

#: Fraction of pixels above the edge threshold beyond which a thumbnail is
#: called "busy". Calibrated so that a clean subject-on-background image sits
#: well under it and a dense collage sits well over.
BUSY_EDGE_DENSITY = 0.14

#: Luminance standard deviation below which a thumbnail is called low-contrast.
LOW_CONTRAST = 45.0

#: The size a thumbnail is actually chosen at on a desktop homepage. Every
#: legibility judgement is made here rather than at full resolution, because
#: this is the only size the viewer ever sees before deciding.
DISPLAY_SIZE = (210, 118)

#: And roughly the size in a phone feed, where most watching now happens.
MOBILE_SIZE = (168, 94)

#: Below this share of edge detail surviving the downscale, the image is
#: carrying detail that vanishes at the size it is displayed.
POOR_DETAIL_RETENTION = 0.45

#: Contrast floor measured *after* downscaling. Lower than the full-size floor
#: because downscaling averages pixels together and always reduces spread.
LOW_SMALL_CONTRAST = 38.0

#: Text-like coverage beyond which the overlay is competing with the image
#: rather than supporting it.
HEAVY_TEXT_AREA = 0.30


class ThumbnailError(Exception):
    """Downloading or decoding a thumbnail failed."""


@dataclass
class LocalFeatures:
    """Measured from the pixels. No API, no model, no cost."""

    width: int
    height: int
    dominant_colors: list[str]
    mean_brightness: float
    mean_saturation: float
    contrast: float
    edge_density: float

    small_contrast: float = 0.0
    detail_retention: float = 1.0
    weight_x: float = 0.5
    weight_y: float = 0.5
    composition_note: str = ""
    text_area_estimate: float = 0.0
    text_bands: list[dict[str, float]] = field(default_factory=list)

    @property
    def is_busy(self) -> bool:
        return self.edge_density > BUSY_EDGE_DENSITY

    @property
    def is_low_contrast(self) -> bool:
        return self.contrast < LOW_CONTRAST

    @property
    def loses_detail(self) -> bool:
        return self.detail_retention < POOR_DETAIL_RETENTION

    @property
    def is_text_heavy(self) -> bool:
        return self.text_area_estimate > HEAVY_TEXT_AREA

    def observations(self) -> list[str]:
        """Plain-language readings of the measurements.

        Only genuine problems produce an entry. A thumbnail with nothing wrong
        returns an empty list rather than a reassuring "looks good!" — silence
        is what gives the warnings their weight.
        """
        notes: list[str] = []
        if self.loses_detail:
            notes.append(
                f"Only about {self.detail_retention * 100:.0f}% of its detail survives being "
                f"shown at {DISPLAY_SIZE[0]}×{DISPLAY_SIZE[1]} — most of what is in this "
                f"image never reaches the viewer before they decide."
            )
        if self.small_contrast and self.small_contrast < LOW_SMALL_CONTRAST:
            notes.append(
                f"Contrast collapses when scaled down (spread {self.small_contrast:.0f} at "
                f"display size, {self.contrast:.0f} at full size)."
            )
        elif self.is_low_contrast:
            notes.append(
                f"Low contrast (luminance spread {self.contrast:.0f}). Likely to blend "
                f"into the page at sidebar size."
            )
        if self.is_busy:
            notes.append(
                f"Visually busy ({self.edge_density * 100:.0f}% of pixels on a hard edge). "
                f"Detail this dense turns to mush when scaled down."
            )
        if self.is_text_heavy:
            notes.append(
                f"Text-like detail covers roughly {self.text_area_estimate * 100:.0f}% of the "
                f"frame — enough to compete with the image rather than support it."
            )
        if self.composition_note.startswith("far "):
            notes.append(
                f"Visual weight sits {self.composition_note}. YouTube crops thumbnails "
                f"differently across surfaces, and anything near an edge is what goes first."
            )
        if self.mean_brightness < 60:
            notes.append("Very dark overall — dark thumbnails lose ground against a light UI.")
        elif self.mean_brightness > 210:
            notes.append("Very bright overall, with little tonal range to anchor the eye.")
        if self.mean_saturation < 40:
            notes.append("Nearly desaturated — little colour to separate it from neighbours.")
        return notes

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "dominant_colors": self.dominant_colors,
            "mean_brightness": round(self.mean_brightness, 1),
            "mean_saturation": round(self.mean_saturation, 1),
            "contrast": round(self.contrast, 1),
            "edge_density": round(self.edge_density, 4),
            "small_contrast": round(self.small_contrast, 1),
            "detail_retention": round(self.detail_retention, 3),
            "weight_x": round(self.weight_x, 3),
            "weight_y": round(self.weight_y, 3),
            "composition_note": self.composition_note,
            "text_area_estimate": round(self.text_area_estimate, 4),
            "text_bands": self.text_bands,
            "is_busy": self.is_busy,
            "is_low_contrast": self.is_low_contrast,
            "loses_detail": self.loses_detail,
            "is_text_heavy": self.is_text_heavy,
            "observations": self.observations(),
        }


def _edge_array(image: Image.Image) -> "np.ndarray":
    """Edge magnitude for a greyscale image, as a float array in 0–255."""
    return np.asarray(image.filter(ImageFilter.FIND_EDGES), dtype=float)


def detail_retention(grey: Image.Image, size: tuple[int, int] = DISPLAY_SIZE) -> float:
    """How much of the image survives being shown at ``size``.

    Full-resolution sharpness is not what a viewer sees. This shrinks the image
    to the size it is genuinely chosen at, scales it back up, and measures how
    far the result has drifted from the original. Whatever cannot be
    reconstructed is information the viewer never receives before deciding
    whether to click.

    The difference is expressed against the image's own contrast, so a
    low-contrast image is not penalised twice — that is what
    :attr:`LocalFeatures.small_contrast` is for.

    Returns 0–1, higher meaning more survives.

    A few large shapes come through almost intact:

    >>> from PIL import Image, ImageDraw
    >>> clean = Image.new("L", (1280, 720), 30)
    >>> _d = ImageDraw.Draw(clean).rectangle([400, 200, 880, 520], fill=230)
    >>> detail_retention(clean) > 0.9
    True

    Fine detail does not:

    >>> busy = Image.effect_noise((1280, 720), 90)
    >>> detail_retention(busy) < 0.5
    True
    >>> detail_retention(clean) > detail_retention(busy)
    True

    A perfectly flat image has nothing to lose, which is not a legibility
    problem and must not be reported as one:

    >>> detail_retention(Image.new("L", (640, 360), 128))
    1.0
    """
    original = np.asarray(grey, dtype=float)
    spread = float(original.std())
    if spread <= 1.0:
        return 1.0

    shrunk = grey.resize(size, Image.Resampling.LANCZOS)
    restored = np.asarray(
        shrunk.resize(grey.size, Image.Resampling.LANCZOS), dtype=float
    )

    lost = float(np.abs(original - restored).mean())
    return float(max(0.0, min(1.0, 1.0 - lost / spread)))


def visual_weight(rgb: Image.Image) -> tuple[float, float]:
    """Centre of visual weight, as fractions of width and height.

    Weight combines edge energy with colour intensity — the two things that
    pull the eye — so it does not need to recognise anything to say where a
    thumbnail's attention sits. It is a centre of mass, not a subject detector:
    two subjects on opposite sides average to the middle, which the caller has
    to keep in mind.

    Weight is measured as *departure from the background*, not as absolute
    brightness or colour. Using absolute values makes a large uniform
    background contribute weight across the whole frame, which drags every
    centroid toward the middle — the first version of this did exactly that
    and reported "dead centre" for a subject jammed against the right edge.

    >>> from PIL import Image, ImageDraw
    >>> img = Image.new("RGB", (400, 200), (10, 10, 10))
    >>> _d = ImageDraw.Draw(img).ellipse([20, 40, 120, 160], fill=(250, 80, 40))
    >>> x, y = visual_weight(img)
    >>> x < 0.4            # weight sits on the left
    True
    >>> 0.35 < y < 0.65    # and vertically centred
    True

    A subject against the opposite edge lands on the opposite side:

    >>> img2 = Image.new("RGB", (400, 200), (10, 10, 10))
    >>> _d = ImageDraw.Draw(img2).ellipse([300, 40, 390, 160], fill=(250, 200, 40))
    >>> visual_weight(img2)[0] > 0.7
    True
    """
    grey = rgb.convert("L")
    luminance = np.asarray(grey, dtype=float)
    edges = _edge_array(grey)
    saturation = np.asarray(rgb.convert("HSV"), dtype=float)[:, :, 1]

    # Deviation from the typical pixel. A flat background sits at the median in
    # both channels and therefore contributes almost nothing.
    luminance_pull = np.abs(luminance - float(np.median(luminance)))
    colour_pull = np.abs(saturation - float(np.median(saturation)))

    weight = edges + luminance_pull + colour_pull * 0.5
    total = float(weight.sum())
    if total <= 0:
        return 0.5, 0.5

    height, width = weight.shape
    xs = np.arange(width, dtype=float)
    ys = np.arange(height, dtype=float)
    cx = float((weight.sum(axis=0) * xs).sum() / total)
    cy = float((weight.sum(axis=1) * ys).sum() / total)
    return cx / max(1, width - 1), cy / max(1, height - 1)


def describe_composition(x: float, y: float, tolerance: float = 0.09) -> str:
    """Name where the visual weight sits, in terms a person can act on.

    The rule of thirds is a convention rather than a law, so this reports the
    position and lets the reader decide; it never scores the composition.

    >>> describe_composition(0.34, 0.34)
    'on the upper-left third'
    >>> describe_composition(0.5, 0.5)
    'dead centre'
    >>> describe_composition(0.85, 0.5)
    'far right, close to the edge'
    >>> describe_composition(0.5, 0.2)
    'high and centred'
    """
    if x > 0.8 or x < 0.2:
        side = "right" if x > 0.5 else "left"
        return f"far {side}, close to the edge"

    thirds = (1 / 3, 2 / 3)
    near_x = min(thirds, key=lambda t: abs(x - t))
    near_y = min(thirds, key=lambda t: abs(y - t))
    if abs(x - near_x) <= tolerance and abs(y - near_y) <= tolerance:
        horizontal = "left" if near_x < 0.5 else "right"
        vertical = "upper" if near_y < 0.5 else "lower"
        return f"on the {vertical}-{horizontal} third"

    if abs(x - 0.5) <= tolerance and abs(y - 0.5) <= tolerance:
        return "dead centre"
    if abs(x - 0.5) <= tolerance:
        return "high and centred" if y < 0.5 else "low and centred"
    if abs(y - 0.5) <= tolerance:
        return "centred, offset left" if x < 0.5 else "centred, offset right"
    return "off-centre"


def text_like_regions(
    grey: Image.Image, cell: int = 20
) -> tuple[float, list[dict[str, float]]]:
    """Find dense, high-contrast bands that behave like overlaid text.

    Text has a signature that needs no OCR: locally busy, locally
    high-contrast, and arranged in horizontal runs. This measures that
    signature over a grid and groups the hits into bands.

    It is honestly an *estimate*. Fine texture — foliage, wood grain, gravel —
    has the same local statistics, so the number is reported as "text-like"
    everywhere it surfaces, never as "text".

    Returns ``(covered_fraction, bands)``.

    >>> from PIL import Image, ImageDraw
    >>> blank = Image.new("L", (640, 360), 128)
    >>> area, bands = text_like_regions(blank)
    >>> area == 0.0 and bands == []
    True

    A bright block of dense stripes across the lower third registers, and lands
    in the lower part of the frame:

    >>> striped = Image.new("L", (640, 360), 20)
    >>> d = ImageDraw.Draw(striped)
    >>> for x in range(40, 600, 6):
    ...     _ = d.rectangle([x, 250, x + 3, 320], fill=255)
    >>> area, bands = text_like_regions(striped)
    >>> area > 0.05
    True
    >>> bands[0]["top"] > 0.5
    True
    """
    array = np.asarray(grey, dtype=float)
    edges = _edge_array(grey)
    height, width = array.shape

    rows, cols = max(1, height // cell), max(1, width // cell)
    covered = np.zeros((rows, cols), dtype=bool)

    for r in range(rows):
        for c in range(cols):
            y0, y1 = r * cell, min(height, (r + 1) * cell)
            x0, x1 = c * cell, min(width, (c + 1) * cell)
            block_edges = edges[y0:y1, x0:x1]
            block_pixels = array[y0:y1, x0:x1]
            if block_edges.size == 0:
                continue
            # Both conditions must hold: busy *and* high-contrast. Busy alone
            # is texture; contrast alone is a plain hard-edged shape.
            covered[r, c] = block_edges.mean() > 26.0 and block_pixels.std() > 46.0

    fraction = float(covered.mean()) if covered.size else 0.0
    if fraction <= 0:
        return 0.0, []

    # Collapse to horizontal bands: text runs across, so a row of the grid with
    # several hits is far more likely to be text than isolated scattered cells.
    row_coverage = covered.mean(axis=1)
    bands: list[dict[str, float]] = []
    start: int | None = None
    for r in range(rows + 1):
        active = r < rows and row_coverage[r] >= 0.2
        if active and start is None:
            start = r
        elif not active and start is not None:
            band_rows = row_coverage[start:r]
            bands.append({
                "top": round(start / rows, 3),
                "bottom": round(r / rows, 3),
                "coverage": round(float(band_rows.mean()), 3),
            })
            start = None

    return round(fraction, 4), bands


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    return thumbnail_cache_dir() / f"{digest}.jpg"


def fetch_thumbnail(url: str, *, http: httpx.Client | None = None) -> Path:
    """Download a thumbnail to the local cache, or return the cached copy.

    Cached on the URL's hash. Because YouTube mints a new URL per image, a hit
    is always the right image and never a stale one.
    """
    path = _cache_path(url)
    if path.exists() and path.stat().st_size > 0:
        return path

    client = http or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        response = client.get(url)
        if response.status_code != 200:
            raise ThumbnailError(
                f"Thumbnail download failed with HTTP {response.status_code}."
            )
        path.write_bytes(response.content)
    except httpx.HTTPError as exc:
        raise ThumbnailError(f"Could not download the thumbnail: {exc}") from exc
    finally:
        if http is None:
            client.close()
    return path


def _to_hex(rgb: tuple[int, ...]) -> str:
    """Format an RGB triple as a CSS hex colour.

    >>> _to_hex((255, 0, 128))
    '#ff0080'
    >>> _to_hex((0, 0, 0))
    '#000000'
    """
    return "#{:02x}{:02x}{:02x}".format(*rgb[:3])


def analyse_image(image: Image.Image) -> LocalFeatures:
    """Measure colour, tone, and edge density.

    ``edge_density`` is the share of pixels sitting on a hard edge after a
    Sobel-style filter — a direct proxy for how much detail survives being
    scaled to sidebar size.
    """
    original_size = image.size
    rgb = image.convert("RGB")
    if rgb.width > ANALYSIS_WIDTH:
        ratio = ANALYSIS_WIDTH / rgb.width
        rgb = rgb.resize((ANALYSIS_WIDTH, max(1, int(rgb.height * ratio))))

    quantized = rgb.quantize(colors=5, method=Image.Quantize.FASTOCTREE)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], key=lambda c: c[0], reverse=True)
    dominant = [
        _to_hex(tuple(palette[index * 3 : index * 3 + 3]))
        for _count, index in counts[:5]
        if len(palette) >= index * 3 + 3
    ]

    grey = rgb.convert("L")
    grey_stat = ImageStat.Stat(grey)
    brightness = float(grey_stat.mean[0])
    contrast = float(grey_stat.stddev[0])

    hsv_stat = ImageStat.Stat(rgb.convert("HSV"))
    saturation = float(hsv_stat.mean[1])

    edges = grey.filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    total = sum(histogram) or 1
    # Pixels in the top ~75% of the intensity range are treated as real edges;
    # everything below is texture and compression noise.
    strong = sum(histogram[64:])
    edge_density = strong / total

    # Everything below judges the image at the size it is actually chosen at,
    # rather than at the resolution it was uploaded in.
    small_grey = grey.resize(DISPLAY_SIZE, Image.Resampling.LANCZOS)
    small_contrast = float(ImageStat.Stat(small_grey).stddev[0])
    retention = detail_retention(grey)

    wx, wy = visual_weight(rgb)
    text_area, text_bands = text_like_regions(grey)

    return LocalFeatures(
        width=original_size[0],
        height=original_size[1],
        dominant_colors=dominant,
        mean_brightness=brightness,
        mean_saturation=saturation,
        contrast=contrast,
        edge_density=edge_density,
        small_contrast=small_contrast,
        detail_retention=retention,
        weight_x=wx,
        weight_y=wy,
        composition_note=describe_composition(wx, wy),
        text_area_estimate=text_area,
        text_bands=text_bands,
    )


def analyse_path(path: Path) -> LocalFeatures:
    try:
        with Image.open(path) as image:
            return analyse_image(image)
    except (OSError, ValueError) as exc:
        raise ThumbnailError(f"Could not read the thumbnail image: {exc}") from exc


# --------------------------------------------------------------------------
# Vision
# --------------------------------------------------------------------------

#: Schema for the vision call. Kept as a raw JSON schema rather than a Pydantic
#: model so the exact field set is visible right here next to the prompt — the
#: two have to agree, and splitting them across files is how they drift apart.
VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "face_count": {
            "type": "integer",
            "description": "Number of clearly visible human faces.",
        },
        "dominant_emotion": {
            "type": "string",
            "enum": [
                "none", "neutral", "happy", "shocked", "angry",
                "sad", "confused", "excited", "smug", "fearful",
            ],
            "description": "Expression of the most prominent face, or 'none' if no face.",
        },
        "has_text": {"type": "boolean"},
        "text_content": {
            "type": "string",
            "description": "Overlaid text, transcribed exactly. Empty string if none.",
        },
        "text_area_fraction": {
            "type": "number",
            "description": "Approximate share of the frame covered by overlaid text, 0 to 1.",
        },
        "has_arrow_or_circle": {
            "type": "boolean",
            "description": "Whether a drawn arrow, circle, or highlight marker points at something.",
        },
        "subject": {
            "type": "string",
            "description": "The main subject as a short noun phrase.",
        },
        "composition": {
            "type": "string",
            "enum": ["closeup", "medium", "wide", "screenshot", "graphic", "collage"],
        },
        "clarity_score": {
            "type": "integer",
            "description": (
                "0-100: how well this reads at roughly 210x118 pixels, where it is "
                "actually chosen. Judge legibility only, not appeal."
            ),
        },
        "notes": {
            "type": "string",
            "description": (
                "One or two sentences on what works and what does not, written to be "
                "acted on. Name the specific problem, never a generic verdict."
            ),
        },
    },
    "required": [
        "face_count", "dominant_emotion", "has_text", "text_content",
        "text_area_fraction", "has_arrow_or_circle", "subject",
        "composition", "clarity_score", "notes",
    ],
    "additionalProperties": False,
}

VISION_PROMPT = """\
Analyse this YouTube thumbnail.

It will be seen at roughly 210x118 pixels on a desktop homepage, smaller on \
mobile, surrounded by a dozen competing thumbnails. Judge it in that context, \
not as a standalone image.

Report only what you can actually see. If there is no text, say so rather than \
inferring what the video is about. `clarity_score` is about legibility at that \
size — whether the subject and any text survive the scale-down — not about \
whether the image is attractive or whether you would click it."""


class VisionUnavailable(Exception):
    """No Anthropic API key configured, or the SDK call failed."""


def analyse_with_vision(
    image_path: Path, *, api_key: str, model: str, effort: str = "low"
) -> dict[str, Any]:
    """Run one thumbnail through Claude and return the structured fields.

    One image per call. Batching several into one request was considered and
    rejected: the model's attention gets split, per-image quality drops, and a
    single malformed response would lose the whole batch. Thumbnails are also
    analysed once and cached forever, so throughput is not the constraint.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise VisionUnavailable("The anthropic package is not installed.") from exc

    if not api_key:
        raise VisionUnavailable("No Anthropic API key is configured.")

    data = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": VISION_SCHEMA},
            },
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": data,
                            },
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError as exc:
        raise VisionUnavailable(
            "The Anthropic API key was rejected. Check it in Settings."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise VisionUnavailable(
            "Anthropic rate limit reached. Wait a moment and retry."
        ) from exc
    except anthropic.APIStatusError as exc:
        raise VisionUnavailable(f"Anthropic API error ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise VisionUnavailable("Could not reach the Anthropic API.") from exc

    if response.stop_reason == "refusal":
        raise VisionUnavailable("The model declined to analyse this image.")

    import json

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise VisionUnavailable("The model returned no analysis.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionUnavailable("The model returned malformed output.") from exc


def analyse_video_thumbnail(
    session: Session,
    video: Video,
    *,
    api_key: str = "",
    model: str = "claude-opus-5",
    effort: str = "low",
    use_vision: bool = True,
    http: httpx.Client | None = None,
) -> ThumbnailAnalysis:
    """Analyse one video's current thumbnail, reusing any cached analysis.

    Local features are always computed. Vision runs only when a key is present
    and ``use_vision`` is set, and a vision failure downgrades the result to
    local-only rather than losing the free measurements too.
    """
    if not video.thumbnail_url:
        raise ThumbnailError("This video has no thumbnail URL stored.")

    existing = session.scalar(
        select(ThumbnailAnalysis).where(
            ThumbnailAnalysis.video_id == video.id,
            ThumbnailAnalysis.thumbnail_url == video.thumbnail_url,
        )
    )
    # A cached row is reused unless it lacks vision fields and vision is now
    # available — that is the one case where re-running adds something.
    if existing is not None and (existing.model or not use_vision or not api_key):
        return existing

    path = fetch_thumbnail(video.thumbnail_url, http=http)
    local = analyse_path(path)

    row = existing or ThumbnailAnalysis(
        video_id=video.id, thumbnail_url=video.thumbnail_url
    )
    row.width = local.width
    row.height = local.height
    row.dominant_colors = local.dominant_colors
    row.mean_brightness = local.mean_brightness
    row.mean_saturation = local.mean_saturation
    row.contrast = local.contrast
    row.edge_density = local.edge_density
    row.small_contrast = local.small_contrast
    row.detail_retention = local.detail_retention
    row.weight_x = local.weight_x
    row.weight_y = local.weight_y
    row.composition_note = local.composition_note
    row.text_area_estimate = local.text_area_estimate
    row.text_bands = local.text_bands
    if existing is None:
        session.add(row)

    if use_vision and api_key:
        try:
            vision = analyse_with_vision(path, api_key=api_key, model=model, effort=effort)
        except VisionUnavailable as exc:
            log.warning("Vision analysis unavailable for %s: %s", video.id, exc)
        else:
            row.model = model
            row.face_count = vision.get("face_count")
            row.dominant_emotion = vision.get("dominant_emotion")
            row.has_text = vision.get("has_text")
            row.text_content = vision.get("text_content") or ""
            row.text_word_count = len((vision.get("text_content") or "").split())
            row.text_area_fraction = vision.get("text_area_fraction")
            row.has_arrow_or_circle = vision.get("has_arrow_or_circle")
            row.subject = vision.get("subject")
            row.composition = vision.get("composition")
            row.clarity_score = vision.get("clarity_score")
            row.notes = vision.get("notes")
            row.raw_response = vision

    session.flush()
    return row


def summarise_pattern(analyses: Sequence[ThumbnailAnalysis]) -> dict[str, Any]:
    """Describe what a set of thumbnails has in common.

    Run over a niche's outliers, this is the "visual formula" the paid tools
    sell — except the sample it describes is visible and the counts are shown,
    so a pattern drawn from six thumbnails can be recognised as such.
    """
    if not analyses:
        return {"sample_size": 0, "patterns": []}

    total = len(analyses)
    with_vision = [a for a in analyses if a.model]
    patterns: list[dict[str, Any]] = []

    def add(label: str, count: int, note: str = "") -> None:
        patterns.append(
            {
                "label": label,
                "count": count,
                "total": total,
                "share": round(count / total * 100),
                "note": note,
            }
        )

    if with_vision:
        vision_total = len(with_vision)
        faces = sum(1 for a in with_vision if (a.face_count or 0) > 0)
        text = sum(1 for a in with_vision if a.has_text)
        markers = sum(1 for a in with_vision if a.has_arrow_or_circle)
        patterns.append(
            {
                "label": "Shows a human face",
                "count": faces,
                "total": vision_total,
                "share": round(faces / vision_total * 100),
                "note": "",
            }
        )
        patterns.append(
            {
                "label": "Has overlaid text",
                "count": text,
                "total": vision_total,
                "share": round(text / vision_total * 100),
                "note": "",
            }
        )
        patterns.append(
            {
                "label": "Uses an arrow or circle marker",
                "count": markers,
                "total": vision_total,
                "share": round(markers / vision_total * 100),
                "note": "",
            }
        )

        emotions = [a.dominant_emotion for a in with_vision if a.dominant_emotion not in (None, "none")]
        if emotions:
            from collections import Counter

            emotion, count = Counter(emotions).most_common(1)[0]
            patterns.append(
                {
                    "label": f"Most common expression: {emotion}",
                    "count": count,
                    "total": len(emotions),
                    "share": round(count / len(emotions) * 100),
                    "note": "",
                }
            )

    # Local measurements, present for every analysed thumbnail whether or not
    # a vision model was ever involved.
    busy = sum(1 for a in analyses if (a.edge_density or 0) > BUSY_EDGE_DENSITY)
    low_contrast = sum(1 for a in analyses if (a.contrast or 999) < LOW_CONTRAST)
    fading = sum(
        1 for a in analyses
        if a.detail_retention is not None and a.detail_retention < POOR_DETAIL_RETENTION
    )
    text_heavy = sum(
        1 for a in analyses
        if a.text_area_estimate is not None and a.text_area_estimate > HEAVY_TEXT_AREA
    )
    off_centre = sum(
        1 for a in analyses if (a.composition_note or "").startswith("far ")
    )
    add("Visually busy", busy, "Dense detail that degrades when scaled down.")
    add("Low contrast", low_contrast, "Risks blending into the surrounding page.")
    add("Loses detail at display size", fading,
        f"Under {POOR_DETAIL_RETENTION * 100:.0f}% of their detail survives "
        f"{DISPLAY_SIZE[0]}×{DISPLAY_SIZE[1]}.")
    add("Heavy text-like coverage", text_heavy,
        "Overlay competing with the image rather than supporting it.")
    add("Weight near an edge", off_centre,
        "Vulnerable to how YouTube crops across surfaces.")

    colours: list[str] = []
    for analysis in analyses:
        colours.extend(analysis.dominant_colors or [])

    return {
        "sample_size": total,
        "vision_sample_size": len(with_vision),
        "patterns": patterns,
        "palette": colours[:40],
        "median_clarity": (
            sorted(a.clarity_score for a in with_vision if a.clarity_score is not None)[
                len([a for a in with_vision if a.clarity_score is not None]) // 2
            ]
            if any(a.clarity_score is not None for a in with_vision)
            else None
        ),
    }
