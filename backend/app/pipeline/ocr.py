"""OCR engines behind one interface.

§8 decomposes OCR into detection and recognition and insists the engine stay
replaceable: PaddleOCR PP-OCRv5 is the *primary candidate*, not a selection,
and §12 is explicit that production selection requires the project benchmark on
Indian retail packaging. So nothing above this module names an engine. The
pipeline asks a :class:`OcrEngine` for lines and gets back geometry, text and
confidence, whichever implementation is configured.

That indirection is what makes §17's benchmark harness possible at all: the
same images run through every engine, scored the same way, with only this
module's registry changing.

Two things every engine here must do:

* Return geometry in the coordinate space of the image it was given, and say
  which space that is. The caller maps back to the original.
* Report a recognition confidence, or say it has none. A fabricated 1.0 would
  flow into fact confidence and make an unmeasured quantity look measured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from ..domain.geometry import CoordinateSpace, Geometry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrLine:
    """One recognised line of text with where it was found.

    ``confidence`` is ``None`` when the engine does not report one. Callers
    must handle that rather than defaulting: §7 requires knowing which
    component scores are real, and a substituted value is indistinguishable
    from a measured one once stored.
    """

    text: str
    geometry: Geometry
    confidence: float | None
    script: str = ""
    language: str = ""


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...]
    engine: str
    model_id: str
    model_version: str
    languages: tuple[str, ...]
    #: Wall-clock milliseconds, for the latency budget in §17.
    duration_ms: int = 0
    #: Populated when the engine ran but produced nothing usable.
    error: str | None = None


class OcrEngine(Protocol):
    """What the pipeline needs from any recognizer."""

    name: str
    model_id: str
    model_version: str
    languages: tuple[str, ...]

    def is_available(self) -> bool:
        """Whether this engine can run in the current environment.

        Checked rather than assumed because the heavy engines are optional
        dependencies — a machine running the contract tests should not need
        PaddlePaddle installed.
        """
        ...

    def recognize(self, image_path: Path) -> OcrResult:
        """Detect and recognise text in one image."""
        ...


# ---------------------------------------------------------------------------
# PaddleOCR — the primary candidate
# ---------------------------------------------------------------------------

class PaddleOcrEngine:
    """PP-OCRv5 multilingual.

    The primary *candidate* per §12, chosen because the current PP-OCRv5
    documentation lists Devanagari/Hindi, Marathi, Tamil, Telugu and English
    models — which is the language coverage Indian retail packaging actually
    needs, and more than the on-device assist can offer. It is not a selection:
    §12's caveat is that accuracy on this domain "must be measured", and until
    the benchmark runs this is a hypothesis with good documentation.

    Imported lazily. PaddlePaddle is a large dependency and a machine that only
    runs the domain tests should not have to install it.
    """

    name = "paddleocr"

    def __init__(
        self,
        languages: Sequence[str] = ("en",),
        model_version: str = "PP-OCRv5",
        use_angle_classification: bool = True,
    ) -> None:
        self.languages = tuple(languages)
        self.model_id = f"pp-ocrv5-{'-'.join(self.languages)}"
        self.model_version = model_version
        self._use_angle_cls = use_angle_classification
        self._engines: dict[str, Any] = {}

    def is_available(self) -> bool:
        try:
            import paddleocr  # noqa: F401
        except ImportError:
            return False
        return True

    def _engine_for(self, language: str) -> Any:
        if language not in self._engines:
            from paddleocr import PaddleOCR

            self._engines[language] = PaddleOCR(
                use_angle_cls=self._use_angle_cls,
                lang=language,
                show_log=False,
            )
        return self._engines[language]

    def recognize(self, image_path: Path) -> OcrResult:
        import time

        if not self.is_available():
            return OcrResult(
                lines=(),
                engine=self.name,
                model_id=self.model_id,
                model_version=self.model_version,
                languages=self.languages,
                error="paddleocr is not installed",
            )

        started = time.monotonic()
        lines: list[OcrLine] = []
        errors: list[str] = []

        # One pass per configured language. PaddleOCR's models are per-script,
        # so a bilingual Hindi/English panel genuinely needs two passes; the
        # resolver de-duplicates the overlap afterwards.
        for language in self.languages:
            try:
                engine = self._engine_for(language)
                raw = engine.ocr(str(image_path), cls=self._use_angle_cls)
            except Exception as exc:  # noqa: BLE001 - engine faults are data
                errors.append(f"{language}: {exc}")
                continue

            for block in raw or []:
                for entry in block or []:
                    line = _paddle_entry_to_line(entry, language)
                    if line is not None:
                        lines.append(line)

        duration_ms = int((time.monotonic() - started) * 1000)
        return OcrResult(
            lines=tuple(lines),
            engine=self.name,
            model_id=self.model_id,
            model_version=self.model_version,
            languages=self.languages,
            duration_ms=duration_ms,
            error="; ".join(errors) if errors and not lines else None,
        )


def _paddle_entry_to_line(entry: Any, language: str) -> OcrLine | None:
    """Convert one PaddleOCR result entry.

    Paddle returns ``[quad_points, (text, confidence)]``. The quad is preserved
    as a QUADRILATERAL rather than flattened to a box — §9's rule, and the
    reason the detector's four corners are worth having on perspective-
    distorted packaging.
    """
    try:
        quad, (text, confidence) = entry[0], entry[1]
    except (IndexError, TypeError, ValueError):
        return None

    if not text or not str(text).strip():
        return None

    try:
        geometry = Geometry.quad(
            [(float(p[0]), float(p[1])) for p in quad],
            space=CoordinateSpace.ORIGINAL_IMAGE_PX,
        )
    except (TypeError, ValueError, IndexError):
        return None

    return OcrLine(
        text=str(text),
        geometry=geometry,
        confidence=float(confidence) if confidence is not None else None,
        language=language,
    )


# ---------------------------------------------------------------------------
# Tesseract — the transparent baseline
# ---------------------------------------------------------------------------

class TesseractEngine:
    """Tesseract via pytesseract's TSV output.

    §21 keeps this as a baseline rather than a production path: it emits
    word-level boxes and confidences, which makes it useful for regression
    diagnosis and for sanity-checking a suspicious PaddleOCR result, but it is
    more preprocessing-sensitive than a modern scene-text stack and packaging
    is scene text.

    Words are grouped back into lines using Tesseract's own block/paragraph/line
    numbering rather than by geometry, because those fields are exactly the
    reading order the engine already determined.
    """

    name = "tesseract"

    def __init__(self, languages: Sequence[str] = ("eng",)) -> None:
        self.languages = tuple(languages)
        self.model_id = f"tesseract-{'+'.join(self.languages)}"
        self.model_version = "unknown"

    def is_available(self) -> bool:
        try:
            import pytesseract

            self.model_version = str(pytesseract.get_tesseract_version())
            return True
        except Exception:  # noqa: BLE001 - missing binary or missing package
            return False

    def recognize(self, image_path: Path) -> OcrResult:
        import time

        if not self.is_available():
            return OcrResult(
                lines=(),
                engine=self.name,
                model_id=self.model_id,
                model_version=self.model_version,
                languages=self.languages,
                error="tesseract is not installed",
            )

        import pytesseract
        from PIL import Image

        started = time.monotonic()
        try:
            with Image.open(image_path) as image:
                data = pytesseract.image_to_data(
                    image,
                    lang="+".join(self.languages),
                    output_type=pytesseract.Output.DICT,
                )
        except Exception as exc:  # noqa: BLE001
            return OcrResult(
                lines=(),
                engine=self.name,
                model_id=self.model_id,
                model_version=self.model_version,
                languages=self.languages,
                error=str(exc),
            )

        lines = _tesseract_tsv_to_lines(data)
        return OcrResult(
            lines=tuple(lines),
            engine=self.name,
            model_id=self.model_id,
            model_version=self.model_version,
            languages=self.languages,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _tesseract_tsv_to_lines(data: dict[str, list[Any]]) -> list[OcrLine]:
    grouped: dict[tuple[int, int, int, int], list[int]] = {}
    for index, text in enumerate(data.get("text", [])):
        if not str(text).strip():
            continue
        try:
            conf = float(data["conf"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            conf = -1.0
        # Tesseract marks non-text detections with -1.
        if conf < 0:
            continue
        key = (
            int(data["page_num"][index]),
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        grouped.setdefault(key, []).append(index)

    lines: list[OcrLine] = []
    for indices in grouped.values():
        words = [str(data["text"][i]).strip() for i in indices]
        confidences = [float(data["conf"][i]) / 100.0 for i in indices]
        lefts = [int(data["left"][i]) for i in indices]
        tops = [int(data["top"][i]) for i in indices]
        rights = [
            int(data["left"][i]) + int(data["width"][i]) for i in indices
        ]
        bottoms = [
            int(data["top"][i]) + int(data["height"][i]) for i in indices
        ]

        lines.append(
            OcrLine(
                text=" ".join(words),
                geometry=Geometry.bbox(
                    min(lefts), min(tops), max(rights), max(bottoms)
                ),
                # Mean over the line's words. Tesseract has no line-level
                # score, and the mean is what the TSV supports; a min would
                # let one bad word condemn a readable line.
                confidence=sum(confidences) / len(confidences),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Deterministic engine for tests and contract work
# ---------------------------------------------------------------------------

@dataclass
class StubOcrEngine:
    """Returns a scripted result for a given image path.

    Not a mock in the testing-library sense — it is a real engine that happens
    to read its answers from a table. It exists so the contract behaviour
    (projection, quarantine, conflict handling) can be tested exhaustively
    without a model, a GPU or a corpus, and so those tests stay fast enough to
    run on every change.
    """

    name: str = "stub"
    model_id: str = "stub-ocr"
    model_version: str = "1.0.0"
    languages: tuple[str, ...] = ("en",)
    responses: dict[str, tuple[OcrLine, ...]] = field(default_factory=dict)

    def is_available(self) -> bool:
        return True

    def script(self, image_path: str, lines: Sequence[OcrLine]) -> None:
        self.responses[image_path] = tuple(lines)

    def recognize(self, image_path: Path) -> OcrResult:
        return OcrResult(
            lines=self.responses.get(str(image_path), ()),
            engine=self.name,
            model_id=self.model_id,
            model_version=self.model_version,
            languages=self.languages,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: The languages worth running on Indian retail packaging.
#:
#: `en` and `devanagari` cover the overwhelming majority; Tamil and Telugu are
#: listed because PP-OCRv5 has models for them and §5's evidence requirements
#: do not stop at the Hindi belt. Which of these a deployment actually enables
#: is a latency decision — each is another pass over every image — and §24's
#: open question about initial language scope has not been answered.
DEFAULT_LANGUAGES: tuple[str, ...] = ("en", "devanagari")

_REGISTRY: dict[str, Any] = {}


def register(name: str, factory: Any) -> None:
    _REGISTRY[name] = factory


def build_engine(name: str, **kwargs: Any) -> OcrEngine:
    """Construct an engine by name.

    Raises on an unknown name rather than falling back to a default. An
    extraction run that silently used a different recognizer than the one
    configured would produce results nobody could interpret.
    """
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown OCR engine {name!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name](**kwargs)


def available_engines() -> list[str]:
    """Engines whose dependencies are actually installed here."""
    out: list[str] = []
    for name, factory in _REGISTRY.items():
        try:
            if factory().is_available():
                out.append(name)
        except Exception as exc:  # noqa: BLE001
            log.debug("engine %s unavailable: %s", name, exc)
    return sorted(out)


register("paddleocr", lambda **kw: PaddleOcrEngine(**kw))
register("tesseract", lambda **kw: TesseractEngine(**kw))
register("stub", lambda **kw: StubOcrEngine(**kw))
