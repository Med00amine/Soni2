import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { draftResponse, forgetDraft, lastSavedState } from './test/draftApi'

// Step 3 (Exportar): exactly two actions, «Descargar ZIP» and «Regenerar ZIP»,
// over the two ways a ZIP is produced (with quality review, the default, or
// directly). Everything a download used to guarantee still holds: the file is
// streamed to disk under the book's title, an unvalidated package says so in
// its name, progress is the server's real progress, failures are explained
// where the reader looks, and abandoned work is stopped.

let root: Root

const capabilities = {
  daisy_profile: 'DAISY 3 (provisional)', tts_provider: 'deepgram', asr_provider: 'deepgram',
  tts_configured: true, voice: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
  voice_is_peninsular: true, requires_peninsular_voice: true, voice_speed_control: false,
  voice_id_configurable: true, audio_review_configured: true, offline_mock_mode: false,
  chapter_audio: true, chapter_smil: true, full_text_in_package: true, ncx_navigation: true,
  ncc_navigation: false, once_intro: false, once_bibliographic_audio: false,
  automatic_audio_review: true, retakes: true, max_quality_retakes: 3, max_technical_attempts: 6,
  sentence_audio_cache: true, official_daisy_pipeline_validation: false,
}

const voices = {
  provider: 'deepgram', default_voice_id: 'aura-2-diana-es',
  voices: [{ voice_id: 'aura-2-diana-es', name: 'Diana', language: 'es-ES', peninsular: true, labels: { accent: 'España', gender: 'female' } }],
}

const structure = {
  sections: [{ id: 'section-1', title: 'Capítulo uno', category: 'main', paragraph_count: 1, included: true, needs_review: false, reason: '' }],
  selected_ids: ['section-1'],
}

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob(['zip']) }
}
function fail(status: number, detail: string) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => detail, blob: async () => new Blob() }
}

let calls: string[]
let jobPosts: FormData[]
let taskPosts: FormData[]
let packageResponse: unknown
let taskStatus: unknown
let artifactResponse: unknown
let jobResponse: unknown

const pipelineDone = {
  stage: 'completed', running: false, outcome: 'completed', total_seconds: 42,
  units: { total: 1, reused: 1, to_narrate: 0, narrated: 0 }, chapters: { total: 1, done: 1, reused: 1, rebuilt: 0 },
  timings: { checking: 0.4, narrating: 0, chapters: 1.2, package: 2.1, validating: 3.3, zipping: 1.1, verifying: 0.2 },
}

function finishedJob(state: string, reviews: string[] = [], extra: Record<string, unknown> = {}) {
  return ok({
    job_id: 'job-1', state: 'completed', source_name: 'libro.txt', total_sentences: 1, progress: { verified: 1 }, sentences: [],
    release: { state, label: state, blockers: [], reviews, dimensions: {}, uncertainty: [] },
    pipeline: pipelineDone,
    package: { bytes: 5 * 1024 * 1024, audit: 'passed', audit_detail: '', audio_files: 5, chapter_files: 4, total_time: '0:12:05.120', sections: 4 },
    ...extra,
  })
}

// Every anchor the page clicks to hand a file to the browser.
function watchDownloads() {
  const started: { href: string; name: string }[] = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    started.push({ href: this.href, name: this.download })
  })
  return started
}

function stubApi() {
  calls = []
  jobPosts = []
  taskPosts = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push(url)
    if (url.endsWith('/api/capabilities')) return ok(capabilities)
    if (url.endsWith('/api/voices')) return ok(voices)
    if (url.includes('/api/drafts')) return ok(draftResponse(url, init, structure))
    // The direct download: start a task, poll it, then fetch the ZIP.
    if (url.endsWith('/api/books/package-tasks')) {
      taskPosts.push(init?.body as FormData)
      return packageResponse
    }
    if (url.endsWith('/api/books/package-tasks/task-1/download')) return ok({})
    if (url.endsWith('/api/books/package-tasks/task-1/cancel')) return ok({ task_id: 'task-1', state: 'cancelled' })
    if (url.endsWith('/api/books/package-tasks/task-1')) return taskStatus
    if (url.includes('/artifacts')) return artifactResponse
    if (url.endsWith('/api/books/jobs')) {
      jobPosts.push(init?.body as FormData)
      return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
    }
    if (url.includes('/api/jobs/job-1')) return jobResponse ?? ok({
      job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 1,
      progress: { needs_human_review: 1 }, sentences: [],
    })
    return ok({})
  }))
  // jsdom implements neither of these, and the component uses both to save a file.
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:zip', revokeObjectURL: () => {} }))
}

