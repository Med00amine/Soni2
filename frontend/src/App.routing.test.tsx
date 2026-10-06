import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

let root: Root;

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) };
}

function renderAt(path: string) {
  window.history.replaceState({}, "", path);
  root = createRoot(document.getElementById("root")!);
  act(() => root.render(<App />));
}

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = '<div id="root"></div>';
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/voices")) return Promise.resolve(response([]));
    if (path.endsWith("/catalog/audiobooks")) return Promise.resolve(response({ items: [], page: 1, page_size: 20, total: 0 }));
    if (path.endsWith("/books/example")) return Promise.resolve(response({ id: "example", title: "Example book", chapters: [] }));
    if (path.endsWith("/books/example/text")) return Promise.resolve(response({ book_id: "example", sentences: [] }));
    return Promise.resolve(response({ detail: "Not authenticated" }, false, 401));
  }));
});

afterEach(() => {
  root?.unmount();
  vi.unstubAllGlobals();
});

describe("frontend routes", () => {
  it("renders login independently from the audiobook workspace", async () => {
    renderAt("/auth/login");
    await act(async () => {});
    expect(document.querySelector("form")).not.toBeNull();
    expect(document.querySelector("h1")?.textContent).toBe("Sign in");
    expect(document.querySelector(".player-panel")).toBeNull();
    expect(document.body.textContent).not.toContain("AUDIOBOOK WORKSPACE");
  });

  it("renders registration independently and keeps the form accessible", async () => {
    renderAt("/auth/register");
    await act(async () => {});
    expect(document.querySelector("#display-name")?.getAttribute("autocomplete")).toBe("name");
    expect(document.querySelector("#email")?.getAttribute("autocomplete")).toBe("email");
    expect(document.querySelector("#password")?.getAttribute("autocomplete")).toBe("new-password");
    expect(document.querySelector("button[type=submit]")?.textContent).toContain("Create account");
  });

  it("shows public navigation without putting authentication in the audiobook workspace", async () => {
    renderAt("/catalog");
    await act(async () => {});
    expect(document.body.textContent).toContain("Sign in");
    expect(document.body.textContent).toContain("Create account");
    expect(document.querySelector(".auth-card")).toBeNull();
  });

  it("renders a separate home menu at the root route", async () => {
    renderAt("/");
    await act(async () => {});
    expect(document.querySelector("#home-title")?.textContent).toContain("Accessible audiobooks");
    expect(document.body.textContent).toContain("Explore audiobooks");
    expect(document.body.textContent).toContain("Upload a book");
    expect(document.body.textContent).not.toContain("Browse audiobooks");
  });

  it("keeps the audiobook workspace focused on the book", async () => {
    renderAt("/audiobooks/example");
    await act(async () => {});
    expect(document.body.textContent).toContain("AUDIOBOOK WORKSPACE");
    expect(document.body.textContent).toContain("Example book");
    expect(document.querySelector(".auth-card")).toBeNull();
    expect(document.querySelector("form")).toBeNull();
    expect(document.body.textContent).not.toContain("Sign in");
    expect(document.body.textContent).not.toContain("Create account");
    expect(document.querySelector('input[aria-label="Audio progress"]')).not.toBeNull();
  });

  it("shows authenticated navigation after loading the current user", async () => {
    localStorage.setItem("vocality-token", "valid-token");
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/me")) return Promise.resolve(response({ id: "u1", email: "reader@example.com", display_name: "Reader", created_at: "2026-01-01T00:00:00Z" }));
      if (path.endsWith("/voices")) return Promise.resolve(response([]));
      if (path.endsWith("/catalog/audiobooks")) return Promise.resolve(response({ items: [], page: 1, page_size: 20, total: 0 }));
      return Promise.resolve(response({ detail: "Not found" }, false, 404));
    }));
    renderAt("/catalog");
    await act(async () => {});
    expect(document.body.textContent).toContain("My Library");
    expect(document.body.textContent).toContain("Favorites");
    expect(document.body.textContent).toContain("Reader");
    expect(document.body.textContent).not.toContain("Create your account");
  });
});
