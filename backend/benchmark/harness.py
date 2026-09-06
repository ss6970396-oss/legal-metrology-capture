"""The benchmark harness.

§17: "No OCR technology should be considered selected until evaluated on a
project-specific benchmark." This is the thing that does that evaluation, and
the thing that turns the uncalibrated thresholds in ``quality.py`` into fitted
ones.

It runs every configured engine over the same annotated images and scores each
layer separately, so a comparison says *where* one engine beats another rather
than only that it does.

## What it cannot do

It cannot supply the dataset. §19's phase 6 — "ground-truth dataset:
field/region/geometry annotations available" — is field work: photograph real
Indian retail packaging under real conditions (glare, curved bottles, tiny
print, mixed scripts, a shop aisle's lighting) and annotate what each region
says and which field it is. Until that exists, every threshold in this codebase
remains a plausible guess, and this harness will report that it has nothing to
measure rather than producing a number that looks like a result.

Run it with::

    python -m benchmark.harness --dataset ./groundtruth --engines paddleocr,tesseract
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.domain.geometry import Geometry
from app.pipeline import attribution, normalize as nz, ocr, quality, resolver
from app.domain.observation import (
    ObservationStore,
    OcrObservation,
    OcrRun,
    Region,
    SurfaceView,
)

from .metrics import (
    BenchmarkReport,
    DetectionScore,
    FieldScore,
    character_error_rate,
    roc_auc,
    score_detection,
    sweep_threshold,
    word_error_rate,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

@dataclass
class TruthRegion:
    """One annotated region: where the text is, what it says, which field."""

    geometry: Geometry
    text: str
    #: Contract field name, or "" for text that is not a declaration.
    field: str = ""
    #: The normalized value a correct pipeline should produce.
    normalized_value: Any = None
    script: str = ""


@dataclass
class TruthImage:
    """One annotated photograph."""

    image_path: Path
    regions: list[TruthRegion]
    surface: SurfaceView = SurfaceView.UNKNOWN
    #: Conditions present, for slicing results — "glare", "curved",
    #: "tiny_print", "mixed_script". Which conditions a model fails on is more
    #: actionable than its average.
    conditions: list[str] = field(default_factory=list)
    #: Whether a human judged this image unusable for extraction. The label
    #: the quality thresholds are fitted against.
    extraction_failed: bool = False

    @property
    def declaration_regions(self) -> list[TruthRegion]:
        return [r for r in self.regions if r.field]


def load_dataset(root: Path) -> list[TruthImage]:
    """Load annotations from ``<root>/annotations.json``.

    Format::

        {
          "images": [
            {
              "image": "front_001.jpg",
              "surface": "front",
              "conditions": ["glare"],
              "extraction_failed": false,
              "regions": [
                {
                  "geometry": {"type": "QUADRILATERAL",
                               "coordinates": [[100,200],[420,200],[420,260],[100,260]]},
                  "text": "MRP Rs. 120",
                  "field": "package.mrp",
                  "normalized_value": {"amount": 120.0, "currency": "INR"}
                }
              ]
            }
          ]
        }
    """
    manifest = root / "annotations.json"
    if not manifest.exists():
        raise FileNotFoundError(
            f"no annotations.json in {root}. The benchmark needs a "
            "ground-truth dataset of real Indian retail packaging before any "
            "engine or threshold can be selected (contract §17, §19 phase 6)."
        )

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    images: list[TruthImage] = []

    for entry in raw.get("images", []):
        image_path = root / entry["image"]
        if not image_path.exists():
            log.warning("annotated image %s is missing; skipping", image_path)
            continue

        images.append(
            TruthImage(
                image_path=image_path,
                surface=SurfaceView.parse(entry.get("surface")),
                conditions=list(entry.get("conditions", [])),
                extraction_failed=bool(entry.get("extraction_failed", False)),
                regions=[
                    TruthRegion(
                        geometry=Geometry.from_json(r["geometry"]),
                        text=r.get("text", ""),
                        field=r.get("field", ""),
                        normalized_value=r.get("normalized_value"),
                        script=r.get("script", ""),
                    )
                    for r in entry.get("regions", [])
                ],
            )
        )
    return images


# ---------------------------------------------------------------------------
# Running one engine
# ---------------------------------------------------------------------------

def benchmark_engine(
    engine: ocr.OcrEngine, dataset: Sequence[TruthImage]
) -> BenchmarkReport:
    """Score one engine over the whole dataset."""
    report = BenchmarkReport(
        engine=engine.name,
        model_id=engine.model_id,
        model_version=engine.model_version,
        image_count=len(dataset),
    )

    all_predicted: list[Geometry] = []
    all_truth: list[Geometry] = []
    cers: list[float] = []
    wers: list[float] = []
    latencies: list[float] = []
    field_scores: dict[str, FieldScore] = {}

    for truth_image in dataset:
        started = time.monotonic()
        result = engine.recognize(truth_image.image_path)
        latencies.append((time.monotonic() - started) * 1000)

        predicted_geometries = [line.geometry for line in result.lines]
        truth_geometries = [r.geometry for r in truth_image.regions]
        all_predicted.extend(predicted_geometries)
        all_truth.extend(truth_geometries)

        # Recognition: score each prediction against its best-matching truth
        # region. Unmatched predictions are not scored for CER — they are a
        # detection failure, already counted there, and charging them twice
        # would conflate the two layers.
        for line in result.lines:
            best = _best_match(line.geometry, truth_image.regions)
            if best is not None:
                cers.append(character_error_rate(line.text, best.text))
                wers.append(word_error_rate(line.text, best.text))

        _score_fields(engine, result, truth_image, field_scores)

    report.detection = (
        score_detection(all_predicted, all_truth)
        if all_truth
        else DetectionScore(0, 0, 0, 0, 0, 0, 0)
    )
    report.mean_cer = statistics.fmean(cers) if cers else 0.0
    report.mean_wer = statistics.fmean(wers) if wers else 0.0
    report.field_scores = field_scores
    report.mean_latency_ms = statistics.fmean(latencies) if latencies else 0.0
    report.p95_latency_ms = (
        sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0
    )

    if not cers:
        report.notes.append(
            "No prediction matched any annotated region. Either the engine "
            "found nothing or the annotations are in a different coordinate "
            "space than the images."
        )
    return report


def _best_match(
    geometry: Geometry, regions: Sequence[TruthRegion], threshold: float = 0.5
) -> TruthRegion | None:
    best: TruthRegion | None = None
    best_iou = threshold
    for region in regions:
        iou = geometry.iou(region.geometry)
        if iou >= best_iou:
            best, best_iou = region, iou
    return best


def _score_fields(
    engine: ocr.OcrEngine,
    result: ocr.OcrResult,
    truth_image: TruthImage,
    scores: dict[str, FieldScore],
) -> None:
    """Run attribution over one image's predictions and score per field."""
    store = ObservationStore(extraction_run_id="BENCH", package_id="BENCH")
    run = OcrRun.create(
        engine=result.engine,
        model_id=result.model_id,
        model_version=result.model_version,
        languages=result.languages,
        preprocessing_version="benchmark",
    )
    pairs: list[tuple[Region, OcrObservation]] = []

    for line in result.lines:
        region = Region.create(
            artifact_id="BENCH-IMG",
            geometry=line.geometry,
            detection_confidence=line.confidence or 0.5,
        )
        observation = OcrObservation.create(
            region=region,
            run=run,
            raw_text=line.text,
            normalized_text=nz.clean_text(line.text),
            recognition_confidence=line.confidence or 0.5,
        )
        store.add_region(region)
        store.add_observation(observation)
        pairs.append((region, observation))

    candidates = attribution.attribute(
        pairs, {"BENCH-IMG": truth_image.surface}
    )
    for candidate in candidates:
        store.add_candidate(candidate)

    resolved = resolver.resolve(store)
    expected = {
        r.field: r.normalized_value for r in truth_image.declaration_regions
    }

    for field_name in set(expected) | set(resolver.ALWAYS_RESOLVED):
        score = scores.setdefault(field_name, FieldScore(field=field_name))
        fact = resolved.get(field_name)
        predicted_value = (
            fact.value if fact and fact.status.carries_value else None
        )
        truth_value = expected.get(field_name)

        if predicted_value is not None and truth_value is not None:
            score.true_positives += 1
            score.value_comparisons += 1
            if resolver.values_equal(field_name, predicted_value, truth_value):
                score.value_matches += 1
        elif predicted_value is not None:
            score.false_positives += 1
        elif truth_value is not None:
            score.false_negatives += 1


