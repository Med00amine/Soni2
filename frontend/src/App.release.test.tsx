import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ReleasePanel } from './App'

let root: Root

const passed = {
  state: 'PASSED', label: 'Validado', blockers: [], reviews: [],
  uncertainty: ['Un reconocimiento automático que coincide con el texto no demuestra que la pronunciación sea correcta.'],
  dimensions: {
    technical: { status: 'passed', segments: 24, verified: 24, quality_retakes: 0 },
    source: { status: 'passed', coverage: { ratio: 1 }, open_warnings: [] },
    content: { status: 'passed', wer: 0.015, reference_words: 540 },
    audio: { status: 'passed', suspicious: 0 },
    package: { status: 'passed', structural: 'passed', dtd: 'passed', completeness: 'passed' },
    human: { decisions: 0, listening_recommended: ['a', 'b'] },
  },
}

function render(release: typeof passed | Record<string, unknown>, props: Partial<Parameters<typeof ReleasePanel>[0]> = {}) {
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  const handlers = { onReviewerName: vi.fn(), onConfirmed: vi.fn(), onAcknowledge: vi.fn(), onRelease: vi.fn() }
  act(() => {
    root.render(<ReleasePanel release={release as never} reviewerName="" confirmed={false} {...handlers} {...props}/>)
  })
  return handlers
}

afterEach(() => act(() => root?.unmount()))

describe('the release panel', () => {
  it('shows every quality dimension separately and what is not proven', () => {
    render(passed)
    const text = document.body.textContent ?? ''
    for (const label of ['Generación técnica', 'Texto de origen completo', 'Contenido (reconocimiento automático)',
      'Integridad del audio', 'Paquete DAISY', 'Revisión humana']) expect(text).toContain(label)
    expect(text).toContain('1.50 % de 540 palabras')
    expect(text).toContain('Lo que estas comprobaciones no demuestran')
    expect(text).not.toMatch(/100 ?% (libre de errores|sin errores)/i)
  })

  it('only lets a named person publish, after confirming the limits', () => {
    const unnamed = render(passed, { confirmed: true })
    const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'Publicar el audiolibro')!
    expect(button.disabled).toBe(true)
    act(() => root.unmount())
    const named = render(passed, { confirmed: true, reviewerName: 'Ana' })
    const enabled = [...document.querySelectorAll('button')].find(item => item.textContent === 'Publicar el audiolibro')!
    expect(enabled.disabled).toBe(false)
    act(() => { enabled.click() })
    expect(named.onRelease).toHaveBeenCalledOnce()
    expect(unnamed.onRelease).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('he escuchado los 2 párrafos recomendados')
  })

  it('never offers publication for a blocked book and lists why', () => {
    render({ ...passed, state: 'BLOCKED', label: 'Bloqueado',
      blockers: ['La página 3 es una imagen sin capa de texto y no hay OCR disponible.'] })
    expect(document.body.textContent).toContain('La página 3 es una imagen sin capa de texto')
    expect([...document.querySelectorAll('button')].some(item => item.textContent === 'Publicar el audiolibro')).toBe(false)
    expect(document.querySelector('.release-bad')).not.toBeNull()
  })

  it('lets a named reviewer confirm an extraction warning', () => {
    const handlers = render({ ...passed, state: 'NEEDS_REVIEW', label: 'Requiere revisión', reviews: ['Extracción: faltan palabras'],
      dimensions: { ...passed.dimensions, source: { status: 'needs_review', coverage: { ratio: 0.99 },
        open_warnings: [{ id: 'w1', code: 'unaccounted_words', message: '3 palabras del original no aparecen', text: 'hierro' }] } } },
    { reviewerName: 'Ana' })
    const confirm = [...document.querySelectorAll('button')].find(item => item.textContent === 'He comprobado el original')!
    act(() => { confirm.click() })
    expect(handlers.onAcknowledge).toHaveBeenCalledWith('w1')
  })
})
