import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { NO_EDITS, deleteSection, deleteUnit, editUnit, insertAfter, sectionFamily, type EditableSection } from './bookEdits'
import { draftResponse, forgetDraft, lastSavedState, rememberJob, savedStates } from './test/draftApi'

// Step 2: correcting, adding and deleting phrases and chapters. A change is
// saved as it is made, survives a reload, and is what production narrates.

let root: Root

const capabilities = {
  daisy_profile: 'DAISY 3', tts_provider: 'elevenlabs', asr_provider: 'deepgram', tts_configured: true,
  voice: 'v-es', voice_name: 'Gabo', voice_language: 'es-ES', voice_is_peninsular: true, voice_accent_verified: true,
  requires_peninsular_voice: true, voice_speed_control: false, voice_id_configurable: false,
  audio_review_configured: true, offline_mock_mode: false, chapter_audio: true, chapter_smil: true,
  full_text_in_package: true, ncx_navigation: true, ncc_navigation: false, once_intro: false,
  once_bibliographic_audio: false, automatic_audio_review: true, retakes: true, max_quality_retakes: 2,
  max_technical_attempts: 6, sentence_audio_cache: true, official_daisy_pipeline_validation: false,
}
const structure = {
  sections: [{ id: 'section-1', title: 'Uno', paragraph_count: 3 }, { id: 'section-2', title: 'Dos', paragraph_count: 1 }],
  selected_ids: ['section-1', 'section-2'],
}
const chapters = [
  { id: 'section-1', title: 'Uno', paragraphs: ['Frase A.', 'Frase B.', 'Frase C.'] },
  { id: 'section-2', title: 'Dos', paragraphs: ['Frase D.'] },
]
const finishedJob = {
  job_id: 'job-1', state: 'completed', source_name: 'libro.txt', total_sentences: 4, voice_id: 'v-es', voice_name: 'Gabo',
  progress: { verified: 4 }, release: { state: 'PASSED', label: 'Superado', blockers: [], reviews: [], uncertainty: [] },
  sentences: [{ sentence_id: 's1', chapter_index: 0, chapter_title: 'Uno', sequence: 0, text: 'Frase A.', state: 'verified',
                recognized_text: 'Frase A.', issues: [], audio_path: 'a.mp3', quality_retakes: 0, technical_attempts: 1, last_error: '' }],
}

let requests: { url: string; init?: RequestInit }[]
let failSaves = false

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}

function stub() {
  requests = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push({ url, init })
    if (url.endsWith('/api/capabilities')) return json(capabilities)
    if (url.includes('/api/drafts')) {
      if (failSaves && init?.method === 'PUT') return json({ detail: 'Disco lleno.' }, 500)
      const body = draftResponse(url, init, structure, { chapters, title: 'Libro' })
      return body ? json(body) : json({ detail: 'No existe.' }, 404)
    }
    if (url.endsWith('/api/books/jobs')) return json({ job_id: 'job-1', state: 'queued', total_sentences: 4 })
    if (url.includes('/api/jobs/job-1')) return json(finishedJob)
    return json({})
  }))
}

async function settle() {
  for (let round = 0; round < 6; round += 1) {
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)) })
  }
}

async function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
  await settle()
}

async function reload() {
  act(() => root.unmount())
  await render()
}

async function openEditor() {
  await render()
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['texto'], 'libro.txt', { type: 'text/plain' })] })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
  await click('Ver la estructura del audiolibro')
}

function find(label: string) {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].find(item => item.getAttribute('aria-label') === label
    || item.textContent?.includes(label))
}

async function click(label: string) {
  const target = find(label)
  if (!target) throw new Error(`No button «${label}»`)
  await act(async () => { target.click() })
  await settle()
}

