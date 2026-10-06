import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { BookOpen, CheckCircle2, ChevronLeft, ChevronRight, Download, FileText, Keyboard, Pause, Play, Settings, Upload, Volume2, X } from "lucide-react";
import { api } from "./api";
import type { Audiobook, Book, GenerationJob, PlayerState, Sentence, User, UserSettings, Voice } from "./types";

const defaults: UserSettings = { theme: "system", fontSize: "medium", highContrast: false, reduceMotion: false };
const formatTime = (seconds = 0) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

export default function App() {
  const [route, setRoute] = useState(window.location.pathname);
  const [book, setBook] = useState<Book | null>(null);
  const [sentences, setSentences] = useState<Sentence[]>([]);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [job, setJob] = useState<GenerationJob | null>(null);
  const [selected, setSelected] = useState(0);
  const [player, setPlayer] = useState<PlayerState>({ playing: false, chapterIndex: 0, position: 0, rate: 1 });
  const [audioDuration, setAudioDuration] = useState(0);
  const [settings, setSettings] = useState<UserSettings>(() => JSON.parse(localStorage.getItem("vocality-settings") || JSON.stringify(defaults)));
  const [showSettings, setShowSettings] = useState(false);
  const [showShortcuts, setShowShortcuts] = useState(false);
  const [error, setError] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [catalog, setCatalog] = useState<Audiobook[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const lastProgressSave = useRef(0);

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
  useEffect(() => {
    api.voices().then(available => { setVoices(available); setVoiceId(available[0]?.id || ""); }).catch(() => setError("Connect to the audiobook API to load voices."));
    if (localStorage.getItem("vocality-token")) api.me().then(setUser).catch(() => localStorage.removeItem("vocality-token"));
  }, []);
  useEffect(() => {
    const onPopState = () => setRoute(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    if (route === "/catalog" || route === "/search") {
      api.catalog(search).then(result => setCatalog(result.items)).catch(() => setError("Unable to load the catalog."));
    }
    const match = route.match(/^\/audiobooks\/([^/]+)$/);
    if (match) void loadBook(decodeURIComponent(match[1]));
  }, [route, search]);
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
    if (!/(\.xml|\.dtbook|\.epub|\.html|\.htm|\.pdf)$/i.test(file.name)) { setError("Supported formats: DTBook XML, EPUB, HTML, PDF."); return; }
    setError(""); try { const uploaded = await api.uploadBook(file); navigate(`/audiobooks/${uploaded.id}`); } catch (cause) { setError(cause instanceof Error ? cause.message : "Upload failed."); }
  }
  async function generate() {
    if (!book) return;
    try { const started = await api.generate(book.id, voiceId || undefined); setJob(started); } catch (cause) { setError(cause instanceof Error ? cause.message : "Generation failed."); }
  }
  function togglePlayback() { if (!currentSentence?.audioUrl) { setError("Generate the audiobook before playing audio."); return; } setPlayer(p => ({ ...p, playing: !p.playing })); }
  function seek(delta: number) { if (!audioRef.current) return; audioRef.current.currentTime = Math.max(0, audioRef.current.currentTime + delta); }
  function selectChapter(delta: number) { if (!book) return; setPlayer(p => ({ ...p, chapterIndex: Math.max(0, Math.min(book.chapters.length - 1, p.chapterIndex + delta)), playing: false })); setSelected(0); }
  function onAudioEnded() { if (selected + 1 < chapterSentences.length) setSelected(value => value + 1); else selectChapter(1); }
  function navigate(path: string) {
    window.history.pushState({}, "", path);
    setRoute(path);
  }
  function signOut() {
    localStorage.removeItem("vocality-token");
    setUser(null);
    navigate("/catalog");
  }
  if (route === "/auth/login" || route === "/auth/register") {
    return <AuthPage mode={route === "/auth/register" ? "register" : "login"} onSuccess={nextUser => { setUser(nextUser); navigate("/catalog"); }} onNavigate={navigate} />;
  }
  if (route === "/library" || route === "/favorites" || route === "/account") {
    if (!user) return <AuthRequired onNavigate={() => navigate("/auth/login")} />;
    return <AccountPage route={route} user={user} onNavigate={navigate} onSignOut={signOut} />;
  }
  const isWorkspace = route.startsWith("/audiobooks/");
  const isHome = route === "/";
  const isCatalog = route === "/catalog" || route === "/search";

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <input ref={fileInput} hidden type="file" accept=".xml,.dtbook,.epub,.html,.htm,.pdf" onChange={event => { void upload(event.target.files?.[0]); event.currentTarget.value = ""; }} />
    <header className="topbar"><a className="brand" href="/" onClick={event => { event.preventDefault(); navigate("/"); }} aria-label="Vocality.AI home"><img className="brand-logo" src="/vocality-mark.svg" alt="" /><span>VOCALITY.AI <small>DAISY STUDIO</small></span></a><nav className="main-nav" aria-label="Primary navigation"><a href="/" onClick={event => { event.preventDefault(); navigate("/"); }}>Home</a><a href="/catalog" onClick={event => { event.preventDefault(); navigate("/catalog"); }}>Catalog</a><a href="/search" onClick={event => { event.preventDefault(); navigate("/search"); }}>Search</a>{user && <><a href="/library" onClick={event => { event.preventDefault(); navigate("/library"); }}>My Library</a><a href="/favorites" onClick={event => { event.preventDefault(); navigate("/favorites"); }}>Favorites</a></>}</nav><div className="top-actions"><span className="status-dot"><CheckCircle2 size={15} /> Accessible mode</span>{!isWorkspace && (user ? <><a href="/account" onClick={event => { event.preventDefault(); navigate("/account"); }}>{user.display_name}</a><button className="button secondary small" onClick={signOut}>Logout</button></> : <><a href="/auth/login" onClick={event => { event.preventDefault(); navigate("/auth/login"); }}>Sign in</a><a className="button secondary small" href="/auth/register" onClick={event => { event.preventDefault(); navigate("/auth/register"); }}>Create account</a></>)}<button className="icon-button" aria-label="Keyboard shortcuts" onClick={() => setShowShortcuts(true)}><Keyboard size={19} /></button><button className="icon-button" aria-label="Settings" onClick={() => setShowSettings(true)}><Settings size={19} /></button></div></header>
    <main id="main-content" className={isWorkspace ? "workspace" : "catalog-layout"}>
      {isHome && <HomePage user={user} onNavigate={navigate} onUpload={() => fileInput.current?.click()} />}
      {isCatalog && <CatalogPage items={catalog} search={search} onSearch={setSearch} onOpen={id => navigate(`/audiobooks/${id}`)} onUpload={() => fileInput.current?.click()} />}
      {isWorkspace && <>
      <aside className="sidebar"><div className="side-heading"><h2>My books</h2><button className="button primary small" onClick={() => fileInput.current?.click()}><Upload size={16} /> Upload</button></div>{book ? <div className="book-card selected"><div className="book-cover"><BookOpen /></div><div><strong>{book.title}</strong><span>{book.author}</span><small>{book.chapters.length} chapters · {formatTime(total)}</small></div></div> : <div className="empty-side">No books uploaded yet.</div>}{user && <section aria-labelledby="catalog-heading"><h2 id="catalog-heading">Discover</h2><label>Search catalog<input value={search} onChange={e => setSearch(e.target.value)} /></label>{catalog.map(item => <button className="chapter" key={item.id} onClick={() => loadBook(item.book_id)}>{item.id}</button>)}</section>}<div className="side-help"><FileText size={19} /><strong>Multiple formats</strong><p>Supported formats: DTBook XML, EPUB, HTML, PDF.</p></div></aside>
      <section className="content">{!book ? <UploadFirst onUpload={() => fileInput.current?.click()} error={error} /> : <><div className="page-heading"><div><p className="eyebrow">AUDIOBOOK WORKSPACE</p><h1>{book.title}</h1><p className="muted">{book.author || "Unknown author"} · {book.language?.toUpperCase() || "—"} · {book.chapters.length} chapters · {book.source_format?.toUpperCase() || "DTBOOK"}</p><div className="muted">Source: {book.source_filename || "uploaded document"} · {book.sentence_count ?? sentences.length} sentences</div></div><div className="heading-actions"><button className="button secondary" onClick={() => setShowSettings(true)}><Settings size={17} /> Reading settings</button><button className="button primary" onClick={generate} disabled={job?.status === "running" || job?.status === "queued"}><Play size={17} /> {job?.status === "running" ? `Generating ${Math.round(job.progress * 100)}%` : "Generate audiobook"}</button></div></div>
        {error && <div className="alert" role="alert"><span>{error}</span><button onClick={() => setError("")} aria-label="Dismiss"><X size={17} /></button></div>}
        {voices.length > 0 && <label className="voice-picker">Voice <select value={voiceId} onChange={e => setVoiceId(e.target.value)}>{voices.map(voice => <option key={voice.id} value={voice.id}>{voice.name} · {voice.language}</option>)}</select></label>}
        <div className="player-panel" aria-label="Audiobook player"><audio ref={audioRef} src={currentSentence?.audioUrl} onLoadedMetadata={event => setAudioDuration(event.currentTarget.duration || 0)} onDurationChange={event => setAudioDuration(event.currentTarget.duration || 0)} onEnded={onAudioEnded} onTimeUpdate={e => { const currentTime = e.currentTarget.currentTime; const position = currentTime + (currentSentence?.start ?? 0); setPlayer(p => ({ ...p, position })); const audiobookId = job?.audiobook_id; if (user && audiobookId && position - lastProgressSave.current >= 5) { lastProgressSave.current = position; void api.saveProgress(audiobookId, { current_sentence_id: currentSentence?.id, position_seconds: position, completed: false }); } }} /><div className="player-top"><div><span className="now-playing">NOW PLAYING</span><h2>{chapter?.title}</h2></div><span className="tag">{job?.status === "completed" || book.status === "ready" ? "DAISY ready" : job?.status || "Not generated"}</span></div><div className="progress-row"><span>{formatTime(audioRef.current?.currentTime || 0)}</span><input aria-label="Audio progress" type="range" min="0" max={Math.max(audioDuration, 1)} step="0.01" value={Math.min(audioRef.current?.currentTime || 0, Math.max(audioDuration, 1))} onChange={e => { if (audioRef.current) audioRef.current.currentTime = Number(e.target.value); }} /><span>{formatTime(audioDuration)}</span></div><div className="player-controls"><button className="icon-button" onClick={() => selectChapter(-1)} aria-label="Previous chapter"><ChevronLeft /></button><button className="play-button" onClick={togglePlayback} aria-label={player.playing ? "Pause" : "Play"}>{player.playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}</button><button className="icon-button" onClick={() => selectChapter(1)} aria-label="Next chapter"><ChevronRight /></button><label className="rate">Speed <select value={player.rate} onChange={e => setPlayer({ ...player, rate: Number(e.target.value) })}><option value="0.75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="1.75">1.75×</option><option value="2">2×</option></select></label><span className="volume"><Volume2 size={18} /> <input aria-label="Volume" type="range" defaultValue="80" onChange={e => { if (audioRef.current) audioRef.current.volume = Number(e.target.value) / 100; }} /></span></div></div>
        <div className="reader-grid"><article className="reader"><div className="reader-toolbar"><span><FileText size={17} /> Text view</span><span className="muted">Chapter {player.chapterIndex + 1} of {book.chapters.length}</span></div><h2>{chapter?.title}</h2>{chapterSentences.length ? chapterSentences.map((sentence, index) => <button className={`sentence ${index === selected ? "current" : ""}`} key={sentence.id} onClick={() => { setSelected(index); setPlayer(p => ({ ...p, playing: true })); }}>{sentence.text}</button>) : <p className="muted">Synchronized text will appear after generation.</p>}</article><nav className="chapters" aria-label="Book chapters"><div className="chapters-title"><h2>Contents</h2><span>{book.chapters.length}</span></div>{book.chapters.map((item, index) => <button className={`chapter ${index === player.chapterIndex ? "active" : ""}`} key={item.id} onClick={() => { setPlayer(p => ({ ...p, chapterIndex: index, playing: false })); setSelected(0); }}><span className="chapter-number">{String(index + 1).padStart(2, "0")}</span><span><strong>{item.title}</strong><small>{formatTime(item.durationSeconds || 0)}</small></span></button>)}<button className="download" onClick={() => window.open(api.downloadDaisy(book.id), "_blank")}><Download size={17} /> Download DAISY package</button></nav></div></>}</section></>}
    </main>
    <footer className="footer"><span><CheckCircle2 size={15} /> Keyboard accessible · Screen reader friendly</span><button onClick={() => setShowShortcuts(true)}>View shortcuts</button></footer>
    {showSettings && <Modal title="Reading settings" close={() => setShowSettings(false)}><label className="setting">Text size<select value={settings.fontSize} onChange={e => setSettings({ ...settings, fontSize: e.target.value as UserSettings["fontSize"] })}><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option></select></label><label className="setting">Colour theme<select value={settings.theme} onChange={e => setSettings({ ...settings, theme: e.target.value as UserSettings["theme"] })}><option value="system">System default</option><option value="light">Light</option><option value="dark">Dark</option></select></label><label className="check"><input type="checkbox" checked={settings.highContrast} onChange={e => setSettings({ ...settings, highContrast: e.target.checked })} /> High contrast</label></Modal>}
    {showShortcuts && <Modal title="Keyboard shortcuts" close={() => setShowShortcuts(false)}><dl className="shortcuts"><dt>Space</dt><dd>Play or pause</dd><dt>← / →</dt><dd>Seek 15 seconds</dd><dt>N / P</dt><dd>Next / previous chapter</dd><dt>?</dt><dd>Show this panel</dd></dl></Modal>}
  </div>;
}

function UploadFirst({ onUpload, error }: { onUpload: () => void; error: string }) { return <div className="upload-first"><div className="upload-icon"><Upload size={30} /></div><p className="eyebrow">START A NEW AUDIOBOOK</p><h1>Upload a document</h1><p>Turn a structured document into an accessible, synchronized DAISY audiobook.</p><button className="button primary" onClick={onUpload}><Upload size={17} /> Choose document</button><small>Supported formats: DTBook XML, EPUB, HTML, PDF</small>{error && <div className="alert" role="alert">{error}</div>}</div>; }
function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) { return <div className="modal-backdrop" role="presentation" onClick={close}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onClick={e => e.stopPropagation()}><div className="modal-header"><h2 id="modal-title">{title}</h2><button className="icon-button" onClick={close} aria-label="Close"><X /></button></div>{children}</section></div>; }

