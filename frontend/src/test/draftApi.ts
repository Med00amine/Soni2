// A small stand-in for the server's /api/drafts: it keeps one book, stores what
// the page saves and answers with the outline those edits give, the way
// backend/app/book_edits.py does. backend/tests/test_drafts_and_edits.py checks
// the real one.

export type FakeChapter = { id: string; title: string; paragraphs: string[] }
type Structure = { sections: { id: string; title: string; paragraph_count: number }[]; selected_ids: string[] }
export type FakeBook = { chapters?: FakeChapter[]; outline?: unknown; title?: string }
type Edits = { slots: Record<string, string[] | string>; titles: Record<string, string>; deleted_titles: string[] }

type Server = {
  revision: number
  state: Record<string, unknown>
  structure: Structure
  book: FakeBook
  lastJob: unknown
  sourceName: string
  saves: Record<string, unknown>[]
}

let server: Server | undefined

export function forgetDraft() {
  server = undefined
}

export function savedStates() {
  return server?.saves ?? []
}

export function lastSavedState() {
  const saves = savedStates()
  return saves[saves.length - 1] as Record<string, unknown> | undefined
}

export function rememberJob(job: { job_id: string; state: string; draft_revision: number }) {
  if (server) server.lastJob = job
}

function outlineFrom(chapters: FakeChapter[], title: string, edits: Edits) {
  const sections: unknown[] = []
  const texts: { title: string; paragraphs: string[]; section_id: string }[] = []
  chapters.forEach((chapter, chapterIndex) => {
    const units: unknown[] = []
    const paragraphs: string[] = []
    const place = (slot: string, value: string[] | string | undefined, fallback: string[]) => {
      const phrases = value === undefined ? fallback : typeof value === 'string' ? [value] : value
      if (phrases.length) paragraphs.push(phrases.join('\n\n'))
      phrases.forEach((text, part) => units.push({
        role: 'text', text, page_breaks: [], note: null, part,
        source: { chapter: chapterIndex, paragraph: paragraphs.length - 1, slot, mode: 'units', part, parts: phrases.length },
      }))
    }
    place(`${chapter.id}:-1+`, edits.slots[`${chapter.id}:-1+`], [])
    chapter.paragraphs.forEach((paragraph, number) => {
      const slot = `${chapter.id}:${number}`
      place(slot, edits.slots[slot], [paragraph])
      place(`${slot}+`, edits.slots[`${slot}+`], [])
    })
    if (!units.length && edits.deleted_titles.includes(chapter.id)) return
    const heading = edits.titles[chapter.id] ?? chapter.title
    texts.push({ title: heading, paragraphs, section_id: chapter.id })
    sections.push({
      index: sections.length, key: `chapter-${chapterIndex + 1}`, kind: 'chapter', level: 1, title: heading,
      heading_spoken: false, page_start: '', page_end: '', deletable: true,
      title_source: { mode: 'title', section: chapter.id, text: heading }, units,
    })
  })
  return { once: false, title, author: '', edition: '', publication_year: '', publisher: '', chapters: texts,
           sections, warnings: [], planilla: null }
}

function view() {
  const current = server as Server
  const edits = { slots: {}, titles: {}, deleted_titles: [], ...(current.state.edits as Partial<Edits>) }
  const title = current.book.title ?? ''
  const chapters = current.book.chapters ?? current.structure.sections.map(section => ({
    id: section.id, title: section.title,
    paragraphs: Array.from({ length: section.paragraph_count }, (_, index) => `Texto ${index + 1} de ${section.title}.`),
  }))
  return {
    draft: { draft_id: 'draft-1', source_name: current.sourceName, source_size: 10, revision: current.revision, state: current.state,
             updated_at: '2026-11-04T10:00:00Z', last_job: current.lastJob ?? null },
    plan: { title, sections: current.structure.sections, selected_ids: current.structure.selected_ids },
    outline: current.book.outline ?? outlineFrom(chapters, title, edits),
    outline_error: '',
  }
}

// The answer for a request to /api/drafts, or undefined for any other URL.
export function draftResponse(url: string, init: RequestInit | undefined, structure: Structure, book: FakeBook = {}) {
  if (!url.includes('/api/drafts')) return undefined
  const method = init?.method ?? 'GET'
  if (method === 'POST') {
    const file = (init?.body as FormData | undefined)?.get?.('file') as File | null
    server = {
      revision: 1, structure, book, lastJob: null, saves: [], sourceName: file?.name ?? 'libro.txt',
      state: { pages: String((init?.body as FormData | undefined)?.get?.('pages') ?? ''), selected_ids: null, metadata: {},
               planilla: {}, once_enabled: true, create_sections: false,
               edits: { slots: {}, titles: {}, deleted_titles: [] }, voice_id: '' },
    }
  } else if (method === 'PUT' && server) {
    const body = JSON.parse(String(init?.body)) as { state: Record<string, unknown>; revision: number }
    server.state = body.state
    server.revision = body.revision + 1
    server.saves.push(body.state)
  }
  return server ? view() : undefined
}
