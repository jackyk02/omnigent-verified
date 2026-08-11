// Render tests for the Best-of-N run screen. The query/mutation hooks are
// mocked at their module seam (same approach as BestOfNSettings.test.tsx) so
// no QueryClient or network is involved; the form → run-view switch and the
// pane/banner rendering under test are the component's own.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BestOfNRunPage } from "./BestOfNRunPage";
import type { BestOfNConfigResponse } from "@/lib/bestOfNApi";
import type { BestOfNRun } from "@/lib/bestOfNRunsApi";
import * as hooks from "@/hooks/useBestOfNRun";
import * as configHooks from "@/hooks/useBestOfNConfig";
import { showToast } from "@/components/ui/toast";

vi.mock("@/hooks/useBestOfNRun", () => ({
  useStartBestOfNRun: vi.fn(),
  useBestOfNRun: vi.fn(),
  useBestOfNRuns: vi.fn(),
}));

vi.mock("@/hooks/useBestOfNConfig", () => ({
  useBestOfNConfig: vi.fn(),
}));

// The page fires the shared toast on copy; mock the seam so tests assert the
// call instead of racing the window-event plumbing.
vi.mock("@/components/ui/toast", () => ({
  showToast: vi.fn(),
}));

function proposingRun(): BestOfNRun {
  return {
    id: "abc123",
    status: "proposing",
    prompt: "Fix the flaky retry test",
    repoPath: "/srv/repo",
    createdAt: 1723000000,
    finishedAt: null,
    proposals: [
      {
        harness: "claude",
        index: 0,
        model: "deepseek/deepseek-v4-flash",
        branch: "best-of-n/abc123/claude-0",
        status: "running",
        transcript: "Reading retry.test.ts…",
        diff: "",
        diffChars: 0,
        durationS: null,
        error: null,
        score: null,
        progress: 0.35,
        progressHistory: [0.2, 0.35],
      },
      {
        harness: "codex",
        index: 1,
        model: "deepseek/deepseek-v4-flash",
        branch: "best-of-n/abc123/codex-0",
        status: "running",
        transcript: "Planning…",
        diff: "",
        diffChars: 0,
        durationS: null,
        error: null,
        score: null,
        progress: null,
        progressHistory: [],
      },
      {
        harness: "pi",
        index: 2,
        model: "deepseek/deepseek-v4-flash",
        branch: "best-of-n/abc123/pi-0",
        status: "pending",
        transcript: "",
        diff: "",
        diffChars: 0,
        durationS: null,
        error: null,
        score: null,
        progress: null,
        progressHistory: [],
      },
    ],
    ranking: null,
    winnerIndex: null,
    nComparisons: null,
    mergeCommit: null,
    error: null,
  };
}

function doneRun(): BestOfNRun {
  const run = proposingRun();
  run.status = "done";
  run.finishedAt = 1723000456;
  run.ranking = [1, 0, 2];
  run.winnerIndex = 1;
  run.nComparisons = 6;
  run.mergeCommit = "231ac34def567";
  run.proposals = run.proposals.map((p, i) => ({
    ...p,
    status: "ok" as const,
    transcript: `${p.harness} transcript`,
    diff: `diff --git ${p.harness}`,
    diffChars: 1636 - i,
    durationS: 28.1 + i,
    score: [0.4, 0.507, 0.3][i],
    progress: [0.55, 0.9, 0.42][i],
    progressHistory: [0.3, [0.55, 0.9, 0.42][i]],
  }));
  return run;
}

const mutate = vi.fn();