async function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
}

async function uploadBook() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  const file = new File(['Capítulo uno. Texto.'], 'libro.txt', { type: 'text/plain' })
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
}

function primary(action: 'download' | 'regenerate') {
  return document.querySelector<HTMLButtonElement>(`#panel-export [data-primary-action="${action}"]`)!
}

async function downloadZip() {
  await act(async () => { primary('download').click() })
}

async function regenerateZip() {
  await act(async () => { primary('regenerate').click() })
}

function exportText() {
  return document.querySelector('#panel-export')?.textContent ?? ''
}

function status() {
  return document.querySelector('#panel-export .xp-status')
}

function buttonLabelled(text: string) {
  return Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes(text))
}

function task(state: string, done: number, total: number, reused = 0, extra: Record<string, unknown> = {}) {
  return ok({ task_id: 'task-1', state, done, total, reused, stage: state === 'running' ? 'narrating' : 'done', ...extra })
}

beforeEach(() => {
  window.localStorage.clear()
  forgetDraft()
  packageResponse = task('running', 0, 0)
  taskStatus = task('completed', 3, 3)
  artifactResponse = ok({})
  jobResponse = undefined
  stubApi()
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the two actions of step 3', () => {
  it('are exactly two, in the export step only, enabled once a book is loaded', async () => {
    await render()
    await uploadBook()
    const actions = document.querySelectorAll('[data-primary-action]')
    expect(actions).toHaveLength(2)
    expect([...actions].map(button => button.textContent?.trim())).toEqual(['Descargar ZIP', 'Regenerar ZIP'])
    expect(document.querySelector('#panel-review [data-primary-action]')).toBeNull()
    expect(primary('download').disabled).toBe(false)
    expect(primary('regenerate').disabled).toBe(false)
    // The old label claimed a correction step the button never performed.
    expect(document.body.textContent).not.toContain('Corrige y descarga')
  })

  it('stay two in every state a production goes through', async () => {
    const states = [
      ok({ job_id: 'job-1', state: 'running', source_name: 'libro.txt', total_sentences: 3, progress: { verified: 2, generating: 1 }, sentences: [],
           pipeline: { stage: 'narrating', running: true, units: { total: 3, reused: 2, to_narrate: 1, narrated: 0 } } }),
      ok({ job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 1, progress: { needs_human_review: 1 },
           sentences: [{ sentence_id: 's1', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 0, text: 'Texto.', state: 'needs_human_review',
                         recognized_text: '', issues: [], audio_path: 'a.mp3', quality_retakes: 0, technical_attempts: 1, last_error: '' }] }),
      ok({ job_id: 'job-1', state: 'failed', source_name: 'libro.txt', total_sentences: 1, progress: { pending: 1 }, sentences: [], error: 'Disco lleno.' }),
      finishedJob('PASSED'),
    ]
    for (const response of states) {
      jobResponse = response
      await render()
      await uploadBook()
      await act(async () => { buttonLabelled('Ver la estructura del audiolibro')?.click() })
      await act(async () => { buttonLabelled('Producir el audiolibro con revisión')!.click() })
      expect(document.querySelectorAll('#panel-export [data-primary-action]')).toHaveLength(2)
      act(() => root.unmount())
      forgetDraft()
      window.localStorage.clear()
    }
  })

  it('downloads a ZIP that is ready at once, without building it again', async () => {
    jobResponse = finishedJob('PASSED')
    const started = watchDownloads()
    await render()
    await uploadBook()
    await act(async () => { buttonLabelled('Ver la estructura del audiolibro')!.click() })
    await act(async () => { buttonLabelled('Producir el audiolibro con revisión')!.click() })
    expect(status()?.textContent).toContain('ZIP listo')
    expect(jobPosts).toHaveLength(1)
    await downloadZip()
    expect(jobPosts).toHaveLength(1)
    expect(started).toHaveLength(1)
    expect(started[0].href).toMatch(/\/api\/jobs\/job-1\/artifacts$/)
  })

  it('regenerates from the latest text without downloading, and asks the server for a new build', async () => {
    jobResponse = finishedJob('PASSED')
    const started = watchDownloads()
    await render()
    await uploadBook()
    await regenerateZip()
    expect(jobPosts).toHaveLength(1)
    expect(jobPosts[0].get('force')).toBe('true')
    expect(jobPosts[0].get('draft_id')).toBe('draft-1')
    expect(started).toHaveLength(0)
    expect(status()?.textContent).toContain('ZIP listo')
    // The download that follows takes that ZIP: nothing is built again.
    await downloadZip()
    expect(jobPosts).toHaveLength(1)
    expect(started).toHaveLength(1)
  })

  it('never starts a second build while one is running', async () => {
    jobResponse = ok({ job_id: 'job-1', state: 'running', source_name: 'libro.txt', total_sentences: 3, progress: { generating: 1 }, sentences: [],
                       pipeline: { stage: 'chapters', running: true, chapters: { total: 4, done: 1, reused: 1, rebuilt: 0 } } })
    await render()
    await uploadBook()
    act(() => { primary('download').click() })
    act(() => { primary('download').click() })
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 20)) })
    expect(jobPosts).toHaveLength(1)
    expect(primary('download').disabled).toBe(true)
    expect(primary('regenerate').disabled).toBe(true)
    expect(primary('download').getAttribute('aria-busy')).toBe('true')
  })
})

