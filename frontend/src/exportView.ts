// What step 3 shows about the ZIP, worked out from what the server reports.
// Nothing here is estimated: every stage, count and time comes from the job's
// pipeline (backend PipelineRun) or the direct download's progress.

export type StageId = 'prepare' | 'check' | 'narrate' | 'chapters' | 'package' | 'validate' | 'zip' | 'ready'
export type StageState = 'pending' | 'active' | 'done' | 'skipped' | 'failed'
export type BuildStage = {
  id: StageId
  label: string
  state: StageState
  detail?: string
  seconds?: number
  progress?: { value: number; max: number }
}

export type PipelineUnits = { total?: number; reused?: number; to_narrate?: number; narrated?: number; already_done?: number }
export type PipelineChapters = { total?: number; done?: number; reused?: number; rebuilt?: number }
export type PipelineView = {
  stage?: string | null
  running?: boolean
  outcome?: string
  units?: PipelineUnits
  chapters?: PipelineChapters
  timings?: Record<string, number>
  total_seconds?: number
  started_at?: string
  finished_at?: string
}
export type PackageView = {
  bytes?: number
  built_at?: string | null
  audit?: string
  audit_detail?: string
  audio_files?: number | null
  chapter_files?: number | null
  total_time?: string | null
  sections?: number | null
}
export type DirectProgress = {
  state?: string
  stage?: string
  total?: number
  done?: number
  reused?: number
  chapters_done?: number
  chapters_total?: number
  chapters_reused?: number
  chapters_rebuilt?: number
  timings?: Record<string, number>
}

export const STAGES: { id: StageId; label: string }[] = [
  { id: 'prepare', label: 'Preparando el audiolibro' },
  { id: 'check', label: 'Comprobando qué ha cambiado' },
  { id: 'narrate', label: 'Generando el audio que falta' },
  { id: 'chapters', label: 'Montando los capítulos' },
  { id: 'package', label: 'Creando la estructura DAISY' },
  { id: 'validate', label: 'Validando el paquete' },
  { id: 'zip', label: 'Creando y comprobando el ZIP' },
  { id: 'ready', label: 'Listo para descargar' },
]

// The server's stage names (backend PipelineRun and the direct download) in step 3's order.
const JOB_STAGE: Record<string, StageId> = {
  queued: 'prepare', checking: 'check', narrating: 'narrate', chapters: 'chapters', package: 'package',
  validating: 'validate', zipping: 'zip', verifying: 'zip', completed: 'ready',
}
const TASK_STAGE: Record<string, StageId> = {
  checking: 'check', narrating: 'narrate', assembling: 'chapters', packaging: 'package', validating: 'validate',
  zipping: 'zip', verifying: 'zip', done: 'ready',
}

