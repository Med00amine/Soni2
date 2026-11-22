import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { BookOpen, Upload, Download, Volume2, CheckCircle2 } from 'lucide-react'
import './production-status.css'
// Loaded after, so the modern tokens win over the older hardcoded palette.
import './export-modern.css'
import AudiobookEditor, { KIND_LABEL, formatClock, type EditorModel, type EditorSection, type EditorUnit } from './AudiobookEditor'
import { NO_EDITS, addSection, changeCount, deleteSection, deleteUnit, editUnit, insertAfter, normalizeEdits, type BookEdits, type UnitSource } from './bookEdits'
import { BuildStages, ExportHeader, ExportSection, Facts, type ExportTone } from './ExportPanel'
import { changesSince, describeChanges, directStages, formatBytes, formatSeconds, formatTotalTime, jobStages, type DirectProgress, type PackageView, type PipelineView } from './exportView'

const API = (import.meta.env.VITE_API_URL ?? '').replace(/\/+$/, '')

type Capabilities = {
  daisy_profile: string
  tts_provider: string
  asr_provider: string
  tts_configured: boolean
  voice: string
  voice_name: string
  voice_language: string
  voice_is_peninsular: boolean
  voice_accent_verified?: boolean
  requires_peninsular_voice: boolean
  voice_speed_control: boolean
  voice_id_configurable: boolean
  audio_review_configured: boolean
  offline_mock_mode: boolean
  chapter_audio: boolean
  chapter_smil: boolean
  full_text_in_package: boolean
  ncx_navigation: boolean
  ncc_navigation: boolean
  once_intro: boolean
  once_bibliographic_audio: boolean
  automatic_audio_review: boolean
  retakes: boolean
  max_quality_retakes: number
  max_technical_attempts: number
  sentence_audio_cache: boolean
  official_daisy_pipeline_validation: boolean
}

type TimedPhrase = { text: string; start: number; end: number; text_start: number; text_end: number }
type BookChapter = { title: string; paragraphs: string[] }
type NarrationSection = { id: string; title: string; category: string; paragraph_count: number; included: boolean; needs_review: boolean; reason: string }
type BookMetadata = { title: string; author: string; edition: string; publication_year: string; publisher: string }
type VoiceOption = { voice_id: string; name: string; provider?: string; language?: string; peninsular?: boolean; accent_verified?: boolean; labels?: Record<string, string> }
type QaIssue = { kind: string; category?: string; difference_type?: string; expected?: string; recognized?: string; detail?: string; reason?: string; expected_start?: number; expected_end?: number; recognized_start?: number; recognized_end?: number; audio_start?: number; audio_end?: number; chapter_time?: number; book_time?: number | null }
type AudioReview = { recognized_text: string; recognizer?: string; verdict?: string; confidence?: number; issues: QaIssue[]; review_note: string }
type Diagnosis = { origin?: string; severity?: string; action?: string; recommendation?: string; reasons?: string[] }
type UnitProvenance = { role?: string; page_breaks?: string[]; note?: number | null; kind?: string; level?: number; source?: UnitSource | null; page?: string }
type ProductionSentence = { sentence_id: string; chapter_index: number; chapter_title: string; sequence: number; text: string; state: string; recognized_text: string; issues: QaIssue[]; audio_path?: string | null; quality_retakes: number; technical_attempts: number; last_error: string; reused_audio?: number | boolean; updated_at?: string; spoken_text?: string; diagnosis?: Diagnosis; verification?: string; is_heading?: number | boolean; provenance?: UnitProvenance; acoustic?: { duration_seconds?: number; stream?: { duration_seconds?: number } } }
// The ONCE production sheet. Only what the reader sets is sent; the server
// fills the rest with MODELO ONCE's rules (legal text, production line...).
type Planilla = Partial<{
  absys: string; legal_text: string; bibliographic_heading: string; edition_line: string; publisher_line: string
  isbn: string; production_line: string; production_date: string; file1_page: string; file2_page: string
  index_page: string; index_depth: number; closing: string; note_open: string; note_close: string
  index_heading: string; index_end: string; chapter_pages: Record<string, string>
  expected_files: { number: number; heading: string }[]
}>
type ResolvedPlanilla = Planilla & { title?: string; author?: string; missing?: string[] }
type OutlineUnit = { role: string; text: string; page_breaks: string[]; note?: number | null; source?: UnitSource | null }
type OutlineSection = { index: number; key: string; kind: string; level: number; title: string; heading_spoken: boolean; page_start: string; page_end: string; title_source?: UnitSource | null; deletable?: boolean; units: OutlineUnit[] }
type EditorWarning = { code: string; message: string; section?: number | null }
type BookOutline = { once: boolean; title: string; author: string; edition: string; publication_year: string; publisher: string; chapters: BookChapter[]; sections: OutlineSection[]; warnings: EditorWarning[]; planilla?: ResolvedPlanilla | null }
type StructureUnit = { sentence_id: string; role: string; text: string; state: string; page_breaks: string[]; note?: number | null; source?: UnitSource | null; duration?: number | null; clip?: [number, number] | null; issues: number; has_audio: boolean; updated_at?: string; editable: boolean }
type StructureSection = { index: number; file_number: number; key: string; kind: string; level: number; title: string; heading_spoken: boolean; page_start: string; page_end: string; status: string; duration?: number | null; duration_final: boolean; audio_url?: string | null; units: StructureUnit[] }
type AudioFormatInfo = { codec: string; sample_rate: number; channels: number; bitrate_kbps?: number[] | null }
type JobStructure = { job_id: string; state: string; once: boolean; planilla?: ResolvedPlanilla | null; warnings: EditorWarning[]; assembled: boolean; total_duration?: number | null; pages: number; audio_format?: { segments?: AudioFormatInfo | null; final?: AudioFormatInfo | null }; sections: StructureSection[] }
type ExtractionWarning = { id: string; code: string; message: string; location?: string; text?: string }
type ReleaseStatus = {
  state: string
  label: string
  blockers: string[]
  reviews: string[]
  uncertainty: string[]
  releasable?: boolean
  released?: { at: string; by: string; note?: string } | null
  dimensions?: {
    technical?: { status?: string; segments?: number; verified?: number; failed?: number; quality_retakes?: number }
    source?: { status?: string; coverage?: { ratio?: number; unaccounted_words?: number }; open_warnings?: ExtractionWarning[]; ocr_pages?: number }
    content?: { status?: string; wer?: number | null; reference_words?: number; awaiting_review?: number }
    audio?: { status?: string; failed?: string[]; suspicious?: number }
    package?: { status?: string; structural?: string; dtd?: string; completeness?: string }
    human?: { decisions?: number; listening_recommended?: string[] }
  }
}
type FaultLocation = { sentence_id: string; chapter: number; chapter_title: string; paragraph: number; kind: string; expected: string; heard: string; reason: string; decision: string; paragraph_state: string; paragraph_time: number; chapter_file: string; chapter_time: number; chapter_clock: string; book_file: string; book_time: number | null; book_clock: string | null }
type ProductionJob = { fault_locations?: FaultLocation[]; job_id: string; state: string; source_name: string; total_sentences: number; progress: Record<string, number>; assembly?: { stage: string; done: number; total: number } | null; sentences: ProductionSentence[]; error?: string; voice_id?: string; voice_name?: string; voice_language?: string; release?: ReleaseStatus; pipeline?: PipelineView | null; package?: PackageView | null; completed_at?: string | null }
type PronunciationSuggestion = { respelling: string; label: string; reason: string }
type PronunciationAnalysis = {
  word: string
  ipa: string
  syllables: string[]
  stressed: number
  looks_spanish: boolean
  suggestions: PronunciationSuggestion[]
  saved_respelling: string
}
type WorkspaceTab = 'upload' | 'review' | 'export'
// Everything the reader chose and changed for this book, saved on the server.
type DraftState = { pages: string; selected_ids: string[] | null; metadata: Partial<BookMetadata>; planilla: Planilla | null; once_enabled: boolean; create_sections: boolean; edits: BookEdits; voice_id: string }
type DraftView = {
  draft: { draft_id: string; source_name: string; source_size: number; revision: number; state: DraftState; updated_at: string; last_job?: { job_id: string; state: string; draft_revision: number } | null }
  plan: { title: string; sections: NarrationSection[]; selected_ids: string[] }
  outline: BookOutline | null
  outline_error: string
}
type PackageTaskStatus = DirectProgress & { task_id: string; state: string; total: number; done: number; reused: number; detail?: string; reused_package?: boolean; package?: PackageView | null }
// The direct download's ZIP, and the book state it was built from.
type DirectZip = { taskId: string; signature: string; status: PackageTaskStatus }

const PACKAGE_TASK_KEY = 'soni2-active-package-task'
// Whether step 3 builds the ZIP with automatic quality review (the default) or
// as a direct, unverified download: a preference of this browser.
const REVIEW_MODE_KEY = 'soni2-export-review'

function storedReviewMode() {
  try {
    return window.localStorage.getItem(REVIEW_MODE_KEY) !== 'off'
  } catch {
    return true
  }
}
// The book being prepared and the step it was left on, so a reload finds them.
const DRAFT_KEY = 'soni2-book-draft'
const TAB_KEY = 'soni2-workspace-tab'
const SAVE_DELAY_MS = 500

function publicCopy(value: string) { return value.replace(/Fish Audio|FishAudio|Deepgram|ElevenLabs/gi, 'servicio de voz') }

function providerLabel(provider?: string) {
  if (provider === 'mock') return 'simulado (sin coste)'
  return provider ? 'configurado' : 'sin configurar'
}

// Mark the exact spans the comparison flagged, so a reviewer reads one
// sentence with the differences highlighted instead of comparing two
// paragraphs by eye. An empty span is an omission or an addition, shown as a
// caret at the position where the words belong.
function highlightDifferences(text: string, ranges: { start?: number; end?: number }[]): ReactNode {
  if (!text) return null
  const spans = ranges
    .filter(range => typeof range.start === 'number' && typeof range.end === 'number'
      && range.start >= 0 && range.end <= text.length && range.end >= range.start)
    .map(range => ({ start: range.start as number, end: range.end as number }))
    .sort((left, right) => left.start - right.start)
  if (!spans.length) return text
  const parts: ReactNode[] = []
  let cursor = 0
  spans.forEach((span, index) => {
    const start = Math.max(span.start, cursor)
    const end = Math.max(span.end, start)
    if (start > cursor) parts.push(text.slice(cursor, start))
    if (end > start) parts.push(<mark key={`diff-${index}`}>{text.slice(start, end)}</mark>)
    else parts.push(<span key={`gap-${index}`} className="diff-gap" title="Aquí falta o sobra texto">·</span>)
    cursor = end
  })
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}

function sentenceStateLabel(state: string) {
  return ({
    pending: 'En cola',
    generating: 'Generando',
    verified: 'Correctas',
    needs_human_review: 'Requieren tu revisión',
    unresolved: 'Sin resolver',
    failed: 'Con fallo técnico',
  } as Record<string, string>)[state] ?? state
}

// Start an audio element just before a moment, so the word is heard in context.
function playFrom(audioId: string, seconds: number) {
  const audio = document.getElementById(audioId) as HTMLAudioElement | null
  if (!audio) return
  const start = () => { audio.currentTime = Math.max(0, seconds - 0.8); void audio.play().catch(() => undefined) }
  if (audio.readyState >= 1) start()
  else { audio.preload = 'auto'; audio.addEventListener('loadedmetadata', start, { once: true }); audio.load() }
}

const RELEASE_STATE_CLASS: Record<string, string> = {
  PASSED: 'release-ok', RELEASED: 'release-ok', NEEDS_REVIEW: 'release-warn', BLOCKED: 'release-bad',
}

function dimensionStatus(status?: string) {
  return ({ passed: 'superado', failed: 'fallido', needs_review: 'requiere revisión', simulated: 'audio simulado',
            partial: 'parcial', in_progress: 'en curso', not_run: 'no realizado' } as Record<string, string>)[status ?? ''] ?? (status || '—')
}

function originLabel(origin?: string) {
  return ({ synthesis: 'la locución', audio: 'el archivo de audio', normalization: 'la lectura de una expresión normalizada',
            source: 'el texto de origen', asr: 'el reconocimiento automático', technical: 'un fallo técnico' } as Record<string, string>)[origin ?? ''] ?? ''
}

// A refusal from the server can carry the reasons as a list; show them.
function errorDetail(detail: unknown, fallback: string) {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const { message, blockers = [], reviews = [] } = detail as { message?: string; blockers?: string[]; reviews?: string[] }
    return [message, ...blockers, ...reviews].filter(Boolean).join(' ') || fallback
  }
  return fallback
}

function qaIssueLabel(category?: string, kind?: string) {
  if (category === 'audio_defect') return 'Posible problema de audio'
  if (category === 'ambiguous_expression') return 'Expresión ambigua; requiere revisión'
  if (category === 'likely_mismatch') return 'Diferencia probable; revisar'
  return kind === 'insert' ? 'Posible palabra añadida' : kind === 'delete' ? 'Posible palabra omitida' : 'Posible sustitución'
}

function isDraftView(value: unknown): value is DraftView {
  const view = value as DraftView | undefined
  return Boolean(view && typeof view === 'object' && view.draft?.draft_id && view.draft.state && view.plan
    && (view.outline === null || (Array.isArray(view.outline?.sections)
      && view.outline?.sections.every(section => section && Array.isArray(section.units)))))
}

function filledMetadata(metadata: BookMetadata): Partial<BookMetadata> {
  // Only what is filled in: an empty title would override the one in the document.
  return Object.fromEntries(Object.entries(metadata).filter(([, value]) => value.trim()))
}

function isStructure(value: unknown): value is JobStructure {
  const structure = value as JobStructure | undefined
  return Boolean(structure && typeof structure === 'object' && Array.isArray(structure.sections)
    && structure.sections.every(section => section && Array.isArray(section.units)))
}

function countPages(sections: EditorSection[]) {
  return sections.reduce((sum, section) => sum + section.units.reduce((total, unit) => total + unit.pageBreaks.length, 0), 0)
}

// The editor before production: the structure the server will narrate.
function modelFromOutline(outline: BookOutline): EditorModel {
  const sections: EditorSection[] = outline.sections.map((section, index) => ({
    index, chapterIndex: section.index, fileNumber: index + 1, key: section.key, kind: section.kind,
    level: section.level, title: section.title, headingSpoken: section.heading_spoken,
    pageStart: section.page_start ?? '', pageEnd: section.page_end ?? '', status: 'planned',
    titleSource: section.title_source ?? null, deletable: Boolean(section.deletable),
    units: section.units.map((unit, position) => ({
      key: `${section.key}:${position}`, role: unit.role, text: unit.text, pageBreaks: unit.page_breaks ?? [],
      note: unit.note, source: unit.source ?? null, editable: Boolean(unit.source?.mode),
    })),
  }))
  return { mode: 'outline', once: outline.once, sections, warnings: outline.warnings ?? [], pages: countPages(sections) }
}

function unitDuration(sentence?: ProductionSentence) {
  return sentence?.acoustic?.stream?.duration_seconds ?? sentence?.acoustic?.duration_seconds ?? null
}

function sectionStatus(states: string[]) {
  if (states.includes('failed')) return 'failed'
  if (states.some(state => state === 'needs_human_review' || state === 'unresolved')) return 'needs_review'
  if (states.includes('generating')) return 'generating'
  if (states.includes('pending')) return 'pending'
  return 'verified'
}

