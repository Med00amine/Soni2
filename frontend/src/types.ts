export interface Book { id: string; title: string; author?: string; language?: string; source_format?: string; source_filename?: string; chapter_count?: number; sentence_count?: number; chapters: Chapter[]; durationSeconds?: number; status?: "draft" | "ready" | "generating" | "error"; }
export interface Chapter { id: string; title: string; order: number; paragraphs?: Paragraph[]; audioUrl?: string; durationSeconds?: number; }
export interface Paragraph { id: string; text: string; sentences?: Sentence[]; }
export interface Sentence { id: string; chapter_id: string; text: string; start?: number; end?: number; audioUrl?: string; audio_file?: string; }
export interface Voice { id: string; name: string; engine: string; language: string; }
export interface GenerationJob { id: string; bookId: string; audiobook_id?: string; status: "queued" | "running" | "completed" | "failed"; progress: number; message?: string; }
export interface PlayerState { playing: boolean; chapterIndex: number; position: number; rate: number; }
export interface UserSettings { theme: "light" | "dark" | "system"; fontSize: "small" | "medium" | "large"; highContrast: boolean; reduceMotion: boolean; }
export interface User { id: string; email: string; display_name: string; created_at: string; }
export interface AuthResponse { access_token: string; token_type: string; user: User; }
export interface Audiobook { id: string; book_id: string; engine: string; voice_id: string; language: string; status: string; created_at: string; }
export interface CatalogPage { items: Audiobook[]; page: number; page_size: number; total: number; }
export interface Recommendation extends Audiobook { reason: string; }
export interface Progress { audiobook_id: string; current_sentence_id?: string; position_seconds: number; completed: boolean; updated_at: string; }
