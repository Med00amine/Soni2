import type { ReactNode } from 'react'
import { AlertTriangle, Check, CheckCircle2, Circle, Download, Loader2, MinusCircle, RefreshCw, XCircle } from 'lucide-react'
import { formatSeconds, type BuildStage } from './exportView'
import './export-workspace.css'

export type ExportTone = 'idle' | 'busy' | 'ready' | 'stale' | 'review' | 'error'

// Step 3's header: the book, where its ZIP stands, and the only two actions
// the step has. Everything else on the step is information or a contextual
// control inside it.
export function ExportHeader({ title, tone, status, downloadLabel, downloadHint, regenerateHint, canDownload,
  canRegenerate, busy, downloading, regenerating, onDownload, onRegenerate, notice }: {
  title: string
  tone: ExportTone
  status: string
  downloadLabel: string
  downloadHint: string
  regenerateHint: string
  canDownload: boolean
  canRegenerate: boolean
  // A ZIP is being built (by either action, or by step 2's «Producir»): both
  // actions wait, so the same book is never built twice at once.
  busy: boolean
  downloading: boolean
  regenerating: boolean
  onDownload: () => void
  onRegenerate: () => void
  notice?: { text: string; tone: 'info' | 'error'; href?: string } | null
}) {
  return <header className="xp-head">
    <div className="xp-title">
      <p className="xp-kicker">Paso 3 · Exportar</p>
      <h2 id="export-title">{title}</h2>
      <p className={`xp-status is-${tone}`} role="status" aria-live="polite">
        {tone === 'busy' ? <Loader2 size={16} className="xp-spin" aria-hidden="true"/>
          : tone === 'ready' ? <CheckCircle2 size={16} aria-hidden="true"/>
            : tone === 'error' ? <XCircle size={16} aria-hidden="true"/>
              : tone === 'idle' ? <Circle size={16} aria-hidden="true"/> : <AlertTriangle size={16} aria-hidden="true"/>}
        <span>{status}</span>
      </p>
    </div>
    <div className="xp-actions" role="group" aria-label="Acciones del ZIP">
      <button type="button" className="xp-primary" data-primary-action="download" onClick={onDownload}
        disabled={!canDownload || busy} aria-busy={downloading} title={downloadHint}>
        {downloading ? <Loader2 size={18} className="xp-spin" aria-hidden="true"/> : <Download size={18} aria-hidden="true"/>}
        <span>{downloadLabel}</span>
      </button>
      <button type="button" className="xp-primary is-outline" data-primary-action="regenerate" onClick={onRegenerate}
        disabled={!canRegenerate || busy} aria-busy={regenerating} title={regenerateHint}>
        {regenerating ? <Loader2 size={18} className="xp-spin" aria-hidden="true"/> : <RefreshCw size={18} aria-hidden="true"/>}
        <span>Regenerar ZIP</span>
      </button>
    </div>
    {notice && <p className={`xp-notice is-${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
      {notice.text}{notice.href && <> <a href={notice.href} download>Si la descarga no empieza, pulsa aquí.</a></>}
    </p>}
  </header>
}

function StageIcon({ state }: { state: BuildStage['state'] }) {
  if (state === 'done') return <Check size={15} aria-hidden="true"/>
  if (state === 'active') return <Loader2 size={15} className="xp-spin" aria-hidden="true"/>
  if (state === 'failed') return <XCircle size={15} aria-hidden="true"/>
  if (state === 'skipped') return <MinusCircle size={15} aria-hidden="true"/>
  return <Circle size={15} aria-hidden="true"/>
}

const STATE_TEXT: Record<BuildStage['state'], string> = {
  done: 'hecho', active: 'en curso', failed: 'detenido aquí', skipped: 'no hacía falta', pending: 'pendiente',
}

// The real stages of a build, each with what it did and how long it took.
export function BuildStages({ stages, label }: { stages: BuildStage[]; label: string }) {
  return <ol className="xp-stages" aria-label={label}>
    {stages.map(stage => <li key={stage.id} className={`xp-stage is-${stage.state}`}
      aria-current={stage.state === 'active' ? 'step' : undefined}>
      <span className="xp-stage-icon"><StageIcon state={stage.state}/></span>
      <span className="xp-stage-body">
        <span className="xp-stage-label">{stage.label}<span className="visually-hidden">: {STATE_TEXT[stage.state]}</span></span>
        {stage.detail && <span className="xp-stage-detail">{stage.detail}</span>}
        {stage.progress && <progress max={Math.max(1, stage.progress.max)} value={stage.progress.value}
          aria-label={`${stage.label}: ${stage.progress.value} de ${stage.progress.max}`}/>}
      </span>
      {stage.seconds != null && stage.state !== 'pending' && <span className="xp-stage-time">{formatSeconds(stage.seconds)}</span>}
    </li>)}
  </ol>
}

// A flat block of the step: a heading and its content, no card around it.
export function ExportSection({ title, id, children, aside }: { title: string; id: string; children: ReactNode; aside?: ReactNode }) {
  return <section className="xp-section" aria-labelledby={id}>
    <div className="xp-section-head"><h3 id={id}>{title}</h3>{aside}</div>
    {children}
  </section>
}

export function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return <dl className="xp-facts">
    {rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
  </dl>
}
