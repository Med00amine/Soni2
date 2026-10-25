import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

let root: Root

const capabilities = {
  daisy_profile: 'DAISY 3', tts_provider: 'elevenlabs', asr_provider: 'deepgram',
  tts_configured: true, voice: 'v-es', voice_name: 'Lucía', voice_language: 'es-ES',
  voice_is_peninsular: true, requires_peninsular_voice: true, voice_speed_control: false,
  voice_id_configurable: true, audio_review_configured: true, offline_mock_mode: false,
  chapter_audio: true, chapter_smil: true, full_text_in_package: true, ncx_navigation: true,
  ncc_navigation: false, once_intro: false, once_bibliographic_audio: false,
  automatic_audio_review: true, retakes: true, max_quality_retakes: 3, max_technical_attempts: 6,
  sentence_audio_cache: true, official_daisy_pipeline_validation: false,
}
const voices = {
  provider: 'elevenlabs', default_voice_id: 'v-es',
  voices: [{ voice_id: 'v-es', name: 'Lucía', language: 'es-ES', peninsular: true, labels: { accent: 'España' } }],
}
const structure = {
  sections: [{ id: 'section-1', title: 'Capítulo uno', category: 'main', paragraph_count: 1, included: true, needs_review: false, reason: '' }],
  selected_ids: ['section-1'],
}
const flagged = {
  job_id: 'job-1', state: 'partially_completed', source_name: 'libro.txt', total_sentences: 1,
  voice_id: 'v-es', voice_name: 'Lucía', voice_language: 'es-ES',
  progress: { needs_human_review: 1 },
  sentences: [{
    sentence_id: 'sentence-1', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 0,
    text: 'Vivía en Washington desde niño.', state: 'needs_human_review',
    recognized_text: 'Vivía en Uáshington desde niño.', issues: [],
    audio_path: 'audio/chapter-001/retakes/candidate.mp3',
    quality_retakes: 1, technical_attempts: 1, last_error: '',
  }],
}
const analysis = {
  word: 'Washington', ipa: 'ɡwaˈtʃinɡton',
  syllables: ['gua', 'ching', 'ton'], stressed: 1, looks_spanish: false,
  suggestions: [{
    respelling: 'guáshinton', label: 'Adaptado al español',
    reason: 'Las letras que el español no usa se cambian por las que suenan igual.',
  }],
  saved_respelling: '',
}

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}

let calls: { url: string; body?: unknown }[]

function stub(overrides: Record<string, unknown> = {}) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    // Only JSON bodies are recorded: the upload sends FormData, which is not JSON.
    const raw = init?.body
    calls.push({ url, body: typeof raw === 'string' ? JSON.parse(raw) : undefined })
    if (url.endsWith('/api/capabilities')) return ok(capabilities)
    if (url.endsWith('/api/voices')) return ok(voices)
    if (url.endsWith('/api/books/structure')) return ok(structure)
    if (url.endsWith('/api/books/jobs')) return ok({ job_id: 'job-1', state: 'queued', total_sentences: 1 })
    if (url.includes('/api/jobs/job-1')) return ok(flagged)
    if (url.endsWith('/api/pronunciation/analyse')) return ok(overrides.analyse ?? analysis)
    if (url.endsWith('/api/pronunciation/preview')) {
      return ok(overrides.preview ?? { audio_base64: btoa('audio'), audio_mime_type: 'audio/mpeg', spoken_text: 'x' })
    }
    if (url.endsWith('/api/pronunciation-lexicon/entries')) {
      return ok(overrides.save ?? { entries: { Washington: 'guáshinton' } })
    }
    return ok({})
  }))
}

async function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
}

async function reachReviewQueue() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  const file = new File(['Texto.'], 'libro.txt', { type: 'text/plain' })
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  const produce = Array.from(document.querySelectorAll('button'))
    .find(b => b.textContent?.includes('Producir con revisión de calidad'))!
  await act(async () => { produce.click() })
}

function wordChip(word: string) {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('.pron-word'))
    .find(b => b.textContent?.trim().startsWith(word))
}

