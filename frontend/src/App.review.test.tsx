import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { draftResponse, forgetDraft } from './test/draftApi'

// A paragraph the quality review could not settle stops the ZIP: step 3 lists
// it, and step 2 is where it is heard and decided, with the words that differ
// marked. These are the decisions the old step-3 queue offered, made where the
// rest of the editing is.

let root: Root

const capabilities = {
  daisy_profile: 'DAISY 3', tts_provider: 'deepgram', asr_provider: 'deepgram', tts_configured: true,
  voice: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES', voice_is_peninsular: true,
  requires_peninsular_voice: true, voice_speed_control: false, voice_id_configurable: false,
  audio_review_configured: true, offline_mock_mode: false, chapter_audio: true, chapter_smil: true,
  full_text_in_package: true, ncx_navigation: true, ncc_navigation: false, once_intro: false,
  once_bibliographic_audio: false, automatic_audio_review: true, retakes: true, max_quality_retakes: 3,
  max_technical_attempts: 6, sentence_audio_cache: true, official_daisy_pipeline_validation: false,
}
const structure = {
  sections: [{ id: 'section-1', title: 'Capítulo uno', category: 'main', paragraph_count: 1, included: true, needs_review: false, reason: '' }],
  selected_ids: ['section-1'],
}
const flaggedSentence = {
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
}
const flagged = {
  job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 2,
  voice_id: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
  progress: { verified: 1, needs_human_review: 1 }, sentences: [flaggedSentence],
}

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}
function fail(status: number, detail: string) {
  return { ok: false, status, json: async () => ({ detail }), text: async () => detail, blob: async () => new Blob() }
}

// The server narrates the paragraph inside the review request and answers
// with it; the job status afterwards reports the same, as the real one does.
function stub(job: typeof flagged, reply?: { ok: boolean; status: number; body: unknown }) {
  let current = job
  const requests: { url: string; body?: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push({ url, body: typeof init?.body === 'string' ? init.body : undefined })
    if (url.endsWith('/api/capabilities')) return ok(capabilities)
    if (url.includes('/api/drafts')) return ok(draftResponse(url, init, structure))
    if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 2 })
    if (url.endsWith('/api/jobs/job-1/review')) {
      if (!reply) return ok({ state: 'queued', sentence: { ...current.sentences[0], state: 'verified' } })
      const sentence = (reply.body as { sentence?: typeof flaggedSentence }).sentence
      if (reply.ok && sentence) current = { ...current, state: 'queued', sentences: [sentence] }
      return reply.ok ? ok(reply.body) : fail(reply.status, (reply.body as { detail: string }).detail)
    }
    if (url.includes('/api/jobs/job-1')) return ok(current)
    return ok({})
  }))
  return requests
}

function button(label: string) {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].find(item => item.textContent?.includes(label))
}

// Upload, ask for the ZIP, and follow step 3's link to the paragraph.
async function openFlaggedParagraph() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { configurable: true, value: [new File(['Texto.'], 'libro.txt', { type: 'text/plain' })] })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  await act(async () => { document.querySelector<HTMLButtonElement>('[data-primary-action="download"]')!.click() })
  expect(document.querySelector('#panel-export')?.textContent).toContain('Párrafos que esperan tu decisión (1)')
  await act(async () => { button('Capítulo uno · párrafo 2')!.click() })
  expect(document.querySelector('#tab-review')?.getAttribute('aria-selected')).toBe('true')
}