describe('what step 3 shows while a ZIP is built', () => {
  it('names the real stage and its counts, never a made-up percentage', async () => {
    jobResponse = ok({ job_id: 'job-1', state: 'running', source_name: 'libro.txt', total_sentences: 3,
      voice_id: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
      progress: { verified: 2, generating: 1 }, sentences: [],
      pipeline: { stage: 'narrating', running: true, units: { total: 3, reused: 2, to_narrate: 1, narrated: 0 },
                  timings: { checking: 0.3 } } })
    await render()
    await uploadBook()
    await downloadZip()
    const text = exportText()
    expect(status()?.textContent).toContain('Generando el audio que falta')
    expect(text).toContain('2 de 3 frases sin cambios')
    expect(text).toContain('0 de 1 frase nueva o corregida')
    const active = document.querySelector('.xp-stage.is-active')
    expect(active?.textContent).toContain('Generando el audio que falta')
    expect(active?.querySelector('progress')?.max).toBe(1)
    // Raw API state names are not shown to the user.
    expect(text).not.toContain('needs_human_review')
    expect(text).not.toContain('generating:')
  })

  it('shows the last build: what was reused, what was rebuilt, and how long each stage took', async () => {
    jobResponse = finishedJob('PASSED')
    await render()
    await uploadBook()
    await regenerateZip()
    const text = exportText()
    expect(text).toContain('Nada que generar: todo el audio ya estaba hecho y revisado')
    expect(text).toContain('1 de 1 · 1 sin cambios, reutilizados')
    expect(text).toContain('42 s en total')
    expect(document.querySelectorAll('.xp-stage.is-done, .xp-stage.is-skipped')).toHaveLength(8)
    // What the ZIP holds, as the server read it back.
    expect(text).toContain('Coherente')
    expect(text).toContain('0:12:05')
    expect(text).toContain('5 MB')
  })

  it('says what to do next when a production fails', async () => {
    jobResponse = ok({ job_id: 'job-1', state: 'failed', source_name: 'libro.txt', total_sentences: 1, progress: { pending: 1 }, sentences: [],
                       error: 'El disco del servidor se ha llenado.' })
    await render()
    await uploadBook()
    await downloadZip()
    const line = status()?.textContent ?? ''
    expect(status()?.className).toContain('is-error')
    expect(line).toContain('El disco del servidor se ha llenado.')
    expect(line).toContain('sin volver a pagarlo')
  })

  it('names the voice a job used and warns when the selection has moved on', async () => {
    jobResponse = ok({
      job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 1,
      voice_id: 'aura-2-alvaro-es', voice_name: 'Álvaro', voice_language: 'es-ES',
      progress: { needs_human_review: 1 }, sentences: [],
    })
    await render()
    await uploadBook()
    await downloadZip()
    const text = exportText()
    expect(text).toContain('Voz de este trabajo')
    expect(text).toContain('Álvaro')
    // The page's selected voice is Diana, so the stale job must say so.
    expect(text).toContain('Has seleccionado otra voz después de iniciar este trabajo')
  })
})

