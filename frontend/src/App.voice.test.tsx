import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

let root: Root

const baseCapabilities = {
  daisy_profile: 'DAISY 3 (provisional, validación estructural propia)',
  tts_provider: 'deepgram', asr_provider: 'deepgram', tts_configured: true,
  voice: 'aura-2-diana-es', voice_name: 'Diana', voice_language: 'es-ES',
  voice_is_peninsular: true, requires_peninsular_voice: true,
  voice_speed_control: false, voice_id_configurable: true, audio_review_configured: true,
  offline_mock_mode: false, chapter_audio: true, chapter_smil: true, full_text_in_package: true,
  ncx_navigation: true, ncc_navigation: false, once_intro: false, once_bibliographic_audio: false,
  automatic_audio_review: true, retakes: true, max_quality_retakes: 3, max_technical_attempts: 6,
  sentence_audio_cache: true, official_daisy_pipeline_validation: false,
}

const voices = {
  provider: 'deepgram',
  default_voice_id: 'aura-2-diana-es',
  voices: [
    { voice_id: 'aura-2-diana-es', name: 'Diana', provider: 'deepgram', language: 'es-ES', peninsular: true, labels: { language: 'Spanish', accent: 'España', gender: 'female' } },
    { voice_id: 'aura-2-sirio-es', name: 'Sirio', provider: 'deepgram', language: 'es-MX', peninsular: false, labels: { language: 'Spanish', accent: 'México', gender: 'male' } },
  ],
}

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}

function stubApi(capabilities: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/capabilities')) return response(capabilities)
    if (url.endsWith('/api/voices')) return response(voices)
    return response({ state: 'queued' })
  }))
}

async function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<App />) })
}

function selectVoice(voiceId: string) {
  const select = document.querySelector<HTMLSelectElement>('#voice-id')!
  select.value = voiceId
  act(() => { select.dispatchEvent(new Event('change', { bubbles: true })) })
}

beforeEach(() => window.localStorage.clear())

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('narration voice and provider status', () => {
  it('defaults to the Spain Spanish voice the server recommends', async () => {
    stubApi(baseCapabilities)
    await render()
    const select = document.querySelector<HTMLSelectElement>('#voice-id')!
    expect(select.value).toBe('aura-2-diana-es')
    // Spain Spanish is offered first.
    expect(Array.from(select.options).map(option => option.value)[0]).toBe('aura-2-diana-es')
  })

  it('names the active voice and its language so the accent is visible', async () => {
    stubApi(baseCapabilities)
    await render()
    expect(document.body.textContent).toContain('Diana')
    expect(document.body.textContent).toContain('es-ES')
  })

  it('warns when a Latin American voice is chosen for a Spain Spanish book', async () => {
    stubApi(baseCapabilities)
    await render()
    expect(document.body.textContent).not.toContain('no español de España')
    selectVoice('aura-2-sirio-es')
    expect(document.body.textContent).toContain('no español de España')
    expect(document.body.textContent).toContain('rechazará la producción')
  })

  it('says plainly when narration is only simulated', async () => {
    stubApi({ ...baseCapabilities, tts_provider: 'mock', asr_provider: 'mock', offline_mock_mode: true })
    await render()
    expect(document.body.textContent).toContain('Modo simulado activo')
    expect(document.body.textContent).toContain('sin coste')
  })

  it('warns that every sentence needs a person when no transcriber is configured', async () => {
    stubApi({ ...baseCapabilities, asr_provider: 'none', audio_review_configured: false })
    await render()
    expect(document.body.textContent).toContain('revisión automática de calidad no se puede ejecutar')
  })

  it('never shows a provider brand name to the user', async () => {
    stubApi(baseCapabilities)
    await render()
    expect(document.body.textContent).not.toContain('Deepgram')
    expect(document.body.textContent).not.toContain('Fish Audio')
  })
})

describe('a voice whose accent nobody could check', () => {
  it('says so instead of calling it Latin American', async () => {
    const unknown = {
      provider: 'elevenlabs', default_voice_id: 'o0SveC0zgHFuCsEO3vHR',
      voices: [{ voice_id: 'o0SveC0zgHFuCsEO3vHR', name: 'Gabo', provider: 'elevenlabs', language: 'es',
        peninsular: false, accent_verified: false, labels: { language: 'Spanish', accent: '', gender: '' } }],
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return response({ ...baseCapabilities, tts_provider: 'elevenlabs',
        voice: 'o0SveC0zgHFuCsEO3vHR', voice_name: 'Gabo', voice_language: 'es', voice_is_peninsular: false,
        voice_accent_verified: false, requires_peninsular_voice: false, voice_speed_control: true })
      if (url.endsWith('/api/voices')) return response(unknown)
      return response({ state: 'queued' })
    }))
    await render()
    const text = document.body.textContent ?? ''
    expect(text).toContain('No se ha podido comprobar el acento de la voz «Gabo»')
    expect(text).not.toContain('y no español de España')
    expect(document.querySelector<HTMLSelectElement>('#voice-id')?.selectedOptions[0]?.textContent)
      .toContain('acento sin comprobar')
  })
})
