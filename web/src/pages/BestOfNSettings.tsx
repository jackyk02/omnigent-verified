/**
 * Settings › Best-of-N Verifier (`/settings/verifier`).
 *
 * Edits the server's Best-of-N run configuration (`/v1/best-of-n/config`):
 * which proposal harnesses fan out, the pairwise selection criteria the
 * verifier judges on, and the verifier/selection parameters. Local draft
 * state seeds from the query; nothing persists until "Save changes" PUTs
 * the whole config back.
 */

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { showToast } from "@/components/ui/toast";
import { ConfigRow, DescribedSelect } from "@/components/HarnessConfigControls";
import { useBestOfNConfig, useUpdateBestOfNConfig } from "@/hooks/useBestOfNConfig";
import type { BestOfNConfig, BestOfNConfigResponse, ProposerSpec } from "@/lib/bestOfNApi";

// Picker-facing names for the known proposal harness slugs; unknown slugs
// fall back to the raw slug so a server-added harness still gets a row.
const HARNESS_DISPLAY_NAMES: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  pi: "Pi",
};

const ON_ERROR_OPTIONS = [
  { value: "tie", label: "Tie", description: "Score failed comparisons 0.5/0.5" },
  { value: "raise", label: "Raise", description: "Abort the run" },
] as const;

// ── Draft state ──────────────────────────────────────────────────────────────
// Numeric fields are kept as raw strings so mid-edit states (empty box, an
// out-of-range number on the way to a valid one) aren't clamped on every
// keystroke; parsing + clamping happens once when the config is built.

interface ProposerDraft {
  harness: string;
  enabled: boolean;
  count: string;
  /** Per-harness model override; "" = use the run-level proposal model. */
  model: string;
  // Pass-through fields the UI doesn't edit — preserved across save.
  command: string | null;
  env: Record<string, string>;
}

interface CriterionDraft {
  /** Stable local row key — criteria have no server id and names are editable. */
  id: number;
  name: string;
  description: string;
}

let criterionKeyCounter = 0;
function nextCriterionKey(): number {
  criterionKeyCounter += 1;
  return criterionKeyCounter;
}

interface Draft {
  proposers: ProposerDraft[];
  proposalModel: string;
  criteria: CriterionDraft[];
  verifierModel: string;
  verifierBaseUrl: string;
  verifierApiKeyEnv: string;
  nEvaluations: string;
  pivots: string;
  seed: string;
  maxWorkers: string;
  onError: "tie" | "raise";
  proposalTimeoutS: string;
  keepProposalBranches: boolean;
}