describe('downloading with quality review', () => {
  it('builds the ZIP and downloads it as the reviewed package', async () => {
    jobResponse = finishedJob('PASSED')
    const started = watchDownloads()
    await render()
    await uploadBook()
    await downloadZip()
    expect(jobPosts).toHaveLength(1)
    expect(jobPosts[0].get('force')).toBeNull()
    expect(started).toHaveLength(1)
    expect(started[0].href).toMatch(/\/api\/jobs\/job-1\/artifacts$/)
    expect(started[0].name).toBe('libro.zip')
    expect(document.querySelector('.xp-notice')?.textContent).toContain('La descarga ha empezado')
    expect(document.querySelector<HTMLAnchorElement>('.xp-notice a')?.href).toMatch(/\/artifacts$/)
  })

  it('still downloads a book waiting for a person, named as not validated, and says what is missing', async () => {
    jobResponse = finishedJob('NEEDS_REVIEW', ['OCR de la página 1: 1 palabras dudosas.'])
    const started = watchDownloads()
    await render()
    await uploadBook()
    await downloadZip()
    expect(started).toHaveLength(1)
    expect(started[0].href).toMatch(/artifacts\?diagnostic=true$/)
    expect(started[0].name).toContain('(SIN VALIDAR)')
    expect(primary('download').textContent).toContain('Descargar ZIP (sin validar)')
    expect(exportText()).toContain('OCR de la página 1')
  })

  it('explains a refused download under the actions', async () => {
    jobResponse = finishedJob('PASSED')
    artifactResponse = fail(409, 'El paquete completo todavía no está disponible.')
    const started = watchDownloads()
    await render()
    await uploadBook()
    await downloadZip()
    expect(started).toHaveLength(0)
    const notice = document.querySelector('#panel-export .xp-notice')
    expect(notice?.getAttribute('role')).toBe('alert')
    expect(notice?.textContent).toContain('todavía no está disponible')
    expect(notice?.textContent).toContain('Regenerar ZIP')
  })

  it('keeps following a production through a moment without connection', async () => {
    jobResponse = finishedJob('PASSED')
    const started = watchDownloads()
    const real = globalThis.fetch
    let failures = 1
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/api/jobs/job-1?view=live') && failures > 0) {
        failures -= 1
        throw new TypeError('Failed to fetch')
      }
      return real(input, init)
    }))
    await render()
    await uploadBook()
    await downloadZip()
    expect(started).toHaveLength(0)
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 2100)) })
    expect(started).toHaveLength(1)
    expect(status()?.textContent).toContain('ZIP listo')
  })

  it('stops at the paragraphs that need a decision, and opens them in step 2', async () => {
    jobResponse = ok({
      job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 2,
      progress: { verified: 1, needs_human_review: 1 },
      sentences: [{
        sentence_id: 'sentence-1', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 0,
        text: 'El camino medía 18 km.', state: 'needs_human_review', recognized_text: 'El camino mide 18 kilómetros.',
        issues: [{ kind: 'replace', category: 'likely_mismatch', expected: 'medía', recognized: 'mide',
                   expected_start: 10, expected_end: 15, recognized_start: 10, recognized_end: 14 }],
        audio_path: 'audio/chapter-001/retakes/candidate.mp3', quality_retakes: 2, technical_attempts: 3, last_error: '',
        diagnosis: { recommendation: 'Escucha la frase y decide.' },
      }],
    })
    const started = watchDownloads()
    await render()
    await uploadBook()
    await downloadZip()
    expect(started).toHaveLength(0)
    expect(status()?.textContent).toContain('Un párrafo necesita tu decisión')
    expect(exportText()).toContain('Párrafos que esperan tu decisión (1)')
    expect(document.body.textContent).not.toContain('La descarga del paquete DAISY ha comenzado')
    await act(async () => { buttonLabelled('Capítulo uno · párrafo 1')!.click() })
    expect(document.querySelector('#tab-review')?.getAttribute('aria-selected')).toBe('true')
    // Step 2 shows that paragraph with the words that differ marked.
    const marks = Array.from(document.querySelectorAll('#panel-review .qa-compare mark')).map(mark => mark.textContent)
    expect(marks).toEqual(expect.arrayContaining(['medía', 'mide']))
    expect(buttonLabelled('La locución es correcta')).toBeTruthy()
  })

  it('says why a production stopped when the voice account ran out of credits', async () => {
    jobResponse = ok({
      job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 2,
      error: 'La cuenta de ElevenLabs no tiene créditos suficientes para generar audio. Recarga créditos o espera a la renovación del plan.',
      progress: { needs_human_review: 1, pending: 1 },
      sentences: [{
        sentence_id: 'sentence-1', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 0,
        text: 'Empieza.', state: 'needs_human_review', recognized_text: 'Empiezan.', issues: [],
        audio_path: 'audio/chapter-001/retakes/candidate.mp3', quality_retakes: 2, technical_attempts: 1, last_error: '',
      }, {
        sentence_id: 'sentence-2', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 1,
        text: 'Sigue.', state: 'pending', recognized_text: '', issues: [], audio_path: null,
        quality_retakes: 0, technical_attempts: 1, last_error: 'credit (401): La cuenta de ElevenLabs no tiene créditos suficientes.',
      }],
    })
    await render()
    await uploadBook()
    await downloadZip()
    expect(status()?.textContent).toContain('no tiene créditos suficientes')
    expect(document.body.textContent).not.toContain('Fallo técnico')
  })

  it('streams the package to disk instead of buffering it in the tab', async () => {
    // An audiobook package is far larger than a tab can hold in memory, so
    // reading it into a blob made the button appear to do nothing.
    const probes: string[] = []
    const blob = vi.fn(async () => new Blob(['zip']))
    const createObjectURL = vi.fn(() => 'blob:never')
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL: () => {} }))
    const real = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      if (String(input).includes('/artifacts')) {
        probes.push((init?.headers as Record<string, string> | undefined)?.Range ?? '')
        return { ok: true, status: 206, json: async () => ({}), text: async () => '', blob } as unknown as Response
      }
      return real(input, init)
    }))
    jobResponse = finishedJob('PASSED')
    await render()
    await uploadBook()
    await downloadZip()
    // Only a one-byte probe, then the browser is handed the URL.
    expect(probes).toEqual(['bytes=0-0'])
    expect(blob).not.toHaveBeenCalled()
    expect(createObjectURL).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('La descarga del paquete DAISY ha comenzado')
  })
})