async function type(text: string) {
  const area = document.querySelector<HTMLTextAreaElement>('.ed-edit textarea')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(area, text)
    area.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function phrases() {
  return [...document.querySelectorAll('#panel-review .ed-phrase-text')].map(item => item.textContent)
}

function savedEdits() {
  return lastSavedState()?.edits as { slots: Record<string, string[]>; deleted_titles: string[] }
}

beforeEach(() => {
  window.localStorage.clear()
  forgetDraft()
  failSaves = false
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }))
  stub()
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('changing the phrases of a chapter', () => {
  it('shows which phrase is being deleted and deletes it only when confirmed', async () => {
    await openEditor()
    await click('Eliminar la frase 2')
    const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]')!
    expect(dialog.textContent).toContain('¿Eliminar la frase 2 del audiolibro?')
    expect(dialog.querySelector('blockquote')?.textContent).toBe('Frase B.')
    // Cancelling changes nothing.
    await click('Cancelar')
    expect(phrases()).toEqual(['Frase A.', 'Frase B.', 'Frase C.'])
    expect(savedStates().some(state => Object.keys((state.edits as { slots: object }).slots).length)).toBe(false)

    await click('Eliminar la frase 2')
    await click('Eliminar la frase')
    expect(phrases()).toEqual(['Frase A.', 'Frase C.'])
    expect(savedEdits().slots).toEqual({ 'section-1:1': [] })
  })

  it('adds a phrase after the chosen one and corrects another', async () => {
    await openEditor()
    await click('Añadir una frase después de la frase 1')
    await type('Frase nueva.')
    await click('Añadir la frase')
    expect(phrases()).toEqual(['Frase A.', 'Frase nueva.', 'Frase B.', 'Frase C.'])

    await click('Corregir la frase 4')
    expect(document.querySelector<HTMLTextAreaElement>('.ed-edit textarea')?.value).toBe('Frase C.')
    await type('Frase C corregida.')
    await click('Guardar la corrección')
    expect(phrases()).toEqual(['Frase A.', 'Frase nueva.', 'Frase B.', 'Frase C corregida.'])
    expect(savedEdits().slots).toEqual({ 'section-1:0': ['Frase A.', 'Frase nueva.'], 'section-1:2': ['Frase C corregida.'] })
  })

  it('never brings back a deleted phrase when changes follow each other quickly', async () => {
    await openEditor()
    await click('Añadir una frase después de la frase 1')
    await type('Segunda.')
    await click('Añadir la frase')
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(phrases()).toEqual(['Frase B.', 'Frase C.'])
    expect(savedEdits().slots['section-1:0']).toEqual([])
  })

  it('undoes the last change', async () => {
    await openEditor()
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(phrases()).toEqual(['Frase B.', 'Frase C.'])
    await click('Deshacer')
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 650)) })
    await settle()
    expect(phrases()).toEqual(['Frase A.', 'Frase B.', 'Frase C.'])
  })

  it('says when a change is saved and when it could not be', async () => {
    await openEditor()
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(document.querySelector('.ed-save')?.textContent).toContain('Cambios guardados · 1 cambio en el texto')
    failSaves = true
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(document.querySelector('.ed-save')?.textContent).toContain('No se han guardado los cambios: Disco lleno.')
  })
})

describe('deleting a chapter', () => {
  it('asks first, then removes that chapter and leaves the others alone', async () => {
    await openEditor()
    await act(async () => { document.querySelector<HTMLElement>('#ed-tree-1')!.click() })
    await click('Eliminar capítulo')
    const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]')!
    expect(dialog.textContent).toContain('¿Eliminar «Dos» del audiolibro?')
    expect(dialog.textContent).toContain('Se quitan 1 frase')
    await click('Cancelar')
    expect(document.querySelectorAll('[role="treeitem"]')).toHaveLength(2)

    await click('Eliminar capítulo')
    await click('Eliminar el capítulo')
    expect(document.querySelectorAll('[role="treeitem"]')).toHaveLength(1)
    expect(phrases()).toEqual(['Frase A.', 'Frase B.', 'Frase C.'])
    expect(savedEdits()).toMatchObject({ slots: { 'section-2:0': [] }, deleted_titles: ['section-2'] })
    expect(savedEdits().slots['section-1:0']).toBeUndefined()
  })
})

