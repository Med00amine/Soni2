"""Drive the real application in a browser through the whole journey, and record what happened.

    cd backend
    python -m tools.e2e_ui --base http://127.0.0.1:5181 --api http://127.0.0.1:8011 --out <folder>

Needs the application running (tools.e2e_server plus the Vite dev server, or
any deployment in mock or offline mode) and Microsoft Edge (Playwright's
"msedge" channel). Nothing paid is called when the server is tools.e2e_server.

The journey is the one a person follows: import a book (every supported
format), check its chapters and phrases, produce it, play it, move between
chapters, correct, add and remove phrases, add and remove chapters, regenerate
and download the ZIP, then open the ZIP and check it on its own (archive,
DAISY structure, DTD, timing, references, chapters). Failure paths are tried
too: an unsupported file, a lost connection, a ZIP removed from the server.
Every check is recorded as PASS or FAIL with its evidence in results.json and
results.md; screenshots go beside them.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from playwright.sync_api import BrowserContext, Page, sync_playwright  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402

from app import audio_master, daisy_dtd, daisy_timing, package_integrity, zedval_rules  # noqa: E402
from app.daisy_validation import validate_daisy_package  # noqa: E402

UPLOAD = 'input[type="file"][accept=".pdf,.epub,.docx,.txt,.xml"]'
NORMALIZED = "El camino medía 18 km y costó 3.500 euros."


# --------------------------------------------------------------------------- #
# Books in every format the application reads
# --------------------------------------------------------------------------- #

CHAPTERS = [
    ("Capítulo 1. La llegada", ["Lucía llegó al faro cuando caía la tarde.", NORMALIZED,
                                "La llave de hierro pesaba más de lo que parecía.",
                                "Subió los ciento doce escalones sin detenerse."]),
    ("Capítulo 2. El cuaderno", ["El cuaderno empezaba en enero de 1987.",
                                 "Cada noche anotaba el viento y los barcos.",
                                 "Una palabra añadida con otra tinta decía gracias."]),
    ("Capítulo 3. La visita", ["Una mujer subió al faro con un paraguas roto.",
                               "Traía café y una caja de galletas.",
                               "Se fue antes de que empezara a llover."]),
    ("Capítulo 4. El invierno", ["El invierno llegó con tres días de niebla.",
                                 "Lucía encendió la luz a mano cada noche.",
                                 "En primavera nadie recordaba la tormenta."]),
]


def dtbook() -> bytes:
    page = 1
    levels = []
    for number, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        parts = [f'<level1 id="c{number}"><pagenum page="normal" id="p{page}">{page}</pagenum><h1>{escape(title)}</h1>']
        page += 1
        parts += [f"<p>{escape(text)}</p>" for text in paragraphs]
        levels.append("".join(parts) + "</level1>")
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">
<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" version="2005-3" xml:lang="es-ES"><head>
<meta name="dtb:uid" content="e2e"/><meta name="dc:Title" content="El faro de prueba"/>
<meta name="dc:Creator" content="Equipo de pruebas"/><meta name="dc:Language" content="es-ES"/></head>
<book><frontmatter><doctitle>El faro de prueba</doctitle><docauthor>Equipo de pruebas</docauthor></frontmatter>
<bodymatter>""" + "".join(levels) + "</bodymatter></book></dtbook>").encode("utf-8")


def epub() -> bytes:
    from ebooklib import epub as builder

    book = builder.EpubBook()
    book.set_identifier("e2e-epub")
    book.set_title("El faro de prueba")
    book.set_language("es")
    book.add_author("Equipo de pruebas")
    items = []
    for number, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        item = builder.EpubHtml(title=title, file_name=f"c{number}.xhtml", lang="es")
        item.content = f"<h1>{escape(title)}</h1>" + "".join(f"<p>{escape(text)}</p>" for text in paragraphs)
        book.add_item(item)
        items.append(item)
    book.toc = items
    book.spine = items
    book.add_item(builder.EpubNcx())
    book.add_item(builder.EpubNav())
    target = io.BytesIO()
    builder.write_epub(target, book)
    return target.getvalue()


def docx() -> bytes:
    import docx as builder

    document = builder.Document()
    document.core_properties.title = "El faro de prueba"
    document.core_properties.author = "Equipo de pruebas"
    for title, paragraphs in CHAPTERS:
        document.add_heading(title, level=1)
        for text in paragraphs:
            document.add_paragraph(text)
    target = io.BytesIO()
    document.save(target)
    return target.getvalue()


