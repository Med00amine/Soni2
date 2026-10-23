import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

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
let packageResponse: unknown
let taskStatus: unknown
let artifactResponse: unknown

function stubApi() {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    if (url.endsWith('/api/capabilities')) return ok(capabilities)
    if (url.endsWith('/api/voices')) return ok(voices)
    if (url.endsWith('/api/books/structure')) return ok(structure)
    // The direct download: start a task, poll it, then fetch the ZIP.
    if (url.endsWith('/api/books/package-tasks')) return packageResponse
    if (url.endsWith('/api/books/package-tasks/task-1/download')) return ok({})
    if (url.endsWith('/api/books/package-tasks/task-1/cancel')) return ok({ task_id: 'task-1', state: 'cancelled' })
    if (url.endsWith('/api/books/package-tasks/task-1')) return taskStatus
    if (url.includes('/artifacts')) return artifactResponse
    if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
    if (url.includes('/api/jobs/job-1')) return ok({
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

// Start a reviewed production run the way a person does: upload, then produce.
async function startJob() {
  await uploadBook()
  await act(async () => { buttonLabelled('Producir con revisión de calidad')!.click() })
}

function buttonLabelled(text: string) {
  return Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes(text))
}

function task(state: string, done: number, total: number, reused = 0) {
  return ok({ task_id: 'task-1', state, done, total, reused })
}

beforeEach(() => {
  window.localStorage.clear()
  packageResponse = task('running', 0, 0)
  taskStatus = task('completed', 3, 3)
  artifactResponse = ok({})
  stubApi()
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('downloading the audiobook', () => {
  it('offers a direct download that does not mention correcting anything', async () => {
    await render()
    await uploadBook()
    const direct = buttonLabelled('Descargar audiolibro ahora')
    expect(direct).toBeTruthy()
    expect(direct!.disabled).toBe(false)
    // The old label claimed a correction step the button never performed.
    expect(document.body.textContent).not.toContain('Corrige y descarga')
  })

  it('downloads in one step without starting a review queue', async () => {
    await render()
    await uploadBook()
    await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
    expect(calls.some(url => url.endsWith('/api/books/package-tasks'))).toBe(true)
    expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/download'))).toBe(true)
    expect(calls.some(url => url.endsWith('/api/books/jobs'))).toBe(false)
    expect(document.body.textContent).toContain('se ha descargado')
    expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
  })

  it('shows real progress while the book is narrated, then saves the ZIP', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      taskStatus = task('running', 2, 5, 1)
      await render()
      await uploadBook()
      await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
      const bar = document.querySelector<HTMLProgressElement>('progress.direct-progress')
      expect(bar?.value).toBe(2)
      expect(bar?.max).toBe(5)
      // Audio that already existed is reported as such: it costs nothing.
      expect(document.body.textContent).toContain('Párrafo 2 de 5 · 1 ya narrados, sin coste')

      taskStatus = task('completed', 5, 5, 1)
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(document.querySelector('progress.direct-progress')).toBeNull()
      expect(document.body.textContent).toContain('1 de 5 párrafos ya estaban narrados')
    } finally {
      vi.useRealTimers()
    }
  })

  it('explains a download that failed part way through', async () => {
    taskStatus = ok({ task_id: 'task-1', state: 'failed', done: 1, total: 5, reused: 0,
      detail: 'La cuenta de Deepgram no tiene saldo suficiente para generar audio (HTTP 402).' })
    await render()
    await uploadBook()
    await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
    expect(document.body.textContent).toContain('no tiene saldo suficiente')
    expect(calls.some(url => url.endsWith('/download'))).toBe(false)
  })

  it('stops the running download when another book is chosen', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      taskStatus = task('running', 1, 5)
      await render()
      await uploadBook()
      await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
      expect(window.localStorage.getItem('soni2-active-package-task')).toBe('task-1')
      await uploadBook()
      expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/cancel'))).toBe(true)
      expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops a download the previous visit left running', async () => {
    window.localStorage.setItem('soni2-active-package-task', 'task-1')
    await render()
    expect(calls.some(url => url.endsWith('/api/books/package-tasks/task-1/cancel'))).toBe(true)
    expect(window.localStorage.getItem('soni2-active-package-task')).toBeNull()
  })

  it('reports why a direct download failed instead of staying silent', async () => {
    packageResponse = fail(503, 'La cuenta no tiene saldo suficiente para generar audio.')
    await render()
    await uploadBook()
    await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
    expect(document.body.textContent).toContain('no tiene saldo suficiente')
  })

  it('keeps the reviewed production run available as a separate action', async () => {
    await render()
    await uploadBook()
    expect(buttonLabelled('Producir con revisión de calidad')).toBeTruthy()
  })

  it('explains a refused package download instead of claiming it started', async () => {
    // The package is only served once every sentence is verified or accepted.
    artifactResponse = fail(409, 'El paquete completo todavía no está disponible.')
    await render()
    await startJob()
    const text = document.body.textContent ?? ''
    expect(text).toContain('necesita una revisión')
    expect(text).not.toContain('La descarga del paquete DAISY ha comenzado')
  })
})

describe('the export step', () => {
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

    expect(document.body.textContent).not.toContain('Producción del audiolibro')
    expect(window.localStorage.getItem('soni2-active-production-job')).toBeNull()
    expect(cancellationRequested).toBe(true)
  })

  it('clears the previous job and cancels it when another book is selected', async () => {
    let cancellationRequested = false
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.endsWith('/api/jobs/job-1/cancel')) {
        cancellationRequested = init?.method === 'POST'
        return ok({ job_id: 'job-1', state: 'cancellation_requested' })
      }
      if (url.endsWith('/api/jobs/job-1')) return ok({
        job_id: 'job-1', state: cancellationRequested ? 'cancelled' : 'running', source_name: 'libro.txt',
        total_sentences: 1, progress: { generating: 1 }, sentences: [],
      })
      return ok({})
    }))

    await render()
    await startJob()
    expect(document.body.textContent).toContain('Producción del audiolibro')

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
    expect(document.body.textContent).not.toContain('Producción del audiolibro')
    expect(window.localStorage.getItem('soni2-active-production-job')).toBeNull()
    expect(cancellationRequested).toBe(true)
  })

  it('keeps the export actions in the Export step, not on every tab', async () => {
    await render()
    await uploadBook()
    // Step 1 shows the upload controls, not the export buttons.
    expect(buttonLabelled('Descargar audiolibro ahora')).toBeTruthy()
    const dock = document.querySelector('.review-action-dock')
    expect(dock).toBeTruthy()
    // The dock lives inside the export panel, so it cannot duplicate itself on
    // the listen-and-review step.
    expect(document.querySelector('#panel-export')?.contains(dock!)).toBe(true)
    expect(document.querySelector('#panel-review')?.contains(dock!)).toBe(false)
  })

  it('explains both export choices before the buttons', async () => {
    await render()
    await uploadBook()
    const panel = document.querySelector('#panel-export')?.textContent ?? ''
    expect(panel).toContain('Descargar directamente')
    expect(panel).toContain('Producir con revisión de calidad')
    expect(panel).toContain('Sin revisión automática de calidad')
    expect(panel).toContain('informe de calidad')
    // And says the two share already-generated audio.
    expect(panel).toContain('no vuelve a')
  })

  it('names the voice a job used and warns when the selection has moved on', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok({
        job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 1,
        voice_id: 'aura-2-alvaro-es', voice_name: 'Álvaro', voice_language: 'es-ES',
        progress: { needs_human_review: 1 }, sentences: [],
      })
      return ok({})
    }))
    await render()
    await startJob()
    const text = document.body.textContent ?? ''
    expect(text).toContain('Voz utilizada en este trabajo')
    expect(text).toContain('Álvaro')
    // The page's selected voice is Diana, so the stale job must say so.
    expect(text).toContain('Has seleccionado otra voz después de iniciar este trabajo')
  })
})

