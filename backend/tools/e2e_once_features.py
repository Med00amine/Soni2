"""End to end, at no cost: one book that has everything ONCE asked for, produced and audited.

    cd backend
    python -m tools.e2e_once_features --out ../e2e-results/once-features

The book is an EPUB built here with: a table of contents called "Sumario"
with its pages (roman for the prologue), page breaks, a note and its
reference, boxed and shaded text, a heading with a name the book marks as
English, a subchapter opening with a Roman numeral, a bibliography with
foreign and Spanish surnames, and a photo insert the book names "láminas".

It is produced twice through the application's own endpoints: as a reviewed
production job, and as a direct download in the flat package layout. The
narration is the offline mock provider's, made audible (a tone per paragraph,
its length following the text) so that the silence at the edges and the
alignment of the clips are measured on a real waveform. No paid service is
called. Each ZIP is then opened and audited as ONCE would receive it:
structure, official DTDs, timing against the decoded audio, alignment, and
the ONCE structure.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import audio_master, main, once_validation, providers  # noqa: E402
from app import jobs as job_store  # noqa: E402
from app.audio_cache import AudioCache  # noqa: E402
from app.narration_plan import build_narration_plan  # noqa: E402
from tools.audit_package import alignment, audit  # noqa: E402

PROSE = ("La barca dejó a Clara en el muelle de la isla cuando el sol ya rozaba el agua. Traía una maleta, un "
         "cuaderno de tapas azules y la carta del farero anterior.")
SHEET = {"file2_page": "2", "index_page": "3", "production_date": "noviembre de 2026"}


def _png(shade: int) -> bytes:
    import fitz

    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 300), 0)
    pixmap.clear_with(shade)
    return pixmap.tobytes("png")


def build_epub(target: Path) -> Path:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("soni2-once-features")
    book.set_title("El faro de los viajeros")
    book.set_language("es")
    book.add_author("Ana Pérez")
    page = '<span epub:type="pagebreak" title="{0}" id="p{0}"/>'
    docs = {
        # Printed like an ONCE book: the initial files take pages 1-3 (planilla),
        # the prologue is roman, chapter 1 starts on page 11.
        "sumario.xhtml": ("<h1>Sumario</h1><p>Prólogo ........ ix</p><p>Capítulo I. La llegada ........ 11</p>"
                          "<p>Capítulo II. William Shakespeare en la isla ........ 15</p>"
                          "<p>Bibliografía ........ 19</p>"),
        "prologo.xhtml": f"{page.format('ix')}<h1>Prólogo</h1><p>Este libro reúne historias del faro.</p>",
        "cap1.xhtml": (f"{page.format('11')}<h1>Capítulo I. La llegada</h1>"
                       f'<p>{PROSE} La luz parpadeó antes de apagarse<a epub:type="noteref" href="#n1">1</a>.</p>'
                       '<div class="recuadro"><p>Los faros de la costa se encendían a mano.</p>'
                       "<p>Funcionaban con aceite de ballena.</p></div>"
                       f"{page.format('12')}<p>Clara subió por el camino de piedras hasta la puerta.</p>"
                       '<div style="background-color: #dddddd"><p>Recuerda: la luz nunca se apaga.</p></div>'
                       '<aside epub:type="footnote" id="n1"><p>1. Véase Smith (2004), p. 12.</p></aside>'),
        "laminas.xhtml": ('<section class="encarte"><figure><img src="images/f1.png" alt="Un faro al atardecer"/>'
                          "<figcaption>El faro en 1910.</figcaption></figure>"
                          '<figure><img src="images/f2.png" alt=""/></figure></section>'),
        "cap2.xhtml": (f"{page.format('15')}<h1>Capítulo II. <span lang=\"en\">William Shakespeare</span> en la isla</h1>"
                       f"<p>{PROSE}</p><h2>III. El regreso</h2><p>Cuando llegó la primavera volvió al puerto.</p>"),
        "biblio.xhtml": (f"{page.format('19')}<h1>Bibliografía</h1>"
                         "<p>Smith, J. (2004). The Lighthouse. Londres, pp. 23-45.</p>"
                         "<p>García, P. (1999). Faros de España. Madrid: Espasa, 2.ª ed.</p>"),
    }
    items = []
    for name, body in docs.items():
        item = epub.EpubHtml(title=name, file_name=name, lang="es")
        item.content = f'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>{body}</body></html>'
        book.add_item(item)
        items.append(item)
    for index, shade in ((1, 90), (2, 170)):
        book.add_item(epub.EpubItem(uid=f"f{index}", file_name=f"images/f{index}.png", media_type="image/png",
                                    content=_png(shade)))
    book.toc = tuple(epub.Link(item.file_name, item.file_name, item.file_name) for item in items)
    book.spine = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(target), book)
    return target


def audible_mock(folder: Path):
    """The mock provider's clip, made audible: a tone as long as the text needs."""
    folder.mkdir(parents=True, exist_ok=True)

    def tone_audio(text: str) -> bytes:
        encoded = text.encode("utf-8")
        name = hashlib.sha256(encoded).hexdigest()[:16]
        target = folder / f"{name}.mp3"
        if not target.is_file():
            seconds = max(0.4, min(8.0, len(text) * 0.055))
            frequency = 180 + int(name[:2], 16)
            subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                            "-i", f"sine=frequency={frequency}:duration={seconds}",
                            "-af", "volume=0.6,adelay=250:all=1,apad=pad_dur=0.35", "-ar", "44100", "-ac", "2",
                            "-c:a", "libmp3lame", "-q:a", "2", str(target)], check=True, capture_output=True)
        return providers.MOCK_MARKER + len(encoded).to_bytes(4, "big") + encoded + target.read_bytes()

    return tone_audio