function mockStart(startedRun: BestOfNRun | null = null) {
  mutate.mockReset();
  if (startedRun) {
    mutate.mockImplementation((_vars: unknown, opts?: { onSuccess?: (run: BestOfNRun) => void }) =>
      opts?.onSuccess?.(startedRun),
    );
  }
  vi.mocked(hooks.useStartBestOfNRun).mockReturnValue({
    mutate,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useStartBestOfNRun>);
}

function mockRun(run: BestOfNRun | null) {
  vi.mocked(hooks.useBestOfNRun).mockReturnValue({
    data: run ?? undefined,
    isPending: run === null,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof hooks.useBestOfNRun>);
}

function mockRunsList(runs: BestOfNRun[]) {
  vi.mocked(hooks.useBestOfNRuns).mockReturnValue({
    data: runs,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useBestOfNRuns>);
}

/** Build a config response for the readiness preflight. */
function configResponse(overrides?: {
  enabled?: string[];
  availability?: Record<string, boolean>;
  keyPresent?: boolean;
  commands?: Record<string, string>;
}): BestOfNConfigResponse {
  const enabled = overrides?.enabled ?? ["claude", "codex"];
  return {
    config: {
      proposers: ["claude", "codex", "pi"].map((harness) => ({
        harness,
        enabled: enabled.includes(harness),
        count: 1,
        model: null,
        command: overrides?.commands?.[harness] ?? null,
        env: {},
      })),
      proposalModel: "deepseek-v4-flash",
      criteria: { Correctness: "..." },
      verifier: {
        model: "deepseek-v4-flash",
        baseUrl: "https://api.deepseek.com/v1",
        apiKeyEnv: "DEEPSEEK_API_KEY",
        nEvaluations: 4,
        pivots: 2,
        seed: 0,
        maxWorkers: 16,
        onError: "tie",
      },
      proposalTimeoutS: 900,
      keepProposalBranches: false,
    },
    knownHarnesses: ["claude", "codex", "pi"],
    harnessAvailability: overrides?.availability ?? { claude: true, codex: true, pi: true },
    verifierKeyPresent: overrides?.keyPresent ?? true,
    defaultCriteria: { Correctness: "..." },
  };
}

function mockConfig(response: BestOfNConfigResponse | null) {
  vi.mocked(configHooks.useBestOfNConfig).mockReturnValue({
    data: response ?? undefined,
    isPending: response === null,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof configHooks.useBestOfNConfig>);
}

const clipboardWriteText = vi.fn();

function renderPage(state?: { autoStart?: { prompt: string; repoPath: string } }) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/best-of-n", state: state ?? null }]}>
      <BestOfNRunPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockStart();
  mockRun(null);
  mockRunsList([]);
  mockConfig(configResponse());
  clipboardWriteText.mockReset();
  clipboardWriteText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: clipboardWriteText },
    configurable: true,
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

/** Fill the form and submit, flipping the page into the run view. */
function launch(run: BestOfNRun) {
  mockStart(run);
  mockRun(run);
  fireEvent.change(screen.getByTestId("best-of-n-run-prompt"), {
    target: { value: "Fix the flaky retry test" },
  });
  fireEvent.change(screen.getByTestId("best-of-n-run-repo"), {
    target: { value: "/srv/repo" },
  });
  fireEvent.click(screen.getByTestId("best-of-n-run-start"));
}

describe("BestOfNRunPage", () => {
  it("shows the launch form initially, with the start button disabled while empty", () => {
    renderPage();
    expect(screen.getByTestId("best-of-n-run-prompt")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-run-repo")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-run-start")).toBeDisabled();
    expect(screen.queryByTestId("best-of-n-run-view")).not.toBeInTheDocument();
  });

  it("starts a run and renders three live panes from a proposing snapshot", () => {
    renderPage();
    launch(proposingRun());

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({
      prompt: "Fix the flaky retry test",
      repoPath: "/srv/repo",
    });

    expect(screen.getByTestId("best-of-n-run-view")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-run-status")).toHaveTextContent("Proposing…");

    // One pane per proposal, index order, harness display names.
    const panes = [0, 1, 2].map((i) => screen.getByTestId(`best-of-n-pane-${i}`));
    expect(within(panes[0]).getByText("Claude Code")).toBeInTheDocument();
    expect(within(panes[1]).getByText("Codex")).toBeInTheDocument();
    expect(within(panes[2]).getByText("Pi")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-pane-0-transcript")).toHaveTextContent(
      "Reading retry.test.ts…",
    );
    // No winner emphasis while proposing.
    expect(panes[1]).not.toHaveAttribute("data-winner");
    expect(screen.queryByTestId("best-of-n-run-banner")).not.toBeInTheDocument();

    // Verifier progress: Claude has a sample → 35% bar + label; Codex is
    // running with no sample yet → empty track + warming-up microcopy; Pi is
    // still pending → empty track, no microcopy.
    expect(screen.getByTestId("best-of-n-pane-0-progress-fill")).toHaveStyle({ width: "35%" });
    expect(screen.getByTestId("best-of-n-pane-0-progress-label")).toHaveTextContent("35%");
    expect(screen.getByTestId("best-of-n-pane-1-progress-fill")).toHaveStyle({ width: "0%" });
    expect(screen.getByTestId("best-of-n-pane-1-progress-warming")).toHaveTextContent(
      "verifier warming up…",
    );
    expect(screen.queryByTestId("best-of-n-pane-2-progress-warming")).not.toBeInTheDocument();
    expect(screen.queryByTestId("best-of-n-pane-2-progress-label")).not.toBeInTheDocument();
  });

  it("highlights the winner and shows the result banner when the run is done", () => {
    renderPage();
    launch(doneRun());

    expect(screen.getByTestId("best-of-n-run-status")).toHaveTextContent("Done");

    // Winner pane (index 1, Codex) gets the highlight + merged badge + score.
    const winnerPane = screen.getByTestId("best-of-n-pane-1");
    expect(winnerPane).toHaveAttribute("data-winner", "true");
    expect(screen.getByTestId("best-of-n-pane-1-merged")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-pane-1-score")).toHaveTextContent("0.507");
    expect(screen.getByTestId("best-of-n-pane-0")).not.toHaveAttribute("data-winner");

    // Banner: winner line, comparisons, merge short-sha, and the score bars.
    const banner = screen.getByTestId("best-of-n-run-banner");
    expect(banner).toHaveTextContent("Winner: Codex — score 0.507 over 6 comparisons");
    expect(screen.getByTestId("best-of-n-merge-commit")).toHaveTextContent("231ac34d");
    expect(screen.getByTestId("best-of-n-score-bars")).toBeInTheDocument();

    // The winning diff section is present (collapsed by default).
    expect(screen.getByTestId("best-of-n-winning-diff-toggle")).toBeInTheDocument();

    // Progress bars settle at the final sample; only the winner's fill flips
    // to the success tone, and no pane shows the warming-up microcopy.
    expect(screen.getByTestId("best-of-n-pane-1-progress-fill")).toHaveStyle({ width: "90%" });
    expect(screen.getByTestId("best-of-n-pane-1-progress-fill")).toHaveClass("bg-success");
    expect(screen.getByTestId("best-of-n-pane-0-progress-fill")).not.toHaveClass("bg-success");
    expect(screen.queryByTestId("best-of-n-pane-0-progress-warming")).not.toBeInTheDocument();
  });

  it("returns to the launch form via New run", () => {
    renderPage();
    launch(proposingRun());
    fireEvent.click(screen.getByTestId("best-of-n-new-run"));
    expect(screen.getByTestId("best-of-n-run-prompt")).toBeInTheDocument();
    expect(screen.queryByTestId("best-of-n-run-view")).not.toBeInTheDocument();
  });

  it("prefills the repo from the most recent run and fills from a clicked chip", () => {
    localStorage.setItem(
      "codify:best-of-n-recent-repos",
      JSON.stringify(["/repo/newest", "/repo/older"]),
    );
    renderPage();
    const repo = screen.getByTestId("best-of-n-run-repo") as HTMLInputElement;
    // Most-recent path seeds the input; both recents render as chips.
    expect(repo.value).toBe("/repo/newest");
    expect(screen.getByTestId("best-of-n-recent-repo-0")).toHaveTextContent("/repo/newest");
    fireEvent.click(screen.getByTestId("best-of-n-recent-repo-1"));
    expect(repo.value).toBe("/repo/older");
  });

  it("submits on Cmd+Enter in the task textarea when the form is valid", () => {
    mockStart(proposingRun());
    mockRun(proposingRun());
    renderPage();
    const promptBox = screen.getByTestId("best-of-n-run-prompt");
    fireEvent.change(promptBox, { target: { value: "Fix it" } });
    // Invalid (no repo yet): the shortcut must not fire the mutation.
    fireEvent.keyDown(promptBox, { key: "Enter", metaKey: true });
    expect(mutate).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId("best-of-n-run-repo"), { target: { value: "/srv/repo" } });
    fireEvent.keyDown(promptBox, { key: "Enter", metaKey: true });
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("best-of-n-run-view")).toBeInTheDocument();
  });

  it("lists recent runs under the form and opens one on click", () => {
    const past = doneRun();
    mockRunsList([past]);
    renderPage();
    const row = screen.getByTestId(`best-of-n-recent-run-${past.id}`);
    // Row summarizes the run: prompt, repo basename, winner + score.
    expect(row).toHaveTextContent("Fix the flaky retry test");
    expect(row).toHaveTextContent("repo");
    expect(row).toHaveTextContent("Codex");
    expect(row).toHaveTextContent("0.507");
    mockRun(past);
    fireEvent.click(row);
    expect(screen.getByTestId("best-of-n-run-view")).toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-run-status")).toHaveTextContent("Done");
  });

  it("renders no recent-runs chrome when there is no history", () => {
    renderPage();
    expect(screen.queryByTestId("best-of-n-recent-runs")).not.toBeInTheDocument();
  });

  it("Run again returns to the form prefilled without auto-submitting", () => {
    renderPage();
    launch(doneRun());
    mutate.mockClear();
    fireEvent.click(screen.getByTestId("best-of-n-run-again"));
    // Back on the form, seeded with the finished run's prompt + repo.
    expect((screen.getByTestId("best-of-n-run-prompt") as HTMLTextAreaElement).value).toBe(
      "Fix the flaky retry test",
    );
    expect((screen.getByTestId("best-of-n-run-repo") as HTMLInputElement).value).toBe("/srv/repo");
    // Prefill only — nothing was submitted.
    expect(mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("best-of-n-run-view")).not.toBeInTheDocument();
  });

  it("surfaces the server 400 message inline under the form", () => {
    vi.mocked(hooks.useStartBestOfNRun).mockReturnValue({
      mutate,
      isPending: false,
      isError: true,
      error: new Error("repository has uncommitted changes"),
    } as unknown as ReturnType<typeof hooks.useStartBestOfNRun>);
    renderPage();
    expect(screen.getByTestId("best-of-n-run-error")).toHaveTextContent(
      "repository has uncommitted changes",
    );
  });

  it("renders a readiness chip per enabled proposer plus the verifier key", () => {
    renderPage();
    // claude + codex enabled → chips; pi disabled → no chip.
    expect(screen.getByTestId("best-of-n-readiness-claude")).toHaveAttribute("data-available");
    expect(screen.getByTestId("best-of-n-readiness-codex")).toHaveAttribute("data-available");
    expect(screen.queryByTestId("best-of-n-readiness-pi")).not.toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-readiness-verifier-key")).toHaveTextContent("loaded");
    // All ready → no problem sentence, form usable once filled in.
    expect(screen.queryByTestId("best-of-n-preflight-problem")).not.toBeInTheDocument();
  });

  it("marks an unavailable proposer 'not installed' without blocking while one remains", () => {
    mockConfig(configResponse({ availability: { claude: true, codex: false, pi: true } }));
    renderPage();
    expect(screen.getByTestId("best-of-n-readiness-codex")).toHaveTextContent("not installed");
    expect(screen.getByTestId("best-of-n-readiness-codex")).not.toHaveAttribute("data-available");
    // claude still runs → no block.
    expect(screen.queryByTestId("best-of-n-preflight-problem")).not.toBeInTheDocument();
  });

  it("counts a custom-command proposer as available despite a failed CLI probe", () => {
    mockConfig(
      configResponse({
        enabled: ["codex"],
        availability: { claude: true, codex: false, pi: true },
        commands: { codex: "my-codex --yolo" },
      }),
    );
    renderPage();
    expect(screen.getByTestId("best-of-n-readiness-codex")).toHaveAttribute("data-available");
    expect(screen.queryByTestId("best-of-n-preflight-problem")).not.toBeInTheDocument();
  });

  it("disables Run and explains when the verifier key is missing, linking to settings", () => {
    mockConfig(configResponse({ keyPresent: false }));
    renderPage();
    expect(screen.getByTestId("best-of-n-readiness-verifier-key")).toHaveTextContent("missing");
    // Even a fully filled form must not submit.
    fireEvent.change(screen.getByTestId("best-of-n-run-prompt"), { target: { value: "go" } });
    fireEvent.change(screen.getByTestId("best-of-n-run-repo"), { target: { value: "/srv/repo" } });
    expect(screen.getByTestId("best-of-n-run-start")).toBeDisabled();
    const problem = screen.getByTestId("best-of-n-preflight-problem");
    expect(problem).toHaveTextContent("verifier API key is missing");
    expect(screen.getByTestId("best-of-n-preflight-settings-link")).toHaveAttribute(
      "href",
      "/settings/verifier",
    );
  });

  it("disables Run when no enabled proposer is installed", () => {
    mockConfig(configResponse({ availability: { claude: false, codex: false, pi: true } }));
    renderPage();
    fireEvent.change(screen.getByTestId("best-of-n-run-prompt"), { target: { value: "go" } });
    fireEvent.change(screen.getByTestId("best-of-n-run-repo"), { target: { value: "/srv/repo" } });
    expect(screen.getByTestId("best-of-n-run-start")).toBeDisabled();
    expect(screen.getByTestId("best-of-n-preflight-problem")).toHaveTextContent(
      "No enabled proposal harness is installed",
    );
  });

  it("renders no readiness chrome and doesn't block while the config loads", () => {
    mockConfig(null);
    renderPage();
    expect(screen.queryByTestId("best-of-n-readiness")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("best-of-n-run-prompt"), { target: { value: "go" } });
    fireEvent.change(screen.getByTestId("best-of-n-run-repo"), { target: { value: "/srv/repo" } });
    expect(screen.getByTestId("best-of-n-run-start")).not.toBeDisabled();
  });

  it("colors winning-diff lines by prefix", () => {
    const run = doneRun();
    run.proposals[1].diff = "@@ -1,2 +1,2 @@\n+added line\n-removed line\n context line";
    renderPage();
    launch(run);
    fireEvent.click(screen.getByTestId("best-of-n-winning-diff-toggle"));
    const pre = screen.getByTestId("best-of-n-winning-diff");
    expect(within(pre).getByText("+added line")).toHaveClass("text-success");
    expect(within(pre).getByText("-removed line")).toHaveClass("text-destructive");
    expect(within(pre).getByText("@@ -1,2 +1,2 @@")).toHaveClass("text-muted-foreground");
    const context = within(pre).getByText("context line");
    expect(context).not.toHaveClass("text-success");
    expect(context).not.toHaveClass("text-destructive");
    expect(context).not.toHaveClass("text-muted-foreground");
  });

  it("copies the merge sha and the winning diff, toasting Copied", async () => {
    const run = doneRun();
    run.proposals[1].diff = "diff --git full contents";
    renderPage();
    launch(run);

    fireEvent.click(screen.getByTestId("best-of-n-copy-commit"));
    // Full sha, not the truncated display form.
    expect(clipboardWriteText).toHaveBeenCalledWith("231ac34def567");
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Copied"));

    fireEvent.click(screen.getByTestId("best-of-n-copy-diff"));
    expect(clipboardWriteText).toHaveBeenCalledWith("diff --git full contents");
  });

  it("shows example task chips while empty, fills on click, and hides once typed", () => {
    renderPage();
    expect(screen.getByTestId("best-of-n-example-tasks")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("best-of-n-example-task-0"));
    const promptBox = screen.getByTestId("best-of-n-run-prompt") as HTMLTextAreaElement;
    // Click fills the textarea (no submit) and the chips yield to the draft.
    expect(promptBox.value).toBe("Fix the failing test and explain the root cause");
    expect(mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("best-of-n-example-tasks")).not.toBeInTheDocument();
    // Clearing the draft brings the guidance back.
    fireEvent.change(promptBox, { target: { value: "" } });
    expect(screen.getByTestId("best-of-n-example-tasks")).toBeInTheDocument();
  });

  it("links to the verifier settings from the page header", () => {
    renderPage();
    expect(screen.getByTestId("best-of-n-settings-link")).toHaveAttribute(
      "href",
      "/settings/verifier",
    );
  });

  it("exposes verifier progress as accessible progressbars", () => {
    renderPage();
    launch(proposingRun());
    const claude = screen.getByLabelText("Claude Code verifier progress");
    expect(claude).toHaveAttribute("role", "progressbar");
    expect(claude).toHaveAttribute("aria-valuemin", "0");
    expect(claude).toHaveAttribute("aria-valuemax", "100");
    expect(claude).toHaveAttribute("aria-valuenow", "35");
    // No sample yet → no valuenow claim (indeterminate).
    expect(screen.getByLabelText("Codex verifier progress")).not.toHaveAttribute("aria-valuenow");
    // Transcripts are labeled per harness.
    expect(screen.getByLabelText("Claude Code transcript")).toBe(
      screen.getByTestId("best-of-n-pane-0-transcript"),
    );
  });

  it("activates a recent-run row with the Enter key", () => {
    const past = doneRun();
    mockRunsList([past]);
    renderPage();
    const row = screen.getByTestId(`best-of-n-recent-run-${past.id}`);
    mockRun(past);
    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });
    expect(screen.getByTestId("best-of-n-run-view")).toBeInTheDocument();
  });

  it("explains the comparisons count with a tooltip", () => {
    renderPage();
    launch(doneRun());
    expect(screen.getByTestId("best-of-n-comparisons")).toHaveAttribute(
      "title",
      "Pairwise verifier comparisons in the selection tournament",
    );
  });

  it("auto-starts a run from composer handoff state and shows the run view", () => {
    mockStart(proposingRun());
    mockRun(proposingRun());
    renderPage({ autoStart: { prompt: "Fix the flaky retry test", repoPath: "/srv/repo" } });
    // Fired exactly once, straight from the handed-over payload.
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0][0]).toEqual({
      prompt: "Fix the flaky retry test",
      repoPath: "/srv/repo",
    });
    expect(screen.getByTestId("best-of-n-run-view")).toBeInTheDocument();
  });

  it("drops a failed auto-start into the prefilled form with the server error", () => {
    // The start mutation rejects (e.g. dirty tree): mutate fires but never
    // resolves to a run, and the hook reports the failure state.
    vi.mocked(hooks.useStartBestOfNRun).mockReturnValue({
      mutate,
      isPending: false,
      isError: true,
      error: new Error("repository has uncommitted changes"),
    } as unknown as ReturnType<typeof hooks.useStartBestOfNRun>);
    renderPage({ autoStart: { prompt: "Fix it", repoPath: "/srv/dirty" } });
    expect(mutate).toHaveBeenCalledTimes(1);
    // No run to show — the normal form, seeded with the handed-over values,
    // with the inline server error visible.
    expect(screen.queryByTestId("best-of-n-run-view")).not.toBeInTheDocument();
    expect((screen.getByTestId("best-of-n-run-prompt") as HTMLTextAreaElement).value).toBe(
      "Fix it",
    );
    expect((screen.getByTestId("best-of-n-run-repo") as HTMLInputElement).value).toBe("/srv/dirty");
    expect(screen.getByTestId("best-of-n-run-error")).toHaveTextContent(
      "repository has uncommitted changes",
    );
  });

  it("shows the failed banner with the error and the run-again hint", () => {
    const run = proposingRun();
    run.status = "failed";
    run.error = "verifier exploded";
    renderPage();
    launch(run);
    const banner = screen.getByTestId("best-of-n-run-failed-banner");
    expect(banner).toHaveTextContent("verifier exploded");
    expect(screen.getByTestId("best-of-n-failed-hint")).toHaveTextContent(
      "Run again keeps your prompt — fix the cause and retry.",
    );
    // A settled (failed) run offers Run again right in the strip.
    expect(screen.getByTestId("best-of-n-run-again")).toBeInTheDocument();
  });
});