describe('regenerating a sentence', () => {
  it('shows what is happening instead of looking frozen', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok({
        job_id: 'job-1', state: 'running', source_name: 'libro.txt', total_sentences: 3,
        voice_id: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
        progress: { verified: 2, generating: 1 }, sentences: [],
      })
      return ok({})
    }))
    await render()
    await startJob()
    const text = document.body.textContent ?? ''
    expect(text).toContain('Narrando y comprobando una frase')
    expect(text).toContain('puede tardar un minuto')
    // Raw API state names are not shown to the user.
    expect(text).toContain('Correctas: 2')
    expect(text).not.toContain('needs_human_review')
    expect(text).not.toContain('generating:')
  })
})

describe('the quality review queue', () => {
  const flagged = {
    job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 2,
    voice_id: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
    progress: { verified: 1, needs_human_review: 1 },
    sentences: [{
      sentence_id: 'sentence-1', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 1,
      text: 'El camino medía 18 km.', state: 'needs_human_review',
      recognized_text: 'El camino mide 18 kilómetros.',
      issues: [{
        kind: 'replace', category: 'likely_mismatch', difference_type: 'substitution',
        expected: 'medía', recognized: 'mide', reason: 'Diferencia amplia respecto al texto revisado.',
        expected_start: 10, expected_end: 15, recognized_start: 10, recognized_end: 14,
      }],
      audio_path: 'audio/chapter-001/retakes/candidate.mp3',
      quality_retakes: 3, technical_attempts: 4, last_error: 'Revisión humana necesaria.',
    }],
  }

  function stubFlagged() {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok(flagged)
      return ok({})
    }))
  }

  it('marks exactly the words that differ, in both the text and the audio', async () => {
    stubFlagged()
    await render()
    await startJob()
    const marks = Array.from(document.querySelectorAll('.qa-compare mark')).map(m => m.textContent)
    // Not two paragraphs to compare by eye: the differing words are marked.
    expect(marks).toContain('medía')
    expect(marks).toContain('mide')
    // And the rest of the sentence is still readable around them.
    const compare = document.querySelector('.qa-compare')?.textContent ?? ''
    expect(compare).toContain('El camino medía 18 km.')
    expect(compare).toContain('El camino mide 18 kilómetros.')
  })

  it('lets the reviewer hear the sentence and then accept or regenerate it', async () => {
    stubFlagged()
    await render()
    await startJob()
    const player = document.querySelector<HTMLAudioElement>('.qa-audio audio')
    expect(player).toBeTruthy()
    expect(player!.getAttribute('src')).toContain('/api/jobs/job-1/sentences/sentence-1/audio')
    expect(buttonLabelled('La locución es correcta')).toBeTruthy()
    expect(buttonLabelled('Volver a narrarla')).toBeTruthy()
    expect(buttonLabelled('Corregir el texto y narrarla')).toBeTruthy()
    expect(document.body.textContent).toContain('Párrafos que necesitan tu revisión (1)')
  })

  it('releases the controls once a decision is saved, rather than waiting for the job', async () => {
    stubFlagged()
    await render()
    await startJob()
    await act(async () => { buttonLabelled('La locución es correcta')!.click() })
    // Buttons must not stay disabled for the whole rebuild; that looked frozen.
    const accept = buttonLabelled('Volver a narrarla')
    expect(accept?.disabled).toBe(false)
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/jobs/job-1/review', expect.objectContaining({ method: 'POST' }))
  })

  it('cannot accept a sentence that has no audio to accept', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok({
        ...flagged,
        sentences: [{ ...flagged.sentences[0], audio_path: null, state: 'failed' }],
      })
      return ok({})
    }))
    await render()
    await startJob()
    expect(buttonLabelled('La locución es correcta')?.disabled).toBe(true)
    expect(document.body.textContent).toContain('No hay audio guardado para este párrafo')
  })

  // The server narrates the paragraph inside the review request and answers
  // with it; the job status afterwards reports the same, as the real one does.
  function stubReview(reply: { ok: boolean; status: number; body: unknown }) {
    let job: typeof flagged = flagged
    const requests: { url: string; body?: string }[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, body: typeof init?.body === 'string' ? init.body : undefined })
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.endsWith('/api/jobs/job-1/review')) {
        const sentence = (reply.body as { sentence?: (typeof flagged.sentences)[number] }).sentence
        if (reply.ok && sentence) job = { ...flagged, state: 'queued', sentences: [sentence] }
        return reply.ok ? ok(reply.body) : fail(reply.status, (reply.body as { detail: string }).detail)
      }
      if (url.includes('/api/jobs/job-1')) return ok(job)
      return ok({})
    }))
    return requests
  }

  async function typeCorrection(value: string) {
    const textarea = document.querySelector<HTMLTextAreaElement>('.qa-correction textarea')!
    const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
    await act(async () => {
      setValue.call(textarea, value)
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }

  it('narrates a corrected paragraph at once and plays the new reading in the same card', async () => {
    const corrected = { ...flagged.sentences[0], text: 'El camino medía 18 kilómetros.', state: 'verified',
      recognized_text: 'El camino medía 18 kilómetros.', issues: [], last_error: '',
      audio_path: 'audio/chapter-001/segment-0002.mp3', updated_at: '2026-10-23T10:00:00+00:00' }
    const requests = stubReview({ ok: true, status: 200, body: { state: 'queued', sentence: corrected } })
    await render()
    await startJob()
    await typeCorrection('El camino medía 18 kilómetros.')
    await act(async () => { buttonLabelled('Corregir el texto y narrarla')!.click() })

    const review = requests.find(request => request.url.endsWith('/review'))!
    expect(JSON.parse(review.body!)).toMatchObject({ decision: 'correct', corrected_text: 'El camino medía 18 kilómetros.' })
    const card = document.querySelector('.review-queue-item')!
    // Still on screen even though it now passes, so the new reading can be heard.
    expect(card.textContent).toContain('Corregida y comprobada')
    expect(document.body.textContent).toContain('Nueva locución lista y comprobada')
    const player = card.querySelector<HTMLAudioElement>('.qa-audio audio')!
    // A new version in the URL: the browser cannot replay the old cached reading.
    expect(player.getAttribute('src')).toContain('?v=2026-10-23T10%3A00%3A00%2B00%3A00')
    expect(player.autoplay).toBe(true)
    expect(document.querySelector('.review-queue-title')?.textContent).toBe('Párrafos corregidos')
    await act(async () => { buttonLabelled('Listo')!.click() })
    expect(document.querySelector('.review-queue-item')).toBeNull()
  })

  it('keeps a paragraph that still differs on screen with its new reading', async () => {
    const retaken = { ...flagged.sentences[0], audio_path: 'audio/chapter-001/retakes/candidate-2.mp3',
      updated_at: '2026-10-23T10:05:00+00:00', last_error: 'La nueva locución todavía no coincide del todo con el texto.' }
    stubReview({ ok: true, status: 200, body: { state: 'queued', sentence: retaken } })
    await render()
    await startJob()
    await act(async () => { buttonLabelled('Volver a narrarla')!.click() })
    expect(document.body.textContent).toContain('todavía hay diferencias')
    const player = document.querySelector<HTMLAudioElement>('.qa-audio audio')!
    expect(player.getAttribute('src')).toContain('2026-10-23T10%3A05%3A00')
    expect(buttonLabelled('La locución es correcta')).toBeTruthy()
    expect(document.body.textContent).toContain('Párrafos que necesitan tu revisión (1)')
  })

  it('says why a new reading failed and leaves the paragraph alone', async () => {
    stubReview({ ok: false, status: 503, body: {
      detail: 'No se pudo narrar el párrafo: La cuenta de ElevenLabs no tiene créditos suficientes. No se ha cambiado nada.' } })
    await render()
    await startJob()
    await act(async () => { buttonLabelled('Volver a narrarla')!.click() })
    expect(document.body.textContent).toContain('no tiene créditos suficientes')
    // Provider names are not shown to the user.
    expect(document.body.textContent).not.toContain('ElevenLabs')
    expect(document.querySelector('.review-queue-item')?.textContent).toContain('El camino medía 18 km.')
    expect(buttonLabelled('Volver a narrarla')?.disabled).toBe(false)
  })
})