// The editor during and after production. The job's structure gives the files,
// pages and measured clips; the job's own status, polled every second, gives
// each unit's state the moment it changes.
function modelFromJob(job: ProductionJob, structure?: JobStructure): EditorModel {
  const live = new Map(job.sentences.map(sentence => [sentence.sentence_id, sentence]))
  if (structure && isStructure(structure) && structure.job_id === job.job_id) {
    const sections: EditorSection[] = structure.sections.map((section, index) => {
      const units = section.units.map((unit, position) => {
        const current = live.get(unit.sentence_id)
        return {
          key: unit.sentence_id || `${section.key}:${position}`, role: unit.role, text: current?.text ?? unit.text,
          pageBreaks: unit.page_breaks ?? [], note: unit.note, source: unit.source ?? null, sentenceId: unit.sentence_id,
          state: current?.state ?? unit.state, duration: unitDuration(current) ?? unit.duration ?? null,
          // A clip belongs to the audio it was measured in: a unit narrated
          // again since then is played on its own until the section is rebuilt.
          clip: current?.updated_at && unit.updated_at && current.updated_at !== unit.updated_at ? null : unit.clip ?? null,
          issues: current ? current.issues.length : unit.issues, hasAudio: current ? Boolean(current.audio_path) : unit.has_audio,
          editable: unit.editable, updatedAt: current?.updated_at ?? unit.updated_at,
        }
      })
      const final = Boolean(section.audio_url && units.every(unit => unit.clip))
      return {
        index, chapterIndex: section.index, fileNumber: section.file_number ?? index + 1, key: section.key,
        kind: section.kind, level: section.level, title: section.title, headingSpoken: section.heading_spoken,
        pageStart: section.page_start ?? '', pageEnd: section.page_end ?? '', status: sectionStatus(units.map(unit => unit.state ?? 'pending')),
        duration: final ? section.duration : units.every(unit => !unit.hasAudio || unit.duration)
          ? units.reduce((sum, unit) => sum + (unit.duration ?? 0), 0) || null : section.duration,
        durationFinal: final, audioUrl: final ? section.audio_url : null, units,
      }
    })
    const format = structure.audio_format?.final ?? structure.audio_format?.segments ?? null
    return {
      mode: 'job', once: structure.once, sections, warnings: structure.warnings ?? [], pages: structure.pages,
      totalDuration: sections.every(section => section.duration) ? sections.reduce((sum, section) => sum + (section.duration ?? 0), 0) : null,
      audioFormat: format, audioFormatFinal: Boolean(structure.audio_format?.final), assembled: structure.assembled,
    }
  }
  // The job's own units, grouped by section, when its structure is not available.
  const groups = new Map<number, ProductionSentence[]>()
  for (const sentence of job.sentences) groups.set(sentence.chapter_index, [...(groups.get(sentence.chapter_index) ?? []), sentence])
  const sections: EditorSection[] = [...groups.entries()].sort(([left], [right]) => left - right).map(([chapterIndex, units], index) => {
    const provenance = units[0]?.provenance ?? {}
    return {
      index, chapterIndex, fileNumber: index + 1, key: `chapter-${chapterIndex + 1}`, kind: provenance.kind ?? 'chapter',
      level: provenance.level ?? 1, title: units[0]?.chapter_title || `Sección ${index + 1}`,
      headingSpoken: units.some(unit => unit.is_heading), pageStart: '', pageEnd: '',
      status: sectionStatus(units.map(unit => unit.state)),
      duration: units.every(unit => !unit.audio_path || unitDuration(unit)) ? units.reduce((sum, unit) => sum + (unitDuration(unit) ?? 0), 0) || null : null,
      units: units.map(unit => ({
        key: unit.sentence_id, role: unit.provenance?.role ?? (unit.is_heading ? 'heading' : 'text'), text: unit.text,
        pageBreaks: unit.provenance?.page_breaks ?? [], note: unit.provenance?.note, sentenceId: unit.sentence_id,
        state: unit.state, duration: unitDuration(unit), issues: unit.issues.length, hasAudio: Boolean(unit.audio_path),
        editable: !['legal', 'bibliographic', 'index_entry', 'closing'].includes(unit.provenance?.role ?? ''),
        updatedAt: unit.updated_at,
      })),
    }
  })
  return { mode: 'job', once: sections.some(section => section.kind.startsWith('once_')), sections, warnings: [], pages: countPages(sections) }
}

// The server marks each voice the provider documents as Spain Spanish; the
// name is only a fallback for an older response without the flag.
function isSpanishFromSpain(voice: VoiceOption) {
  if (typeof voice.peninsular === 'boolean') return voice.peninsular
  if (voice.language) return voice.language.toLowerCase() === 'es-es'
  const details = [voice.name, ...Object.values(voice.labels ?? {})].join(' ').toLocaleLowerCase()
  return /\b(spain|españa|castilian|castellano|madrid)\b/.test(details)
}

