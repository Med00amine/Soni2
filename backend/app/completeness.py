"""Prove that the finished package contains the whole selected book, once, in order.

Every link in the chain is checked independently of the code that built it:

1. **source → units**: the reviewed source (after the reader's section
   selection) is split again with the same rule, and compared position by
   position with the production units. A missing, extra, duplicated or
   reordered paragraph fails; a reviewer's logged correction is allowed.
2. **units → audio**: every unit is verified and has a readable audio file.
3. **units → DTBook**: the package text holds every unit, in the same order.
4. **DTBook → SMIL → audio**: every synchronized element has exactly one SMIL
   ``par`` whose clip lies inside its chapter audio, starts after the previous
   clip ends, and has a positive length; each chapter file is a valid stream
   and all share one format.
5. **navigation**: one NCX entry per chapter, in play order; the OPF total
   time matches the audio.

Each check returns ``passed``/``failed``/``warning`` with the evidence.
"""

from __future__ import annotations

import difflib
import xml.etree.ElementTree as ET
from pathlib import Path

from app import audio_integrity
from app.daisy_export import make_paragraphs, mp3_duration

DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"
CLIP_TOLERANCE = 0.05


def expected_units(chapters: list[dict]) -> list[tuple[int, str, bool]]:
    """``(chapter index, text, is heading)`` in narration order, from the reviewed source."""
    units: list[tuple[int, str, bool]] = []
    for index, chapter in enumerate(chapters):
        title = (chapter.get("title") or "").strip()
        body = [part for paragraph in chapter.get("paragraphs", []) for part in make_paragraphs(paragraph)]
        if not body:
            continue
        if title and not chapter.get("title_generated"):
            units.append((index, title, True))
        units.extend((index, part, False) for part in body)
    return units


def _check(check_id: str, label: str, status: str, detail: str, **evidence) -> dict:
    return {"id": check_id, "label": label, "status": status, "detail": detail, "evidence": evidence}


