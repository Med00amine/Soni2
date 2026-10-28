import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import {
  AlertTriangle, ChevronDown, ChevronLeft, ChevronRight, CheckCircle2, CircleDashed, FileAudio, Loader2,
  Pause, Pencil, Play, SkipBack, SkipForward, XCircle,
} from 'lucide-react'
import './audiobook-editor.css'

// What the editor shows: the audiobook's files (sections) and the units each is
// narrated in. Before production it comes from the book's outline; once a job
// exists, from the job itself, with every unit's state and audio.
export type EditorUnit = {
  key: string
  role: string
  text: string
  pageBreaks: string[]
  note?: number | null
  source?: { chapter: number; paragraph: number } | null
  sentenceId?: string
  state?: string
  duration?: number | null
  clip?: [number, number] | null
  issues?: number
  hasAudio?: boolean
  editable?: boolean
  updatedAt?: string
}

export type EditorSection = {
  index: number
  chapterIndex: number
  fileNumber: number
  key: string
  kind: string
  level: number
  title: string
  headingSpoken: boolean
  pageStart: string
  pageEnd: string
  status: string
  duration?: number | null
  durationFinal?: boolean
  audioUrl?: string | null
  units: EditorUnit[]
}

export type EditorWarning = { code: string; message: string; section?: number | null }
export type AudioFormat = { codec: string; sample_rate: number; channels: number; bitrate_kbps?: number[] | null }

export type EditorModel = {
  mode: 'outline' | 'job'
  once: boolean
  sections: EditorSection[]
  warnings: EditorWarning[]
  totalDuration?: number | null
  pages: number
  audioFormat?: AudioFormat | null
  audioFormatFinal?: boolean
  assembled?: boolean
}

export const KIND_LABEL: Record<string, string> = {
  once_title: 'Título y aviso legal',
  once_bibliographic: 'Datos bibliográficos',
  once_index: 'Índice',
  chapter: 'Capítulo',
  subchapter: 'Subcapítulo',
}

const ROLE_LABEL: Record<string, string> = {
  heading: 'Encabezado',
  legal: 'Texto legal ONCE',
  bibliographic: 'Dato bibliográfico',
  index_entry: 'Entrada del índice',
  closing: 'Frase final',
}

const STATE_LABEL: Record<string, string> = {
  planned: 'Sin narrar',
  pending: 'En cola',
  generating: 'Narrando',
  verified: 'Correcta',
  needs_human_review: 'Revisar',
  needs_review: 'Revisar',
  unresolved: 'Sin resolver',
  failed: 'Fallo técnico',
}

const NEEDS_DECISION = new Set(['needs_human_review', 'unresolved', 'failed'])
const INITIAL_KINDS = new Set(['once_title', 'once_bibliographic', 'once_index'])