export default function App() {
  const input = useRef<HTMLInputElement>(null)
  const playerRef = useRef<HTMLAudioElement>(null)
  const activeProductionJobId = useRef<string>()
  const activePackageTaskId = useRef<string>()
  const bookSelectionVersion = useRef(0)
  const [busy, setBusy] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [message, setMessage] = useState('')
  const [audio, setAudio] = useState('')
  const [transcript, setTranscript] = useState('')
  const [hasManualSections, setHasManualSections] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('upload')
  const [book, setBook] = useState('')
  const [bookMetadata, setBookMetadata] = useState<BookMetadata>({ title: '', author: '', edition: '', publication_year: '', publisher: '' })
  // The book on the server (see /api/drafts): it holds the upload and every change.
  const [draftId, setDraftId] = useState<string>()
  const [edits, setEdits] = useState<BookEdits>(NO_EDITS)
  const [undoStack, setUndoStack] = useState<BookEdits[]>([])
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [saveError, setSaveError] = useState('')
  const [editBusy, setEditBusy] = useState(false)
  // Back in the text after producing: the job stays, the editor shows the draft.
  const [editingProduced, setEditingProduced] = useState(false)
  const [producedSignature, setProducedSignature] = useState('')
  // PDF pages to narrate, as a PDF viewer numbers them ("20-114"); blank = all.
  const [pageRange, setPageRange] = useState('')
  const [pageRangeDraft, setPageRangeDraft] = useState('')
  const [narrationSections, setNarrationSections] = useState<NarrationSection[]>([])
  const [selectedNarrationIds, setSelectedNarrationIds] = useState<string[]>([])
  const [structureLoading, setStructureLoading] = useState(false)
  // A reload is putting the saved book and its last production back. Until it
  // knows, step 3 says so instead of claiming the book has no ZIP.
  const [restoring, setRestoring] = useState(() => {
    try {
      return Boolean(window.localStorage.getItem(DRAFT_KEY))
    } catch {
      return false
    }
  })
  const [packageBusy, setPackageBusy] = useState(false)
  const [directDownloadBusy, setDirectDownloadBusy] = useState(false)
  const [directProgress, setDirectProgress] = useState<PackageTaskStatus>()
  // The last direct download's ZIP and the book state it was made from.
  const [directZip, setDirectZip] = useState<DirectZip>()
  // Which of step 3's two actions is running; the other one waits.
  const [zipAction, setZipAction] = useState<'' | 'download' | 'regenerate'>('')
  // Step 3 builds the ZIP with automatic quality review, or directly without it.
  const [reviewMode, setReviewMode] = useState(storedReviewMode)
  // A unit step 3 sends the reader to in step 2 (a paragraph waiting for a decision).
  const [editorFocus, setEditorFocus] = useState<{ sentenceId: string; nonce: number }>()
  const [productionJob, setProductionJob] = useState<ProductionJob>()
  const [reviewingSentence, setReviewingSentence] = useState('')
  // Who signs a release or confirms a warning: recorded with the decision.
  const [reviewerName, setReviewerName] = useState('')
  // What became of the last download or production, shown under step 3's
  // actions: the reader looks there for it.
  const [downloadNotice, setDownloadNotice] = useState<{ text: string; href?: string; tone?: 'info' | 'error' } | null>(null)
  const [releaseConfirmed, setReleaseConfirmed] = useState(false)
  // Pronunciation editor: which sentence it is open for, the word being
  // worked on, what Spanish rules make of it, and the reviewer's own spelling.
  const [pronSentence, setPronSentence] = useState('')
  const [pronWord, setPronWord] = useState('')
  const [pronAnalysis, setPronAnalysis] = useState<PronunciationAnalysis>()
  const [pronCustom, setPronCustom] = useState('')
  const [pronBusy, setPronBusy] = useState('')
  const [pronSaved, setPronSaved] = useState<Record<string, string>>({})
  const [sentenceCorrections, setSentenceCorrections] = useState<Record<string, string>>({})
  // Paragraphs narrated again from the review queue. They stay on screen with
  // their new reading, even once it passes, so the reviewer can hear it.
  const [renarrated, setRenarrated] = useState<Record<string, true>>({})
  const [lastRenarrated, setLastRenarrated] = useState('')
  const [capabilities, setCapabilities] = useState<Capabilities>()
  const [capabilityError, setCapabilityError] = useState('')
  const [voiceLoadError, setVoiceLoadError] = useState('')
  const [voicesLoading, setVoicesLoading] = useState(false)
  const [alignment, setAlignment] = useState<TimedPhrase[]>([])
  const [currentPhrase, setCurrentPhrase] = useState(-1)
  const [playbackRate, setPlaybackRate] = useState(1)
  const [voiceId, setVoiceId] = useState('')
  const [voices, setVoices] = useState<VoiceOption[]>([])
  const [review, setReview] = useState<AudioReview>()
  const [reviewBusy, setReviewBusy] = useState(false)
  const [retakeBusy, setRetakeBusy] = useState(false)
  // Why reviewing or redoing the sample failed, shown beside its buttons in
  // step 2: the general status line is in step 1, out of sight there.
  const [sampleError, setSampleError] = useState('')
  // The ONCE structure, from the book's planilla. On by default: Soni2
  // produces for ONCE. Only the values the reader sets are kept here; the
  // server fills the rest with MODELO ONCE's rules.
  const [onceEnabled, setOnceEnabled] = useState(true)
  const [planillaValues, setPlanillaValues] = useState<Planilla>({})
  const [planillaDefaults, setPlanillaDefaults] = useState<Planilla>()
  const [planillaWarnings, setPlanillaWarnings] = useState<string[]>([])
  const [planillaBusy, setPlanillaBusy] = useState(false)
  const [planillaStatus, setPlanillaStatus] = useState('')
  const [showPlanilla, setShowPlanilla] = useState(false)
  // What step 2 shows: the outline before production, the job's structure after.
  const [outline, setOutline] = useState<BookOutline>()
  const [outlineError, setOutlineError] = useState('')
  const [jobStructure, setJobStructure] = useState<JobStructure>()
  const structureTimer = useRef<number>()

  async function refreshCapabilities() {
    setCapabilityError('')
    try {
      const response = await fetch(`${API}/api/capabilities`, { cache: 'no-store' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const value = await response.json() as Capabilities
      setCapabilities(value)
      if (value.voice_id_configurable) {
        setVoicesLoading(true)
        setVoiceLoadError('')
        try {
          const voiceResponse = await fetch(`${API}/api/voices`, { cache: 'no-store' })
          const voiceData = await voiceResponse.json().catch(() => ({})) as { voices?: VoiceOption[]; default_voice_id?: string; detail?: string }
          if (!voiceResponse.ok) throw new Error(voiceData.detail || `HTTP ${voiceResponse.status}`)
          const availableVoices = voiceData.voices ?? []
          // The server already lists Spain Spanish first; keep that order.
          const orderedVoices = [...availableVoices.filter(isSpanishFromSpain),
                                 ...availableVoices.filter(voice => !isSpanishFromSpain(voice))]
          setVoices(orderedVoices)
          setVoiceId(current => current || voiceData.default_voice_id || value.voice
            || orderedVoices.find(isSpanishFromSpain)?.voice_id || orderedVoices[0]?.voice_id || '')
        } catch (error) {
          setVoiceLoadError(error instanceof Error ? error.message : 'No se pudo consultar la lista de voces.')
        } finally {
          setVoicesLoading(false)
        }
      } else {
        setVoices([])
        setVoiceLoadError('')
      }
    } catch (error) {
      setCapabilities(undefined)
      setCapabilityError(error instanceof Error ? error.message : 'No se pudo conectar con el servidor.')
    }
  }

  useEffect(() => { void refreshCapabilities() }, [])

  // Leaving the page: the loops following a production or a download stop
  // asking. (What they followed is stopped, or picked up, by the next visit.)
  useEffect(() => () => {
    activeProductionJobId.current = undefined
    activePackageTaskId.current = undefined
  }, [])

  // MODELO ONCE's rules, to show in the planilla before the reader changes them.
  useEffect(() => {
    void fetch(`${API}/api/planilla/defaults`, { cache: 'no-store' })
      .then(response => response.ok ? response.json() : undefined)
      .then((body?: { planilla?: Planilla; production_date?: string }) => {
        if (body?.planilla && typeof body.planilla.legal_text === 'string') {
          setPlanillaDefaults({ ...body.planilla, production_date: body.production_date ?? '' })
        }
      })
      .catch(() => undefined)
  }, [])

  // A production run nobody can follow any more is stopped, so it does not keep
  // narrating (and spending) unseen. A reload of the same book follows it instead:
  // the restore below decides.
  useEffect(() => {
    // The same goes for a direct download the previous visit was waiting for.
    const taskId = window.localStorage.getItem(PACKAGE_TASK_KEY)
    window.localStorage.removeItem(PACKAGE_TASK_KEY)
    if (taskId) void cancelPackageTask(taskId)
    const jobId = window.localStorage.getItem('soni2-active-production-job')
    if (!jobId || window.localStorage.getItem(DRAFT_KEY)) return
    window.localStorage.removeItem('soni2-active-production-job')
    // -1 matches no book selection, so a job that no longer exists fails quietly.
    void cancelProductionJob(jobId, -1)
  }, [])

  useEffect(() => {
    if (playerRef.current) playerRef.current.playbackRate = playbackRate
  }, [audio, playbackRate])

  // What is saved for this book, from what is on screen. Any difference from
  // what the server last stored is saved a moment later (or at once, before
  // anything is narrated), and the answer brings the outline it gives.
  const draftState = useMemo<DraftState>(() => ({
    pages: pageRange, selected_ids: selectedNarrationIds, metadata: filledMetadata(bookMetadata),
    planilla: planillaValues, once_enabled: onceEnabled, create_sections: hasManualSections, edits, voice_id: voiceId,
  }), [pageRange, selectedNarrationIds, bookMetadata, planillaValues, onceEnabled, hasManualSections, edits, voiceId])
  const draftSignature = JSON.stringify(draftState)
  const latestState = useRef(draftState)
  latestState.current = draftState
  const latestDraftId = useRef<string>()
  latestDraftId.current = draftId
  const draftRevision = useRef(0)
  const savedSignature = useRef('')
  // The last state a save was tried for: a failed one is not retried on its own
  // (the page says so); the next change or production tries again.
  const attemptedSignature = useRef('')
  const saveChain = useRef<Promise<boolean>>(Promise.resolve(true))
  const saveTimer = useRef<number>()
  const editChain = useRef<Promise<void>>(Promise.resolve())
  const latestOutline = useRef<BookOutline>()
  latestOutline.current = outline

  function showOutline(view: DraftView) {
    latestOutline.current = view.outline ?? undefined
    setOutline(view.outline ?? undefined)
    setOutlineError(view.outline_error ?? '')
    setNarrationSections(view.plan.sections)
  }

  // Put on screen a book as the server has it: after an upload, after a reload,
  // or when another window saved something newer.
  function applyDraftView(view: DraftView) {
    const state = view.draft.state
    const detected = view.outline ?? { title: view.plan.title, author: '', edition: '', publication_year: '', publisher: '' }
    const metadata: BookMetadata = {
      title: state.metadata?.title || detected.title || '', author: state.metadata?.author || detected.author || '',
      edition: state.metadata?.edition || detected.edition || '',
      publication_year: state.metadata?.publication_year || detected.publication_year || '',
      publisher: state.metadata?.publisher || detected.publisher || '',
    }
    const restored: DraftState = {
      pages: state.pages ?? '', selected_ids: state.selected_ids ?? view.plan.selected_ids, metadata: filledMetadata(metadata),
      planilla: state.planilla ?? {}, once_enabled: state.once_enabled !== false, create_sections: Boolean(state.create_sections),
      edits: normalizeEdits(state.edits), voice_id: state.voice_id ?? '',
    }
    latestDraftId.current = view.draft.draft_id
    draftRevision.current = view.draft.revision
    // This is what the server holds: nothing here needs saving. (A voice chosen
    // before the upload differs, and is saved a moment later.)
    savedSignature.current = JSON.stringify(restored)
    setDraftId(view.draft.draft_id)
    setBook(view.draft.source_name)
    setPageRange(restored.pages)
    setPageRangeDraft(restored.pages)
    setSelectedNarrationIds(restored.selected_ids ?? [])
    setBookMetadata(metadata)
    setPlanillaValues(restored.planilla ?? {})
    setOnceEnabled(restored.once_enabled)
    setHasManualSections(restored.create_sections)
    setEdits(restored.edits)
    setVoiceId(current => restored.voice_id || current)
    setUndoStack([])
    setSaveStatus('idle')
    setSaveError('')
    showOutline(view)
    return JSON.stringify(restored)
  }

  // Saves run one after another, so an older one can never land last. True when
  // what is on screen is what the server holds.
  function saveDraft(): Promise<boolean> {
    const run = async () => {
      const id = latestDraftId.current
      const state = latestState.current
      const signature = JSON.stringify(state)
      if (!id || signature === savedSignature.current) return true
      attemptedSignature.current = signature
      setSaveStatus('saving')
      try {
        const response = await fetch(`${API}/api/drafts/${encodeURIComponent(id)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ state, revision: draftRevision.current }),
        })
        const body = await response.json().catch(() => ({})) as DraftView & { detail?: unknown }
        const conflict = (body.detail as { draft?: DraftView; message?: string } | undefined)
        if (response.status === 409 && conflict?.draft && isDraftView(conflict.draft)) {
          // Another window saved first: its version is the book now.
          applyDraftView(conflict.draft)
          setMessage(conflict.message ?? 'Este libro se ha cambiado en otra ventana.')
          setSaveStatus('error')
          setSaveError(conflict.message ?? '')
          return false
        }
        if (response.status === 404) window.localStorage.removeItem(DRAFT_KEY)
        if (!response.ok || !isDraftView(body)) throw new Error(errorDetail(body.detail, 'El servidor no los ha aceptado.'))
        draftRevision.current = body.draft.revision
        savedSignature.current = signature
        showOutline(body)
        const newer = JSON.stringify(latestState.current) !== signature
        setSaveStatus(newer ? 'saving' : 'saved')
        setSaveError('')
        // What changed while this save was on its way is saved next. An undo
        // made then matched what the server held before it, so the page took
        // it for saved, and this save would have left the server with the
        // change the undo took back.
        if (newer) {
          window.clearTimeout(saveTimer.current)
          saveTimer.current = window.setTimeout(() => void saveDraft(), SAVE_DELAY_MS)
        }
        return true
      } catch (error) {
        setSaveStatus('error')
        setSaveError(error instanceof Error ? error.message : 'No se ha podido conectar con el servidor.')
        return false
      }
    }
    saveChain.current = saveChain.current.then(run, run)
    return saveChain.current
  }

  // Before anything is narrated: what the server would narrate is what is on screen.
  async function flushDraft(): Promise<boolean> {
    window.clearTimeout(saveTimer.current)
    return saveDraft()
  }

  useEffect(() => {
    if (draftId && draftSignature === savedSignature.current) {
      // Back to what the server holds (an undo after a refused change): nothing is pending.
      attemptedSignature.current = ''
      setSaveStatus(status => status === 'error' ? 'saved' : status)
      setSaveError('')
      return
    }
    if (!draftId || draftSignature === attemptedSignature.current) return
    setSaveStatus('saving')
    window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => void saveDraft(), SAVE_DELAY_MS)
    return () => window.clearTimeout(saveTimer.current)
    // saveDraft reads the latest state through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftSignature, draftId])

  useEffect(() => {
    if (draftId) window.localStorage.setItem(TAB_KEY, workspaceTab)
  }, [draftId, workspaceTab])

  // A reload finds the book where it was left, with all its changes. (A job that
  // was still narrating was stopped above; what it paid for is in the cache.)
  useEffect(() => {
    const id = window.localStorage.getItem(DRAFT_KEY)
    if (!id) return
    const selection = bookSelectionVersion.current
    const leftBehind = window.localStorage.getItem('soni2-active-production-job')
    const stopLeftBehind = () => {
      if (!leftBehind) return
      window.localStorage.removeItem('soni2-active-production-job')
      void cancelProductionJob(leftBehind, -1)
    }
    setStructureLoading(true)
    void (async () => {
      try {
        const response = await fetch(`${API}/api/drafts/${encodeURIComponent(id)}`, { cache: 'no-store' })
        const body = await response.json().catch(() => ({})) as DraftView
        if (bookSelectionVersion.current !== selection) return
        if (response.status === 404) {
          stopLeftBehind()
          window.localStorage.removeItem(DRAFT_KEY)
          window.localStorage.removeItem(TAB_KEY)
          setMessage('El libro que estabas preparando ya no está guardado. Vuelve a subirlo para seguir.')
          return
        }
        if (!response.ok || !isDraftView(body)) throw new Error('No se ha podido recuperar el libro.')
        const signature = applyDraftView(body)
        const tab = window.localStorage.getItem(TAB_KEY)
        setWorkspaceTab(tab === 'review' || tab === 'export' ? tab : 'upload')
        setMessage('Hemos recuperado el libro que estabas preparando, con todos tus cambios.')
        const last = body.draft.last_job
        const stillRunning = Boolean(last && last.job_id === leftBehind && ['queued', 'running'].includes(last.state)
          && last.draft_revision === body.draft.revision)
        if (!stillRunning) stopLeftBehind()
        if (stillRunning && last) {
          // Reloading only reconnects to the production of this same text.
          activeProductionJobId.current = last.job_id
          setProducedSignature(signature)
          setPackageBusy(true)
          setMessage('La producción de este libro sigue en marcha. Seguimos su avance.')
          void watchProductionJob(last.job_id)
            .catch(error => setMessage(error instanceof Error ? error.message : 'No se pudo consultar el estado de producción.'))
            .finally(() => { if (bookSelectionVersion.current === selection) setPackageBusy(false) })
        }
        // A finished production of exactly this text is still valid: its
        // audio and its download stay available without producing again.
        if (last && ['completed', 'partially_completed'].includes(last.state) && last.draft_revision === body.draft.revision) {
          const jobResponse = await fetch(`${API}/api/jobs/${encodeURIComponent(last.job_id)}`, { cache: 'no-store' })
          const job = await jobResponse.json().catch(() => undefined) as ProductionJob | undefined
          if (jobResponse.ok && job && bookSelectionVersion.current === selection) {
            activeProductionJobId.current = last.job_id
            setProductionJob(job)
            setProducedSignature(signature)
          }
        }
      } catch (error) {
        if (bookSelectionVersion.current === selection) {
          setMessage(error instanceof Error ? error.message : 'No se ha podido recuperar el libro.')
        }
      } finally {
        if (bookSelectionVersion.current === selection) setStructureLoading(false)
        setRestoring(false)
      }
    })()
    // Once, when the page opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The job's structure (files, pages, measured clips) follows its progress,
  // at most once every second or so; the units' states arrive with the job.
  const latestJobId = useRef<string>()
  latestJobId.current = productionJob?.job_id
  const newestUpdate = productionJob?.sentences.reduce((latest, item) => (item.updated_at ?? '') > latest ? item.updated_at ?? '' : latest, '') ?? ''
  const jobSignature = productionJob
    ? `${productionJob.job_id}|${productionJob.state}|${JSON.stringify(productionJob.progress ?? {})}|${productionJob.sentences.length}|${newestUpdate}`
    : ''
  useEffect(() => {
    if (!jobSignature) {
      setJobStructure(undefined)
      return
    }
    if (structureTimer.current) return
    // The first look at a job is immediate; after that, at most about once a second.
    const delay = jobStructure?.job_id === productionJob?.job_id ? 1200 : 0
    structureTimer.current = window.setTimeout(async () => {
      structureTimer.current = undefined
      const jobId = latestJobId.current
      if (!jobId) return
      try {
        const response = await fetch(`${API}/api/jobs/${encodeURIComponent(jobId)}/structure`, { cache: 'no-store' })
        const body = await response.json().catch(() => undefined)
        if (response.ok && isStructure(body) && body.job_id === jobId && latestJobId.current === jobId) setJobStructure(body)
      } catch {
        // The job's own units still show its structure.
      }
    }, delay)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobSignature])
  useEffect(() => () => window.clearTimeout(structureTimer.current), [])

  // A production or a download of what was on screen before stops spending.
  function stopCurrentWork(selectionVersion: number) {
    const previousJobId = activeProductionJobId.current
      ?? productionJob?.job_id
      ?? window.localStorage.getItem('soni2-active-production-job')
    activeProductionJobId.current = undefined
    window.localStorage.removeItem('soni2-active-production-job')
    // A download still narrating the previous book would keep spending for nothing.
    const previousPackageTask = activePackageTaskId.current ?? window.localStorage.getItem(PACKAGE_TASK_KEY)
    activePackageTaskId.current = undefined
    window.localStorage.removeItem(PACKAGE_TASK_KEY)
    if (previousPackageTask) void cancelPackageTask(previousPackageTask)
    setProductionJob(undefined)
    setJobStructure(undefined)
    setEditingProduced(false)
    setProducedSignature('')
    setReviewingSentence('')
    setSentenceCorrections({})
    setRenarrated({})
    setLastRenarrated('')
    setPackageBusy(false)
    setDirectDownloadBusy(false)
    setDirectProgress(undefined)
    setDirectZip(undefined)
    setZipAction('')
    setDownloadNotice(null)
    setAudio(''); setTranscript(''); setAlignment([]); setCurrentPhrase(-1); setReview(undefined)
    if (previousJobId) void cancelProductionJob(previousJobId, selectionVersion)
  }

  async function selectBook(file?: File, pages = '') {
    if (!file) return
    const selectionVersion = ++bookSelectionVersion.current
    stopCurrentWork(selectionVersion)
    window.clearTimeout(saveTimer.current)
    latestDraftId.current = undefined
    savedSignature.current = ''
    setDraftId(undefined)
    window.localStorage.removeItem(DRAFT_KEY)
    setOutline(undefined)
    setOutlineError('')
    // A planilla and its edits belong to one book.
    setPlanillaValues({})
    setPlanillaWarnings([])
    setPlanillaStatus('')
    setShowPlanilla(false)
    setEdits(NO_EDITS)
    setUndoStack([])
    setSaveStatus('idle')
    setNarrationSections([]); setSelectedNarrationIds([]); setStructureLoading(true)
    setHasManualSections(false); setWorkspaceTab('upload'); setBookMetadata({ title: '', author: '', edition: '', publication_year: '', publisher: '' }); setBook(file.name); setMessage('Libro cargado. Elige una voz y pulsa «Generar vista previa».')
    const form = new FormData(); form.append('file', file); form.append('pages', pages)
    try {
      const response = await fetch(`${API}/api/drafts`, { method: 'POST', body: form })
      const result = await response.json().catch(() => ({})) as DraftView & { detail?: string }
      if (bookSelectionVersion.current !== selectionVersion) return
      if (!response.ok || !isDraftView(result)) throw new Error(result.detail || 'No se ha podido analizar la estructura del libro.')
      applyDraftView(result)
      window.localStorage.setItem(DRAFT_KEY, result.draft.draft_id)
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        setMessage(error instanceof Error ? error.message : 'No se ha podido analizar la estructura del libro.')
      }
    } finally {
      if (bookSelectionVersion.current === selectionVersion) setStructureLoading(false)
    }
  }

  async function cancelProductionJob(jobId: string, selectionVersion: number) {
    try {
      const statusResponse = await fetch(`${API}/api/jobs/${jobId}`, { cache: 'no-store' })
      const status = await statusResponse.json().catch(() => ({})) as ProductionJob & { detail?: string }
      if (!statusResponse.ok) throw new Error(status.detail || 'No se pudo consultar el trabajo anterior.')
      if (!['queued', 'running'].includes(status.state)) return
      const cancelResponse = await fetch(`${API}/api/jobs/${jobId}/cancel`, { method: 'POST' })
      const result = await cancelResponse.json().catch(() => ({})) as { detail?: string }
      if (!cancelResponse.ok) throw new Error(result.detail || 'No se pudo detener el trabajo anterior.')
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        const detail = error instanceof Error ? error.message : 'No se pudo detener el trabajo anterior.'
        setMessage(`El nuevo libro está cargado, pero la producción anterior no se pudo detener: ${detail}`)
      }
    }
  }

  async function generatePreview() {
    if (!draftId || busy) return
    setAudio(''); setTranscript(''); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setSampleError(''); setBusy(true); setMessage('Estamos preparando una muestra de tu libro.')
    const form = new FormData(); form.append('draft_id', draftId); form.append('voice_id', voiceId)
    try {
      if (!await flushDraft()) throw new Error('No se han podido guardar tus cambios, así que no se ha generado la muestra.')
      const response = await fetch(`${API}/api/books/preview`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No hemos podido leer este documento. Prueba con otro archivo.')
      }
      const result: { audio_base64: string; audio_mime_type: string; transcript: string; alignment?: TimedPhrase[] | null; audio_error?: string | null; audio_notice?: string | null; diagnostic_id?: string | null } = await response.json()
      const binary = atob(result.audio_base64)
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0))
      const blob = new Blob([bytes], { type: result.audio_mime_type })
      if (result.audio_base64) {
        setAudio(URL.createObjectURL(blob))
        setMessage(result.audio_notice || 'La narracion esta lista. Pulsa reproducir para escucharla.')
      } else {
        setAudio('')
        setMessage(`${result.audio_error || 'El texto se ha extraido, pero no hay una voz configurada.'}${result.diagnostic_id ? ` (Diagnóstico: ${result.diagnostic_id})` : ''}`)
      }
      setAlignment(result.alignment ?? [])
      const sourceParagraphs = result.transcript
        .split(/\r?\n\s*\r?\n/)
        .map(paragraph => paragraph.replace(/\s+/g, ' ').trim())
        .filter(Boolean)
      const readableParagraphs: string[] = []
      let unfinishedFragment = ''
      for (const paragraph of sourceParagraphs) {
        const combined = unfinishedFragment ? `${unfinishedFragment} ${paragraph}` : paragraph
        unfinishedFragment = ''
        const endsSentence = /[.!?…]["'”’»)]?$/.test(combined)
        if (combined.length < 32 && !endsSentence) {
          unfinishedFragment = combined
        } else {
          readableParagraphs.push(combined)
        }
      }
      if (unfinishedFragment) {
        if (readableParagraphs.length) {
          readableParagraphs[readableParagraphs.length - 1] += ` ${unfinishedFragment}`
        } else {
          readableParagraphs.push(unfinishedFragment)
        }
      }
      const displayParagraphs = readableParagraphs.flatMap(paragraph => {
        const sentences = paragraph.match(/[^.!?…]+[.!?…]+["'”’»)]*|[^.!?…]+$/gu)
          ?.map(sentence => sentence.trim())
          .filter(Boolean) ?? [paragraph]
        const groups: string[] = []
        for (let index = 0; index < sentences.length; index += 3) {
          groups.push(sentences.slice(index, index + 3).join(' '))
        }
        return groups
      })
      setTranscript(displayParagraphs.join('\n\n'))
      setWorkspaceTab('review')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Se ha producido un error. Inténtalo de nuevo.')
    } finally { setBusy(false) }
  }

  // The book is the saved draft, with its edits: a request only says which one
  // and which voice, so it cannot narrate anything else.
  function buildBookForm() {
    const form = new FormData()
    form.append('draft_id', draftId as string)
    form.append('voice_id', voiceId)
    return form
  }

  // The download carries the book's own title, «El faro de Cabo Gris.zip»,
  // with the same rule the server uses: accents and spaces stay, only the
  // characters Windows or macOS refuse in a file name go.
  function bookFilename(sourceName: string, suffix = '') {
    const clean = (value: string) => Array.from(value)
      .map(character => character.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(character) ? ' ' : character)
      .join('').split(/\s+/).join(' ').replace(/^[ .]+|[ .]+$/g, '').slice(0, 120).replace(/[ .]+$/, '')
    const name = clean(bookMetadata.title) || clean((sourceName || book).replace(/\.[^.]+$/, '')) || 'libro'
    return `${name}${suffix}.zip`
  }

  // The ZIP without automatic review: a background task the page polls, so it
  // shows real progress instead of one long, silent request, and so choosing
  // another book can stop it. The server answers a request it already has an
  // answer to with that task (still running, or finished unless ``force``).
  async function produceDirect({ force, download }: { force: boolean; download: boolean }) {
    if (!draftId || directDownloadBusy || packageBusy) return
    const selectionVersion = bookSelectionVersion.current
    const filename = bookFilename(book)
    setDirectDownloadBusy(true)
    setZipAction(download ? 'download' : 'regenerate')
    setDirectProgress(undefined)
    setDownloadNotice(null)
    setMessage('Preparando el audiolibro completo…')
    let taskId: string | undefined
    try {
      if (!await flushDraft()) throw new Error('No se han podido guardar tus cambios, así que no se ha generado el audiolibro.')
      const signature = JSON.stringify(latestState.current)
      const form = buildBookForm()
      if (force) form.append('force', 'true')
      const response = await fetch(`${API}/api/books/package-tasks`, { method: 'POST', body: form })
      const started = await response.json().catch(() => ({})) as Partial<PackageTaskStatus>
      if (!response.ok || !started.task_id) {
        throw new Error(started.detail || `No se ha podido generar el audiolibro (HTTP ${response.status}).`)
      }
      taskId = started.task_id
      activePackageTaskId.current = taskId
      window.localStorage.setItem(PACKAGE_TASK_KEY, taskId)
      const status = started.state === 'running' || !started.state ? await watchPackageTask(taskId)
        : started as PackageTaskStatus
      if (activePackageTaskId.current !== taskId) return
      setDirectZip({ taskId, signature, status })
      if (status.state !== 'completed') throw new Error(status.detail || 'No se ha podido generar el audiolibro.')
      const reusedNote = status.reused
        ? ` ${status.reused} de ${status.total} párrafos ya estaban narrados y no se han vuelto a pagar.` : ''
      if (!download) {
        setMessage(`El ZIP está listo.${reusedNote} Pulsa «Descargar ZIP» para guardarlo.`)
        return
      }
      setMessage('Audio listo. Descargando el ZIP…')
      // Streamed to disk by the browser rather than buffered in a blob: an
      // audiobook package is far too large to hold in the tab's memory, which
      // is why pressing the button used to appear to do nothing.
      await streamDownload(
        `${API}/api/books/package-tasks/${encodeURIComponent(taskId)}/download`, filename,
      )
      setMessage(status.reused
        ? `El audiolibro se ha descargado.${reusedNote}`
        : 'El audiolibro se ha descargado. Incluye el audio por capítulos y la estructura DAISY.')
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        const detail = error instanceof Error ? publicCopy(error.message) : 'No se ha podido descargar el audiolibro.'
        setMessage(detail)
        setDownloadNotice({ text: detail, tone: 'error' })
      }
    } finally {
      if (taskId && activePackageTaskId.current === taskId) {
        activePackageTaskId.current = undefined
        window.localStorage.removeItem(PACKAGE_TASK_KEY)
      }
      if (bookSelectionVersion.current === selectionVersion) {
        setDirectDownloadBusy(false)
        setDirectProgress(undefined)
        setZipAction('')
      }
    }
  }

  // A moment without connection, or the server restarting, is not the end of a
  // long job: the page keeps asking for two minutes before it gives up.
  async function pollStatus(url: string) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        const response = await fetch(url, { cache: 'no-store' })
        if (response.status < 502 || attempt >= 60) return response
      } catch (error) {
        if (attempt >= 60) throw error
      }
      await new Promise(resolve => window.setTimeout(resolve, 2000))
    }
  }

  async function watchPackageTask(taskId: string): Promise<PackageTaskStatus> {
    for (;;) {
      const response = await pollStatus(`${API}/api/books/package-tasks/${encodeURIComponent(taskId)}`)
      const status = await response.json().catch(() => ({})) as PackageTaskStatus
      if (!response.ok) throw new Error(status.detail || 'No se pudo consultar el avance de la descarga.')
      if (activePackageTaskId.current !== taskId || status.state !== 'running') return status
      setDirectProgress(status)
      setMessage(status.stage === 'assembling'
        ? 'Montando los capítulos del audiolibro…'
        : ['packaging', 'validating', 'zipping', 'verifying'].includes(status.stage ?? '')
          ? 'Creando y comprobando el paquete DAISY y el ZIP…'
          : status.total
            ? `Narrando el audiolibro: ${status.done} de ${status.total} párrafos.`
            : 'Preparando el audiolibro completo…')
      await new Promise(resolve => window.setTimeout(resolve, 1000))
    }
  }

  function cancelPackageTask(taskId: string) {
    // A download nobody is waiting for any more must stop narrating, and spending.
    return fetch(`${API}/api/books/package-tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
      .catch(() => undefined)
  }

  // Production with quality review: narrate what is missing, check it, and
  // build the package. The server answers with the job that already has this
  // exact text (running, waiting for decisions, or, unless ``force``,
  // finished with its ZIP), so a second click never pays twice.
  async function produceReviewed({ force = false, download = false }: { force?: boolean; download?: boolean } = {}) {
    if (!draftId || packageBusy || directDownloadBusy) return
    const selectionVersion = bookSelectionVersion.current
    setPackageBusy(true)
    setZipAction(download ? 'download' : force ? 'regenerate' : '')
    setDownloadNotice(null)
    setMessage('Preparando el libro completo. Este proceso puede tardar varios minutos.')
    const form = buildBookForm()
    if (force) form.append('force', 'true')
    try {
      if (!await flushDraft()) throw new Error('No se han podido guardar tus cambios, así que no se ha empezado a producir.')
      const signature = JSON.stringify(latestState.current)
      const response = await fetch(`${API}/api/books/jobs`, { method: 'POST', body: form })
      const created = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(created.detail || 'No hemos podido iniciar la producción del libro.')
      if (bookSelectionVersion.current !== selectionVersion) {
        void cancelProductionJob(created.job_id, selectionVersion)
        return
      }
      const sameJob = productionJob?.job_id === created.job_id
      activeProductionJobId.current = created.job_id
      if (!sameJob) {
        setProductionJob({ job_id: created.job_id, state: created.state, source_name: book,
          total_sentences: created.total_sentences, progress: {}, sentences: [] })
        setJobStructure(undefined)
      }
      setEditingProduced(false)
      setProducedSignature(signature)
      window.localStorage.setItem('soni2-active-production-job', created.job_id)
      setMessage(created.reused_package
        ? 'Este texto ya estaba producido con esta voz: su ZIP está listo y no se ha vuelto a generar.'
        : created.reused_existing_job
          ? 'Este libro ya se está produciendo con esta voz, así que seguimos el trabajo en curso en lugar de empezar otro.'
          : 'Producción iniciada. Puedes seguir el avance aquí.')
      const final = await watchProductionJob(created.job_id)
      if (download && final?.state === 'completed' && bookSelectionVersion.current === selectionVersion) {
        await downloadReviewedZip(final)
      }
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        const detail = error instanceof Error ? publicCopy(error.message) : 'No se ha podido descargar el libro completo.'
        setMessage(detail)
        setDownloadNotice({ text: detail, tone: 'error' })
      }
    } finally {
      if (bookSelectionVersion.current === selectionVersion) {
        setPackageBusy(false)
        setZipAction('')
      }
    }
  }

  // Step 2's «Producir»: a reviewed production of the text as it stands.
  function startReviewedProduction() {
    return produceReviewed()
  }

  // Follows a job until it stops, and returns it as it ended. ``quiet``
  // follows without replacing the message, for the short catch-up after a
  // review decision whose own result is already on screen.
  async function watchProductionJob(jobId: string, quiet = false): Promise<ProductionJob | undefined> {
    for (let attempt = 0; attempt < 7200; attempt += 1) {
      if (activeProductionJobId.current !== jobId) return undefined
      // The live view: units without their full evidence, nothing written.
      const response = await pollStatus(`${API}/api/jobs/${jobId}?view=live`)
      const status = await response.json().catch(() => ({})) as ProductionJob & { detail?: string }
      if (!response.ok) throw new Error(status.detail || 'No se pudo consultar el estado de producción.')
      if (activeProductionJobId.current !== jobId) return undefined
      setProductionJob(status)
      const counts = Object.entries(status.progress ?? {}).reduce((sum, [state, count]) =>
        sum + (['pending', 'generating'].includes(state) ? 0 : count), 0)
      if (!quiet) setMessage(`Producción: ${counts} de ${status.total_sentences} párrafos procesados.`)
      if (status.state === 'completed') {
        setMessage('El audiolibro está listo. Usa «Descargar ZIP» para guardarlo.')
        return status
      }
      if (['partially_completed', 'failed', 'cancelled'].includes(status.state)) {
        if (!quiet) setMessage(status.state === 'partially_completed'
          ? 'La producción necesita una revisión: hay párrafos que esperan tu decisión.'
          : `La producción terminó con estado «${status.state}». ${status.error || ''}`)
        return status
      }
      await new Promise(resolve => window.setTimeout(resolve, 1000))
    }
    throw new Error('La producción sigue activa. Puedes volver a consultar el estado más tarde.')
  }

  // Hand a URL to the browser so it streams the file to disk with its own
  // progress, instead of buffering it in the tab. A one-byte range request
  // first costs nothing and still surfaces the real error when the file is not
  // ready, rather than navigating away to a JSON error page.
  async function streamDownload(url: string, filename: string) {
    const probe = await fetch(url, { headers: { Range: 'bytes=0-0' } })
    if (!probe.ok && probe.status !== 206) {
      const body = await probe.json().catch(() => ({})) as { detail?: unknown }
      throw new Error(errorDetail(body.detail, `No se ha podido descargar el archivo (HTTP ${probe.status}).`))
    }
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    setDownloadNotice({
      text: 'La descarga ha empezado. Búscala en el panel de descargas del navegador: como este sitio aún no usa HTTPS, puede pedirte que confirmes que quieres conservar el archivo.',
      href: url, tone: 'info',
    })
  }

  async function releaseAction(path: 'acknowledge' | 'release', body: Record<string, unknown>) {
    if (!productionJob) return
    try {
      const response = await fetch(`${API}/api/jobs/${encodeURIComponent(productionJob.job_id)}/${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      const result = await response.json().catch(() => ({})) as ReleaseStatus & { detail?: unknown }
      if (!response.ok) throw new Error(errorDetail(result.detail, 'No se pudo registrar la decisión.'))
      setProductionJob(current => current ? { ...current, release: result } : current)
      setMessage(path === 'release' ? 'Libro publicado. La decisión queda registrada con tu nombre.'
                                    : 'Aviso confirmado y registrado con tu nombre.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se pudo registrar la decisión.')
    }
  }

  // A finished reviewed production's ZIP. A book the release gate has not
  // passed (a warning nobody confirmed, say) still downloads, under a name
  // that says it is not validated; its quality-status.json says why.
  //
  // An audiobook package is hundreds of megabytes. Reading it into a blob
  // first filled the tab's memory and the download appeared to do nothing, so
  // the browser is handed the URL and streams it straight to disk. A one-byte
  // range request first, which costs nothing, still surfaces the real reason
  // when the package is not ready instead of navigating away.
  async function downloadReviewedZip(job: ProductionJob) {
    const validated = !job.release || ['PASSED', 'RELEASED'].includes(job.release.state)
    setMessage(validated ? 'Comprobando que el paquete esté listo…'
      : 'Descargando el paquete sin validar. No debe publicarse hasta superar las comprobaciones.')
    try {
      await streamDownload(
        `${API}/api/jobs/${encodeURIComponent(job.job_id)}/artifacts${validated ? '' : '?diagnostic=true'}`,
        bookFilename(job.source_name || book, validated ? '' : ' (SIN VALIDAR)'))
      setMessage('La descarga del paquete DAISY ha comenzado. Puede tardar según el tamaño del libro.')
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'No se ha podido descargar el paquete DAISY.'
      const explained = publicCopy(detail.includes('todavía no está disponible')
        ? `${detail} Pulsa «Regenerar ZIP» para volver a crearlo.` : detail)
      setMessage(explained)
      setDownloadNotice({ text: explained, tone: 'error' })
    }
  }

  // Step 3's first action: the ZIP of the book as it stands. One that is
  // ready is downloaded at once; otherwise it is built (reusing everything
  // that did not change) and downloaded as soon as it is ready.
  async function downloadZip() {
    if (packageBusy || directDownloadBusy) return
    if (reviewMode) {
      if (productionJob && productionJob.state === 'completed' && !productionIsStale) {
        await downloadReviewedZip(productionJob)
        return
      }
      await produceReviewed({ download: true })
      return
    }
    if (directZip && directZip.status.state === 'completed' && directZip.signature === draftSignature) {
      setDownloadNotice(null)
      try {
        await streamDownload(`${API}/api/books/package-tasks/${encodeURIComponent(directZip.taskId)}/download`,
          bookFilename(book))
      } catch (error) {
        const detail = error instanceof Error ? publicCopy(error.message) : 'No se ha podido descargar el ZIP.'
        setDownloadNotice({ text: `${detail} Pulsa «Regenerar ZIP» para volver a crearlo.`, tone: 'error' })
      }
      return
    }
    await produceDirect({ force: false, download: true })
  }

  // Step 3's second action: build the ZIP again from the latest text. Only what
  // changed is narrated, checked and mastered again; the rest is reused.
  async function regenerateZip() {
    if (packageBusy || directDownloadBusy) return
    if (reviewMode) await produceReviewed({ force: true })
    else await produceDirect({ force: true, download: false })
  }

  function chooseReviewMode(value: boolean) {
    setReviewMode(value)
    setDownloadNotice(null)
    try {
      window.localStorage.setItem(REVIEW_MODE_KEY, value ? 'on' : 'off')
    } catch {
      // Only a preference: without storage it lasts until the page closes.
    }
  }

  // Step 3 sends the reader to a paragraph in step 2, where it is decided.
  function openUnitInEditor(sentenceId: string) {
    setEditingProduced(false)
    setEditorFocus({ sentenceId, nonce: Date.now() })
    switchWorkspaceTab('review')
  }

  // The server narrates a corrected or retried paragraph inside the request and
  // answers with it, so the card shows the new text and plays the new reading
  // the moment the answer arrives instead of queueing it out of sight.
  // A word is read wrong because of how it is spelled, so the repair is a
  // different spelling. Spanish rules say how the word reads now; the reviewer
  // hears each candidate and only then saves one.
  async function analysePronunciation(word: string, sentenceId: string) {
    const clean = word.replace(/[^\p{L}\p{N}'-]/gu, '')
    if (!clean) return
    setPronSentence(sentenceId)
    setPronWord(clean)
    setPronAnalysis(undefined)
    setPronCustom('')
    setPronBusy('analyse')
    try {
      const response = await fetch(`${API}/api/pronunciation/analyse`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: clean }),
      })
      const body = await response.json().catch(() => ({})) as PronunciationAnalysis & { detail?: string }
      if (!response.ok) throw new Error(body.detail || 'No se pudo analizar la palabra.')
      setPronAnalysis(body)
      setPronCustom(body.saved_respelling || '')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se pudo analizar la palabra.')
    } finally { setPronBusy('') }
  }

  // The candidate, read inside its sentence. The caller plays it in its own
  // player, so the editor in step 2 and the queue in step 3 never share one.
  async function hearRespelling(respelling: string, sentence: string): Promise<string | undefined> {
    if (!pronWord || !respelling.trim()) return undefined
    setPronBusy(`hear:${respelling}`)
    try {
      const response = await fetch(`${API}/api/pronunciation/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: pronWord, respelling, sentence, voice_id: voiceId }),
      })
      const body = await response.json().catch(() => ({})) as { audio_base64?: string; detail?: string }
      if (!response.ok || !body.audio_base64) throw new Error(body.detail || 'No se pudo generar la muestra.')
      const bytes = Uint8Array.from(atob(body.audio_base64), character => character.charCodeAt(0))
      const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }))
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
      return url
    } catch (error) {
      setMessage(error instanceof Error ? publicCopy(error.message) : 'No se pudo generar la muestra.')
      return undefined
    } finally { setPronBusy('') }
  }

  async function saveRespelling(respelling: string) {
    if (!pronWord || !respelling.trim()) return
    setPronBusy(`save:${respelling}`)
    try {
      const response = await fetch(`${API}/api/pronunciation-lexicon/entries`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ term: pronWord, pronunciation: respelling }),
      })
      const body = await response.json().catch(() => ({})) as { entries?: Record<string, string>; detail?: string }
      if (!response.ok) throw new Error(body.detail || 'No se pudo guardar la pronunciación.')
      setPronSaved(body.entries ?? {})
      setPronAnalysis(current => current && { ...current, saved_respelling: respelling })
      setMessage(`«${pronWord}» se pronunciará «${respelling}» en este libro y en los siguientes. `
        + 'Vuelve a narrar la frase para oírla con el cambio.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se pudo guardar la pronunciación.')
    } finally { setPronBusy('') }
  }

  async function decideSentence(sentence: ProductionSentence, decision: 'accept' | 'retry' | 'correct') {
    if (!productionJob || reviewingSentence) return
    setReviewingSentence(sentence.sentence_id)
    const jobId = productionJob.job_id
    setMessage(decision === 'accept' ? 'Guardando tu decisión…'
      : decision === 'correct' ? 'Aplicando tu corrección y narrando el párrafo…' : 'Narrando el párrafo de nuevo…')
    try {
      const response = await fetch(`${API}/api/jobs/${jobId}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sentence_id: sentence.sentence_id, decision,
          corrected_text: sentenceCorrections[sentence.sentence_id] ?? '' }),
      })
      const body = await response.json().catch(() => ({})) as { detail?: string; sentence?: ProductionSentence }
      if (!response.ok) throw new Error(body.detail || 'No se pudo guardar la decisión de revisión.')
      const updated = body.sentence
      if (updated) {
        setProductionJob(current => current?.job_id === jobId
          ? { ...current, sentences: current.sentences.map(item => item.sentence_id === updated.sentence_id ? updated : item) }
          : current)
      }
      if (decision === 'accept') {
        dismissRenarrated(sentence.sentence_id)
        setMessage('Párrafo aceptado.')
      } else {
        setRenarrated(current => ({ ...current, [sentence.sentence_id]: true }))
        setLastRenarrated(sentence.sentence_id)
        setSentenceCorrections(current => {
          const next = { ...current }
          delete next[sentence.sentence_id]
          return next
        })
        setMessage(updated?.state === 'verified'
          ? 'Nueva locución lista y comprobada: coincide con el texto. Escúchala abajo.'
          : 'Nueva locución lista, pero todavía hay diferencias con el texto. Escúchala y decide.')
      }
    } catch (error) {
      setMessage(error instanceof Error ? publicCopy(error.message) : 'No se pudo guardar la revisión.')
      setReviewingSentence('')
      return
    }
    setReviewingSentence('')
    // Catch up with the job (the book is assembled once nothing is left)
    // without replacing the message about the paragraph just decided.
    void watchProductionJob(jobId, true).catch(error =>
      setMessage(error instanceof Error ? error.message : 'No se pudo seguir el avance del trabajo.'))
  }

  // A new range is a different reading of the book: its sections and edits
  // start again from what those pages say.
  async function applyPageRange() {
    const value = pageRangeDraft.trim()
    if (value === pageRange || !draftId) return
    const selectionVersion = ++bookSelectionVersion.current
    stopCurrentWork(selectionVersion)
    setStructureLoading(true)
    try {
      await flushDraft()
      const response = await fetch(`${API}/api/drafts/${encodeURIComponent(draftId)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: { ...latestState.current, pages: value, selected_ids: null, edits: NO_EDITS },
                               revision: draftRevision.current }),
      })
      const body = await response.json().catch(() => ({})) as DraftView & { detail?: unknown }
      if (bookSelectionVersion.current !== selectionVersion) return
      if (!response.ok || !isDraftView(body)) throw new Error(errorDetail(body.detail, 'No se han podido aplicar esas páginas.'))
      applyDraftView(body)
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        setPageRangeDraft(pageRange)
        setMessage(error instanceof Error ? error.message : 'No se han podido aplicar esas páginas.')
      }
    } finally {
      if (bookSelectionVersion.current === selectionVersion) setStructureLoading(false)
    }
  }

  function dismissRenarrated(sentenceId: string) {
    setRenarrated(current => {
      const next = { ...current }
      delete next[sentenceId]
      return next
    })
  }

  function trackPlayback() {
    const time = playerRef.current?.currentTime ?? 0
    const phrase = alignment.findIndex(item => time >= item.start && time < item.end)
    setCurrentPhrase(phrase)
  }

  function switchWorkspaceTab(tab: WorkspaceTab) {
    if (tab !== 'review') playerRef.current?.pause()
    setWorkspaceTab(tab)
  }

  const alignedParagraphs: TimedPhrase[][] = []
  for (let index = 0; index < alignment.length; index += 3) {
    alignedParagraphs.push(alignment.slice(index, index + 3))
  }
  const canGenerateAudio = Boolean(capabilities?.tts_configured || (capabilities?.voice_id_configurable && voiceId))
  const selectedVoice = voices.find(voice => voice.voice_id === (voiceId || capabilities?.voice))
  // Paragraphs waiting for a person, in reading order: step 3 lists them, and
  // each opens in step 2, where it is heard and decided.
  const awaitingDecision = (productionJob?.sentences ?? [])
    .filter(sentence => ['needs_human_review', 'unresolved', 'failed'].includes(sentence.state))
  const bookChapters: BookChapter[] = outline?.chapters ?? []
  const totalParagraphCount = bookChapters.reduce((count, chapter) => count + chapter.paragraphs.length, 0)
  // The book changed after this production: its audio and ZIP are the earlier text.
  const productionIsStale = Boolean(productionJob && producedSignature && producedSignature !== draftSignature)
  useEffect(() => { setDownloadNotice(null) }, [draftId, productionIsStale])
  // A finished book the release gate has not passed yet can still be taken
  // away, under a name that says it is not validated.
  const release = productionJob?.release
  const unvalidated = productionJob?.state === 'completed' && Boolean(release) && !['PASSED', 'RELEASED'].includes(release?.state ?? '')
  const generating = packageBusy || directDownloadBusy
  const jobRunning = Boolean(productionJob && ['queued', 'running'].includes(productionJob.state))
  // Where the ZIP of the book as it stands is, in the way step 3 builds it.
  const reviewedReady = Boolean(productionJob?.state === 'completed' && !productionIsStale)
  const directReady = Boolean(directZip?.status.state === 'completed' && directZip.signature === draftSignature)
  const zipReady = reviewMode ? reviewedReady : directReady
  const zipPackage: PackageView | null | undefined = reviewMode ? productionJob?.package : directZip?.status.package
  const buildView = reviewMode
    ? (productionJob ? jobStages(productionJob.state, productionJob.pipeline) : null)
    : (directProgress ? directStages(directProgress) : directZip ? directStages(directZip.status) : null)
  const buildSeconds = reviewMode ? productionJob?.pipeline?.total_seconds
    : Object.values(directZip?.status.timings ?? {}).reduce((sum, seconds) => sum + seconds, 0) || undefined
  // What changed since that ZIP, from the job's structure and the outline.
  const changesSinceZip = useMemo(() => {
    if (!productionIsStale || !jobStructure || !outline) return ''
    return describeChanges(changesSince(
      jobStructure.sections.map(section => ({ title: section.title, units: section.units })),
      outline.sections.map(section => ({ title: section.title, units: section.units }))))
  }, [productionIsStale, jobStructure, outline])
  const directStale = Boolean(!reviewMode && directZip && directZip.signature !== draftSignature)
  let exportTone: ExportTone = 'idle'
  let exportStatus = 'Todavía no hay un ZIP de este libro. «Descargar ZIP» lo genera y lo descarga al terminar.'
  if (generating || (reviewMode && jobRunning)) {
    const active = buildView?.find(stage => stage.state === 'active')
    exportTone = 'busy'
    exportStatus = `${active?.label ?? 'Preparando el audiolibro'}…`
  } else if (restoring && !productionJob) {
    exportTone = 'busy'
    exportStatus = 'Recuperando el libro y su último ZIP…'
  } else if ((reviewMode && productionIsStale) || directStale) {
    exportTone = 'stale'
    exportStatus = 'Has cambiado el libro desde el último ZIP. «Regenerar ZIP» lo actualiza: solo se genera lo nuevo o corregido.'
  } else if (zipReady) {
    exportTone = 'ready'
    exportStatus = ['ZIP listo', zipPackage?.sections ? `${zipPackage.sections} archivos` : '',
      formatTotalTime(zipPackage?.total_time), formatBytes(zipPackage?.bytes),
      !reviewMode ? 'sin verificar' : unvalidated ? 'sin validar' : 'validado'].filter(Boolean).join(' · ')
  } else if (reviewMode && productionJob?.state === 'partially_completed' && !productionJob.error
             && awaitingDecision.some(item => item.state !== 'failed')) {
    exportTone = 'review'
    exportStatus = `${awaitingDecision.length === 1 ? 'Un párrafo necesita' : `${awaitingDecision.length} párrafos necesitan`} tu decisión antes de crear el ZIP.`
  } else if (reviewMode && productionJob && ['partially_completed', 'failed', 'cancelled'].includes(productionJob.state)) {
    exportTone = 'error'
    exportStatus = productionJob.state === 'cancelled'
      ? 'La producción se detuvo. «Regenerar ZIP» continúa: lo ya narrado no se paga de nuevo.'
      : `La producción no ha terminado${productionJob.error ? `: ${publicCopy(productionJob.error)}` : '.'} El audio ya narrado se conserva: «Regenerar ZIP» continúa sin volver a pagarlo.`
  }
  const canBuild = Boolean(draftId) && canGenerateAudio && selectedNarrationIds.length > 0

  async function reviewPreview() {
    if (!audio || !transcript || reviewBusy) return
    setReviewBusy(true)
    setSampleError('')
      setMessage('Revisando la muestra de audio con el texto…')
    try {
      const audioResponse = await fetch(audio)
      const audioBlob = await audioResponse.blob()
      const form = new FormData()
      form.append('file', audioBlob, `${book.replace(/\.[^.]+$/, '') || 'preview'}.mp3`)
      form.append('source_text', transcript)
      const response = await fetch(`${API}/api/audio/review`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No se pudo revisar la narración.')
      }
      const result: AudioReview = await response.json()
      setReview(result)
      setMessage(`Revisión terminada. Se encontraron ${result.issues.length} diferencias para revisar.`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'No se pudo revisar la narración.'
      setMessage(detail)
      setSampleError(detail)
    } finally { setReviewBusy(false) }
  }

  async function regenerateRetake() {
    if (!transcript.trim() || retakeBusy) return
    setRetakeBusy(true)
    setSampleError('')
    setMessage('Generando de nuevo el fragmento editado…')
    try {
      const form = new FormData()
      form.append('text', transcript)
      form.append('voice_id', voiceId)
      const response = await fetch(`${API}/api/audio/retake`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No se pudo regenerar el fragmento.')
      }
      const result: { audio_base64: string; audio_mime_type: string; transcript: string; alignment?: TimedPhrase[] | null } = await response.json()
      const bytes = Uint8Array.from(atob(result.audio_base64), character => character.charCodeAt(0))
      const nextAudio = URL.createObjectURL(new Blob([bytes], { type: result.audio_mime_type }))
      if (audio) URL.revokeObjectURL(audio)
      setAudio(nextAudio)
      setAlignment(result.alignment ?? [])
      setCurrentPhrase(-1)
      setReview(undefined)
      setMessage('El fragmento corregido ya está listo. Reprodúcelo para revisarlo.')
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'No se pudo regenerar el fragmento.'
      setMessage(detail)
      setSampleError(detail)
    } finally { setRetakeBusy(false) }
  }

  function pronunciationEditor(sentence: ProductionSentence) {
    return <PronunciationEditor sentence={sentence} open={pronSentence === sentence.sentence_id} word={pronWord}
      analysis={pronSentence === sentence.sentence_id ? pronAnalysis : undefined} custom={pronCustom} busy={pronBusy}
      saved={pronSaved} onAnalyse={word => void analysePronunciation(word, sentence.sentence_id)}
      onHear={respelling => hearRespelling(respelling, sentence.text)} onSave={respelling => void saveRespelling(respelling)}
      onCustom={setPronCustom}/>
  }

  // One change to the text. It is worked out from the outline after the saves
  // before it have come back: a phrase's place is only known in the book as
  // it stands, and working from an older outline would bring back what was
  // just deleted.
  function changeBook(change: (current: BookEdits, sections: EditorSection[]) => BookEdits) {
    setEditBusy(true)
    const run = async () => {
      try {
        // An earlier change that could not be saved is not in the outline yet:
        // working from it would undo that change without anyone noticing.
        if (!await flushDraft()) return
        const current = latestState.current.edits
        const next = change(current, modelFromOutline(latestOutline.current as BookOutline).sections)
        if (next === current) return
        latestState.current = { ...latestState.current, edits: next }
        setUndoStack(stack => [...stack.slice(-49), current])
        setEdits(next)
        await saveDraft()
      } finally {
        setEditBusy(false)
      }
    }
    editChain.current = editChain.current.then(run, run)
  }

  function undoChange() {
    const previous = undoStack[undoStack.length - 1]
    if (!previous) return
    setUndoStack(stack => stack.slice(0, -1))
    setEdits(previous)
  }

  // A planilla written like MODELO ONCE.docx fills the form; a person checks it
  // before anything is narrated.
  async function importPlanilla(file?: File) {
    if (!file) return
    setPlanillaBusy(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const response = await fetch(`${API}/api/planilla/parse`, { method: 'POST', body: form })
      const body = await response.json().catch(() => ({})) as { planilla?: Planilla & { title?: string; author?: string }; warnings?: string[]; detail?: unknown }
      if (!response.ok || !body.planilla) throw new Error(errorDetail(body.detail, 'No se ha podido leer la planilla.'))
      const { title, author, ...values } = body.planilla
      setPlanillaValues(current => ({ ...current, ...values }))
      if (title || author) setBookMetadata(current => ({ ...current, title: title || current.title, author: author ?? current.author }))
      setOnceEnabled(true)
      setPlanillaWarnings(body.warnings ?? [])
      setPlanillaStatus(`Planilla «${file.name}» leída. Revisa los datos antes de producir el audiolibro.`)
    } catch (error) {
      setPlanillaStatus(error instanceof Error ? error.message : 'No se ha podido leer la planilla.')
    } finally { setPlanillaBusy(false) }
  }

  function planillaText(name: keyof Planilla): string {
    const value = planillaValues[name]
    if (typeof value === 'string') return value
    const resolved = outline?.planilla?.[name] ?? planillaDefaults?.[name]
    return typeof resolved === 'string' ? resolved : ''
  }

  function setPlanillaText(name: keyof Planilla, value: string) {
    setPlanillaValues(current => ({ ...current, [name]: value }))
  }

  const editorModel = useMemo<EditorModel | undefined>(() => {
    if (productionJob && !editingProduced && (productionJob.sentences.length || jobStructure)) return modelFromJob(productionJob, jobStructure)
    return outline ? modelFromOutline(outline) : undefined
  }, [productionJob, jobStructure, outline, editingProduced])

  const processedSentences = Object.entries(productionJob?.progress ?? {})
    .reduce((sum, [state, count]) => sum + (['pending', 'generating'].includes(state) ? 0 : count), 0)

  const verificationLabel: Record<string, string> = {
    asr_primary: 'Comprobada con el reconocimiento automático.',
    asr_secondary: 'Confirmada por un segundo reconocedor.',
    auto_accepted: 'Aceptada automáticamente con una duda menor: conviene escucharla.',
    human: 'Aceptada por una persona.',
  }

  function renderInspector(unit: EditorUnit, section: EditorSection, seek: (seconds: number) => void): ReactNode {
    const sentence = productionJob?.sentences.find(item => item.sentence_id === unit.sentenceId)
    const position = section.units.indexOf(unit) + 1
    if (!sentence) {
      return <div className="ed-detail"><h3>Frase {position}</h3>
        <p className="ed-hint">Esta frase todavía no tiene datos de producción.</p></div>
    }
    const needsDecision = ['needs_human_review', 'unresolved', 'failed'].includes(sentence.state)
    const busyHere = reviewingSentence === sentence.sentence_id
    const fresh = Boolean(renarrated[sentence.sentence_id])
    const expectedRanges = sentence.issues.map(issue => ({ start: issue.expected_start, end: issue.expected_end }))
    const recognizedRanges = sentence.issues.map(issue => ({ start: issue.recognized_start, end: issue.recognized_end }))
    const editable = unit.editable !== false
    const actions = <>
      {editable && <label className="qa-correction">Texto corregido (sólo si vas a corregirlo)
        <textarea rows={3} value={sentenceCorrections[sentence.sentence_id] ?? sentence.text}
          onChange={event => setSentenceCorrections(current => ({ ...current, [sentence.sentence_id]: event.target.value }))}/>
      </label>}
      {!editable && <p className="ed-hint">Este texto viene de la planilla o del índice: se corrige en la planilla y se vuelve a producir.</p>}
      <div className="review-controls">
        {needsDecision && <button type="button" className="package-download" disabled={Boolean(reviewingSentence) || !sentence.audio_path}
          onClick={() => void decideSentence(sentence, 'accept')}>{busyHere ? 'Guardando…' : 'La locución es correcta'}</button>}
        <button type="button" className="package-download" disabled={Boolean(reviewingSentence)}
          onClick={() => void decideSentence(sentence, 'retry')}>Volver a narrarla</button>
        {editable && <button type="button" className="package-download" disabled={Boolean(reviewingSentence)}
          onClick={() => void decideSentence(sentence, 'correct')}>Corregir el texto y narrarla</button>}
      </div>
      {pronunciationEditor(sentence)}
    </>
    return <div className="ed-detail" aria-live="polite">
      <div className="ed-detail-head">
        <span className={`qa-badge qa-${sentence.state}`}>{fresh && sentence.state === 'verified' ? 'Corregida y comprobada' : sentenceStateLabel(sentence.state)}</span>
        <span className="ed-hint">Archivo {section.fileNumber} · {KIND_LABEL[section.kind] ?? 'Sección'} · frase {position}</span>
      </div>
      <h3>Frase {position}{unit.role === 'note' && unit.note ? ` · Nota ${unit.note}` : ''}</h3>
      <dl className="ed-detail-facts">
        <dt>Duración</dt><dd>{formatClock(unit.duration)}</dd>
        {unit.clip && <><dt>En el archivo</dt><dd>{formatClock(unit.clip[0])} – {formatClock(unit.clip[1])}</dd></>}
        {unit.pageBreaks.length > 0 && <><dt>Página</dt><dd>Empieza la página {unit.pageBreaks.join(', ')}</dd></>}
        <dt>Intentos</dt><dd>{sentence.technical_attempts} técnicos · {sentence.quality_retakes} retomas</dd>
      </dl>
      {busyHere && <p className="production-working" role="status"><span className="spinner" aria-hidden="true"/>Narrando la nueva versión de la frase…</p>}
      {/* The reading a decision just produced, played at once; a new version
          in the URL, so the browser never replays the old one. */}
      {fresh && sentence.audio_path && productionJob && <label className="qa-audio">Escucha la nueva locución
        <audio key={sentence.updated_at || sentence.audio_path} controls preload="auto"
          autoPlay={lastRenarrated === sentence.sentence_id}
          src={`${API}/api/jobs/${encodeURIComponent(productionJob.job_id)}/sentences/${encodeURIComponent(sentence.sentence_id)}/audio?v=${encodeURIComponent(sentence.updated_at || sentence.audio_path)}`}/>
      </label>}
      {needsDecision ? <>
        <div className="qa-compare">
          <section><h5>Texto del libro</h5><p>{highlightDifferences(sentence.text, expectedRanges)}</p></section>
          <section><h5>Lo que se oye en el audio</h5><p>{sentence.recognized_text
            ? highlightDifferences(sentence.recognized_text, recognizedRanges)
            : <em>No se obtuvo transcripción de esta frase.</em>}</p></section>
        </div>
        {sentence.issues.length > 0 && <ul className="qa-issue-list">{sentence.issues.map((issue, index) =>
          <li key={`${issue.kind}-${index}`}><strong>{qaIssueLabel(issue.category, issue.kind)}.</strong>{issue.reason ? ` ${issue.reason}` : ''}
            {typeof issue.audio_start === 'number' && <span className="qa-issue-time">
              {' '}Se oye en el segundo {issue.audio_start.toFixed(1).replace('.', ',')} de la frase.
              {sentence.audio_path && <button type="button" className="qa-seek" onClick={() => seek(
                unit.clip && typeof issue.chapter_time === 'number' ? issue.chapter_time - unit.clip[0] : issue.audio_start ?? 0)}>
                Escuchar desde ahí</button>}
            </span>}</li>)}</ul>}
        {sentence.diagnosis?.recommendation && <p className="qa-diagnosis"><strong>
          {sentence.diagnosis.origin ? `Origen probable: ${originLabel(sentence.diagnosis.origin)}. ` : ''}</strong>
          {sentence.diagnosis.recommendation}</p>}
        {sentence.last_error && <p className="qa-note">{publicCopy(sentence.last_error)}</p>}
        {!sentence.audio_path && <p className="qa-note">No hay audio guardado para esta frase, así que hay que volver a narrarla.</p>}
        {actions}
      </> : <>
        <p className="ed-detail-text">{sentence.text}</p>
        {sentence.state === 'verified' && <p className="ed-hint">{verificationLabel[sentence.verification ?? ''] ?? 'Correcta.'}
          {sentence.last_error && sentence.verification === 'auto_accepted' ? ` ${publicCopy(sentence.last_error)}` : ''}</p>}
        {['pending', 'generating'].includes(sentence.state) && <p className="ed-hint">{sentence.state === 'generating' ? 'Narrando y comprobando esta frase…' : 'En cola para narrarse.'}</p>}
        {sentence.state === 'verified' && <details className="ed-change">
          <summary>Cambiar esta frase</summary>
          <p className="ed-hint">Si al escucharla oyes algo mal que la comprobación no detectó, puedes narrarla de nuevo (tiene coste) o corregir su texto.</p>
          {actions}
        </details>}
      </>}
    </div>
  }

  const idlePanel = <div className="ed-idle">
    <section className="ed-detail">
      <h3>Antes de producir</h3>
      <p>Así quedará el audiolibro: {editorModel?.sections.length ?? 0} archivos de audio, con sus niveles, páginas y notas.
         Nada se narra ni se paga hasta que pulses «Producir el audiolibro con revisión».</p>
      <label className="planilla-toggle"><input type="checkbox" checked={onceEnabled} onChange={event => setOnceEnabled(event.target.checked)}/>
        Estructura ONCE (archivos iniciales, índice, notas y frase final)</label>
      {onceEnabled && <button type="button" className="ed-secondary" onClick={() => setShowPlanilla(true)}>Revisar la planilla de producción</button>}
      {onceEnabled && (outline?.planilla?.missing?.length ?? 0) > 0 && <p className="ed-hint">Falta en la planilla: {outline?.planilla?.missing?.join(', ')}.</p>}
      {(outline?.warnings?.length ?? 0) > 0 && <ul className="ed-warnings">{outline?.warnings.map(warning =>
        <li key={`${warning.code}-${warning.message}`}>{warning.message}</li>)}</ul>}
    </section>
    {audio && <section id="listen-section" className="player workflow-card" aria-labelledby="player-title"><div className="player-heading"><span className="player-icon" aria-hidden="true"><Volume2 size={21}/></span><div className="player-title"><h2 id="player-title">Vista previa de audio</h2><span>{bookMetadata.title || book} · Español</span></div></div><audio ref={playerRef} controls src={audio} onTimeUpdate={trackPlayback} onSeeked={trackPlayback} aria-label={`Escuchar la vista previa narrada de ${bookMetadata.title || book}`}/><div className="player-playback"><label htmlFor="playback-speed">Velocidad<select id="playback-speed" value={playbackRate} onChange={event => setPlaybackRate(Number(event.target.value))}><option value={0.75}>0,75×</option><option value={1}>1×</option><option value={1.25}>1,25×</option><option value={1.5}>1,5×</option><option value={1.75}>1,75×</option><option value={2}>2×</option></select></label></div><a className="download" href={audio} download={`${bookMetadata.title || book.replace(/\.[^.]+$/, '')}-vista-previa.mp3`}><Download size={17} aria-hidden="true"/>Descargar muestra MP3</a></section>}
    {transcript && <section className="transcript workflow-card audio-transcript" aria-labelledby="transcript-title"><h2 id="transcript-title">Texto de la muestra</h2><details><summary>Mostrar texto sincronizado</summary>{alignment.length > 0 ? <div className="timed-transcript" aria-live="off">{alignedParagraphs.map((paragraph, paragraphIndex) => <p key={`paragraph-${paragraphIndex}`}>{paragraph.map(phrase => { const index = alignment.indexOf(phrase); const active = index === currentPhrase; return <span key={`${phrase.text_start}-${index}`} className={active ? 'timed-phrase is-speaking' : 'timed-phrase'} aria-current={active ? 'true' : undefined}>{phrase.text} </span>})}</p>)}</div> : <><p className="sync-note">{'No se pudieron obtener tiempos para sincronizar esta muestra. El texto sigue disponible por parrafos.'}</p>{transcript.split(/\n{2,}/).map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>)}</>}</details>
      {capabilities?.audio_review_configured && <div className="review-controls"><button type="button" className="package-download" onClick={reviewPreview} disabled={reviewBusy || !audio}>{reviewBusy ? 'Revisando el audio…' : 'Revisar calidad del audio'}</button><small>Las diferencias son avisos para que una persona las revise.</small></div>}
      {sampleError && <p className="audio-setup-note" role="alert">{publicCopy(sampleError)}</p>}
      {review && <section className="review-results" aria-labelledby="review-title"><h3 id="review-title">{review.verdict === 'verified' ? 'El audio coincide con el texto' : review.verdict === 'ambiguous' ? 'Diferencias ambiguas: requieren tu criterio' : `Diferencias detectadas: ${review.issues.length}`}</h3><p>{publicCopy(review.review_note)}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, index) => <li key={`${issue.kind}-${index}`}><strong>{qaIssueLabel(issue.category, issue.kind)}:</strong> {issue.expected ? `Texto: “${issue.expected}”. ` : ''}{issue.recognized ? `Audio: “${issue.recognized}”.` : ''}{issue.reason ? <small> {issue.reason}</small> : null}</li>)}</ul>}<label htmlFor="retake-text">Edita el texto de esta muestra<textarea id="retake-text" value={transcript} onChange={event => setTranscript(event.target.value)} rows={5}/></label><button type="button" className="package-download" onClick={regenerateRetake} disabled={retakeBusy}>{retakeBusy ? 'Generando…' : 'Generar muestra corregida'}</button></section>}</section>}
  </div>

  const pair = (bookMetadata.title || outline?.title || '').trim()
  const titleLine = bookMetadata.author.trim() ? `${pair}, De ${bookMetadata.author.trim()}` : pair
  const planillaPanel = <div className="planilla-panel" aria-labelledby="planilla-title">
    <header>
      <div>
        <h2 id="planilla-title">Planilla de producción ONCE</h2>
        <p>Los datos de esta planilla forman los archivos 1, 2 y 3 del audiolibro y la frase final. Puedes importar la
           planilla de la ONCE (.docx) o escribirlos aquí; el texto legal y la línea de producción vienen de la planilla modelo.</p>
      </div>
      <div className="planilla-import">
        <label className="ed-secondary">{planillaBusy ? 'Leyendo…' : 'Importar planilla (.docx)'}
          <input type="file" accept=".docx" className="visually-hidden" disabled={planillaBusy}
            onChange={event => { const file = event.target.files?.[0]; event.currentTarget.value = ''; void importPlanilla(file) }}/></label>
        <button type="button" className="ed-secondary" onClick={() => setShowPlanilla(false)}>Volver a la estructura</button>
      </div>
    </header>
    {planillaStatus && <p className="ed-notice" role="status" aria-live="polite">{planillaStatus}</p>}
    {planillaWarnings.length > 0 && <ul className="ed-warnings" aria-label="Avisos al leer la planilla">{planillaWarnings.map(warning => <li key={warning}>{warning}</li>)}</ul>}
    <label className="planilla-toggle"><input type="checkbox" checked={onceEnabled} onChange={event => setOnceEnabled(event.target.checked)}/>
      Producir con la estructura ONCE</label>
    <fieldset disabled={!onceEnabled}>
      <legend>Archivo 1 · Título, autor y aviso legal</legend>
      <div className="planilla-row">
        <label>Título<input value={bookMetadata.title} maxLength={500} onChange={event => setBookMetadata(current => ({ ...current, title: event.target.value }))}/></label>
        <label>Autor<input value={bookMetadata.author} maxLength={500} onChange={event => setBookMetadata(current => ({ ...current, author: event.target.value }))}/></label>
      </div>
      <div className="planilla-row">
        <label>Número ABSYS<small>Identifica el audiolibro; va en los metadatos, no se narra.</small><input value={planillaText('absys')} maxLength={120} onChange={event => setPlanillaText('absys', event.target.value)}/></label>
        <label>Página del archivo 1<small>El archivo 1 acaba siempre en la página 1.</small><input inputMode="numeric" value={planillaText('file1_page')} maxLength={8} onChange={event => setPlanillaText('file1_page', event.target.value)}/></label>
      </div>
      <p className="planilla-preview">Se lee: «{titleLine || 'Título, De Autor'}», y a continuación el aviso legal.</p>
      <label>Texto legal de la ONCE<small>Tal como lo da la planilla modelo; no se resume ni se cambia.</small>
        <textarea rows={7} value={planillaText('legal_text')} onChange={event => setPlanillaText('legal_text', event.target.value)}/></label>
      {planillaValues.legal_text !== undefined && <button type="button" className="ed-secondary" onClick={() => setPlanillaValues(current => {
        const next = { ...current }; delete next.legal_text; return next })}>Restaurar el texto de la planilla modelo</button>}
    </fieldset>
    <fieldset disabled={!onceEnabled}>
      <legend>Archivo 2 · Datos bibliográficos</legend>
      <div className="planilla-row">
        <label>Edición y publicación<small>Por ejemplo «PRIMERA EDICIÓN MADRID, FEBRERO DE 2026».</small><input value={planillaText('edition_line')} maxLength={500} onChange={event => setPlanillaText('edition_line', event.target.value)}/></label>
        <label>Editorial<input value={planillaText('publisher_line')} maxLength={500} onChange={event => setPlanillaText('publisher_line', event.target.value)}/></label>
      </div>
      <div className="planilla-row">
        <label>Línea de producción<input value={planillaText('production_line')} maxLength={500} onChange={event => setPlanillaText('production_line', event.target.value)}/></label>
        <label>Fecha de grabación<input value={planillaText('production_date')} maxLength={60} onChange={event => setPlanillaText('production_date', event.target.value)}/></label>
      </div>
      <div className="planilla-row">
        <label>Página de los datos bibliográficos<small>En el modelo, la 2.</small><input inputMode="numeric" value={planillaText('file2_page')} maxLength={8} onChange={event => setPlanillaText('file2_page', event.target.value)}/></label>
        <label>ISBN<small>Solo si la planilla lo pide.</small><input value={planillaText('isbn')} maxLength={40} onChange={event => setPlanillaText('isbn', event.target.value)}/></label>
      </div>
    </fieldset>
    <fieldset disabled={!onceEnabled}>
      <legend>Archivo 3 · Índice</legend>
      <div className="planilla-row">
        <label>Página del índice<small>En el modelo, la 3.</small><input inputMode="numeric" value={planillaText('index_page')} maxLength={8} onChange={event => setPlanillaText('index_page', event.target.value)}/></label>
        <label>El índice incluye<select value={String(planillaValues.index_depth ?? outline?.planilla?.index_depth ?? 2)}
          onChange={event => setPlanillaValues(current => ({ ...current, index_depth: Number(event.target.value) }))}>
          <option value="1">Capítulos</option><option value="2">Capítulos y subcapítulos</option></select></label>
      </div>
      <p className="planilla-preview">Se genera a partir de los capítulos del libro, con la página en la que empieza cada uno, y termina con «Fin de índice.».</p>
    </fieldset>
    <fieldset disabled={!onceEnabled}>
      <legend>Notas y frase final</legend>
      <div className="planilla-row">
        <label>Inicio de nota<input value={planillaText('note_open')} maxLength={80} onChange={event => setPlanillaText('note_open', event.target.value)}/></label>
        <label>Fin de nota<input value={planillaText('note_close')} maxLength={80} onChange={event => setPlanillaText('note_close', event.target.value)}/></label>
      </div>
      <label>Frase final<small>{'{title_and_author}'} es «Título, De Autor».</small><input value={planillaText('closing')} maxLength={600} onChange={event => setPlanillaText('closing', event.target.value)}/></label>
      <p className="planilla-preview">Se lee al final del último capítulo: «{(planillaText('closing') || 'Aquí finaliza la grabación de {title_and_author}.').replace('{title_and_author}', titleLine || 'Título, De Autor').replace('{title}', pair).replace('{author}', bookMetadata.author)}»</p>
    </fieldset>
  </div>

  // Step 2 is a screen-sized editor whose columns scroll on their own; the
  // other steps are documents, and the page itself scrolls.
  return <main className={workspaceTab === 'review' ? 'shell editor-shell' : 'shell editor-shell is-document'}>
    <a className="skip" href="#main">Saltar al contenido principal</a>
    <header className="top editor-top"><a className="brand" href="/" aria-label="Inicio de Soni2"><img className="brand-logo" src="/soni2-logo.svg" alt="Soni2" width="353" height="83"/></a><span className="top-note">Una forma más tranquila de disfrutar los libros</span></header>
    <section id="main" className="hero workspace editor-workspace" aria-label="Espacio de trabajo de Soni2">
      <nav className="workspace-tabs" role="tablist" aria-label="Flujo de trabajo del audiolibro">
        <button type="button" role="tab" id="tab-upload" aria-controls="panel-upload" aria-selected={workspaceTab === 'upload'} onClick={() => switchWorkspaceTab('upload')}><span>1</span>Subir libro</button>
        <button type="button" role="tab" id="tab-review" aria-controls="panel-review" aria-selected={workspaceTab === 'review'} disabled={!transcript && !editorModel} onClick={() => switchWorkspaceTab('review')}><span>2</span>Escuchar y revisar</button>
        <button type="button" role="tab" id="tab-export" aria-controls="panel-export" aria-selected={workspaceTab === 'export'} disabled={!transcript && !editorModel} onClick={() => switchWorkspaceTab('export')}><span>3</span>Exportar</button>
      </nav>
      <section id="panel-upload" className="workspace-panel upload-panel" role="tabpanel" aria-labelledby="tab-upload" hidden={Boolean(draftId) && workspaceTab !== 'upload'}>
        <section id="upload-section" className="upload-card workflow-card upload-experience" aria-labelledby="upload-title">
          <h1 id="upload-title" className="visually-hidden">Subir libro</h1>
          <input ref={input} className="visually-hidden" type="file" accept=".pdf,.epub,.docx,.txt,.xml" aria-label="Elige un libro en formato PDF, EPUB, Word, texto o XML" onChange={e => { const file = e.target.files?.[0]; e.currentTarget.value = ''; selectBook(file) }}/>
          <div className={"upload-dropzone" + (dragActive ? " is-dragging" : "") + (draftId ? " has-file" : "")} onDragOver={event => { event.preventDefault(); if (!busy) setDragActive(true) }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragActive(false) }} onDrop={event => { event.preventDefault(); setDragActive(false); if (!busy) selectBook(event.dataTransfer.files[0]) }}>
            <span className="upload-drop-icon" aria-hidden="true"><Upload size={25}/></span>
            {draftId ? <><strong className="upload-file-name" title={book}>{book}</strong><span className="upload-drop-copy">Archivo preparado. Puedes elegir otro si lo necesitas.</span></> : <><strong>Arrastra tu libro aqui</strong><span className="upload-drop-copy">o selecciona un archivo desde tu dispositivo</span></>}
            <button className="choose upload-select" type="button" onClick={() => input.current?.click()} disabled={busy}>{busy ? <span className="spinner" aria-hidden="true"/> : <BookOpen size={18} aria-hidden="true"/>}{busy ? "Preparando..." : draftId ? "Cambiar archivo" : "Elegir archivo"}</button>
            <span className="upload-formats">PDF <i/> EPUB <i/> DOCX <i/> TXT <i/> XML</span>
          </div>
          {draftId && !canGenerateAudio && <p className="audio-setup-note" role="status">No hay una voz configurada para generar la vista previa. Revisa la configuracion del servicio de narracion.</p>}
          {capabilityError && <div className="audio-setup-note" role="alert">No se pudo conectar con el servicio. <button type="button" onClick={() => void refreshCapabilities()}>Reintentar</button></div>}
          <p className="privacy upload-privacy">Tu libro se trata con cuidado y privacidad.</p>
          {capabilities?.tts_configured && <p className="sync-note upload-voice-status"><CheckCircle2 size={16} aria-hidden="true"/> Narración lista para generar{capabilities.voice_name ? ` · voz ${capabilities.voice_name}` : ''}{capabilities.voice_language ? ` (${capabilities.voice_language})` : ''}</p>}
          {capabilities?.offline_mock_mode && <p className="audio-setup-note" role="status">Modo simulado activo: Soni2 genera audio de prueba sin coste y sin voz real. Configura un servicio de voz para producir el audiolibro definitivo.</p>}
          {capabilities?.tts_configured && !capabilities.audio_review_configured && <p className="audio-setup-note" role="status">No hay servicio de transcripción configurado, así que la revisión automática de calidad no se puede ejecutar y cada frase pasará a revisión humana.</p>}
          {selectedVoice && selectedVoice.accent_verified === false && <p className="audio-setup-note" role="status">No se ha podido comprobar el acento de la voz «{selectedVoice.name}». Escucha la vista previa para confirmar que es el que necesitas.{capabilities?.requires_peninsular_voice ? ' El servidor exige una voz de España y la rechazará hasta que se declare su acento.' : ''}</p>}
          {selectedVoice && selectedVoice.accent_verified !== false && !isSpanishFromSpain(selectedVoice) && <p className="audio-setup-note" role="alert">La voz «{selectedVoice.name}» es {selectedVoice.labels?.accent || 'de otra región'} y no español de España.{capabilities?.requires_peninsular_voice ? ' El servidor rechazará la producción con esta voz.' : ' Elige una voz es-ES si necesitas acento de España.'}</p>}
          {(capabilities?.voice_id_configurable || voiceLoadError) && <div className="voice-controls" role="group" aria-label="Configuracion de voz e idioma">{capabilities?.voice_id_configurable && <label htmlFor="voice-id">{'Voz de narraci\u00f3n'}<select id="voice-id" className="voice-id-input" value={voiceId || capabilities?.voice || voices[0]?.voice_id || ''} onChange={event => setVoiceId(event.target.value)} disabled={voicesLoading}>{voices.map(voice => <option key={voice.voice_id} value={voice.voice_id}>{voice.name}{voice.labels?.accent ? " - " + voice.labels.accent : voice.accent_verified === false ? " - acento sin comprobar" : ""}{voice.labels?.gender === 'female' ? ' · Voz femenina' : ''}</option>)}</select><small>Elige una voz para la narración.</small></label>}{voiceLoadError && <p className="audio-setup-note" role="alert">No se pudo cargar la lista de voces. Se usara la voz predeterminada si esta configurada.</p>}</div>}
          {draftId && /\.pdf$/i.test(book) && <form className="page-range" onSubmit={event => { event.preventDefault(); void applyPageRange() }}>
            <label htmlFor="page-range">Páginas que se narran <small>(opcional, como las numera el visor de PDF)</small>
              <input id="page-range" inputMode="numeric" placeholder="Todas · por ejemplo 20-114" value={pageRangeDraft}
                onChange={event => setPageRangeDraft(event.target.value)} onBlur={() => void applyPageRange()} disabled={busy || structureLoading}/>
            </label>
            <button type="submit" className="helper-secondary" disabled={busy || structureLoading || pageRangeDraft.trim() === pageRange}>Aplicar</button>
          </form>}
          {draftId && <section className="narration-outline" aria-labelledby="outline-title"><div><h3 id="outline-title">Contenido de la narración</h3><p>Revisa qué partes se incluirán. El índice y la sinopsis se excluyen inicialmente; los prólogos y epílogos se conservan.</p></div>{structureLoading ? <p role="status">Analizando capítulos y secciones…</p> : <ul>{narrationSections.map(section => { const included = selectedNarrationIds.includes(section.id); return <li key={section.id} className={section.needs_review ? 'needs-review' : ''}><label><input type="checkbox" checked={included} onChange={event => setSelectedNarrationIds(current => event.target.checked ? [...current, section.id] : current.filter(id => id !== section.id))}/><span><strong>{section.title}</strong><small>{section.category === 'toc' ? 'Índice' : section.category === 'synopsis' ? 'Sinopsis' : section.category === 'back' ? 'Material complementario' : section.category === 'intro' ? 'Introducción narrativa' : 'Contenido del libro'} · {section.paragraph_count} párrafos · {section.needs_review ? 'Revisar' : included ? 'Incluido' : 'Excluido'}</small></span></label></li>})}</ul>}</section>}
          {draftId && <div className="upload-actions">
            <button className="choose generate-preview" type="button" onClick={() => void generatePreview()} disabled={busy || structureLoading || !selectedNarrationIds.length || !canGenerateAudio}>{busy ? <span className="spinner" aria-hidden="true"/> : <Volume2 size={19} aria-hidden="true"/>}{busy ? "Generando la vista previa..." : "Generar vista previa"}</button>
            {outline && <button className="helper-secondary view-structure" type="button" onClick={() => switchWorkspaceTab('review')} disabled={structureLoading}>Ver la estructura del audiolibro (sin generar muestra)</button>}
          </div>}
          {message && <p className="upload-status" role="status" aria-live="polite">{publicCopy(message)}</p>}
        </section>
      </section>
      <section id="panel-review" className="workspace-panel review-panel" role="tabpanel" aria-labelledby="tab-review" hidden={workspaceTab !== 'review'}>
        <AudiobookEditor model={editorModel} loading={structureLoading}
          error={editorModel ? '' : outlineError} apiBase={API} jobId={productionJob?.job_id} jobState={productionJob?.state}
          active={workspaceTab === 'review'}
          onEditUnit={(unit, text) => changeBook((current, sections) => editUnit(current, sections, unit, text))}
          onInsertAfter={(unit, text) => changeBook((current, sections) => insertAfter(current, sections, unit, text))}
          onDeleteUnit={unit => changeBook((current, sections) => deleteUnit(current, sections, unit))}
          onDeleteSection={index => changeBook((current, sections) => deleteSection(current, sections, index))}
          onAddSection={(index, title, text) => changeBook((current, sections) => addSection(current, sections, index, title, text))}
          focusRequest={editorFocus}
          saveStatus={saveStatus} saveError={saveError} changes={changeCount(edits)} editBusy={editBusy}
          onUndo={undoChange} canUndo={undoStack.length > 0}
          editingProduced={editingProduced}
          onEditText={productionJob && !['queued', 'running'].includes(productionJob.state) && outline ? () => setEditingProduced(true) : undefined}
          onBackToAudio={() => setEditingProduced(false)}
          canProduce={Boolean(draftId) && !busy && !packageBusy && !directDownloadBusy && selectedNarrationIds.length > 0 && canGenerateAudio}
          producing={packageBusy && !productionJob} onProduce={() => void startReviewedProduction()}
          progress={productionJob ? { done: processedSentences, total: productionJob.total_sentences } : undefined}
          renderInspector={renderInspector} idlePanel={idlePanel} planillaPanel={planillaPanel}
          showPlanilla={showPlanilla} onShowPlanilla={() => setShowPlanilla(true)} onHidePlanilla={() => setShowPlanilla(false)}
          // Before production the panel on the right explains the step; the
          // app's status line is about the job, its decisions and the download.
          message={editingProduced
            ? 'Estás editando el texto. El audiolibro producido se conserva hasta que pulses «Producir con los cambios»: solo se narran las frases nuevas o corregidas.'
            : productionJob && message ? publicCopy(message) : ''}
          statusExtra={productionJob?.release ? <span>Calidad: {productionJob.release.label}</span> : null}/>
      </section>
      <section id="panel-export" className="workspace-panel export-panel" role="tabpanel" aria-labelledby="tab-export" hidden={workspaceTab !== 'export'}>
        <div className="xp">
          <ExportHeader title={bookMetadata.title || outline?.title || book || 'Audiolibro'} tone={exportTone} status={exportStatus}
            downloadLabel={reviewMode && zipReady && unvalidated ? 'Descargar ZIP (sin validar)' : 'Descargar ZIP'}
            downloadHint={zipReady ? 'Descarga el ZIP ya generado con el texto actual.'
              : 'Genera el ZIP con el texto actual y lo descarga al terminar.'}
            regenerateHint="Vuelve a crear el ZIP con el texto actual: solo se genera lo que ha cambiado y el resto se reutiliza."
            canDownload={zipReady || canBuild} canRegenerate={canBuild}
            busy={generating || (reviewMode && jobRunning) || (restoring && !productionJob)}
            downloading={zipAction === 'download'} regenerating={zipAction === 'regenerate'}
            onDownload={() => void downloadZip()} onRegenerate={() => void regenerateZip()}
            notice={downloadNotice ? { text: publicCopy(downloadNotice.text), tone: downloadNotice.tone ?? 'info', href: downloadNotice.href } : null}/>
          <div className="xp-body">
            <div className="xp-main">
              <ExportSection id="xp-build-title" title={generating || (reviewMode && jobRunning) ? 'Generación en curso' : 'Generación del ZIP'}
                aside={buildSeconds && !generating && !(reviewMode && jobRunning) ? <small>{formatSeconds(buildSeconds)} en total</small> : undefined}>
                {changesSinceZip && <p className="xp-warn">Desde el último ZIP: {changesSinceZip}.</p>}
                {buildView
                  ? <BuildStages stages={buildView} label="Etapas de la generación del ZIP"/>
                  : <p className="xp-empty">Al pulsar «Descargar ZIP» verás aquí cada etapa con lo que de verdad ocurre: qué se reutiliza, qué se genera y cuánto tarda.</p>}
                {buildView && reviewMode && productionIsStale && <p className="xp-muted">Estas etapas son de la generación anterior a tus cambios.</p>}
                {reviewMode && productionJob && (productionJob.voice_name || productionJob.voice_id) && <p className="xp-muted">
                  Voz de este trabajo: <strong>{productionJob.voice_name || productionJob.voice_id}</strong>{productionJob.voice_language ? ` (${productionJob.voice_language})` : ''}</p>}
                {reviewMode && productionJob?.voice_id && voiceId && productionJob.voice_id !== voiceId
                  && <p className="xp-warn">Has seleccionado otra voz después de iniciar este trabajo. Este ZIP y su informe son de la voz anterior; «Regenerar ZIP» lo crea con la nueva.</p>}
              </ExportSection>
              {reviewMode && awaitingDecision.length > 0 && !productionIsStale && <ExportSection id="xp-review-title"
                title={`Párrafos que esperan tu decisión (${awaitingDecision.length})`}>
                <p className="xp-muted">Se escuchan y se deciden en el paso 2: aceptar la lectura, narrarla de nuevo o corregir su texto. El ZIP se crea en cuanto no quede ninguno.</p>
                <ul className="xp-list">{awaitingDecision.slice(0, 8).map(sentence => <li key={sentence.sentence_id}>
                  <button type="button" className="xp-link" onClick={() => openUnitInEditor(sentence.sentence_id)}>
                    {sentence.chapter_title || `Capítulo ${sentence.chapter_index + 1}`} · párrafo {sentence.sequence + 1}</button>
                  <small>{sentence.state === 'failed' ? 'Fallo técnico: hay que narrarlo de nuevo.'
                    : publicCopy(sentence.diagnosis?.recommendation || sentence.last_error || sentence.text).slice(0, 180)}</small>
                </li>)}</ul>
                {awaitingDecision.length > 8 && <p className="xp-muted">Y {awaitingDecision.length - 8} más. En el paso 2, «Problema siguiente» (Alt+N) los recorre todos.</p>}
              </ExportSection>}
            </div>
            <div className="xp-side">
              <ExportSection id="xp-zip-title" title="Contenido del ZIP">
                {zipReady && zipPackage
                  ? <Facts rows={[
                    ['Archivos de audio', String(zipPackage.sections ?? zipPackage.chapter_files ?? '—')],
                    ['Duración', formatTotalTime(zipPackage.total_time) || '—'],
                    ['Tamaño', formatBytes(zipPackage.bytes) || '—'],
                    ['ZIP comprobado', <span key="audit" className={zipPackage.audit === 'passed' ? 'is-ok' : zipPackage.audit === 'failed' ? 'is-bad' : ''}
                      title={zipPackage.audit_detail}>{zipPackage.audit === 'passed' ? 'Coherente' : zipPackage.audit === 'failed' ? 'Con problemas' : 'Sin comprobar'}</span>],
                    ['Calidad', <span key="quality" className={!reviewMode ? 'is-warn' : unvalidated ? 'is-warn' : 'is-ok'}>{!reviewMode ? 'Sin verificar' : release?.label ?? 'Revisada'}</span>],
                    ...(reviewMode && productionJob ? [['Voz', <>{productionJob.voice_name || productionJob.voice_id || '—'}{productionJob.voice_language
                      ? <> <span className="xp-nowrap">({productionJob.voice_language})</span></> : null}</>] as [string, ReactNode]] : []),
                  ]}/>
                  : <p className="xp-empty">{zipReady ? 'El ZIP está listo.' : 'Todavía no hay un ZIP del texto actual.'}</p>}
                <p className="xp-muted">DAISY 3 (Z39.86-2005): un MP3 por archivo y el audiolibro completo, el texto sincronizado frase a frase y la navegación por capítulos y páginas. Tras crearlo, el ZIP se lee de nuevo y se comprueba entero.</p>
                {zipReady && zipPackage?.audit === 'failed' && zipPackage.audit_detail && <p className="xp-warn">{zipPackage.audit_detail}</p>}
              </ExportSection>
              {reviewMode && productionJob?.release && <ExportSection id="xp-quality-title" title="Calidad">
                <ReleasePanel release={productionJob.release} reviewerName={reviewerName} outdated={productionIsStale}
                  onReviewerName={setReviewerName} confirmed={releaseConfirmed} onConfirmed={setReleaseConfirmed}
                  onAcknowledge={issueId => void releaseAction('acknowledge', { issue_id: issueId, reviewer: reviewerName })}
                  onRelease={() => void releaseAction('release', { reviewer: reviewerName, confirm: releaseConfirmed })}/>
                {(productionJob.fault_locations?.length ?? 0) > 0 && <details className="xp-details">
                  <summary>Dónde se oyen las diferencias <small>{productionJob.fault_locations?.length}</small></summary>
                  <FaultLocations jobId={productionJob.job_id} locations={productionJob.fault_locations ?? []}/>
                </details>}
              </ExportSection>}
              {!reviewMode && <ExportSection id="xp-quality-title" title="Calidad">
                <p className="xp-muted">Sin verificar: sin la revisión de calidad, la narración no se transcribe ni se compara con el texto, y el ZIP lo dice en su quality-status.json. Para una entrega formal, actívala en «Opciones de entrega».</p>
              </ExportSection>}
              <details className="xp-details">
                <summary>Datos del libro<small>{[bookMetadata.title, bookMetadata.author].filter(Boolean).join(' · ')}</small></summary>
                <div className="xp-form">
                  <p className="xp-muted">Se guardan en los metadatos del paquete. Si los cambias, pulsa después «Regenerar ZIP».</p>
                  <label htmlFor="metadata-title">Título<input id="metadata-title" maxLength={500} value={bookMetadata.title} onChange={event => setBookMetadata(current => ({ ...current, title: event.target.value }))}/></label>
                  <label htmlFor="metadata-author">Autoría<input id="metadata-author" maxLength={500} value={bookMetadata.author} onChange={event => setBookMetadata(current => ({ ...current, author: event.target.value }))}/></label>
                  <div className="metadata-row">
                    <label htmlFor="metadata-edition">Edición<input id="metadata-edition" maxLength={500} value={bookMetadata.edition} onChange={event => setBookMetadata(current => ({ ...current, edition: event.target.value }))}/></label>
                    <label htmlFor="metadata-year">Año de publicación<input id="metadata-year" maxLength={500} value={bookMetadata.publication_year} onChange={event => setBookMetadata(current => ({ ...current, publication_year: event.target.value }))}/></label>
                  </div>
                  <label htmlFor="metadata-publisher">Editorial<input id="metadata-publisher" maxLength={500} value={bookMetadata.publisher} onChange={event => setBookMetadata(current => ({ ...current, publisher: event.target.value }))}/></label>
                </div>
              </details>
              <details className="xp-details">
                <summary>Opciones de entrega<small>{reviewMode ? 'con revisión de calidad' : 'sin verificar'}</small></summary>
                <div className="xp-form">
                  <label className="xp-check"><input type="checkbox" checked={reviewMode} disabled={generating}
                    onChange={event => chooseReviewMode(event.target.checked)}/>
                    <span>Revisión de calidad automática (recomendada)
                      <small>Cada párrafo se transcribe y se compara con el texto; las dudas se deciden en el paso 2 y el ZIP lleva el informe de calidad. Sin ella, el ZIP se crea directamente y queda marcado como sin verificar. Las dos formas reutilizan el mismo audio: cambiar de una a otra no vuelve a pagar lo ya narrado.</small></span></label>
                  {((bookChapters.length === 1 && totalParagraphCount > 1) || hasManualSections) && <label className="xp-check">
                    <input type="checkbox" checked={hasManualSections} disabled={generating}
                      onChange={event => setHasManualSections(event.target.checked)}/>
                    <span>Dividir el libro en secciones de navegación
                      <small>El libro no tiene capítulos detectables: se crean secciones por longitud, en límites de párrafo, solo para orientarse; no son capítulos del original.</small></span></label>}
                  <p className="xp-muted">Se narra con la voz elegida en el paso 1{selectedVoice ? `: ${selectedVoice.name}` : ''}.</p>
                </div>
              </details>
            </div>
          </div>
        </div>
      </section>
    </section>
    <footer className="editor-footer"><span>Creado con cuidado para cada lector.</span><span>Accesible desde el principio · Siempre a tu alcance</span></footer>
  </main>
}