/** Parse a draft field to an integer clamped into [min, max]; fallback if unparsable. */
function clampInt(text: string, min: number, max: number, fallback: number): number {
  const value = Number.parseInt(text, 10);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

function draftFromResponse(response: BestOfNConfigResponse): Draft {
  const { config } = response;
  const byHarness = new Map(config.proposers.map((p) => [p.harness, p]));
  // One row per known harness (server order), then any extra configured
  // proposers the server doesn't list — nothing is silently dropped.
  const harnesses = [
    ...response.knownHarnesses,
    ...config.proposers.map((p) => p.harness).filter((h) => !response.knownHarnesses.includes(h)),
  ];
  return {
    proposers: harnesses.map((harness) => {
      const spec: ProposerSpec = byHarness.get(harness) ?? {
        harness,
        enabled: false,
        count: 1,
        model: null,
        command: null,
        env: {},
      };
      return {
        harness,
        enabled: spec.enabled,
        count: String(spec.count),
        model: spec.model ?? "",
        command: spec.command,
        env: spec.env,
      };
    }),
    proposalModel: config.proposalModel,
    criteria: Object.entries(config.criteria).map(([name, description]) => ({
      id: nextCriterionKey(),
      name,
      description,
    })),
    verifierModel: config.verifier.model,
    verifierBaseUrl: config.verifier.baseUrl,
    verifierApiKeyEnv: config.verifier.apiKeyEnv,
    nEvaluations: String(config.verifier.nEvaluations),
    pivots: String(config.verifier.pivots),
    seed: String(config.verifier.seed),
    maxWorkers: String(config.verifier.maxWorkers),
    onError: config.verifier.onError,
    proposalTimeoutS: String(config.proposalTimeoutS),
    keepProposalBranches: config.keepProposalBranches,
  };
}

/** Build the wire-ready config from a draft, clamping numeric fields. */
function configFromDraft(draft: Draft, fallback: BestOfNConfig): BestOfNConfig {
  const criteria: Record<string, string> = {};
  for (const c of draft.criteria) {
    const name = c.name.trim();
    if (name) criteria[name] = c.description;
  }
  return {
    proposers: draft.proposers.map((p) => ({
      harness: p.harness,
      enabled: p.enabled,
      count: clampInt(p.count, 1, 8, 1),
      model: p.model.trim() === "" ? null : p.model.trim(),
      command: p.command,
      env: p.env,
    })),
    proposalModel: draft.proposalModel.trim() || fallback.proposalModel,
    criteria,
    verifier: {
      model: draft.verifierModel.trim() || fallback.verifier.model,
      baseUrl: draft.verifierBaseUrl.trim() || fallback.verifier.baseUrl,
      apiKeyEnv: draft.verifierApiKeyEnv.trim() || fallback.verifier.apiKeyEnv,
      nEvaluations: clampInt(draft.nEvaluations, 1, 16, fallback.verifier.nEvaluations),
      pivots: clampInt(draft.pivots, 1, 8, fallback.verifier.pivots),
      seed: clampInt(draft.seed, Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER, 0),
      maxWorkers: clampInt(draft.maxWorkers, 1, 64, fallback.verifier.maxWorkers),
      onError: draft.onError,
    },
    proposalTimeoutS: (() => {
      const value = Number.parseFloat(draft.proposalTimeoutS);
      return Number.isFinite(value) && value > 0 ? value : fallback.proposalTimeoutS;
    })(),
    keepProposalBranches: draft.keepProposalBranches,
  };
}

/** Client-side mirror of the server's 400s, so Save can disable with a hint. */
function draftProblem(draft: Draft): string | null {
  if (!draft.proposers.some((p) => p.enabled)) {
    return "Enable at least one proposal harness.";
  }
  const names = draft.criteria.map((c) => c.name.trim()).filter(Boolean);
  if (names.length === 0) return "Add at least one named criterion.";
  if (new Set(names).size !== names.length) return "Criterion names must be unique.";
  return null;
}

/** A labeled subsection: heading + one-line helper above its body. */
function Subsection({
  title,
  helper,
  children,
}: {
  title: string;
  helper: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col">
        <h2 className="text-sm font-medium">{title}</h2>
        <span className="text-sm text-muted-foreground">{helper}</span>
      </div>
      {children}
    </div>
  );
}

export function BestOfNSettings() {
  const query = useBestOfNConfig();
  const mutation = useUpdateBestOfNConfig();
  const [draft, setDraft] = useState<Draft | null>(null);

  // Seed the draft once the config arrives; later background refetches must
  // not clobber in-progress edits, so only a null draft is (re)seeded.
  useEffect(() => {
    if (query.data && draft === null) setDraft(draftFromResponse(query.data));
  }, [query.data, draft]);

  const baseline = useMemo(
    () => (query.data ? configFromDraft(draftFromResponse(query.data), query.data.config) : null),
    [query.data],
  );
  const draftConfig = useMemo(
    () => (draft && query.data ? configFromDraft(draft, query.data.config) : null),
    [draft, query.data],
  );
  const dirty =
    draftConfig !== null &&
    baseline !== null &&
    JSON.stringify(draftConfig) !== JSON.stringify(baseline);
  const problem = draft ? draftProblem(draft) : null;

  if (query.isPending || (query.data && draft === null)) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError || !query.data || !draft) {
    return (
      <div className="flex flex-col items-start gap-3">
        <p className="text-sm text-destructive" data-testid="best-of-n-load-error">
          Couldn't load the Best-of-N configuration
          {query.error instanceof Error ? `: ${query.error.message}` : "."}
        </p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          Retry
        </Button>
      </div>
    );
  }
  const { harnessAvailability, defaultCriteria } = query.data;

  const update = (patch: Partial<Draft>) => setDraft((prev) => prev && { ...prev, ...patch });
  const updateProposer = (harness: string, patch: Partial<ProposerDraft>) =>
    setDraft(
      (prev) =>
        prev && {
          ...prev,
          proposers: prev.proposers.map((p) => (p.harness === harness ? { ...p, ...patch } : p)),
        },
    );
  const updateCriterion = (index: number, patch: Partial<CriterionDraft>) =>
    setDraft(
      (prev) =>
        prev && {
          ...prev,
          criteria: prev.criteria.map((c, i) => (i === index ? { ...c, ...patch } : c)),
        },
    );

  const onSave = () => {
    if (!draftConfig) return;
    mutation.mutate(draftConfig, {
      onSuccess: (response) => {
        setDraft(draftFromResponse(response));
        showToast("Best-of-N verifier settings saved.");
      },
    });
  };

  return (
    <div className="flex max-w-2xl flex-col gap-8">
      <Subsection
        title="Proposal harnesses"
        helper="Which coding agents draft candidate solutions, and how many proposals each contributes."
      >
        <div className="flex flex-col gap-2">
          {draft.proposers.map((proposer) => {
            const displayName = HARNESS_DISPLAY_NAMES[proposer.harness] ?? proposer.harness;
            const available = harnessAvailability[proposer.harness] !== false;
            return (
              <div
                key={proposer.harness}
                className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border px-3 py-2.5"
                data-testid={`best-of-n-proposer-${proposer.harness}`}
              >
                <Switch
                  size="sm"
                  checked={proposer.enabled}
                  onCheckedChange={(enabled) => updateProposer(proposer.harness, { enabled })}
                  aria-label={`Enable ${displayName} proposals`}
                  data-testid={`best-of-n-proposer-${proposer.harness}-enabled`}
                />
                <span className="text-sm font-medium">{displayName}</span>
                {!available && (
                  <Badge variant="outline" className="text-muted-foreground">
                    not installed
                  </Badge>
                )}
                <div className="ml-auto flex items-center gap-2">
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    proposals
                    <Input
                      type="number"
                      min={1}
                      max={8}
                      inputMode="numeric"
                      className="h-8 w-16"
                      value={proposer.count}
                      onChange={(e) => updateProposer(proposer.harness, { count: e.target.value })}
                      aria-label={`${displayName} proposals per run`}
                      data-testid={`best-of-n-proposer-${proposer.harness}-count`}
                    />
                  </label>
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    model
                    <Input
                      type="text"
                      placeholder="default"
                      spellCheck={false}
                      autoCapitalize="off"
                      autoCorrect="off"
                      className="h-8 w-40"
                      value={proposer.model}
                      onChange={(e) => updateProposer(proposer.harness, { model: e.target.value })}
                      aria-label={`${displayName} model override`}
                      data-testid={`best-of-n-proposer-${proposer.harness}-model`}
                    />
                  </label>
                </div>
              </div>
            );
          })}
          <div className="mt-2">
            <ConfigRow
              label="Proposal model"
              description="Run-level default model for harnesses without an override."
            >
              <Input
                type="text"
                placeholder="deepseek-v4-flash"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                className="h-9"
                value={draft.proposalModel}
                onChange={(e) => update({ proposalModel: e.target.value })}
                aria-label="Proposal model"
                data-testid="best-of-n-proposal-model"
              />
            </ConfigRow>
          </div>
        </div>
      </Subsection>

      <Subsection
        title="Selection criteria"
        helper="What the verifier compares proposals on. Criteria are judged pairwise, in this order."
      >
        <div className="flex flex-col gap-2">
          {draft.criteria.map((criterion, index) => (
            <div
              key={criterion.id}
              className="flex flex-col gap-2 rounded-lg border border-border p-3"
              data-testid={`best-of-n-criterion-${index}`}
            >
              <div className="flex items-center gap-2">
                <Input
                  type="text"
                  placeholder="Criterion name"
                  className="h-8 flex-1"
                  value={criterion.name}
                  onChange={(e) => updateCriterion(index, { name: e.target.value })}
                  aria-label={`Criterion ${index + 1} name`}
                  data-testid={`best-of-n-criterion-${index}-name`}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={draft.criteria.length <= 1}
                  onClick={() =>
                    setDraft(
                      (prev) =>
                        prev && { ...prev, criteria: prev.criteria.filter((_, i) => i !== index) },
                    )
                  }
                  aria-label={`Remove criterion ${index + 1}`}
                  data-testid={`best-of-n-criterion-${index}-remove`}
                >
                  <Trash2Icon className="size-4" />
                </Button>
              </div>
              <Textarea
                rows={3}
                placeholder="What should the verifier compare here?"
                className="min-h-0 text-sm"
                value={criterion.description}
                onChange={(e) => updateCriterion(index, { description: e.target.value })}
                aria-label={`Criterion ${index + 1} description`}
                data-testid={`best-of-n-criterion-${index}-description`}
              />
            </div>
          ))}
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() =>
                setDraft(
                  (prev) =>
                    prev && {
                      ...prev,
                      criteria: [
                        ...prev.criteria,
                        { id: nextCriterionKey(), name: "", description: "" },
                      ],
                    },
                )
              }
              data-testid="best-of-n-add-criterion"
            >
              <PlusIcon className="size-4" />
              Add criterion
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() =>
                setDraft(
                  (prev) =>
                    prev && {
                      ...prev,
                      criteria: Object.entries(defaultCriteria).map(([name, description]) => ({
                        id: nextCriterionKey(),
                        name,
                        description,
                      })),
                    },
                )
              }
              data-testid="best-of-n-restore-default-criteria"
            >
              Restore defaults
            </Button>
          </div>
        </div>
      </Subsection>

      <Subsection
        title="Selection parameters"
        helper="How the verifier model runs its pairwise tournament over the proposals."
      >
        <div className="flex flex-col gap-4">
          <ConfigRow
            label="Evaluations per pair"
            description="Independent judgments averaged per comparison (1–16)."
          >
            <Input
              type="number"
              min={1}
              max={16}
              inputMode="numeric"
              className="h-9"
              value={draft.nEvaluations}
              onChange={(e) => update({ nEvaluations: e.target.value })}
              aria-label="Evaluations per pair"
              data-testid="best-of-n-n-evaluations"
            />
          </ConfigRow>
          <ConfigRow label="Pivots" description="Tournament pivot proposals (1–8).">
            <Input
              type="number"
              min={1}
              max={8}
              inputMode="numeric"
              className="h-9"
              value={draft.pivots}
              onChange={(e) => update({ pivots: e.target.value })}
              aria-label="Pivots"
              data-testid="best-of-n-pivots"
            />
          </ConfigRow>
          <ConfigRow label="Seed" description="Random seed for reproducible tournaments.">
            <Input
              type="number"
              inputMode="numeric"
              className="h-9"
              value={draft.seed}
              onChange={(e) => update({ seed: e.target.value })}
              aria-label="Seed"
              data-testid="best-of-n-seed"
            />
          </ConfigRow>
          <ConfigRow label="Max workers" description="Parallel verifier requests (1–64).">
            <Input
              type="number"
              min={1}
              max={64}
              inputMode="numeric"
              className="h-9"
              value={draft.maxWorkers}
              onChange={(e) => update({ maxWorkers: e.target.value })}
              aria-label="Max workers"
              data-testid="best-of-n-max-workers"
            />
          </ConfigRow>
          <ConfigRow
            label="Proposal timeout"
            description="Seconds each proposer may run before it is cut off."
          >
            <Input
              type="number"
              min={1}
              inputMode="numeric"
              className="h-9"
              value={draft.proposalTimeoutS}
              onChange={(e) => update({ proposalTimeoutS: e.target.value })}
              aria-label="Proposal timeout in seconds"
              data-testid="best-of-n-proposal-timeout"
            />
          </ConfigRow>
          <ConfigRow
            label="On comparison error"
            description="What a failed pairwise comparison does to the run."
          >
            <DescribedSelect
              value={draft.onError}
              onValueChange={(value) => update({ onError: value === "raise" ? "raise" : "tie" })}
              options={ON_ERROR_OPTIONS}
              testId="best-of-n-on-error"
              ariaLabel="On comparison error"
            />
          </ConfigRow>
          <ConfigRow label="Verifier model" description="Model that judges the comparisons.">
            <Input
              type="text"
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              className="h-9"
              value={draft.verifierModel}
              onChange={(e) => update({ verifierModel: e.target.value })}
              aria-label="Verifier model"
              data-testid="best-of-n-verifier-model"
            />
          </ConfigRow>
          <ConfigRow label="Base URL" description="OpenAI-compatible endpoint for the verifier.">
            <Input
              type="text"
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              className="h-9"
              value={draft.verifierBaseUrl}
              onChange={(e) => update({ verifierBaseUrl: e.target.value })}
              aria-label="Verifier base URL"
              data-testid="best-of-n-verifier-base-url"
            />
          </ConfigRow>
          <ConfigRow
            label="API key env var"
            description="Environment variable holding the verifier API key."
          >
            <Input
              type="text"
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              className="h-9"
              value={draft.verifierApiKeyEnv}
              onChange={(e) => update({ verifierApiKeyEnv: e.target.value })}
              aria-label="Verifier API key environment variable"
              data-testid="best-of-n-verifier-api-key-env"
            />
          </ConfigRow>
          <ConfigRow
            label="Keep losing proposal branches"
            description="Keep the non-winning proposals' git branches after a run."
          >
            <div className="flex h-9 items-center sm:justify-end">
              <Switch
                checked={draft.keepProposalBranches}
                onCheckedChange={(keepProposalBranches) => update({ keepProposalBranches })}
                aria-label="Keep losing proposal branches"
                data-testid="best-of-n-keep-proposal-branches"
              />
            </div>
          </ConfigRow>
        </div>
      </Subsection>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={onSave}
          disabled={!dirty || problem !== null}
          loading={mutation.isPending}
          data-testid="best-of-n-save"
        >
          Save changes
        </Button>
        <Button
          variant="outline"
          disabled={!dirty || mutation.isPending}
          onClick={() => query.data && setDraft(draftFromResponse(query.data))}
          data-testid="best-of-n-discard"
        >
          Discard
        </Button>
        {dirty && problem && (
          <span className="text-sm text-muted-foreground" data-testid="best-of-n-problem">
            {problem}
          </span>
        )}
        {mutation.isError && (
          <span className="text-sm text-destructive" data-testid="best-of-n-save-error">
            {mutation.error instanceof Error ? mutation.error.message : "Save failed."}
          </span>
        )}
      </div>
    </div>
  );
}