// 1:02:03,4 for a book, 2:03,4 under an hour.
export function formatClock(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const tenths = Math.max(0, Math.round(seconds * 10))
  const hours = Math.floor(tenths / 36000)
  const minutes = Math.floor((tenths % 36000) / 600)
  const rest = tenths % 600
  const secondsText = `${String(Math.floor(rest / 10)).padStart(2, '0')},${rest % 10}`
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secondsText}` : `${minutes}:${secondsText}`
}

function pageRange(section: EditorSection) {
  if (!section.pageStart) return 'sin página'
  return section.pageEnd && section.pageEnd !== section.pageStart ? `p. ${section.pageStart}–${section.pageEnd}` : `p. ${section.pageStart}`
}

function stateOf(unit: EditorUnit) {
  return unit.state ?? 'planned'
}

function StatusIcon({ status, size = 15 }: { status: string; size?: number }) {
  if (status === 'verified') return <CheckCircle2 size={size} aria-hidden="true" className="ed-icon is-ok"/>
  if (status === 'failed') return <XCircle size={size} aria-hidden="true" className="ed-icon is-bad"/>
  if (status === 'needs_review' || status === 'needs_human_review' || status === 'unresolved') {
    return <AlertTriangle size={size} aria-hidden="true" className="ed-icon is-warn"/>
  }
  if (status === 'generating') return <Loader2 size={size} aria-hidden="true" className="ed-icon is-busy"/>
  return <CircleDashed size={size} aria-hidden="true" className="ed-icon is-idle"/>
}

function formatLabel(format?: AudioFormat | null) {
  if (!format) return ''
  const rate = `${(format.sample_rate / 1000).toLocaleString('es-ES', { maximumFractionDigits: 1 })} kHz`
  const channels = format.channels === 1 ? 'mono' : format.channels === 2 ? 'estéreo' : `${format.channels} canales`
  const bitrate = format.bitrate_kbps?.length
    ? format.bitrate_kbps[0] === format.bitrate_kbps[1] ? `${format.bitrate_kbps[0]} kbit/s` : `VBR ${format.bitrate_kbps[0]}–${format.bitrate_kbps[1]} kbit/s`
    : ''
  return [format.codec, rate, channels, bitrate].filter(Boolean).join(' · ')
}

type Position = { section: number; unit: number }

export type AudiobookEditorProps = {
  model?: EditorModel
  loading: boolean
  error: string
  apiBase: string
  jobId?: string
  jobState?: string
  // False while another step is shown: the editor stops playing.
  active?: boolean
  // Editable text before production, as the reader corrects it.
  bookChapters: { title: string; paragraphs: string[] }[]
  onEditParagraph: (chapter: number, paragraph: number, text: string) => void
  onEditsDone: () => void
  // Production.
  canProduce: boolean
  producing: boolean
  progress?: { done: number; total: number }
  onProduce: () => void
  // The inspector for a produced unit (comparison, decisions, pronunciation).
  renderInspector: (unit: EditorUnit, section: EditorSection, seek: (seconds: number) => void) => ReactNode
  // Shown in the inspector when nothing has been produced yet.
  idlePanel?: ReactNode
  planillaPanel?: ReactNode
  onShowPlanilla?: () => void
  showPlanilla: boolean
  onHidePlanilla?: () => void
  message?: string
  statusExtra?: ReactNode
}

export default function AudiobookEditor(props: AudiobookEditorProps) {
  const { model, loading, error, apiBase, jobId } = props
  const sections = model?.sections ?? []
  const audioRef = useRef<HTMLAudioElement>(null)
  const listRef = useRef<HTMLOListElement>(null)
  const treeRef = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<Position>({ section: 0, unit: 0 })
  const [playing, setPlaying] = useState<Position | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [sectionTime, setSectionTime] = useState(0)
  const [speed, setSpeed] = useState(1)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [editing, setEditing] = useState<string>('')
  const [notice, setNotice] = useState('')
  const [treeFocus, setTreeFocus] = useState(0)
  const [query, setQuery] = useState('')
  // What the audio element is playing: a section's final file, or one unit's own audio.
  const source = useRef<{ kind: 'final' | 'unit'; section: number; unit: number; url: string } | null>(null)
  const pendingSeek = useRef<number | null>(null)

  const sectionIndex = Math.min(selected.section, Math.max(0, sections.length - 1))
  const section = sections[sectionIndex]
  const selectedUnit = section?.units[Math.min(selected.unit, Math.max(0, (section?.units.length ?? 1) - 1))]

  useEffect(() => {
    if (selected.section >= sections.length && sections.length) setSelected({ section: 0, unit: 0 })
  }, [sections.length, selected.section])

  useEffect(() => { if (audioRef.current) audioRef.current.playbackRate = speed }, [speed])
  useEffect(() => {
    const audio = audioRef.current
    if (props.active === false && audio && !audio.paused) audio.pause()
  }, [props.active])

  // Everything the toolbar can step through, in reading order.
  const flat = useMemo(() => sections.flatMap((item, s) => item.units.map((unit, u) => ({ s, u, unit }))), [sections])
  const problems = useMemo(() => flat.filter(entry => NEEDS_DECISION.has(stateOf(entry.unit))), [flat])
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const matches = useMemo(() => normalizedQuery
    ? flat.filter(entry => entry.unit.text.toLocaleLowerCase().includes(normalizedQuery))
    : [], [flat, normalizedQuery])

  // Where each unit starts within its section when units play one by one.
  function offsets(item: EditorSection) {
    const result: number[] = []
    let position = 0
    for (const unit of item.units) {
      result.push(position)
      position += unit.duration ?? 0
    }
    return result
  }

  function hasFinal(item?: EditorSection) {
    return Boolean(item?.audioUrl && item.units.length && item.units.every(unit => unit.clip))
  }

  function unitUrl(unit: EditorUnit) {
    if (!jobId || !unit.sentenceId || !unit.hasAudio) return ''
    return `${apiBase}/api/jobs/${encodeURIComponent(jobId)}/sentences/${encodeURIComponent(unit.sentenceId)}/audio?v=${encodeURIComponent(unit.updatedAt ?? '')}`
  }

  // The server versions the URL with the section's fingerprint, so a rebuilt
  // section is never served from the browser's cache.
  function finalUrl(item: EditorSection) {
    return `${apiBase}${item.audioUrl}`
  }

  function firstPlayable(from: Position): Position | null {
    const start = flat.findIndex(entry => entry.s === from.section && entry.u === from.unit)
    for (let index = Math.max(0, start); index < flat.length; index += 1) {
      const entry = flat[index]
      if (hasFinal(sections[entry.s]) || entry.unit.hasAudio) return { section: entry.s, unit: entry.u }
    }
    return null
  }

  function load(url: string) {
    const audio = audioRef.current
    if (!audio) return
    if (audio.getAttribute('src') !== url) {
      audio.src = url
      audio.load()
    }
  }

  function playAt(position: Position, offset = 0, autoplay = true) {
    const target = firstPlayable(position)
    const audio = audioRef.current
    if (!target || !audio) {
      setNotice(props.model?.mode === 'job'
        ? 'Esta parte todavía no tiene audio: se narra durante la producción.'
        : 'Todavía no hay audio del libro completo. Produce el audiolibro para escucharlo frase a frase.')
      return
    }
    setNotice('')
    const item = sections[target.section]
    const unit = item.units[target.unit]
    if (hasFinal(item)) {
      const url = finalUrl(item)
      source.current = { kind: 'final', section: target.section, unit: target.unit, url }
      load(url)
      seekWhenReady(((unit.clip as [number, number])[0]) + (target.section === position.section && target.unit === position.unit ? offset : 0))
    } else {
      const url = unitUrl(unit)
      source.current = { kind: 'unit', section: target.section, unit: target.unit, url }
      load(url)
      seekWhenReady(target.section === position.section && target.unit === position.unit ? offset : 0)
    }
    setPlaying(target)
    setSelected(target)
    if (autoplay) void audio.play().catch(() => undefined)
  }

  function seekWhenReady(seconds: number) {
    const audio = audioRef.current
    if (!audio) return
    if (audio.readyState >= 1) {
      audio.currentTime = Math.max(0, seconds)
      pendingSeek.current = null
    } else {
      pendingSeek.current = Math.max(0, seconds)
    }
  }

  function onLoadedMetadata() {
    const audio = audioRef.current
    if (audio && pendingSeek.current != null) {
      audio.currentTime = pendingSeek.current
      pendingSeek.current = null
    }
    if (audio) audio.playbackRate = speed
  }

  function onTimeUpdate() {
    const audio = audioRef.current
    const current = source.current
    if (!audio || !current) return
    const item = sections[current.section]
    if (!item) return
    if (current.kind === 'final') {
      const time = audio.currentTime
      setSectionTime(time)
      // The unit whose measured clip holds this moment, or the last one begun.
      let index = 0
      item.units.forEach((unit, position) => { if (unit.clip && time >= unit.clip[0] - 0.01) index = position })
      if (index !== current.unit) {
        current.unit = index
        const next = { section: current.section, unit: index }
        setPlaying(next)
        setSelected(next)
      }
    } else {
      setSectionTime((offsets(item)[current.unit] ?? 0) + audio.currentTime)
    }
  }

  function onEnded() {
    const current = source.current
    if (!current) return
    const item = sections[current.section]
    const next: Position | null = current.kind === 'final' || current.unit + 1 >= (item?.units.length ?? 0)
      ? (current.section + 1 < sections.length ? { section: current.section + 1, unit: 0 } : null)
      : { section: current.section, unit: current.unit + 1 }
    if (next) playAt(next)
    else { setIsPlaying(false); setNotice('Fin del audiolibro.') }
  }

  function togglePlay() {
    const audio = audioRef.current
    if (!audio) return
    if (isPlaying) { audio.pause(); return }
    if (source.current && audio.getAttribute('src')) {
      const current = source.current
      if (current.section === selected.section && current.unit === selected.unit) {
        void audio.play().catch(() => undefined)
        return
      }
    }
    playAt(selected)
  }

  function step(delta: number) {
    const index = flat.findIndex(entry => entry.s === selected.section && entry.u === selected.unit)
    const target = flat[Math.min(flat.length - 1, Math.max(0, index + delta))]
    if (!target) return
    const position = { section: target.s, unit: target.u }
    if (isPlaying) playAt(position)
    else { setSelected(position); focusUnit(position) }
  }

  function stepSection(delta: number) {
    const target = Math.min(sections.length - 1, Math.max(0, sectionIndex + delta))
    selectSection(target)
  }

  function finishEditing() {
    if (!editing) return
    setEditing('')
    props.onEditsDone()
  }

  function selectSection(index: number, play = isPlaying) {
    const position = { section: index, unit: 0 }
    if (index !== sectionIndex) finishEditing()
    setTreeFocus(index)
    if (play) playAt(position)
    else { setSelected(position); setSectionTime(0) }
    props.onHidePlanilla?.()
  }

  function goToProblem(delta: number) {
    if (!problems.length) return
    const index = flat.findIndex(entry => entry.s === selected.section && entry.u === selected.unit)
    const ordered = delta > 0 ? problems : [...problems].reverse()
    const target = ordered.find(entry => {
      const position = flat.findIndex(item => item.s === entry.s && item.u === entry.u)
      return delta > 0 ? position > index : position < index
    }) ?? ordered[0]
    const position = { section: target.s, unit: target.u }
    setSelected(position)
    setTreeFocus(target.s)
    props.onHidePlanilla?.()
    focusUnit(position)
  }

  function focusUnit(position: Position) {
    window.requestAnimationFrame(() => {
      const row = document.getElementById(`ed-unit-${position.section}-${position.unit}`)
      row?.scrollIntoView?.({ block: 'nearest' })
    })
  }

  // Keep the phrase being heard in view while it plays.
  useEffect(() => {
    if (!playing) return
    const row = document.getElementById(`ed-unit-${playing.section}-${playing.unit}`)
    row?.scrollIntoView?.({ block: 'nearest' })
  }, [playing])

  function seekSection(value: number) {
    if (!section) return
    if (hasFinal(section)) {
      if (source.current?.kind === 'final' && source.current.section === sectionIndex) {
        seekWhenReady(value)
        setSectionTime(value)
      } else {
        const unit = section.units.reduce((found, item, position) => item.clip && value >= item.clip[0] ? position : found, 0)
        playAt({ section: sectionIndex, unit }, value - (section.units[unit].clip?.[0] ?? 0), isPlaying)
      }
      return
    }
    const starts = offsets(section)
    let unit = 0
    starts.forEach((start, position) => { if (value >= start) unit = position })
    playAt({ section: sectionIndex, unit }, value - starts[unit], isPlaying)
    setSectionTime(value)
  }

  // Shortcuts, as a recording editor has them, never while typing.
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return
    if (event.key === ' ' && target.tagName !== 'BUTTON') { event.preventDefault(); togglePlay() }
    else if (event.altKey && event.key === 'ArrowDown') { event.preventDefault(); stepSection(1) }
    else if (event.altKey && event.key === 'ArrowUp') { event.preventDefault(); stepSection(-1) }
    else if (event.altKey && event.key === 'ArrowRight') { event.preventDefault(); step(1) }
    else if (event.altKey && event.key === 'ArrowLeft') { event.preventDefault(); step(-1) }
    else if (event.altKey && (event.key === 'n' || event.key === 'N')) { event.preventDefault(); goToProblem(1) }
  }

  // The structure as a tree: initial files grouped, subchapters under their chapter.
  type TreeRow = { section: EditorSection; depth: number; parent?: string; hasChildren: boolean; group?: string }
  const treeRows = useMemo(() => {
    const rows: TreeRow[] = []
    let parentKey = ''
    sections.forEach((item, position) => {
      const next = sections[position + 1]
      if (INITIAL_KINDS.has(item.kind)) {
        rows.push({ section: item, depth: 2, parent: 'initial', hasChildren: false, group: 'initial' })
        parentKey = ''
        return
      }
      if (item.level === 2 && parentKey) {
        rows.push({ section: item, depth: 2, parent: parentKey, hasChildren: false })
        return
      }
      parentKey = item.key
      rows.push({ section: item, depth: 1, hasChildren: Boolean(next && next.level === 2 && !INITIAL_KINDS.has(next.kind)) })
    })
    return rows
  }, [sections])
  const hasInitial = sections.some(item => INITIAL_KINDS.has(item.kind))
  const visibleRows = treeRows.filter(row => !(row.parent && collapsed[row.parent]))

  function onTreeKey(event: KeyboardEvent<HTMLElement>, row: TreeRow, position: number) {
    const move = (index: number) => {
      const target = visibleRows[Math.min(visibleRows.length - 1, Math.max(0, index))]
      if (!target) return
      setTreeFocus(target.section.index)
      document.getElementById(`ed-tree-${target.section.index}`)?.focus()
    }
    if (event.key === 'ArrowDown') { event.preventDefault(); move(position + 1) }
    else if (event.key === 'ArrowUp') { event.preventDefault(); move(position - 1) }
    else if (event.key === 'Home') { event.preventDefault(); move(0) }
    else if (event.key === 'End') { event.preventDefault(); move(visibleRows.length - 1) }
    else if (event.key === 'ArrowRight' && row.hasChildren && collapsed[row.section.key]) {
      event.preventDefault(); setCollapsed(current => ({ ...current, [row.section.key]: false }))
    } else if (event.key === 'ArrowLeft' && row.hasChildren && !collapsed[row.section.key]) {
      event.preventDefault(); setCollapsed(current => ({ ...current, [row.section.key]: true }))
    } else if (event.key === 'ArrowLeft' && row.parent && row.parent !== 'initial') {
      event.preventDefault()
      const parentIndex = visibleRows.findIndex(item => item.section.key === row.parent)
      move(parentIndex)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); selectSection(row.section.index)
    }
  }

  const sectionProblems = (item: EditorSection) => item.units.filter(unit => NEEDS_DECISION.has(stateOf(unit))).length
  const notes = (item: EditorSection) => new Set(item.units.filter(unit => unit.role === 'note').map(unit => unit.note)).size
  const totalUnits = flat.length
  const verifiedUnits = flat.filter(entry => entry.unit.state === 'verified').length
  const finalHere = hasFinal(section)
  const sectionDuration = section?.duration ?? (section && section.units.length
    ? offsets(section)[section.units.length - 1] + (section.units[section.units.length - 1].duration ?? 0) : 0)
  const currentPlaying = playing && playing.section === sectionIndex ? playing.unit : -1
  const anyAudio = flat.some(entry => entry.unit.hasAudio)
  const subchapterCount = sections.filter(item => item.level === 2).length
  const noteCount = sections.reduce((sum, item) => sum + notes(item), 0)

  if (loading && !model) {
    return <div className="ed-empty" role="status"><Loader2 size={22} className="ed-icon is-busy" aria-hidden="true"/><h2>Preparando la estructura del audiolibro…</h2><p>Leemos los capítulos, las páginas y las notas del libro.</p></div>
  }
  if (!model || !sections.length) {
    return <div className="ed-empty" role={error ? 'alert' : 'status'}><FileAudio size={24} aria-hidden="true"/><h2>{error ? 'No se pudo preparar la estructura' : 'Todavía no hay estructura que revisar'}</h2><p>{error || 'Sube un libro en el paso 1 para ver aquí sus archivos de audio, capítulos y páginas.'}</p></div>
  }

  return <div className="audiobook-editor" onKeyDown={onKeyDown}>
    <audio ref={audioRef} preload="metadata" onLoadedMetadata={onLoadedMetadata} onTimeUpdate={onTimeUpdate}
      onPlay={() => setIsPlaying(true)} onPause={() => setIsPlaying(false)} onEnded={onEnded}
      onError={() => { if (source.current) setNotice('No se pudo cargar el audio de esta parte.') }}
      className="visually-hidden" aria-hidden="true"/>

    <div className="ed-toolbar" role="toolbar" aria-label="Reproducción y navegación">
      <div className="ed-transport">
        <button type="button" className="ed-tool" onClick={() => stepSection(-1)} disabled={sectionIndex === 0} aria-label="Sección anterior" title="Sección anterior (Alt+↑)"><SkipBack size={17} aria-hidden="true"/></button>
        <button type="button" className="ed-tool" onClick={() => step(-1)} aria-label="Frase anterior" title="Frase anterior (Alt+←)"><ChevronLeft size={19} aria-hidden="true"/></button>
        <button type="button" className="ed-play" onClick={togglePlay} aria-label={isPlaying ? 'Pausar' : 'Reproducir'} aria-pressed={isPlaying} disabled={!anyAudio} title="Reproducir o pausar (Espacio)">
          {isPlaying ? <Pause size={20} aria-hidden="true"/> : <Play size={20} aria-hidden="true"/>}</button>
        <button type="button" className="ed-tool" onClick={() => step(1)} aria-label="Frase siguiente" title="Frase siguiente (Alt+→)"><ChevronRight size={19} aria-hidden="true"/></button>
        <button type="button" className="ed-tool" onClick={() => stepSection(1)} disabled={sectionIndex >= sections.length - 1} aria-label="Sección siguiente" title="Sección siguiente (Alt+↓)"><SkipForward size={17} aria-hidden="true"/></button>
      </div>
      <div className="ed-timeline">
        <div className="ed-time" aria-live="off"><span>{formatClock(sectionTime)}</span><span aria-hidden="true">/</span><span>{formatClock(sectionDuration)}</span></div>
        <input type="range" className="ed-seek" min={0} max={Math.max(0.1, sectionDuration || 0)} step={0.1}
          value={Math.min(sectionTime, sectionDuration || 0)} disabled={!anyAudio || !sectionDuration}
          onChange={event => seekSection(Number(event.target.value))}
          aria-label={`Posición en ${section?.title ?? 'la sección'}`} aria-valuetext={`${formatClock(sectionTime)} de ${formatClock(sectionDuration)}`}/>
        <span className={finalHere ? 'ed-source is-final' : 'ed-source'}>{finalHere ? 'Audio final' : anyAudio ? 'Audio por frases' : 'Sin audio'}</span>
      </div>
      <div className="ed-review-nav">
        <button type="button" className="ed-tool" onClick={() => goToProblem(-1)} disabled={!problems.length} aria-label="Problema anterior"><ChevronLeft size={17} aria-hidden="true"/></button>
        <span className={problems.length ? 'ed-problems is-open' : 'ed-problems'} aria-live="polite"><AlertTriangle size={15} aria-hidden="true"/>{problems.length ? `${problems.length} por revisar` : 'Nada por revisar'}</span>
        <button type="button" className="ed-tool" onClick={() => goToProblem(1)} disabled={!problems.length} aria-label="Problema siguiente" title="Problema siguiente (Alt+N)"><ChevronRight size={17} aria-hidden="true"/></button>
        <label className="ed-speed">Velocidad<select value={speed} onChange={event => setSpeed(Number(event.target.value))}>
          {[0.75, 1, 1.25, 1.5, 1.75, 2].map(value => <option key={value} value={value}>{String(value).replace('.', ',')}×</option>)}</select></label>
      </div>
      {props.model?.mode === 'outline' && <div className="ed-produce">
        <button type="button" className="ed-produce-button" onClick={props.onProduce} disabled={!props.canProduce || props.producing}>
          {props.producing ? <Loader2 size={17} className="ed-icon is-busy" aria-hidden="true"/> : <FileAudio size={17} aria-hidden="true"/>}
          {props.producing ? 'Iniciando…' : 'Producir el audiolibro con revisión'}</button>
      </div>}
      {props.model?.mode === 'job' && props.progress && props.progress.total > 0 && props.progress.done < props.progress.total && <div className="ed-progress" role="status">
        <progress max={props.progress.total} value={props.progress.done} aria-label="Avance de la producción"/>
        <span>{props.progress.done} de {props.progress.total} frases</span>
      </div>}
    </div>
    {(notice || props.message) && <p className="ed-notice" role="status" aria-live="polite">{notice || props.message}</p>}

    <div className="ed-body">
      <nav className="ed-tree-panel" aria-label="Estructura del audiolibro">
        <div className="ed-panel-head">
          <h2>Estructura</h2>
          <p>{sections.length} archivos · {model.pages} páginas{model.totalDuration ? ` · ${formatClock(model.totalDuration)}` : ''}</p>
          <label className="ed-search">
            <span className="visually-hidden">Buscar en el libro</span>
            <input type="search" value={query} onChange={event => setQuery(event.target.value)}
              placeholder="Buscar en el libro" aria-describedby="ed-search-count"/>
          </label>
          {normalizedQuery && <p id="ed-search-count" role="status" aria-live="polite">{matches.length
            ? `${matches.length} frase${matches.length > 1 ? 's' : ''} con «${query.trim()}»` : 'Ninguna frase lo contiene'}</p>}
        </div>
        {normalizedQuery ? <ul className="ed-results" aria-label="Resultados de la búsqueda">
          {matches.slice(0, 60).map(entry => <li key={entry.unit.key}><button type="button" onClick={() => {
            setSelected({ section: entry.s, unit: entry.u }); setTreeFocus(entry.s); props.onHidePlanilla?.()
            focusUnit({ section: entry.s, unit: entry.u })
          }}><strong>Archivo {sections[entry.s].fileNumber} · {sections[entry.s].title} · frase {entry.u + 1}</strong>
            <span>{entry.unit.text}</span></button></li>)}
        </ul> : <div className="ed-tree" role="tree" aria-label="Archivos, capítulos y subcapítulos" ref={treeRef}>
          {hasInitial && <div className="ed-tree-group" role="group" aria-label="Archivos iniciales">
            <button type="button" className="ed-group-toggle" aria-expanded={!collapsed.initial}
              onClick={() => setCollapsed(current => ({ ...current, initial: !current.initial }))}>
              {collapsed.initial ? <ChevronRight size={15} aria-hidden="true"/> : <ChevronDown size={15} aria-hidden="true"/>}Archivos iniciales <small>ONCE</small></button>
          </div>}
          {visibleRows.map((row, position) => {
            const item = row.section
            const active = item.index === sectionIndex && !props.showPlanilla
            const issues = sectionProblems(item)
            const focusable = item.index === treeFocus || (!visibleRows.some(other => other.section.index === treeFocus) && position === 0)
            return <div key={item.key} id={`ed-tree-${item.index}`} role="treeitem" aria-level={row.depth}
              aria-selected={active} aria-expanded={row.hasChildren ? !collapsed[item.key] : undefined}
              tabIndex={focusable ? 0 : -1} className={`ed-tree-row depth-${row.depth}${active ? ' is-active' : ''}${issues ? ' has-issues' : ''}${playing?.section === item.index && isPlaying ? ' is-playing' : ''}`}
              onClick={() => selectSection(item.index)} onKeyDown={event => onTreeKey(event, row, position)}
              onFocus={() => setTreeFocus(item.index)}>
              {row.hasChildren ? <button type="button" tabIndex={-1} className="ed-twisty" aria-hidden="true"
                onClick={event => { event.stopPropagation(); setCollapsed(current => ({ ...current, [item.key]: !current[item.key] })) }}>
                {collapsed[item.key] ? <ChevronRight size={14}/> : <ChevronDown size={14}/>}</button> : <span className="ed-twisty-space" aria-hidden="true"/>}
              <span className="ed-file-number" aria-label={`Archivo ${item.fileNumber}`}>{item.fileNumber}</span>
              <span className="ed-tree-text">
                <strong title={item.title}>{item.title}</strong>
                <small>Nivel {item.level} · {pageRange(item)} · {item.duration ? formatClock(item.duration) : 'sin audio'}{notes(item) ? ` · ${notes(item)} nota${notes(item) > 1 ? 's' : ''}` : ''}</small>
              </span>
              <span className="ed-tree-status" title={STATE_LABEL[item.status] ?? item.status}>
                <StatusIcon status={item.status}/>{issues ? <b>{issues}</b> : null}
                <span className="visually-hidden">{STATE_LABEL[item.status] ?? item.status}{issues ? `, ${issues} por revisar` : ''}</span>
              </span>
            </div>
          })}
        </div>}
      </nav>

      <section className="ed-workspace" aria-label="Revisión de la sección">
        {props.showPlanilla && props.planillaPanel ? props.planillaPanel : section && <>
          <header className="ed-section-head">
            <div>
              <p className="ed-kicker">Archivo {section.fileNumber} · Nivel {section.level} · {KIND_LABEL[section.kind] ?? section.kind}</p>
              <h2 id="ed-section-title">{section.title}</h2>
              <div className="ed-meta">
                <span>{section.pageStart ? `Páginas ${section.pageStart}${section.pageEnd && section.pageEnd !== section.pageStart ? `–${section.pageEnd}` : ''}` : 'Sin número de página'}</span>
                <span>{section.duration ? `${formatClock(section.duration)} ${section.durationFinal ? '(audio final)' : '(sin pulir)'}` : 'Sin audio todavía'}</span>
                <span className={`ed-state state-${section.status}`}><StatusIcon status={section.status} size={13}/>{STATE_LABEL[section.status] ?? section.status}</span>
                {!section.headingSpoken && <span className="ed-generated" title="El libro no dice este título: solo sirve para navegar.">Título de navegación, no se narra</span>}
              </div>
            </div>
            <div className="ed-section-actions">
              <button type="button" className="ed-secondary" onClick={() => playAt({ section: sectionIndex, unit: 0 })} disabled={!section.units.some(unit => unit.hasAudio) && !finalHere}><Play size={15} aria-hidden="true"/>Escuchar desde el principio</button>
              {INITIAL_KINDS.has(section.kind) && props.onShowPlanilla && <button type="button" className="ed-secondary" onClick={props.onShowPlanilla}><Pencil size={15} aria-hidden="true"/>Editar planilla</button>}
            </div>
          </header>
          <ol className="ed-phrases" ref={listRef} aria-labelledby="ed-section-title">
            {section.units.map((unit, position) => {
              const state = stateOf(unit)
              const isCurrent = position === currentPlaying
              const isSelected = position === selected.unit && selected.section === sectionIndex
              const editable = props.model?.mode === 'outline' && Boolean(unit.source) && unit.editable !== false
              const paragraphText = unit.source ? props.bookChapters[unit.source.chapter]?.paragraphs[unit.source.paragraph] : undefined
              const start = finalHere ? unit.clip?.[0] : offsets(section)[position]
              return <li key={unit.key} className={`ed-phrase-item${unit.pageBreaks.length ? ' has-page' : ''}`}>
                {unit.pageBreaks.map(page => <div key={page} className="ed-page-mark" role="separator" aria-label={`Comienza la página ${page}`}><span>Página {page}</span></div>)}
                <div id={`ed-unit-${sectionIndex}-${position}`} className={`ed-phrase${isCurrent ? ' is-current' : ''}${isSelected ? ' is-selected' : ''} state-${state}`}
                  aria-current={isCurrent ? 'true' : undefined}>
                  <button type="button" className="ed-phrase-play" onClick={() => playAt({ section: sectionIndex, unit: position })}
                    disabled={!unit.hasAudio && !finalHere} aria-label={`Escuchar la frase ${position + 1}`}><Play size={14} aria-hidden="true"/></button>
                  <button type="button" className="ed-phrase-main" onClick={() => setSelected({ section: sectionIndex, unit: position })} aria-pressed={isSelected}>
                    <span className="ed-phrase-meta">
                      <span className="ed-phrase-number">{position + 1}</span>
                      {unit.hasAudio || finalHere ? <span>{formatClock(start)}</span> : null}
                      {unit.duration ? <span>{formatClock(unit.duration)}</span> : null}
                      {(ROLE_LABEL[unit.role] || unit.role === 'note') && <span className={`ed-role role-${unit.role}`}>{unit.role === 'note' ? `Nota ${unit.note ?? ''}`.trim() : ROLE_LABEL[unit.role]}</span>}
                      {state !== 'planned' && <span className={`ed-state state-${state}`}><StatusIcon status={state} size={12}/>{STATE_LABEL[state] ?? state}</span>}
                      {unit.issues ? <span className="ed-issue-count">{unit.issues} diferencia{unit.issues > 1 ? 's' : ''}</span> : null}
                    </span>
                    {editing === unit.key && editable ? null : <span className="ed-phrase-text">{unit.text}</span>}
                  </button>
                  {editable && editing !== unit.key && <button type="button" className="ed-phrase-edit" onClick={() => setEditing(unit.key)} aria-label={`Corregir el texto de la frase ${position + 1}`}><Pencil size={14} aria-hidden="true"/></button>}
                </div>
                {editing === unit.key && editable && unit.source && <div className="ed-edit">
                  <label htmlFor={`book-paragraph-${unit.source.chapter}-${unit.source.paragraph}`}>Texto del párrafo (se narrará así)</label>
                  <textarea id={`book-paragraph-${unit.source.chapter}-${unit.source.paragraph}`} rows={Math.min(8, Math.max(3, Math.ceil((paragraphText ?? unit.text).length / 90)))}
                    value={paragraphText ?? unit.text} onChange={event => props.onEditParagraph(unit.source!.chapter, unit.source!.paragraph, event.target.value)}/>
                  <div className="ed-edit-actions"><button type="button" className="ed-secondary" onClick={finishEditing}>Listo</button></div>
                </div>}
              </li>
            })}
          </ol>
        </>}
      </section>

      <aside className="ed-inspector" aria-label="Detalle de la frase">
        {props.model?.mode === 'job' && selectedUnit && section
          ? props.renderInspector(selectedUnit, section, seconds => playAt({ section: sectionIndex, unit: selected.unit }, Math.max(0, seconds - 0.8)))
          : props.idlePanel}
      </aside>
    </div>

    <div className="ed-statusbar" role="group" aria-label="Estado del audiolibro">
      <span>{model.audioFormat ? `${formatLabel(model.audioFormat)}${model.audioFormatFinal ? '' : ' (por frase)'}` : 'Formato de audio: sin audio todavía'}</span>
      <span>{sections.length} archivos · {subchapterCount} {subchapterCount === 1 ? 'subcapítulo' : 'subcapítulos'} · {model.pages} {model.pages === 1 ? 'página' : 'páginas'} · {noteCount} {noteCount === 1 ? 'nota' : 'notas'}</span>
      <span>{model.mode === 'job' ? `${verifiedUnits} de ${totalUnits} frases correctas` : `${totalUnits} frases por narrar`}</span>
      <span title="Cada frase de la lista es una unidad narrada: el texto se sigue frase a frase, no palabra a palabra.">Sincronización por frase</span>
      {model.once && <span className="ed-once-badge">Estructura ONCE</span>}
      {props.statusExtra}
    </div>
  </div>
}