// What the release gate decided, dimension by dimension, and the two decisions
// only a person can take: confirming an extraction warning and publishing.
export function ReleasePanel({ release, reviewerName, onReviewerName, confirmed, onConfirmed, onAcknowledge, onRelease, outdated = false }: {
  release: ReleaseStatus
  reviewerName: string
  onReviewerName: (value: string) => void
  confirmed: boolean
  onConfirmed: (value: boolean) => void
  onAcknowledge: (issueId: string) => void
  onRelease: () => void
  // The report of a ZIP made before the text changed: shown, never decided on.
  outdated?: boolean
}) {
  const decide = !outdated
  const dimensions = release.dimensions ?? {}
  const warnings = dimensions.source?.open_warnings ?? []
  const listen = dimensions.human?.listening_recommended?.length ?? 0
  const rows: [string, string | undefined, string][] = [
    ['Generación técnica', dimensions.technical?.status,
      `${dimensions.technical?.verified ?? 0} de ${dimensions.technical?.segments ?? 0} párrafos verificados, ${dimensions.technical?.quality_retakes ?? 0} retomas`],
    ['Texto de origen completo', dimensions.source?.status,
      `cobertura ${((dimensions.source?.coverage?.ratio ?? 0) * 100).toFixed(1)} %, ${warnings.length} avisos abiertos`],
    ['Contenido (reconocimiento automático)', dimensions.content?.status,
      dimensions.content?.wer == null ? 'sin medir' : `diferencias con el reconocedor: ${(dimensions.content.wer * 100).toFixed(2)} % de ${dimensions.content.reference_words ?? 0} palabras`],
    ['Integridad del audio', dimensions.audio?.status, `${dimensions.audio?.suspicious ?? 0} párrafos sospechosos`],
    ['Paquete DAISY', dimensions.package?.status,
      `estructura ${dimensionStatus(dimensions.package?.structural)}, DTD ${dimensionStatus(dimensions.package?.dtd)}, cadena completa ${dimensionStatus(dimensions.package?.completeness)}`],
    ['Revisión humana', undefined, `${dimensions.human?.decisions ?? 0} decisiones, ${listen} párrafos recomendados para escuchar`],
  ]
  return <section className={`release-panel ${RELEASE_STATE_CLASS[release.state] ?? 'release-progress'}`} aria-labelledby="release-title">
    <h4 id="release-title">Estado de calidad{outdated ? ' del ZIP anterior' : ''}: {release.label}</h4>
    {outdated && <p className="xp-muted release-outdated">Es el informe del ZIP anterior a tus cambios. Confirmar avisos y publicar se hace sobre el ZIP del texto actual: créalo con «Regenerar ZIP».</p>}
    {release.released && <p role="status">Publicado por {release.released.by} el {new Date(release.released.at).toLocaleString('es-ES')}.</p>}
    {(release.blockers.length > 0 || release.reviews.length > 0) && <ul className="release-reasons">
      {release.blockers.map(reason => <li key={reason} className="is-blocker">{reason}</li>)}
      {release.reviews.map(reason => <li key={reason}>{reason}</li>)}
    </ul>}
    <table className="release-dimensions"><tbody>
      {rows.map(([label, status, detail]) => <tr key={label}><th scope="row">{label}</th><td>{status ? dimensionStatus(status) : '—'}</td><td>{detail}</td></tr>)}
    </tbody></table>
    {decide && (warnings.length > 0 || release.state === 'PASSED') && <label className="release-reviewer">Tu nombre (queda registrado con la decisión)
      <input value={reviewerName} onChange={event => onReviewerName(event.target.value)} autoComplete="name"/></label>}
    {warnings.length > 0 && <ul className="release-warnings">{warnings.map(warning =>
      <li key={warning.id}><span>{warning.message}{warning.text ? ` «${warning.text}»` : ''}</span>
        {decide && <button type="button" disabled={!reviewerName.trim()} onClick={() => onAcknowledge(warning.id)}>He comprobado el original</button>}</li>)}
    </ul>}
    {decide && release.state === 'PASSED' && <div className="release-action">
      <label><input type="checkbox" checked={confirmed} onChange={event => onConfirmed(event.target.checked)}/>
        He leído lo que estas comprobaciones no demuestran{listen ? ` y he escuchado los ${listen} párrafos recomendados` : ''}.</label>
      <button type="button" className="package-download" disabled={!confirmed || !reviewerName.trim()} onClick={onRelease}>Publicar el audiolibro</button>
    </div>}
    <details className="release-uncertainty"><summary>Lo que estas comprobaciones no demuestran</summary>
      <ul>{release.uncertainty.map(note => <li key={note}>{note}</li>)}</ul></details>
  </section>
}


