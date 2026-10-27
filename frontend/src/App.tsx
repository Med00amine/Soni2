import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { BookOpen, Upload, Download, Volume2, CheckCircle2 } from 'lucide-react'
import './production-status.css'
// Loaded after, so the modern tokens win over the older hardcoded palette.
import './export-modern.css'
import AudiobookEditor, { KIND_LABEL, formatClock, type EditorModel, type EditorSection, type EditorUnit } from './AudiobookEditor'

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
type UnitProvenance = { role?: string; page_breaks?: string[]; note?: number | null; kind?: string; level?: number; source?: { chapter: number; paragraph: number } | null; page?: string }
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
type OutlineUnit = { role: string; text: string; page_breaks: string[]; note?: number | null; source?: { chapter: number; paragraph: number } | null }
type OutlineSection = { index: number; key: string; kind: string; level: number; title: string; heading_spoken: boolean; page_start: string; page_end: string; units: OutlineUnit[] }
type EditorWarning = { code: string; message: string; section?: number | null }
type BookOutline = { once: boolean; title: string; author: string; edition: string; publication_year: string; publisher: string; chapters: BookChapter[]; sections: OutlineSection[]; warnings: EditorWarning[]; planilla?: ResolvedPlanilla | null }
type StructureUnit = { sentence_id: string; role: string; text: string; state: string; page_breaks: string[]; note?: number | null; source?: { chapter: number; paragraph: number } | null; duration?: number | null; clip?: [number, number] | null; issues: number; has_audio: boolean; updated_at?: string; editable: boolean }
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
type ProductionJob = { fault_locations?: FaultLocation[]; job_id: string; state: string; source_name: string; total_sentences: number; progress: Record<string, number>; sentences: ProductionSentence[]; error?: string; voice_id?: string; voice_name?: string; voice_language?: string; release?: ReleaseStatus }
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
type PackageTaskStatus = { task_id: string; state: string; total: number; done: number; reused: number; detail?: string }
type PackageProgress = Pick<PackageTaskStatus, 'total' | 'done' | 'reused'>

const PACKAGE_TASK_KEY = 'soni2-active-package-task'

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

