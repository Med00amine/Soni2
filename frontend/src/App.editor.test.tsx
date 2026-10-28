import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// Step 2, the audiobook editor: the ONCE structure, its planilla, playback
// that follows the text phrase by phrase, and review from problem to problem.

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
  sections: [{ id: 'section-1', title: 'Capítulo 1. La llegada', category: 'main', paragraph_count: 3, included: true, needs_review: false, reason: '' }],
  selected_ids: ['section-1'],
}
const LEGAL = 'Esta adaptación es propiedad de la Organización Nacional de Ciegos Españoles (ONCE).'
const CLOSING = 'Aquí finaliza la grabación de El faro de la isla Almendra, De Antonio Pérez.'

function unit(role: string, text: string, extra: Record<string, unknown> = {}) {
  return { role, text, page_breaks: [], note: null, source: null, ...extra }
}

const outline = {
  once: true, title: 'El faro de la isla Almendra', author: 'Antonio Pérez', edition: '', publication_year: '2026',
  publisher: 'Editorial Crítica',
  chapters: [
    { title: 'Capítulo 1. La llegada', paragraphs: ['La barca dejó a Clara en el muelle.', 'El inventario', 'Contó tres mantas.'] },
    { title: 'Capítulo 2. La tormenta', paragraphs: ['La luz parpadeó.', 'Nota 1. Los faros se encendían a mano.'] },
  ],
  sections: [
    { index: 0, key: 'once-title', kind: 'once_title', level: 1, title: 'El faro de la isla Almendra, De Antonio Pérez',
      heading_spoken: true, page_start: '1', page_end: '1',
      units: [unit('heading', 'El faro de la isla Almendra, De Antonio Pérez', { page_breaks: ['1'] }), unit('legal', LEGAL)] },
    { index: 1, key: 'once-bibliographic', kind: 'once_bibliographic', level: 1, title: 'DATOS BIBLIOGRÁFICOS',
      heading_spoken: true, page_start: '2', page_end: '2',
      units: [unit('heading', 'DATOS BIBLIOGRÁFICOS', { page_breaks: ['2'] }), unit('bibliographic', 'EDITORIAL CRÍTICA')] },
    { index: 2, key: 'once-index', kind: 'once_index', level: 1, title: 'Índice.', heading_spoken: true,
      page_start: '3', page_end: '3',
      units: [unit('heading', 'Índice.', { page_breaks: ['3'] }), unit('index_entry', 'Capítulo 1. La llegada. Página 4.'),
              unit('index_entry', 'Fin de índice.')] },
    { index: 3, key: '004-s0', kind: 'chapter', level: 1, title: 'Capítulo 1. La llegada', heading_spoken: true,
      page_start: '4', page_end: '4',
      units: [unit('heading', 'Capítulo 1. La llegada', { page_breaks: ['4'] }),
              unit('text', 'La barca dejó a Clara en el muelle.', { source: { chapter: 0, paragraph: 0 } })] },
    { index: 4, key: '005-s0.1', kind: 'subchapter', level: 2, title: 'El inventario', heading_spoken: true,
      page_start: '4', page_end: '4',
      units: [unit('heading', 'El inventario', { source: { chapter: 0, paragraph: 1 } }),
              unit('text', 'Contó tres mantas.', { source: { chapter: 0, paragraph: 2 } })] },
    { index: 5, key: '006-s1', kind: 'chapter', level: 1, title: 'Capítulo 2. La tormenta', heading_spoken: true,
      page_start: '5', page_end: '5',
      units: [unit('heading', 'Capítulo 2. La tormenta', { page_breaks: ['5'] }),
              unit('text', 'La luz parpadeó.', { source: { chapter: 1, paragraph: 0 } }),
              unit('note', 'Nota 1. Los faros se encendían a mano. Fin de nota 1.', { note: 1, source: { chapter: 1, paragraph: 1 } }),
              unit('closing', CLOSING)] },
  ],
  warnings: [{ code: 'missing', message: 'La planilla no indica: página del índice.' }],
  planilla: { title: 'El faro de la isla Almendra', author: 'Antonio Pérez', file1_page: '1', file2_page: '', index_page: '',
              index_depth: 2, missing: ['página del índice'], legal_text: LEGAL, closing: 'Aquí finaliza la grabación de {title_and_author}.',
              note_open: 'Nota {n}.', note_close: 'Fin de nota {n}.', production_line: 'Obra grabada…',
              production_date: 'octubre de 2026', edition_line: '', publisher_line: 'Editorial Crítica' },
}

