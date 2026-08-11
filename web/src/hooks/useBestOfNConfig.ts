import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type BestOfNConfig, fetchBestOfNConfig, updateBestOfNConfig } from "@/lib/bestOfNApi";

// ── Query helpers ────────────────────────────────────────────────────────────

const QUERY_KEY = ["best-of-n-config"];

// ── Hooks ────────────────────────────────────────────────────────────────────

/** Fetch the Best-of-N verifier configuration + harness metadata. */
export function useBestOfNConfig() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchBestOfNConfig,
    staleTime: 5_000,
  });
}

/** PUT /v1/best-of-n/config — replace the configuration. */
export function useUpdateBestOfNConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (config: BestOfNConfig) => updateBestOfNConfig(config),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
