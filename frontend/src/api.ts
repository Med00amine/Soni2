import type { AuthResponse, Audiobook, Book, CatalogPage, GenerationJob, Progress, Recommendation, User, Voice } from "./types";

const baseUrl = (import.meta.env.VITE_API_URL ?? "/api").replace(/\/$/, "");
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem("vocality-token");
  const response = await fetch(`${baseUrl}${path}`, { ...init, headers: { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init?.headers } });
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status})`);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const api = {
  listBooks: () => request<Book[]>("/books"),
  getBook: (id: string) => request<Book>(`/books/${encodeURIComponent(id)}`),
  uploadBook: (file: File) => { const body = new FormData(); body.append("file", file); return request<Book>("/books", { method: "POST", body }); },
  voices: () => request<Voice[]>("/voices"),
  generate: (bookId: string, voiceId?: string) => request<GenerationJob>(`/books/${encodeURIComponent(bookId)}/generate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ voice_id: voiceId || "ES", engine: "production" }) }),
  job: (id: string) => request<GenerationJob>(`/jobs/${encodeURIComponent(id)}`),
  getText: async (bookId: string) => {
    const result = await request<{ book_id: string; sentences: Array<{ id: string; chapter_id: string; text: string; audio_file?: string; start?: number; end?: number }> }>(`/books/${encodeURIComponent(bookId)}/text`);
    return { ...result, sentences: result.sentences.map(sentence => ({ ...sentence, audioUrl: sentence.audio_file ? `${baseUrl}/books/${encodeURIComponent(bookId)}/audio/${encodeURIComponent(sentence.audio_file)}` : undefined })) };
  },
  downloadDaisy: (bookId: string) => `${baseUrl}/books/${encodeURIComponent(bookId)}/download`,
  register: (email: string, password: string, displayName: string) => request<AuthResponse>("/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password, display_name: displayName }) }),
  login: (email: string, password: string) => request<AuthResponse>("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) }),
  me: () => request<User>("/auth/me"),
  catalog: (search = "") => request<CatalogPage>(`/catalog/audiobooks?search=${encodeURIComponent(search)}`),
  library: () => request<Audiobook[]>("/library"),
  favorites: () => request<Audiobook[]>("/favorites"),
  addLibrary: (id: string) => request<void>(`/library/${encodeURIComponent(id)}`, { method: "POST" }),
  removeLibrary: (id: string) => request<void>(`/library/${encodeURIComponent(id)}`, { method: "DELETE" }),
  addFavorite: (id: string) => request<void>(`/favorites/${encodeURIComponent(id)}`, { method: "POST" }),
  recommendations: () => request<Recommendation[]>("/recommendations"),
  getProgress: (id: string) => request<Progress | null>(`/audiobooks/${encodeURIComponent(id)}/progress`),
  saveProgress: (id: string, progress: Omit<Progress, "audiobook_id" | "updated_at">) => request<Progress>(`/audiobooks/${encodeURIComponent(id)}/progress`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_sentence_id: progress.current_sentence_id, position_seconds: progress.position_seconds, completed: progress.completed }) }),
};