# ---------------------------------------------------------------------------
# Threshold calibration
# ---------------------------------------------------------------------------

def calibrate_quality_thresholds(
    dataset: Sequence[TruthImage],
    policy: quality.QualityPolicy = quality.DEFAULT_POLICY,
) -> dict[str, Any]:
    """Measure every quality feature against actual extraction failure.

    This is what §14 requires before any threshold stops being a guess: fit the
    cutoff to the data rather than choosing a plausible-sounding number.

    Reports the AUC first for each feature, because that answers whether the
    feature separates failures from successes at all. A feature near 0.5 is
    noise and no cutoff of it will predict anything — tuning its threshold is
    wasted effort, and shipping one is worse than shipping none.
    """
    by_feature: dict[str, list[tuple[float, bool]]] = {}

    for truth_image in dataset:
        report = quality.analyse(
            truth_image.image_path, "BENCH", policy
        )
        if report.error:
            log.warning(
                "quality diagnostics failed for %s: %s",
                truth_image.image_path,
                report.error,
            )
            continue
        for measurement in report.measurements:
            by_feature.setdefault(measurement.name, []).append(
                (measurement.value, truth_image.extraction_failed)
            )

    # Which direction of the feature indicates trouble. Blur and contrast are
    # bad when low; glare and clipping are bad when high.
    lower_is_worse = {
        "blur_laplacian_variance": True,
        "rms_contrast": True,
        "mean_luminance": True,
        "clipped_fraction": False,
        "largest_specular_blob_fraction": False,
        "dominant_edge_skew_degrees": False,
    }

    out: dict[str, Any] = {}
    for feature, samples in sorted(by_feature.items()):
        auc = roc_auc(samples)
        candidates = sweep_threshold(
            feature, samples, lower_is_worse.get(feature, True)
        )
        out[feature] = {
            "roc_auc": None if auc != auc else round(auc, 4),  # NaN check
            "sample_count": len(samples),
            "separates_failures": (auc == auc and abs(auc - 0.5) > 0.15),
            "candidates": [c.to_json() for c in candidates],
        }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark OCR engines and calibrate quality thresholds.",
    )
    parser.add_argument(
        "--dataset", type=Path, required=True,
        help="directory containing annotations.json and the images",
    )
    parser.add_argument(
        "--engines", default="paddleocr,tesseract",
        help="comma-separated engine names",
    )
    parser.add_argument(
        "--languages", default="en,devanagari",
        help="comma-separated OCR languages",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("benchmark-report.json"),
    )
    parser.add_argument("--calibrate", action="store_true",
                        help="also sweep quality thresholds")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        dataset = load_dataset(args.dataset)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not dataset:
        print(
            "ERROR: the dataset is empty. Engine selection and threshold "
            "calibration both require real annotated packaging photographs; "
            "there is nothing to measure.",
            file=sys.stderr,
        )
        return 2

    languages = tuple(s.strip() for s in args.languages.split(",") if s.strip())
    reports: list[dict[str, Any]] = []

    for name in (s.strip() for s in args.engines.split(",") if s.strip()):
        try:
            engine = ocr.build_engine(name, languages=languages)
        except KeyError as exc:
            log.error("%s", exc)
            continue
        if not engine.is_available():
            log.warning("engine %s is not installed; skipping", name)
            continue

        log.info("benchmarking %s over %d images", name, len(dataset))
        reports.append(benchmark_engine(engine, dataset).to_json())

    payload: dict[str, Any] = {
        "dataset": str(args.dataset),
        "image_count": len(dataset),
        "engines": reports,
    }

    if args.calibrate:
        log.info("sweeping quality thresholds")
        payload["quality_calibration"] = calibrate_quality_thresholds(dataset)

    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("wrote %s", args.out)

    if not reports:
        print(
            "WARNING: no engine ran. Nothing was measured, so nothing is "
            "selected.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
