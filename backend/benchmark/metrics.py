"""Scoring functions for the benchmark.

§17's metric table, implemented. Each layer of the pipeline is scored on its
own terms, because a single end-to-end number cannot tell you whether a wrong
MRP came from a missed detection, a misread digit or a correct read attributed
to the wrong field — and those three have completely different fixes.

Everything here is pure. Given the same predictions and ground truth it returns
the same numbers, so a benchmark run is reproducible and two model versions are
comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.domain.geometry import Geometry


# ---------------------------------------------------------------------------
# Recognition: CER and WER
# ---------------------------------------------------------------------------

def _levenshtein(a: Sequence[Any], b: Sequence[Any]) -> int:
    """Edit distance, two rows at a time.

    Used for both character and word error rates. The two-row form keeps memory
    at O(min(n,m)) rather than O(n*m), which matters when a dense regulatory
    panel produces a few thousand characters per region.
    """
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,          # deletion
                    current[j - 1] + 1,       # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(predicted: str, truth: str) -> float:
    """CER = edit distance / length of truth.

    Can exceed 1.0 when the prediction is longer than the truth — a
    hallucinated run of text is worse than saying nothing, and the metric
    should say so rather than saturating at 1.0.
    """
    if not truth:
        return 0.0 if not predicted else 1.0
    return _levenshtein(predicted, truth) / len(truth)


def word_error_rate(predicted: str, truth: str) -> float:
    truth_words = truth.split()
    if not truth_words:
        return 0.0 if not predicted.split() else 1.0
    return _levenshtein(predicted.split(), truth_words) / len(truth_words)


# ---------------------------------------------------------------------------
# Detection: precision, recall, H-mean
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectionScore:
    """Scene-text detection metrics.

    §17 and §18 of the first report specify precision, recall and H-mean, which
    is the convention EAST and the wider scene-text literature use.
    """

    precision: float
    recall: float
    hmean: float
    true_positives: int
    false_positives: int
    false_negatives: int
    #: Mean IoU over the matched pairs. §18's "tightness" signal: a detector
    #: can score a perfect H-mean with boxes that are consistently too loose,
    #: and loose boxes crop badly, which costs recognition accuracy later.
    mean_iou: float

    def to_json(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "hmean": round(self.hmean, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "mean_iou": round(self.mean_iou, 4),
        }


def score_detection(
    predicted: Sequence[Geometry],
    truth: Sequence[Geometry],
    iou_threshold: float = 0.5,
) -> DetectionScore:
    """Greedy one-to-one matching at an IoU threshold.

    Greedy by descending IoU rather than optimal assignment. The difference is
    marginal on text layouts, where regions rarely overlap enough for the
    optimal matching to differ, and greedy keeps the score explainable — you
    can point at which prediction matched which truth box.
    """
    pairs: list[tuple[float, int, int]] = []
    for i, p in enumerate(predicted):
        for j, t in enumerate(truth):
            iou = p.iou(t)
            if iou >= iou_threshold:
                pairs.append((iou, i, j))
    pairs.sort(reverse=True)

    used_predictions: set[int] = set()
    used_truths: set[int] = set()
    matched_ious: list[float] = []

    for iou, i, j in pairs:
        if i in used_predictions or j in used_truths:
            continue
        used_predictions.add(i)
        used_truths.add(j)
        matched_ious.append(iou)

    tp = len(matched_ious)
    fp = len(predicted) - tp
    fn = len(truth) - tp

    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(truth) if truth else 0.0
    hmean = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return DetectionScore(
        precision=precision,
        recall=recall,
        hmean=hmean,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        mean_iou=sum(matched_ious) / len(matched_ious) if matched_ious else 0.0,
    )


# ---------------------------------------------------------------------------
# Field attribution and normalization
# ---------------------------------------------------------------------------

@dataclass
class FieldScore:
    """Per-field precision, recall and F1, plus exact-value match.

    Two numbers, and the gap between them is the interesting part. A field can
    be attributed correctly (the pipeline knew that region was the MRP) while
    the normalized value is wrong (it read 180 instead of 120). Attribution F1
    catches the first failure; ``value_exact_match`` catches the second. A
    single combined score would hide which one is happening.
    """

    field: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    value_matches: int = 0
    value_comparisons: int = 0

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0

    @property
    def value_exact_match(self) -> float:
        return (
            self.value_matches / self.value_comparisons
            if self.value_comparisons
            else 0.0
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "value_exact_match": round(self.value_exact_match, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
        }


# ---------------------------------------------------------------------------
# Quality-threshold calibration
# ---------------------------------------------------------------------------

@dataclass
class ThresholdCandidate:
    """One candidate cutoff for a quality feature, with its consequences.

    This is the output §14 needs before a threshold can stop being a guess.
    The right cutoff is "the threshold that predicts unacceptable field-level
    extraction performance", so each candidate is scored against whether the
    artifact actually failed extraction — not against anyone's intuition about
    what a blurry photograph looks like.
    """

    feature: str
    threshold: float
    #: Correctly sent back: the image really did fail extraction.
    true_retakes: int
    #: Sent back needlessly. Costs a second trip to a premises.
    false_retakes: int
    #: Let through and it failed. Costs a fact nobody could extract.
    missed_failures: int
    correctly_passed: int

    @property
    def retake_precision(self) -> float:
        total = self.true_retakes + self.false_retakes
        return self.true_retakes / total if total else 0.0

    @property
    def retake_recall(self) -> float:
        total = self.true_retakes + self.missed_failures
        return self.true_retakes / total if total else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "threshold": round(self.threshold, 4),
            "retake_precision": round(self.retake_precision, 4),
            "retake_recall": round(self.retake_recall, 4),
            "true_retakes": self.true_retakes,
            "false_retakes": self.false_retakes,
            "missed_failures": self.missed_failures,
            "correctly_passed": self.correctly_passed,
        }


def sweep_threshold(
    feature: str,
    samples: Sequence[tuple[float, bool]],
    lower_is_worse: bool = True,
) -> list[ThresholdCandidate]:
    """Score every candidate cutoff for one quality feature.

    ``samples`` is ``(measured_value, extraction_failed)`` pairs. The candidate
    cutoffs are the observed values themselves, so the sweep covers exactly the
    decision boundaries the data supports rather than an arbitrary grid.

    Returns every candidate rather than picking one. Choosing the operating
    point is a judgement about the relative cost of a needless retake against a
    missed failure, and that is a product decision — an inspector sent back to
    a premises they have left is a real cost, and so is a fact nobody could
    extract. The sweep lays out the trade-off; a person picks the point.
    """
    if not samples:
        return []

    candidates: list[ThresholdCandidate] = []
    for threshold in sorted({value for value, _ in samples}):
        true_retakes = false_retakes = missed = passed = 0
        for value, failed in samples:
            flagged = value < threshold if lower_is_worse else value > threshold
            if flagged and failed:
                true_retakes += 1
            elif flagged and not failed:
                false_retakes += 1
            elif not flagged and failed:
                missed += 1
            else:
                passed += 1

        candidates.append(
            ThresholdCandidate(
                feature=feature,
                threshold=threshold,
                true_retakes=true_retakes,
                false_retakes=false_retakes,
                missed_failures=missed,
                correctly_passed=passed,
            )
        )
    return candidates


def roc_auc(samples: Sequence[tuple[float, bool]]) -> float:
    """Area under the ROC curve for a quality feature against failure.

    §17 asks for the correlation between quality features and OCR/field
    failure. AUC answers the question that actually matters before any
    threshold is chosen: does this feature separate the failures from the
    successes at all? A feature at 0.5 is noise, and no cutoff of it will
    predict anything however carefully it is tuned.

    Computed via the Mann-Whitney U identity, with ties counted as half, so
    there is no dependence on a plotting library.
    """
    positives = [v for v, failed in samples if failed]
    negatives = [v for v, failed in samples if not failed]
    if not positives or not negatives:
        return float("nan")

    wins = 0.0
    for p in positives:
        for n in negatives:
            if p < n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


@dataclass
class BenchmarkReport:
    """Everything one benchmark run measured."""

    engine: str
    model_id: str
    model_version: str
    image_count: int
    detection: DetectionScore | None = None
    mean_cer: float = 0.0
    mean_wer: float = 0.0
    field_scores: dict[str, FieldScore] = field(default_factory=dict)
    snapshot_exact_match: float = 0.0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "image_count": self.image_count,
            "detection": self.detection.to_json() if self.detection else None,
            "recognition": {
                "mean_cer": round(self.mean_cer, 4),
                "mean_wer": round(self.mean_wer, 4),
            },
            "fields": {
                name: score.to_json()
                for name, score in sorted(self.field_scores.items())
            },
            "end_to_end": {
                "snapshot_exact_match": round(self.snapshot_exact_match, 4),
            },
            "latency": {
                "mean_ms": round(self.mean_latency_ms, 1),
                "p95_ms": round(self.p95_latency_ms, 1),
            },
            "notes": self.notes,
        }