def source_to_units(chapters: list[dict], units: list[dict], corrected_ids: set[str]) -> dict:
    expected = [text for _chapter, text, _heading in expected_units(chapters)]
    produced = [unit["text"] for unit in units]
    if len(expected) == len(produced):
        # A reviewer's logged correction stands in for the text it replaced.
        produced = [expected[index] if unit["sentence_id"] in corrected_ids else unit["text"]
                    for index, unit in enumerate(units)]
    problems: list[dict] = []
    matcher = difflib.SequenceMatcher(a=expected, b=produced, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        corrected = [units[index]["sentence_id"] for index in range(b0, b1)
                     if units[index]["sentence_id"] in corrected_ids]
        if tag == "replace" and len(corrected) == b1 - b0 and a1 - a0 == b1 - b0:
            continue  # a reviewer corrected these units' text, and it is logged
        problems.append({"kind": {"delete": "missing", "insert": "extra", "replace": "changed"}[tag],
                         "expected": expected[a0:a1][:3], "produced": produced[b0:b1][:3],
                         "position": a0})
    duplicates = [text for text in set(produced) if produced.count(text) > expected.count(text)]
    if duplicates:
        problems.append({"kind": "duplicated", "produced": duplicates[:3]})
    status = "failed" if problems else "passed"
    return _check("source_to_units", "Texto de origen → unidades narradas", status,
                  f"{len(expected)} unidades esperadas, {len(produced)} producidas"
                  + (f", {len(problems)} discrepancias" if problems else ", mismo orden y contenido"),
                  expected=len(expected), produced=len(produced), problems=problems[:20])


def units_to_audio(units: list[dict], job_dir: Path) -> dict:
    problems = []
    formats: set[tuple] = set()
    for unit in units:
        path = job_dir / unit["audio_path"] if unit.get("audio_path") else None
        if unit.get("state") != "verified":
            problems.append({"sentence_id": unit["sentence_id"], "problem": f"estado {unit.get('state')}"})
            continue
        if path is None or not path.is_file() or path.stat().st_size == 0:
            problems.append({"sentence_id": unit["sentence_id"], "problem": "sin audio"})
            continue
        stream = audio_integrity.inspect_stream(path.read_bytes())
        if stream["errors"] and stream["errors"] != ["truncated_final_frame"]:
            problems.append({"sentence_id": unit["sentence_id"], "problem": ", ".join(stream["errors"])})
        formats.add((tuple(stream["sample_rates"]), tuple(stream["channels"])))
    status = "failed" if problems else "passed"
    detail = (f"{len(units) - len(problems)} de {len(units)} unidades verificadas con audio válido")
    if len(formats) > 1:
        detail += "; formatos de audio distintos entre unidades (se recodifican al unir)"
    return _check("units_to_audio", "Unidades → audio", status, detail, problems=problems[:20],
                  formats=[list(map(list, item)) for item in formats])


def _package_files(package: Path) -> tuple[Path, ET.Element, Path, ET.Element]:
    opf_path = next(package.glob("*.opf"))
    opf = ET.parse(opf_path).getroot()
    by_id = {item.get("id"): item.get("href") for item in opf.iter(f"{OPF}item")}
    return opf_path, opf, package / by_id["dtbook"], ET.parse(package / by_id["dtbook"]).getroot()


def units_to_package(units: list[dict], package: Path) -> list[dict]:
    """Checks 3-5 on a built package directory."""
    checks: list[dict] = []
    _opf_path, opf, dtbook_path, dtbook = _package_files(package)
    # A page number is a silent marker, not something narrated: it is checked
    # with the page list (once_validation), not as a unit.
    synchronized = [element for element in dtbook.iter() if element.get("smilref")
                    and not (element.get("id") or "").startswith("frontmatter-")
                    and element.tag != f"{DTBOOK}pagenum"]
    texts = ["".join(element.itertext()).strip() for element in synchronized]
    expected = [unit["text"].strip() for unit in units]
    mismatch = [{"position": index, "expected": expected[index] if index < len(expected) else None,
                 "package": texts[index] if index < len(texts) else None}
                for index in range(max(len(texts), len(expected)))
                if (texts[index] if index < len(texts) else None) != (expected[index] if index < len(expected) else None)]
    checks.append(_check("units_to_dtbook", "Unidades → texto DTBook",
                         "failed" if mismatch else "passed",
                         f"{len(texts)} elementos sincronizados para {len(expected)} unidades"
                         + (f"; {len(mismatch)} no coinciden" if mismatch else "; mismo texto y orden"),
                         problems=mismatch[:10]))

    smil_cache: dict[Path, ET.Element] = {}
    durations: dict[Path, float] = {}
    sync_problems: list[dict] = []
    previous_end: dict[Path, float] = {}
    clips = 0
    for element in synchronized:
        reference = element.get("smilref", "")
        smil_name, _, par_id = reference.partition("#")
        smil_path = (dtbook_path.parent / smil_name).resolve()
        if smil_path not in smil_cache:
            if not smil_path.is_file():
                sync_problems.append({"element": element.get("id"), "problem": f"falta {smil_name}"})
                continue
            smil_cache[smil_path] = ET.parse(smil_path).getroot()
        par = next((node for node in smil_cache[smil_path].iter(f"{SMIL}par") if node.get("id") == par_id), None)
        if par is None:
            sync_problems.append({"element": element.get("id"), "problem": f"no existe {reference}"})
            continue
        text = par.find(f"{SMIL}text")
        if text is None or text.get("src", "").partition("#")[2] != element.get("id"):
            sync_problems.append({"element": element.get("id"), "problem": "el par SMIL apunta a otro texto"})
        audio = par.find(f"{SMIL}audio")
        if audio is None:
            sync_problems.append({"element": element.get("id"), "problem": "par sin audio"})
            continue
        audio_path = (smil_path.parent / audio.get("src", "")).resolve()
        if audio_path not in durations:
            if not audio_path.is_file():
                sync_problems.append({"element": element.get("id"), "problem": f"falta {audio.get('src')}"})
                continue
            durations[audio_path] = mp3_duration(audio_path.read_bytes())
        begin, end = _seconds(audio.get("clipBegin", "")), _seconds(audio.get("clipEnd", ""))
        clips += 1
        if begin is None or end is None or end <= begin:
            sync_problems.append({"element": element.get("id"), "problem": f"fragmento inválido {begin}-{end}"})
            continue
        if end > durations[audio_path] + CLIP_TOLERANCE:
            sync_problems.append({"element": element.get("id"),
                                  "problem": f"termina en {end:.3f}s, el audio dura {durations[audio_path]:.3f}s"})
        if begin < previous_end.get(audio_path, 0.0) - CLIP_TOLERANCE:
            sync_problems.append({"element": element.get("id"),
                                  "problem": f"empieza en {begin:.3f}s, antes del final del anterior"})
        previous_end[audio_path] = end
    checks.append(_check("smil_sync", "Texto ↔ audio (SMIL)", "failed" if sync_problems else "passed",
                         f"{clips} fragmentos comprobados en {len(durations)} archivos de audio"
                         + (f"; {len(sync_problems)} problemas" if sync_problems else "; todos dentro de su audio y en orden"),
                         problems=sync_problems[:20]))

    format_problems = []
    formats: set[tuple] = set()
    for path in sorted(durations):
        stream = audio_integrity.inspect_stream(path.read_bytes())
        if stream["errors"]:
            format_problems.append({"file": path.name, "errors": stream["errors"]})
        formats.add((tuple(stream["sample_rates"]), tuple(stream["channels"])))
    if len(formats) > 1:
        format_problems.append({"problem": "los capítulos no comparten frecuencia de muestreo y canales",
                                "formats": [list(map(list, item)) for item in formats]})
    explained = {"corrupt_frames": "datos que no son audio entre las tramas MP3 (por ejemplo cabeceras de cada "
                                   "fragmento unidas sin ffmpeg)",
                 "mixed_sample_rates": "frecuencias de muestreo distintas dentro del archivo",
                 "mixed_channel_modes": "mono y estéreo mezclados", "no_audio_frames": "sin audio",
                 "truncated_final_frame": "la última trama está cortada", "empty": "archivo vacío"}
    reasons = sorted({explained.get(error, error) for problem in format_problems for error in problem.get("errors", [])}
                     | {problem["problem"] for problem in format_problems if "problem" in problem})
    checks.append(_check("audio_format", "Audio de los capítulos", "failed" if format_problems else "passed",
                         f"{len(durations)} archivos de capítulo"
                         + (f"; {len(format_problems)} con problemas: {'; '.join(reasons)}" if format_problems
                            else ", flujos MP3 válidos y homogéneos"),
                         problems=format_problems))

    ncx_href = next((item.get("href") for item in opf.iter(f"{OPF}item") if item.get("id") == "ncx"), "")
    nav_problems = []
    if ncx_href and (package / ncx_href).is_file():
        ncx = ET.parse(package / ncx_href).getroot()
        orders = [int(point.get("playOrder", "0")) for point in ncx.iter(f"{NCX}navPoint")]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            nav_problems.append("playOrder no es estrictamente creciente")
        # One entry per narrated section, nested or not. Front matter synthesized
        # outside the units (the legacy ONCE templates) has entries of its own.
        chapter_points = [point for point in ncx.iter(f"{NCX}navPoint") if point.get("class") != "frontmatter"]
        expected_chapters = len({unit["chapter_index"] for unit in units})
        if len(chapter_points) != expected_chapters:
            nav_problems.append(f"{len(chapter_points)} entradas de capítulo para {expected_chapters} capítulos")
    else:
        nav_problems.append("falta el NCX")
    total_meta = next((meta.get("content") for meta in opf.iter(f"{OPF}meta")
                       if meta.get("name") == "dtb:totalTime"), None)
    full_href = next((item.get("href") for item in opf.iter(f"{OPF}item") if item.get("id") == "full-audiobook"), "")
    total = _seconds(total_meta or "")
    if full_href and (package / full_href).is_file() and total is not None:
        actual = mp3_duration((package / full_href).read_bytes())
        if abs(actual - total) > max(1.0, actual * 0.01):
            nav_problems.append(f"dtb:totalTime {total:.1f}s y el audio completo dura {actual:.1f}s")
        chapter_total = sum(durations.values())
        if actual + 1.0 < chapter_total * 0.98:
            nav_problems.append(f"el audio completo ({actual:.1f}s) es más corto que sus capítulos ({chapter_total:.1f}s)")
    checks.append(_check("navigation", "Navegación NCX y duración total", "failed" if nav_problems else "passed",
                         "; ".join(nav_problems) or "una entrada por capítulo, en orden; la duración total coincide",
                         problems=nav_problems))
    return checks


def _seconds(clock: str) -> float | None:
    try:
        hours, minutes, seconds = clock.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def verify(chapters: list[dict], units: list[dict], job_dir: Path, package: Path | None,
           corrected_ids: set[str], once_plan: dict | None = None) -> dict:
    """The whole chain; with ``once_plan`` (the resolved planilla) the ONCE structure too."""
    checks = [source_to_units(chapters, units, corrected_ids), units_to_audio(units, job_dir)]
    if package is not None:
        try:
            checks.extend(units_to_package(units, package))
        except (ET.ParseError, StopIteration, KeyError, OSError) as exc:
            checks.append(_check("package_readable", "Paquete legible", "failed", f"No se pudo leer el paquete: {exc}"))
        if once_plan is not None:
            from app import once_validation

            checks.extend(once_validation.verify(package, chapters, once_plan))
    failed = [check for check in checks if check["status"] == "failed"]
    return {"status": "failed" if failed else "passed", "checks": checks}
