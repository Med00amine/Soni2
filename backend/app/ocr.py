"""Optical character recognition for PDF pages that carry no text layer.

Tesseract is called as a command-line program with TSV output, because the TSV
gives a confidence for every word: that is what lets the pipeline point a person
at the words it is unsure of instead of narrating them as if they were certain.
No Python binding is required. Without Tesseract the caller is told, and the
pages that needed OCR are reported as missing content rather than skipped.

The Docker image installs ``tesseract-ocr`` with the Spanish model
(``tesseract-ocr-spa``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Below this Tesseract confidence (0-100) a word is listed for review.
LOW_CONFIDENCE = 60
DEFAULT_DPI = 300


@dataclass
class OcrPage:
    page: int
    paragraphs: list[str] = field(default_factory=list)
    mean_confidence: float = 0.0
    words: int = 0
    low_confidence_words: list[dict] = field(default_factory=list)
    engine: str = "tesseract"
    error: str = ""

    def as_dict(self) -> dict:
        return {"page": self.page, "paragraphs": len(self.paragraphs), "words": self.words,
                "mean_confidence": round(self.mean_confidence, 1), "engine": self.engine,
                "low_confidence_words": self.low_confidence_words[:40], "error": self.error}


def tesseract_path() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("tesseract")


def languages() -> set[str]:
    executable = tesseract_path()
    if not executable:
        return set()
    try:
        result = subprocess.run([executable, "--list-langs"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    return {line.strip() for line in lines[1:] if line.strip()}


def available(language: str = "spa") -> bool:
    return language in languages()


def parse_tsv(tsv: str, page: int) -> OcrPage:
    """Group Tesseract's words into paragraphs, keeping each word's confidence."""
    result = OcrPage(page)
    paragraphs: dict[tuple[int, int], list[list[str]]] = {}
    order: list[tuple[int, int]] = []
    confidences: list[float] = []
    rows = tsv.splitlines()
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) < 12 or cells[0] != "5":
            continue
        text = cells[11].strip()
        if not text:
            continue
        try:
            confidence = float(cells[10])
            key = (int(cells[2]), int(cells[3]))
            line = int(cells[4])
        except ValueError:
            continue
        if key not in paragraphs:
            paragraphs[key] = []
            order.append(key)
        lines = paragraphs[key]
        while len(lines) <= line:
            lines.append([])
        lines[line].append(text)
        if confidence >= 0:
            confidences.append(confidence)
            if confidence < LOW_CONFIDENCE:
                result.low_confidence_words.append({"word": text, "confidence": round(confidence, 1)})
    from app.book_structure import _join_lines

    for key in order:
        joined = _join_lines([" ".join(words) for words in paragraphs[key] if words])
        if joined:
            result.paragraphs.append(joined)
    result.words = len(confidences)
    result.mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return result


def ocr_png(png: bytes, page: int, *, language: str = "spa", timeout: float = 180) -> OcrPage:
    executable = tesseract_path()
    if not executable:
        return OcrPage(page, error="tesseract no está instalado")
    with tempfile.TemporaryDirectory(prefix="soni2-ocr-") as folder:
        image = Path(folder) / "page.png"
        image.write_bytes(png)
        try:
            # --psm 1: automatic page segmentation with orientation detection,
            # which keeps the reading order of ordinary book pages.
            completed = subprocess.run(
                [executable, str(image), "stdout", "-l", language, "--psm", "1", "tsv"],
                capture_output=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return OcrPage(page, error="el OCR tardó demasiado")
        except OSError as exc:
            return OcrPage(page, error=f"no se pudo ejecutar tesseract: {exc}")
    if completed.returncode != 0:
        return OcrPage(page, error=completed.stderr.decode("utf-8", "replace")[:300] or "tesseract falló")
    return parse_tsv(completed.stdout.decode("utf-8", "replace"), page)


def ocr_pdf_page(pdf_page, number: int, *, language: str = "spa", dpi: int = DEFAULT_DPI) -> OcrPage:
    pixmap = pdf_page.get_pixmap(dpi=dpi)
    return ocr_png(pixmap.tobytes("png"), number, language=language)