def _audit_zip(archive: Path, out: Path, name: str, records: list[dict] | None, plan: dict | None) -> dict:
    unpacked = out / f"unpacked-{name}"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(unpacked)
    package = unpacked / "DAISY 3"
    lead, tail = audio_master.MasteringOptions().edges
    report = audit(package, leading=lead, trailing=tail)
    report["alignment"] = alignment(package)
    if records is not None:
        report["once"] = {check["id"]: [check["status"], check["detail"]]
                          for check in once_validation.verify(package, records, plan or {})}
    report["files"] = sorted(path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file())
    return report


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not audio_master.available():
        raise SystemExit("ffmpeg is needed (put .venv/tools on PATH).")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    book = build_epub(out / "el-faro-de-los-viajeros.epub")
    content = book.read_bytes()

    main.settings = main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "asr_secondary_provider": "none", "quality_review_enabled": True,
        "elevenlabs_api_key": "", "deepgram_api_key": "", "fish_audio_api_key": ""})
    storage = out / "storage"
    main.storage_root = storage
    main.database_path = storage / "soni2-jobs.sqlite3"
    main.audio_cache = AudioCache(storage / "audio-cache")
    providers.mock_audio = audible_mock(out / "tones")

    async def no_background(job_id: str) -> None:
        return None

    main._start_or_resume_job = no_background
    client = TestClient(main.app)
    structure = main.parse_book(book.name, content)
    plan = build_narration_plan(main.plan_input(structure))
    # Everything but the table of contents is read: the bibliography too.
    selected = json.dumps({"selected_ids": [section.id for section in plan if section.category != "toc"]})
    form = {"planilla_json": json.dumps(SHEET), "narration_plan_json": selected,
            "metadata_json": json.dumps({"title": "El faro de los viajeros", "author": "Ana Pérez"})}

    response = client.post("/api/books/jobs", data=form,
                           files={"file": (book.name, io.BytesIO(content), "application/epub+zip")})
    if response.status_code != 202:
        raise SystemExit(f"Job refused: {response.status_code} {response.text}")
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    job_dir = Path(job["source_path"]).parent
    chain = json.loads((job_dir / "completeness.json").read_text(encoding="utf-8"))
    release = client.get(f"/api/jobs/{job_id}/release").json()
    planilla = ((job["metadata"].get("once") or {}).get("planilla")) or {}
    job_report = {
        "state": job["state"], "release_state": release["state"], "blockers": release["blockers"],
        "chain": {check["id"]: check["status"] for check in chain["checks"]},
        "units": [{"role": (unit.get("provenance") or {}).get("role"), "text": unit["text"],
                   "spoken": unit["spoken_text"], "context": (unit.get("provenance") or {}).get("speech_context")}
                  for unit in job["sentences"]],
        "warnings": [warning["code"] for warning in (job["metadata"].get("once") or {}).get("warnings", [])],
        "zip": _audit_zip(job_dir / f"{job_id}-once-delivery.zip", out, "job", job["chapters"], planilla),
    }

    main.settings = main.settings.model_copy(update={"daisy_package_layout": "flat"})
    direct = client.post("/api/books/package", data={**form, "voice_id": "mock-es-es-female"},
                         files={"file": (book.name, io.BytesIO(content), "application/epub+zip")})
    direct_report: dict = {"status": direct.status_code}
    if direct.status_code == 200:
        archive = out / "direct-flat.zip"
        archive.write_bytes(direct.content)
        direct_report["zip"] = _audit_zip(archive, out, "direct-flat", job["chapters"], planilla)
        with zipfile.ZipFile(archive) as bundle:
            direct_report["quality_status"] = json.loads(bundle.read("DAISY 3/quality-status.json"))["package"]
    else:
        direct_report["detail"] = direct.text[:500]

    report = {"book": book.name, "job": job_report, "direct_download_flat": direct_report}
    (out / "e2e-once-features.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "job_state": job_report["state"], "release": job_report["release_state"],
        "chain_not_passed": {key: value for key, value in job_report["chain"].items() if value != "passed"},
        "job_zip": {"structural": job_report["zip"]["structural"], "dtd": job_report["zip"]["dtd"]["status"],
                    "timing": job_report["zip"]["timing"]["status"],
                    "alignment": f"{job_report['zip']['alignment']['in_a_pause_then_speech']}/"
                                 f"{job_report['zip']['alignment']['boundaries']}",
                    "once": {key: value[0] for key, value in job_report["zip"]["once"].items()}},
        "direct_flat_zip": ({"structural": direct_report["zip"]["structural"],
                             "dtd": direct_report["zip"]["dtd"]["status"],
                             "timing": direct_report["zip"]["timing"]["status"],
                             "folders": sorted({path.split("/")[0] for path in direct_report["zip"]["files"]
                                                if "/" in path})}
                            if "zip" in direct_report else direct_report),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main_cli()