const order = (id: StageId) => STAGES.findIndex(stage => stage.id === id)

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`
}

function secondsFor(timings: Record<string, number> | undefined, names: Record<string, StageId>, id: StageId) {
  if (!timings) return undefined
  const values = Object.entries(timings).filter(([name]) => names[name] === id).map(([, seconds]) => seconds)
  return values.length ? values.reduce((sum, value) => sum + value, 0) : undefined
}

// A reviewed production (a job): its live pipeline, or the last build's record.
export function jobStages(jobState: string, pipeline?: PipelineView | null): BuildStage[] {
  const running = ['queued', 'running'].includes(jobState)
  const raw = pipeline?.stage ?? (running ? 'queued' : jobState === 'completed' ? 'completed' : '')
  const outcome = pipeline?.outcome ?? (running ? '' : jobState)
  const timings = pipeline?.timings ?? {}
  const units = pipeline?.units ?? {}
  const chapters = pipeline?.chapters ?? {}
  // Where it is now, or where it stopped.
  let current: StageId = JOB_STAGE[raw] ?? 'prepare'
  if (!running) {
    const reached = Object.keys(timings).map(name => JOB_STAGE[name]).filter(Boolean) as StageId[]
    const last = reached.sort((left, right) => order(left) - order(right)).pop()
    current = outcome === 'completed' || jobState === 'completed' ? 'ready'
      : outcome === 'review' || outcome === 'partial' ? 'narrate'
        : last ?? current
  }
  const failed = !running && ['failed', 'cancelled', 'partial', 'review'].includes(outcome) && current !== 'ready'
  return STAGES.map(({ id, label }) => {
    const position = order(id)
    let state: StageState = position < order(current) ? 'done' : position === order(current)
      ? (running ? 'active' : failed ? 'failed' : 'done') : 'pending'
    const stage: BuildStage = { id, label, state, seconds: secondsFor(timings, JOB_STAGE, id) }
    if (id === 'prepare' && state !== 'pending') stage.seconds = undefined
    if (id === 'check' && units.total) {
      const known = (units.reused ?? 0) + (units.already_done ?? 0)
      stage.detail = state === 'pending' ? undefined
        : `${known} de ${plural(units.total, 'frase', 'frases')} sin cambios: su audio y su revisión se reutilizan`
    }
    if (id === 'narrate' && units.total) {
      const work = units.to_narrate ?? 0
      if (state !== 'pending' && !work && state !== 'active') {
        state = 'skipped'
        stage.detail = 'Nada que generar: todo el audio ya estaba hecho y revisado'
      } else if (work) {
        stage.detail = `${units.narrated ?? 0} de ${plural(work, 'frase nueva o corregida', 'frases nuevas o corregidas')}`
        if (state === 'active') stage.progress = { value: units.narrated ?? 0, max: work }
      }
      if (outcome === 'review') stage.detail = 'Hay frases que necesitan tu decisión'
      stage.state = state
    }
    if (id === 'chapters' && chapters.total && state !== 'pending') {
      const parts = [`${chapters.done ?? 0} de ${chapters.total}`]
      if (chapters.reused) parts.push(`${chapters.reused} sin cambios, reutilizados`)
      if (chapters.rebuilt) parts.push(`${chapters.rebuilt} montados de nuevo`)
      stage.detail = parts.join(' · ')
      if (state === 'active') stage.progress = { value: chapters.done ?? 0, max: chapters.total }
    }
    return stage
  })
}

// A direct download (a package task).
export function directStages(progress?: DirectProgress | null): BuildStage[] {
  const running = progress?.state === 'running'
  const raw = progress?.stage ?? 'checking'
  const failed = progress?.state === 'failed' || progress?.state === 'cancelled'
  const current: StageId = progress?.state === 'completed' ? 'ready' : TASK_STAGE[raw] ?? 'prepare'
  const total = progress?.total ?? 0
  const narrated = Math.max(0, (progress?.done ?? 0) - (progress?.reused ?? 0))
  return STAGES.map(({ id, label }) => {
    const position = order(id)
    let state: StageState = position < order(current) ? 'done' : position === order(current)
      ? (running ? 'active' : failed ? 'failed' : 'done') : 'pending'
    const stage: BuildStage = { id, label, state, seconds: secondsFor(progress?.timings, TASK_STAGE, id) }
    if (id === 'narrate' && total && state !== 'pending') {
      stage.detail = `${progress?.done ?? 0} de ${plural(total, 'párrafo', 'párrafos')}`
        + (progress?.reused ? ` · ${progress.reused} ya narrados, sin coste` : '')
      if (state === 'active') stage.progress = { value: progress?.done ?? 0, max: total }
      else if (!narrated && progress?.reused === total) {
        state = 'skipped'
        stage.detail = 'Nada que generar: todo el audio ya estaba hecho'
      }
      stage.state = state
    }
    if (id === 'chapters' && progress?.chapters_total && state !== 'pending') {
      const parts = [`${progress.chapters_done ?? 0} de ${progress.chapters_total}`]
      if (progress.chapters_reused) parts.push(`${progress.chapters_reused} sin cambios, reutilizados`)
      if (progress.chapters_rebuilt) parts.push(`${progress.chapters_rebuilt} montados de nuevo`)
      stage.detail = parts.join(' · ')
      if (state === 'active') stage.progress = { value: progress.chapters_done ?? 0, max: progress.chapters_total }
    }
    return stage
  })
}

export function formatSeconds(seconds?: number) {
  if (seconds == null || !Number.isFinite(seconds)) return ''
  if (seconds < 1) return '<1 s'
  if (seconds < 60) return `${Math.round(seconds)} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} min ${String(Math.round(seconds % 60)).padStart(2, '0')} s`
}

export function formatBytes(bytes?: number) {
  if (!bytes) return ''
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toLocaleString('es-ES', { maximumFractionDigits: 1 })} MB`
}

// "1:02:03.456" -> "1:02:03"
export function formatTotalTime(value?: string | null) {
  if (!value) return ''
  return value.replace(/\.\d+$/, '')
}

// What changed in the book since the ZIP was built: sections by title, and
// phrases by text within the sections both have. Exact for what it counts; a
// corrected phrase counts as one new and one removed.
export type BookChanges = { removedSections: string[]; addedSections: string[]; newPhrases: number; removedPhrases: number }
type Comparable = { title: string; units: { text: string }[] }

export function changesSince(produced: Comparable[], current: Comparable[]): BookChanges {
  const key = (title: string) => title.trim().toLocaleLowerCase('es')
  const before = new Map(produced.map(section => [key(section.title), section]))
  const after = new Map(current.map(section => [key(section.title), section]))
  const changes: BookChanges = { removedSections: [], addedSections: [], newPhrases: 0, removedPhrases: 0 }
  for (const [name, section] of before) if (!after.has(name)) changes.removedSections.push(section.title)
  for (const [name, section] of after) {
    const old = before.get(name)
    if (!old) {
      changes.addedSections.push(section.title)
      continue
    }
    const counts = new Map<string, number>()
    for (const unit of old.units) counts.set(unit.text, (counts.get(unit.text) ?? 0) + 1)
    for (const unit of section.units) {
      const left = counts.get(unit.text) ?? 0
      if (left > 0) counts.set(unit.text, left - 1)
      else changes.newPhrases += 1
    }
    for (const left of counts.values()) changes.removedPhrases += left
  }
  return changes
}

export function describeChanges(changes: BookChanges) {
  const parts: string[] = []
  if (changes.removedSections.length) {
    parts.push(`${plural(changes.removedSections.length, 'capítulo eliminado', 'capítulos eliminados')} (${changes.removedSections.slice(0, 3).map(title => `«${title}»`).join(', ')}${changes.removedSections.length > 3 ? '…' : ''})`)
  }
  if (changes.addedSections.length) {
    parts.push(`${plural(changes.addedSections.length, 'capítulo añadido', 'capítulos añadidos')} (${changes.addedSections.slice(0, 3).map(title => `«${title}»`).join(', ')}${changes.addedSections.length > 3 ? '…' : ''})`)
  }
  if (changes.newPhrases) parts.push(plural(changes.newPhrases, 'frase nueva o corregida', 'frases nuevas o corregidas'))
  if (changes.removedPhrases) parts.push(plural(changes.removedPhrases, 'frase quitada', 'frases quitadas'))
  return parts.join(' · ')
}
