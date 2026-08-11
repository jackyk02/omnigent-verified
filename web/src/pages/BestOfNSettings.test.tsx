// Render tests for the Best-of-N Verifier settings body. The query/mutation
// hooks are mocked at their module seam (same approach as InboxPage.test.tsx)
// so no QueryClient or network is involved; the draft/dirty/save wiring under
// test is the component's own.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BestOfNSettings } from "./BestOfNSettings";
import type { BestOfNConfigResponse } from "@/lib/bestOfNApi";
import * as bestOfNHooks from "@/hooks/useBestOfNConfig";

vi.mock("@/hooks/useBestOfNConfig", () => ({
  useBestOfNConfig: vi.fn(),
  useUpdateBestOfNConfig: vi.fn(),
}));

function response(): BestOfNConfigResponse {
  return {
    config: {
      proposers: [
        { harness: "claude", enabled: true, count: 1, model: null, command: null, env: {} },
        { harness: "codex", enabled: true, count: 1, model: null, command: null, env: {} },
        { harness: "pi", enabled: false, count: 1, model: null, command: null, env: {} },
      ],
      proposalModel: "deepseek-v4-flash",
      criteria: {
        Correctness: "Compare the final change...",
        "Code Quality": "Prefer the cleaner diff.",
      },
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
    harnessAvailability: { claude: true, codex: true, pi: false },
    verifierKeyPresent: true,
    defaultCriteria: { Correctness: "Compare the final change..." },
  };
}

const mutate = vi.fn();

function mockHooks(overrides: Partial<Record<string, unknown>> = {}) {
  vi.mocked(bestOfNHooks.useBestOfNConfig).mockReturnValue({
    data: response(),
    isPending: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    ...overrides,
  } as unknown as ReturnType<typeof bestOfNHooks.useBestOfNConfig>);
  vi.mocked(bestOfNHooks.useUpdateBestOfNConfig).mockReturnValue({
    mutate,
    isPending: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof bestOfNHooks.useUpdateBestOfNConfig>);
}

beforeEach(() => {
  mutate.mockReset();
  mockHooks();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BestOfNSettings", () => {
  it("renders a row per known harness, with display names and availability", () => {
    render(<BestOfNSettings />);
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("Pi")).toBeInTheDocument();
    // Pi is unavailable → the muted "not installed" badge appears exactly once.
    const piRow = screen.getByTestId("best-of-n-proposer-pi");
    expect(piRow).toHaveTextContent("not installed");
    expect(screen.getAllByText("not installed")).toHaveLength(1);
  });

  it("renders the criteria rows in config order", () => {
    render(<BestOfNSettings />);
    const first = screen.getByTestId("best-of-n-criterion-0-name") as HTMLInputElement;
    const second = screen.getByTestId("best-of-n-criterion-1-name") as HTMLInputElement;
    expect(first.value).toBe("Correctness");
    expect(second.value).toBe("Code Quality");
    // With two rows, either may be removed.
    expect(screen.getByTestId("best-of-n-criterion-0-remove")).not.toBeDisabled();
  });

  it("disables Save until the draft is dirty, then submits the converted config", () => {
    render(<BestOfNSettings />);
    const save = screen.getByTestId("best-of-n-save");
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByTestId("best-of-n-proposer-claude-count"), {
      target: { value: "3" },
    });
    expect(save).not.toBeDisabled();

    fireEvent.click(save);
    expect(mutate).toHaveBeenCalledTimes(1);
    const config = mutate.mock.calls[0][0];
    expect(config.proposers.find((p: { harness: string }) => p.harness === "claude").count).toBe(3);
    // Untouched fields ride through unchanged.
    expect(config.verifier.onError).toBe("tie");
    expect(Object.keys(config.criteria)).toEqual(["Correctness", "Code Quality"]);
  });

  it("keeps Save disabled when every proposer is off, with a hint", () => {
    render(<BestOfNSettings />);
    fireEvent.click(screen.getByTestId("best-of-n-proposer-claude-enabled"));
    fireEvent.click(screen.getByTestId("best-of-n-proposer-codex-enabled"));
    expect(screen.getByTestId("best-of-n-save")).toBeDisabled();
    expect(screen.getByTestId("best-of-n-problem")).toHaveTextContent(
      "Enable at least one proposal harness.",
    );
  });

  it("restores default criteria and discards edits", () => {
    render(<BestOfNSettings />);
    fireEvent.click(screen.getByTestId("best-of-n-restore-default-criteria"));
    // Defaults hold only Correctness → one row remains and it can't be removed.
    expect(screen.queryByTestId("best-of-n-criterion-1-name")).not.toBeInTheDocument();
    expect(screen.getByTestId("best-of-n-criterion-0-remove")).toBeDisabled();
    expect(screen.getByTestId("best-of-n-save")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("best-of-n-discard"));
    expect((screen.getByTestId("best-of-n-criterion-1-name") as HTMLInputElement).value).toBe(
      "Code Quality",
    );
    expect(screen.getByTestId("best-of-n-save")).toBeDisabled();
  });

  it("shows the load error state with a retry", () => {
    mockHooks({ data: undefined, isError: true, error: new Error("boom") });
    render(<BestOfNSettings />);
    expect(screen.getByTestId("best-of-n-load-error")).toHaveTextContent("boom");
  });
});
