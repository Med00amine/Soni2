import { describe, expect, it } from "vitest";
import type { Book } from "./types";

describe("frontend domain types", () => {
  it("represents an ordered, readable book", () => {
    const book: Book = { id: "book-1", title: "A book", chapters: [{ id: "one", title: "One", order: 1, paragraphs: [{ id: "p", text: "Text" }] }] };
    expect(book.chapters[0].paragraphs?.[0]?.text).toBe("Text");
    expect(book.chapters[0].order).toBe(1);
  });
});
