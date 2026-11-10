// The reader's changes to the book in step 2, as the server stores them
// (backend app/book_edits.py). A change belongs to a place in the book that
// does not move when other places change:
//
//   "section-7:12"   paragraph 12 of section 7 of the narration plan
//   "section-7:12+"  text the reader added right after it ("-1+": before the first)
//
// A paragraph edited phrase by phrase is stored as the list of its phrases, so
// a phrase nobody touched keeps the exact text whose audio is already paid
// for; an empty list deletes it. A note is edited as a whole text. A section
// title can be corrected or taken out of the narration.

export type UnitSource = {
  chapter?: number
  paragraph?: number
  slot?: string
  mode?: 'units' | 'text' | 'title'
  part?: number
  parts?: number
  section?: string
  section_id?: string
  text?: string
}

export type BookEdits = {
  slots: Record<string, string[] | string>
  titles: Record<string, string>
  deleted_titles: string[]
}

export const NO_EDITS: BookEdits = { slots: {}, titles: {}, deleted_titles: [] }

export type EditableUnit = { role: string; text: string; source?: UnitSource | null }
export type EditableSection = {
  kind: string
  level: number
  title: string
  titleSource?: UnitSource | null
  deletable?: boolean
  units: EditableUnit[]
}

const INITIAL_KINDS = new Set(['once_title', 'once_bibliographic', 'once_index'])

export function normalizeEdits(value: unknown): BookEdits {
  const raw = (value && typeof value === 'object' ? value : {}) as Partial<BookEdits>
  return {
    slots: { ...(raw.slots ?? {}) },
    titles: { ...(raw.titles ?? {}) },
    deleted_titles: [...(raw.deleted_titles ?? [])],
  }
}

export function canEdit(unit: EditableUnit) {
  const mode = unit.source?.mode
  return mode === 'units' ? Boolean(unit.source?.slot) : mode === 'text' ? Boolean(unit.source?.slot)
    : mode === 'title' ? Boolean(unit.source?.section) : false
}

// The phrases of one paragraph as they are narrated now, in order.
export function slotUnits(sections: EditableSection[], slot: string): string[] {
  return sections.flatMap(section => section.units)
    .filter(unit => unit.source?.slot === slot && unit.source?.mode === 'units')
    .map(unit => unit.text)
}

function withSlot(edits: BookEdits, slot: string, value: string[] | string): BookEdits {
  return { ...edits, slots: { ...edits.slots, [slot]: value } }
}

function addedAfter(edits: BookEdits, slot: string): string[] {
  const value = edits.slots[`${slot}+`]
  return Array.isArray(value) ? value : typeof value === 'string' && value.trim() ? [value] : []
}

// What the reader may write in the box when correcting a phrase.
export function editableText(unit: EditableUnit) {
  const source = unit.source
  return source?.mode === 'text' || source?.mode === 'title' ? source.text ?? unit.text : unit.text
}

export function editUnit(edits: BookEdits, sections: EditableSection[], unit: EditableUnit, text: string): BookEdits {
  const source = unit.source
  const value = text.replace(/\s+/g, ' ').trim()
  if (!source || !value) return edits
  if (source.mode === 'title' && source.section) return { ...edits, titles: { ...edits.titles, [source.section]: value } }
  if (!source.slot) return edits
  if (source.mode === 'text') return withSlot(edits, source.slot, value)
  const phrases = slotUnits(sections, source.slot)
  phrases[source.part ?? 0] = value
  return withSlot(edits, source.slot, phrases)
}

export function deleteUnit(edits: BookEdits, sections: EditableSection[], unit: EditableUnit): BookEdits {
  const source = unit.source
  if (!source) return edits
  if (source.mode === 'title' && source.section) {
    return edits.deleted_titles.includes(source.section) ? edits
      : { ...edits, deleted_titles: [...edits.deleted_titles, source.section] }
  }
  if (!source.slot) return edits
  if (source.mode === 'text') return withSlot(edits, source.slot, [])
  const phrases = slotUnits(sections, source.slot)
  phrases.splice(source.part ?? 0, 1)
  return withSlot(edits, source.slot, phrases)
}

export function insertAfter(edits: BookEdits, sections: EditableSection[], unit: EditableUnit, text: string): BookEdits {
  const source = unit.source
  const value = text.replace(/\s+/g, ' ').trim()
  if (!source || !value) return edits
  if (source.mode === 'title' && source.section) {
    const slot = `${source.section}:-1`
    return withSlot(edits, `${slot}+`, [value, ...addedAfter(edits, slot)])
  }
  if (!source.slot) return edits
  if (source.mode === 'units') {
    const phrases = slotUnits(sections, source.slot)
    phrases.splice((source.part ?? 0) + 1, 0, value)
    return withSlot(edits, source.slot, phrases)
  }
  // A note or a subchapter heading: the new phrase is a paragraph of its own after it.
  return withSlot(edits, `${source.slot}+`, [value, ...addedAfter(edits, source.slot)])
}

// A chapter and, for a chapter of level 1, the subchapters nested under it.
export function sectionFamily(sections: EditableSection[], index: number): number[] {
  const section = sections[index]
  if (!section) return []
  const family = [index]
  if (section.level === 1 && !INITIAL_KINDS.has(section.kind)) {
    for (let next = index + 1; next < sections.length && sections[next].level === 2
      && !INITIAL_KINDS.has(sections[next].kind); next += 1) family.push(next)
  }
  return family
}

export function deleteSection(edits: BookEdits, sections: EditableSection[], index: number): BookEdits {
  let next = { ...edits, slots: { ...edits.slots }, deleted_titles: [...edits.deleted_titles] }
  for (const position of sectionFamily(sections, index)) {
    const section = sections[position]
    if (!section.deletable) continue
    for (const unit of section.units) {
      const slot = unit.source?.slot
      if (slot && (unit.source?.mode === 'units' || unit.source?.mode === 'text')) next.slots[slot] = []
    }
    const title = section.titleSource
    if (title?.mode === 'text' && title.slot) next.slots[title.slot] = []
    if (title?.mode === 'title' && title.section && !next.deleted_titles.includes(title.section)) {
      next = { ...next, deleted_titles: [...next.deleted_titles, title.section] }
    }
  }
  return next
}

export function changeCount(edits: BookEdits) {
  return Object.keys(edits.slots).length + Object.keys(edits.titles).length + edits.deleted_titles.length
}
