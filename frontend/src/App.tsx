import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { BookOpen, CheckCircle2, ChevronLeft, ChevronRight, Download, FileText, Headphones, Keyboard, Pause, Play, Settings, Upload, Volume2, X } from "lucide-react";
import { api } from "./api";
import type { Book, GenerationJob, PlayerState, Sentence, UserSettings, Voice } from "./types";

const defaults: UserSettings = { theme: "system", fontSize: "medium", highContrast: false, reduceMotion: false };
const formatTime = (seconds = 0) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

export default function App() {
  const [book, setBook] = useState<Book | null>(null);
  const [sentences, setSentences] = useState<Sentence[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [selected, setSelected] = useState(0);
  const [player, setPlayer] = useState<PlayerState>({ playing: false, chapterIndex: 0, position: 0, rate: 1 });
  const [settings, setSettings] = useState<UserSettings>(() => JSON.parse(localStorage.getItem("vocality-settings") || JSON.stringify(defaults)));
  const [showSettings, setShowSettings] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  const chapter = book?.chapters[player.chapterIndex];
  const chapterSentences = useMemo(() => sentences.filter(item => item.chapter_id === chapter?.id), [sentences, chapter?.id]);
  const currentSentence = chapterSentences[selected] ?? chapterSentences[0];
  const total = useMemo(() => sentences.reduce((max, item) => Math.max(max, item.end ?? 0), 0), [sentences]);

  useEffect(() => {
    localStorage.setItem("vocality-settings", JSON.stringify(settings));
    document.documentElement.dataset.theme = settings.theme;
    document.documentElement.dataset.fontSize = settings.fontSize;
    document.documentElement.classList.toggle("contrast", settings.highContrast);
  }, [settings]);
  useEffect(() => { Promise.all([api.listBooks(), api.voices()]).then(([books, available]) => { setVoices(available); if (books[0]) loadBook(books[0].id); }).catch(() => setError("Connect to the audiobook API to load books.")); }, []);
  useEffect(() => { const onKey = (event: KeyboardEvent) => { if (!book || ["INPUT", "TEXTAREA", "SELECT"].includes((event.target as HTMLElement).tagName)) return; if (event.key === " ") { event.preventDefault(); togglePlayback(); } if (event.key === "ArrowRight") seek(15); if (event.key === "ArrowLeft") seek(-15); if (event.key === "n" || event.key === "N") selectChapter(1); if (event.key === "p" || event.key === "P") selectChapter(-1); if (event.key === "?") setShowShortcuts(true); }; window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey); });
  useEffect(() => { if (!job || job.status === "completed" || job.status === "failed") return; const timer = window.setInterval(async () => { try { const next = await api.job(job.id); setJob(next); if (next.status === "completed") { await loadBook(book!.id); } } catch { setError("Unable to check generation status."); } }, 1500); return () => window.clearInterval(timer); }, [job, book]);
  useEffect(() => { if (audioRef.current) { audioRef.current.playbackRate = player.rate; player.playing ? void audioRef.current.play().catch(() => setPlayer(p => ({ ...p, playing: false }))) : audioRef.current.pause(); } }, [player.playing, currentSentence?.id, player.rate]);

  async function loadBook(id: string) {
    setLoading(true);
    try {
      const loaded = await api.getBook(id);
      setBook(loaded);
      try {
        const text = await api.getText(id);
        setSentences(text.sentences);
      } catch {
        setSentences([]);
      }
      setSelected(0);
    } catch {
      setError("The book could not be loaded.");
    } finally {
      setLoading(false);
    }
  }
  async function upload(file?: File) {
    if (!file) return;
    if (!/(\.xml|\.dtbook)$/i.test(file.name)) { setError("Only DTBook XML files are supported."); return; }
    setError(""); try { const uploaded = await api.uploadBook(file); await loadBook(uploaded.id); } catch (cause) { setError(cause instanceof Error ? cause.message : "Upload failed."); }
  }
  async function generate() {
    if (!book) return;
    try { const started = await api.generate(book.id, voiceId || undefined); setJob(started); } catch (cause) { setError(cause instanceof Error ? cause.message : "Generation failed."); }
  }
  function togglePlayback() { if (!currentSentence?.audioUrl) { setError("Generate the audiobook before playing audio."); return; } setPlayer(p => ({ ...p, playing: !p.playing })); }
  function seek(delta: number) { if (!audioRef.current) return; audioRef.current.currentTime = Math.max(0, audioRef.current.currentTime + delta); }
  function selectChapter(delta: number) { if (!book) return; setPlayer(p => ({ ...p, chapterIndex: Math.max(0, Math.min(book.chapters.length - 1, p.chapterIndex + delta)), playing: false })); setSelected(0); }
  function onAudioEnded() { if (selected + 1 < chapterSentences.length) setSelected(value => value + 1); else selectChapter(1); }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="topbar"><a className="brand" href="#" aria-label="Vocality DAISY Studio home"><span className="brand-mark"><Headphones size={21} /></span><span>Vocality <small>DAISY STUDIO</small></span></a><div className="top-actions"><span className="status-dot"><CheckCircle2 size={15} /> Accessible mode</span><button className="icon-button" aria-label="Keyboard shortcuts" onClick={() => setShowShortcuts(true)}><Keyboard size={19} /></button><button className="icon-button" aria-label="Settings" onClick={() => setShowSettings(true)}><Settings size={19} /></button></div></header>
    <main id="main-content" className="workspace">
      <aside className="sidebar"><div className="side-heading"><h2>My books</h2><button className="button primary small" onClick={() => fileInput.current?.click()}><Upload size={16} /> Upload</button><input ref={fileInput} hidden type="file" accept=".xml,.dtbook" onChange={e => upload(e.target.files?.[0])} /></div>{book ? <div className="book-card selected"><div className="book-cover"><BookOpen /></div><div><strong>{book.title}</strong><span>{book.author}</span><small>{book.chapters.length} chapters · {formatTime(total)}</small></div></div> : <div className="empty-side">No books uploaded yet.</div>}<div className="side-help"><FileText size={19} /><strong>DTBook XML only</strong><p>Upload a DTBook XML file. The backend creates the synchronized DAISY package and audio.</p></div></aside>
      <section className="content">{!book ? <UploadFirst onUpload={() => fileInput.current?.click()} error={error} /> : <><div className="page-heading"><div><p className="eyebrow">AUDIOBOOK WORKSPACE</p><h1>{book.title}</h1><p className="muted">{book.author || "Unknown author"} · {book.language?.toUpperCase() || "—"} · {book.chapters.length} chapters</p></div><div className="heading-actions"><button className="button secondary" onClick={() => setShowSettings(true)}><Settings size={17} /> Reading settings</button><button className="button primary" onClick={generate} disabled={job?.status === "running" || job?.status === "queued"}><Play size={17} /> {job?.status === "running" ? `Generating ${Math.round(job.progress * 100)}%` : "Generate audiobook"}</button></div></div>
        {error && <div className="alert" role="alert"><span>{error}</span><button onClick={() => setError("")} aria-label="Dismiss"><X size={17} /></button></div>}
        {voices.length > 0 && <label className="voice-picker">Voice <select value={voiceId} onChange={e => setVoiceId(e.target.value)}>{voices.map(voice => <option key={voice.id} value={voice.id}>{voice.name} · {voice.language}</option>)}</select></label>}
        <div className="player-panel" aria-label="Audiobook player"><audio ref={audioRef} src={currentSentence?.audioUrl} onEnded={onAudioEnded} onTimeUpdate={e => { const currentTime = e.currentTarget.currentTime; setPlayer(p => ({ ...p, position: currentTime + (currentSentence?.start ?? 0) })); }} /><div className="player-top"><div><span className="now-playing">NOW PLAYING</span><h2>{chapter?.title}</h2></div><span className="tag">{job?.status === "completed" || book.status === "ready" ? "DAISY ready" : job?.status || "Not generated"}</span></div><div className="progress-row"><span>{formatTime(player.position)}</span><input aria-label="Playback position" type="range" min="0" max={chapter?.durationSeconds || 1} value={Math.min(player.position, chapter?.durationSeconds || 1)} onChange={e => { if (audioRef.current) audioRef.current.currentTime = Number(e.target.value) - (currentSentence?.start ?? 0); }} /><span>{formatTime(chapter?.durationSeconds || 0)}</span></div><div className="player-controls"><button className="icon-button" onClick={() => selectChapter(-1)} aria-label="Previous chapter"><ChevronLeft /></button><button className="play-button" onClick={togglePlayback} aria-label={player.playing ? "Pause" : "Play"}>{player.playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}</button><button className="icon-button" onClick={() => selectChapter(1)} aria-label="Next chapter"><ChevronRight /></button><label className="rate">Speed <select value={player.rate} onChange={e => setPlayer({ ...player, rate: Number(e.target.value) })}><option value="0.75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option></select></label><span className="volume"><Volume2 size={18} /> <input aria-label="Volume" type="range" defaultValue="80" onChange={e => { if (audioRef.current) audioRef.current.volume = Number(e.target.value) / 100; }} /></span></div></div>
        <div className="reader-grid"><article className="reader"><div className="reader-toolbar"><span><FileText size={17} /> Text view</span><span className="muted">Chapter {player.chapterIndex + 1} of {book.chapters.length}</span></div><h2>{chapter?.title}</h2>{chapterSentences.length ? chapterSentences.map((sentence, index) => <button className={`sentence ${index === selected ? "current" : ""}`} key={sentence.id} onClick={() => { setSelected(index); setPlayer(p => ({ ...p, playing: true })); }}>{sentence.text}</button>) : <p className="muted">Synchronized text will appear after generation.</p>}</article><nav className="chapters" aria-label="Book chapters"><div className="chapters-title"><h2>Contents</h2><span>{book.chapters.length}</span></div>{book.chapters.map((item, index) => <button className={`chapter ${index === player.chapterIndex ? "active" : ""}`} key={item.id} onClick={() => { setPlayer(p => ({ ...p, chapterIndex: index, playing: false })); setSelected(0); }}><span className="chapter-number">{String(index + 1).padStart(2, "0")}</span><span><strong>{item.title}</strong><small>{formatTime(item.durationSeconds || 0)}</small></span></button>)}<button className="download" onClick={() => window.open(api.downloadDaisy(book.id), "_blank")}><Download size={17} /> Download DAISY package</button></nav></div></>}</section>
    </main>
    <footer className="footer"><span><CheckCircle2 size={15} /> Keyboard accessible · Screen reader friendly</span><button onClick={() => setShowShortcuts(true)}>View shortcuts</button></footer>
    {showSettings && <Modal title="Reading settings" close={() => setShowSettings(false)}><label className="setting">Text size<select value={settings.fontSize} onChange={e => setSettings({ ...settings, fontSize: e.target.value as UserSettings["fontSize"] })}><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option></select></label><label className="setting">Colour theme<select value={settings.theme} onChange={e => setSettings({ ...settings, theme: e.target.value as UserSettings["theme"] })}><option value="system">System default</option><option value="light">Light</option><option value="dark">Dark</option></select></label><label className="check"><input type="checkbox" checked={settings.highContrast} onChange={e => setSettings({ ...settings, highContrast: e.target.checked })} /> High contrast</label></Modal>}
    {showShortcuts && <Modal title="Keyboard shortcuts" close={() => setShowShortcuts(false)}><dl className="shortcuts"><dt>Space</dt><dd>Play or pause</dd><dt>← / →</dt><dd>Seek 15 seconds</dd><dt>N / P</dt><dd>Next / previous chapter</dd><dt>?</dt><dd>Show this panel</dd></dl></Modal>}
  </div>;
}

function UploadFirst({ onUpload, error }: { onUpload: () => void; error: string }) { return <div className="upload-first"><div className="upload-icon"><Upload size={30} /></div><p className="eyebrow">START A NEW AUDIOBOOK</p><h1>Upload a DTBook</h1><p>Turn a structured DTBook XML file into an accessible, synchronized DAISY audiobook.</p><button className="button primary" onClick={onUpload}><Upload size={17} /> Choose DTBook XML</button><small>Supported format: .xml or .dtbook</small>{error && <div className="alert" role="alert">{error}</div>}</div>; }
function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) { return <div className="modal-backdrop" role="presentation" onClick={close}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={e => e.stopPropagation()}><div className="modal-header"><h2 id="modal-title">{title}</h2><button className="icon-button" onClick={close} aria-label="Close"><X /></button></div>{children}</section></div>; }
