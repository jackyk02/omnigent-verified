// Persisted, app-global memory of recently-used Best-of-N repository paths.
//
// Mirrors baseBranchPreferences: the run page's launch form prefills the
// Repository field with the most recent path and offers the rest as one-click
// chips, so repeat runs against the same repos skip the retyping. Stored as a
// JSON string array, most-recent first, capped and deduped on write; reads
// are defensive (invalid JSON / wrong shapes / storage errors → empty).

const STORAGE_KEY = "codify:best-of-n-recent-repos";

/** Most recent distinct repo paths kept (and rendered as chips). */
export const MAX_RECENT_REPO_PATHS = 5;

/**
 * Read the recently-used repo paths, most-recent first. Returns `[]` when
 * nothing is stored, on a server render (no `window`), on unparsable/hand-
 * edited values, or when storage is inaccessible — never throws. Entries are
 * trimmed, blanks dropped, deduped, and capped on read so a stale or edited
 * value can't render un-normalized.
 */
export function readRecentRepoPaths(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) return [];
    const parsed: unknown = JSON.parse(stored);
    if (!Array.isArray(parsed)) return [];
    return normalize(parsed.filter((entry): entry is string => typeof entry === "string"));
  } catch {
    return [];
  }
}

/**
 * Record `path` as the most recently used repo. Moves an already-known path
 * to the front (no duplicates) and drops the oldest beyond the cap. A blank
 * path is ignored. Swallows quota/access errors so a failed write can't
 * break launching a run.
 */
export function pushRecentRepoPath(path: string): void {
  if (typeof window === "undefined") return;
  const trimmed = path.trim();
  if (trimmed === "") return;
  try {
    const next = normalize([trimmed, ...readRecentRepoPaths()]);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // localStorage quota or access errors shouldn't break run launch.
  }
}

/** Trim, drop blanks, dedupe (first occurrence wins), cap. */
function normalize(paths: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const raw of paths) {
    const path = raw.trim();
    if (path === "" || seen.has(path)) continue;
    seen.add(path);
    result.push(path);
    if (result.length >= MAX_RECENT_REPO_PATHS) break;
  }
  return result;
}