describe('downloading without review (the direct ZIP)', () => {
  beforeEach(() => window.localStorage.setItem('soni2-export-review', 'off'))

  it('downloads in one step without starting a review queue', async () => {
    const started = watchDownloads()
    await render()
    await uploadBook()
    await downloadZip()
    expect(taskPosts).toHaveLength(1)
    expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/download'))).toBe(true)
    expect(jobPosts).toHaveLength(0)
    expect(started).toHaveLength(1)
    expect(document.body.textContent).toContain('se ha descargado')
    expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
    expect(status()?.textContent).toContain('sin verificar')
  })

  it('names the downloaded ZIP after the book title', async () => {
    const started = watchDownloads()
    await render()
    await uploadBook()
    // Without a title yet, the file name stands in for it.
    await downloadZip()
    const title = document.querySelector<HTMLInputElement>('#metadata-title')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(title, 'El faro: de Cabo Gris?')
      title.dispatchEvent(new Event('input', { bubbles: true }))
    })
    // The title is part of the package: the ZIP before it no longer matches the book.
    expect(status()?.textContent).toContain('Has cambiado el libro')
    await downloadZip()
    expect(started.map(item => item.name)).toEqual(['libro.zip', 'El faro de Cabo Gris.zip'])
    expect(taskPosts).toHaveLength(2)
  })

  it('shows real progress while the book is narrated, then saves the ZIP', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      taskStatus = task('running', 2, 5, 1)
      await render()
      await uploadBook()
      await downloadZip()
      const bar = document.querySelector<HTMLProgressElement>('.xp-stage.is-active progress')
      expect(bar?.value).toBe(2)
      expect(bar?.max).toBe(5)
      // Audio that already existed is reported as such: it costs nothing.
      expect(exportText()).toContain('2 de 5 párrafos · 1 ya narrados, sin coste')

      taskStatus = task('completed', 5, 5, 1, { timings: { narrating: 3, assembling: 2 } })
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(document.querySelector('.xp-stage.is-active')).toBeNull()
      expect(document.body.textContent).toContain('1 de 5 párrafos ya estaban narrados')
    } finally {
      vi.useRealTimers()
    }
  })

  it('follows the chapters being assembled instead of stopping at the last paragraph', async () => {
    const started = watchDownloads()
    taskStatus = task('running', 3, 3, 3, { stage: 'assembling', chapters_done: 1, chapters_total: 2, chapters_reused: 1 })
    await render()
    await uploadBook()
    await downloadZip()
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 50)) })
    const active = document.querySelector('.xp-stage.is-active')
    expect(active?.textContent).toContain('Montando los capítulos')
    expect(active?.textContent).toContain('1 de 2 · 1 sin cambios, reutilizados')
    taskStatus = task('completed', 3, 3, 3)
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 1100)) })
    expect(started).toHaveLength(1)
  })

  it('explains a download that failed part way through', async () => {
    taskStatus = ok({ task_id: 'task-1', state: 'failed', done: 1, total: 5, reused: 0,
      detail: 'La cuenta de Deepgram no tiene saldo suficiente para generar audio (HTTP 402).' })
    await render()
    await uploadBook()
    await downloadZip()
    expect(document.querySelector('.xp-notice')?.textContent).toContain('no tiene saldo suficiente')
    expect(document.body.textContent).not.toContain('Deepgram')
    expect(calls.some(url => url.endsWith('/download'))).toBe(false)
  })

  it('reports why a direct download failed instead of staying silent', async () => {
    packageResponse = fail(503, 'La cuenta no tiene saldo suficiente para generar audio.')
    await render()
    await uploadBook()
    await downloadZip()
    expect(document.querySelector('.xp-notice')?.textContent).toContain('no tiene saldo suficiente')
  })

  it('stops the running download when another book is chosen', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      taskStatus = task('running', 1, 5)
      await render()
      await uploadBook()
      await downloadZip()
      expect(window.localStorage.getItem('soni2-active-package-task')).toBe('task-1')
      await uploadBook()
      expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/cancel'))).toBe(true)
      expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('regenerates without downloading, and the download then takes that ZIP', async () => {
    const started = watchDownloads()
    await render()
    await uploadBook()
    await regenerateZip()
    expect(taskPosts).toHaveLength(1)
    expect(taskPosts[0].get('force')).toBe('true')
    expect(started).toHaveLength(0)
    await downloadZip()
    expect(taskPosts).toHaveLength(1)
    expect(started).toHaveLength(1)
  })
})