// The produced job: one paragraph of chapter 2 waits for a person.
const ids = ['h1', 'legal', 'h2', 'bib', 'h3', 'i1', 'i2', 'h4', 't1', 'h5', 't2', 'h6', 't3', 'n1', 'c1']
function sentence(id: string, index: number, chapter: number, text: string, state = 'verified') {
  return { sentence_id: id, chapter_index: chapter, chapter_title: '', sequence: index, text, state, recognized_text: text,
           issues: state === 'verified' ? [] : [{ kind: 'replace', category: 'likely_mismatch', expected: 'parpadeó', recognized: 'parpadea',
                                                  expected_start: 7, expected_end: 15, audio_start: 1.2, reason: 'Se oye otra palabra.' }],
           audio_path: `audio/${id}.mp3`, quality_retakes: 0, technical_attempts: 1, last_error: '', updated_at: `2026-10-27T10:00:${String(index).padStart(2, '0')}Z`,
           acoustic: { stream: { duration_seconds: 2 } }, provenance: {} }
}
const flat = outline.sections.flatMap((section, chapter) => section.units.map((item, position) => ({ chapter, position, text: item.text })))
const job = {
  job_id: 'job-1', state: 'partially_completed', source_name: 'faro.xml', total_sentences: flat.length,
  voice_id: 'v-es', voice_name: 'Gabo', voice_language: 'es-ES', progress: { verified: flat.length - 1, needs_human_review: 1 },
  sentences: flat.map((item, index) => sentence(ids[index], item.position, item.chapter, item.text,
                                                ids[index] === 't3' ? 'needs_human_review' : 'verified')),
}
function structureFor(finalAudio: boolean) {
  return {
    job_id: 'job-1', state: 'partially_completed', once: true, warnings: [], assembled: false, pages: 5, total_duration: null,
    audio_format: { segments: { codec: 'MP3', sample_rate: 44100, channels: 1, bitrate_kbps: [128, 128] }, final: null },
    sections: outline.sections.map((section, chapter) => ({
      index: chapter, file_number: chapter + 1, key: section.key, kind: section.kind, level: section.level, title: section.title,
      heading_spoken: true, page_start: section.page_start, page_end: section.page_end, status: chapter === 5 ? 'needs_review' : 'verified',
      duration: section.units.length * 2, duration_final: finalAudio && chapter === 3,
      audio_url: finalAudio && chapter === 3 ? '/api/jobs/job-1/sections/3/audio?v=abc' : null,
      units: section.units.map((item, position) => {
        const index = flat.findIndex(entry => entry.chapter === chapter && entry.position === position)
        return { sentence_id: ids[index], role: item.role, text: item.text, state: job.sentences[index].state,
                 page_breaks: item.page_breaks, note: item.note, source: item.source, duration: 2,
                 clip: finalAudio && chapter === 3 ? [position * 2.5, position * 2.5 + 2] : null, issues: 0, has_audio: true,
                 updated_at: job.sentences[index].updated_at, editable: !['legal', 'bibliographic', 'index_entry', 'closing'].includes(item.role) }
      }),
    })),
  }
}

let calls: { url: string; init?: RequestInit }[]
let finalAudio = false

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}

function stub(extra: (url: string) => unknown = () => undefined) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, init })
    const custom = extra(url)
    if (custom) return ok(custom)
    if (url.endsWith('/api/capabilities')) return ok(capabilities)
    if (url.endsWith('/api/planilla/defaults')) return ok({ planilla: { legal_text: LEGAL, file1_page: '1' }, production_date: 'octubre de 2026' })
    if (url.endsWith('/api/books/structure')) return ok(structure)
    if (url.endsWith('/api/books/outline')) return ok(outline)
    if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: flat.length })
    if (url.endsWith('/api/jobs/job-1/structure')) return ok(structureFor(finalAudio))
    if (url.endsWith('/api/jobs/job-1/review')) return ok({ sentence: { ...job.sentences[12], state: 'verified', issues: [] } })
    if (url.includes('/api/jobs/job-1')) return ok(job)
    return ok({})
  }))
}

async function flush() {
  for (let round = 0; round < 4; round += 1) {
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)) })
  }
}

async function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
  await flush()
}

async function upload() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  const file = new File(['<dtbook/>'], 'faro.xml', { type: 'application/xml' })
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  await flush()
}

function button(label: string, scope: ParentNode = document) {
  return [...scope.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.includes(label)
    || item.getAttribute('aria-label') === label)
}

async function openEditor() {
  await upload()
  await act(async () => { button('Ver la estructura del audiolibro')!.click() })
}

function form(url: string) {
  const call = [...calls].reverse().find(item => item.url === url)
  return call?.init?.body as FormData | undefined
}

