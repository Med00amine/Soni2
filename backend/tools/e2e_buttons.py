"""Press every button of the application in a browser and check that each one does its job.

    cd backend
    python -m tools.e2e_buttons --base http://127.0.0.1:5181 --api http://127.0.0.1:8011 --out <folder>
        [--planilla "D:/Downloads/MODELO ONCE.docx"]

The companion of tools.e2e_ui. That journey drives the main path (import,
structure, produce from step 3, play, edit, undo, search, regenerate,
download, the direct ZIP) and this run presses everything else, one control
at a time, checking what each did on the page and on the server, not only
that it could be clicked:

* step 1: a lost connection and «Reintentar», the skip link, the logo, choosing
  and changing the file, the PDF page range, the sections to narrate, the
  voice, and the preview with its speed, download, text, review and corrected
  sample;
* step 2 before production: the initial-files group, a phrase, the planilla
  (opened from file 1 and from the side panel, its fields, restoring the
  model's legal text, the ONCE switch), every «Cancelar», a chapter's text
  imported from a file, and producing from step 2;
* a phrase the voice misreads: the problem navigation, step 3's list of
  phrases awaiting a decision, the comparison, «La locución es correcta», and
  where the difference is heard in the finished book;
* step 2 with the book produced: listening from the start and from a phrase,
  previous and next phrase and section, the position slider, the speed,
  narrating a phrase again, correcting and narrating it, the pronunciation
  editor, editing the text and going back to the audio, producing the changes;
* step 3: every field of the book data, the fallback download link, the
  checks' limits, and publishing;
* other books: a chapter's subchapters, a planilla imported from Word, and
  navigation sections for a book without chapters.

Every visible control is recorded. The report ends with each control and what
pressed it, here or in the journey, and lists any seen but pressed by neither.

Needs tools.e2e_server started with ``--extra-voice`` (two voices to choose
between) and ``--mishear catalejo=sombrero`` (a phrase this run adds is
misread, so a person's decisions can be pressed), and the Vite dev server.
Nothing paid is called.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from playwright.sync_api import Locator, Page, sync_playwright  # noqa: E402

from tools.e2e_ui import BOOKS, CHAPTERS, UPLOAD, Results, button, status_text, wait_build, wait_saved  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "corpus"
SUBCHAPTERS_BOOK = CORPUS / "el-faro-de-la-isla-almendra.xml"
# Added before production; the offline voice says «sombrero» for «catalejo».
# New on every run: a person's acceptance of the same text and audio in an
# earlier run is remembered, and the phrase would not wait for a decision.
MISREAD = "El farero {} guardaba un catalejo traído de Whitby.".format(
    random.choice(["Andrés", "Benito", "Ciro", "Damián", "Elías", "Fermín", "Gonzalo", "Hilario"])
    + " " + random.choice(["Arias", "Bravo", "Cano", "Duque", "Esteve", "Fuentes", "Gil", "Herrera"]))

# What a person sees a control as: its kind and its name, numbers as N, and
# the controls repeated for each phrase, word or section as one entry.
KEY_JS = """element => {
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim()
  const textOf = node => {
    const copy = node.cloneNode(true)
    copy.querySelectorAll('small, svg, .visually-hidden').forEach(child => child.remove())
    return clean(copy.textContent)
  }
  const labelOf = node => {
    const label = node.labels && node.labels[0]
    if (!label) return ''
    const copy = label.cloneNode(true)
    copy.querySelectorAll('input, select, textarea, small, option').forEach(child => child.remove())
    return clean(copy.textContent)
  }
  const kind = element.getAttribute('role') === 'tab' ? 'tab' : element.getAttribute('role') === 'treeitem' ? 'treeitem'
    : element.tagName === 'INPUT' ? 'input:' + element.type : element.tagName.toLowerCase()
  const fixed = name => kind + ' | ' + name
  if (kind === 'treeitem') return fixed('(un archivo de la estructura)')
  if (element.closest('.narration-outline') && element.type === 'checkbox') return fixed('(casilla de una sección)')
  if (element.classList.contains('ed-phrase-main')) return fixed('(una frase de la lista)')
  if (element.classList.contains('pron-word')) return fixed('(una palabra de la frase)')
  if (element.classList.contains('ed-twisty')) return fixed('(flecha de un capítulo con subcapítulos)')
  if (element.closest('.ed-results')) return fixed('(un resultado de la búsqueda)')
  if (element.classList.contains('xp-link')) return fixed('(un párrafo que espera decisión)')
  if (element.closest('.fault-locations') && element.tagName === 'BUTTON') return fixed('Escuchar (dónde se oye una diferencia)')
  if (element.closest('.pron-options') && element.tagName === 'BUTTON') return fixed(textOf(element) + ' (pronunciación)')
  let name = element.getAttribute('aria-label') || labelOf(element)
    || (['SELECT', 'TEXTAREA', 'INPUT'].includes(element.tagName) ? '' : textOf(element))
    || element.getAttribute('title') || element.getAttribute('placeholder') || ''
  if (kind === 'tab') name = textOf(element).replace(/^\\d+/, '')
  if (name.startsWith('Posición en')) name = 'Posición en (la sección)'
  if (name.startsWith('Descargar ZIP')) name = 'Descargar ZIP'
  return fixed(name.replace(/\\d+/g, 'N').slice(0, 80))
}"""
CONTROLS = "button, a[href], input, select, textarea, summary, [role=treeitem]"
COVERAGE: dict[str, str] = {}
SEEN: dict[str, str] = {}
# The controls tools.e2e_ui presses, and the check that shows what each did.
JOURNEY = {
    "tab | Subir libro": "Navigation: Open the application",
    "tab | Escuchar y revisar": "Playback: The chapter's final audio plays",
    "tab | Exportar": "Step 3: Exactly two primary actions before any build",
    "button | Descargar ZIP": "Audiobook generation: «Descargar ZIP» builds the book and downloads it",
    "button | Regenerar ZIP": "ZIP generation: «Regenerar ZIP» rebuilds from the latest text without downloading",
    "button | Corregir la frase N": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Guardar la corrección": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Añadir una frase después de la frase N": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Añadir la frase": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Eliminar la frase N": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Eliminar la frase": "Sentence editing: Correct, add and remove phrases in chapter 1",
    "button | Añadir capítulo después": "Chapter addition: Add a chapter after chapter 2",
    "input:text | Título del capítulo (se leerá así)": "Chapter addition: Add a chapter after chapter 2",
    "textarea | Texto": "Chapter addition: Add a chapter after chapter 2",
    "button | Añadir el capítulo": "Error handling: Adding a chapter without a title is refused, with the reason",
    "button | Eliminar capítulo": "Chapter removal: Remove chapter 4",
    "button | Eliminar el capítulo": "Chapter removal: Remove chapter 4",
    "input:search | Buscar en el libro": "Search: A phrase is found anywhere in the book and opens its chapter",
    "button | (un resultado de la búsqueda)": "Search: A phrase is found anywhere in the book and opens its chapter",
    "button | Deshacer": "Undo: «Deshacer» brings back a deleted phrase, on the page and on the server",
    "summary | Opciones de entrega": "ZIP generation: Quality review off: «Descargar ZIP» builds the ZIP directly",
    "input:checkbox | Revisión de calidad automática (recomendada)":
        "ZIP generation: Quality review off: «Descargar ZIP» builds the ZIP directly",
    "input:text | Editorial": "Book data: A new publisher outdates the ZIP, and the regenerated ZIP's metadata carries it",
    "button | Reproducir": "Playback: The chapter's final audio plays",
}


def key_of(locator: Locator) -> str:
    return locator.first.evaluate(KEY_JS)


def mark(locator: Locator, check: str) -> Locator:
    """Record that ``check`` presses this control, under the name a person sees it by."""
    try:
        COVERAGE.setdefault(key_of(locator), check)
    except Exception:  # an absent control is reported by the check itself
        pass
    return locator


def see(page: Page) -> None:
    """Remember every control visible now, and the step it was seen in, for the coverage table."""
    tab = page.locator("[role=tab][aria-selected=true]")
    where = tab.first.inner_text().replace("\n", " ") if tab.count() else ""
    for key in page.evaluate(f"""() => [...document.querySelectorAll('{CONTROLS}')]
        .filter(element => element.offsetParent !== null || element.type === 'file')
        .map({KEY_JS})"""):
        SEEN.setdefault(key, where)


def not_run(results: Results, feature: str, test: str, reason: str) -> None:
    """A control this environment cannot show, said plainly rather than counted as passed."""
    results.rows.append({"feature": feature, "test": test, "result": "NOT RUN", "evidence": reason})
    print(f"NOT RUN | {feature} | {test} | {reason}", flush=True)


def wait_for(predicate, timeout: float = 30, step: float = 0.4):
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(step)
        value = predicate()
    return value


def attempt(results: Results, feature: str, action) -> None:
    """Run one check; an exception is a FAIL with its message, never a stop."""
    try:
        action()
    except Exception as exc:  # recorded, never hidden
        results.check(feature, action.__name__.replace("_", " "), False, f"{type(exc).__name__}: {exc}"[:300])
        traceback.print_exc()


def draft_view(page: Page, api: str) -> dict:
    draft = page.evaluate("() => window.localStorage.getItem('soni2-book-draft')")
    return httpx.get(f"{api}/api/drafts/{draft}").json()


def last_job(page: Page, api: str) -> str:
    return (draft_view(page, api)["draft"].get("last_job") or {}).get("job_id") or ""


def job(api: str, job_id: str) -> dict:
    return httpx.get(f"{api}/api/jobs/{job_id}").json()


def editor_audio(page: Page) -> dict:
    return page.evaluate("""() => { const a = document.querySelector('#panel-review .audiobook-editor > audio');
        return a ? { time: a.currentTime, paused: a.paused, rate: a.playbackRate, src: a.getAttribute('src') || '' } : {} }""")


def pause(page: Page) -> None:
    if not editor_audio(page).get("paused", True):
        page.locator("#panel-review .ed-play").click()


def selected_number(page: Page) -> str:
    item = page.locator("#panel-review .ed-phrase.is-selected .ed-phrase-number")
    return item.first.inner_text() if item.count() else ""


def selected_text(page: Page) -> str:
    item = page.locator("#panel-review .ed-phrase.is-selected .ed-phrase-text")
    return item.first.inner_text() if item.count() else ""


def section_title(page: Page) -> str:
    return page.locator("#ed-section-title").inner_text()


def tree(page: Page) -> Locator:
    return page.locator('#panel-review [role="treeitem"]')


def open_details(locator: Locator) -> None:
    """Open a <details> by its summary, leaving it open if it already is."""
    if not locator.evaluate("details => details.open"):
        locator.locator("summary").first.click()


# --------------------------------------------------------------------------- #
# Step 1
# --------------------------------------------------------------------------- #

def step1(page: Page, base: str, api: str, out: Path, results: Results) -> None:
    books = out / "books"

    def connection_retry() -> None:
        page.route("**/api/capabilities", lambda route: route.abort())
        page.goto(base)
        page.evaluate("() => window.localStorage.clear()")
        page.goto(base)
        alert = page.locator(".audio-setup-note[role=alert]").filter(has_text="No se pudo conectar")
        alert.wait_for(timeout=15_000)
        page.unroute("**/api/capabilities")
        time.sleep(2)  # nothing retries on its own
        still = alert.count() == 1
        retry = mark(alert.get_by_role("button", name="Reintentar"), "Connection: «Reintentar» reconnects")
        see(page)
        retry.click()
        gone = wait_for(lambda: alert.count() == 0, 15)
        ready = wait_for(lambda: page.locator(".upload-voice-status").count() == 1, 15)
        results.check("Connection", "With the server unreachable the page says so; «Reintentar» reconnects",
                      still and bool(gone) and bool(ready),
                      f"alert stays until pressed: {still}; after «Reintentar»: alert gone {bool(gone)}, "
                      f"«{page.locator('.upload-voice-status').inner_text() if ready else 'no voice status'}»")

    def tabs_locked() -> None:
        review, export = page.locator("#tab-review"), page.locator("#tab-export")
        results.check("Navigation", "Steps 2 and 3 cannot be opened before a book",
                      review.is_disabled() and export.is_disabled(),
                      f"«Escuchar y revisar» disabled {review.is_disabled()}, «Exportar» disabled {export.is_disabled()}")

    def skip_link() -> None:
        page.goto(base)
        page.wait_for_selector("#tab-upload")
        page.keyboard.press("Tab")
        focused = page.evaluate("() => document.activeElement && document.activeElement.textContent.trim()")
        mark(page.locator("a.skip"), "Accessibility: the skip link")
        page.keyboard.press("Enter")
        target = page.evaluate("() => location.hash")
        results.check("Accessibility", "«Saltar al contenido principal» is the first stop and jumps to the content",
                      focused == "Saltar al contenido principal" and target == "#main",
                      f"first Tab: «{focused}»; Enter: {target}")

    def logo() -> None:
        link = mark(page.get_by_role("link", name="Inicio de Soni2"), "Navigation: the logo leads home")
        link.click()
        page.wait_for_load_state()
        results.check("Navigation", "The Soni2 logo leads home", page.url.rstrip("/") == base.rstrip("/")
                      and page.locator("#tab-upload").is_visible(), page.url)

    def choose_file() -> None:
        choose = mark(button(page, "Elegir archivo"), "Upload: «Elegir archivo» opens the file chooser")
        mark(page.locator(UPLOAD), "Upload: «Elegir archivo» opens the file chooser")
        with page.expect_file_chooser() as chooser:
            choose.click()
        chooser.value.set_files(str(books / "el-faro.pdf"))
        page.wait_for_selector(".narration-outline ul li", timeout=60_000)
        name = page.locator(".upload-file-name").inner_text()
        results.check("Upload", "«Elegir archivo» opens the file chooser and loads the book", name == "el-faro.pdf",
                      f"loaded «{name}», {page.locator('.narration-outline li').count()} sections")

    for check in (connection_retry, tabs_locked, skip_link, logo, choose_file):
        attempt(results, "Step 1", check)
    see(page)

    def outline_count() -> int:
        return page.locator(".narration-outline ul li").count() if page.locator(".narration-outline ul").count() else -1

    def page_range() -> None:
        before = outline_count()
        field = mark(page.locator("#page-range"), "PDF pages: a range narrows the book")
        apply = mark(page.get_by_role("button", name="Aplicar"), "PDF pages: a range narrows the book")
        field.fill("1-2")
        apply.click()
        narrowed = wait_for(lambda: 0 < outline_count() < before, 60)
        during = outline_count()
        stored = draft_view(page, api)["draft"]["state"].get("pages")
        field.fill("")
        apply.click()
        restored = wait_for(lambda: outline_count() == before, 60)
        results.check("PDF pages", "A page range narrows the book to those pages; clearing it brings the book back",
                      bool(narrowed) and bool(restored) and stored == "1-2",
                      f"{before} sections → «1-2»: {during} (the server keeps «{stored}») → cleared: {outline_count()}")

    def selected_count() -> int:
        view = draft_view(page, api)
        ids = view["draft"]["state"].get("selected_ids")
        return len(ids if ids is not None else view["plan"]["selected_ids"])

    def sections() -> None:
        before = selected_count()
        row = page.locator(".narration-outline li").filter(has_text=CHAPTERS[3][0])
        box = mark(row.locator('input[type="checkbox"]'), "Sections: a section is left out and brought back")
        box.uncheck()
        excluded = wait_for(lambda: "Excluido" in row.inner_text(), 10)
        saved = wait_for(lambda: selected_count() == before - 1, 15)
        box.check()
        included = wait_for(lambda: "Incluido" in row.inner_text(), 10)
        restored = wait_for(lambda: selected_count() == before, 15)
        results.check("Sections", "Unticking a section leaves it out (on the server too); ticking it brings it back",
                      bool(excluded and saved and included and restored),
                      f"«{CHAPTERS[3][0]}»: {before} selected → excluded {bool(excluded)}, saved {bool(saved)} → "
                      f"included {bool(included)}, saved {bool(restored)}")

    def voice() -> None:
        select = mark(page.locator("#voice-id"), "Voice: another voice is chosen and saved")
        values = [option.get_attribute("value") for option in select.locator("option").all()]
        names = select.locator("option").all_inner_texts()
        if len(values) < 2:
            results.check("Voice", "Another voice can be chosen", False, f"only {names}: start e2e_server --extra-voice")
            return
        current = select.input_value()
        other = next(value for value in values if value != current)
        select.select_option(other)
        saved = wait_for(lambda: draft_view(page, api)["draft"]["state"].get("voice_id") == other, 15)
        select.select_option(current)
        back = wait_for(lambda: draft_view(page, api)["draft"]["state"].get("voice_id") == current, 15)
        results.check("Voice", "Choosing another voice is saved with the book, and choosing back too",
                      bool(saved and back), f"voices {names}; {other} saved {bool(saved)}; {current} again {bool(back)}")

    def preview() -> None:
        generate = mark(button(page, "Generar vista previa"), "Preview: a sample is narrated")
        generate.click()
        page.wait_for_selector("#player-title", timeout=180_000)
        loaded = wait_for(lambda: page.evaluate("""() => { const a = document.querySelector('#listen-section audio');
            return a && a.duration > 0 }"""), 30)
        duration = page.evaluate("() => document.querySelector('#listen-section audio').duration")
        results.check("Preview", "«Generar vista previa» narrates a sample and shows its player (in step 2)",
                      bool(loaded) and page.locator("#tab-review").get_attribute("aria-selected") == "true",
                      f"sample of {duration:.1f} s; step 2 open")
        see(page)
        speed = mark(page.locator("#playback-speed"), "Preview: the speed")
        speed.select_option("1.5")
        rate = page.evaluate("() => document.querySelector('#listen-section audio').playbackRate")
        results.check("Preview", "The preview's «Velocidad» changes its playback rate", rate == 1.5, f"playbackRate {rate}")
        link = mark(page.get_by_role("link", name="Descargar muestra MP3"), "Preview: the sample downloads")
        with page.expect_download(timeout=30_000) as download:
            link.click()
        sample = out / "preview-sample.mp3"
        download.value.save_as(sample)
        data = sample.read_bytes()
        results.check("Preview", "«Descargar muestra MP3» downloads the sample, named after the book", len(data) > 0
                      and (b"\xff\xfb" in data[:4096] or b"\xff\xf3" in data[:4096])
                      and not download.value.suggested_filename.startswith("-"),
                      f"{download.value.suggested_filename}, {len(data) // 1024} KB, MPEG frames present")
        details = page.locator(".audio-transcript details")
        mark(details.locator("summary"), "Preview: the synchronized text")
        open_details(details)
        text = details.inner_text().replace("\n", " ")
        results.check("Preview", "«Mostrar texto sincronizado» shows the sample's text",
                      details.evaluate("node => node.open") and "Lucía llegó al faro" in text, text[:110])
        review = mark(button(page, "Revisar calidad del audio"), "Preview: the quality review")
        review.click()
        answer = page.locator("#review-title, #listen-section ~ * [role=alert], .audio-transcript [role=alert]")
        answer.first.wait_for(timeout=120_000)
        if not page.locator("#review-title").count():
            # The offline recognizer reads only the fake voice's own clips, not
            # a mastered sample: the review fails here, and must say so where
            # the button is.
            error = page.locator(".audio-transcript [role=alert]").inner_text()
            results.check("Preview", "A failed «Revisar calidad del audio» says why, beside the button", bool(error), error)
            not_run(results, "Preview", "«Revisar calidad del audio» gives a verdict, and «Generar muestra corregida»",
                    "the offline recognizer cannot read a mastered sample; the corrected sample is offered only "
                    "after a verdict")
            return
        verdict = page.locator("#review-title").inner_text()
        results.check("Preview", "«Revisar calidad del audio» compares the sample with its text", bool(verdict), verdict)
        see(page)
        player = page.locator("#listen-section audio")
        old = player.get_attribute("src")
        retake = mark(page.locator("#retake-text"), "Preview: a corrected sample")
        retake.fill(retake.input_value() + " Fin de la muestra.")
        corrected = mark(button(page, "Generar muestra corregida"), "Preview: a corrected sample")
        corrected.click()
        changed = wait_for(lambda: player.get_attribute("src") not in (None, old), 120)
        results.check("Preview", "«Generar muestra corregida» narrates the edited sample again", bool(changed),
                      f"new audio in the player: {bool(changed)}")

    def change_file() -> None:
        page.locator("#tab-upload").click()
        change = mark(button(page, "Cambiar archivo"), "Upload: «Cambiar archivo» replaces the book")
        with page.expect_file_chooser() as chooser:
            change.click()
        chooser.value.set_files(str(books / "el-faro.xml"))
        loaded = wait_for(lambda: page.locator(".upload-file-name").inner_text() == "el-faro.xml", 60)
        page.wait_for_selector(".narration-outline ul li", timeout=60_000)
        results.check("Upload", "«Cambiar archivo» replaces the book", bool(loaded),
                      f"now «{page.locator('.upload-file-name').inner_text()}», "
                      f"{page.locator('.narration-outline li').count()} sections")

    for check in (page_range, sections, voice, preview, change_file):
        attempt(results, "Step 1", check)
    see(page)


# --------------------------------------------------------------------------- #
# Step 2 before production
# --------------------------------------------------------------------------- #

def step2_outline(page: Page, api: str, out: Path, results: Results) -> None:
    structure = mark(button(page, "Ver la estructura del audiolibro (sin generar muestra)"), "Structure: step 2 opens")
    structure.click()
    page.wait_for_selector('[role="treeitem"]')
    see(page)

    def initial_group() -> None:
        toggle = mark(page.locator(".ed-group-toggle"), "Structure: the initial files fold and unfold")
        before = tree(page).count()
        toggle.click()
        folded, state = tree(page).count(), toggle.get_attribute("aria-expanded")
        toggle.click()
        results.check("Structure", "«Archivos iniciales ONCE» folds and unfolds the three initial files",
                      folded == before - 3 and state == "false" and tree(page).count() == before,
                      f"{before} files → folded {folded} (aria-expanded {state}) → {tree(page).count()}")

    def phrase_select() -> None:
        mark(tree(page).nth(3), "Structure: a file opens")
        tree(page).nth(3).click()
        phrase = mark(page.locator("#panel-review .ed-phrase-main").nth(2), "Structure: a phrase is selected")
        phrase.click()
        results.check("Structure", "Clicking a phrase selects it", phrase.get_attribute("aria-pressed") == "true"
                      and selected_number(page) == "3", f"selected phrase {selected_number(page)} of «{section_title(page)}»")

    def planilla_from_file() -> None:
        tree(page).nth(0).click()
        edit = mark(button(page, "Editar planilla"), "Planilla: opened from file 1")
        edit.click()
        shown = page.locator("#planilla-title").is_visible()
        back = mark(button(page, "Volver a la estructura"), "Planilla: «Volver a la estructura»")
        back.click()
        hidden = page.locator("#planilla-title").count() == 0
        results.check("Planilla", "«Editar planilla» on file 1 opens the planilla; «Volver a la estructura» closes it",
                      shown and hidden, f"opened {shown}; closed {hidden}")

    def planilla_fields() -> None:
        tree(page).nth(3).click()
        review = mark(button(page, "Revisar la planilla de producción"), "Planilla: opened from the side panel")
        review.click()
        panel = page.locator(".planilla-panel")
        see(page)
        title = mark(panel.get_by_label("Título", exact=True), "Planilla: the title and its preview")
        original = title.input_value()
        title.fill(original + " revisado")
        preview_line = panel.locator(".planilla-preview").first.inner_text()
        title.fill(original)
        legal = mark(panel.get_by_label("Texto legal de la ONCE"), "Planilla: restoring the model's legal text")
        model_text = legal.input_value()
        legal.fill(model_text + " Añadido de prueba.")
        restore = mark(panel.get_by_role("button", name="Restaurar el texto de la planilla modelo"),
                       "Planilla: restoring the model's legal text")
        restore.click()
        restored = legal.input_value() == model_text
        depth = mark(panel.get_by_label("El índice incluye"), "Planilla: the index depth")
        depth.select_option("1")
        depth_now = depth.input_value()
        depth.select_option("2")
        page_field = mark(panel.get_by_label("Página de los datos bibliográficos"), "Planilla: file 2's page")
        page_field.fill("2")
        saved = wait_for(lambda: (draft_view(page, api)["draft"]["state"].get("planilla") or {}).get("file2_page") == "2", 15)
        shown = wait_for(lambda: "p. 2" in tree(page).nth(1).inner_text(), 15)
        page_field.fill("")
        # Every other field: what is typed is saved with the book, under its planilla name.
        fields = {"Número ABSYS": "absys", "Edición y publicación": "edition_line", "Editorial": "publisher_line",
                  "Línea de producción": "production_line", "Fecha de grabación": "production_date", "ISBN": "isbn",
                  "Página del índice": "index_page", "Inicio de nota": "note_open", "Fin de nota": "note_close",
                  "Frase final": "closing"}
        fields["Página del archivo 1"] = "file1_page"
        # Pages are numbers (or roman numerals): the server refuses anything else.
        typed = {key: str(number + 10) if key.endswith("_page") else f"Prueba {number}"
                 for number, key in enumerate(fields.values(), start=1)}
        before = {label: panel.get_by_label(label, exact=False).first.input_value() for label in fields}
        for label, key in fields.items():
            mark(panel.get_by_label(label, exact=False).first, "Planilla: every field is saved").fill(typed[key])
        author = mark(panel.get_by_label("Autor", exact=True), "Planilla: every field is saved")
        author_before = author.input_value()
        author.fill("Autora de prueba")
        sheet = lambda: draft_view(page, api)["draft"]["state"]  # noqa: E731
        all_saved = wait_for(lambda: all((sheet().get("planilla") or {}).get(key) == value for key, value in typed.items())
                             and sheet()["metadata"].get("author") == "Autora de prueba", 20)
        for label, value in before.items():
            panel.get_by_label(label, exact=False).first.fill(value)
        author.fill(author_before)
        switch = mark(panel.get_by_label("Producir con la estructura ONCE"), "Planilla: the ONCE switch locks its fields")
        switch.uncheck()
        locked = panel.locator("fieldset").first.locator("input").first.is_disabled()
        switch.check()
        unlocked = not panel.locator("fieldset").first.locator("input").first.is_disabled()
        results.check("Planilla", "The planilla's fields work: title preview, legal text restore, index depth, "
                                  "a page that reaches the structure, and the ONCE switch",
                      "revisado" in preview_line and restored and depth_now == "1" and bool(saved) and bool(shown)
                      and locked and unlocked and bool(all_saved),
                      f"preview «{preview_line[:60]}»; {len(fields) + 1} other fields saved {bool(all_saved)}; "
                      f"legal text restored {restored}; depth {depth_now}; file 2 on "
                      f"page 2 saved {bool(saved)}, in the tree {bool(shown)}; switch off locks {locked}, on unlocks {unlocked}")
        button(page, "Volver a la estructura").click()

    def once_switch() -> None:
        tree(page).nth(3).click()
        before = tree(page).count()
        box = mark(page.get_by_label("Estructura ONCE (archivos iniciales, índice, notas y frase final)"),
                   "Structure: the ONCE switch")
        box.uncheck()
        without = wait_for(lambda: tree(page).count() == before - 3, 20)
        during = tree(page).count()
        box.check()
        back = wait_for(lambda: tree(page).count() == before, 20)
        results.check("Structure", "The ONCE structure switch removes and restores files 1 to 3", bool(without and back),
                      f"{before} files → without ONCE {during} → {tree(page).count()}")

    def cancels() -> None:
        tree(page).nth(3).click()
        phrases = page.locator("#panel-review .ed-phrase-text").all_inner_texts()
        button(page, "Eliminar la frase 2", exact=True).click()
        mark(page.locator(".ed-confirm").get_by_role("button", name="Cancelar"), "Editing: every «Cancelar»")
        page.locator(".ed-confirm").get_by_role("button", name="Cancelar").click()
        button(page, "Corregir la frase 2").click()
        mark(page.locator(".ed-edit textarea"), "Editing: every «Cancelar»")
        page.locator(".ed-edit textarea").fill("Esto no se guarda.")
        page.locator(".ed-edit").get_by_role("button", name="Cancelar").click()
        unchanged = page.locator("#panel-review .ed-phrase-text").all_inner_texts() == phrases
        button(page, "Eliminar capítulo").click()
        page.locator(".ed-confirm").get_by_role("button", name="Cancelar").click()
        results.check("Editing", "«Cancelar» after delete phrase, correct phrase and delete chapter changes nothing",
                      unchanged and section_title(page) == CHAPTERS[0][0] and tree(page).count() == 7,
                      f"phrases unchanged {unchanged}; «{section_title(page)}» still open; {tree(page).count()} files")

    def chapter_from_file() -> None:
        text_file = out / "capitulo.txt"
        text_file.write_text("Un capítulo leído de un archivo.\n\nCon dos párrafos.", encoding="utf-8")
        button(page, "Añadir capítulo después").click()
        field = mark(page.locator(".ed-add-file input[type=file]"), "Editing: a chapter's text from a .txt file")
        field.set_input_files(str(text_file))
        # With no title typed, the file's first line is the title and the rest its text.
        filled = wait_for(lambda: page.locator("#ed-add-name").input_value() == "Un capítulo leído de un archivo."
                          and page.locator("#ed-add-text").input_value() == "Con dos párrafos.", 10)
        see(page)
        page.locator(".ed-add").get_by_role("button", name="Cancelar").click()
        results.check("Editing", "A new chapter is imported from a .txt file (first line the title); «Cancelar» adds nothing",
                      bool(filled) and page.locator(".ed-add").count() == 0 and tree(page).count() == 7,
                      f"title and text imported {bool(filled)}; form closed; {tree(page).count()} files")

    def phrase_to_misread() -> None:
        tree(page).nth(4).click()
        count = page.locator("#panel-review .ed-phrase-text").count()
        button(page, f"Añadir una frase después de la frase {count}").click()
        page.locator(".ed-edit textarea").fill(MISREAD)
        button(page, "Añadir la frase").click()
        wait_saved(page)
        results.check("Editing", "A phrase is added for the voice to misread (setup for the decisions)",
                      MISREAD in page.locator("#panel-review .ed-phrase-text").all_inner_texts(),
                      f"«{MISREAD}» after phrase {count} of «{section_title(page)}»")

    for check in (initial_group, phrase_select, planilla_from_file, planilla_fields, once_switch, cancels,
                  chapter_from_file, phrase_to_misread):
        attempt(results, "Step 2", check)
    see(page)


def produce_from_step2(page: Page, api: str, results: Results) -> str:
    tree(page).nth(3).click()
    produce = mark(button(page, "Producir el audiolibro con revisión"), "Production: started from step 2")
    produce.click()
    job_id = wait_for(lambda: last_job(page, api), 60)
    finished = wait_for(lambda: job(api, job_id)["state"] in {"completed", "partially_completed", "failed"}, 900, 2)
    state = job(api, job_id)["state"]
    waiting = [unit["text"] for unit in job(api, job_id)["sentences"] if unit["state"] != "verified"]
    results.check("Production", "«Producir el audiolibro con revisión» produces the book from step 2",
                  bool(finished) and state in {"completed", "partially_completed"},
                  f"job {job_id}: {state}; awaiting a decision: {waiting}")
    page.wait_for_function("() => { const b = document.querySelector('#panel-review .ed-play'); return b && !b.disabled }",
                           timeout=60_000)
    return job_id


# --------------------------------------------------------------------------- #
# A phrase the voice misread
# --------------------------------------------------------------------------- #

def decisions(page: Page, api: str, job_id: str, results: Results) -> None:
    flagged = [unit for unit in job(api, job_id)["sentences"] if unit["state"] in {"needs_human_review", "unresolved"}]
    if not flagged or "catalejo" not in flagged[0]["text"]:
        results.check("Review", "The misread phrase waits for a person's decision", False,
                      f"awaiting: {[unit['text'] for unit in flagged]}: start e2e_server --mishear catalejo=sombrero")
        return
    target = flagged[0]

    def problem_navigation() -> None:
        tree(page).nth(3).click()
        label = page.locator(".ed-problems").inner_text()
        following = mark(page.get_by_role("button", name="Problema siguiente"), "Review: the problem navigation")
        following.click()
        found = (section_title(page), selected_text(page))
        previous = mark(page.get_by_role("button", name="Problema anterior"), "Review: the problem navigation")
        previous.click()
        again = selected_text(page)
        results.check("Review", "«Problema siguiente» and «Problema anterior» go to the phrase awaiting a decision",
                      "1 por revisar" in label and found == (CHAPTERS[1][0], MISREAD) and again == MISREAD,
                      f"«{label}»; next: «{found[1]}» in «{found[0]}»; previous: «{again}»")

    def awaiting_in_step3() -> None:
        page.locator("#tab-export").click()
        heading = page.locator("#xp-review-title")
        listed = heading.inner_text() if heading.count() else ""
        link = mark(page.locator("#panel-export .xp-link").first, "Review: step 3 lists the phrases awaiting a decision")
        see(page)
        link.click()
        back = page.locator("#tab-review").get_attribute("aria-selected") == "true"
        results.check("Review", "Step 3 lists the phrase awaiting a decision, and its link opens it in step 2",
                      "(1)" in listed and back and selected_text(page) == MISREAD,
                      f"«{listed}»; back in step 2 {back} on «{selected_text(page)}»")

    def comparison() -> None:
        compare = page.locator("#panel-review .qa-compare").inner_text().replace("\n", " ")
        see(page)
        results.check("Review", "The phrase shows the book's text beside what the audio says",
                      "catalejo" in compare and "sombrero" in compare, compare[:160])
        seek = page.locator("#panel-review .ed-inspector .qa-seek")
        if not seek.count():
            not_run(results, "Review", "«Escuchar desde ahí» plays from the difference",
                    "not offered: the offline recognizer gives no time for a difference")
            return
        mark(seek, "Review: «Escuchar desde ahí»")
        seek.first.click()
        playing = wait_for(lambda: not editor_audio(page).get("paused", True), 10)
        pause(page)
        results.check("Review", "«Escuchar desde ahí» plays from the difference", bool(playing), str(editor_audio(page)))

    def accept() -> None:
        correct = mark(button(page, "La locución es correcta"), "Review: «La locución es correcta»")
        correct.click()
        verified = wait_for(lambda: next(unit for unit in job(api, job_id)["sentences"]
                                         if unit["sentence_id"] == target["sentence_id"])["state"] == "verified", 60)
        completed = wait_for(lambda: job(api, job_id)["state"] == "completed", 600, 2)
        unit = next(item for item in job(api, job_id)["sentences"] if item["sentence_id"] == target["sentence_id"])
        results.check("Review", "«La locución es correcta» accepts the phrase, and the book is then assembled",
                      bool(verified) and bool(completed) and unit.get("verification") == "human",
                      f"phrase {unit['state']} ({unit.get('verification')}); job {job(api, job_id)['state']}")

    def where_differences_are_heard() -> None:
        page.locator("#tab-export").click()
        details = page.locator("#panel-export details").filter(has_text="Dónde se oyen las diferencias")
        if not wait_for(lambda: details.count() == 1, 30):
            not_run(results, "Quality", "«Dónde se oyen las diferencias» plays a difference",
                    "the finished job lists no difference to place")
            page.locator("#tab-review").click()
            return
        open_details(details)
        see(page)
        listen = mark(details.locator(".fault-locations button.qa-seek").first, "Quality: where the differences are heard")
        listen.click()
        player = page.locator(f"[id='fault-player-{job_id}']")
        playing = wait_for(lambda: player.count() == 1 and player.evaluate("a => a.currentTime > 0.2 && !a.paused"), 15)
        row = details.locator("tbody tr").first.inner_text().replace("\n", " ").replace("\t", " ")
        if player.count():
            player.evaluate("a => a.pause()")
        results.check("Quality", "«Dónde se oyen las diferencias» places the difference and plays it", bool(playing),
                      row[:160])
        page.locator("#tab-review").click()

    for check in (problem_navigation, awaiting_in_step3, comparison, accept, where_differences_are_heard):
        attempt(results, "Decisions", check)


# --------------------------------------------------------------------------- #
# Step 2 with the book produced
# --------------------------------------------------------------------------- #

def step2_produced(page: Page, api: str, job_id: str, results: Results) -> None:
    page.wait_for_function("() => !document.querySelector('#panel-review .ed-progress')", timeout=60_000)
    tree(page).nth(3).click()
    see(page)

    def from_start() -> None:
        listen = mark(button(page, "Escuchar desde el principio"), "Playback: from the start")
        listen.click()
        playing = wait_for(lambda: editor_audio(page).get("time", 0) > 0.3 and not editor_audio(page).get("paused"), 10)
        state = editor_audio(page)
        mark(page.locator("#panel-review .ed-play"), "Playback: play and pause")
        page.locator("#panel-review .ed-play").click()
        paused = wait_for(lambda: editor_audio(page).get("paused"), 5)
        results.check("Playback", "«Escuchar desde el principio» plays the chapter from its start; «Pausar» stops it",
                      bool(playing) and bool(paused) and "/sections/" in state.get("src", ""),
                      f"playing at {state.get('time', 0):.1f} s of {state.get('src', '')[-48:]}; paused {bool(paused)}")

    def phrase_play() -> None:
        structure = httpx.get(f"{api}/api/jobs/{job_id}/structure").json()
        clip = structure["sections"][3]["units"][2].get("clip") or [0, 0]
        listen = mark(button(page, "Escuchar la frase 3"), "Playback: from one phrase")
        listen.click()
        reached = wait_for(lambda: editor_audio(page).get("time", 0) >= clip[0] - 0.05, 10)
        state = editor_audio(page)
        pause(page)
        results.check("Playback", "«Escuchar la frase 3» plays from that phrase's place in the chapter",
                      bool(reached) and clip[0] - 0.05 <= state.get("time", 0) < clip[1] + 0.5,
                      f"phrase 3 is {clip[0]:.2f}–{clip[1]:.2f} s; playing at {state.get('time', 0):.2f} s")

    def phrase_steps() -> None:
        pause(page)
        page.locator("#panel-review .ed-phrase-main").nth(1).click()
        before = selected_number(page)
        mark(button(page, "Frase siguiente"), "Navigation: previous and next phrase").click()
        after_next = selected_number(page)
        mark(button(page, "Frase anterior"), "Navigation: previous and next phrase").click()
        after_previous = selected_number(page)
        results.check("Navigation", "«Frase siguiente» and «Frase anterior» move the selection by one phrase",
                      (before, after_next, after_previous) == ("2", "3", "2"),
                      f"{before} → next {after_next} → previous {after_previous}")

    def section_steps() -> None:
        before = section_title(page)
        mark(button(page, "Sección siguiente"), "Navigation: previous and next section").click()
        after_next = section_title(page)
        mark(button(page, "Sección anterior"), "Navigation: previous and next section").click()
        after_previous = section_title(page)
        results.check("Navigation", "«Sección siguiente» and «Sección anterior» open the next and previous chapter",
                      (before, after_next, after_previous) == (CHAPTERS[0][0], CHAPTERS[1][0], CHAPTERS[0][0]),
                      f"«{before}» → «{after_next}» → «{after_previous}»")

    def seek_and_speed() -> None:
        slider = mark(page.locator("#panel-review input.ed-seek"), "Playback: the position slider")
        slider.evaluate("""element => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            set.call(element, '4'); element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true })) }""")
        moved = wait_for(lambda: abs(editor_audio(page).get("time", 0) - 4) < 0.6, 10)
        position = editor_audio(page).get("time", 0)
        speed = mark(page.locator("#panel-review .ed-speed select"), "Playback: the speed")
        speed.select_option("1.5")
        rate = editor_audio(page).get("rate")
        speed.select_option("1")
        results.check("Playback", "The position slider moves the audio; «Velocidad» changes its rate",
                      bool(moved) and rate == 1.5, f"slider to 4 s → audio at {position:.2f} s; speed 1,5× → rate {rate}")

    def nothing_to_review() -> None:
        previous = page.get_by_role("button", name="Problema anterior")
        following = page.get_by_role("button", name="Problema siguiente")
        label = page.locator(".ed-problems").inner_text()
        results.check("Navigation", "With nothing left to review, the problem buttons are off and say so",
                      previous.is_disabled() and following.is_disabled() and "Nada por revisar" in label,
                      f"«{label}»; previous disabled {previous.is_disabled()}, next disabled {following.is_disabled()}")

    def unit_by_text(text: str) -> dict:
        return next(unit for unit in job(api, job_id)["sentences"] if unit["text"] == text)

    def attempts(sentence_id: str) -> int:
        return httpx.get(f"{api}/api/jobs/{job_id}/attempts", params={"sentence_id": sentence_id}).json()["total"]

    def renarrate() -> None:
        tree(page).nth(3).click()
        page.locator("#panel-review .ed-phrase-main").nth(1).click()
        unit = unit_by_text(CHAPTERS[0][1][0])
        before = attempts(unit["sentence_id"])
        change = page.locator("#panel-review .ed-change")
        mark(change.locator("summary"), "Review: «Cambiar esta frase»")
        open_details(change)
        see(page)
        again = mark(button(page, "Volver a narrarla"), "Review: «Volver a narrarla»")
        again.click()
        narrated = wait_for(lambda: attempts(unit["sentence_id"]) > before, 120)
        checked = wait_for(lambda: unit_by_text(CHAPTERS[0][1][0])["state"] == "verified", 120)
        fresh = wait_for(lambda: page.locator("#panel-review .qa-audio audio").count() == 1, 30)
        results.check("Review", "«Volver a narrarla» narrates the phrase again, checks it and lets you hear it",
                      bool(narrated) and bool(checked) and bool(fresh),
                      f"attempts {before} → {attempts(unit['sentence_id'])}; state {unit_by_text(CHAPTERS[0][1][0])['state']}; "
                      f"«Escucha la nueva locución» shown {bool(fresh)}")

    def correct() -> None:
        page.locator("#panel-review .ed-phrase-main").nth(1).click()
        open_details(page.locator("#panel-review .ed-change"))
        new_text = "Lucía llegó al faro cuando caía la tarde de otoño."
        field = mark(page.locator("#panel-review .qa-correction textarea"), "Review: «Corregir el texto y narrarla»")
        field.fill(new_text)
        fix = mark(button(page, "Corregir el texto y narrarla"), "Review: «Corregir el texto y narrarla»")
        fix.click()
        stored = wait_for(lambda: any(unit["text"] == new_text and unit["state"] == "verified"
                                      for unit in job(api, job_id)["sentences"]), 120)
        shown = wait_for(lambda: new_text in page.locator("#panel-review .ed-phrase-text").all_inner_texts(), 30)
        results.check("Review", "«Corregir el texto y narrarla» replaces the phrase's text and narrates it",
                      bool(stored and shown), f"server has the new text, verified: {bool(stored)}; page shows it: {bool(shown)}")

    def pronunciation() -> None:
        tree(page).nth(4).click()
        index = page.locator("#panel-review .ed-phrase-text").all_inner_texts().index(MISREAD)
        page.locator("#panel-review .ed-phrase-main").nth(index).click()
        open_details(page.locator("#panel-review .ed-change"))
        editor = page.locator("#panel-review .pron-editor")
        mark(editor.locator("summary"), "Pronunciation: the editor")
        open_details(editor)
        word = mark(editor.locator(".pron-word").filter(has_text="Whitby"), "Pronunciation: a word is analysed")
        word.click()
        page.wait_for_selector("#panel-review .pron-result", timeout=30_000)
        facts = editor.locator(".pron-facts").inner_text().replace("\n", " ")
        see(page)
        player = editor.locator(".pron-player")
        suggestions = editor.locator(".pron-options li").filter(has_not=page.locator(".pron-custom"))
        heard = saved = None
        if suggestions.count():
            first = suggestions.first
            mark(first.get_by_role("button", name="Escuchar"), "Pronunciation: a suggested spelling is heard")
            first.get_by_role("button", name="Escuchar").click()
            heard = wait_for(lambda: bool(player.evaluate("a => a.src")), 60)
            mark(first.get_by_role("button", name="Usar esta"), "Pronunciation: a suggested spelling is saved")
            first.get_by_role("button", name="Usar esta").click()
            saved = wait_for(lambda: "Whitby" in httpx.get(f"{api}/api/pronunciation-lexicon").json()["entries"], 30)
        custom = mark(editor.locator(".pron-custom input"), "Pronunciation: your own spelling")
        custom.fill("Uítbi")
        row = editor.locator(".pron-options li").filter(has=page.locator(".pron-custom"))
        before_src = player.evaluate("a => a.src")
        row.get_by_role("button", name="Escuchar").click()
        custom_heard = wait_for(lambda: player.evaluate("a => a.src") not in ("", before_src), 60)
        row.get_by_role("button", name="Usar esta").click()
        custom_saved = wait_for(lambda: httpx.get(f"{api}/api/pronunciation-lexicon").json()["entries"].get("Whitby")
                                == "Uítbi", 30)
        ticked = wait_for(lambda: "✓" in editor.locator(".pron-word").filter(has_text="Whitby").inner_text(), 10)
        # A saved spelling changes what is narrated: later productions would
        # read this phrase anew (and the offline voice misreads it again).
        httpx.delete(f"{api}/api/pronunciation-lexicon/entries/Whitby")
        results.check("Pronunciation", "A word is analysed; its suggested and own spellings are heard and saved",
                      bool(custom_heard) and bool(custom_saved) and bool(ticked)
                      and (heard is None or (bool(heard) and bool(saved))),
                      f"«Whitby»: {facts[:70]}; suggestion heard {heard}, saved {saved}; own «Uítbi» heard "
                      f"{bool(custom_heard)}, saved {bool(custom_saved)}; ✓ on the word {bool(ticked)}")

    def edit_and_back() -> None:
        edit = mark(button(page, "Editar el texto"), "Editing: «Editar el texto» and back")
        edit.click()
        back_button = mark(button(page, "Volver al audio producido"), "Editing: «Editar el texto» and back")
        editing = back_button.is_visible() and "Producir con los cambios" in page.locator(
            "#panel-review .ed-produce-button").inner_text()
        back_button.click()
        back = button(page, "Editar el texto").is_visible()
        results.check("Editing", "«Editar el texto» opens the text; «Volver al audio producido» returns to the audio",
                      editing and back, f"editing {editing}; back to the produced audio {back}")

    def produce_changes() -> None:
        button(page, "Editar el texto").click()
        see(page)
        tree(page).nth(4).click()
        button(page, "Corregir la frase 2").click()
        new_text = "Cada noche anotaba el viento, la lluvia y los barcos."
        page.locator(".ed-edit textarea").fill(new_text)
        button(page, "Guardar la corrección").click()
        wait_saved(page)
        produce = mark(button(page, "Producir con los cambios"), "Production: «Producir con los cambios»")
        produce.click()
        new_job = wait_for(lambda: last_job(page, api) if last_job(page, api) not in ("", job_id) else "", 60)
        finished = wait_for(lambda: job(api, new_job)["state"] == "completed", 900, 2)
        texts = [unit["text"] for unit in job(api, new_job)["sentences"]]
        results.check("Production", "«Producir con los cambios» produces the edited text",
                      bool(finished) and new_text in texts,
                      f"job {new_job}: {job(api, new_job)['state']}; edited phrase in it: {new_text in texts}")

    for check in (from_start, phrase_play, phrase_steps, section_steps, seek_and_speed, nothing_to_review, renarrate,
                  correct, pronunciation, edit_and_back, produce_changes):
        attempt(results, "Step 2", check)
    see(page)


# --------------------------------------------------------------------------- #
# Step 3
# --------------------------------------------------------------------------- #

def step3(page: Page, api: str, out: Path, results: Results) -> None:
    page.locator("#tab-export").click()
    page.wait_for_function("() => !document.querySelector('#panel-export .xp-status.is-busy')", timeout=60_000)
    see(page)

    def book_data() -> None:
        details = page.locator("#panel-export details").filter(has_text="Datos del libro")
        mark(details.locator("summary"), "Book data: title, author, edition and year")
        open_details(details)
        see(page)
        values = {"#metadata-title": "El faro de prueba revisado", "#metadata-author": "Equipo de pruebas Soni2",
                  "#metadata-edition": "Segunda edición", "#metadata-year": "2025"}
        for selector, value in values.items():
            mark(page.locator(selector), "Book data: title, author, edition and year").fill(value)
        changed = wait_for(lambda: "Has cambiado el libro" in status_text(page), 20)
        page.locator('#panel-export [data-primary-action="regenerate"]').click()
        final = wait_build(page)
        with page.expect_download(timeout=120_000) as download:
            page.locator('#panel-export [data-primary-action="download"]').click()
        target = out / "book-data.zip"
        download.value.save_as(target)
        with zipfile.ZipFile(target) as archive:
            opf = next(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".opf"))
        found = {"title": "<dc:Title>El faro de prueba revisado</dc:Title>" in opf,
                 "author": "<dc:Creator>Equipo de pruebas Soni2</dc:Creator>" in opf,
                 "edition": 'name="dtb:sourceEdition" content="Segunda edición"' in opf,
                 "year": 'name="dtb:sourceDate" content="2025"' in opf}
        results.check("Book data", "Title, author, edition and year each reach the regenerated ZIP's metadata",
                      bool(changed) and "ZIP listo" in final and all(found.values()),
                      f"status changed {bool(changed)}; {final[:40]}; OPF {found}")

    def fallback_link() -> None:
        link = page.get_by_role("link", name="Si la descarga no empieza, pulsa aquí.")
        if not link.count():
            with page.expect_download(timeout=120_000):
                page.locator('#panel-export [data-primary-action="download"]').click()
        mark(link, "Download: the fallback link")
        with page.expect_download(timeout=120_000) as download:
            link.click()
        target = out / "fallback-link.zip"
        download.value.save_as(target)
        results.check("Download", "«Si la descarga no empieza, pulsa aquí» downloads the ZIP too",
                      zipfile.is_zipfile(target), f"{download.value.suggested_filename}, {target.stat().st_size // 1024} KB")

    def limits() -> None:
        details = page.locator("#panel-export details.release-uncertainty")
        mark(details.locator("summary"), "Quality: the checks' limits")
        open_details(details)
        notes = details.locator("li").count()
        results.check("Quality", "«Lo que estas comprobaciones no demuestran» opens its explanation",
                      details.evaluate("node => node.open") and notes > 0,
                      f"{notes} limits listed: «{details.locator('li').first.inner_text()[:90]}…»")

    def publish() -> None:
        panel = page.locator("#panel-export .release-panel")
        state = page.locator("#release-title").inner_text()
        name = panel.get_by_label("Tu nombre (queda registrado con la decisión)")
        publish_button = panel.get_by_role("button", name="Publicar el audiolibro")
        if not name.count() or not publish_button.count():
            release = httpx.get(f"{api}/api/jobs/{last_job(page, api)}/release").json()
            results.check("Quality", "Publishing needs a name and the acknowledgement, then records the release", False,
                          f"«{state}»: not offered; blockers {release.get('blockers')}; reviews {release.get('reviews')}")
            return
        mark(publish_button, "Quality: publishing")
        mark(name, "Quality: publishing")
        box = mark(panel.locator(".release-action input[type=checkbox]"), "Quality: publishing")
        locked = publish_button.is_disabled()
        name.fill("Revisora de pruebas")
        half = publish_button.is_disabled()
        box.check()
        unlocked = not publish_button.is_disabled()
        see(page)
        publish_button.click()
        job_id = last_job(page, api)
        released = wait_for(lambda: httpx.get(f"{api}/api/jobs/{job_id}/release").json().get("state") == "RELEASED", 30)
        record = httpx.get(f"{api}/api/jobs/{job_id}/release").json().get("released") or {}
        shown = wait_for(lambda: "Publicado por Revisora de pruebas" in panel.inner_text(), 15)
        results.check("Quality", "Publishing needs a name and the acknowledgement, then records the release",
                      locked and half and unlocked and bool(released) and bool(shown),
                      f"«{state}»; locked with nothing {locked}, with a name only {half}, unlocked with both {unlocked}; "
                      f"released by {record.get('by')} at {record.get('at')}; shown on the page {bool(shown)}")

    for check in (book_data, fallback_link, limits, publish):
        attempt(results, "Step 3", check)
    see(page)


# --------------------------------------------------------------------------- #
# Other books
# --------------------------------------------------------------------------- #

def other_books(page: Page, base: str, api: str, out: Path, planilla_docx: Path | None, results: Results) -> None:
    def open_book(path: Path) -> None:
        page.goto(base)
        page.evaluate("() => window.localStorage.clear()")
        page.goto(base)
        page.set_input_files(UPLOAD, str(path))
        page.wait_for_selector(".narration-outline ul li", timeout=60_000)

    def subchapters() -> None:
        open_book(SUBCHAPTERS_BOOK)
        button(page, "Ver la estructura del audiolibro (sin generar muestra)").click()
        page.wait_for_selector('[role="treeitem"]')
        parent = page.locator('[role="treeitem"][aria-expanded]').first
        arrow = mark(parent.locator(".ed-twisty"), "Structure: a chapter's subchapters fold and unfold")
        see(page)
        before = tree(page).count()
        arrow.click()
        folded, state = tree(page).count(), parent.get_attribute("aria-expanded")
        arrow.click()
        results.check("Structure", "A chapter's arrow folds and unfolds its subchapters",
                      folded < before and state == "false" and tree(page).count() == before,
                      f"{before} files → folded {folded} (aria-expanded {state}) → {tree(page).count()}")

    def import_planilla() -> None:
        if planilla_docx is None or not planilla_docx.is_file():
            not_run(results, "Planilla", "«Importar planilla (.docx)» reads ONCE's planilla", "no planilla .docx given")
            return
        tree(page).nth(3).click()
        button(page, "Revisar la planilla de producción").click()
        field = mark(page.locator(".planilla-import input[type=file]"), "Planilla: imported from Word")
        field.set_input_files(str(planilla_docx))
        notice = page.locator(".planilla-panel .ed-notice")
        read = wait_for(lambda: notice.count() == 1 and "leída" in notice.inner_text(), 30)
        legal = page.locator(".planilla-panel").get_by_label("Texto legal de la ONCE").input_value()
        saved = wait_for(lambda: bool(draft_view(page, api)["draft"]["state"].get("planilla")), 15)
        stored = draft_view(page, api)["draft"]["state"].get("planilla") or {}
        results.check("Planilla", "«Importar planilla (.docx)» reads ONCE's planilla into the form and saves it",
                      bool(read) and "ONCE" in legal and bool(saved),
                      f"«{notice.inner_text() if notice.count() else ''}»; legal text «{legal[:50]}…»; "
                      f"saved fields {sorted(stored)[:8]}")
        button(page, "Volver a la estructura").click()

    def navigation_sections() -> None:
        text = "\n\n".join(f"Párrafo {number} de un texto sin capítulos, con palabras suficientes para que el libro "
                           "pida secciones de navegación y se pueda oír por partes sin perderse en él."
                           for number in range(1, 25))
        book = out / "books" / "sin-capitulos.txt"
        book.write_text(text, encoding="utf-8")
        open_book(book)
        button(page, "Ver la estructura del audiolibro (sin generar muestra)").click()
        page.wait_for_selector('[role="treeitem"]')
        before = tree(page).count()
        page.locator("#tab-export").click()
        details = page.locator("#panel-export details").filter(has_text="Opciones de entrega")
        open_details(details)
        box = mark(details.get_by_label("Dividir el libro en secciones de navegación"),
                   "Delivery: navigation sections for a book without chapters")
        see(page)
        box.check()
        page.locator("#tab-review").click()
        split = wait_for(lambda: tree(page).count() > before, 20)
        files = tree(page).all_inner_texts()
        page.locator("#tab-export").click()
        box.uncheck()
        page.locator("#tab-review").click()
        joined = wait_for(lambda: tree(page).count() == before, 20)
        results.check("Delivery", "«Dividir el libro en secciones de navegación» splits a book without chapters, "
                                  "and unticking it joins it again",
                      bool(split) and bool(joined),
                      f"{before} files → split {len(files)}: {[row.splitlines()[1] for row in files[3:]]} → "
                      f"{tree(page).count()}")

    for check in (subchapters, import_planilla, navigation_sections):
        attempt(results, "Other books", check)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def save(out: Path, results: Results) -> dict:
    (out / "buttons.json").write_text(json.dumps(results.rows, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for key in sorted(set(SEEN) | set(COVERAGE)):
        by = COVERAGE.get(key) or (f"journey (tools.e2e_ui): {JOURNEY[key]}" if key in JOURNEY else "")
        rows.append({"control": key, "seen_in": SEEN.get(key, ""), "pressed_by": by})
    gaps = [row["control"] for row in rows if not row["pressed_by"]]
    lines = ["| Feature | Test | Result | Evidence |", "|---|---|---|---|"]
    for row in results.rows:
        lines.append(f"| {row['feature']} | {row['test']} | {row['result']} | "
                     f"{row['evidence'].replace('|', '/').replace(chr(10), ' ')} |")
    lines += ["", f"## Controls: {len(rows)} seen, {len(rows) - len(gaps)} pressed", "",
              "| Control | Seen in | Pressed by |", "|---|---|---|"]
    lines += [f"| {row['control'].replace('|', '·')} | {row['seen_in']} | {row['pressed_by'] or '**not pressed**'} |"
              for row in rows]
    (out / "buttons.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "buttons-coverage.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"controls": len(rows), "gaps": gaps}


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5181")
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    parser.add_argument("--out", required=True)
    parser.add_argument("--planilla", default="", help="a planilla .docx to import (MODELO ONCE.docx)")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    (out / "books").mkdir(parents=True, exist_ok=True)
    for name, make in BOOKS.items():
        (out / "books" / name).write_bytes(make())
    results = Results(out)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=not args.headed,
                                             args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="es-ES")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            step1(page, args.base, args.api, out, results)
            step2_outline(page, args.api, out, results)
            job_id = produce_from_step2(page, args.api, results)
            decisions(page, args.api, job_id, results)
            step2_produced(page, args.api, job_id, results)
            step3(page, args.api, out, results)
            other_books(page, args.base, args.api, out, Path(args.planilla) if args.planilla else None, results)
        except Exception as exc:  # recorded, never hidden
            results.check("Run", "The button run reached its end", False, f"{type(exc).__name__}: {exc}"[:400])
            traceback.print_exc()
            page.screenshot(path=str(out / "buttons-failure.png"), full_page=True)
        results.check("Stability", "No uncaught error in the page", not errors, "; ".join(errors)[:300] or "none")
        browser.close()
    summary = save(out, results)
    passed = sum(row["result"] == "PASS" for row in results.rows)
    skipped = sum(row["result"] == "NOT RUN" for row in results.rows)
    print(f"\n{passed}/{len(results.rows)} checks passed ({skipped} not run); {summary['controls']} controls, "
          f"{len(summary['gaps'])} pressed by neither run: {summary['gaps']}\n{out / 'buttons.md'}")


if __name__ == "__main__":
    main_cli()