async function typeCorrection(value: string) {
  const textarea = document.querySelector<HTMLTextAreaElement>('#panel-review .qa-correction textarea')!
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!
  await act(async () => {
    setValue.call(textarea, value)
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  window.localStorage.clear()
  forgetDraft()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }))
  Object.defineProperty(window.HTMLMediaElement.prototype, 'play', { configurable: true, value: () => Promise.resolve() })
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('deciding a paragraph the review could not settle', () => {
  it('marks exactly the words that differ, in both the text and the audio', async () => {
    stub(flagged)
    await openFlaggedParagraph()
    const marks = Array.from(document.querySelectorAll('#panel-review .qa-compare mark')).map(mark => mark.textContent)
    // Not two paragraphs to compare by eye: the differing words are marked.
    expect(marks).toContain('medía')
    expect(marks).toContain('mide')
    // And the rest of the sentence is still readable around them.
    const compare = document.querySelector('#panel-review .qa-compare')?.textContent ?? ''
    expect(compare).toContain('El camino medía 18 km.')
    expect(compare).toContain('El camino mide 18 kilómetros.')
  })

  it('lets the reviewer hear the paragraph and accept or narrate it again', async () => {
    stub(flagged)
    await openFlaggedParagraph()
    // The paragraph is the selected one, and it plays from its own audio.
    const play = document.querySelector<HTMLButtonElement>('#panel-review .ed-phrase.is-selected .ed-phrase-play')
    expect(play?.disabled).toBe(false)
    expect(button('La locución es correcta')).toBeTruthy()
    expect(button('Volver a narrarla')).toBeTruthy()
    expect(button('Corregir el texto y narrarla')).toBeTruthy()
  })

  it('releases the controls once a decision is saved, rather than waiting for the job', async () => {
    stub(flagged)
    await openFlaggedParagraph()
    await act(async () => { button('La locución es correcta')!.click() })
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/jobs/job-1/review', expect.objectContaining({ method: 'POST' }))
    // Buttons must not stay disabled for the whole rebuild; that looked frozen.
    expect(button('Volver a narrarla')?.disabled).toBe(false)
  })

  it('cannot accept a paragraph that has no audio to accept', async () => {
    stub({ ...flagged, sentences: [{ ...flaggedSentence, audio_path: null as unknown as string, state: 'failed' }] })
    await openFlaggedParagraph()
    expect(button('La locución es correcta')?.disabled).toBe(true)
    expect(document.querySelector('#panel-review')?.textContent).toContain('No hay audio guardado para esta frase')
  })

  it('narrates a corrected paragraph at once and plays the new reading', async () => {
    const corrected = { ...flaggedSentence, text: 'El camino medía 18 kilómetros.', state: 'verified',
      recognized_text: 'El camino medía 18 kilómetros.', issues: [], last_error: '',
      audio_path: 'audio/chapter-001/segment-0002.mp3', updated_at: '2026-10-23T10:00:00+00:00' }
    const requests = stub(flagged, { ok: true, status: 200, body: { state: 'queued', sentence: corrected } })
    await openFlaggedParagraph()
    await typeCorrection('El camino medía 18 kilómetros.')
    await act(async () => { button('Corregir el texto y narrarla')!.click() })

    const review = requests.find(request => request.url.endsWith('/review'))!
    expect(JSON.parse(review.body!)).toMatchObject({ decision: 'correct', corrected_text: 'El camino medía 18 kilómetros.' })
    const panel = document.querySelector('#panel-review')!
    expect(panel.textContent).toContain('Corregida y comprobada')
    expect(panel.textContent).toContain('Nueva locución lista y comprobada')
    const player = panel.querySelector<HTMLAudioElement>('.qa-audio audio')!
    // A new version in the URL: the browser cannot replay the old cached reading.
    expect(player.getAttribute('src')).toContain('?v=2026-10-23T10%3A00%3A00%2B00%3A00')
    expect(player.autoplay).toBe(true)
  })

  it('keeps a paragraph that still differs on screen with its new reading', async () => {
    const retaken = { ...flaggedSentence, audio_path: 'audio/chapter-001/retakes/candidate-2.mp3',
      updated_at: '2026-10-23T10:05:00+00:00', last_error: 'La nueva locución todavía no coincide del todo con el texto.' }
    stub(flagged, { ok: true, status: 200, body: { state: 'queued', sentence: retaken } })
    await openFlaggedParagraph()
    await act(async () => { button('Volver a narrarla')!.click() })
    expect(document.body.textContent).toContain('todavía hay diferencias')
    const player = document.querySelector<HTMLAudioElement>('#panel-review .qa-audio audio')!
    expect(player.getAttribute('src')).toContain('2026-10-23T10%3A05%3A00')
    expect(button('La locución es correcta')).toBeTruthy()
  })

  it('says why a new reading failed and leaves the paragraph alone', async () => {
    stub(flagged, { ok: false, status: 503, body: {
      detail: 'No se pudo narrar el párrafo: La cuenta de ElevenLabs no tiene créditos suficientes. No se ha cambiado nada.' } })
    await openFlaggedParagraph()
    await act(async () => { button('Volver a narrarla')!.click() })
    expect(document.body.textContent).toContain('no tiene créditos suficientes')
    // Provider names are not shown to the user.
    expect(document.body.textContent).not.toContain('ElevenLabs')
    expect(document.querySelector('#panel-review .qa-compare')?.textContent).toContain('El camino medía 18 km.')
    expect(button('Volver a narrarla')?.disabled).toBe(false)
  })
})