describe('narrating part of a PDF', () => {
  it('sends the chosen page range with the book, and reads the book again for it', async () => {
    const bodies: FormData[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.body instanceof FormData) bodies.push(init.body)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      return ok({})
    }))
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
    const structures = bodies.filter(body => body.has('pages'))
    expect(structures[structures.length - 1]?.get('pages')).toBe('20-114')
    // And the export sends it too, so the audiobook has exactly those pages.
    await act(async () => { buttonLabelled('Descargar audiolibro ahora')!.click() })
    expect(bodies[bodies.length - 1]?.get('pages')).toBe('20-114')
  })
})

describe('large downloads', () => {
  const completedJob = {
    job_id: 'job-1', state: 'completed', source_name: 'libro.txt', total_sentences: 1,
    voice_id: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
    progress: { verified: 1 }, sentences: [],
  }

  it('streams the package to disk instead of buffering it in the tab', async () => {
    // An audiobook package is far larger than a tab can hold in memory, so
    // reading it into a blob made the button appear to do nothing.
    const probes: string[] = []
    const blob = vi.fn(async () => new Blob(['zip']))
    const createObjectURL = vi.fn(() => 'blob:never')
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL: () => {} }))
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.includes('/artifacts')) {
        probes.push((init?.headers as Record<string, string> | undefined)?.Range ?? '')
        return { ok: true, status: 206, json: async () => ({}), text: async () => '', blob }
      }
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok(completedJob)
      return ok({})
    }))
    await render()
    await startJob()
    const button = buttonLabelled('Descargar paquete revisado')
    expect(button).toBeTruthy()
    await act(async () => { button!.click() })
    // Only a one-byte probe, then the browser is handed the URL.
    expect(probes).toEqual(['bytes=0-0'])
    expect(blob).not.toHaveBeenCalled()
    expect(createObjectURL).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('La descarga del paquete DAISY ha comenzado')
  })

  it('still explains a package that is not ready yet', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return ok(capabilities)
      if (url.endsWith('/api/voices')) return ok(voices)
      if (url.endsWith('/api/books/structure')) return ok(structure)
      if (url.includes('/artifacts')) return fail(409, 'El paquete completo todavía no está disponible.')
      if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
      if (url.includes('/api/jobs/job-1')) return ok(completedJob)
      return ok({})
    }))
    await render()
    await startJob()
    await act(async () => { buttonLabelled('Descargar paquete revisado')!.click() })
    expect(document.body.textContent).toContain('todavía no está disponible')
    expect(document.body.textContent).toContain('Descargar audiolibro ahora')
  })
})