function HomePage({ user, onNavigate, onUpload }: { user: User | null; onNavigate: (path: string) => void; onUpload: () => void }) {
  return <section className="home-page" aria-labelledby="home-title">
    <div className="home-hero">
      <div className="home-copy">
        <p className="eyebrow">WELCOME TO VOCALITY.AI</p>
        <h1 id="home-title">Accessible audiobooks that keep you connected to the text.</h1>
        <p>Listen to books, follow synchronized text, and control your reading experience at your own pace.</p>
        <div className="home-actions">
          <button className="button primary" onClick={() => onNavigate("/catalog")}>Explore audiobooks</button>
          <button className="button secondary" onClick={onUpload}><Upload size={17} /> Upload a book</button>
        </div>
      </div>
      <div className="home-card" aria-label="Vocality features">
        <img src="/vocality-mark.svg" alt="" className="home-logo" />
        <h2>{user ? `Welcome back, ${user.display_name}` : "A calmer way to listen"}</h2>
        <p>Clear narration, synchronized reading, and accessible playback controls in one place.</p>
      </div>
    </div>
    <div className="home-features">
      <article><h2>Listen naturally</h2><p>Adjust speed, pause, resume, and navigate by chapter.</p></article>
      <article><h2>Follow the text</h2><p>Read synchronized source text while your audiobook plays.</p></article>
      <article><h2>Make it yours</h2><p>Save books, favorites, and progress when you sign in.</p></article>
    </div>
  </section>;
}