describe('the export step', () => {
  it('stops a download the previous visit left running', async () => {
    window.localStorage.setItem('soni2-active-package-task', 'task-1')
    await render()
    expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/cancel'))).toBe(true)
    expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
  })

  it('starts empty on reload and stops the unfinished job it left behind', async () => {
    window.localStorage.setItem('soni2-active-production-job', 'job-1')
    let cancellationRequested = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/jobs/job-1/cancel')) {
        cancellationRequested = init?.method === 'POST'
        return ok({ job_id: 'job-1', state: 'cancellation_requested' })
      }
      if (url.endsWith('/api/jobs/job-1')) return ok({
        job_id: 'job-1', state: 'running', source_name: 'libro.txt', total_sentences: 1,
        progress: { generating: 1 }, sentences: [],
      })
      return ok({})
    }))

    await render()
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)) })

    expect(document.querySelector('.xp-stages')).toBeNull()
    expect(window.localStorage.getItem('soni2-active-production-job')).toBeNull()
    expect(cancellationRequested).toBe(true)
  })

  it('clears the previous job and cancels it when another book is selected', async () => {
    let cancellationRequested = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.includes('/api/drafts')) return ok(draftResponse(url, init, structure))
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.endsWith('/api/jobs/job-1/cancel')) {
        cancellationRequested = init?.method === 'POST'
        return ok({ job_id: 'job-1', state: 'cancellation_requested' })
      }
      if (url.includes('/api/jobs/job-1')) return ok({
        job_id: 'job-1', state: cancellationRequested ? 'cancelled' : 'running', source_name: 'libro.txt',
        total_sentences: 1, progress: { generating: 1 }, sentences: [],
        pipeline: { stage: 'narrating', running: true, units: { total: 1, reused: 0, to_narrate: 1, narrated: 0 } },
      })
      return ok({})
    }))

    await render()
    await uploadBook()
    act(() => { primary('download').click() })
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 20)) })
    expect(document.querySelector('.xp-stages')).not.toBeNull()

    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['Otro capítulo.'], 'otro-libro.txt', { type: 'text/plain' })],
    })
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }))
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    expect(document.body.textContent).toContain('otro-libro.txt')
    expect(document.querySelector('.xp-stages')).toBeNull()
    expect(window.localStorage.getItem('soni2-active-production-job')).toBeNull()
    expect(cancellationRequested).toBe(true)
  })

  it('explains the quality option, and that both ways share the audio already made', async () => {
    await render()
    await uploadBook()
    const text = exportText()
    expect(text).toContain('Revisión de calidad automática (recomendada)')
    expect(text).toContain('marcado como sin verificar')
    expect(text).toContain('no vuelve a pagar lo ya narrado')
    // The choice is remembered for the next visit.
    const box = [...document.querySelectorAll<HTMLInputElement>('#panel-export input[type="checkbox"]')]
      .find(input => input.closest('label')?.textContent?.includes('Revisión de calidad automática'))!
    expect(box.checked).toBe(true)
    await act(async () => { box.click() })
    expect(window.localStorage.getItem('soni2-export-review')).toBe('off')
  })

  it('keeps the reviewed production available from step 2 as well', async () => {
    await render()
    await uploadBook()
    expect(buttonLabelled('Producir el audiolibro con revisión')).toBeTruthy()
  })

  it('sends the chosen page range with the book, and builds that saved draft', async () => {
    await render()
    const file = document.querySelector<HTMLInputElement>('input[type="file"]')!
    Object.defineProperty(file, 'files', { configurable: true, value: [new File(['%PDF'], 'libro.pdf', { type: 'application/pdf' })] })
    await act(async () => { file.dispatchEvent(new Event('change', { bubbles: true })) })
    const range = document.querySelector<HTMLInputElement>('#page-range')!
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    await act(async () => {
      setValue.call(range, '20-114')
      range.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => { buttonLabelled('Aplicar')!.click() })
    expect(lastSavedState()?.pages).toBe('20-114')
    // And the ZIP is made from that saved draft, so it has exactly those pages.
    jobResponse = finishedJob('PASSED')
    await downloadZip()
    expect(jobPosts[jobPosts.length - 1]?.get('draft_id')).toBe('draft-1')
  })
})
