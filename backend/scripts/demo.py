"""Run the extraction pipeline end to end and print what it produced.

A verification tool, not a test. The tests assert; this shows. Run it to see a
real PackageFactSnapshot come out of the pipeline without installing
PaddleOCR, OpenCV or standing up a server:

    python scripts/demo.py

It uses the stub OCR engine, which returns scripted text instead of reading an
image. Everything downstream of recognition — geometry, region quality,
attribution, normalization, fact resolution, projection, contract validation —
is the real code path, the same one a production run takes.

Four scenarios, each demonstrating a contract rule that is easy to get wrong:

  1. A normal two-view package        → OBSERVED facts with provenance
  2. Panels that disagree on the MRP  → CONFLICTING, null projection
  3. A package nothing could be read from → UNKNOWN, never DECLARED_ABSENCE
  4. MRP and quantity but no printed unit price → DERIVED with a trace
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/demo.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.geometry import Geometry  # noqa: E402
from app.domain.observation import SurfaceView  # noqa: E402
from app.domain.snapshot import CommercialContext, Jurisdiction  # noqa: E402
from app.pipeline.ocr import OcrLine, StubOcrEngine  # noqa: E402
from app.pipeline.runner import (  # noqa: E402
    ArtifactInput,
    ExtractionRequest,
    PipelineConfig,
    run_extraction,
)

# The Windows console defaults to cp1252, which cannot encode the box-drawing
# and typographic characters used below. Without this the script dies partway
# through printing a provenance trail — on the exact platform this project is
# developed on.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Colour only when writing to a terminal. Piped into a file or a pager the
# escape codes are noise that makes the output harder to read, not easier.
_TTY = sys.stdout.isatty()

GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
BOLD = "\033[1m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if code else text


def banner(title: str) -> None:
    print()
    print(colour("=" * 74, DIM))
    print(colour(f"  {title}", BOLD))
    print(colour("=" * 74, DIM))


def line(text: str, top: int, left: int = 100) -> OcrLine:
    """One recognised line of text, 40px tall, at a given position."""
    return OcrLine(
        text=text,
        geometry=Geometry.bbox(left, top, left + 340, top + 40),
        confidence=0.96,
    )


def make_artifact(
    directory: Path,
    name: str,
    artifact_id: str,
    index: int,
    surface: SurfaceView,
) -> ArtifactInput:
    """Write a placeholder image file and describe it to the pipeline.

    The bytes are not a real JPEG and do not need to be: the stub recognizer
    is keyed by path, and the only thing the pipeline does with the file is
    hash it for the integrity check.
    """
    path = directory / name
    payload = f"placeholder-image-{name}".encode()
    path.write_bytes(payload)

    return ArtifactInput(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        local_path=path,
        sequence_index=index,
        captured_at=datetime.now(timezone.utc),
        width_px=4032,
        height_px=3024,
        surface=surface,
    )


def make_request(artifacts: list[ArtifactInput]) -> ExtractionRequest:
    return ExtractionRequest(
        capture_session_id="CS-demo",
        package_id="PKG-demo",
        jurisdiction=Jurisdiction(country="IN", state="Maharashtra"),
        # Declared by the inspector, never inferred from the photographs.
        commercial_context=CommercialContext(
            sale_channel="RETAIL",
            is_imported=False,
            is_for_retail=True,
            is_ecommerce_listing=False,
        ),
        artifacts=artifacts,
        coverage={"complete": True, "required_surfaces": 2},
    )


def show_facts(result) -> None:
    """Print each fact with its status, value and where it came from."""
    payload = result.snapshot.to_json()

    print()
    print(colour("  facts[] — the authoritative record", BOLD))
    print()

    style = {
        "OBSERVED": GREEN,
        "DERIVED": YELLOW,
        "CONFLICTING": RED,
        "UNKNOWN": DIM,
    }

    for fact in payload["facts"]:
        status = fact["status"]
        marker = colour(f"{status:<10}", style.get(status, ""))
        field = fact["field"]

        value = fact["value"]
        rendered = "—" if value is None else json.dumps(value, ensure_ascii=False)
        if len(rendered) > 44:
            rendered = rendered[:41] + "..."

        print(f"    {marker} {field:<34} {rendered}")

        provenance = fact.get("provenance")
        if provenance:
            left, top = provenance["geometry"]["coordinates"][0]
            supporting = len(provenance.get("supporting", []))
            trail = (
                f"        {DIM}└ {provenance['artifact_id']} "
                f"at ({int(left)},{int(top)}) "
                f"via {provenance['ocr_run_id'][:12]}…"
            )
            if supporting:
                trail += f" +{supporting} agreeing view(s)"
            print(trail + RESET)

        if fact.get("conflicts"):
            for conflict in fact["conflicts"]:
                shown = json.dumps(conflict["value"], ensure_ascii=False)
                print(
                    f"        {DIM}└ candidate {shown} "
                    f"@ {conflict['confidence']:.2f} "
                    f"from {conflict['provenance']['artifact_id']}{RESET}"
                )

        if fact.get("derivation"):
            print(
                f"        {DIM}└ derived by {fact['derivation']['rule_id']} "
                f"from {len(fact['derivation']['source_fact_ids'])} "
                f"source fact(s){RESET}"
            )

    print()
    print(colour("  Typed projection — generated from facts[], never written", BOLD))
    print()
    for block in ("package", "declarations"):
        for key, value in payload[block].items():
            rendered = "null" if value is None else json.dumps(
                value, ensure_ascii=False
            )
            if len(rendered) > 40:
                rendered = rendered[:37] + "..."
            tint = DIM if value is None else ""
            print(f"    {colour(f'{block}.{key:<28}', tint)} {rendered}")


def scenario_normal(workdir: Path) -> None:
    banner("1. A normal package, photographed front and back")

    engine = StubOcrEngine()
    front = make_artifact(workdir, "front.jpg", "IMG-1", 1, SurfaceView.FRONT)
    back = make_artifact(workdir, "back.jpg", "IMG-2", 2, SurfaceView.BACK)

    engine.script(
        str(front.local_path),
        [
            line("ACME GOLD", 60),
            line("Generic Name: Refined Sunflower Oil", 140),
            line("Net Quantity: 500 g", 220),
            line("MRP Rs. 120 (incl. of all taxes)", 300),
        ],
    )
    engine.script(
        str(back.local_path),
        [
            line("Manufactured by: Acme Foods Ltd, Pune 411001", 100),
            line("Country of Origin: India", 180),
            line("Mfg Date: 07/2026", 260),
            line("Best Before: 01/2027", 340),
            line("Consumer Care: care@acme.example, 1800 123 4567", 420),
            # Same MRP on the back panel: agreement, not a second fact.
            line("MRP Rs. 120", 500),
        ],
    )

    result = run_extraction(
        make_request([front, back]), PipelineConfig(engine=engine)
    )

    print(f"\n  Released to compliance engine: {result.released}")
    print(f"  Regions detected: {len(result.store.regions)}")
    print(f"  Candidates proposed: {len(result.store.candidates)}")
    print(f"  Snapshot hash: {result.snapshot.content_hash()[:16]}…")
    show_facts(result)

    print()
    print(
        colour(
            "  Note: MRP was read on BOTH panels. It is one fact with the "
            "second\n  view attached as supporting provenance — not two facts, "
            "and not a\n  conflict.",
            DIM,
        )
    )


def scenario_conflict(workdir: Path) -> None:
    banner("2. Two panels disagree about the MRP")

    engine = StubOcrEngine()
    front = make_artifact(workdir, "c_front.jpg", "IMG-1", 1, SurfaceView.FRONT)
    back = make_artifact(workdir, "c_back.jpg", "IMG-2", 2, SurfaceView.BACK)

    engine.script(str(front.local_path), [line("MRP Rs. 120", 300)])
    # A sticker over the original price, or a misread. Extraction cannot tell
    # which, and it is not extraction's job to decide.
    engine.script(str(back.local_path), [line("MRP Rs. 180", 300)])

    result = run_extraction(
        make_request([front, back]), PipelineConfig(engine=engine)
    )

    mrp = next(
        f for f in result.snapshot.to_json()["facts"]
        if f["field"] == "package.mrp"
    )
    print(f"\n  package.mrp status:     {colour(mrp['status'], RED)}")
    print(f"  package.mrp projection: {result.snapshot.project()['package']['mrp']}")
    print(f"  candidates retained:    {len(mrp['conflicts'])}")
    show_facts(result)

    print()
    print(
        colour(
            "  Note: neither reading was silently chosen. The projection is "
            "null and\n  both candidates stay on the fact, so a reviewer sees "
            "the disagreement\n  rather than a confident wrong answer.",
            DIM,
        )
    )


def scenario_unreadable(workdir: Path) -> None:
    banner("3. A package nothing could be read from")

    engine = StubOcrEngine()
    blurred = make_artifact(workdir, "blur.jpg", "IMG-1", 1, SurfaceView.FRONT)
    engine.script(str(blurred.local_path), [])  # OCR found nothing

    result = run_extraction(
        make_request([blurred]), PipelineConfig(engine=engine)
    )

    statuses = {
        f["status"] for f in result.snapshot.to_json()["facts"]
    }
    print(f"\n  Statuses present: {sorted(statuses)}")
    print(
        f"  DECLARED_ABSENCE anywhere: "
        f"{colour('no', GREEN) if 'DECLARED_ABSENCE' not in statuses else colour('YES — BUG', RED)}"
    )

    mrp = next(
        f for f in result.snapshot.to_json()["facts"]
        if f["field"] == "package.mrp"
    )
    print(f"\n  package.mrp → {mrp['status']}")
    print(colour(f"    {mrp['note']}", DIM))

    print()
    print(
        colour(
            "  Note: this is the rule with the most downstream consequence.\n"
            "  'OCR found no MRP' is an extraction uncertainty. 'This package "
            "carries\n  no price declaration' is a finding. Only the second "
            "would be\n  DECLARED_ABSENCE, and it requires stated evidence "
            "that the pipeline\n  does not have here.",
            DIM,
        )
    )


def scenario_derived(workdir: Path) -> None:
    banner("4. Unit sale price: derived, and never overwriting what is printed")

    engine = StubOcrEngine()
    art = make_artifact(workdir, "d_front.jpg", "IMG-1", 1, SurfaceView.FRONT)
    engine.script(
        str(art.local_path),
        [line("Net Quantity: 500 g", 200), line("MRP Rs. 120", 300)],
    )
    derived_result = run_extraction(
        make_request([art]), PipelineConfig(engine=engine)
    )
    derived = next(
        f for f in derived_result.snapshot.to_json()["facts"]
        if f["field"] == "package.unit_sale_price"
    )
    print(f"\n  Nothing printed  → {colour(derived['status'], YELLOW)}"
          f"  {json.dumps(derived['value'])}")
    print(colour(f"    rule: {derived['derivation']['rule_id']}", DIM))
    print(colour(f"    {derived['derivation']['note']}", DIM))

    # Now the same package, but it prints a unit price that disagrees with
    # what a calculation would give.
    engine2 = StubOcrEngine()
    art2 = make_artifact(workdir, "d2_front.jpg", "IMG-1", 1, SurfaceView.FRONT)
    engine2.script(
        str(art2.local_path),
        [
            line("Net Quantity: 500 g", 200),
            line("MRP Rs. 120", 300),
            line("Unit Sale Price: Rs 0.30 per g", 400),
        ],
    )
    printed_result = run_extraction(
        make_request([art2]), PipelineConfig(engine=engine2)
    )
    printed = next(
        f for f in printed_result.snapshot.to_json()["facts"]
        if f["field"] == "package.unit_sale_price"
    )
    print(f"  Printed 0.30/g   → {colour(printed['status'], GREEN)}"
          f"  {json.dumps(printed['value'])}")

    print()
    print(
        colour(
            "  Note: the calculation would have given 0.24/g. The package says "
            "0.30/g,\n  so 0.30 is what the fact carries. A computed value "
            "never replaces an\n  observed declaration.",
            DIM,
        )
    )


def main() -> int:
    # The runner logs an uncalibrated-policy warning per run. That is the right
    # behaviour in a service and pure noise across a four-scenario demo, so it
    # is silenced here and stated once, below, instead.
    logging.getLogger("app.pipeline.runner").setLevel(logging.ERROR)

    print()
    print(colour("  Legal Metrology extraction pipeline — demo run", BOLD))
    print(
        colour(
            "  Quality thresholds are UNCALIBRATED; RETAKE verdicts are "
            "advisory only.",
            DIM,
        )
    )
    print(
        colour(
            "  Stub recognizer; every stage after recognition is the real "
            "code path.",
            DIM,
        )
    )

    with tempfile.TemporaryDirectory(prefix="lm-demo-") as tmp:
        workdir = Path(tmp)
        scenario_normal(workdir)
        scenario_conflict(workdir)
        scenario_unreadable(workdir)
        scenario_derived(workdir)

    banner("Done")
    print()
    print("  Every snapshot above passed contract validation: the typed")
    print("  projection was regenerated from facts[] and checked against it.")
    print("  A divergence would have quarantined the run instead.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
