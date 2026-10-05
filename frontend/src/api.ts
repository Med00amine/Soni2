import type { Book, GenerationJob, Voice } from "./types";

const baseUrl = (import.meta.env.VITE_API_URL ?? "/api").replace(/\/$/, "");
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, { ...init, headers: { Accept: "application/json", ...init?.headers } });
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status})`);
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
};