function AuthPage({ mode, onSuccess, onNavigate }: { mode: "login" | "register"; onSuccess: (user: User) => void; onNavigate: (path: string) => void }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const register = mode === "register";
  useEffect(() => { headingRef.current?.focus(); }, [mode]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const result = register ? await api.register(email, password, displayName) : await api.login(email, password);
      localStorage.setItem("vocality-token", result.access_token);
      onSuccess(result.user);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to complete authentication.");
    } finally {
      setSubmitting(false);
    }
  }
  return <div className="auth-shell">
    <a className="skip-link" href="#auth-main">Skip to authentication form</a>
    <header className="auth-brand"><a href="/catalog" onClick={event => { event.preventDefault(); onNavigate("/catalog"); }}><img className="auth-logo" src="/vocality-mark.svg" alt="" />VOCALITY.AI</a></header>
    <main id="auth-main" className="auth-main">
      <section className="auth-card" aria-labelledby="auth-heading">
        <h1 id="auth-heading" tabIndex={-1} ref={headingRef}>{register ? "Create your account" : "Sign in"}</h1>
        <p className="muted">{register ? "Save your library, favorites, and listening progress." : "Continue to your accessible audiobook workspace."}</p>
        {error && <div id="auth-error" className="alert" role="alert">{error}</div>}
        <form onSubmit={submit} noValidate aria-busy={submitting}>
          {register && <label htmlFor="display-name">Display name<input id="display-name" name="displayName" autoComplete="name" value={displayName} onChange={event => setDisplayName(event.target.value)} required /></label>}
          <label htmlFor="email">Email<input id="email" name="email" type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} required aria-describedby={error ? "auth-error" : undefined} /></label>
          <label htmlFor="password">Password<input id="password" name="password" type="password" autoComplete={register ? "new-password" : "current-password"} minLength={8} value={password} onChange={event => setPassword(event.target.value)} required /></label>
          <button className="button primary auth-submit" type="submit" disabled={submitting}>{submitting ? "Please wait…" : register ? "Create account" : "Sign in"}</button>
        </form>
        <p className="auth-switch">{register ? "Already have an account?" : "Don't have an account?"}{" "}<a href={register ? "/auth/login" : "/auth/register"} onClick={event => { event.preventDefault(); onNavigate(register ? "/auth/login" : "/auth/register"); }}>{register ? "Sign in" : "Create an account"}</a></p>
      </section>
    </main>
  </div>;
}

