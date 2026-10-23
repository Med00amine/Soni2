import { useEffect, useRef, useState, type ReactNode } from 'react'
import { BookOpen, Upload, Download, Volume2, CheckCircle2 } from 'lucide-react'
import './production-status.css'

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
type QaIssue = { kind: string; category?: string; difference_type?: string; expected?: string; recognized?: string; detail?: string; reason?: string; expected_start?: number; expected_end?: number; recognized_start?: number; recognized_end?: number }
type AudioReview = { recognized_text: string; recognizer?: string; verdict?: string; confidence?: number; issues: QaIssue[]; review_note: string }
type ProductionSentence = { sentence_id: string; chapter_index: number; chapter_title: string; sequence: number; text: string; state: string; recognized_text: string; issues: QaIssue[]; audio_path?: string | null; quality_retakes: number; technical_attempts: number; last_error: string; reused_audio?: number | boolean; updated_at?: string }
type ProductionJob = { job_id: string; state: string; source_name: string; total_sentences: number; progress: Record<string, number>; sentences: ProductionSentence[]; error?: string; voice_id?: string; voice_name?: string; voice_language?: string }
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

function qaIssueLabel(category?: string, kind?: string) {
  if (category === 'audio_defect') return 'Posible problema de audio'
  if (category === 'ambiguous_expression') return 'Expresión ambigua; requiere revisión'
  if (category === 'likely_mismatch') return 'Diferencia probable; revisar'
  return kind === 'insert' ? 'Posible palabra añadida' : kind === 'delete' ? 'Posible palabra omitida' : 'Posible sustitución'
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
  const [searchQuery, setSearchQuery] = useState('')
  const [reviewChapterIndex, setReviewChapterIndex] = useState(0)
  const [reviewPageIndex, setReviewPageIndex] = useState(0)
  const [reviewSidebarOpen, setReviewSidebarOpen] = useState(true)
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
    setAudio(''); setTranscript(''); setBookChapters([]); setSourceBookChapters([]); setOriginalBookChapters([]); setHasManualSections(false); setSearchQuery(''); setReviewChapterIndex(0); setReviewPageIndex(0); setReviewSidebarOpen(true); setWorkspaceTab('upload'); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setBookMetadata({ title: '', author: '', edition: '', publication_year: '', publisher: '' }); setBook(file.name); setMessage('Libro cargado. Elige una voz y pulsa «Generar vista previa».')
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
    const corrections: Record<string, string> = {}
    const originalParagraphs = originalBookChapters.flatMap((chapter, chapterIndex) => chapter.paragraphs.map((text, paragraphIndex) => ({ chapterIndex, paragraphIndex, text })))
    const currentParagraphs = bookChapters.flatMap(chapter => chapter.paragraphs)
    currentParagraphs.forEach((paragraph, flatIndex) => {
      const original = originalParagraphs[flatIndex]
      if (original && paragraph !== original.text) {
        corrections[`${original.chapterIndex}:${original.paragraphIndex}`] = paragraph
      }
    })
    form.append('corrections_json', JSON.stringify(corrections))
    form.append('create_navigation_sections', String(hasManualSections))
    return form
  }

  function bookFilename(sourceName: string) {
    return `${(sourceName || book).replace(/\.[^.]+$/, '') || 'libro'}-daisy.zip`
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
      const body = await probe.json().catch(() => ({})) as { detail?: string }
      throw new Error(body.detail || `No se ha podido descargar el archivo (HTTP ${probe.status}).`)
    }
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
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
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase()
  const chapterSearchResults = bookChapters.map((chapter, chapterIndex) => ({
    ...chapter,
    chapterIndex,
    matchingParagraphs: chapter.paragraphs
      .map((text, paragraphIndex) => ({ text, paragraphIndex }))
      .filter(paragraph => !normalizedSearch || paragraph.text.toLocaleLowerCase().includes(normalizedSearch)),
  })).filter(chapter => !normalizedSearch || chapter.matchingParagraphs.length > 0)
  const matchingParagraphCount = chapterSearchResults.reduce((count, chapter) => count + chapter.matchingParagraphs.length, 0)
  const totalParagraphCount = bookChapters.reduce((count, chapter) => count + chapter.paragraphs.length, 0)
  const totalWordCount = bookChapters.reduce((count, chapter) => count + chapter.paragraphs.join(' ').split(/\s+/).filter(Boolean).length, 0)
  const helperSearchResults = normalizedSearch
    ? chapterSearchResults.flatMap(chapter => chapter.matchingParagraphs.map(paragraph => ({
      ...paragraph,
      chapterIndex: chapter.chapterIndex,
      chapterTitle: chapter.title,
    }))).slice(0, 8)
    : []
  const reviewPages = bookChapters.flatMap((chapter, chapterIndex) => {
    const pageSize = 12
    const pageCount = Math.max(1, Math.ceil(chapter.paragraphs.length / pageSize))
    return Array.from({ length: pageCount }, (_, pageIndex) => ({
      chapterIndex,
      chapterTitle: chapter.title,
      pageIndex,
      pageCount,
      start: pageIndex * pageSize,
      paragraphs: chapter.paragraphs.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    }))
  })
  const foundReviewPageIndex = reviewPages.findIndex(page => page.chapterIndex === reviewChapterIndex && page.pageIndex === reviewPageIndex)
  const currentReviewPageIndex = Math.max(0, foundReviewPageIndex)
  const currentReviewPage = reviewPages[currentReviewPageIndex]

  function navigateReviewPage(offset: number) {
    const nextPage = reviewPages[currentReviewPageIndex + offset]
    if (!nextPage) return
    setReviewChapterIndex(nextPage.chapterIndex)
    setReviewPageIndex(nextPage.pageIndex)
  }

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

  function openParagraphForEditing(chapterIndex: number, paragraphIndex: number) {
    setReviewChapterIndex(chapterIndex)
    setReviewPageIndex(Math.floor(paragraphIndex / 12))
    setReviewSidebarOpen(true)
    window.requestAnimationFrame(() => {
      const paragraph = document.getElementById(`book-paragraph-${chapterIndex}-${paragraphIndex}`)
      paragraph?.focus({ preventScroll: true })
      paragraph?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
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

  return <main className="shell editor-shell">
    <a className="skip" href="#main">Saltar al contenido principal</a>
    <header className="top editor-top"><a className="brand" href="/" aria-label="Inicio de Soni2"><img className="brand-logo" src="/soni2-logo.svg" alt="Soni2" width="200" height="42"/></a><span className="top-note">Una forma más tranquila de disfrutar los libros</span></header>
    <section id="main" className="hero workspace editor-workspace" aria-label="Espacio de trabajo de Soni2">
      <nav className="workspace-tabs" role="tablist" aria-label="Flujo de trabajo del audiolibro">
        <button type="button" role="tab" id="tab-upload" aria-controls="panel-upload" aria-selected={workspaceTab === 'upload'} onClick={() => switchWorkspaceTab('upload')}><span>1</span>Subir libro</button>
        <button type="button" role="tab" id="tab-review" aria-controls="panel-review" aria-selected={workspaceTab === 'review'} disabled={!transcript} onClick={() => switchWorkspaceTab('review')}><span>2</span>Escuchar y revisar</button>
        <button type="button" role="tab" id="tab-export" aria-controls="panel-export" aria-selected={workspaceTab === 'export'} disabled={!transcript} onClick={() => switchWorkspaceTab('export')}><span>3</span>Exportar</button>
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
          {message && <p className="upload-status" role="status" aria-live="polite">{publicCopy(message)}</p>}
        </section>
      </section>
      <section id="panel-review" className="workspace-panel review-panel" role="tabpanel" aria-labelledby="tab-review" hidden={workspaceTab !== 'review'}>
        {(audio || transcript) ? <div className={'review-workspace-grid' + (bookChapters.length === 0 ? ' no-sidebar' : '') + (!reviewSidebarOpen ? ' sidebar-collapsed' : '')}>
          {bookChapters.length > 0 && <aside className={`workspace-chapter-sidebar${reviewSidebarOpen ? '' : ' sidebar-hidden'}`} aria-label="Navegación del libro"><div className="sidebar-title"><h2>Contenido</h2><button type="button" className="review-sidebar-toggle" aria-label={reviewSidebarOpen ? 'Ocultar capítulos' : 'Mostrar capítulos'} aria-expanded={reviewSidebarOpen} onClick={() => setReviewSidebarOpen(open => !open)}>{reviewSidebarOpen ? 'Ocultar' : 'Mostrar'}</button></div>{reviewSidebarOpen && <nav aria-label="Capítulos y secciones">{bookChapters.map((chapter, chapterIndex) => <div className="review-nav-group" key={`${chapter.title}-${chapterIndex}`}><button type="button" className={`review-chapter-link${chapterIndex === reviewChapterIndex ? ' active' : ''}`} aria-current={chapterIndex === reviewChapterIndex ? 'location' : undefined} onClick={() => { setReviewChapterIndex(chapterIndex); setReviewPageIndex(0) }}><span>{chapter.title}</span><small>{chapter.paragraphs.length} párrafos</small></button>{chapterIndex === reviewChapterIndex && reviewPages.filter(page => page.chapterIndex === chapterIndex).length > 1 && <div className="review-section-links" aria-label={`Secciones de ${chapter.title}`}>{reviewPages.filter(page => page.chapterIndex === chapterIndex).map(page => <button type="button" key={page.pageIndex} className={page.pageIndex === reviewPageIndex ? 'active' : ''} aria-current={page.pageIndex === reviewPageIndex ? 'page' : undefined} onClick={() => setReviewPageIndex(page.pageIndex)}>Sección {page.pageIndex + 1}</button>)}</div>}</div>)}</nav>}</aside>}
          <section className="book-review-document" aria-label="Editor del libro">
            <div className="review-document-toolbar"><div><p>REVISIÓN DEL TEXTO</p><h2>{currentReviewPage?.chapterTitle || 'Texto del libro'}</h2></div><div className="review-page-progress">{currentReviewPage ? `Capítulo ${currentReviewPage.chapterIndex + 1} de ${bookChapters.length} · Sección ${currentReviewPage.pageIndex + 1} de ${currentReviewPage.pageCount}` : 'Texto disponible'}</div><button type="button" className="review-sidebar-toggle mobile-chapter-toggle" aria-expanded={reviewSidebarOpen} onClick={() => setReviewSidebarOpen(open => !open)}>{reviewSidebarOpen ? 'Ocultar índice' : 'Ver índice'}</button></div>
            <div className="book-review-toolbar"><label htmlFor="book-search-input">Buscar en el libro<input id="book-search-input" type="search" value={searchQuery} onChange={event => setSearchQuery(event.target.value)} aria-describedby="book-search-count" placeholder="Palabra, persona o lugar"/></label><span id="book-search-count" role="status" aria-live="polite">{normalizedSearch ? `${matchingParagraphCount} párrafos encontrados` : `${totalParagraphCount} párrafos`}</span></div>
            <div className="review-document-content" aria-label={currentReviewPage ? `Texto de ${currentReviewPage.chapterTitle}, sección ${currentReviewPage.pageIndex + 1}` : 'Texto del libro'}>{normalizedSearch && <div className="helper-results" aria-label="Resultados de búsqueda">{helperSearchResults.length ? helperSearchResults.map(result => <button type="button" key={`${result.chapterIndex}-${result.paragraphIndex}`} onClick={() => openParagraphForEditing(result.chapterIndex, result.paragraphIndex)}><strong>{result.chapterTitle} · párrafo {result.paragraphIndex + 1}</strong><span>{result.text}</span></button>) : <p>No he encontrado esa palabra en el texto del libro.</p>}</div>}{currentReviewPage ? currentReviewPage.paragraphs.map((text, localIndex) => { const paragraphIndex = currentReviewPage.start + localIndex; return <label className="book-paragraph" key={`${currentReviewPage.chapterIndex}-${paragraphIndex}`} htmlFor={`book-paragraph-${currentReviewPage.chapterIndex}-${paragraphIndex}`}><span>Párrafo {paragraphIndex + 1}</span><textarea id={`book-paragraph-${currentReviewPage.chapterIndex}-${paragraphIndex}`} rows={Math.min(8, Math.max(3, Math.ceil(text.length / 90)))} value={text} onChange={event => setBookChapters(current => current.map((chapter, index) => index !== currentReviewPage.chapterIndex ? chapter : { ...chapter, paragraphs: chapter.paragraphs.map((value, part) => part === paragraphIndex ? event.target.value : value) }))}/></label>}) : <p>No hay capítulos que mostrar en el texto extraído.</p>}</div>
            {currentReviewPage && <nav className="review-pagination" aria-label="Navegación de secciones"><button type="button" onClick={() => navigateReviewPage(-1)} disabled={currentReviewPageIndex === 0}>Anterior</button><span>Sección {currentReviewPageIndex + 1} de {reviewPages.length}</span><button type="button" onClick={() => navigateReviewPage(1)} disabled={currentReviewPageIndex >= reviewPages.length - 1}>Siguiente</button></nav>}
          </section>
          <aside className="audio-preview-panel" aria-label="Reproductor y revisión de audio">
            {audio && <section id="listen-section" className="player workflow-card" aria-labelledby="player-title"><div className="player-heading"><span className="player-icon" aria-hidden="true"><Volume2 size={21}/></span><div className="player-title"><h2 id="player-title">Vista previa de audio</h2><span>{bookMetadata.title || book} · Español</span></div></div>{message && <p className="player-status" role="status" aria-live="polite" aria-atomic="true"><CheckCircle2 size={17} aria-hidden="true"/>{publicCopy(message)}</p>}<audio ref={playerRef} controls src={audio} onTimeUpdate={trackPlayback} onSeeked={trackPlayback} aria-label={`Escuchar la vista previa narrada de ${bookMetadata.title || book}`}/><div className="player-playback"><label htmlFor="playback-speed">Velocidad<select id="playback-speed" value={playbackRate} onChange={event => setPlaybackRate(Number(event.target.value))}><option value={0.75}>0,75×</option><option value={1}>1×</option><option value={1.25}>1,25×</option><option value={1.5}>1,5×</option><option value={1.75}>1,75×</option><option value={2}>2×</option></select></label></div><a className="download" href={audio} download={`${(bookMetadata.title || book).replace(/.[^.]+$/, '')}-vista-previa.mp3`}><Download size={17} aria-hidden="true"/>Descargar muestra MP3</a></section>}
            {transcript && <section className="transcript workflow-card audio-transcript" aria-labelledby="transcript-title"><h2 id="transcript-title">Texto de la muestra</h2><details><summary>Mostrar texto sincronizado</summary>{alignment.length > 0 ? <div className="timed-transcript" aria-live="off">{alignedParagraphs.map((paragraph, paragraphIndex) => <p key={`paragraph-${paragraphIndex}`}>{paragraph.map(phrase => { const index = alignment.indexOf(phrase); const active = index === currentPhrase; return <span key={`${phrase.text_start}-${index}`} className={active ? 'timed-phrase is-speaking' : 'timed-phrase'} aria-current={active ? 'true' : undefined}>{phrase.text} </span>})}</p>)}</div> : <><p className="sync-note">{'No se pudieron obtener tiempos para sincronizar esta muestra. El texto sigue disponible por parrafos.'}</p>{transcript.split(/\n{2,}/).map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>)}</>}</details>
              {capabilities?.audio_review_configured && <div className="review-controls"><button type="button" className="package-download" onClick={reviewPreview} disabled={reviewBusy || !audio}>{reviewBusy ? 'Revisando el audio…' : 'Revisar calidad del audio'}</button><small>Las diferencias son avisos para que una persona las revise.</small></div>}
              {review && <section className="review-results" aria-labelledby="review-title"><h3 id="review-title">{review.verdict === 'verified' ? 'El audio coincide con el texto' : review.verdict === 'ambiguous' ? 'Diferencias ambiguas: requieren tu criterio' : `Diferencias detectadas: ${review.issues.length}`}</h3><p>{publicCopy(review.review_note)}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, index) => <li key={`${issue.kind}-${index}`}><strong>{qaIssueLabel(issue.category, issue.kind)}:</strong> {issue.expected ? `Texto: “${issue.expected}”. ` : ''}{issue.recognized ? `Audio: “${issue.recognized}”.` : ''}{issue.reason ? <small> {issue.reason}</small> : null}</li>)}</ul>}<label htmlFor="retake-text">Edita el texto de esta muestra<textarea id="retake-text" value={transcript} onChange={event => setTranscript(event.target.value)} rows={5}/></label><button type="button" className="package-download" onClick={regenerateRetake} disabled={retakeBusy}>{retakeBusy ? 'Generando…' : 'Generar muestra corregida'}</button></section>}</section>}
          </aside>
        </div> : <div className="workspace-empty"><Volume2 size={24}/><h2>La muestra todavía no está lista</h2><p>Vuelve a Subir libro para generar una vista previa.</p><button type="button" className="helper-secondary" onClick={() => setWorkspaceTab('upload')}>Ir a Subir libro</button></div>}
      </section>
      <section id="panel-export" className="workspace-panel export-panel" role="tabpanel" aria-labelledby="tab-export" hidden={workspaceTab !== 'export'}>
        <section className="export-workspace workflow-card" aria-labelledby="export-title"><header className="export-workspace-heading"><div><p className="book-review-kicker">PASO 3 DE 3 · ENTREGA</p><h2 id="export-title">Exportar audiolibro</h2><p>Elige abajo cómo generarlo. El paquete DAISY incluye el audio por capítulos, la navegación y el texto sincronizado.</p></div><div className="book-review-stats"><span>{bookChapters.length} capítulos</span><span>{totalParagraphCount} párrafos revisables</span></div></header>
          <details className="metadata-editor"><summary>Datos bibliográficos del libro</summary><p>Corrige los datos detectados. Se guardarán en los metadatos del libro dentro del ZIP.</p><label htmlFor="metadata-title">Título<input id="metadata-title" maxLength={500} value={bookMetadata.title} onChange={event => setBookMetadata(current => ({ ...current, title: event.target.value }))}/></label><label htmlFor="metadata-author">Autoría<input id="metadata-author" maxLength={500} value={bookMetadata.author} onChange={event => setBookMetadata(current => ({ ...current, author: event.target.value }))}/></label><div className="metadata-row"><label htmlFor="metadata-edition">Edición<input id="metadata-edition" maxLength={500} value={bookMetadata.edition} onChange={event => setBookMetadata(current => ({ ...current, edition: event.target.value }))}/></label><label htmlFor="metadata-year">Año de publicación<input id="metadata-year" maxLength={500} value={bookMetadata.publication_year} onChange={event => setBookMetadata(current => ({ ...current, publication_year: event.target.value }))}/></label></div><label htmlFor="metadata-publisher">Editorial<input id="metadata-publisher" maxLength={500} value={bookMetadata.publisher} onChange={event => setBookMetadata(current => ({ ...current, publisher: event.target.value }))}/></label></details>
          {bookChapters.length === 1 && totalParagraphCount > 1 && !hasManualSections && <div className="helper-action"><div><strong>¿El libro no tiene capítulos detectables?</strong><p>Puedo crear secciones de navegación según la longitud, respetando los límites de párrafo. Son secciones para orientarse, no capítulos originales.</p></div><button type="button" className="package-download" onClick={createNavigationSections}>Crear secciones de navegación</button></div>}
          {hasManualSections && <div className="helper-action"><p>Secciones creadas por longitud. Revisa sus límites antes de exportar.</p><button type="button" className="helper-secondary" onClick={restoreDetectedStructure}>Restaurar estructura original</button></div>}
          {productionJob && <section className="workflow-card production-progress" aria-labelledby="production-title"><h3 id="production-title">Producción del audiolibro</h3><p className="production-voice">Voz utilizada en este trabajo: <strong>{productionJob.voice_name || productionJob.voice_id || 'sin registrar'}</strong>{productionJob.voice_language ? ` (${productionJob.voice_language})` : ''}</p>{productionJob.voice_id && voiceId && productionJob.voice_id !== voiceId && <p className="audio-setup-note" role="status">Has seleccionado otra voz después de iniciar este trabajo. Este resultado y su informe corresponden a la voz anterior; vuelve a producir para usar la nueva.</p>}<p role="status" aria-live="polite">{publicCopy(message)}</p><progress max={productionJob.total_sentences || 1} value={Object.entries(productionJob.progress ?? {}).reduce((sum, [state, count]) => sum + (['pending', 'generating'].includes(state) ? 0 : count), 0)} aria-label="Avance de producción"/><p>{Object.entries(productionJob.progress ?? {}).map(([state, count]) => `${sentenceStateLabel(state)}: ${count}`).join(' · ')}</p>{inFlightSentences > 0 && <p className="production-working" role="status" aria-live="polite"><span className="spinner" aria-hidden="true"/>Narrando y comprobando {inFlightSentences === 1 ? 'una frase' : `${inFlightSentences} frases`}… Al terminar se reconstruyen el capítulo afectado y el paquete, así que puede tardar un minuto.</p>}<div className="review-controls"><button type="button" className="package-download" onClick={refreshProductionJob}>Actualizar estado</button>{productionJob.state === 'completed' && <button type="button" className="package-download" onClick={() => void downloadJobArtifact(productionJob.job_id, productionJob.source_name)}>Descargar ZIP DAISY</button>}</div>
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
                    <li key={`${issue.kind}-${index}`}><strong>{qaIssueLabel(issue.category, issue.kind)}.</strong>{issue.reason ? ` ${issue.reason}` : ''}</li>)}</ul>}
                  {sentence.last_error && <p className="qa-note">{publicCopy(sentence.last_error)}</p>}
                  {busy && <p className="production-working" role="status"><span className="spinner" aria-hidden="true"/>Narrando la nueva versión del párrafo…</p>}
                  {sentence.audio_path
                    ? <label className="qa-audio">{fresh ? 'Escucha la nueva locución' : 'Escucha este párrafo'}
                        <audio key={version} controls preload={fresh ? 'auto' : 'none'} autoPlay={lastRenarrated === sentence.sentence_id}
                          src={`${API}/api/jobs/${productionJob.job_id}/sentences/${encodeURIComponent(sentence.sentence_id)}/audio?v=${version}`}/>
                      </label>
                    : <p className="qa-note">No hay audio guardado para este párrafo, así que hay que volver a narrarlo.</p>}
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
          <div className="export-choices">
            <article>
              <h4>Descargar directamente</h4>
              <p>Genera el audiolibro completo y lo descarga en un paso. Sin revisión automática de calidad, sin
                 informe y sin cola de revisión. Es la opción rápida si sólo quieres el archivo.</p>
            </article>
            <article>
              <h4>Producir con revisión de calidad</h4>
              <p>Narra párrafo a párrafo, transcribe el audio y lo compara con el texto. Los párrafos dudosos aparecen
                 aquí para que decidas, y el paquete incluye el informe de calidad y el registro. Es la opción para una
                 entrega formal.</p>
            </article>
          </div>
          <p className="export-note">La vista previa y las dos opciones comparten el audio ya generado: cambiar de una a
             otra no vuelve a pagar por los párrafos que no han cambiado.</p>
              {(selectedFile || productionJob) && <div className="review-action-dock workspace-action-bar">
            <div className={directDownloadBusy ? 'is-busy' : undefined}>
              <strong>{directDownloadBusy ? 'Generando el audiolibro…' : packageBusy ? 'Produciendo con revisión…' : productionJob?.state === 'completed' ? 'El audiolibro está listo' : 'Elige cómo generarlo'}</strong>
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
                  ? 'Narrando y revisando párrafo a párrafo. Puedes seguir el avance más abajo.'
                  : productionJob?.state === 'completed'
                    ? 'Descarga el ZIP con el audio por capítulos, la navegación DAISY y el informe de calidad.'
                    : 'Las dos opciones usan la voz seleccionada en el paso 1.'}</span>
            </div>
            <div className="action-bar-buttons">
              {/* Direct: generate and download in one step, no review queue. */}
              <button type="button" onClick={() => void downloadBookDirectly()}
                disabled={!selectedFile || busy || packageBusy || directDownloadBusy || !selectedNarrationIds.length || !canGenerateAudio}
                aria-busy={directDownloadBusy}>
                {directDownloadBusy && <span className="spinner" aria-hidden="true"/>}
                <Download size={18} aria-hidden="true"/>Descargar audiolibro ahora
              </button>
              {productionJob?.state === 'completed'
                ? <button type="button" className="secondary-action" onClick={() => void downloadJobArtifact(productionJob.job_id, productionJob.source_name || book)} disabled={directDownloadBusy}>
                    <Download size={18} aria-hidden="true"/>Descargar paquete revisado
                  </button>
                : <button type="button" className="secondary-action" onClick={() => void startReviewedProduction()}
                    disabled={!selectedFile || busy || packageBusy || directDownloadBusy || !selectedNarrationIds.length || !canGenerateAudio}
                    aria-busy={packageBusy}>
                    {packageBusy && <span className="spinner" aria-hidden="true"/>}
                    <CheckCircle2 size={18} aria-hidden="true"/>Producir con revisión de calidad
                  </button>}
            </div>
              </div>}
        </section>
      </section>
    </section>
    <footer className="editor-footer"><span>Creado con cuidado para cada lector.</span><span>Accesible desde el principio · Siempre a tu alcance</span></footer>
  </main>
}