beforeEach(() => {
  window.localStorage.clear()
  finalAudio = false
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }))
  const media = window.HTMLMediaElement.prototype
  Object.defineProperty(media, 'play', { configurable: true, value() { this.dispatchEvent(new Event('play')); return Promise.resolve() } })
  Object.defineProperty(media, 'pause', { configurable: true, value() { this.dispatchEvent(new Event('pause')) } })
  Object.defineProperty(media, 'load', { configurable: true, value() {} })
  Object.defineProperty(media, 'currentTime', { configurable: true,
    get() { return (this as unknown as { _time?: number })._time ?? 0 },
    set(value: number) { (this as unknown as { _time?: number })._time = value } })
  stub()
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('the audiobook structure', () => {
  it('shows the initial files, chapters and level-2 subchapters with their pages', async () => {
    await render()
    await openEditor()
    expect(document.querySelector('#tab-review')?.getAttribute('aria-selected')).toBe('true')
    const rows = [...document.querySelectorAll<HTMLElement>('#panel-review [role="treeitem"]')]
    expect(rows.map(row => row.querySelector('strong')?.textContent)).toEqual([
      'El faro de la isla Almendra, De Antonio Pérez', 'DATOS BIBLIOGRÁFICOS', 'Índice.',
      'Capítulo 1. La llegada', 'El inventario', 'Capítulo 2. La tormenta'])
    expect(rows.map(row => row.getAttribute('aria-level'))).toEqual(['2', '2', '2', '1', '2', '1'])
    expect(document.querySelector('#panel-review')?.textContent).toContain('Archivos iniciales')
    expect(rows[4].textContent).toContain('Nivel 2')
    expect(rows[3].getAttribute('aria-expanded')).toBe('true')
    expect(document.querySelector('.ed-statusbar')?.textContent).toContain('Estructura ONCE')
    expect(document.querySelector('.ed-statusbar')?.textContent).toContain('5 páginas')
  })

  it('opens a section with its phrases, page marks, notes and closing phrase', async () => {
    await render()
    await openEditor()
    await act(async () => { document.querySelector<HTMLElement>('#ed-tree-5')!.click() })
    expect(document.querySelector('#ed-section-title')?.textContent).toBe('Capítulo 2. La tormenta')
    expect(document.querySelector('.ed-kicker')?.textContent).toContain('Archivo 6 · Nivel 1 · Capítulo')
    const marks = [...document.querySelectorAll('#panel-review .ed-page-mark')].map(item => item.textContent)
    expect(marks).toEqual(['Página 5'])
    const text = document.querySelector('#panel-review .ed-phrases')?.textContent ?? ''
    expect(text).toContain('Nota 1')
    expect(text).toContain('Frase final')
    expect(text).toContain(CLOSING)
  })

  it('collapses a chapter and moves through the tree with the keyboard', async () => {
    await render()
    await openEditor()
    const chapter = document.querySelector<HTMLElement>('#ed-tree-3')!
    await act(async () => { chapter.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })) })
    expect(document.querySelector('#ed-tree-4')).toBeNull()
    await act(async () => { chapter.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })) })
    expect(document.querySelector('#ed-tree-4')).not.toBeNull()
    await act(async () => { chapter.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })) })
    await act(async () => { document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) })
    expect(document.querySelector('#ed-section-title')?.textContent).toBe('El inventario')
  })

  it('finds a phrase anywhere in the book', async () => {
    await render()
    await openEditor()
    const search = document.querySelector<HTMLInputElement>('#panel-review input[type="search"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'mantas')
      search.dispatchEvent(new Event('input', { bubbles: true }))
    })
    const result = document.querySelector<HTMLButtonElement>('.ed-results button')!
    expect(result.textContent).toContain('El inventario')
    await act(async () => { result.click() })
    expect(document.querySelector('#ed-section-title')?.textContent).toBe('El inventario')
  })
})

describe('the planilla', () => {
  it('is sent with the production, and an empty one is not sent when ONCE is off', async () => {
    await render()
    await openEditor()
    await act(async () => { button('Revisar la planilla de producción')!.click() })
    const field = [...document.querySelectorAll<HTMLLabelElement>('.planilla-panel label')]
      .find(label => label.textContent?.startsWith('Página de los datos bibliográficos'))!.querySelector('input')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, '2')
      field.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => { button('Producir el audiolibro con revisión')!.click() })
    await flush()
    expect(JSON.parse(String(form('/api/books/jobs')?.get('planilla_json')))).toMatchObject({ file2_page: '2' })

    await render()
    await openEditor()
    const toggle = [...document.querySelectorAll<HTMLLabelElement>('.ed-idle label')]
      .find(label => label.textContent?.includes('Estructura ONCE'))!.querySelector('input')!
    await act(async () => { toggle.click() })
    await act(async () => { button('Producir el audiolibro con revisión')!.click() })
    await flush()
    expect(form('/api/books/jobs')?.get('planilla_json')).toBe('')
  })

  it('is filled from a Word planilla for a person to check', async () => {
    stub(url => url.endsWith('/api/planilla/parse') ? {
      planilla: { title: 'El faro de la isla Almendra', author: 'Antonio Pérez', file2_page: '2', index_page: '3',
                  index_depth: 1, edition_line: 'PRIMERA EDICIÓN MADRID, FEBRERO DE 2026', publisher_line: 'EDITORIAL CRÍTICA' },
      warnings: ['La planilla no trae número ABSYS.'],
    } : undefined)
    await render()
    await openEditor()
    await act(async () => { button('Revisar la planilla de producción')!.click() })
    const input = document.querySelector<HTMLInputElement>('.planilla-panel input[type="file"]')!
    Object.defineProperty(input, 'files', { configurable: true, value: [new File(['docx'], 'MODELO ONCE.docx')] })
    await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
    await flush()
    expect(calls.some(call => call.url.endsWith('/api/planilla/parse'))).toBe(true)
    const value = (label: string) => [...document.querySelectorAll<HTMLLabelElement>('.planilla-panel label')]
      .find(item => item.textContent?.startsWith(label))?.querySelector<HTMLInputElement | HTMLSelectElement>('input, select')?.value
    expect(value('Página del índice')).toBe('3')
    expect(value('Edición y publicación')).toBe('PRIMERA EDICIÓN MADRID, FEBRERO DE 2026')
    expect(value('El índice incluye')).toBe('1')
    expect(document.querySelector('.planilla-panel')?.textContent).toContain('La planilla no trae número ABSYS.')
  })
})