function buttonIn(selector: string, label: string) {
  return Array.from(document.querySelectorAll<HTMLButtonElement>(`${selector} button`))
    .find(b => b.textContent?.includes(label))
}

beforeEach(() => {
  window.localStorage.clear()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} }))
  // jsdom has no audio pipeline.
  Object.defineProperty(window.HTMLMediaElement.prototype, 'play', {
    configurable: true, value: () => Promise.resolve(),
  })
  stub()
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('correcting a pronunciation', () => {
  it('offers the sentence\'s words to pick from', async () => {
    await render()
    await reachReviewQueue()
    expect(document.querySelector('.pron-editor')).toBeTruthy()
    expect(wordChip('Washington')).toBeTruthy()
    expect(wordChip('Vivía')).toBeTruthy()
    // Punctuation is stripped, and one-letter words are not offered.
    expect(wordChip('niño')).toBeTruthy()
  })

  it('shows how Spanish reads the word, with the stress marked', async () => {
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    const panel = document.querySelector('.pron-result')!.textContent ?? ''
    expect(panel).toContain('gua')
    expect(panel).toContain('ɡwaˈtʃinɡton')
    expect(panel).toContain('Tiene letras que el español no usa')
    // The stressed syllable is marked, not just listed.
    expect(document.querySelector('.pron-stress')?.textContent).toBe('ching')
    expect(calls.some(c => c.url.endsWith('/api/pronunciation/analyse')
      && (c.body as { word: string }).word === 'Washington')).toBe(true)
  })

  it('plays a candidate inside its sentence before anything is saved', async () => {
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    await act(async () => { buttonIn('.pron-options li', 'Escuchar')!.click() })
    const preview = calls.find(c => c.url.endsWith('/api/pronunciation/preview'))
    expect(preview).toBeTruthy()
    expect(preview!.body).toMatchObject({
      word: 'Washington', respelling: 'guáshinton',
      sentence: 'Vivía en Washington desde niño.',
    })
    // Nothing was saved by listening.
    expect(calls.some(c => c.url.endsWith('/api/pronunciation-lexicon/entries'))).toBe(false)
  })

  it('saves the chosen spelling for every book, and says so', async () => {
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    await act(async () => { buttonIn('.pron-options li', 'Usar esta')!.click() })
    const saved = calls.find(c => c.url.endsWith('/api/pronunciation-lexicon/entries'))
    expect(saved!.body).toEqual({ term: 'Washington', pronunciation: 'guáshinton' })
    const text = document.body.textContent ?? ''
    expect(text).toContain('se pronunciará')
    expect(text).toContain('y en los siguientes')
    // And it tells you the sentence has to be narrated again to hear it.
    expect(text).toContain('Vuelve a narrar')
  })

  it('lets a reviewer write their own spelling and hear it', async () => {
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    const field = document.querySelector<HTMLInputElement>('.pron-custom input')!
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
    await act(async () => {
      setter.call(field, 'uáshinton')
      field.dispatchEvent(new Event('input', { bubbles: true }))
    })
    const items = Array.from(document.querySelectorAll<HTMLElement>('.pron-options li'))
    const custom = items[items.length - 1]
    const listen = Array.from(custom.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Escuchar'))!
    await act(async () => { listen.click() })
    const previews = calls.filter(call => call.url.endsWith('/api/pronunciation/preview'))
    const preview = previews[previews.length - 1]
    expect((preview.body as { respelling: string }).respelling).toBe('uáshinton')
  })

  it('shows a spelling that was already saved for the word', async () => {
    stub({ analyse: { ...analysis, saved_respelling: 'guáshinton' } })
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    expect(document.querySelector('.pron-saved')?.textContent).toContain('guáshinton')
  })

  it('does not let an empty spelling be heard or saved', async () => {
    await render()
    await reachReviewQueue()
    await act(async () => { wordChip('Washington')!.click() })
    const items = Array.from(document.querySelectorAll<HTMLElement>('.pron-options li'))
    const custom = items[items.length - 1]
    for (const button of Array.from(custom.querySelectorAll<HTMLButtonElement>('button'))) {
      expect(button.disabled).toBe(true)
    }
  })
})
