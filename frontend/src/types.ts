export interface Book { id: string; title: string; author?: string; language?: string; chapters: Chapter[]; durationSeconds?: number; status?: "draft" | "ready" | "generating" | "error"; }
export interface Chapter { id: string; title: string; order: number; paragraphs?: Paragraph[]; audioUrl?: string; durationSeconds?: number; }
export interface Paragraph { id: string; text: string; sentences?: Sentence[]; }
export interface Sentence { id: string; chapter_id: string; text: string; start?: number; end?: number; audioUrl?: string; audio_file?: string; }
export interface Voice { id: string; name: string; engine: string; language: string; }
export interface GenerationJob { id: string; bookId: string; status: "queued" | "running" | "completed" | "failed"; progress: number; message?: string; }
export interface PlayerState { playing: boolean; chapterIndex: number; position: number; rate: number; }
export interface UserSettings { theme: "light" | "dark" | "system"; fontSize: "small" | "medium" | "large"; highContrast: boolean; reduceMotion: boolean; }