describe('listening and reviewing a produced book', () => {
  async function produce() {
    await render()
    await openEditor()
    await act(async () => { button('Producir el audiolibro con revisión')!.click() })
    await flush()
  }

  it('plays the final audio from the clip a phrase was measured at and follows it', async () => {
    finalAudio = true
    await produce()
    await act(async () => { document.querySelector<HTMLElement>('#ed-tree-3')!.click() })
    expect(document.querySelector('.ed-source')?.textContent).toBe('Audio final')
    await act(async () => { button('Escuchar la frase 2')!.click() })
    const audio = document.querySelector<HTMLAudioElement>('.audiobook-editor audio')!
    expect(audio.getAttribute('src')).toBe('/api/jobs/job-1/sections/3/audio?v=abc')
    await act(async () => { audio.dispatchEvent(new Event('loadedmetadata')) })
    expect(audio.currentTime).toBe(2.5)
    // The highlight follows the audio, phrase by phrase.
    audio.currentTime = 0.4
    await act(async () => { audio.dispatchEvent(new Event('timeupdate')) })
    expect(document.querySelector('#ed-unit-3-0')?.getAttribute('aria-current')).toBe('true')
    expect(document.querySelector('.ed-time')?.textContent).toContain('0:00,4')
  })

  it('plays each phrase on its own before the section is assembled', async () => {
    await produce()
    await act(async () => { document.querySelector<HTMLElement>('#ed-tree-3')!.click() })
    expect(document.querySelector('.ed-source')?.textContent).toBe('Audio por frases')
    await act(async () => { button('Escuchar la frase 2')!.click() })
    const audio = document.querySelector<HTMLAudioElement>('.audiobook-editor audio')!
    expect(audio.getAttribute('src')).toContain('/api/jobs/job-1/sentences/t1/audio')
    // At the end of a phrase the next one starts, as in a talking book.
    await act(async () => { audio.dispatchEvent(new Event('ended')) })
    expect(audio.getAttribute('src')).toContain('/api/jobs/job-1/sentences/h5/audio')
  })

  it('goes straight to the phrase that needs a decision and records it', async () => {
    await produce()
    expect(document.querySelector('.ed-problems')?.textContent).toBe('1 por revisar')
    await act(async () => { button('Problema siguiente')!.click() })
    expect(document.querySelector('#ed-section-title')?.textContent).toBe('Capítulo 2. La tormenta')
    const inspector = document.querySelector<HTMLElement>('.ed-inspector')!
    expect(inspector.textContent).toContain('Lo que se oye en el audio')
    expect(inspector.querySelector('mark')?.textContent).toBe('parpadeó')
    await act(async () => { button('La locución es correcta', inspector)!.click() })
    await flush()
    const review = calls.find(call => call.url.endsWith('/api/jobs/job-1/review'))!
    expect(JSON.parse(String(review.init?.body))).toMatchObject({ sentence_id: 't3', decision: 'accept' })
  })

  it('does not offer to rewrite text that comes from the planilla', async () => {
    await produce()
    await act(async () => { document.querySelector<HTMLElement>('#ed-tree-0')!.click() })
    await act(async () => { document.querySelector<HTMLButtonElement>('#ed-unit-0-1 .ed-phrase-main')!.click() })
    const inspector = document.querySelector<HTMLElement>('.ed-inspector')!
    expect(inspector.textContent).toContain('Este texto viene de la planilla')
    expect(button('Corregir el texto y narrarla', inspector)).toBeUndefined()
  })
})
