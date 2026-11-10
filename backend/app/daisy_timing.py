"""Timing and synchronisation of a built DAISY 3 package, checked against its real audio.

The package is read back from disk, independently of the code that wrote it,
and every SMIL ``audio`` reference is checked against the decoded audio file it
points at (frames counted, not estimated):

* **Clips**: the audio file and the text element exist; ``clipBegin`` and
  ``clipEnd`` are SMIL clock values (Z39.86-2005 §7.7); ``clipEnd`` is after
  ``clipBegin`` and never after the end of the file (§7.3).
* **Contiguity**: the clips of a file tile it, in reading order, with no gap and
  no overlap: the first starts at 0, each starts where the one before ended,
  and the last ends with the file. A stretch no clip covers is never played by
  a SMIL player, and it is what made the SMIL times disagree with the audio.
* **Times**: each SMIL file's ``dtb:totalElapsedTime`` is the playing time of
  the files before it, its ``seq dur`` its own playing time, and the OPF's
  ``dtb:totalTime`` the playing time of them all (§3.2.3, §7.5), all summed
  from the clips themselves.
* **Audio format**: MP3 at one constant bitrate and one sample rate a DAISY
  player must support (§5.1). Variable bitrate without a length header has no
  duration a player or validator can rely on: estimated from its first frame,
  the delivered chapters of *Cuentos de la Selva* were up to 28 % short, so
  their clips appeared to end after the audio.
* **Edges**: every audio file starts and ends with the silence ONCE asks for,
  measured on the waveform (silence detection and RMS of the edge windows).
* **Book audio and transitions**: the complete audiobook is as long as its
  parts, and the navigation reaches every SMIL file in spine order.

Each check is ``passed``, ``failed`` or ``warning`` with its evidence, in the
form the completeness chain and the release gate already read.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app import audio_master
from app.audio_integrity import inspect_stream

SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"

# One millisecond: the resolution every time in the package is written at.
MS = 0.0015
# How far the last clip may end before the end of its file: one MP3 frame
# (26 ms at 44.1 kHz) plus the millisecond the end is rounded down by.
END_SLACK = 0.03
# The silence at an edge may fall short of the target by this much.
EDGE_SLACK = 0.03

_FULL = re.compile(r"^(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)$")
_PARTIAL = re.compile(r"^([0-5]\d):([0-5]\d(?:\.\d+)?)$")
_TIMECOUNT = re.compile(r"^(\d+(?:\.\d+)?)(h|min|s|ms)?$")


def parse_clock(value: str | None) -> float | None:
    """Seconds in a SMIL clock value (full, partial or timecount), or None if invalid."""
    if value is None:
        return None
    text = value.strip()
    if text.startswith("npt="):
        text = text[4:]
    match = _FULL.match(text)
    if match:
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    match = _PARTIAL.match(text)
    if match:
        return int(match.group(1)) * 60 + float(match.group(2))
    match = _TIMECOUNT.match(text)
    if match:
        scale = {"h": 3600, "min": 60, "s": 1, "ms": 0.001, None: 1}[match.group(2)]
        return float(match.group(1)) * scale
    return None


def _check(check_id: str, label: str, status: str, detail: str, **evidence) -> dict:
    return {"id": check_id, "label": label, "status": status, "detail": detail, **evidence}


@dataclass
class _Audio:
    path: Path
    duration: float
    stream: dict
    data: bytes


@dataclass
class _Clip:
    smil: str
    par: str
    src: str
    begin: float | None
    end: float | None
    raw: tuple[str | None, str | None]


class _Package:
    def __init__(self, package: Path) -> None:
        self.root = package
        self.opf_path = next(package.glob("*.opf"))
        self.opf = ET.parse(self.opf_path).getroot()
        self.items = {item.get("id"): item.get("href", "") for item in self.opf.iter(f"{OPF}item")}
        self.spine = [self.items.get(ref.get("idref"), "") for ref in self.opf.iter(f"{OPF}itemref")]
        self.smil: dict[str, ET.Element] = {}
        self.audio: dict[Path, _Audio] = {}
        self.xml_errors: list[dict] = []
        for href in self.spine:
            path = package / href
            try:
                self.smil[href] = ET.parse(path).getroot()
            except (ET.ParseError, OSError) as exc:
                self.xml_errors.append({"file": href, "error": str(exc)[:200]})
        self._dtbook_ids: dict[Path, set[str]] = {}

    def audio_file(self, path: Path) -> _Audio | None:
        path = path.resolve()
        if path not in self.audio:
            if not path.is_file():
                return None
            data = path.read_bytes()
            stream = inspect_stream(data)
            self.audio[path] = _Audio(path, stream["duration_seconds"], stream, data)
        return self.audio[path]

    def text_ids(self, path: Path) -> set[str] | None:
        path = path.resolve()
        if path not in self._dtbook_ids:
            if not path.is_file():
                return None
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError:
                return None
            self._dtbook_ids[path] = {element.get("id") for element in root.iter() if element.get("id")}
        return self._dtbook_ids[path]


def _xml_files(built: _Package) -> dict:
    problems = list(built.xml_errors)
    checked = 0
    for path in sorted(built.root.rglob("*")):
        if path.suffix.lower() not in {".xml", ".opf", ".ncx", ".res", ".smil"}:
            continue
        checked += 1
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            problems.append({"file": path.relative_to(built.root).as_posix(), "error": str(exc)[:200]})
    unique = {item["file"]: item for item in problems}
    return _check("timing_xml", "XML bien formado", "failed" if unique else "passed",
                  f"{checked} documentos XML" + (f"; {len(unique)} no se pueden leer" if unique
                                                 else ", todos bien formados"),
                  problems=list(unique.values())[:20])


def _clips(built: _Package) -> tuple[dict, dict[str, list[_Clip]]]:
    """Check every audio reference; return the clips of each SMIL file in order."""
    problems: list[dict] = []
    by_smil: dict[str, list[_Clip]] = {}
    count = 0
    for href in built.spine:
        root = built.smil.get(href)
        if root is None:
            problems.append({"smil": href, "problem": "falta el archivo SMIL o no se puede leer"})
            continue
        smil_path = built.root / href
        clips: list[_Clip] = []
        for par in root.iter(f"{SMIL}par"):
            audio = par.find(f"{SMIL}audio")
            text = par.find(f"{SMIL}text")
            if text is not None:
                target, _, fragment = (text.get("src") or "").partition("#")
                ids = built.text_ids(smil_path.parent / target) if target else None
                if ids is None:
                    problems.append({"smil": href, "par": par.get("id"), "problem": f"falta el texto {target}"})
                elif fragment and fragment not in ids:
                    problems.append({"smil": href, "par": par.get("id"),
                                     "problem": f"el texto no tiene el elemento #{fragment}"})
            if audio is None:
                continue
            count += 1
            src = audio.get("src") or ""
            raw = (audio.get("clipBegin"), audio.get("clipEnd"))
            begin, end = parse_clock(raw[0]), parse_clock(raw[1])
            clip = _Clip(href, par.get("id") or "", src, begin, end, raw)
            clips.append(clip)
            info = built.audio_file(smil_path.parent / src) if src else None
            if info is None:
                problems.append({"smil": href, "par": clip.par, "problem": f"falta el audio {src}"})
                continue
            if begin is None or end is None:
                problems.append({"smil": href, "par": clip.par,
                                 "problem": f"clipBegin/clipEnd no son valores de reloj SMIL: {raw[0]} – {raw[1]}"})
                continue
            if end <= begin:
                problems.append({"smil": href, "par": clip.par,
                                 "problem": f"clipEnd ({raw[1]}) no es posterior a clipBegin ({raw[0]})"})
            if end > info.duration + 0.0005:
                problems.append({"smil": href, "par": clip.par,
                                 "problem": f"clipEnd {end:.3f} s pasa del final del audio ({info.duration:.3f} s)"})
        by_smil[href] = clips
    return _check("timing_clips", "Fragmentos de audio SMIL", "failed" if problems else "passed",
                  f"{count} fragmentos en {len(built.spine)} archivos SMIL"
                  + (f"; {len(problems)} problemas" if problems
                     else "; cada uno con su audio y su texto, y dentro de su audio"),
                  problems=problems[:30], clips=count), by_smil


def _contiguity(built: _Package, by_smil: dict[str, list[_Clip]]) -> dict:
    problems: list[dict] = []
    files = 0
    for href, clips in by_smil.items():
        groups: dict[str, list[_Clip]] = {}
        for clip in clips:
            if clip.begin is not None and clip.end is not None:
                groups.setdefault(clip.src, []).append(clip)
        for src, group in groups.items():
            files += 1
            info = built.audio_file((built.root / href).parent / src)
            if group[0].begin > MS:
                problems.append({"smil": href, "audio": src, "problem":
                                 f"el primer fragmento empieza en {group[0].begin:.3f} s: lo anterior no se reproduce"})
            for before, after in zip(group, group[1:]):
                difference = after.begin - before.end
                if difference > MS:
                    problems.append({"smil": href, "audio": src, "par": after.par,
                                     "problem": f"hueco de {difference:.3f} s antes de este fragmento"})
                elif difference < -MS:
                    problems.append({"smil": href, "audio": src, "par": after.par,
                                     "problem": f"se solapa {-difference:.3f} s con el anterior"})
            if info is not None and info.duration - group[-1].end > END_SLACK:
                problems.append({"smil": href, "audio": src, "problem":
                                 f"el último fragmento acaba en {group[-1].end:.3f} s y el audio dura "
                                 f"{info.duration:.3f} s: el final no se reproduce"})
    return _check("timing_contiguity", "Fragmentos contiguos", "failed" if problems else "passed",
                  f"{files} archivos de audio"
                  + (f"; {len(problems)} huecos, solapes o tramos sin cubrir" if problems
                     else ": los fragmentos cubren cada archivo de principio a fin, sin huecos ni solapes"),
                  problems=problems[:30])


def _times(built: _Package, by_smil: dict[str, list[_Clip]]) -> dict:
    problems: list[dict] = []
    elapsed = 0.0
    files = []
    for href in built.spine:
        root = built.smil.get(href)
        if root is None:
            continue
        own = sum(clip.end - clip.begin for clip in by_smil.get(href, [])
                  if clip.begin is not None and clip.end is not None)
        declared = next((meta.get("content") for meta in root.iter(f"{SMIL}meta")
                         if meta.get("name") == "dtb:totalElapsedTime"), None)
        value = parse_clock(declared)
        if value is None:
            problems.append({"smil": href, "problem": f"dtb:totalElapsedTime ausente o no válido: {declared}"})
        elif abs(value - elapsed) > MS:
            problems.append({"smil": href, "problem": f"dtb:totalElapsedTime {value:.3f} s; lo anterior suma "
                                                      f"{elapsed:.3f} s"})
        seq = root.find(f"{SMIL}body/{SMIL}seq")
        dur = seq.get("dur") if seq is not None else None
        if dur is not None:
            seconds = parse_clock(dur)
            if seconds is None or abs(seconds - own) > MS:
                problems.append({"smil": href, "problem": f"dur del seq {dur}; sus fragmentos suman {own:.3f} s"})
        files.append({"smil": href, "elapsed": round(elapsed, 3), "own": round(own, 3)})
        elapsed += own
    declared_total = next((meta.get("content") for meta in built.opf.iter(f"{OPF}meta")
                           if meta.get("name") == "dtb:totalTime"), None)
    total = parse_clock(declared_total)
    if total is None:
        problems.append({"problem": f"dtb:totalTime ausente o no válido: {declared_total}"})
    elif abs(total - elapsed) > MS:
        problems.append({"problem": f"dtb:totalTime {total:.3f} s; los archivos SMIL suman {elapsed:.3f} s"})
    return _check("timing_totals", "Tiempos del libro (totalElapsedTime, dur, totalTime)",
                  "failed" if problems else "passed",
                  f"{len(files)} archivos SMIL, {elapsed:.3f} s en total"
                  + (f"; {len(problems)} tiempos no cuadran" if problems
                     else "; cada tiempo declarado coincide con la suma de sus fragmentos"),
                  problems=problems[:30], total_seconds=round(elapsed, 3), declared_total=declared_total)


def _first_frame_estimate(info: _Audio) -> float | None:
    """What a tool that trusts the first frame estimates: size over its bitrate."""
    data = info.data
    offset = info.stream.get("id3_bytes") or 0
    from app.daisy_export import _mp3_frame

    while offset < len(data) - 4 and not _mp3_frame(data, offset):
        offset += 1
    frame = _mp3_frame(data, offset)
    if not frame:
        return None
    length, samples, rate = frame
    return (len(data) - offset) * 8 / (length * 8 * rate / samples)


def _book_audio(built: _Package) -> list[Path]:
    full = built.items.get("full-audiobook")
    return [built.root / full] if full and (built.root / full).is_file() else []


def _audio_format(built: _Package, bitrate_kbps: int | None) -> dict:
    problems: list[dict] = []
    formats: set[tuple] = set()
    files = list(built.audio.values()) + [info for info in (built.audio_file(path) for path in _book_audio(built))
                                          if info is not None and info.path not in built.audio]
    seen: set[Path] = set()
    rows = []
    for info in files:
        if info.path in seen:
            continue
        seen.add(info.path)
        stream = info.stream
        name = info.path.relative_to(built.root.resolve()).as_posix() if built.root.resolve() in info.path.parents \
            else info.path.name
        rows.append({"file": name, "bitrates_kbps": stream["bitrates_kbps"], "sample_rates": stream["sample_rates"],
                     "channels": stream["channels"], "duration": stream["duration_seconds"]})
        if stream["errors"]:
            problems.append({"file": name, "problem": "flujo MP3 dañado: " + ", ".join(stream["errors"])})
        rates = stream["sample_rates"]
        if len(rates) != 1 or rates[0] not in audio_master.DAISY_SAMPLE_RATES:
            problems.append({"file": name, "problem": f"frecuencia de muestreo {rates}: un reproductor DAISY solo "
                                                      "tiene que admitir 44,1, 22,05 y 11,025 kHz"})
        if len(stream["bitrates_kbps"]) > 1:
            estimate = _first_frame_estimate(info)
            gap = f"; estimada por su primera trama dura {estimate:.1f} s y en realidad {info.duration:.1f} s" \
                if estimate is not None else ""
            problems.append({"file": name, "problem": "velocidad variable (VBR) sin cabecera de duración" + gap})
        elif bitrate_kbps and stream["bitrates_kbps"] and stream["bitrates_kbps"] != [bitrate_kbps]:
            problems.append({"file": name, "problem": f"{stream['bitrates_kbps'][0]} kbit/s en lugar de "
                                                      f"{bitrate_kbps} kbit/s"})
        if stream["sample_rates"] and stream["channels"]:
            formats.add((tuple(stream["sample_rates"]), tuple(stream["channels"])))
    if len(formats) > 1:
        problems.append({"problem": "los archivos no comparten frecuencia de muestreo y canales",
                         "formats": [list(map(list, item)) for item in sorted(formats)]})
    return _check("timing_audio_format", "Formato del audio", "failed" if problems else "passed",
                  f"{len(rows)} archivos MP3"
                  + (f"; {len(problems)} problemas" if problems
                     else ", todos a velocidad constante y con la misma frecuencia y canales"),
                  problems=problems[:30], files=rows[:50])


def _edges(built: _Package, leading: float, trailing: float) -> dict:
    if not audio_master.available():
        return _check("timing_edges", "Silencio al principio y al final de cada audio", "warning",
                      "No medido: ffmpeg no está disponible.")
    problems: list[dict] = []
    rows = []
    paths = sorted(set(built.audio) | {path.resolve() for path in _book_audio(built)})
    for path in paths:
        info = built.audio_file(path)
        if info is None:
            continue
        name = path.relative_to(built.root.resolve()).as_posix() if built.root.resolve() in path.parents else path.name
        edges = audio_master.edge_silence(info.data)
        if edges is None:
            problems.append({"file": name, "problem": "no se pudo medir el silencio de los bordes"})
            continue
        start_db = audio_master.window_level_db(info.data, 0.0, max(0.05, leading - 0.05)) if leading else None
        end_db = (audio_master.window_level_db(info.data, max(0.0, info.duration - trailing + 0.05),
                                               max(0.05, trailing - 0.05)) if trailing else None)
        rows.append({"file": name, **edges.as_dict(), "start_rms_db": start_db, "end_rms_db": end_db})
        if edges.leading < leading - EDGE_SLACK:
            problems.append({"file": name, "problem": f"empieza con {edges.leading:.3f} s de silencio; "
                                                      f"se piden {leading:.2f} s"})
        if edges.trailing < trailing - EDGE_SLACK:
            problems.append({"file": name, "problem": f"acaba con {edges.trailing:.3f} s de silencio; "
                                                      f"se piden {trailing:.2f} s"})
    return _check("timing_edges", "Silencio al principio y al final de cada audio",
                  "failed" if problems else "passed",
                  f"{len(rows)} archivos medidos sobre la forma de onda"
                  + (f"; {len(problems)} sin el silencio pedido" if problems
                     else f": todos empiezan con al menos {leading:.2f} s y acaban con al menos {trailing:.2f} s"),
                  problems=problems[:30], files=rows[:50], leading_target=leading, trailing_target=trailing)


def _transitions(built: _Package, gap_extra: float | None) -> dict:
    problems: list[dict] = []
    # Every SMIL file is reached from the navigation, in spine order.
    ncx_href = built.items.get("ncx", "")
    position = {href: index for index, href in enumerate(built.spine)}
    try:
        ncx = ET.parse(built.root / ncx_href).getroot() if ncx_href else None
    except (ET.ParseError, OSError):
        ncx = None
    nav_map = ncx.find(f"{NCX}navMap") if ncx is not None else None
    if nav_map is None:
        problems.append({"problem": "falta el NCX o su navMap"})
    else:
        reached = []
        for point in nav_map.iter(f"{NCX}navPoint"):
            content = point.find(f"{NCX}content")
            href = PurePosixPath((content.get("src") or "").partition("#")[0]).as_posix() if content is not None else ""
            if href not in position:
                problems.append({"problem": f"la entrada «{point.findtext(f'{NCX}navLabel/{NCX}text')}» apunta a "
                                            f"{href or 'nada'}"})
                continue
            reached.append(position[href])
        if reached != sorted(reached):
            problems.append({"problem": "las entradas de navegación no siguen el orden del spine"})
        missing = [href for href in built.spine if position[href] not in set(reached)]
        if missing:
            problems.append({"problem": f"{len(missing)} archivos SMIL sin entrada de navegación",
                             "smil": missing[:10]})
    # The complete audiobook is its parts, in order, and nothing else.
    for path in _book_audio(built):
        book = built.audio_file(path)
        parts = [info for info in built.audio.values() if info.path != path.resolve()]
        if book is None or not parts:
            continue
        total = sum(info.duration for info in parts)
        extra = (gap_extra or 0.0) * (len(parts) - 1)
        allowance = 0.06 * max(1, len(parts) - 1) if gap_extra else MS
        if book.duration < total - 0.05:
            problems.append({"problem": f"el audiolibro completo dura {book.duration:.3f} s y sus partes "
                                        f"{total:.3f} s"})
        elif gap_extra is not None and book.duration > total + extra + allowance:
            problems.append({"problem": f"el audiolibro completo dura {book.duration:.3f} s, más que sus partes "
                                        f"({total:.3f} s) y las pausas entre ellas"})
    return _check("timing_transitions", "Transiciones entre capítulos y audiolibro completo",
                  "failed" if problems else "passed",
                  "; ".join(item["problem"] for item in problems[:3])
                  or "cada archivo SMIL tiene su entrada, en orden; el audiolibro completo dura lo que sus partes",
                  problems=problems[:20])


def verify(package: Path, *, leading: float | None = None, trailing: float | None = None,
           bitrate_kbps: int | None = None, chapter_gap: float | None = None) -> list[dict]:
    """Every timing check on a built package.

    ``leading`` and ``trailing`` are the silence wanted at the edges of each
    audio file (not checked when None), ``bitrate_kbps`` the constant bitrate
    expected, and ``chapter_gap`` the pause wanted between chapters of the
    complete audiobook, from which the silence added between them follows.
    """
    try:
        built = _Package(package)
    except (StopIteration, ET.ParseError, OSError) as exc:
        return [_check("timing_package", "Paquete legible", "failed", f"No se pudo leer el paquete: {exc}")]
    checks = [_xml_files(built)]
    clips_check, by_smil = _clips(built)
    checks += [clips_check, _contiguity(built, by_smil), _times(built, by_smil), _audio_format(built, bitrate_kbps)]
    if leading is not None or trailing is not None:
        checks.append(_edges(built, leading or 0.0, trailing or 0.0))
    extra = None
    if chapter_gap is not None and leading is not None and trailing is not None:
        extra = max(0.0, chapter_gap - leading - trailing)
    checks.append(_transitions(built, extra))
    return checks


def summary(checks: list[dict]) -> dict:
    failed = [check["id"] for check in checks if check["status"] == "failed"]
    return {"status": "failed" if failed else "passed", "failed": failed,
            "checks": {check["id"]: check["status"] for check in checks}}