function AuthRequired({ onNavigate }: { onNavigate: () => void }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { headingRef.current?.focus(); }, []);
  return <div className="auth-shell"><header className="auth-brand"><img className="auth-logo" src="/vocality-mark.svg" alt="" />VOCALITY.AI</header><main className="auth-main"><section className="auth-card" aria-labelledby="auth-required-heading"><h1 id="auth-required-heading" tabIndex={-1} ref={headingRef}>Sign in to continue</h1><p>You need an account to access your private library and listening history.</p><button className="button primary" onClick={onNavigate}>Sign in</button></section></main></div>;
}

function CatalogPage({ items, search, onSearch, onOpen, onUpload }: { items: Audiobook[]; search: string; onSearch: (value: string) => void; onOpen: (id: string) => void; onUpload: () => void }) {
  return <section className="catalog-page" aria-labelledby="catalog-title"><div className="catalog-heading"><div><p className="eyebrow">VOCALITY.AI CATALOG</p><h1 id="catalog-title">Browse audiobooks</h1></div><button className="button primary" onClick={onUpload}><Upload size={17} /> Upload a book</button></div><label className="catalog-search" htmlFor="catalog-search">Search by title or author<input id="catalog-search" type="search" value={search} onChange={event => onSearch(event.target.value)} /></label><div className="catalog-grid">{items.length ? items.map(item => <article className="catalog-card" key={item.id}><h2>Audiobook</h2><p className="muted">Language: {item.language.toUpperCase()} · Voice: {item.voice_id}</p><button className="button primary" onClick={() => onOpen(item.book_id)}>Open audiobook</button></article>) : <p className="muted">No ready audiobooks match your search.</p>}</div></section>;
}

function AccountPage({ route, user, onNavigate, onSignOut }: { route: string; user: User; onNavigate: (path: string) => void; onSignOut: () => void }) {
  const heading = route === "/library" ? "My Library" : route === "/favorites" ? "Favorites" : "Account";
  return <div className="account-page"><h1>{heading}</h1>{route === "/account" ? <><p>{user.email}</p><p className="muted">Your account keeps your library and listening progress private.</p></> : <p className="muted">Your {route === "/library" ? "saved audiobooks" : "favorite audiobooks"} will appear here.</p>}<div className="account-actions"><button className="button primary" onClick={() => onNavigate("/catalog")}><Upload size={17} /> Upload a book</button><button className="button secondary" onClick={() => onNavigate("/catalog")}>Browse catalog</button><button className="button secondary" onClick={onSignOut}>Logout</button></div></div>;
}
