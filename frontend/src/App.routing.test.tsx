import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

let root: Root

const capabilities = {
  daisy_profile: 'provisional-daisy3', tts_configured: true,
  voice: 'ec0718028a2445fb9be8e8bfb56a0150', voice_speed_control: true,
  voice_id_configurable: true, audio_review_configured: true, chapter_audio: true,
  chapter_smil: true, full_text_in_package: true, ncx_navigation: true,
  ncc_navigation: false, once_intro: false, once_bibliographic_audio: false,
  automatic_audio_review: true, retakes: true,
}

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body), blob: async () => new Blob() }
}

function render() {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  act(() => root.render(<App />))
}

async function uploadTextBook() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  const file = new File(['Capítulo uno. Capítulo dos.'], 'sample.txt', { type: 'text/plain' })
  Object.defineProperty(input, 'files', { configurable: true, value: [file] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  return file
}

beforeEach(() => {
  window.localStorage.clear()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:audio', revokeObjectURL: () => {} }))
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/capabilities')) return response(capabilities)
    if (url.endsWith('/api/voices')) return response({ voices: [
      { voice_id: 'ec0718028a2445fb9be8e8bfb56a0150', name: 'Rodrigo', labels: { accent: 'Mexico', gender: 'male' } },
      { voice_id: 'fb9d5855f185451983404deb10788ad4', name: 'Isabela', labels: { accent: 'Colombia', gender: 'female' } },
      { voice_id: '3f065d660212478a9c697fa965b4d098', name: 'Carmen', labels: { accent: 'España', gender: 'female' } },
    ] })
    if (url.endsWith('/api/books/structure')) return response({ sections: [{ id: 'section-1', title: 'Capítulo uno', category: 'main', paragraph_count: 2, included: true, needs_review: false, reason: '' }], selected_ids: ['section-1'] })
    if (url.endsWith('/api/books/preview')) return response({
      audio_base64: '', audio_mime_type: 'audio/mpeg', transcript: 'Primera frase.\n\nSegunda frase.',
      chapters: [{ title: 'Capítulo uno', paragraphs: ['Primera frase.', 'Segunda frase.'] }],
      title: 'Libro de prueba', author: 'Autora', audio_error: 'Sin audio de muestra.',
    })
    if (url.endsWith('/api/books/jobs')) return response({ job_id: 'job-1', state: 'queued', total_sentences: 2 })
    if (url.endsWith('/api/jobs/job-1')) return response({
      job_id: 'job-1', state: 'partially_completed', source_name: 'sample.txt', total_sentences: 2,
      progress: { verified: 1, needs_human_review: 1 }, sentences: [{
        sentence_id: 'sentence-review', chapter_index: 0, chapter_title: 'Capítulo uno', sequence: 1,
        text: 'Segunda frase.', state: 'needs_human_review', recognized_text: 'Segunda frase',
        issues: [], audio_path: 'audio/chapter-001/retakes/candidate.mp3', quality_retakes: 3,
        technical_attempts: 4, last_error: 'Revisión humana necesaria.',
      }],
    })
    return response({ state: 'queued' })
  }))
})

afterEach(() => {
  act(() => root?.unmount())
  vi.unstubAllGlobals()
})

