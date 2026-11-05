import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AudiobookEditor, { type EditorModel, type EditorSection } from './AudiobookEditor'

let root: Root

function section(index: number, title: string, source: boolean, deletable: boolean): EditorSection {
  return {
    index, chapterIndex: index, fileNumber: index + 1, key: `s${index}`, kind: 'chapter', level: 1, title, headingSpoken: true,
    pageStart: '', pageEnd: '', status: 'planned', deletable,
    units: [{ key: `${index}-1`, text: `Frase de ${title}.`, role: 'body', pageBreaks: [], ...(source ? { source: { mode: 'units', slot: `section-${index}:0`, part: 0, parts: 1 } } : {}) }],
  } as unknown as EditorSection
}

function model(mode: 'outline' | 'job'): EditorModel {
  const edit = mode === 'outline'
  return { mode, once: false, pages: 0, warnings: [], sections: [section(0, 'Uno', edit, edit), section(1, 'Dos', edit, edit)] } as unknown as EditorModel
}

async function render(extra: Record<string, unknown>, currentModel: EditorModel) {
  const props = { loading: false, error: '', apiBase: '', canProduce: false, producing: false, onProduce: () => {}, renderInspector: () => null, showPlanilla: false, ...extra }
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root')!)
  await act(async () => { root.render(<AudiobookEditor {...props} model={currentModel}/>) })
  return (next: EditorModel) => act(async () => { root.render(<AudiobookEditor {...props} model={next}/>) })
}

const button = (label: string) => Array.from(document.querySelectorAll('button'))
  .find(item => item.getAttribute('aria-label') === label || item.textContent?.trim() === label)!

afterEach(() => { act(() => root?.unmount()); vi.restoreAllMocks() })

describe('deleting from step 2', () => {
  it('puts the chapter button under the structure and the phrase button left of the phrase', async () => {
    await render({ onDeleteSection: () => {}, onDeleteUnit: () => {} }, model('outline'))
    expect(document.querySelector('.ed-tree-panel')?.contains(button('Eliminar capítulo'))).toBe(true)
    const phrase = button('Eliminar la frase 1')
    expect(phrase.closest('.ed-phrase-lead')).toBeTruthy()
    expect(document.querySelector('.ed-phrase-main')!.compareDocumentPosition(phrase) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
  })

  it('keeps the chapter button in view on an initial file, disabled, and says why', async () => {
    const current = model('outline')
    current.sections[0] = { ...current.sections[0], kind: 'once_title', deletable: false }
    await render({ onDeleteSection: () => {} }, current)
    expect(button('Eliminar capítulo').disabled).toBe(true)
    expect(document.querySelector('.ed-tree-actions')?.textContent).toContain('no se puede eliminar')
  })

  it('shows both buttons during a production, disabled, and says why', async () => {
    await render({ jobState: 'running' }, model('job'))
    expect(button('Eliminar capítulo').disabled).toBe(true)
    expect(button('Eliminar la frase 1').disabled).toBe(true)
    expect(document.querySelector('.ed-tree-actions')?.textContent).toContain('cuando termine la producción')
  })

  it('opens the text from the produced audio and asks to confirm the phrase there', async () => {
    const onEditText = vi.fn()
    const rerender = await render({ jobState: 'completed', onEditText, onDeleteUnit: () => {} }, model('job'))
    await act(async () => { button('Eliminar la frase 1').click() })
    expect(onEditText).toHaveBeenCalledOnce()
    await rerender(model('outline'))
    expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('¿Eliminar la frase 1 del audiolibro?')
  })

  it('opens the text from the produced audio and asks to confirm the chapter there', async () => {
    const onEditText = vi.fn()
    const rerender = await render({ jobState: 'completed', onEditText, onDeleteSection: () => {} }, model('job'))
    await act(async () => { button('Eliminar capítulo').click() })
    await rerender(model('outline'))
    expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('¿Eliminar «Uno» del audiolibro?')
  })
})