def pdf() -> bytes:
    import fitz

    document = fitz.open()
    for number, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        page = document.new_page()
        page.insert_text((72, 90), title, fontsize=20)
        y = 140
        for text in paragraphs:
            page.insert_text((72, y), text, fontsize=11)
            y += 24
        page.insert_text((300, 800), str(number), fontsize=9)
    document.set_metadata({"title": "El faro de prueba", "author": "Equipo de pruebas"})
    return document.tobytes()


def txt() -> bytes:
    lines = []
    for number, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        lines += [f"Capítulo {number}", ""] + [text + "\n" for text in paragraphs]
    return "\n".join(lines).encode("utf-8")


BOOKS = {"el-faro.xml": dtbook, "el-faro.epub": epub, "el-faro.docx": docx, "el-faro.pdf": pdf, "el-faro.txt": txt}


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

class Results:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.rows: list[dict] = []

    def check(self, feature: str, test: str, passed: bool, evidence: str) -> bool:
        self.rows.append({"feature": feature, "test": test, "result": "PASS" if passed else "FAIL",
                          "evidence": evidence})
        print(f"{'PASS' if passed else 'FAIL'} | {feature} | {test} | {evidence}", flush=True)
        return passed

    def save(self) -> None:
        (self.out / "results.json").write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["| Feature | Test | Result | Evidence |", "|---|---|---|---|"]
        for row in self.rows:
            evidence = row["evidence"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {row['feature']} | {row['test']} | {row['result']} | {evidence} |")
        (self.out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

def button(page: Page, name: str, exact: bool = False):
    return page.get_by_role("button", name=name, exact=exact)


def wait_saved(page: Page) -> None:
    page.wait_for_function("""() => { const s = document.querySelector('.ed-save');
        return !s || s.classList.contains('is-saved') || s.classList.contains('is-error') }""", timeout=30_000)
    time.sleep(0.3)


def status_text(page: Page) -> str:
    return page.locator("#panel-export .xp-status").inner_text()


def wait_build(page: Page, timeout: float = 900) -> str:
    """Until step 3 says the ZIP is ready, needs a decision, or failed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = status_text(page)
        busy = page.locator('#panel-export [data-primary-action="download"]').get_attribute("aria-busy") == "true" \
            or "is-busy" in (page.locator("#panel-export .xp-status").get_attribute("class") or "")
        if not busy and any(word in text for word in ("ZIP listo", "necesita", "no ha terminado", "se detuvo")):
            return text
        time.sleep(1)
    raise TimeoutError(f"the build did not finish: {status_text(page)}")


def primary_count(page: Page) -> int:
    return page.locator("#panel-export [data-primary-action]").count()


INVENTORY: dict[str, list[dict]] = {}


def inventory(page: Page, where: str) -> None:
    """Every interactive element visible now: what it is, its accessible name, whether it can be used."""
    INVENTORY[where] = page.evaluate("""() => {
      // As a screen reader names it: aria-label, then the label, then the text; the title is only a hint.
      const name = element => {
        const label = element.getAttribute('aria-label') || (element.labels && element.labels[0]
          && element.labels[0].textContent) || (element.tagName === 'SELECT' ? '' : element.textContent)
          || element.getAttribute('title') || element.value || ''
        return label.replace(/\\s+/g, ' ').trim().slice(0, 90)
      }
      // Rendered or inside a collapsed section (reachable by opening it); never hidden steps.
      return [...document.querySelectorAll('button, a[href], input, select, textarea, summary, [role=treeitem], [role=tab]')]
        .filter(element => element.offsetParent !== null || element.type === 'file')
        .map(element => ({ kind: element.getAttribute('role') || (element.tagName === 'INPUT' ? `input:${element.type}`
                             : element.tagName.toLowerCase()),
                           name: name(element), enabled: !element.disabled,
                           visible: element.checkVisibility ? element.checkVisibility() : true,
                           primary: element.hasAttribute('data-primary-action') }))
    }""")


def open_upload(page: Page, base: str, path: Path) -> None:
    page.goto(base)
    page.evaluate("() => window.localStorage.clear()")
    page.goto(base)
    page.set_input_files(UPLOAD, str(path))
    page.wait_for_selector(".narration-outline li", timeout=60_000)


def decode_errors(path: Path) -> str:
    """What ffmpeg reports decoding the whole file, as a player would: empty when it decodes cleanly."""
    result = subprocess.run([audio_master.ffmpeg_path() or "ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, timeout=900)
    return (result.stderr.decode("utf-8", "replace").strip() or ("" if result.returncode == 0 else
                                                                 f"exit {result.returncode}"))


def check_audio(results: Results, label: str, package: Path, player: BrowserContext | None) -> None:
    """Every MP3 in the package decodes from start to end; one chapter plays in the browser."""
    # Chapters wherever the layout puts them; the complete audiobook beside the package.
    package_audio = sorted(package.rglob("*.mp3"))
    chapters = [item for item in package_audio if item.name.startswith("chapter-")]
    files = package_audio + sorted(package.parent.glob("*.mp3"))
    bad = {item.name: error[:120] for item in files if (error := decode_errors(item))}
    results.check("Audio playback", f"{label}: every MP3 in the ZIP decodes from start to end", bool(files) and not bad,
                  f"{len(files)} files ({len(chapters)} chapters + the complete book) decoded"
                  + (f"; errors {bad}" if bad else " without an error"))
    if player is None or not chapters:
        return
    tab = player.new_page()
    try:
        tab.goto(chapters[0].resolve().as_uri())
        tab.wait_for_selector("video, audio", timeout=15_000)
        time.sleep(2.5)
        state = tab.evaluate("""() => { const m = document.querySelector('video, audio');
            return { time: m.currentTime, duration: m.duration, error: m.error ? m.error.code : null } }""")
    finally:
        tab.close()
    results.check("Audio playback", f"{label}: a chapter MP3 taken from the ZIP plays in the browser",
                  state["time"] > 0.5 and state["error"] is None,
                  f"{chapters[0].name}: played to {state['time']:.1f} s of {state['duration']:.1f} s, error {state['error']}")


def check_zip(results: Results, label: str, path: Path, scratch: Path, *, present: list[str] = (),
              absent: list[str] = (), titles: list[str] | None = None, player: BrowserContext | None = None) -> dict:
    """Open the ZIP as a reader would and check it on its own."""
    audit = package_integrity.audit_zip(path, expected_titles=titles)
    failed = [f"{check['id']}: {check['detail']}" for check in audit["checks"] if check["status"] == "failed"]
    results.check("ZIP integrity", f"{label}: read back and audited", audit["status"] == "passed",
                  f"{audit['files']} entries; " + ("; ".join(failed)[:300] if failed else
                                                   f"CRC, layout, manifest, references, audio, order, metadata"
                                                   + (", sections" if titles else "") + " passed"))
    target = scratch / f"unzipped-{label}"
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(target)
    package = target / "DAISY 3"
    try:
        validate_daisy_package(package)
        structural = "passed"
    except ValueError as exc:
        structural = f"failed: {exc}"
    dtd = daisy_dtd.validate_package(package)
    book_audio = next(iter(sorted(target.glob("*.mp3"))), None)
    timing = daisy_timing.summary(daisy_timing.verify(package, leading=1.0, trailing=1.2, bitrate_kbps=128,
                                                      chapter_gap=2.0, book_audio=book_audio))
    rules = zedval_rules.summary(zedval_rules.verify(package))
    results.check("DAISY validation", f"{label}: structure, DTD (Z39.86-2005), timing and ZedVal's Z39.86 rules",
                  structural == "passed" and dtd["status"] == "passed" and timing["status"] == "passed"
                  and rules["status"] == "passed",
                  f"structural {structural}; DTD {dtd['status']} ({len(dtd['errors'])} errors); timing "
                  f"{timing['status']} {timing['failed'] or ''}; ZedVal rules {rules['status']} "
                  f"{rules['requirements_failed'] or ''}")
    check_audio(results, label, package, player)
    text = "".join(item.read_text(encoding="utf-8") for item in package.iterdir()
                   if item.suffix in {".xml", ".ncx", ".opf"})
    if present or absent:
        missing = [item for item in present if item not in text]
        lingering = [item for item in absent if item in text]
        results.check("ZIP content", f"{label}: edited text is in, removed text is out", not missing and not lingering,
                      f"missing {missing}; still present {lingering}" if missing or lingering
                      else f"{len(present)} expected texts present, {len(absent)} removed texts absent")
    shutil.rmtree(target, ignore_errors=True)
    return audit


def journey(page: Page, base: str, api: str, out: Path, results: Results) -> None:
    books = out / "books"
    book = books / "el-faro.xml"

    # 1-6. Open, import, parse, chapters, phrases, normalization.
    page.goto(base)
    results.check("Navigation", "Open the application", page.locator("#tab-upload").is_visible(),
                  f"title «{page.title()}», tabs: {page.locator('[role=tab]').all_inner_texts()}")
    inventory(page, "step 1, before a book")
    open_upload(page, base, book)
    inventory(page, "step 1, with a book")
    sections = page.locator(".narration-outline li").all_inner_texts()
    results.check("Import", "Import DTBook XML and list its sections", len(sections) >= 4,
                  f"{len(sections)} sections: {[item.splitlines()[0] for item in sections][:6]}")
    button(page, "Ver la estructura del audiolibro").click()
    page.wait_for_selector('[role="treeitem"]')
    files = page.locator('[role="treeitem"]').all_inner_texts()
    results.check("Chapter extraction", "Chapters become audio files (3 ONCE files + 4 chapters)", len(files) == 7,
                  f"{len(files)} files: {[item.splitlines()[1] if len(item.splitlines()) > 1 else item for item in files]}")
    page.locator('[role="treeitem"]').nth(3).click()
    phrases = page.locator("#panel-review .ed-phrase-text").all_inner_texts()
    results.check("Sentence segmentation", "Chapter 1 shows its heading and four paragraphs, in order",
                  phrases[:1] == [CHAPTERS[0][0]] and phrases[1:5] == CHAPTERS[0][1],
                  f"{len(phrases)} units: {phrases[:5]}")
    page.screenshot(path=str(out / "step2-outline.png"), full_page=False)
    inventory(page, "step 2, before production")

    # 7. Produce (from step 3, the first action) and download.
    page.locator("#tab-export").click()
    inventory(page, "step 3, no ZIP yet")
    results.check("Step 3", "Exactly two primary actions before any build", primary_count(page) == 2,
                  f"{page.locator('#panel-export [data-primary-action]').all_inner_texts()}")
    page.screenshot(path=str(out / "step3-idle.png"), full_page=True)
    started = time.monotonic()
    with page.expect_download(timeout=900_000) as download_info:
        page.locator('#panel-export [data-primary-action="download"]').click()
        time.sleep(1.5)
        busy_actions = primary_count(page)
        disabled = page.locator('#panel-export [data-primary-action="regenerate"]').is_disabled()
        page.screenshot(path=str(out / "step3-building.png"), full_page=True)
        stages_seen = page.locator(".xp-stage").all_inner_texts()
        quality = page.locator("#panel-export .release-panel")
        quality_text = quality.inner_text() if quality.count() else ""
    first_zip = out / "first.zip"
    download_info.value.save_as(first_zip)
    seconds = time.monotonic() - started
    results.check("Step 3", "While building: two actions, both locked against a second build",
                  busy_actions == 2 and disabled, f"{busy_actions} actions; regenerate disabled={disabled}")
    results.check("Progress", "Real stages shown while building", len(stages_seen) == 8,
                  " / ".join(stage.replace("\n", " ")[:60] for stage in stages_seen[:4]))
    results.check("Progress", "While building, quality reports units not narrated yet, not a missing tool",
                  "ffmpeg" not in quality_text,
                  " / ".join(line for line in quality_text.splitlines()[:3] if line.strip())[:200] or "no report yet")
    results.check("Audiobook generation", "«Descargar ZIP» builds the book and downloads it", first_zip.stat().st_size > 0,
                  f"{download_info.value.suggested_filename}, {first_zip.stat().st_size // 1024} KB in {seconds:.0f} s")
    page.screenshot(path=str(out / "step3-ready.png"), full_page=True)
    inventory(page, "step 3, ZIP ready")
    results.check("Step 3", "Ready: status and the ZIP read back", "ZIP listo" in status_text(page),
                  status_text(page))
    check_zip(results, "first", first_zip, out, titles=None,
              present=[CHAPTERS[0][1][0], CHAPTERS[3][0]], absent=[], player=page.context)

    # Normalization: the voice was given the spoken form, the package keeps the text.
    draft = page.evaluate("() => window.localStorage.getItem('soni2-book-draft')")
    job = httpx.get(f"{api}/api/drafts/{draft}").json()["draft"]["last_job"]
    units = httpx.get(f"{api}/api/jobs/{job['job_id']}").json()["sentences"]
    normalized = next((unit for unit in units if unit["text"] == NORMALIZED), {})
    results.check("Text normalization", "Numbers and units are spelled for the voice, not in the text",
                  "kilómetros" in normalized.get("spoken_text", "") and "3.500" in normalized.get("text", ""),
                  f"text «{normalized.get('text')}» → spoken «{normalized.get('spoken_text')}»")
    narrated_first = sum(1 for attempt in httpx.get(f"{api}/api/jobs/{job['job_id']}/attempts").json()["attempts"]
                         if (attempt.get("provider") or {}).get("cache") == "miss")

    # 8-9. Play and navigate.
    page.locator("#tab-review").click()
    page.locator('[role="treeitem"]').nth(3).click()
    inventory(page, "step 2, produced book")
    page.locator(".ed-play").click()
    time.sleep(2.5)
    played = page.evaluate("""() => { const a = document.querySelector('#panel-review audio');
        return { time: a.currentTime, paused: a.paused, src: a.getAttribute('src') } }""")
    results.check("Playback", "The chapter's final audio plays", played["time"] > 0.5 and "/sections/" in (played["src"] or ""),
                  f"currentTime {played['time']:.1f} s, source {played['src']}")
    page.locator(".ed-play").click()
    page.keyboard.press("Alt+ArrowDown")
    title_after = page.locator("#ed-section-title").inner_text()
    results.check("Navigation", "Alt+↓ moves to the next chapter", title_after == CHAPTERS[1][0], f"now on «{title_after}»")
    page.locator('[role="treeitem"]').nth(5).click()
    results.check("Navigation", "Clicking a chapter in the structure opens it",
                  page.locator("#ed-section-title").inner_text() == CHAPTERS[2][0],
                  page.locator("#ed-section-title").inner_text())

    # 10-19. Edit the produced book.
    button(page, "Editar el texto").click()
    page.locator('[role="treeitem"]').nth(3).click()
    inventory(page, "step 2, editing the text")
    button(page, "Corregir la frase 2").click()
    page.locator(".ed-edit textarea").fill("Lucía llegó al faro cuando ya caía la tarde, cansada.")
    button(page, "Guardar la corrección").click()
    wait_saved(page)
    button(page, "Añadir una frase después de la frase 3").click()
    page.locator(".ed-edit textarea").fill("Nadie la esperaba en el muelle aquella tarde.")
    button(page, "Añadir la frase").click()
    wait_saved(page)
    button(page, "Eliminar la frase 6", exact=True).click()
    button(page, "Eliminar la frase", exact=True).click()
    wait_saved(page)
    phrases = page.locator("#panel-review .ed-phrase-text").all_inner_texts()
    results.check("Sentence editing", "Correct, add and remove phrases in chapter 1",
                  "Lucía llegó al faro cuando ya caía la tarde, cansada." in phrases
                  and "Nadie la esperaba en el muelle aquella tarde." in phrases
                  and CHAPTERS[0][1][3] not in phrases, f"{len(phrases)} units now: {phrases[1:6]}")
    page.locator('[role="treeitem"]').nth(4).click()
    button(page, "Añadir capítulo después").click()
    rows_before = page.locator('[role="treeitem"]').count()
    button(page, "Añadir el capítulo").click()
    refused = page.locator(".ed-add-error")
    results.check("Error handling", "Adding a chapter without a title is refused, with the reason",
                  refused.count() == 1 and page.locator('[role="treeitem"]').count() == rows_before,
                  (refused.inner_text() if refused.count() else "no message")
                  + f"; {page.locator('[role=treeitem]').count()} files (was {rows_before})")
    page.locator("#ed-add-name").fill("Capítulo nuevo. La tormenta")
    page.locator("#ed-add-text").fill("La tormenta llegó de noche.\n\nPor la mañana el mar estaba en calma.")
    button(page, "Añadir el capítulo").click()
    wait_saved(page)
    tree = page.locator('[role="treeitem"]').all_inner_texts()
    results.check("Chapter addition", "Add a chapter after chapter 2", any("La tormenta" in row for row in tree)
                  and len(tree) == 8, f"{len(tree)} files; {[row.splitlines()[1] for row in tree[3:]]}")
    page.locator('[role="treeitem"]').filter(has_text=CHAPTERS[3][0]).click()
    button(page, "Eliminar capítulo").click()
    button(page, "Eliminar el capítulo").click()
    wait_saved(page)
    tree = page.locator('[role="treeitem"]').all_inner_texts()
    results.check("Chapter removal", "Remove chapter 4", not any(CHAPTERS[3][0] in row for row in tree),
                  f"{len(tree)} files now")

    # Finding a phrase anywhere, and undoing a change.
    search = page.locator('#panel-review input[type="search"]')
    search.fill("paraguas")
    page.locator(".ed-results button").first.click()
    found = page.locator("#ed-section-title").inner_text()
    results.check("Search", "A phrase is found anywhere in the book and opens its chapter", found == CHAPTERS[2][0],
                  f"«paraguas» → {found}")
    search.fill("")
    page.locator('[role="treeitem"]').filter(has_text=CHAPTERS[1][0]).click()
    phrase = CHAPTERS[1][1][0]

    def stored(present: bool) -> bool:
        """Whether the server's copy of the book has the phrase, waiting for the save to land."""
        deadline = time.monotonic() + 15
        while True:
            outline = json.dumps(httpx.get(f"{api}/api/drafts/{draft}").json()["outline"], ensure_ascii=False)
            if (phrase in outline) == present or time.monotonic() > deadline:
                return (phrase in outline) == present
            time.sleep(0.4)

    def shown(present: bool) -> bool:
        """Whether the page shows the phrase. The editor shows the server's answer, so this waits for it."""
        try:
            page.wait_for_function("""([text, present]) => [...document.querySelectorAll('#panel-review .ed-phrase-text')]
                .some(item => item.textContent.trim() === text) === present""", arg=[phrase, present], timeout=15_000)
            return True
        except PlaywrightTimeout:
            return False

    button(page, "Eliminar la frase 2", exact=True).click()
    button(page, "Eliminar la frase", exact=True).click()
    gone = shown(False) and stored(False)
    button(page, "Deshacer").click()
    back = shown(True) and stored(True)
    wait_saved(page)
    results.check("Undo", "«Deshacer» brings back a deleted phrase, on the page and on the server", gone and back,
                  f"deleted (page and server): {gone}; back after undo (page and server): {back}")
    page.screenshot(path=str(out / "step2-edited.png"), full_page=False)

    # 20-22. Step 3: stale, regenerate, download.
    page.locator("#tab-export").click()
    stale = status_text(page)
    changes = page.locator("#panel-export .xp-warn").first.inner_text() if page.locator("#panel-export .xp-warn").count() else ""
    results.check("Step 3", "Says the book changed since the ZIP, and what changed", "Has cambiado el libro" in stale,
                  f"{stale} | {changes}")
    outdated = page.locator("#panel-export .release-outdated")
    publish = page.locator("#panel-export").get_by_role("button", name="Publicar el audiolibro")
    results.check("Step 3", "The previous ZIP's quality report is shown, and not offered for publication",
                  outdated.count() == 1 and publish.count() == 0,
                  (outdated.inner_text() if outdated.count() else "no note") + f" | publish buttons: {publish.count()}")
    page.screenshot(path=str(out / "step3-stale.png"), full_page=True)
    inventory(page, "step 3, book changed since the ZIP")
    started = time.monotonic()
    page.locator('#panel-export [data-primary-action="regenerate"]').click()
    final = wait_build(page)
    regen_seconds = time.monotonic() - started
    stages = page.locator(".xp-stage").all_inner_texts()
    results.check("ZIP generation", "«Regenerar ZIP» rebuilds from the latest text without downloading", "ZIP listo" in final,
                  f"{final} in {regen_seconds:.0f} s; " + " / ".join(stage.replace("\n", " ")[:70] for stage in stages[1:4]))
    page.screenshot(path=str(out / "step3-regenerated.png"), full_page=True)
    with page.expect_download(timeout=120_000) as download_info:
        page.locator('#panel-export [data-primary-action="download"]').click()
    second_zip = out / "edited.zip"
    download_info.value.save_as(second_zip)
    results.check("ZIP download", "«Descargar ZIP» hands over the regenerated ZIP at once", second_zip.stat().st_size > 0,
                  f"{download_info.value.suggested_filename}, {second_zip.stat().st_size // 1024} KB")
    job = httpx.get(f"{api}/api/drafts/{draft}").json()["draft"]["last_job"]
    attempts = httpx.get(f"{api}/api/jobs/{job['job_id']}/attempts").json()["attempts"]
    narrated = sorted({(attempt.get("provider") or {}).get("cache") for attempt in attempts})
    misses = sum(1 for attempt in attempts if (attempt.get("provider") or {}).get("cache") == "miss")
    reused = sum(1 for attempt in attempts if attempt.get("outcome") == "verdict_reused")
    results.check("Audio regeneration", "Only new and changed text is narrated; the rest is reused",
                  0 < misses <= 6 and reused > 0,
                  f"first build narrated {narrated_first} units; regeneration narrated {misses} and reused {reused} "
                  f"({narrated})")
    structure = httpx.get(f"{api}/api/jobs/{job['job_id']}/structure").json()
    titles = [section["title"] for section in structure["sections"]]
    edited_present = ["Lucía llegó al faro cuando ya caía la tarde, cansada.",
                      "Nadie la esperaba en el muelle aquella tarde.", "Capítulo nuevo. La tormenta",
                      "Por la mañana el mar estaba en calma."]
    edited_absent = [CHAPTERS[0][1][3], CHAPTERS[3][0], CHAPTERS[3][1][0], "Lucía llegó al faro cuando caía la tarde."]
    check_zip(results, "edited", second_zip, out, titles=titles, present=edited_present, absent=edited_absent,
              player=page.context)

    # A reload finds the book, the step and the ZIP. While it restores, step 3 says so (a busy status).
    page.reload()
    page.wait_for_selector("#panel-export .xp-status")
    restoring = status_text(page)
    page.wait_for_function("() => !document.querySelector('#panel-export .xp-status.is-busy')", timeout=30_000)
    results.check("Persistence", "A reload restores the book, the step and the ready ZIP",
                  page.locator("#tab-export").get_attribute("aria-selected") == "true" and "ZIP listo" in status_text(page)
                  and "Todavía no hay un ZIP" not in restoring,
                  f"while restoring: «{restoring}»; then: «{status_text(page)}»")

    # Failure paths.
    output = job.get("job_id")
    zip_path = httpx.get(f"{api}/api/jobs/{output}").json()["output_path"]
    Path(zip_path).rename(Path(zip_path).with_suffix(".moved"))
    page.locator('#panel-export [data-primary-action="download"]').click()
    page.wait_for_selector("#panel-export .xp-notice")
    notice = page.locator("#panel-export .xp-notice").inner_text()
    results.check("Error handling", "A ZIP missing on the server is explained, with what to do",
                  "Regenerar ZIP" in notice, notice)
    Path(zip_path).with_suffix(".moved").rename(zip_path)

    page.route("**/api/jobs/**", lambda route: route.abort())
    page.locator('#panel-export [data-primary-action="regenerate"]').click()
    time.sleep(4)
    page.unroute("**/api/jobs/**")
    final = wait_build(page, timeout=600)
    results.check("Error handling", "A lost connection during a build is waited out, and the build finishes",
                  "ZIP listo" in final, final)

    # Book data: a new publisher outdates the ZIP; a double click starts one build; the new ZIP carries it.
    page.locator("#panel-export summary").filter(has_text="Datos del libro").click()
    page.locator("#metadata-publisher").fill("Ediciones del Faro")
    page.wait_for_function("""() => (document.querySelector('#panel-export .xp-status')?.textContent || '')
        .includes('Has cambiado el libro')""", timeout=15_000)
    outdated_status = status_text(page)
    requests_seen = []

    def job_request(response) -> None:
        if response.request.method == "POST" and response.url.endswith("/api/books/jobs"):
            requests_seen.append(response)

    page.on("response", job_request)
    page.locator('#panel-export [data-primary-action="regenerate"]').dblclick()
    final = wait_build(page)
    page.remove_listener("response", job_request)
    jobs_started = {(response.json() or {}).get("job_id") for response in requests_seen}
    results.check("Duplicate prevention", "A double click on «Regenerar ZIP» starts one build",
                  len(jobs_started) == 1 and "ZIP listo" in final,
                  f"{len(requests_seen)} job request(s), {len(jobs_started)} job; {final}")
    with page.expect_download(timeout=120_000) as download_info:
        page.locator('#panel-export [data-primary-action="download"]').click()
    metadata_zip = out / "metadata.zip"
    download_info.value.save_as(metadata_zip)
    with zipfile.ZipFile(metadata_zip) as archive:
        opf = next(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".opf"))
    # The printed book's publisher is the source's (dtb:sourcePublisher);
    # dc:Publisher names the publisher of the DAISY book itself.
    publisher = re.search(r'<meta name="dtb:sourcePublisher" content="([^"]*)"', opf)
    results.check("Book data", "A new publisher outdates the ZIP, and the regenerated ZIP's metadata carries it",
                  "Has cambiado el libro" in outdated_status and bool(publisher)
                  and publisher.group(1) == "Ediciones del Faro",
                  f"status «{outdated_status[:58]}…»; OPF dtb:sourcePublisher "
                  f"«{publisher.group(1) if publisher else 'missing'}»")

    # Without the quality review the same two actions build the ZIP directly.
    page.locator("#panel-export summary").filter(has_text="Opciones de entrega").click()
    page.get_by_label("Revisión de calidad automática").uncheck()
    with page.expect_download(timeout=300_000) as download_info:
        page.locator('#panel-export [data-primary-action="download"]').click()
    direct_zip = out / "direct.zip"
    download_info.value.save_as(direct_zip)
    with zipfile.ZipFile(direct_zip) as archive:
        names = archive.namelist()
        quality_status = json.loads(archive.read("quality-status.json")) if "quality-status.json" in names else {}
    page.wait_for_function("() => !document.querySelector('#panel-export .xp-status.is-busy')", timeout=60_000)
    results.check("ZIP generation", "Quality review off: «Descargar ZIP» builds the ZIP directly, marked unverified",
                  "sin verificar" in status_text(page) and bool(quality_status)
                  and "DAISY 3/quality-status.json" not in names,
                  f"{download_info.value.suggested_filename}, {direct_zip.stat().st_size // 1024} KB; "
                  f"status «{status_text(page)}»; quality-status.json at the root: "
                  f"{json.dumps(quality_status, ensure_ascii=False)[:120]}")
    check_zip(results, "direct", direct_zip, out, titles=titles, present=edited_present, absent=edited_absent,
              player=page.context)
    page.get_by_label("Revisión de calidad automática").check()

    # Mobile layout.
    page.set_viewport_size({"width": 390, "height": 844})
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    page.screenshot(path=str(out / "step3-mobile.png"), full_page=True)
    results.check("Responsive", "Step 3 at 390 px: no horizontal scroll, two actions", overflow <= 0 and primary_count(page) == 2,
                  f"overflow {overflow}px")
    page.set_viewport_size({"width": 1440, "height": 900})
    unnamed = page.evaluate("""() => [...document.querySelectorAll('button')].filter(b =>
        !(b.getAttribute('aria-label') || b.textContent || '').trim() && b.offsetParent !== null).length""")
    results.check("Accessibility", "Every visible button has a name", unnamed == 0, f"{unnamed} unnamed")


def formats(page: Page, base: str, out: Path, results: Results) -> None:
    for name in ("el-faro.epub", "el-faro.docx", "el-faro.pdf", "el-faro.txt"):
        open_upload(page, base, out / "books" / name)
        button(page, "Ver la estructura del audiolibro").click()
        page.wait_for_selector('[role="treeitem"]')
        rows = page.locator('[role="treeitem"]').all_inner_texts()
        page.locator('[role="treeitem"]').nth(3).click()
        phrases = page.locator("#panel-review .ed-phrase-text").all_inner_texts()
        titles = [row.splitlines()[1] if len(row.splitlines()) > 1 else row for row in rows[3:]]
        results.check("Import", f"Import {name.rsplit('.', 1)[1].upper()}: its 4 chapters and their text",
                      len(rows) == 7 and CHAPTERS[0][1][0] in phrases,
                      f"{len(rows)} files (3 ONCE + {len(rows) - 3}); chapters {titles}; chapter 1 starts "
                      f"{phrases[:2]}")
    page.goto(base)
    html = out / "books" / "el-faro.html"
    html.write_text("<html><body><h1>Capítulo 1</h1><p>Texto.</p></body></html>", encoding="utf-8")
    accept = page.locator(UPLOAD).get_attribute("accept")
    results.check("Import", "HTML is not a supported format: the picker does not offer it", ".html" not in accept,
                  f"accept=\"{accept}\"")


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5181")
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    parser.add_argument("--out", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    (out / "books").mkdir(parents=True, exist_ok=True)
    for name, make in BOOKS.items():
        (out / "books" / name).write_bytes(make())
    results = Results(out)
    response = httpx.post(f"{args.api}/api/drafts", files={"file": ("x.html", b"<html></html>", "text/html")})
    results.check("Import", "The server refuses an unsupported file with a reason", response.status_code == 415,
                  f"HTTP {response.status_code}: {response.json().get('detail')}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.headed,
                                             args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="es-ES")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            journey(page, args.base, args.api, out, results)
            formats(page, args.base, out, results)
        except Exception as exc:  # recorded, never hidden
            results.check("E2E run", "The journey ran to its end", False, f"{type(exc).__name__}: {exc}"[:400])
            traceback.print_exc()
            page.screenshot(path=str(out / "failure.png"), full_page=True)
        results.check("Stability", "No uncaught error in the page", not errors, "; ".join(errors)[:300] or "none")
        browser.close()
    results.save()
    (out / "inventory.json").write_text(json.dumps(INVENTORY, ensure_ascii=False, indent=2), encoding="utf-8")
    for where, elements in INVENTORY.items():
        primaries = sum(1 for element in elements if element["primary"])
        print(f"inventory: {where}: {len(elements)} interactive elements, {primaries} primary actions")
    passed = sum(row["result"] == "PASS" for row in results.rows)
    print(f"\n{passed}/{len(results.rows)} checks passed. {out / 'results.md'}")


if __name__ == "__main__":
    main_cli()