// 1:02:03,4 in a book, 2:03,4 under an hour: the clock a person reads in a player.
function clockLabel(seconds: number) {
  const tenths = Math.max(0, Math.round(seconds * 10))
  const hours = Math.floor(tenths / 36000)
  const minutes = Math.floor((tenths % 36000) / 600)
  const rest = tenths % 600
  const secondsText = `${String(Math.floor(rest / 10)).padStart(2, '0')},${rest % 10}`
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secondsText}` : `${minutes}:${secondsText}`
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

function isOutline(value: unknown): value is BookOutline {
  const outline = value as BookOutline | undefined
  return Boolean(outline && typeof outline === 'object' && Array.isArray(outline.sections) && Array.isArray(outline.chapters)
    && outline.sections.every(section => section && Array.isArray(section.units)))
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
    units: section.units.map((unit, position) => ({
      key: `${section.key}:${position}`, role: unit.role, text: unit.text, pageBreaks: unit.page_breaks ?? [],
      note: unit.note, source: unit.source ?? null, editable: Boolean(unit.source),
    })),
  }))
  return { mode: 'outline', once: outline.once, sections, warnings: outline.warnings ?? [], pages: countPages(sections) }
}

// Without the server's outline the chapters the preview returned still give a
// usable, if flatter, view of the book.
function modelFromChapters(chapters: BookChapter[]): EditorModel {
  const sections: EditorSection[] = chapters.map((chapter, index) => ({
    index, chapterIndex: index, fileNumber: index + 1, key: `chapter-${index + 1}`, kind: 'chapter', level: 1,
    title: chapter.title || `Sección ${index + 1}`, headingSpoken: false, pageStart: '', pageEnd: '', status: 'planned',
    units: chapter.paragraphs.map((text, paragraph) => ({
      key: `chapter-${index + 1}:${paragraph}`, role: 'text', text, pageBreaks: [],
      source: { chapter: index, paragraph }, editable: true,
    })),
  }))
  return { mode: 'outline', once: false, sections, warnings: [], pages: 0 }
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
  const [bookChapters, setBookChapters] = useState<BookChapter[]>([])
  const [sourceBookChapters, setSourceBookChapters] = useState<BookChapter[]>([])
  const [originalBookChapters, setOriginalBookChapters] = useState<BookChapter[]>([])
  const [hasManualSections, setHasManualSections] = useState(false)
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('upload')
  const [book, setBook] = useState('')
  const [bookMetadata, setBookMetadata] = useState<BookMetadata>({ title: '', author: '', edition: '', publication_year: '', publisher: '' })
  const [selectedFile, setSelectedFile] = useState<File>()
  // PDF pages to narrate, as a PDF viewer numbers them ("20-114"); blank = all.
  const [pageRange, setPageRange] = useState('')
  const [pageRangeDraft, setPageRangeDraft] = useState('')
  const [narrationSections, setNarrationSections] = useState<NarrationSection[]>([])
  const [selectedNarrationIds, setSelectedNarrationIds] = useState<string[]>([])
  const [structureLoading, setStructureLoading] = useState(false)
  const [packageBusy, setPackageBusy] = useState(false)
  const [directDownloadBusy, setDirectDownloadBusy] = useState(false)
  const [directProgress, setDirectProgress] = useState<PackageProgress>()
  const [productionJob, setProductionJob] = useState<ProductionJob>()
  const [reviewingSentence, setReviewingSentence] = useState('')
  // Which export the reader means to use; it decides the primary action.
  const [exportMode, setExportMode] = useState<'direct' | 'reviewed'>('reviewed')
  // Who signs a release or confirms a warning: recorded with the decision.
  const [reviewerName, setReviewerName] = useState('')
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
  const [outlineLoading, setOutlineLoading] = useState(false)
  const [outlineError, setOutlineError] = useState('')
  const [jobStructure, setJobStructure] = useState<JobStructure>()
  const outlineVersion = useRef(0)
  const outlineChaptersKey = useRef('')
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

  // Every visit starts with an empty workspace. A production run left behind by
  // a closed or reloaded page is stopped rather than restored, so it neither
  // reappears in the Export step nor keeps narrating (and spending) unseen.
  useEffect(() => {
    // The same goes for a direct download the previous visit was waiting for.
    const taskId = window.localStorage.getItem(PACKAGE_TASK_KEY)
    window.localStorage.removeItem(PACKAGE_TASK_KEY)
    if (taskId) void cancelPackageTask(taskId)
    const jobId = window.localStorage.getItem('soni2-active-production-job')
    if (!jobId) return
    window.localStorage.removeItem('soni2-active-production-job')
    // -1 matches no book selection, so a job that no longer exists fails quietly.
    void cancelProductionJob(jobId, -1)
  }, [])

  useEffect(() => {
    if (playerRef.current) playerRef.current.playbackRate = playbackRate
  }, [audio, playbackRate])

  // Keep the outline in step with the planilla, the bibliographic data and the
  // sections chosen; a pause in typing is waited for.
  const outlineInputs = JSON.stringify([onceEnabled, planillaValues, bookMetadata, hasManualSections, pageRange,
    selectedNarrationIds])
  useEffect(() => {
    if (!selectedFile || structureLoading || !selectedNarrationIds.length) return
    const timer = window.setTimeout(() => void refreshOutline(), outline ? 700 : 0)
    return () => window.clearTimeout(timer)
    // refreshOutline reads the state of this render, which is what changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outlineInputs, selectedFile, structureLoading])

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

  async function selectBook(file?: File, pages = pageRange) {
    if (!file) return
    const selectionVersion = ++bookSelectionVersion.current
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
    setOutline(undefined)
    setOutlineError('')
    outlineChaptersKey.current = ''
    // A planilla belongs to one book.
    setPlanillaValues({})
    setPlanillaWarnings([])
    setPlanillaStatus('')
    setShowPlanilla(false)
    setReviewingSentence('')
    setSentenceCorrections({})
    setRenarrated({})
    setLastRenarrated('')
    setPackageBusy(false)
    setDirectDownloadBusy(false)
    setDirectProgress(undefined)
    if (previousJobId) void cancelProductionJob(previousJobId, selectionVersion)
    setSelectedFile(file)
    setNarrationSections([]); setSelectedNarrationIds([]); setStructureLoading(true)
    setAudio(''); setTranscript(''); setBookChapters([]); setSourceBookChapters([]); setOriginalBookChapters([]); setHasManualSections(false); setWorkspaceTab('upload'); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setBookMetadata({ title: '', author: '', edition: '', publication_year: '', publisher: '' }); setBook(file.name); setMessage('Libro cargado. Elige una voz y pulsa «Generar vista previa».')
    const form = new FormData(); form.append('file', file); form.append('pages', pages)
    try {
      const response = await fetch(`${API}/api/books/structure`, { method: 'POST', body: form })
      const result = await response.json().catch(() => ({})) as { sections?: NarrationSection[]; selected_ids?: string[]; detail?: string }
      if (bookSelectionVersion.current !== selectionVersion) return
      if (!response.ok) throw new Error(result.detail || 'No se ha podido analizar la estructura del libro.')
      setNarrationSections(result.sections ?? []); setSelectedNarrationIds(result.selected_ids ?? [])
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
    const file = selectedFile
    if (!file || busy) return
    setAudio(''); setTranscript(''); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setBusy(true); setMessage('Estamos preparando una muestra de tu libro.')
    const form = new FormData(); form.append('file', file); form.append('voice_id', voiceId); form.append('narration_plan_json', JSON.stringify({ selected_ids: selectedNarrationIds })); form.append('pages', pageRange)
    try {
      const response = await fetch(`${API}/api/books/preview`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No hemos podido leer este documento. Prueba con otro archivo.')
      }
      const result: { audio_base64: string; audio_mime_type: string; transcript: string; alignment?: TimedPhrase[] | null; audio_error?: string | null; audio_notice?: string | null; diagnostic_id?: string | null; chapters?: BookChapter[]; title?: string; author?: string; edition?: string; publication_year?: string; publisher?: string } = await response.json()
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
      setBookChapters(result.chapters ?? [])
      setSourceBookChapters(result.chapters ?? [])
      setOriginalBookChapters(result.chapters ?? [])
      setBookMetadata({ title: result.title ?? '', author: result.author ?? '', edition: result.edition ?? '', publication_year: result.publication_year ?? '', publisher: result.publisher ?? '' })
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

  // The upload, the chosen sections, the edited paragraphs and the bibliographic
  // data, shared by the direct download and the reviewed production run.
  function buildBookForm() {
    const form = new FormData()
    form.append('file', selectedFile as File)
    form.append('pages', pageRange)
    form.append('voice_id', voiceId)
    // Only send metadata the user actually filled in; an empty title would
    // otherwise override the title detected in the document and be rejected.
    const metadata = Object.fromEntries(
      Object.entries(bookMetadata).filter(([, value]) => value.trim()),
    )
    form.append('metadata_json', JSON.stringify(metadata))
    form.append('narration_plan_json', JSON.stringify({ selected_ids: selectedNarrationIds }))
    form.append('corrections_json', JSON.stringify(paragraphCorrections()))
    form.append('create_navigation_sections', String(hasManualSections))
    form.append('planilla_json', planillaJson())
    return form
  }

  // The paragraphs the reader changed, by their position in the selected chapters.
  function paragraphCorrections(chapters = bookChapters, originals = originalBookChapters) {
    const corrections: Record<string, string> = {}
    const originalParagraphs = originals.flatMap((chapter, chapterIndex) => chapter.paragraphs.map((text, paragraphIndex) => ({ chapterIndex, paragraphIndex, text })))
    const currentParagraphs = chapters.flatMap(chapter => chapter.paragraphs)
    currentParagraphs.forEach((paragraph, flatIndex) => {
      const original = originalParagraphs[flatIndex]
      if (original && paragraph !== original.text) {
        corrections[`${original.chapterIndex}:${original.paragraphIndex}`] = paragraph
      }
    })
    return corrections
  }

  // An empty planilla still means "ONCE structure, with the model's rules".
  function planillaJson(values = planillaValues, enabled = onceEnabled) {
    return enabled ? JSON.stringify(values) : ''
  }

  // The structure step 2 shows before anything is narrated. Nothing is paid for.
  async function refreshOutline() {
    const file = selectedFile
    if (!file || !selectedNarrationIds.length) return
    const version = ++outlineVersion.current
    const selection = bookSelectionVersion.current
    const chaptersKey = `${file.name}|${file.size}|${pageRange}|${selectedNarrationIds.join(',')}`
    // Another selection of sections is another text: earlier corrections were
    // made to paragraphs that are no longer in the same place.
    const sameText = outlineChaptersKey.current === chaptersKey
    const form = new FormData()
    form.append('file', file)
    form.append('pages', pageRange)
    form.append('narration_plan_json', JSON.stringify({ selected_ids: selectedNarrationIds }))
    form.append('metadata_json', JSON.stringify(Object.fromEntries(Object.entries(bookMetadata).filter(([, value]) => value.trim()))))
    form.append('corrections_json', JSON.stringify(sameText && !hasManualSections ? paragraphCorrections() : {}))
    form.append('create_navigation_sections', String(hasManualSections && sameText))
    form.append('planilla_json', planillaJson())
    setOutlineLoading(true)
    try {
      const response = await fetch(`${API}/api/books/outline`, { method: 'POST', body: form })
      const body = await response.json().catch(() => ({})) as BookOutline & { detail?: string }
      if (version !== outlineVersion.current || selection !== bookSelectionVersion.current) return
      if (!response.ok) throw new Error(errorDetail(body.detail, 'No se ha podido preparar la estructura del audiolibro.'))
      if (!isOutline(body)) throw new Error('El servidor no ha devuelto la estructura del audiolibro.')
      setOutline(body)
      setOutlineError('')
      if (!sameText) {
        outlineChaptersKey.current = chaptersKey
        // The editable text starts from the book itself (a preview, if one is
        // made, starts it again from the same chapters).
        if (!transcript) {
          setBookChapters(body.chapters)
          setSourceBookChapters(body.chapters)
          setOriginalBookChapters(body.chapters)
          setHasManualSections(false)
        }
        setBookMetadata(current => ({
          title: current.title || body.title || '', author: current.author || body.author || '',
          edition: current.edition || body.edition || '', publication_year: current.publication_year || body.publication_year || '',
          publisher: current.publisher || body.publisher || '',
        }))
      }
    } catch (error) {
      if (version === outlineVersion.current) setOutlineError(error instanceof Error ? error.message : 'No se ha podido preparar la estructura.')
    } finally {
      if (version === outlineVersion.current) setOutlineLoading(false)
    }
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

  // Generate the audiobook and download it with no review queue. It runs as a
  // background task the page polls, so it can show real progress instead of
  // one long, silent request, and so choosing another book can stop it.
  async function downloadBookDirectly() {
    if (!selectedFile || directDownloadBusy || packageBusy) return
    const selectionVersion = bookSelectionVersion.current
    const filename = bookFilename(selectedFile.name)
    setDirectDownloadBusy(true)
    setDirectProgress(undefined)
    setMessage('Preparando el audiolibro completo…')
    let taskId: string | undefined
    try {
      const response = await fetch(`${API}/api/books/package-tasks`, { method: 'POST', body: buildBookForm() })
      const started = await response.json().catch(() => ({})) as Partial<PackageTaskStatus>
      if (!response.ok || !started.task_id) {
        throw new Error(started.detail || `No se ha podido generar el audiolibro (HTTP ${response.status}).`)
      }
      taskId = started.task_id
      activePackageTaskId.current = taskId
      window.localStorage.setItem(PACKAGE_TASK_KEY, taskId)
      const status = await watchPackageTask(taskId)
      if (activePackageTaskId.current !== taskId) return
      if (status.state !== 'completed') throw new Error(status.detail || 'No se ha podido generar el audiolibro.')
      setMessage('Audio listo. Descargando el ZIP…')
      // Streamed to disk by the browser rather than buffered in a blob: an
      // audiobook package is far too large to hold in the tab's memory, which
      // is why pressing the button used to appear to do nothing.
      await streamDownload(
        `${API}/api/books/package-tasks/${encodeURIComponent(taskId)}/download`, filename,
      )
      setMessage(status.reused
        ? `El audiolibro se ha descargado. ${status.reused} de ${status.total} párrafos ya estaban narrados y no se han vuelto a pagar.`
        : 'El audiolibro se ha descargado. Incluye el audio por capítulos y la estructura DAISY.')
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        setMessage(error instanceof Error ? publicCopy(error.message) : 'No se ha podido descargar el audiolibro.')
      }
    } finally {
      if (taskId && activePackageTaskId.current === taskId) {
        activePackageTaskId.current = undefined
        window.localStorage.removeItem(PACKAGE_TASK_KEY)
      }
      if (bookSelectionVersion.current === selectionVersion) {
        setDirectDownloadBusy(false)
        setDirectProgress(undefined)
      }
    }
  }

  async function watchPackageTask(taskId: string): Promise<PackageTaskStatus> {
    for (;;) {
      const response = await fetch(`${API}/api/books/package-tasks/${encodeURIComponent(taskId)}`, { cache: 'no-store' })
      const status = await response.json().catch(() => ({})) as PackageTaskStatus
      if (!response.ok) throw new Error(status.detail || 'No se pudo consultar el avance de la descarga.')
      if (activePackageTaskId.current !== taskId || status.state !== 'running') return status
      setDirectProgress({ total: status.total, done: status.done, reused: status.reused })
      setMessage(status.total
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

  async function startReviewedProduction() {
    const file = selectedFile
    if (!file || packageBusy) return
    const selectionVersion = bookSelectionVersion.current
    setPackageBusy(true)
    setMessage('Preparando el libro completo. Este proceso puede tardar varios minutos.')
    const form = buildBookForm()
    try {
      const response = await fetch(`${API}/api/books/jobs`, { method: 'POST', body: form })
      const created = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(created.detail || 'No hemos podido iniciar la producción del libro.')
      if (bookSelectionVersion.current !== selectionVersion) {
        void cancelProductionJob(created.job_id, selectionVersion)
        return
      }
      activeProductionJobId.current = created.job_id
      setProductionJob({ job_id: created.job_id, state: created.state, source_name: file.name,
        total_sentences: created.total_sentences, progress: {}, sentences: [] })
      window.localStorage.setItem('soni2-active-production-job', created.job_id)
      setMessage(created.reused_existing_job
        ? 'Este libro ya se está produciendo con esta voz, así que seguimos el trabajo en curso en lugar de empezar otro.'
        : 'Producción iniciada. Puedes seguir el avance aquí; las frases pendientes de revisión aparecerán abajo.')
      await watchProductionJob(created.job_id)
    } catch (error) {
      if (bookSelectionVersion.current === selectionVersion) {
        setMessage(error instanceof Error ? error.message : 'No se ha podido descargar el libro completo.')
      }
    } finally {
      if (bookSelectionVersion.current === selectionVersion) setPackageBusy(false)
    }
  }

  // ``quiet`` follows the job without replacing the message, for the short
  // catch-up after a review decision whose own result is already on screen.
  async function watchProductionJob(jobId: string, quiet = false) {
    for (let attempt = 0; attempt < 7200; attempt += 1) {
      if (activeProductionJobId.current !== jobId) return
      const response = await fetch(`${API}/api/jobs/${jobId}`, { cache: 'no-store' })
      const status = await response.json().catch(() => ({})) as ProductionJob & { detail?: string }
      if (!response.ok) throw new Error(status.detail || 'No se pudo consultar el estado de producción.')
      if (activeProductionJobId.current !== jobId) return
      setProductionJob(status)
      const counts = Object.entries(status.progress ?? {}).reduce((sum, [state, count]) =>
        sum + (['pending', 'generating'].includes(state) ? 0 : count), 0)
      if (!quiet) setMessage(`Producción: ${counts} de ${status.total_sentences} párrafos procesados. Estado: ${status.state}.`)
      if (status.state === 'completed') {
        setMessage('El audiolibro est\u00e1 listo. Pulsa Descarga ZIP DAISY para guardarlo.')
        return
      }
      if (['partially_completed', 'failed', 'cancelled'].includes(status.state)) {
        if (!quiet) setMessage(status.state === 'partially_completed'
          ? 'La producción necesita una revisión. Revisa los párrafos señalados abajo y decide si aceptarlos o regenerarlos.'
          : `La producción terminó con estado «${status.state}». ${status.error || ''}`)
        return
      }
      await new Promise(resolve => window.setTimeout(resolve, 1000))
    }
    throw new Error('La producción sigue activa. Puedes volver a consultar el estado más tarde.')
  }

  async function refreshProductionJob() {
    if (!productionJob) return
    try {
      const response = await fetch(`${API}/api/jobs/${productionJob.job_id}`, { cache: 'no-store' })
      const status = await response.json().catch(() => ({})) as ProductionJob & { detail?: string }
      if (!response.ok) throw new Error(status.detail || 'No se pudo consultar el trabajo.')
      setProductionJob(status)
      setMessage(`Estado del trabajo: ${status.state}.`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se pudo actualizar el trabajo.')
    }
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

  async function downloadDiagnosticArtifact(jobId: string, sourceName: string) {
    setMessage('Descargando el paquete de diagnóstico. No está validado y no debe publicarse.')
    try {
      await streamDownload(`${API}/api/jobs/${encodeURIComponent(jobId)}/artifacts?diagnostic=true`,
        bookFilename(sourceName, ' (SIN VALIDAR)'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se ha podido descargar el paquete.')
    }
  }

  async function downloadJobArtifact(jobId: string, sourceName: string) {
    // An audiobook package is hundreds of megabytes. Reading it into a blob
    // first filled the tab's memory and the download appeared to do nothing,
    // so the browser is handed the URL and streams it straight to disk.
    // A one-byte range request first, which costs nothing, still surfaces the
    // real reason when the package is not ready instead of navigating away.
    setMessage('Comprobando que el paquete esté listo…')
    try {
      await streamDownload(`${API}/api/jobs/${encodeURIComponent(jobId)}/artifacts`, bookFilename(sourceName))
      setMessage('La descarga del paquete DAISY ha comenzado. Puede tardar según el tamaño del libro.')
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'No se ha podido descargar el paquete DAISY.'
      setMessage(publicCopy(detail.includes('todavía no está disponible')
        ? `${detail} Resuelve las frases pendientes de revisión, o usa «Descargar audiolibro ahora» para generarlo sin revisión.`
        : detail))
    }
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

  // A new range is a different book to narrate, so it is read again from the start.
  function applyPageRange() {
    const value = pageRangeDraft.trim()
    if (value === pageRange || !selectedFile) return
    setPageRange(value)
    void selectBook(selectedFile, value)
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
  const reviewQueue = (productionJob?.sentences ?? [])
    .filter(sentence => ['needs_human_review', 'unresolved', 'failed'].includes(sentence.state) || renarrated[sentence.sentence_id])
  const pendingReview = reviewQueue.filter(sentence => sentence.state !== 'verified').length
  const inFlightSentences = ['pending', 'generating']
    .reduce((sum, state) => sum + (productionJob?.progress?.[state] ?? 0), 0)
  const totalParagraphCount = bookChapters.reduce((count, chapter) => count + chapter.paragraphs.length, 0)

  function createNavigationSections() {
    const sourceChapter = sourceBookChapters[0]
    if (!sourceChapter || sourceBookChapters.length !== 1 || sourceChapter.paragraphs.length < 2) return
    const paragraphs = sourceChapter.paragraphs
    const wordCount = paragraphs.join(' ').split(/\s+/).filter(Boolean).length
    const targetWords = Math.max(120, Math.min(1200, Math.ceil(wordCount / 5)))
    const sections: BookChapter[] = []
    let currentParagraphs: string[] = []
    let currentWords = 0
    for (let index = 0; index < paragraphs.length; index += 1) {
      const paragraph = paragraphs[index]
      currentParagraphs.push(paragraph)
      currentWords += paragraph.split(/\s+/).filter(Boolean).length
      const hasRoomForAnotherSection = sections.length < 4
      const hasRemainingParagraphs = index < paragraphs.length - 1
      if (currentWords >= targetWords && hasRoomForAnotherSection && hasRemainingParagraphs) {
        sections.push({ title: `Sección ${sections.length + 1}`, paragraphs: currentParagraphs })
        currentParagraphs = []
        currentWords = 0
      }
    }
    if (currentParagraphs.length) sections.push({ title: `Sección ${sections.length + 1}`, paragraphs: currentParagraphs })
    if (sections.length < 2) {
      setMessage('El libro tiene demasiado poco texto para crear varias secciones. Puedes conservar su estructura original.')
      return
    }
    setBookChapters(sections)
    setHasManualSections(true)
    setMessage(`He creado ${sections.length} secciones en límites de párrafo. Revisa los nombres antes de descargar el libro.`)
  }

  function restoreDetectedStructure() {
    setBookChapters(sourceBookChapters)
    setOriginalBookChapters(sourceBookChapters)
    setHasManualSections(false)
    setMessage('Se ha restaurado la estructura detectada en el archivo original.')
  }

  async function reviewPreview() {
    if (!audio || !transcript || reviewBusy) return
    setReviewBusy(true)
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
      setMessage(error instanceof Error ? error.message : 'No se pudo revisar la narración.')
    } finally { setReviewBusy(false) }
  }

  async function regenerateRetake() {
    if (!transcript.trim() || retakeBusy) return
    setRetakeBusy(true)
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
      setMessage(error instanceof Error ? error.message : 'No se pudo regenerar el fragmento.')
    } finally { setRetakeBusy(false) }
  }

  function pronunciationEditor(sentence: ProductionSentence) {
    return <PronunciationEditor sentence={sentence} open={pronSentence === sentence.sentence_id} word={pronWord}
      analysis={pronSentence === sentence.sentence_id ? pronAnalysis : undefined} custom={pronCustom} busy={pronBusy}
      saved={pronSaved} onAnalyse={word => void analysePronunciation(word, sentence.sentence_id)}
      onHear={respelling => hearRespelling(respelling, sentence.text)} onSave={respelling => void saveRespelling(respelling)}
      onCustom={setPronCustom}/>
  }

  const editParagraph = useCallback((chapter: number, paragraph: number, text: string) => {
    setBookChapters(current => current.map((item, index) => index !== chapter ? item
      : { ...item, paragraphs: item.paragraphs.map((value, part) => part === paragraph ? text : value) }))
  }, [])

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
    if (productionJob && (productionJob.sentences.length || jobStructure)) return modelFromJob(productionJob, jobStructure)
    if (outline) return modelFromOutline(outline)
    if (bookChapters.length) return modelFromChapters(bookChapters)
    return undefined
  }, [productionJob, jobStructure, outline, bookChapters])

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
    {audio && <section id="listen-section" className="player workflow-card" aria-labelledby="player-title"><div className="player-heading"><span className="player-icon" aria-hidden="true"><Volume2 size={21}/></span><div className="player-title"><h2 id="player-title">Vista previa de audio</h2><span>{bookMetadata.title || book} · Español</span></div></div><audio ref={playerRef} controls src={audio} onTimeUpdate={trackPlayback} onSeeked={trackPlayback} aria-label={`Escuchar la vista previa narrada de ${bookMetadata.title || book}`}/><div className="player-playback"><label htmlFor="playback-speed">Velocidad<select id="playback-speed" value={playbackRate} onChange={event => setPlaybackRate(Number(event.target.value))}><option value={0.75}>0,75×</option><option value={1}>1×</option><option value={1.25}>1,25×</option><option value={1.5}>1,5×</option><option value={1.75}>1,75×</option><option value={2}>2×</option></select></label></div><a className="download" href={audio} download={`${(bookMetadata.title || book).replace(/.[^.]+$/, '')}-vista-previa.mp3`}><Download size={17} aria-hidden="true"/>Descargar muestra MP3</a></section>}
    {transcript && <section className="transcript workflow-card audio-transcript" aria-labelledby="transcript-title"><h2 id="transcript-title">Texto de la muestra</h2><details><summary>Mostrar texto sincronizado</summary>{alignment.length > 0 ? <div className="timed-transcript" aria-live="off">{alignedParagraphs.map((paragraph, paragraphIndex) => <p key={`paragraph-${paragraphIndex}`}>{paragraph.map(phrase => { const index = alignment.indexOf(phrase); const active = index === currentPhrase; return <span key={`${phrase.text_start}-${index}`} className={active ? 'timed-phrase is-speaking' : 'timed-phrase'} aria-current={active ? 'true' : undefined}>{phrase.text} </span>})}</p>)}</div> : <><p className="sync-note">{'No se pudieron obtener tiempos para sincronizar esta muestra. El texto sigue disponible por parrafos.'}</p>{transcript.split(/\n{2,}/).map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>)}</>}</details>
      {capabilities?.audio_review_configured && <div className="review-controls"><button type="button" className="package-download" onClick={reviewPreview} disabled={reviewBusy || !audio}>{reviewBusy ? 'Revisando el audio…' : 'Revisar calidad del audio'}</button><small>Las diferencias son avisos para que una persona las revise.</small></div>}
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
    <header className="top editor-top"><a className="brand" href="/" aria-label="Inicio de Soni2"><img className="brand-logo" src="/soni2-logo.svg" alt="Soni2" width="200" height="42"/></a><span className="top-note">Una forma más tranquila de disfrutar los libros</span></header>
    <section id="main" className="hero workspace editor-workspace" aria-label="Espacio de trabajo de Soni2">
      <nav className="workspace-tabs" role="tablist" aria-label="Flujo de trabajo del audiolibro">
        <button type="button" role="tab" id="tab-upload" aria-controls="panel-upload" aria-selected={workspaceTab === 'upload'} onClick={() => switchWorkspaceTab('upload')}><span>1</span>Subir libro</button>
        <button type="button" role="tab" id="tab-review" aria-controls="panel-review" aria-selected={workspaceTab === 'review'} disabled={!transcript && !editorModel} onClick={() => switchWorkspaceTab('review')}><span>2</span>Escuchar y revisar</button>
        <button type="button" role="tab" id="tab-export" aria-controls="panel-export" aria-selected={workspaceTab === 'export'} disabled={!transcript && !editorModel} onClick={() => switchWorkspaceTab('export')}><span>3</span>Exportar</button>
      </nav>
      <section id="panel-upload" className="workspace-panel upload-panel" role="tabpanel" aria-labelledby="tab-upload" hidden={Boolean(selectedFile) && workspaceTab !== 'upload'}>
        <section id="upload-section" className="upload-card workflow-card upload-experience" aria-labelledby="upload-title">
          <h1 id="upload-title" className="visually-hidden">Subir libro</h1>
          <input ref={input} className="visually-hidden" type="file" accept=".pdf,.epub,.docx,.txt,.xml" aria-label="Elige un libro en formato PDF, EPUB, Word, texto o XML" onChange={e => { const file = e.target.files?.[0]; e.currentTarget.value = ''; selectBook(file) }}/>
          <div className={"upload-dropzone" + (dragActive ? " is-dragging" : "") + (selectedFile ? " has-file" : "")} onDragOver={event => { event.preventDefault(); if (!busy) setDragActive(true) }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragActive(false) }} onDrop={event => { event.preventDefault(); setDragActive(false); if (!busy) selectBook(event.dataTransfer.files[0]) }}>
            <span className="upload-drop-icon" aria-hidden="true"><Upload size={25}/></span>
            {selectedFile ? <><strong className="upload-file-name" title={book}>{book}</strong><span className="upload-drop-copy">Archivo preparado. Puedes elegir otro si lo necesitas.</span></> : <><strong>Arrastra tu libro aqui</strong><span className="upload-drop-copy">o selecciona un archivo desde tu dispositivo</span></>}
            <button className="choose upload-select" type="button" onClick={() => input.current?.click()} disabled={busy}>{busy ? <span className="spinner" aria-hidden="true"/> : <BookOpen size={18} aria-hidden="true"/>}{busy ? "Preparando..." : selectedFile ? "Cambiar archivo" : "Elegir archivo"}</button>
            <span className="upload-formats">PDF <i/> EPUB <i/> DOCX <i/> TXT <i/> XML</span>
          </div>
          {selectedFile && !canGenerateAudio && <p className="audio-setup-note" role="status">No hay una voz configurada para generar la vista previa. Revisa la configuracion del servicio de narracion.</p>}
          {capabilityError && <div className="audio-setup-note" role="alert">No se pudo conectar con el servicio. <button type="button" onClick={() => void refreshCapabilities()}>Reintentar</button></div>}
          <p className="privacy upload-privacy">Tu libro se trata con cuidado y privacidad.</p>
          {capabilities?.tts_configured && <p className="sync-note upload-voice-status"><CheckCircle2 size={16} aria-hidden="true"/> Narración lista para generar{capabilities.voice_name ? ` · voz ${capabilities.voice_name}` : ''}{capabilities.voice_language ? ` (${capabilities.voice_language})` : ''}</p>}
          {capabilities?.offline_mock_mode && <p className="audio-setup-note" role="status">Modo simulado activo: Soni2 genera audio de prueba sin coste y sin voz real. Configura un servicio de voz para producir el audiolibro definitivo.</p>}
          {capabilities?.tts_configured && !capabilities.audio_review_configured && <p className="audio-setup-note" role="status">No hay servicio de transcripción configurado, así que la revisión automática de calidad no se puede ejecutar y cada frase pasará a revisión humana.</p>}
          {selectedVoice && selectedVoice.accent_verified === false && <p className="audio-setup-note" role="status">No se ha podido comprobar el acento de la voz «{selectedVoice.name}». Escucha la vista previa para confirmar que es el que necesitas.{capabilities?.requires_peninsular_voice ? ' El servidor exige una voz de España y la rechazará hasta que se declare su acento.' : ''}</p>}
          {selectedVoice && selectedVoice.accent_verified !== false && !isSpanishFromSpain(selectedVoice) && <p className="audio-setup-note" role="alert">La voz «{selectedVoice.name}» es {selectedVoice.labels?.accent || 'de otra región'} y no español de España.{capabilities?.requires_peninsular_voice ? ' El servidor rechazará la producción con esta voz.' : ' Elige una voz es-ES si necesitas acento de España.'}</p>}
          {(capabilities?.voice_id_configurable || voiceLoadError) && <div className="voice-controls" role="group" aria-label="Configuracion de voz e idioma">{capabilities?.voice_id_configurable && <label htmlFor="voice-id">{'Voz de narraci\u00f3n'}<select id="voice-id" className="voice-id-input" value={voiceId || capabilities?.voice || voices[0]?.voice_id || ''} onChange={event => setVoiceId(event.target.value)} disabled={voicesLoading}>{voices.map(voice => <option key={voice.voice_id} value={voice.voice_id}>{voice.name}{voice.labels?.accent ? " - " + voice.labels.accent : voice.accent_verified === false ? " - acento sin comprobar" : ""}{voice.labels?.gender === 'female' ? ' · Voz femenina' : ''}</option>)}</select><small>Elige una voz para la narración.</small></label>}{voiceLoadError && <p className="audio-setup-note" role="alert">No se pudo cargar la lista de voces. Se usara la voz predeterminada si esta configurada.</p>}</div>}
          {selectedFile && /\.pdf$/i.test(selectedFile.name) && <form className="page-range" onSubmit={event => { event.preventDefault(); applyPageRange() }}>
            <label htmlFor="page-range">Páginas que se narran <small>(opcional, como las numera el visor de PDF)</small>
              <input id="page-range" inputMode="numeric" placeholder="Todas · por ejemplo 20-114" value={pageRangeDraft}
                onChange={event => setPageRangeDraft(event.target.value)} onBlur={applyPageRange} disabled={busy || structureLoading}/>
            </label>
            <button type="submit" className="helper-secondary" disabled={busy || structureLoading || pageRangeDraft.trim() === pageRange}>Aplicar</button>
          </form>}
          {selectedFile && <section className="narration-outline" aria-labelledby="outline-title"><div><h3 id="outline-title">Contenido de la narración</h3><p>Revisa qué partes se incluirán. El índice y la sinopsis se excluyen inicialmente; los prólogos y epílogos se conservan.</p></div>{structureLoading ? <p role="status">Analizando capítulos y secciones…</p> : <ul>{narrationSections.map(section => { const included = selectedNarrationIds.includes(section.id); return <li key={section.id} className={section.needs_review ? 'needs-review' : ''}><label><input type="checkbox" checked={included} onChange={event => setSelectedNarrationIds(current => event.target.checked ? [...current, section.id] : current.filter(id => id !== section.id))}/><span><strong>{section.title}</strong><small>{section.category === 'toc' ? 'Índice' : section.category === 'synopsis' ? 'Sinopsis' : section.category === 'back' ? 'Material complementario' : section.category === 'intro' ? 'Introducción narrativa' : 'Contenido del libro'} · {section.paragraph_count} párrafos · {section.needs_review ? 'Revisar' : included ? 'Incluido' : 'Excluido'}</small></span></label></li>})}</ul>}</section>}
          {selectedFile && <button className="choose generate-preview" type="button" onClick={() => void generatePreview()} disabled={busy || structureLoading || !selectedNarrationIds.length || !canGenerateAudio}>{busy ? <span className="spinner" aria-hidden="true"/> : <Volume2 size={19} aria-hidden="true"/>}{busy ? "Generando la vista previa..." : "Generar vista previa"}</button>}
          {selectedFile && outline && <button className="helper-secondary view-structure" type="button" onClick={() => switchWorkspaceTab('review')} disabled={structureLoading}>Ver la estructura del audiolibro (sin generar muestra)</button>}
          {message && <p className="upload-status" role="status" aria-live="polite">{publicCopy(message)}</p>}
        </section>
      </section>
      <section id="panel-review" className="workspace-panel review-panel" role="tabpanel" aria-labelledby="tab-review" hidden={workspaceTab !== 'review'}>
        <AudiobookEditor model={editorModel} loading={structureLoading || (outlineLoading && !outline && !bookChapters.length)}
          error={editorModel ? '' : outlineError} apiBase={API} jobId={productionJob?.job_id} jobState={productionJob?.state}
          active={workspaceTab === 'review'} bookChapters={bookChapters} onEditParagraph={editParagraph}
          onEditsDone={() => void refreshOutline()}
          canProduce={Boolean(selectedFile) && !busy && !packageBusy && !directDownloadBusy && selectedNarrationIds.length > 0 && canGenerateAudio}
          producing={packageBusy && !productionJob} onProduce={() => void startReviewedProduction()}
          progress={productionJob ? { done: processedSentences, total: productionJob.total_sentences } : undefined}
          renderInspector={renderInspector} idlePanel={idlePanel} planillaPanel={planillaPanel}
          showPlanilla={showPlanilla} onShowPlanilla={() => setShowPlanilla(true)} onHidePlanilla={() => setShowPlanilla(false)}
          // Before production the panel on the right explains the step; the
          // app's status line is about the job, its decisions and the download.
          message={productionJob && message ? publicCopy(message) : ''}
          statusExtra={productionJob?.release ? <span>Calidad: {productionJob.release.label}</span> : null}/>
      </section>
      <section id="panel-export" className="workspace-panel export-panel" role="tabpanel" aria-labelledby="tab-export" hidden={workspaceTab !== 'export'}>
        <section className="export-workspace workflow-card" aria-labelledby="export-title"><header className="export-workspace-heading"><div><p className="book-review-kicker">PASO 3 DE 3 · ENTREGA</p><h2 id="export-title">Exportar audiolibro</h2><p>Elige abajo cómo generarlo. El paquete DAISY incluye el audio por capítulos, la navegación y el texto sincronizado.</p></div><div className="book-review-stats"><span>{bookChapters.length} capítulos</span><span>{totalParagraphCount} párrafos revisables</span></div></header>
          <details className="metadata-editor"><summary>Datos bibliográficos del libro</summary><p>Corrige los datos detectados. Se guardarán en los metadatos del libro dentro del ZIP.</p><label htmlFor="metadata-title">Título<input id="metadata-title" maxLength={500} value={bookMetadata.title} onChange={event => setBookMetadata(current => ({ ...current, title: event.target.value }))}/></label><label htmlFor="metadata-author">Autoría<input id="metadata-author" maxLength={500} value={bookMetadata.author} onChange={event => setBookMetadata(current => ({ ...current, author: event.target.value }))}/></label><div className="metadata-row"><label htmlFor="metadata-edition">Edición<input id="metadata-edition" maxLength={500} value={bookMetadata.edition} onChange={event => setBookMetadata(current => ({ ...current, edition: event.target.value }))}/></label><label htmlFor="metadata-year">Año de publicación<input id="metadata-year" maxLength={500} value={bookMetadata.publication_year} onChange={event => setBookMetadata(current => ({ ...current, publication_year: event.target.value }))}/></label></div><label htmlFor="metadata-publisher">Editorial<input id="metadata-publisher" maxLength={500} value={bookMetadata.publisher} onChange={event => setBookMetadata(current => ({ ...current, publisher: event.target.value }))}/></label></details>
          {bookChapters.length === 1 && totalParagraphCount > 1 && !hasManualSections && <div className="helper-action"><div><strong>¿El libro no tiene capítulos detectables?</strong><p>Puedo crear secciones de navegación según la longitud, respetando los límites de párrafo. Son secciones para orientarse, no capítulos originales.</p></div><button type="button" className="package-download" onClick={createNavigationSections}>Crear secciones de navegación</button></div>}
          {hasManualSections && <div className="helper-action"><p>Secciones creadas por longitud. Revisa sus límites antes de exportar.</p><button type="button" className="helper-secondary" onClick={restoreDetectedStructure}>Restaurar estructura original</button></div>}
          {productionJob && <section className="workflow-card production-progress" aria-labelledby="production-title"><h3 id="production-title">Producción del audiolibro</h3><p className="production-voice">Voz utilizada en este trabajo: <strong>{productionJob.voice_name || productionJob.voice_id || 'sin registrar'}</strong>{productionJob.voice_language ? ` (${productionJob.voice_language})` : ''}</p>{productionJob.voice_id && voiceId && productionJob.voice_id !== voiceId && <p className="audio-setup-note" role="status">Has seleccionado otra voz después de iniciar este trabajo. Este resultado y su informe corresponden a la voz anterior; vuelve a producir para usar la nueva.</p>}<p role="status" aria-live="polite">{publicCopy(message)}</p><progress max={productionJob.total_sentences || 1} value={Object.entries(productionJob.progress ?? {}).reduce((sum, [state, count]) => sum + (['pending', 'generating'].includes(state) ? 0 : count), 0)} aria-label="Avance de producción"/><p>{Object.entries(productionJob.progress ?? {}).map(([state, count]) => `${sentenceStateLabel(state)}: ${count}`).join(' · ')}</p>{inFlightSentences > 0 && <p className="production-working" role="status" aria-live="polite"><span className="spinner" aria-hidden="true"/>Narrando y comprobando {inFlightSentences === 1 ? 'una frase' : `${inFlightSentences} frases`}… Al terminar se reconstruyen el capítulo afectado y el paquete, así que puede tardar un minuto.</p>}{productionJob.release && <ReleasePanel release={productionJob.release} reviewerName={reviewerName}
              onReviewerName={setReviewerName} confirmed={releaseConfirmed} onConfirmed={setReleaseConfirmed}
              onAcknowledge={issueId => void releaseAction('acknowledge', { issue_id: issueId, reviewer: reviewerName })}
              onRelease={() => void releaseAction('release', { reviewer: reviewerName, confirm: releaseConfirmed })}/>}
            {(productionJob.fault_locations?.length ?? 0) > 0 && <FaultLocations jobId={productionJob.job_id}
              locations={productionJob.fault_locations ?? []}/>}<div className="review-controls"><button type="button" className="package-download" onClick={refreshProductionJob}>Actualizar estado</button>{productionJob.state === 'completed' && (!productionJob.release || ['PASSED', 'RELEASED'].includes(productionJob.release.state)) && <button type="button" className="package-download" onClick={() => void downloadJobArtifact(productionJob.job_id, productionJob.source_name)}>Descargar ZIP DAISY</button>}{productionJob.state === 'completed' && productionJob.release && !['PASSED', 'RELEASED'].includes(productionJob.release.state) && <button type="button" className="package-download secondary" onClick={() => void downloadDiagnosticArtifact(productionJob.job_id, productionJob.source_name)}>Descargar paquete de diagnóstico (sin validar)</button>}</div>
            {reviewQueue.length > 0 && <div className="review-queue">
              <h4 className="review-queue-title">{pendingReview ? `Párrafos que necesitan tu revisión (${pendingReview})` : 'Párrafos corregidos'}</h4>
              <p className="review-queue-help">Las partes resaltadas son las que no coinciden entre el texto y el audio.
                 Escucha el párrafo y decide: si la locución es correcta, acéptala; si no, vuelve a narrarla o corrige
                 el texto. La nueva versión se narra al momento y suena aquí mismo.</p>
              {reviewQueue.map(sentence => {
                const expectedRanges = sentence.issues.map(issue => ({ start: issue.expected_start, end: issue.expected_end }))
                const recognizedRanges = sentence.issues.map(issue => ({ start: issue.recognized_start, end: issue.recognized_end }))
                const busy = reviewingSentence === sentence.sentence_id
                const fixed = sentence.state === 'verified'
                const fresh = Boolean(renarrated[sentence.sentence_id])
                // A new reading has a new version, so the player never replays a cached old one.
                const version = encodeURIComponent(sentence.updated_at || sentence.audio_path || '')
                return <article className={`review-results review-queue-item${fixed ? ' is-fixed' : ''}`} key={sentence.sentence_id}>
                  <header className="review-queue-head">
                    <span className={`qa-badge qa-${sentence.state}`}>{fixed ? 'Corregida y comprobada' : sentenceStateLabel(sentence.state)}</span>
                    <span className="review-queue-where">Capítulo {sentence.chapter_index + 1} · {sentence.chapter_title} · Párrafo {sentence.sequence + 1}</span>
                  </header>
                  {fixed && <p className="qa-fixed-note">La nueva locución coincide con el texto. Escúchala y, si te gusta, pulsa «Listo».</p>}
                  {!fixed && <div className="qa-compare">
                    <section><h5>Texto del libro</h5><p>{highlightDifferences(sentence.text, expectedRanges)}</p></section>
                    <section><h5>Lo que se oye en el audio</h5><p>{sentence.recognized_text
                      ? highlightDifferences(sentence.recognized_text, recognizedRanges)
                      : <em>No se obtuvo transcripción de este párrafo.</em>}</p></section>
                  </div>}
                  {!fixed && sentence.issues.length > 0 && <ul className="qa-issue-list">{sentence.issues.map((issue, index) =>
                    <li key={`${issue.kind}-${index}`}><strong>{qaIssueLabel(issue.category, issue.kind)}.</strong>{issue.reason ? ` ${issue.reason}` : ''}
                      {typeof issue.audio_start === 'number' && <span className="qa-issue-time">
                        {' '}Se oye en el segundo {issue.audio_start.toFixed(1).replace('.', ',')} del párrafo
                        {typeof issue.book_time === 'number' ? ` (${clockLabel(issue.book_time)} del libro completo)` : ''}.
                        {sentence.audio_path && <button type="button" className="qa-seek"
                          onClick={() => playFrom(`audio-${sentence.sentence_id}`, issue.audio_start ?? 0)}>Escuchar desde ahí</button>}
                      </span>}</li>)}</ul>}
                  {!fixed && sentence.diagnosis?.recommendation && <p className="qa-diagnosis"><strong>
                    {sentence.diagnosis.origin ? `Origen probable: ${originLabel(sentence.diagnosis.origin)}. ` : ''}</strong>
                    {sentence.diagnosis.recommendation}</p>}
                  {sentence.last_error && <p className="qa-note">{publicCopy(sentence.last_error)}</p>}
                  {busy && <p className="production-working" role="status"><span className="spinner" aria-hidden="true"/>Narrando la nueva versión del párrafo…</p>}
                  {sentence.audio_path
                    ? <label className="qa-audio">{fresh ? 'Escucha la nueva locución' : 'Escucha este párrafo'}
                        <audio id={`audio-${sentence.sentence_id}`} key={version} controls preload={fresh ? 'auto' : 'none'} autoPlay={lastRenarrated === sentence.sentence_id}
                          src={`${API}/api/jobs/${productionJob.job_id}/sentences/${encodeURIComponent(sentence.sentence_id)}/audio?v=${version}`}/>
                      </label>
                    : <p className="qa-note">No hay audio guardado para este párrafo, así que hay que volver a narrarlo.</p>}
                  {pronunciationEditor(sentence)}
                  <label className="qa-correction">Texto corregido (sólo si vas a corregirlo)
                    <textarea rows={3} value={sentenceCorrections[sentence.sentence_id] ?? sentence.text}
                      onChange={event => setSentenceCorrections(current => ({ ...current, [sentence.sentence_id]: event.target.value }))}/>
                  </label>
                  <div className="review-controls">
                    {fixed
                      ? <button type="button" className="package-download" disabled={Boolean(reviewingSentence)}
                          onClick={() => dismissRenarrated(sentence.sentence_id)}>Listo</button>
                      : <button type="button" className="package-download" disabled={Boolean(reviewingSentence) || !sentence.audio_path}
                          onClick={() => decideSentence(sentence, 'accept')}>{busy ? 'Guardando…' : 'La locución es correcta'}</button>}
                    <button type="button" className="package-download" disabled={Boolean(reviewingSentence)}
                      onClick={() => decideSentence(sentence, 'retry')}>Volver a narrarla</button>
                    <button type="button" className="package-download" disabled={Boolean(reviewingSentence)}
                      onClick={() => decideSentence(sentence, 'correct')}>Corregir el texto y narrarla</button>
                  </div>
                </article>
              })}
            </div>}
          </section>}
          <fieldset className="export-choices">
            <legend>Cómo quieres generar el audiolibro</legend>
            <label className={exportMode === 'direct' ? 'export-choice is-selected' : 'export-choice'}>
              <input type="radio" name="export-mode" value="direct" checked={exportMode === 'direct'}
                onChange={() => setExportMode('direct')}/>
              <span className="export-choice-title">Descargar directamente
                <span className="export-choice-tag is-unverified">Sin verificar</span></span>
              <span className="export-choice-text">Genera el audiolibro completo y lo descarga en un paso. Sin revisión
                 automática de calidad, sin informe y sin cola de revisión. Es la opción rápida si sólo quieres el archivo.</span>
            </label>
            <label className={exportMode === 'reviewed' ? 'export-choice is-selected' : 'export-choice'}>
              <input type="radio" name="export-mode" value="reviewed" checked={exportMode === 'reviewed'}
                onChange={() => setExportMode('reviewed')}/>
              <span className="export-choice-title">Producir con revisión de calidad
                <span className="export-choice-tag is-verified">Recomendada</span></span>
              <span className="export-choice-text">Narra párrafo a párrafo, transcribe el audio y lo compara con el texto.
                 Los párrafos dudosos aparecen aquí para que decidas, y el paquete incluye el informe de calidad y el
                 registro. Es la opción para una entrega formal.</span>
            </label>
          </fieldset>
          <p className="export-note">La vista previa y las dos opciones comparten el audio ya generado: cambiar de una a
             otra no vuelve a pagar por los párrafos que no han cambiado.</p>
          {(selectedFile || productionJob) && <div className="export-actions" role="group" aria-labelledby="export-actions-title">
            <div className={directDownloadBusy ? 'export-actions-status is-busy' : 'export-actions-status'}>
              <strong id="export-actions-title">{directDownloadBusy ? 'Generando el audiolibro…' : packageBusy ? 'Produciendo con revisión…' : productionJob?.state === 'completed' ? 'El audiolibro está listo' : 'Elige cómo generarlo'}</strong>
              {directDownloadBusy && directProgress?.total
                ? <progress className="direct-progress" max={directProgress.total} value={directProgress.done}
                    aria-label="Avance del audiolibro"/>
                : null}
              <span>{directDownloadBusy
                ? directProgress?.total
                  ? `Párrafo ${directProgress.done} de ${directProgress.total}`
                    + (directProgress.reused ? ` · ${directProgress.reused} ya narrados, sin coste` : '')
                    + '. La descarga empezará sola al terminar.'
                  : 'Preparando el libro. Reutilizamos el audio que ya exista; sólo se genera lo que falte.'
                : packageBusy
                  ? 'Narrando y revisando párrafo a párrafo. Puedes seguir el avance más arriba.'
                  : productionJob?.state === 'completed'
                    ? 'Descarga el ZIP con el audio por capítulos, la navegación DAISY y el informe de calidad.'
                    : 'Las dos opciones usan la voz seleccionada en el paso 1.'}</span>
            </div>
            <div className="export-actions-buttons">
              {/* Direct: generate and download in one step, no review queue. */}
              <button type="button" className={exportMode === 'direct' ? 'export-button is-primary' : 'export-button'}
                onClick={() => void downloadBookDirectly()}
                disabled={!selectedFile || busy || packageBusy || directDownloadBusy || !selectedNarrationIds.length || !canGenerateAudio}
                aria-busy={directDownloadBusy}>
                {directDownloadBusy ? <span className="spinner" aria-hidden="true"/> : <Download size={18} aria-hidden="true"/>}
                <span>Descargar audiolibro ahora <small className="unverified-tag">(sin verificar)</small></span>
              </button>
              {productionJob?.state === 'completed'
                ? <button type="button" className={exportMode === 'reviewed' ? 'export-button is-primary' : 'export-button'}
                    onClick={() => void downloadJobArtifact(productionJob.job_id, productionJob.source_name || book)} disabled={directDownloadBusy}>
                    <Download size={18} aria-hidden="true"/><span>Descargar paquete revisado</span>
                  </button>
                : <button type="button" className={exportMode === 'reviewed' ? 'export-button is-primary' : 'export-button'}
                    onClick={() => void startReviewedProduction()}
                    disabled={!selectedFile || busy || packageBusy || directDownloadBusy || !selectedNarrationIds.length || !canGenerateAudio}
                    aria-busy={packageBusy}>
                    {packageBusy ? <span className="spinner" aria-hidden="true"/> : <CheckCircle2 size={18} aria-hidden="true"/>}
                    <span>Producir con revisión de calidad</span>
                  </button>}
            </div>
          </div>}
        </section>
      </section>
    </section>
    <footer className="editor-footer"><span>Creado con cuidado para cada lector.</span><span>Accesible desde el principio · Siempre a tu alcance</span></footer>
  </main>
}


// What the release gate decided, dimension by dimension, and the two decisions
// only a person can take: confirming an extraction warning and publishing.
export function ReleasePanel({ release, reviewerName, onReviewerName, confirmed, onConfirmed, onAcknowledge, onRelease }: {
  release: ReleaseStatus
  reviewerName: string
  onReviewerName: (value: string) => void
  confirmed: boolean
  onConfirmed: (value: boolean) => void
  onAcknowledge: (issueId: string) => void
  onRelease: () => void
}) {
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
    <h4 id="release-title">Estado de calidad: {release.label}</h4>
    {release.released && <p role="status">Publicado por {release.released.by} el {new Date(release.released.at).toLocaleString('es-ES')}.</p>}
    {(release.blockers.length > 0 || release.reviews.length > 0) && <ul className="release-reasons">
      {release.blockers.map(reason => <li key={reason} className="is-blocker">{reason}</li>)}
      {release.reviews.map(reason => <li key={reason}>{reason}</li>)}
    </ul>}
    <table className="release-dimensions"><tbody>
      {rows.map(([label, status, detail]) => <tr key={label}><th scope="row">{label}</th><td>{status ? dimensionStatus(status) : '—'}</td><td>{detail}</td></tr>)}
    </tbody></table>
    {(warnings.length > 0 || release.state === 'PASSED') && <label className="release-reviewer">Tu nombre (queda registrado con la decisión)
      <input value={reviewerName} onChange={event => onReviewerName(event.target.value)} autoComplete="name"/></label>}
    {warnings.length > 0 && <ul className="release-warnings">{warnings.map(warning =>
      <li key={warning.id}><span>{warning.message}{warning.text ? ` «${warning.text}»` : ''}</span>
        <button type="button" disabled={!reviewerName.trim()} onClick={() => onAcknowledge(warning.id)}>He comprobado el original</button></li>)}
    </ul>}
    {release.state === 'PASSED' && <div className="release-action">
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
