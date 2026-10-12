import { useEffect, useRef, useState } from 'react'
import { BookOpen, Upload, Download, Volume2, CheckCircle2 } from 'lucide-react'
import './production-status.css'

const API = (import.meta.env.VITE_API_URL ?? '').replace(/\/+$/, '')

type Capabilities = {
  daisy_profile: string
  tts_provider: string
  tts_configured: boolean
  voice: string
  elevenlabs_available: boolean
  voice_speed_control: boolean
  voice_id_configurable: boolean
  audio_review_configured: boolean
  chapter_audio: boolean
  chapter_smil: boolean
  full_text_in_package: boolean
  ncx_navigation: boolean
  ncc_navigation: boolean
  once_intro: boolean
  once_bibliographic_audio: boolean
  automatic_audio_review: boolean
  retakes: boolean
}

type TimedPhrase = { text: string; start: number; end: number; text_start: number; text_end: number }
type BookChapter = { title: string; paragraphs: string[] }
type BookMetadata = { title: string; author: string; edition: string; publication_year: string; publisher: string }
type VoiceOption = { voice_id: string; name: string; labels?: Record<string, string> }
type AudioReview = { recognized_text: string; issues: { kind: string; expected: string; recognized: string }[]; review_note: string }

function isSpanishFromSpain(voice: VoiceOption) {
  const details = [voice.name, ...Object.values(voice.labels ?? {})].join(' ').toLocaleLowerCase()
  return /\b(spain|españa|castilian|castellano|madrid)\b/.test(details)
}

