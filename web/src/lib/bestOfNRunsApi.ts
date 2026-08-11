// Typed client for the `/v1/best-of-n/runs` endpoints — starting a
// Best-of-N run, polling its live snapshot, and listing past runs.
//
// Naming: TS surface is camelCase; the wire is snake_case. The converters
// below translate at the boundary so callers never see raw wire fields
// (same contract as `bestOfNApi.ts` for the config endpoints).

import { authenticatedFetch } from "./identity";

/** Lifecycle of a whole Best-of-N run. */
export type BestOfNRunStatus =
  "preparing" | "proposing" | "scoring" | "merging" | "done" | "failed";

/** Lifecycle of one harness proposal inside a run. */
export type BestOfNProposalStatus = "pending" | "running" | "ok" | "failed";

/** One harness's proposal: a live transcript tail while running, then a diff + score. */
export interface BestOfNProposal {
  /** Harness slug, e.g. `"claude"` / `"codex"` / `"pi"`. */
  harness: string;
  /** Position in the run's proposal list (stable across polls). */
  index: number;
  model: string;
  /** Git branch the proposal was written to. */
  branch: string;
  status: BestOfNProposalStatus;
  /** Streaming transcript tail (up to ~20k chars). Empty until output arrives. */
  transcript: string;
  /** The proposal's final diff; empty until the harness finishes. */
  diff: string;
  diffChars: number;
  /** Wall-clock seconds; `null` until the proposal finishes. */
  durationS: number | null;
  error: string | null;
  /** Mean tournament preference in [0, 1]; populated once the run is done. */
  score: number | null;
  /**
   * Latest online verifier progress estimate in [0, 1] — how close the
   * verifier currently thinks this harness is to satisfying the task.
   * Sampled every ~20s while running (plus one final post-finish sample
   * that includes the diff); `null` until the first sample lands.
   */
  progress: number | null;
  /** All progress samples so far, oldest first. Empty until the first sample. */
  progressHistory: number[];
}

/** Live snapshot of a Best-of-N run. */
export interface BestOfNRun {
  id: string;
  status: BestOfNRunStatus;
  prompt: string;
  repoPath: string;
  createdAt: number;
  finishedAt: number | null;
  proposals: BestOfNProposal[];
  /** Proposal indices best-first once scored; `null` before scoring. */
  ranking: number[] | null;
  winnerIndex: number | null;
  nComparisons: number | null;
  mergeCommit: string | null;
  error: string | null;
}

// ── Wire shapes ──────────────────────────────────────────────────────────────

interface BestOfNProposalWire {
  harness: string;
  index: number;
  model: string;
  branch: string;
  status: BestOfNProposalStatus;
  // Absent in list snapshots (the list endpoint strips transcript/diff).
  transcript?: string | null;
  diff?: string | null;
  diff_chars?: number | null;
  duration_s?: number | null;
  error?: string | null;
  score?: number | null;
  progress?: number | null;
  progress_history?: number[] | null;
}

interface BestOfNRunWire {
  id: string;
  object: "best_of_n_run";
  status: BestOfNRunStatus;
  prompt: string;
  repo_path: string;
  created_at: number;
  finished_at?: number | null;
  proposals: BestOfNProposalWire[];
  ranking?: number[] | null;
  winner_index?: number | null;
  n_comparisons?: number | null;
  merge_commit?: string | null;
  error?: string | null;
}

interface BestOfNRunListWire {
  object: "list";
  data: BestOfNRunWire[];
}

// ── Converters (exported for tests) ──────────────────────────────────────────

export function bestOfNProposalFromWire(wire: BestOfNProposalWire): BestOfNProposal {
  return {
    harness: wire.harness,
    index: wire.index,
    model: wire.model,
    branch: wire.branch,
    status: wire.status,
    transcript: wire.transcript ?? "",
    diff: wire.diff ?? "",
    diffChars: wire.diff_chars ?? 0,
    durationS: wire.duration_s ?? null,
    error: wire.error ?? null,
    score: wire.score ?? null,
    progress: wire.progress ?? null,
    progressHistory: wire.progress_history ?? [],
  };
}

export function bestOfNRunFromWire(wire: BestOfNRunWire): BestOfNRun {
  return {
    id: wire.id,
    status: wire.status,
    prompt: wire.prompt,
    repoPath: wire.repo_path,
    createdAt: wire.created_at,
    finishedAt: wire.finished_at ?? null,
    // Render order is index order regardless of wire order.
    proposals: wire.proposals.map(bestOfNProposalFromWire).sort((a, b) => a.index - b.index),
    ranking: wire.ranking ?? null,
    winnerIndex: wire.winner_index ?? null,
    nComparisons: wire.n_comparisons ?? null,
    mergeCommit: wire.merge_commit ?? null,
    error: wire.error ?? null,
  };
}

// ── Requests ─────────────────────────────────────────────────────────────────

/** Surface the server's message ("dirty tree", "not a git repo", …) over the status line. */
async function readRunOrThrow(res: Response): Promise<BestOfNRunWire> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as {
        detail?: string;
        error?: { message?: string };
      };
      if (body.error?.message) message = body.error.message;
      else if (typeof body.detail === "string" && body.detail) message = body.detail;
    } catch {
      // Non-JSON body — keep the status-line fallback.
    }
    throw new Error(message);
  }
  return (await res.json()) as BestOfNRunWire;
}

/** POST /v1/best-of-n/runs — start a run; resolves to its initial snapshot. */
export async function startBestOfNRun(params: {
  prompt: string;
  repoPath: string;
}): Promise<BestOfNRun> {
  const res = await authenticatedFetch("/v1/best-of-n/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: params.prompt, repo_path: params.repoPath }),
  });
  return bestOfNRunFromWire(await readRunOrThrow(res));
}

/** GET /v1/best-of-n/runs/{id} — the run's live snapshot. */
export async function fetchBestOfNRun(id: string): Promise<BestOfNRun> {
  const res = await authenticatedFetch(`/v1/best-of-n/runs/${encodeURIComponent(id)}`);
  return bestOfNRunFromWire(await readRunOrThrow(res));
}

/** GET /v1/best-of-n/runs — all runs (snapshots without transcript/diff). */
export async function fetchBestOfNRuns(): Promise<BestOfNRun[]> {
  const res = await authenticatedFetch("/v1/best-of-n/runs");
  if (!res.ok) {
    // The list endpoint has no field-level validation; the status line is enough.
    throw new Error(`${res.status} ${res.statusText}`);
  }
  const body = (await res.json()) as BestOfNRunListWire;
  return body.data.map(bestOfNRunFromWire);
}