describe('leaving and coming back', () => {
  it('keeps every change across steps and across a reload', async () => {
    await openEditor()
    await click('Eliminar la frase 2')
    await click('Eliminar la frase')
    await act(async () => { document.querySelector<HTMLButtonElement>('#tab-export')!.click() })
    await act(async () => { document.querySelector<HTMLButtonElement>('#tab-review')!.click() })
    expect(phrases()).toEqual(['Frase A.', 'Frase C.'])

    const saves = savedStates().length
    await reload()
    // Back on the same step, with the same text, without uploading anything again.
    expect(document.querySelector('#tab-review')?.getAttribute('aria-selected')).toBe('true')
    expect(phrases()).toEqual(['Frase A.', 'Frase C.'])
    expect(requests.filter(call => call.url.endsWith('/api/drafts') && call.init?.method === 'POST')).toHaveLength(1)
    // Restoring saves nothing, so it cannot overwrite anything either.
    expect(savedStates()).toHaveLength(saves)

    // Editing goes on from where it was left.
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(savedEdits().slots).toEqual({ 'section-1:1': [], 'section-1:0': [] })
  })

  it('says so when the book is no longer on the server', async () => {
    window.localStorage.setItem('soni2-book-draft', 'gone')
    await render()
    expect(document.body.textContent).toContain('ya no está guardado')
    expect(window.localStorage.getItem('soni2-book-draft')).toBeNull()
    expect(document.body.textContent).toContain('Arrastra tu libro aqui')
  })
})

describe('producing the edited book', () => {
  it('saves pending changes first and asks the server for the saved draft', async () => {
    await openEditor()
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    await click('Producir el audiolibro con revisión')
    const job = requests.find(call => call.url.endsWith('/api/books/jobs'))!
    const form = job.init?.body as FormData
    expect(form.get('draft_id')).toBe('draft-1')
    // The text cannot travel with the request: only the saved draft is narrated.
    expect(form.has('file') || form.has('corrections_json') || form.has('edits_json')).toBe(false)
    const lastSave = requests.map(call => call.init?.method).lastIndexOf('PUT')
    expect(lastSave).toBeLessThan(requests.indexOf(job))
  })

  it('does not produce when the changes could not be saved', async () => {
    await openEditor()
    failSaves = true
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    await click('Producir el audiolibro con revisión')
    expect(requests.some(call => call.url.endsWith('/api/books/jobs'))).toBe(false)
    // And a further change waits too, so the unsaved one is not undone by it.
    await click('Eliminar la frase 2')
    await click('Eliminar la frase')
    failSaves = false
    await click('Producir el audiolibro con revisión')
    const produced = requests.filter(call => call.url.endsWith('/api/books/jobs'))
    expect(produced).toHaveLength(1)
    expect(savedEdits().slots).toEqual({ 'section-1:0': [] })
  })

  it('lets a produced book be edited, warns that its ZIP is older, and produces only on request', async () => {
    await openEditor()
    await click('Producir el audiolibro con revisión')
    expect(find('Editar el texto')).toBeTruthy()
    await click('Editar el texto')
    expect(find('Producir con los cambios')).toBeTruthy()
    expect(document.querySelector('.ed-notice')?.textContent).toContain('solo se narran las frases nuevas o corregidas')
    expect(document.querySelector('.production-progress')?.textContent).not.toContain('no incluye tus últimos cambios')

    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    expect(document.querySelector('.production-progress')?.textContent).toContain('no incluye tus últimos cambios')
    // The download of the earlier text is no longer presented as the finished book.
    expect(document.querySelector('.export-actions')?.textContent).toContain('Producir con los cambios')
    expect(document.querySelector('.export-actions')?.textContent).not.toContain('Descargar paquete revisado')
    expect(requests.filter(call => call.url.endsWith('/api/books/jobs'))).toHaveLength(1)

    await click('Producir con los cambios')
    expect(requests.filter(call => call.url.endsWith('/api/books/jobs'))).toHaveLength(2)
  })

  it('brings back a finished production of the same text after a reload', async () => {
    await openEditor()
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    const revision = 1 + savedStates().length
    rememberJob({ job_id: 'job-1', state: 'completed', draft_revision: revision })
    await reload()
    expect(document.querySelector('.production-progress')?.textContent).toContain('Producción del audiolibro')
    expect(document.querySelector('.export-actions')?.textContent).toContain('Descargar paquete revisado')
  })

  it('does not bring back a production of an earlier text', async () => {
    await openEditor()
    await click('Eliminar la frase 1')
    await click('Eliminar la frase')
    rememberJob({ job_id: 'job-1', state: 'completed', draft_revision: 1 })
    await reload()
    expect(document.querySelector('.production-progress')).toBeNull()
    expect(document.querySelector('.export-actions')?.textContent).not.toContain('Descargar paquete revisado')
  })
})