export default function App() {
  const input = useRef<HTMLInputElement>(null)
  const playerRef = useRef<HTMLAudioElement>(null)
  const outlineRef = useRef<HTMLDetailsElement>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [audio, setAudio] = useState('')
  const [transcript, setTranscript] = useState('')
  const [bookChapters, setBookChapters] = useState<BookChapter[]>([])
  const [sourceBookChapters, setSourceBookChapters] = useState<BookChapter[]>([])
  const [originalBookChapters, setOriginalBookChapters] = useState<BookChapter[]>([])
  const [hasManualSections, setHasManualSections] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [book, setBook] = useState('')
  const [bookMetadata, setBookMetadata] = useState<BookMetadata>({ title: '', author: '', edition: '', publication_year: '', publisher: '' })
  const [selectedFile, setSelectedFile] = useState<File>()
  const [packageBusy, setPackageBusy] = useState(false)
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
          const voiceData = await voiceResponse.json().catch(() => ({})) as { voices?: VoiceOption[]; detail?: string }
          if (!voiceResponse.ok) throw new Error(voiceData.detail || `HTTP ${voiceResponse.status}`)
          const availableVoices = voiceData.voices ?? []
          const regionalVoices = availableVoices.filter(isSpanishFromSpain).sort((left, right) => {
            const leftFemale = Object.entries(left.labels ?? {}).some(([key, value]) => key.toLowerCase() === 'gender' && value.toLowerCase() === 'female')
            const rightFemale = Object.entries(right.labels ?? {}).some(([key, value]) => key.toLowerCase() === 'gender' && value.toLowerCase() === 'female')
            return Number(rightFemale) - Number(leftFemale)
          })
          const orderedVoices = [...regionalVoices, ...availableVoices.filter(voice => !isSpanishFromSpain(voice))]
          setVoices(orderedVoices)
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

  useEffect(() => {
    if (playerRef.current) playerRef.current.playbackRate = playbackRate
  }, [audio, playbackRate])

  function selectBook(file?: File) {
    if (!file) return
    setSelectedFile(file)
    setAudio(''); setTranscript(''); setBookChapters([]); setSourceBookChapters([]); setOriginalBookChapters([]); setHasManualSections(false); setSearchQuery(''); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setBookMetadata({ title: '', author: '', edition: '', publication_year: '', publisher: '' }); setBook(file.name); setMessage('Libro cargado. Elige una voz y pulsa «Generar vista previa».')
  }

  async function generatePreview() {
    const file = selectedFile
    if (!file || busy) return
    setAudio(''); setTranscript(''); setAlignment([]); setCurrentPhrase(-1); setReview(undefined); setBusy(true); setMessage('Estamos preparando una muestra de tu libro.')
    const form = new FormData(); form.append('file', file); form.append('voice_id', voiceId)
    try {
      const response = await fetch(`${API}/api/books/preview`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No hemos podido leer este documento. Prueba con otro archivo.')
      }
      const result: { audio_base64: string; audio_mime_type: string; transcript: string; alignment?: TimedPhrase[] | null; audio_error?: string | null; audio_notice?: string | null; chapters?: BookChapter[]; title?: string; author?: string; edition?: string; publication_year?: string; publisher?: string } = await response.json()
      const binary = atob(result.audio_base64)
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0))
      const blob = new Blob([bytes], { type: result.audio_mime_type })
      if (result.audio_base64) {
        setAudio(URL.createObjectURL(blob))
        setMessage(result.audio_notice || 'La narracion esta lista. Pulsa reproducir para escucharla.')
      } else {
        setAudio('')
        setMessage(result.audio_error || 'El texto se ha extraido, pero no hay una voz configurada.')
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
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Se ha producido un error. Inténtalo de nuevo.')
    } finally { setBusy(false) }
  }

  async function downloadFullBook() {
    if (!selectedFile || packageBusy) return
    setPackageBusy(true)
    setMessage('Preparando el libro completo. Este proceso puede tardar varios minutos.')
    const form = new FormData()
    form.append('file', selectedFile)
    form.append('voice_id', voiceId)
    form.append('metadata_json', JSON.stringify(bookMetadata))
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
    try {
      const response = await fetch(`${API}/api/books/package`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No hemos podido crear el paquete del libro.')
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${book.replace(/\.[^.]+$/, '') || 'libro'}-daisy.zip`
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setMessage('El paquete completo del libro se ha descargado.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'No se ha podido descargar el libro completo.')
    } finally {
      setPackageBusy(false)
    }
  }

  function trackPlayback() {
    const time = playerRef.current?.currentTime ?? 0
    const phrase = alignment.findIndex(item => time >= item.start && time < item.end)
    setCurrentPhrase(phrase)
  }

  const alignedParagraphs: TimedPhrase[][] = []
  for (let index = 0; index < alignment.length; index += 3) {
    alignedParagraphs.push(alignment.slice(index, index + 3))
  }
  const canGenerateAudio = Boolean(capabilities?.tts_configured || (capabilities?.voice_id_configurable && voiceId))
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
    if (outlineRef.current) outlineRef.current.open = true
    window.requestAnimationFrame(() => {
      const paragraph = document.getElementById(`book-paragraph-${chapterIndex}-${paragraphIndex}`)
      paragraph?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      paragraph?.focus({ preventScroll: true })
    })
  }

  async function reviewPreview() {
    if (!audio || !transcript || reviewBusy) return
    setReviewBusy(true)
      setMessage('Enviando esta vista previa a ElevenLabs Scribe para compararla con el texto…')
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

  return <main className="shell">
    <a className="skip" href="#main">Saltar al contenido principal</a>
    <header className="top"><a className="brand" href="/" aria-label="Inicio de Soni2"><img className="brand-logo" src="/soni2-logo.svg" alt="Soni2" width="200" height="42"/></a><span className="top-note">Una forma más tranquila de disfrutar los libros</span></header>
    <section id="main" className="hero workspace" aria-labelledby="title">
      <div className="eyebrow"><span className="eyebrow-dot"/>TU LIBRO, EN VOZ ALTA</div>
      <h1 id="title">Escucha tu libro.</h1>
      <p className="intro">Sube un libro, escucha una muestra, revisa el texto y descarga el audiolibro completo.</p>
      <nav className="workflow-nav" aria-label="Pasos para crear el audiolibro"><ol><li><a href="#upload-section"><span>1</span><span>Sube el libro</span></a></li><li>{audio ? <a href="#listen-section"><span>2</span><span>Escucha y revisa</span></a> : <span aria-disabled="true"><span>2</span><span>Escucha y revisa</span></span>}</li><li>{bookChapters.length ? <a href="#book-helper"><span>3</span><span>Corrige y descarga</span></a> : <span aria-disabled="true"><span>3</span><span>Corrige y descarga</span></span>}</li></ol></nav>
      <section id="upload-section" className="upload-card workflow-card" aria-labelledby="upload-title">
        <div className="card-heading"><div className="book-icon"><BookOpen size={23}/></div><div><h2 id="upload-title">Añade tu libro</h2><p>PDF, EPUB, Word, texto o XML</p></div></div>
        <button className="choose" type="button" onClick={() => input.current?.click()} disabled={busy}>
          {busy ? <span className="spinner" aria-hidden="true"/> : <Upload size={19} aria-hidden="true"/>}
          {busy ? 'Preparando la narración…' : 'Elegir un libro'}
        </button>
        <input ref={input} className="visually-hidden" type="file" accept=".pdf,.epub,.docx,.txt,.xml" aria-label="Elige un libro en formato PDF, EPUB, Word, texto o XML" onChange={e => { const file = e.target.files?.[0]; e.currentTarget.value = ''; selectBook(file) }}/>
        {selectedFile && <p className="selected-file"><BookOpen size={17} aria-hidden="true"/><span>{book}</span></p>}
        {selectedFile && !canGenerateAudio && <p className="audio-setup-note" role="status">No hay una voz configurada para generar la vista previa. Revisa la configuración del servicio de narración.</p>}
        {capabilityError && <div className="audio-setup-note" role="alert">No se pudo conectar con el servicio ({capabilityError}). <button type="button" onClick={() => void refreshCapabilities()}>Reintentar</button></div>}
        <p className="privacy"><span aria-hidden="true">⌑</span> Tu libro se trata con cuidado y privacidad.</p>
        <div className="supported"><span>PDF</span><span>EPUB</span><span>DOCX</span><span>TXT</span><span>XML</span></div>
        {capabilities?.tts_configured && <p className="sync-note">Voz activa: {capabilities.tts_provider === 'deepgram' && capabilities.voice === 'aura-2-celeste-es' ? 'Celeste · Deepgram (español)' : capabilities.tts_provider === 'elevenlabs' ? voices.find(voice => voice.voice_id === capabilities.voice)?.name ?? capabilities.voice : capabilities.voice}</p>}
        {(capabilities?.voice_id_configurable || voiceLoadError) && <div className="voice-controls" role="group" aria-label="Configuración de voz e idioma">
        {capabilities?.voice_id_configurable && <label htmlFor="voice-id">Voz de narración · Español (España)<select id="voice-id" className="voice-id-input" value={voiceId} onChange={event => setVoiceId(event.target.value)} disabled={voicesLoading}><option value="">{voicesLoading ? 'Cargando voces…' : 'Usar la voz configurada'}</option>{voices.map(voice => <option key={voice.voice_id} value={voice.voice_id}>{voice.name}{voice.labels?.accent ? ` · ${voice.labels.accent}` : ''}</option>)}</select><small>El idioma del texto será español. El acento regional depende de la voz: el andaluz es un acento; el catalán y el euskera son idiomas distintos y necesitan voces específicas.</small></label>}
        {voiceLoadError && <p className="audio-setup-note" role="alert">No se pudo cargar la lista de voces: {voiceLoadError}. Se usará la voz predeterminada del servidor si está configurada.</p>}
        </div>}
        {selectedFile && <button className="choose generate-preview" type="button" onClick={() => void generatePreview()} disabled={busy || !canGenerateAudio}>
          {busy ? <span className="spinner" aria-hidden="true"/> : <Volume2 size={19} aria-hidden="true"/>}
          {busy ? 'Generando la vista previa…' : 'Generar vista previa'}
        </button>}
      </section>
      {!audio && <div className="live" role="status" aria-live="polite" aria-atomic="true">{message && <><span className="status-icon" aria-hidden="true"><Volume2 size={18}/></span><span>{message}</span></>}</div>}
      {(audio || transcript) && <div className="reader-grid">
      {audio && <section id="listen-section" className="player workflow-card" aria-labelledby="player-title">
        <div className="player-heading"><span className="player-icon" aria-hidden="true"><Volume2 size={21}/></span><div className="player-title"><h2 id="player-title">Vista previa de audio</h2><span>{bookMetadata.title || book} · Español</span></div></div>
        {message && <p className="player-status" role="status" aria-live="polite" aria-atomic="true"><CheckCircle2 size={17} aria-hidden="true"/>{message}</p>}
        <audio ref={playerRef} controls src={audio} onTimeUpdate={trackPlayback} onSeeked={trackPlayback} aria-label={`Escuchar la vista previa narrada de ${bookMetadata.title || book}`}/>
        <div className="player-playback"><label htmlFor="playback-speed">Velocidad<select id="playback-speed" value={playbackRate} onChange={event => setPlaybackRate(Number(event.target.value))}><option value={0.75}>0,75×</option><option value={1}>1×</option><option value={1.25}>1,25×</option><option value={1.5}>1,5×</option><option value={1.75}>1,75×</option><option value={2}>2×</option></select></label></div>
        <a className="download" href={audio} download={`${(bookMetadata.title || book).replace(/\.[^.]+$/, '')}-vista-previa.mp3`}><Download size={17} aria-hidden="true"/>Descargar vista previa en MP3</a>
      </section>}
      {transcript && <section className="transcript workflow-card" aria-labelledby="transcript-title">
        <h2 id="transcript-title">Texto de la narración</h2>
        {alignment.length > 0 ? <div className="timed-transcript" aria-live="off">{alignedParagraphs.map((paragraph, paragraphIndex) => <p key={"paragraph-" + paragraphIndex}>{paragraph.map(phrase => { const index = alignment.indexOf(phrase); const active = index === currentPhrase; return <span key={phrase.text_start + "-" + index} className={active ? "timed-phrase is-speaking" : "timed-phrase"} aria-current={active ? "true" : undefined}>{phrase.text} </span> })}</p>)}</div> : <><p className="sync-note">{capabilities?.tts_provider === 'deepgram' ? 'La voz Celeste no proporciona tiempos palabra por palabra. El texto se muestra organizado por párrafos.' : 'El seguimiento sincronizado está disponible cuando se genera el audio con ElevenLabs.'}</p>{transcript.split(/\n{2,}/).map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>)}</>}
        {capabilities?.audio_review_configured && <div className="review-controls"><button type="button" className="package-download" onClick={reviewPreview} disabled={reviewBusy || !audio}>{reviewBusy ? 'Revisando el audio…' : 'Comparar audio con ElevenLabs Scribe'}</button><small>Esta comprobación usa ElevenLabs Scribe, aunque la narración se haya creado con otro proveedor. Si falla, la narración y el MP3 siguen disponibles; el mensaje indicará si es un problema de clave, permisos, cuota o conexión. Las diferencias son avisos que debe revisar una persona.</small></div>}
        {review && <section className="review-results" aria-labelledby="review-title"><h3 id="review-title">Diferencias detectadas: {review.issues.length}</h3><p>{review.review_note}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, index) => <li key={`${issue.kind}-${index}`}><strong>{issue.kind === 'insert' ? 'Posible palabra añadida' : issue.kind === 'delete' ? 'Posible palabra omitida' : 'Posible sustitución'}:</strong> {issue.expected ? `Texto: “${issue.expected}”. ` : ''}{issue.recognized ? `Audio: “${issue.recognized}”.` : ''}</li>)}</ul>}<label htmlFor="retake-text">Edita el texto de esta vista previa<textarea id="retake-text" value={transcript} onChange={event => setTranscript(event.target.value)} rows={6}/></label><button type="button" className="package-download" onClick={regenerateRetake} disabled={retakeBusy}>{retakeBusy ? 'Generando…' : 'Generar vista previa corregida'}</button></section>}
      </section>}
      </div>}
      {bookChapters.length > 0 && <section id="book-helper" className="book-helper workflow-card" aria-labelledby="book-helper-title">
        <h2 id="book-helper-title">Asistente del libro</h2>
        <p>He encontrado {bookChapters.length} {bookChapters.length === 1 ? 'capítulo' : 'capítulos'}, {totalParagraphCount} párrafos y unas {totalWordCount.toLocaleString('es-ES')} palabras. Usa estas herramientas con el texto que acabamos de extraer.</p>
        <details className="metadata-editor"><summary>Revisar los datos bibliográficos</summary><p>Corrige los datos detectados. Se guardarán en los metadatos del libro dentro del ZIP.</p><label htmlFor="metadata-title">Título<input id="metadata-title" maxLength={500} value={bookMetadata.title} onChange={event => setBookMetadata(current => ({ ...current, title: event.target.value }))}/></label><label htmlFor="metadata-author">Autoría<input id="metadata-author" maxLength={500} value={bookMetadata.author} onChange={event => setBookMetadata(current => ({ ...current, author: event.target.value }))}/></label><div className="metadata-row"><label htmlFor="metadata-edition">Edición<input id="metadata-edition" maxLength={500} value={bookMetadata.edition} onChange={event => setBookMetadata(current => ({ ...current, edition: event.target.value }))}/></label><label htmlFor="metadata-year">Año de publicación<input id="metadata-year" maxLength={500} value={bookMetadata.publication_year} onChange={event => setBookMetadata(current => ({ ...current, publication_year: event.target.value }))}/></label></div><label htmlFor="metadata-publisher">Editorial<input id="metadata-publisher" maxLength={500} value={bookMetadata.publisher} onChange={event => setBookMetadata(current => ({ ...current, publisher: event.target.value }))}/></label></details>
        {bookChapters.length === 1 && totalParagraphCount > 1 && !hasManualSections && <div className="helper-action"><div><strong>¿El libro no tiene capítulos detectables?</strong><p>Puedo crear secciones de navegación según la longitud, respetando los límites de párrafo. Son secciones útiles para orientarse, no capítulos originales del autor.</p></div><button type="button" className="package-download" onClick={createNavigationSections}>Crear secciones de navegación</button></div>}
        {hasManualSections && <div className="helper-action"><p>Estas secciones se han creado automáticamente por longitud. Revisa sus límites y el texto antes de generar el audiolibro.</p><button type="button" className="helper-secondary" onClick={restoreDetectedStructure}>Deshacer y restaurar estructura original</button></div>}
        <div className="book-search"><label htmlFor="book-search-input">Buscar una palabra, persona o lugar</label><input id="book-search-input" type="search" value={searchQuery} onChange={event => setSearchQuery(event.target.value)} aria-describedby="book-search-count" placeholder="Por ejemplo: Robespierre"/><p id="book-search-count" role="status" aria-live="polite">{normalizedSearch ? `${matchingParagraphCount} párrafos encontrados${matchingParagraphCount > helperSearchResults.length ? ` · mostrando ${helperSearchResults.length}` : ''}` : 'Escribe una palabra para localizarla y corregir el párrafo correspondiente.'}</p></div>
        {normalizedSearch && <div className="helper-results" aria-label="Resultados de búsqueda">{helperSearchResults.length ? helperSearchResults.map(result => <button type="button" key={`${result.chapterIndex}-${result.paragraphIndex}`} onClick={() => openParagraphForEditing(result.chapterIndex, result.paragraphIndex)}><strong>{result.chapterTitle} · párrafo {result.paragraphIndex + 1}</strong><span>{result.text}</span></button>) : <p>No he encontrado esa palabra en el texto del libro.</p>}</div>}
        <details className="book-outline" ref={outlineRef}><summary>Revisar y corregir capítulos y párrafos</summary>{chapterSearchResults.length === 0 && <p>No hay coincidencias en el texto del libro.</p>}{chapterSearchResults.map(chapter => <section key={`${chapter.chapterIndex}-${chapter.title}`} aria-labelledby={`book-chapter-${chapter.chapterIndex}`}><h3 id={`book-chapter-${chapter.chapterIndex}`}>{chapter.title}</h3>{chapter.matchingParagraphs.map(({ text, paragraphIndex }) => <label className="book-paragraph" key={`${chapter.chapterIndex}-${paragraphIndex}`} htmlFor={`book-paragraph-${chapter.chapterIndex}-${paragraphIndex}`}><span>Párrafo {paragraphIndex + 1}</span><textarea id={`book-paragraph-${chapter.chapterIndex}-${paragraphIndex}`} rows={Math.min(8, Math.max(3, Math.ceil(text.length / 90)))} value={text} onChange={event => setBookChapters(current => current.map((item, index) => index !== chapter.chapterIndex ? item : { ...item, paragraphs: item.paragraphs.map((value, part) => part === paragraphIndex ? event.target.value : value) }))}/></label>)}</section>)}</details>
      </section>}
      {selectedFile && transcript && <section className="export-card" aria-labelledby="export-title"><div><h2 id="export-title">Crear y descargar audiolibro completo</h2><p>Genera la narración completa y descarga un ZIP con el audio y los archivos de navegación.</p><small>Las correcciones que hayas hecho se incluirán en el libro.</small></div><button className="choose export-button" type="button" onClick={downloadFullBook} disabled={busy || packageBusy || !canGenerateAudio}>{packageBusy ? <span className="spinner" aria-hidden="true"/> : <Download size={18} aria-hidden="true"/>}{packageBusy ? 'Generando el audiolibro…' : 'Generar y descargar audiolibro (ZIP)'}</button></section>}
      <p className="promise">Escucha a tu ritmo.</p>
    </section>
    <footer><span>Creado con cuidado para cada lector.</span><span>Accesible desde el principio <span aria-hidden="true">·</span> Siempre a tu alcance</span></footer>
  </main>
}
