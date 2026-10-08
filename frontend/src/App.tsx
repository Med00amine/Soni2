import { useRef, useState } from 'react'
import { BookOpen, Upload, Download, Volume2, CheckCircle2 } from 'lucide-react'

const API = (import.meta.env.VITE_API_URL ?? '').replace(/\/+$/, '')

export default function App() {
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [audio, setAudio] = useState('')
  const [transcript, setTranscript] = useState('')
  const [book, setBook] = useState('')
  const [selectedFile, setSelectedFile] = useState<File>()
  const [packageBusy, setPackageBusy] = useState(false)

  async function choose(file?: File) {
    if (!file) return
    setSelectedFile(file)
    setAudio(''); setTranscript(''); setBook(file.name); setBusy(true); setMessage('Estamos leyendo tu libro. Puede tardar un momento.')
    const form = new FormData(); form.append('file', file)
    try {
      const response = await fetch(`${API}/api/books/preview`, { method: 'POST', body: form })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'No hemos podido leer este documento. Prueba con otro archivo.')
      }
      const result: { audio_base64: string; audio_mime_type: string; transcript: string } = await response.json()
      const binary = atob(result.audio_base64)
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0))
      const blob = new Blob([bytes], { type: result.audio_mime_type })
      setAudio(URL.createObjectURL(blob)); setMessage('La narración está lista. Pulsa el botón de reproducción para escucharla.')
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

  return <main className="shell">
    <a className="skip" href="#main">Saltar al contenido principal</a>
    <header className="top"><a className="brand" href="/" aria-label="Inicio de Soni2"><img className="brand-logo" src="/soni2-logo.svg" alt="Soni2" width="200" height="42"/></a><span className="top-note">Una forma más tranquila de disfrutar los libros</span></header>
    <section id="main" className="hero" aria-labelledby="title">
      <div className="eyebrow"><span className="eyebrow-dot"/>TU LIBRO, EN VOZ ALTA</div>
      <h1 id="title">Convierte tu libro<br/>en <em>una voz que te acompaña.</em></h1>
      <p className="intro">Una narración clara y agradable está a solo un archivo de distancia.<br className="desktop"/> Elige un documento y nos encargamos del resto.</p>
      <section className="upload-card" aria-labelledby="upload-title">
        <div className="card-heading"><div className="book-icon"><BookOpen size={23}/></div><div><h2 id="upload-title">Empieza con tu libro</h2><p>PDF, EPUB, Word, texto o XML</p></div></div>
        <button className="choose" type="button" onClick={() => input.current?.click()} disabled={busy}>
          {busy ? <span className="spinner" aria-hidden="true"/> : <Upload size={19} aria-hidden="true"/>}
          {busy ? 'Preparando la narración…' : 'Elegir un libro'}
        </button>
        <input ref={input} className="visually-hidden" type="file" accept=".pdf,.epub,.docx,.txt,.xml" aria-label="Elige un libro en formato PDF, EPUB, Word, texto o XML" onChange={e => { const file = e.target.files?.[0]; e.currentTarget.value = ''; void choose(file) }}/>
        {selectedFile && <button className="package-download" type="button" onClick={downloadFullBook} disabled={busy || packageBusy}>
          {packageBusy ? <span className="spinner" aria-hidden="true"/> : <Download size={18} aria-hidden="true"/>}
          {packageBusy ? 'Creando el libro completo…' : 'Descargar libro completo (ZIP)'}
        </button>}
        <p className="privacy"><span aria-hidden="true">⌑</span> Tu libro se trata con cuidado y privacidad.</p>
        <div className="supported"><span>PDF</span><span>EPUB</span><span>DOCX</span><span>TXT</span><span>XML</span></div>
      </section>
      <div className="live" role="status" aria-live="polite" aria-atomic="true">{message && <><span className="status-icon" aria-hidden="true">{audio ? <CheckCircle2 size={18}/> : <Volume2 size={18}/>}</span><span>{message}</span></>}</div>
      {audio && <section className="player" aria-label="Vista previa del audiolibro"><div className="player-title"><strong>{book}</strong><span>Español · Vista previa</span></div><audio controls src={audio} aria-label={`Escuchar la vista previa narrada de ${book}`}/><a className="download" href={audio} download={`${book.replace(/\.[^.]+$/, '')}-vista-previa.mp3`}>Descargar esta vista previa</a></section>}
      {transcript && <section className="transcript" aria-labelledby="transcript-title">
        <h2 id="transcript-title">Texto de la narración</h2>
        {transcript.split(/\n{2,}/).map((paragraph, index) => (
          <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>
        ))}
      </section>}
      <p className="promise">Escucha a tu ritmo.</p>
    </section>
    <footer><span>Creado con cuidado para cada lector.</span><span>Accesible desde el principio <span aria-hidden="true">·</span> Siempre a tu alcance</span></footer>
  </main>
}
