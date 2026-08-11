import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_RECENT_REPO_PATHS,
  pushRecentRepoPath,
  readRecentRepoPaths,
} from "./bestOfNPreferences";

const STORAGE_KEY = "codify:best-of-n-recent-repos";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("bestOfNPreferences", () => {
  it("returns an empty list when nothing is stored", () => {
    expect(readRecentRepoPaths()).toEqual([]);
  });

  it("round-trips pushed paths most-recent first", () => {
    pushRecentRepoPath("/repo/a");
    pushRecentRepoPath("/repo/b");
    // The launch form prefills from index 0, so newest must lead.
    expect(readRecentRepoPaths()).toEqual(["/repo/b", "/repo/a"]);
  });

  it("dedupes: re-pushing a known path moves it to the front", () => {
    pushRecentRepoPath("/repo/a");
    pushRecentRepoPath("/repo/b");
    pushRecentRepoPath("/repo/a");
    expect(readRecentRepoPaths()).toEqual(["/repo/a", "/repo/b"]);
  });

  it("caps the list at the max, dropping the oldest", () => {
    for (let i = 1; i <= MAX_RECENT_REPO_PATHS + 2; i++) {
      pushRecentRepoPath(`/repo/${i}`);
    }
    const paths = readRecentRepoPaths();
    expect(paths).toHaveLength(MAX_RECENT_REPO_PATHS);
    // Newest first; the two oldest fell off the end.
    expect(paths[0]).toBe(`/repo/${MAX_RECENT_REPO_PATHS + 2}`);
    expect(paths).not.toContain("/repo/1");
    expect(paths).not.toContain("/repo/2");
  });

  it("trims pushed paths and ignores blanks", () => {
    pushRecentRepoPath("  /repo/a  ");
    pushRecentRepoPath("   ");
    expect(readRecentRepoPaths()).toEqual(["/repo/a"]);
  });

  it("normalizes a hand-edited stored value on read", () => {
    // Duplicates, blanks, non-strings, and overflow must all wash out.
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(["/a", " /a ", "", 7, "/b", "/c", "/d", "/e", "/f", "/g"]),
    );
    expect(readRecentRepoPaths()).toEqual(["/a", "/b", "/c", "/d", "/e"]);
  });

  it("returns an empty list for unparsable or wrong-shaped storage", () => {
    localStorage.setItem(STORAGE_KEY, "not json");
    expect(readRecentRepoPaths()).toEqual([]);
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ nope: true }));
    expect(readRecentRepoPaths()).toEqual([]);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => pushRecentRepoPath("/repo/a")).not.toThrow();
    expect(readRecentRepoPaths()).toEqual([]);
  });
});
