import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type BestOfNRun,
  fetchBestOfNRun,
  fetchBestOfNRuns,
  startBestOfNRun,
} from "@/lib/bestOfNRunsApi";

// ── Query helpers ────────────────────────────────────────────────────────────

const RUNS_KEY = ["best-of-n-runs"];
const runKey = (id: string) => ["best-of-n-run", id];

/** Poll while the run is live; stop once it settles. */
function isFinished(run: BestOfNRun | undefined): boolean {
  return run?.status === "done" || run?.status === "failed";
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/** POST /v1/best-of-n/runs — start a run. Resolves to the initial snapshot. */
export function useStartBestOfNRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: startBestOfNRun,
    onSuccess: (run) => {
      // Seed the run query so the run view paints from the POST's snapshot
      // instead of waiting a poll cycle, and refresh the list.
      queryClient.setQueryData(runKey(run.id), run);
      void queryClient.invalidateQueries({ queryKey: RUNS_KEY });
    },
  });
}

/**
 * Live snapshot of one run. Polls every ~1.5s while the run is in flight and
 * stops once it reaches `done`/`failed` (or when `id` is null — no run open).
 */
export function useBestOfNRun(id: string | null) {
  return useQuery({
    queryKey: runKey(id ?? "none"),
    queryFn: () => fetchBestOfNRun(id as string),
    enabled: id !== null,
    refetchInterval: (query) => (isFinished(query.state.data) ? false : 1500),
  });
}

/** GET /v1/best-of-n/runs — all runs (list snapshots, no transcript/diff). */
export function useBestOfNRuns() {
  return useQuery({
    queryKey: RUNS_KEY,
    queryFn: fetchBestOfNRuns,
    staleTime: 5_000,
  });
}
