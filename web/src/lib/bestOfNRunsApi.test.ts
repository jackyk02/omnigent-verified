import { describe, expect, it } from "vitest";
import { bestOfNProposalFromWire, bestOfNRunFromWire } from "./bestOfNRunsApi";

// A wire payload shaped exactly like the server's GET /v1/best-of-n/runs/{id}
// snapshot, exercising both live (nulls) and settled (filled) fields.
const WIRE_RUN = {
  id: "abc123",
  object: "best_of_n_run" as const,
  status: "proposing" as const,
  prompt: "Fix the flaky retry test",
  repo_path: "/srv/repo",
  created_at: 1723000000,
  finished_at: null,
  proposals: [
    {
      harness: "codex",
      index: 1,
      model: "deepseek/deepseek-v4-flash",
      branch: "best-of-n/abc123/codex-0",
      status: "running" as const,
      transcript: "thinking…",
      diff: "",
      diff_chars: 0,
      duration_s: null,
      error: null,
      score: null,
      progress: 0.35,
      progress_history: [0.2, 0.35],
    },
    {
      harness: "claude",
      index: 0,
      model: "deepseek/deepseek-v4-flash",
      branch: "best-of-n/abc123/claude-0",
      status: "ok" as const,
      transcript: "done.",
      diff: "diff --git a/x b/x",
      diff_chars: 1636,
      duration_s: 28.1,
      error: null,
      score: 0.507,
      progress: 0.82,
      progress_history: [0.4, 0.61, 0.82],
    },
  ],
  ranking: null,
  winner_index: null,
  n_comparisons: null,
  merge_commit: null,
  error: null,
};

describe("bestOfNProposalFromWire", () => {
  it("converts snake_case wire fields to the camelCase surface", () => {
    const proposal = bestOfNProposalFromWire(WIRE_RUN.proposals[1]);
    expect(proposal).toEqual({
      harness: "claude",
      index: 0,
      model: "deepseek/deepseek-v4-flash",
      branch: "best-of-n/abc123/claude-0",
      status: "ok",
      transcript: "done.",
      diff: "diff --git a/x b/x",
      diffChars: 1636,
      durationS: 28.1,
      error: null,
      score: 0.507,
      progress: 0.82,
      progressHistory: [0.4, 0.61, 0.82],
    });
  });

  it("defaults absent transcript/diff fields (list snapshots strip them)", () => {
    const proposal = bestOfNProposalFromWire({
      harness: "pi",
      index: 2,
      model: "m",
      branch: "b",
      status: "pending",
    });
    expect(proposal.transcript).toBe("");
    expect(proposal.diff).toBe("");
    expect(proposal.diffChars).toBe(0);
    expect(proposal.durationS).toBeNull();
    expect(proposal.error).toBeNull();
    expect(proposal.score).toBeNull();
    // Progress fields may be omitted too — surfaced as null / empty.
    expect(proposal.progress).toBeNull();
    expect(proposal.progressHistory).toEqual([]);
  });
});

describe("bestOfNRunFromWire", () => {
  it("converts the run envelope and sorts proposals by index", () => {
    const run = bestOfNRunFromWire(WIRE_RUN);
    expect(run.id).toBe("abc123");
    expect(run.status).toBe("proposing");
    expect(run.repoPath).toBe("/srv/repo");
    expect(run.createdAt).toBe(1723000000);
    expect(run.finishedAt).toBeNull();
    expect(run.ranking).toBeNull();
    expect(run.winnerIndex).toBeNull();
    expect(run.nComparisons).toBeNull();
    expect(run.mergeCommit).toBeNull();
    expect(run.error).toBeNull();
    // Wire order was [codex(1), claude(0)] — the surface is index order.
    expect(run.proposals.map((p) => p.index)).toEqual([0, 1]);
    expect(run.proposals.map((p) => p.harness)).toEqual(["claude", "codex"]);
  });

  it("carries a settled run's ranking, winner, and merge commit through", () => {
    const run = bestOfNRunFromWire({
      ...WIRE_RUN,
      status: "done",
      finished_at: 1723000123,
      ranking: [1, 0],
      winner_index: 1,
      n_comparisons: 6,
      merge_commit: "231ac34def",
    });
    expect(run.status).toBe("done");
    expect(run.finishedAt).toBe(1723000123);
    expect(run.ranking).toEqual([1, 0]);
    expect(run.winnerIndex).toBe(1);
    expect(run.nComparisons).toBe(6);
    expect(run.mergeCommit).toBe("231ac34def");
  });
});