describe('Soni2 audiobook workspace', () => {
  it('renders the Spanish book workflow and supported formats', async () => {
    render()
    await act(async () => {})
    expect(document.querySelector('.workspace-titlebar')).toBeNull()
    expect(document.querySelector('#tab-upload')?.textContent).toContain('Subir libro')
    expect(document.querySelector('#tab-review')?.textContent).toContain('Escuchar y revisar')
    expect(document.querySelector('#tab-export')?.textContent).toContain('Exportar')
    expect(document.body.textContent).toContain('Elegir archivo')
    expect(document.querySelector('#panel-upload')?.hasAttribute('hidden')).toBe(false)
    expect(document.querySelector('.upload-intro')).toBeNull()
    expect(document.querySelector('#upload-title')?.textContent).toBe('Subir libro')
    expect(document.body.textContent).toContain('Arrastra tu libro aqui')
    expect(document.body.textContent).not.toMatch(/ElevenLabs|Deepgram|Fish Audio|FishAudio/i)
    expect(document.querySelector('#panel-review')?.hasAttribute('hidden')).toBe(true)
    expect(document.body.textContent).toContain('EPUB')
    expect(document.querySelectorAll('#voice-id option')).toHaveLength(3)
    expect(document.querySelector('#voice-id')?.textContent).toContain('Isabela')
    expect(document.querySelector('#voice-id')?.textContent).toContain('Carmen - España')
    expect(document.querySelector('input[type="file"]')).not.toBeNull()
    expect([...document.querySelectorAll('button')].some(button => button.textContent?.includes('Generar vista previa'))).toBe(false)
  })

  it('extracts and displays the source text after upload', async () => {
    render()
    await act(async () => {})
    await uploadTextBook()
    const previewButton = [...document.querySelectorAll('button')].find(button => button.textContent?.includes('Generar vista previa'))!
    await act(async () => previewButton.click())
    expect(document.body.textContent).toContain('Texto de la muestra')
    expect(document.body.textContent).toContain('Primera frase.')
    expect(document.body.textContent).toContain('Segunda frase.')
  })

  it('shows long books in navigable sections and keeps the real correction action available', async () => {
    const paragraphs = Array.from({ length: 115 }, (_, index) => `Párrafo de prueba ${index + 1}.`)
    vi.mocked(fetch).mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return response(capabilities) as Response
      if (url.endsWith('/api/books/structure')) return response({ sections: [{ id: 'section-1', title: 'Capítulo de prueba', category: 'main', paragraph_count: paragraphs.length, included: true, needs_review: false, reason: '' }], selected_ids: ['section-1'] }) as Response
      if (url.endsWith('/api/books/preview')) return response({
        audio_base64: '', audio_mime_type: 'audio/mpeg', transcript: paragraphs.join('\n\n'),
        chapters: [{ title: 'Capítulo de prueba', paragraphs }], title: 'Libro largo', audio_error: 'Sin audio de muestra.',
      }) as Response
      if (url.endsWith('/api/books/jobs')) return response({ job_id: 'job-long', state: 'queued', total_sentences: 115 }) as Response
      if (url.endsWith('/api/jobs/job-long')) return response({
        job_id: 'job-long', state: 'partially_completed', source_name: 'sample.txt', total_sentences: 115,
        progress: {}, sentences: [],
      }) as Response
      return response({ state: 'queued' }) as Response
    })

    render()
    await act(async () => {})
    await uploadTextBook()
    const previewButton = [...document.querySelectorAll('button')].find(button => button.textContent?.includes('Generar vista previa'))!
    await act(async () => previewButton.click())

    expect(document.querySelectorAll('.review-document-content textarea')).toHaveLength(12)
    expect(document.querySelector('#book-paragraph-0-0')).not.toBeNull()
    expect(document.querySelector('#book-paragraph-0-12')).toBeNull()
    expect(document.body.textContent).toContain('Sección 1 de 10')
    expect(document.querySelector('.export-actions button')?.textContent).toContain('Descargar audiolibro ahora')
    expect(document.querySelector('.export-actions')?.textContent).toContain('Producir con revisión de calidad')

    const nextButton = document.querySelector<HTMLButtonElement>('.review-pagination button:last-child')!
    await act(async () => nextButton.click())
    expect(document.body.textContent).toContain('Sección 2 de 10')
    expect(document.querySelector('#book-paragraph-0-12')).not.toBeNull()
    expect(document.querySelector('#book-paragraph-0-0')).toBeNull()
    expect(document.querySelector('#tab-review')?.getAttribute('aria-selected')).toBe('true')

    const currentParagraph = document.querySelector<HTMLTextAreaElement>('#book-paragraph-0-12')!
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
      setter?.call(currentParagraph, 'Texto corregido de forma persistente.')
      currentParagraph.dispatchEvent(new Event('input', { bubbles: true }))
    })
    const previousButton = document.querySelector<HTMLButtonElement>('.review-pagination button:first-child')!
    await act(async () => previousButton.click())
    await act(async () => document.querySelector<HTMLButtonElement>('.review-pagination button:last-child')!.click())
    expect(document.querySelector<HTMLTextAreaElement>('#book-paragraph-0-12')?.value).toBe('Texto corregido de forma persistente.')

    let paragraphCount = 24
    while (!document.querySelector<HTMLButtonElement>('.review-pagination button:last-child')!.disabled) {
      await act(async () => document.querySelector<HTMLButtonElement>('.review-pagination button:last-child')!.click())
      paragraphCount += document.querySelectorAll('.review-document-content textarea').length
    }
    expect(paragraphCount).toBe(115)
    expect(document.querySelector('#book-paragraph-0-114')).not.toBeNull()

    // The reviewed production run is now its own action, beside the direct download.
    const produce = [...document.querySelectorAll<HTMLButtonElement>('.export-actions button')]
      .find(button => button.textContent?.includes('Producir con revisión de calidad'))!
    await act(async () => produce.click())
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/books/jobs', expect.objectContaining({ method: 'POST', body: expect.any(FormData) }))
  })

  it('switches chapters without losing edits and keeps export as a separate workspace tab', async () => {
    vi.mocked(fetch).mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/capabilities')) return response(capabilities) as Response
      if (url.endsWith('/api/books/structure')) return response({ sections: [
        { id: 'section-1', title: 'Capítulo primero', category: 'main', paragraph_count: 1, included: true, needs_review: false, reason: '' },
        { id: 'section-2', title: 'Capítulo segundo', category: 'main', paragraph_count: 1, included: true, needs_review: false, reason: '' },
      ], selected_ids: ['section-1', 'section-2'] }) as Response
      if (url.endsWith('/api/books/preview')) return response({
        audio_base64: '', audio_mime_type: 'audio/mpeg', transcript: 'Texto del capítulo primero.\n\nTexto del capítulo segundo.',
        chapters: [
          { title: 'Capítulo primero', paragraphs: ['Texto del capítulo primero.'] },
          { title: 'Capítulo segundo', paragraphs: ['Texto del capítulo segundo.'] },
        ],
        title: 'Libro con dos capítulos', audio_error: 'Sin audio de muestra.',
      }) as Response
      return response({ state: 'queued' }) as Response
    })

    render()
    await act(async () => {})
    await uploadTextBook()
    const previewButton = [...document.querySelectorAll('button')].find(button => button.textContent?.includes('Generar vista previa'))!
    await act(async () => previewButton.click())

    expect(document.querySelector<HTMLTextAreaElement>('#book-paragraph-0-0')?.value).toBe('Texto del capítulo primero.')
    await act(async () => document.querySelectorAll<HTMLButtonElement>('.review-chapter-link')[1].click())
    expect(document.querySelector<HTMLTextAreaElement>('#book-paragraph-1-0')?.value).toBe('Texto del capítulo segundo.')
    const secondChapterInput = document.querySelector<HTMLTextAreaElement>('#book-paragraph-1-0')!
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
      setter?.call(secondChapterInput, 'Capítulo segundo editado.')
      secondChapterInput.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => document.querySelector<HTMLButtonElement>('.review-chapter-link')!.click())
    await act(async () => document.querySelectorAll<HTMLButtonElement>('.review-chapter-link')[1].click())
    expect(document.querySelector<HTMLTextAreaElement>('#book-paragraph-1-0')?.value).toBe('Capítulo segundo editado.')

    await act(async () => document.querySelector<HTMLButtonElement>('#tab-export')!.click())
    expect(document.querySelector('#panel-export')?.hasAttribute('hidden')).toBe(false)
    expect(document.querySelector('#panel-review')?.hasAttribute('hidden')).toBe(true)
    expect(document.querySelector('.export-actions button')?.textContent).toContain('Descargar audiolibro ahora')
    expect(document.querySelector('.export-actions')?.textContent).toContain('Producir con revisión de calidad')
    await act(async () => document.querySelector<HTMLButtonElement>('#tab-review')!.click())
    expect(document.querySelector<HTMLTextAreaElement>('#book-paragraph-1-0')?.value).toBe('Capítulo segundo editado.')
  })

  it('starts the resumable job and exposes unresolved sentences for human review', async () => {
    render()
    await act(async () => {})
    await uploadTextBook()
    const previewButton = [...document.querySelectorAll('button')].find(button => button.textContent?.includes('Generar vista previa'))!
    await act(async () => previewButton.click())
    const generateButton = [...document.querySelectorAll<HTMLButtonElement>('.export-actions button')]
      .find(button => button.textContent?.includes('Producir con revisión de calidad'))!
    await act(async () => generateButton.click())
    expect(document.body.textContent).toContain('Producción del audiolibro')
    expect(document.body.textContent).toContain('Párrafos que necesitan tu revisión (1)')
    expect(document.body.textContent).toContain('Lo que se oye en el audio')
    const acceptButton = [...document.querySelectorAll('button')].find(button => button.textContent?.includes('La locución es correcta'))!
    await act(async () => acceptButton.click())
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/jobs/job-1/review', expect.objectContaining({ method: 'POST' }))
  })
})