describe('what can be changed', () => {
  const sections: EditableSection[] = [
    { kind: 'once_title', level: 1, title: 'Título', deletable: false, units: [{ role: 'legal', text: 'Legal.' }] },
    { kind: 'chapter', level: 1, title: 'Uno', deletable: true, titleSource: { mode: 'title', section: 'section-1' }, units: [
      { role: 'heading', text: 'Uno', source: { mode: 'title', section: 'section-1', text: 'Uno' } },
      { role: 'text', text: 'Parte uno.', source: { mode: 'units', slot: 'section-1:0', part: 0 } },
      { role: 'text', text: 'Parte dos.', source: { mode: 'units', slot: 'section-1:0', part: 1 } },
      { role: 'note', text: 'Nota 1. Texto. Fin de nota 1.', source: { mode: 'text', slot: 'section-1:1', text: 'Nota 1. Texto.' } },
    ] },
    { kind: 'subchapter', level: 2, title: 'Sub', deletable: true, titleSource: { mode: 'text', slot: 'section-1:2' }, units: [
      { role: 'heading', text: 'Sub', source: { mode: 'text', slot: 'section-1:2', text: 'Sub' } },
      { role: 'text', text: 'Dentro.', source: { mode: 'units', slot: 'section-1:3', part: 0 } },
    ] },
    { kind: 'chapter', level: 1, title: 'Dos', deletable: true, titleSource: { mode: 'title', section: 'section-2' }, units: [
      { role: 'text', text: 'Otro.', source: { mode: 'units', slot: 'section-2:0', part: 0 } },
    ] },
  ]
  const chapter = sections[1]

  it('keeps the untouched phrases of a paragraph byte for byte', () => {
    expect(editUnit(NO_EDITS, sections, chapter.units[2], '  Parte\n dos  nueva. ').slots)
      .toEqual({ 'section-1:0': ['Parte uno.', 'Parte dos nueva.'] })
    expect(deleteUnit(NO_EDITS, sections, chapter.units[1]).slots).toEqual({ 'section-1:0': ['Parte dos.'] })
    expect(insertAfter(NO_EDITS, sections, chapter.units[1], 'Nueva.').slots)
      .toEqual({ 'section-1:0': ['Parte uno.', 'Nueva.', 'Parte dos.'] })
  })

  it('edits a note as a whole, and adds after it without touching it', () => {
    const note = chapter.units[3]
    expect(editUnit(NO_EDITS, sections, note, 'Otra nota.').slots).toEqual({ 'section-1:1': 'Otra nota.' })
    expect(deleteUnit(NO_EDITS, sections, note).slots).toEqual({ 'section-1:1': [] })
    expect(insertAfter(NO_EDITS, sections, note, 'Después.').slots).toEqual({ 'section-1:1+': ['Después.'] })
  })

  it('takes a title out of the narration and adds before the first paragraph', () => {
    const heading = chapter.units[0]
    expect(deleteUnit(NO_EDITS, sections, heading).deleted_titles).toEqual(['section-1'])
    expect(editUnit(NO_EDITS, sections, heading, 'Capítulo uno').titles).toEqual({ 'section-1': 'Capítulo uno' })
    expect(insertAfter(NO_EDITS, sections, heading, 'Antes.').slots).toEqual({ 'section-1:-1+': ['Antes.'] })
  })

  it('deletes a chapter with its subchapters, and nothing else', () => {
    expect(sectionFamily(sections, 1)).toEqual([1, 2])
    expect(sectionFamily(sections, 3)).toEqual([3])
    const edited = deleteSection(NO_EDITS, sections, 1)
    expect(edited.slots).toEqual({ 'section-1:0': [], 'section-1:1': [], 'section-1:2': [], 'section-1:3': [] })
    expect(edited.deleted_titles).toEqual(['section-1'])
    expect(deleteSection(NO_EDITS, sections, 2).slots).toEqual({ 'section-1:2': [], 'section-1:3': [] })
    expect(deleteSection(NO_EDITS, sections, 2).deleted_titles).toEqual([])
  })

  it('refuses to delete an initial ONCE file', () => {
    expect(deleteSection(NO_EDITS, sections, 0)).toEqual(NO_EDITS)
  })
})
