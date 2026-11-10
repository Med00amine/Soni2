"""Audit any DAISY 3 package or delivery ZIP: structure, DTDs, timing against the real audio.

    cd backend
    python -m tools.audit_package "D:/Downloads/Cuentos de la Selva/DAISY 3" --out report.json
    python -m tools.audit_package ../e2e-results/almendra/delivery.zip

Nothing is written into the package. A ZIP is extracted to a temporary folder.
The silence at the edges is checked against the targets given (the delivery
defaults unless told otherwise); ``--no-edges`` skips that measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import daisy_dtd, daisy_timing  # noqa: E402
from app.audio_master import MasteringOptions  # noqa: E402
from app.daisy_validation import validate_daisy_package  # noqa: E402


def _package_root(folder: Path) -> Path:
    opf = sorted(folder.rglob("*.opf"))
    if len(opf) != 1:
        raise SystemExit(f"Expected one OPF file under {folder}, found {len(opf)}.")
    return opf[0].parent


def audit(package: Path, *, edges: bool = True, leading: float | None = None, trailing: float | None = None,
          bitrate_kbps: int | None = None, chapter_gap: float | None = None) -> dict:
    defaults = MasteringOptions()
    lead, tail = defaults.edges
    try:
        validate_daisy_package(package)
        structural = "passed"
    except ValueError as exc:
        structural = f"failed: {exc}"
    checks = daisy_timing.verify(
        package,
        leading=(lead if leading is None else leading) if edges else None,
        trailing=(tail if trailing is None else trailing) if edges else None,
        bitrate_kbps=bitrate_kbps if bitrate_kbps is not None else defaults.bitrate_kbps,
        chapter_gap=chapter_gap if chapter_gap is not None else defaults.chapter_gap_ms / 1000,
    )
    return {"package": str(package), "structural": structural, "dtd": daisy_dtd.validate_package(package),
            "timing": daisy_timing.summary(checks), "checks": checks}


def alignment(package: Path, before: float = 0.2, after: float = 0.3) -> dict:
    """Whether every clip boundary falls in a pause and is followed by speech.

    Each clip but the first of a file starts where its passage's speech starts,
    after the pause that ended the previous passage. So the stretch just before
    a boundary should be silence and the stretch just after it speech. Measured
    on the waveform with no recognizer: evidence of alignment, free to run.
    """
    import xml.etree.ElementTree as ET

    from app import audio_master
    from app.daisy_timing import SMIL, parse_clock

    rows, misaligned = 0, []
    cache: dict[Path, bytes] = {}
    for smil in sorted(package.rglob("*.smil")):
        audio = [item for item in ET.parse(smil).getroot().iter(f"{SMIL}audio")]
        for previous, item in zip(audio, audio[1:]):
            if previous.get("src") != item.get("src"):
                continue
            path = (smil.parent / item.get("src")).resolve()
            data = cache.setdefault(path, path.read_bytes())
            begin = parse_clock(item.get("clipBegin")) or 0.0
            quiet = audio_master.window_level_db(data, max(0.0, begin - before - 0.02), before)
            speech = audio_master.window_level_db(data, begin + 0.02, after)
            rows += 1
            if quiet is None or speech is None or quiet > -45 or speech < -40:
                misaligned.append({"smil": smil.name, "clipBegin": item.get("clipBegin"),
                                   "before_db": quiet, "after_db": speech})
    return {"boundaries": rows, "in_a_pause_then_speech": rows - len(misaligned),
            "not_confirmed": misaligned[:30]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="a package folder (with the .opf) or a delivery ZIP")
    parser.add_argument("--out", default="", help="write the full report here as JSON")
    parser.add_argument("--no-edges", action="store_true", help="do not measure the silence at the edges")
    parser.add_argument("--alignment", action="store_true",
                        help="also measure that every clip boundary falls in a pause followed by speech")
    args = parser.parse_args()
    source = Path(args.source)
    with tempfile.TemporaryDirectory(prefix="soni2-audit-") as folder:
        if source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source) as archive:
                archive.extractall(folder)
            package = _package_root(Path(folder))
        else:
            package = _package_root(source)
        report = audit(package, edges=not args.no_edges)
        if args.alignment:
            report["alignment"] = alignment(package)
        report["package"] = str(source)
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"structural": report["structural"], "dtd": report["dtd"]["status"],
                      "timing": report["timing"],
                      **({"alignment": {key: value for key, value in report["alignment"].items()
                                        if key != "not_confirmed"}} if "alignment" in report else {})},
                     ensure_ascii=False, indent=2))
    for check in report["checks"]:
        print(f"{check['status']:>8}  {check['id']:<22} {check['detail'][:150]}")
        for problem in check.get("problems", [])[:3]:
            print("          -", json.dumps(problem, ensure_ascii=False)[:180])


if __name__ == "__main__":
    main()