type PronunciationEditorProps = {
  sentence: ProductionSentence
  open: boolean
  word: string
  analysis?: PronunciationAnalysis
  custom: string
  busy: string
  saved: Record<string, string>
  onAnalyse: (word: string) => void
  onHear: (respelling: string) => Promise<string | undefined>
  onSave: (respelling: string) => void
  onCustom: (value: string) => void
}

// A word is read wrong because of how it is spelled, so the repair is a
// different spelling: how Spanish reads the word now, candidates to hear in
// the sentence, and the reviewer's own. Each copy plays in its own player.
export function PronunciationEditor({ sentence, open, word: activeWord, analysis, custom, busy, saved, onAnalyse, onHear, onSave, onCustom }: PronunciationEditorProps) {
  const player = useRef<HTMLAudioElement>(null)
  async function hear(respelling: string) {
    const url = await onHear(respelling)
    if (url && player.current) {
      player.current.src = url
      void player.current.play().catch(() => undefined)
    }
  }
  return <details className="pron-editor" open={open}>
    <summary>Corregir la pronunciación de una palabra</summary>
    <p className="pron-help">Pulsa la palabra que suena mal. Verás cómo la lee el español,
       podrás escuchar otras formas de escribirla y guardar la que suene bien.</p>
    <div className="pron-words">
      {Array.from(new Set(sentence.text.split(/\s+/)
        .map(part => part.replace(/[^\p{L}\p{N}'-]/gu, ''))
        .filter(part => part.length > 1))).map(word =>
        <button type="button" key={word}
          className={open && activeWord === word ? 'pron-word is-active' : 'pron-word'}
          onClick={() => onAnalyse(word)}>
          {word}{(saved[word] || (activeWord === word && analysis?.saved_respelling)) ? ' ✓' : ''}
        </button>)}
    </div>
    {open && busy === 'analyse' && <p role="status">Analizando «{activeWord}»…</p>}
    {open && analysis && <div className="pron-result">
      <dl className="pron-facts">
        <div><dt>Sílabas</dt><dd>{analysis.syllables.map((part, index) =>
          <span key={index} className={index === analysis.stressed ? 'pron-stress' : undefined}>{part}</span>)
          .reduce<ReactNode[]>((all, part, index) => index ? [...all, '-', part] : [part], [])}</dd></div>
        <div><dt>Suena como</dt><dd className="pron-ipa">{analysis.ipa || '—'}</dd></div>
        <div><dt>Escritura</dt><dd>{analysis.looks_spanish
          ? 'El español puede leerla tal cual'
          : 'Tiene letras que el español no usa'}</dd></div>
      </dl>
      {analysis.saved_respelling && <p className="pron-saved">Guardada ahora como
        <strong> {analysis.saved_respelling}</strong></p>}
      <ul className="pron-options">
        {analysis.suggestions.map(option =>
          <li key={option.respelling}>
            <div><strong>{option.label}:</strong> <span className="pron-try">{option.respelling}</span>
              <small>{option.reason}</small></div>
            <div className="pron-actions">
              <button type="button" disabled={Boolean(busy)} onClick={() => void hear(option.respelling)}>
                {busy === `hear:${option.respelling}` ? 'Generando…' : 'Escuchar'}</button>
              <button type="button" disabled={Boolean(busy)} onClick={() => onSave(option.respelling)}>
                {busy === `save:${option.respelling}` ? 'Guardando…' : 'Usar esta'}</button>
            </div>
          </li>)}
        <li>
          <label className="pron-custom">Escríbela como debe sonar
            <input type="text" value={custom} maxLength={200} placeholder="por ejemplo: guáshinton"
              onChange={event => onCustom(event.target.value)}/>
          </label>
          <div className="pron-actions">
            <button type="button" disabled={Boolean(busy) || !custom.trim()} onClick={() => void hear(custom)}>
              {busy === `hear:${custom}` ? 'Generando…' : 'Escuchar'}</button>
            <button type="button" disabled={Boolean(busy) || !custom.trim()} onClick={() => onSave(custom)}>
              {busy === `save:${custom}` ? 'Guardando…' : 'Usar esta'}</button>
          </div>
        </li>
      </ul>
      <audio ref={player} controls className="pron-player" aria-label="Muestra de pronunciación"/>
    </div>}
  </details>
}


const DECISION_LABEL: Record<string, string> = {
  accept_flagged: 'Aceptada; escúchala', accept: 'Aceptada', human_review: 'Requiere tu decisión',
  review_source: 'Revisa el texto original', review_normalization: 'Revisa la lectura normalizada', retake: 'Se volvió a narrar',
}

// Every difference the recognizers found, placed in the finished audiobook:
// the time in the complete book and in the chapter file, so a person can go
// straight to it in any player, and a button to hear it here.
export function FaultLocations({ jobId, locations }: { jobId: string; locations: FaultLocation[] }) {
  const [current, setCurrent] = useState<FaultLocation>()
  const playerId = `fault-player-${jobId}`
  function listen(location: FaultLocation) {
    setCurrent(location)
    // The paragraph's own audio, started just before the difference.
    window.setTimeout(() => playFrom(playerId, location.paragraph_time), 0)
  }
  return <section className="fault-locations" aria-labelledby="fault-locations-title">
    <h4 id="fault-locations-title">Dónde se oyen las diferencias ({locations.length})</h4>
    <p>El momento exacto en el audiolibro completo y en el archivo de su capítulo. Abre el MP3 en cualquier
       reproductor y ve a esa hora, o escúchala aquí.</p>
    <div className="fault-table"><table>
      <thead><tr><th>Libro completo</th><th>Capítulo</th><th>Qué se oye</th><th>Estado</th><th><span className="visually-hidden">Escuchar</span></th></tr></thead>
      <tbody>{locations.map((location, index) => <tr key={`${location.sentence_id}-${index}`}
        className={current === location ? 'is-current' : undefined}>
        <td className="fault-clock">{location.book_clock ?? '—'}</td>
        <td>Cap. {location.chapter} · <span className="fault-clock">{location.chapter_clock}</span></td>
        <td>{location.kind === 'difference'
          ? <>«{location.expected || '…'}» → «{location.heard || '…'}»</>
          : <>«{location.expected}» alargada</>}<small>{location.reason}</small></td>
        <td>{DECISION_LABEL[location.decision] ?? location.paragraph_state}</td>
        <td><button type="button" className="qa-seek" onClick={() => listen(location)}>Escuchar</button></td>
      </tr>)}</tbody>
    </table></div>
    {current && <audio id={playerId} key={current.sentence_id} controls preload="auto"
      src={`${API}/api/jobs/${encodeURIComponent(jobId)}/sentences/${encodeURIComponent(current.sentence_id)}/audio`}
      aria-label={`Párrafo ${current.paragraph} del capítulo ${current.chapter}`}/>}
  </section>
}
