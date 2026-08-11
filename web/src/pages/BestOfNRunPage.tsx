/**
 * Best-of-N run screen (`/best-of-n`).
 *
 * One prompt in; three coding harnesses (Claude Code / Codex / Pi) solve it
 * directly in parallel git worktrees. The page shows three live panes of the
 * harnesses working (streamed transcript tails via a ~1.5s poll), then the
 * LLM verifier scores the trajectories and the winning proposal is
 * squash-merged as the final commit. No orchestrator in between — this page
 * drives `/v1/best-of-n/runs` directly (see `lib/bestOfNRunsApi.ts`).
 */

import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { CheckIcon, ChevronDownIcon, CopyIcon, Settings2Icon, XIcon } from "lucide-react";
import { Link, useLocation, useNavigate } from "@/lib/routing";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { showToast } from "@/components/ui/toast";
import { useBestOfNConfig } from "@/hooks/useBestOfNConfig";
import { useBestOfNRun, useBestOfNRuns, useStartBestOfNRun } from "@/hooks/useBestOfNRun";
import { pushRecentRepoPath, readRecentRepoPaths } from "@/lib/bestOfNPreferences";
import type { BestOfNProposal, BestOfNRun, BestOfNRunStatus } from "@/lib/bestOfNRunsApi";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

// Display names for the known harness slugs; unknown slugs render as-is so a
// server-added harness still gets a pane. Mirrors BestOfNSettings.
const HARNESS_DISPLAY_NAMES: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  pi: "Pi",
};

function harnessDisplayName(slug: string): string {
  return HARNESS_DISPLAY_NAMES[slug] ?? slug;
}

// First-run guidance: concrete starter tasks shown as chips while the task
// box is empty. Clicking fills the textarea; the user still picks the repo.
const EXAMPLE_TASKS = [
  "Fix the failing test and explain the root cause",
  "Add input validation with tests",
  "Refactor the largest function and keep tests green",
];

/**
 * Payload handed over by the new-session composer (router state): submitting
 * the landing composer with the Best-of-N agent selected navigates here and
 * auto-starts a run instead of creating a chat session.
 */
export interface BestOfNAutoStart {
  prompt: string;
  repoPath: string;
}

// Shared focus treatment for the page's plain-button chips/rows, mirroring
// the badge primitive's focus-visible ring.
const CHIP_FOCUS_CLASS =
  "focus-visible:outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50";

function formatScore(score: number): string {
  return score.toFixed(3);
}

function formatDiffChars(chars: number): string {
  return `${chars.toLocaleString("en-US")} diff chars`;
}

/** `m:ss` (or `h:mm:ss`) for the live elapsed timers. */
function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const ss = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** Last path segment for compact repo display ("/srv/repo" → "repo"). */
function repoBasename(path: string): string {
  return path.replace(/\/+$/, "").split("/").at(-1) || path;
}

/** Wall clock in ms, ticking once per second while `active` (else frozen). */
function useNowMs(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

// ── Status chips ─────────────────────────────────────────────────────────────

const RUN_STATUS_LABELS: Record<BestOfNRunStatus, string> = {
  preparing: "Preparing…",
  proposing: "Proposing…",
  scoring: "Verifier scoring…",
  merging: "Merging…",
  done: "Done",
  failed: "Failed",
};

/** Run-level status chip: spinner while live, green when done, red on failure. */
function RunStatusChip({ status }: { status: BestOfNRunStatus }) {
  const live = status !== "done" && status !== "failed";
  return (
    <span
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1.5 rounded-4xl border border-transparent px-2 text-xs font-medium",
        live && "bg-muted text-muted-foreground",
        status === "done" && "bg-success/10 text-success",
        status === "failed" && "bg-destructive/10 text-destructive",
      )}
      data-testid="best-of-n-run-status"
    >
      {live && <Spinner className="size-3" />}
      {status === "done" && <CheckIcon className="size-3" />}
      {status === "failed" && <XIcon className="size-3" />}
      {RUN_STATUS_LABELS[status]}
    </span>
  );
}

/** Per-proposal status chip: pulsing dot while running, check/x when settled. */
function ProposalStatusChip({ status }: { status: BestOfNProposal["status"] }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 text-xs",
        status === "ok" && "text-success",
        status === "failed" && "text-destructive",
        (status === "pending" || status === "running") && "text-muted-foreground",
      )}
    >
      {status === "running" && (
        <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-primary" />
      )}
      {status === "ok" && <CheckIcon className="size-3" />}
      {status === "failed" && <XIcon className="size-3" />}
      {status}
    </span>
  );
}

// ── Transcript ───────────────────────────────────────────────────────────────

/**
 * The live transcript tail. Fixed height, monospace, and it follows the
 * stream: while the user is at (or near) the bottom, each new poll's text
 * keeps the view pinned to the end — but once they scroll up to read, the
 * view stays put until they return to the bottom (standard chat-log
 * behavior).
 */
function TranscriptView({
  text,
  testId,
  ariaLabel,
}: {
  text: string;
  testId: string;
  ariaLabel: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Ref, not state: scroll position isn't render state, and flipping it must
  // not re-render the pane on every scroll event.
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div
      ref={ref}
      // role="log": streaming append-only output, so the aria-label is valid
      // and screen readers announce it as a live region.
      role="log"
      aria-label={ariaLabel}
      onScroll={() => {
        const el = ref.current;
        if (!el) return;
        stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="h-80 overflow-y-auto px-3 py-2 font-mono text-xs whitespace-pre-wrap break-words text-muted-foreground"
      data-testid={testId}
    >
      {text || "Waiting for output…"}
    </div>
  );
}

// ── Verifier progress ────────────────────────────────────────────────────────

/**
 * Slim online-progress bar: the verifier's latest estimate of how close this
 * harness is to satisfying the task (sampled ~every 20s). Before the first
 * sample the track sits empty with a "warming up" note (only while the
 * proposal is actually running). With ≥2 samples, a tiny plain-div micro-bar
 * strip shows the history trend — no chart lib.
 */
function ProposalProgress({
  proposal,
  isWinner,
}: {
  proposal: BestOfNProposal;
  isWinner: boolean;
}) {
  const { progress, progressHistory } = proposal;
  return (
    <div
      className="flex items-center gap-2 border-b border-border px-3 py-2"
      data-testid={`best-of-n-pane-${proposal.index}-progress`}
    >
      <div
        role="progressbar"
        aria-label={`${harnessDisplayName(proposal.harness)} verifier progress`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress === null ? undefined : Math.round(progress * 100)}
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-500",
            isWinner ? "bg-success" : "bg-primary",
          )}
          style={{ width: `${progress === null ? 0 : progress * 100}%` }}
          data-testid={`best-of-n-pane-${proposal.index}-progress-fill`}
        />
      </div>
      {progressHistory.length >= 2 && (
        <div
          aria-hidden
          className="flex h-2 shrink-0 items-end gap-px"
          data-testid={`best-of-n-pane-${proposal.index}-progress-history`}
        >
          {progressHistory.slice(-16).map((sample, i) => (
            <div
              // Samples are append-only value-typed points with no identity;
              // the index IS the stable key here.
              // eslint-disable-next-line react/no-array-index-key
              key={i}
              className="w-0.5 rounded-full bg-muted-foreground/40"
              style={{ height: `${Math.max(20, sample * 100)}%` }}
            />
          ))}
        </div>
      )}
      {progress !== null ? (
        <span
          className="shrink-0 font-mono text-xs text-muted-foreground tabular-nums"
          data-testid={`best-of-n-pane-${proposal.index}-progress-label`}
        >
          {Math.round(progress * 100)}%
        </span>
      ) : proposal.status === "running" ? (
        <span
          className="shrink-0 text-xs text-muted-foreground"
          data-testid={`best-of-n-pane-${proposal.index}-progress-warming`}
        >
          verifier warming up…
        </span>
      ) : null}
    </div>
  );
}

// ── Proposal pane ────────────────────────────────────────────────────────────

function ProposalPane({
  proposal,
  runDone,
  isWinner,
  elapsedS,
}: {
  proposal: BestOfNProposal;
  runDone: boolean;
  isWinner: boolean;
  /** Live seconds since the run started (ticking); `null` once the run settles. */
  elapsedS: number | null;
}) {
  return (
    <Card
      size="sm"
      className={cn(
        "gap-0 py-0",
        // Winner pops with a success ring + full opacity; once the run is
        // done the non-winners recede slightly.
        isWinner && "ring-2 ring-success",
        runDone && !isWinner && "opacity-60",
      )}
      data-testid={`best-of-n-pane-${proposal.index}`}
      data-winner={isWinner || undefined}
    >
      <CardHeader className="flex flex-row flex-wrap items-center gap-x-2 gap-y-1 border-b border-border py-2">
        <span className="text-sm font-medium">{harnessDisplayName(proposal.harness)}</span>
        <ProposalStatusChip status={proposal.status} />
        <div className="ml-auto flex items-center gap-2">
          {runDone && proposal.score !== null && (
            <span
              className={cn(
                "font-mono text-xs tabular-nums",
                isWinner ? "text-success" : "text-muted-foreground",
              )}
              data-testid={`best-of-n-pane-${proposal.index}-score`}
            >
              {formatScore(proposal.score)}
            </span>
          )}
          {isWinner && (
            <span
              className="inline-flex h-5 items-center rounded-4xl bg-success/10 px-2 text-xs font-medium text-success"
              data-testid={`best-of-n-pane-${proposal.index}-merged`}
            >
              merged
            </span>
          )}
          {proposal.durationS !== null ? (
            <span className="text-xs text-muted-foreground tabular-nums">
              {proposal.durationS.toFixed(1)}s
            </span>
          ) : proposal.status === "running" && elapsedS !== null ? (
            // No per-proposal start time on the wire — proposals launch with
            // the run, so the run clock is the honest live approximation.
            <span
              className="font-mono text-xs text-muted-foreground tabular-nums"
              data-testid={`best-of-n-pane-${proposal.index}-running-elapsed`}
            >
              {formatElapsed(elapsedS)}
            </span>
          ) : null}
        </div>
      </CardHeader>
      <ProposalProgress proposal={proposal} isWinner={isWinner} />
      <CardContent className="p-0">
        <TranscriptView
          text={proposal.transcript}
          testId={`best-of-n-pane-${proposal.index}-transcript`}
          ariaLabel={`${harnessDisplayName(proposal.harness)} transcript`}
        />
      </CardContent>
      <CardFooter className="flex flex-col items-stretch gap-1 border-t border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          <span className="min-w-0 truncate font-mono" title={proposal.branch}>
            {proposal.branch}
          </span>
          <span className="ml-auto shrink-0 tabular-nums">
            {/* "no changes" only reads truthfully once the harness finished. */}
            {(proposal.status === "ok" || proposal.status === "failed") && proposal.diffChars === 0
              ? "no changes"
              : formatDiffChars(proposal.diffChars)}
          </span>
        </div>
        {proposal.status === "failed" && proposal.error && (
          <p
            className="truncate text-xs text-destructive"
            title={proposal.error}
            data-testid={`best-of-n-pane-${proposal.index}-error`}
          >
            {proposal.error}
          </p>
        )}
      </CardFooter>
    </Card>
  );
}

// ── Result banner ────────────────────────────────────────────────────────────

/** Simple horizontal score-bar comparison — plain divs, winner in success tone. */
function ScoreBars({ run }: { run: BestOfNRun }) {
  return (
    <div className="flex flex-col gap-1.5" data-testid="best-of-n-score-bars">
      {run.proposals.map((p) => (
        <div key={p.index} className="flex items-center gap-2 text-xs">
          <span className="w-24 shrink-0 truncate text-muted-foreground">
            {harnessDisplayName(p.harness)}
          </span>
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
            <div
              className={cn(
                "h-full rounded-full",
                p.index === run.winnerIndex ? "bg-success" : "bg-muted-foreground/40",
              )}
              style={{ width: `${Math.max(2, (p.score ?? 0) * 100)}%` }}
            />
          </div>
          <span className="w-12 shrink-0 text-right font-mono text-muted-foreground tabular-nums">
            {p.score !== null ? formatScore(p.score) : "—"}
          </span>
        </div>
      ))}
    </div>
  );
}

function ResultBanner({ run }: { run: BestOfNRun }) {
  if (run.status === "failed") {
    return (
      <div
        className="flex flex-col gap-1 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm"
        data-testid="best-of-n-run-failed-banner"
      >
        <div className="flex items-center gap-2">
          <XIcon className="size-4 shrink-0 text-destructive" />
          <span className="flex-1">{run.error ?? "The run failed."}</span>
        </div>
        <p className="pl-6 text-xs text-muted-foreground" data-testid="best-of-n-failed-hint">
          Run again keeps your prompt — fix the cause and retry.
        </p>
      </div>
    );
  }
  if (run.status !== "done" || run.winnerIndex === null) return null;
  const winner = run.proposals.find((p) => p.index === run.winnerIndex);
  if (!winner) return null;
  return (
    <div
      className="flex flex-col gap-3 rounded-lg border border-success/30 bg-success/5 px-4 py-3"
      data-testid="best-of-n-run-banner"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <CheckIcon className="size-4 shrink-0 text-success" />
        <span className="font-medium">
          Winner: {harnessDisplayName(winner.harness)}
          {winner.score !== null && <> — score {formatScore(winner.score)}</>}
          {run.nComparisons !== null && (
            <>
              {" over "}
              <span
                title="Pairwise verifier comparisons in the selection tournament"
                data-testid="best-of-n-comparisons"
              >
                {run.nComparisons} comparisons
              </span>
            </>
          )}
        </span>
        {run.mergeCommit && (
          <span className="ml-auto flex items-center gap-0.5">
            <span
              className="font-mono text-xs text-muted-foreground"
              data-testid="best-of-n-merge-commit"
              title={run.mergeCommit}
            >
              {run.mergeCommit.slice(0, 8)}
            </span>
            <CopyButton
              text={run.mergeCommit}
              label="Copy merge commit sha"
              testId="best-of-n-copy-commit"
            />
          </span>
        )}
      </div>
      <ScoreBars run={run} />
    </div>
  );
}

/** Per-line tone for the diff view: adds green, deletes red, headers muted. */
function diffLineClass(line: string): string | undefined {
  // File headers before add/delete: "+++"/"---" would otherwise color as
  // changes, which misreads.
  if (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")) {
    return "text-muted-foreground";
  }
  if (line.startsWith("+")) return "text-success";
  if (line.startsWith("-")) return "text-destructive";
  return undefined;
}

/** The diff, one span per line, colored by prefix. Split/memoized on the string. */
function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(
    () =>
      diff.split("\n").map((line, i) => (
        // Lines have no identity beyond their position in this exact string.
        // eslint-disable-next-line react/no-array-index-key
        <span key={i} className={cn("block", diffLineClass(line))}>
          {line === "" ? " " : line}
        </span>
      )),
    [diff],
  );
  return (
    <pre
      className="mt-2 max-h-96 overflow-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs whitespace-pre-wrap break-words"
      data-testid="best-of-n-winning-diff"
    >
      {lines}
    </pre>
  );
}

/** Collapsible view of the winning proposal's final diff. */
function WinningDiff({ run }: { run: BestOfNRun }) {
  if (run.status !== "done" || run.winnerIndex === null) return null;
  const winner = run.proposals.find((p) => p.index === run.winnerIndex);
  if (!winner || !winner.diff) return null;
  return (
    <Collapsible>
      <div className="flex items-center gap-1">
        <CollapsibleTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="group gap-1.5 text-muted-foreground"
            data-testid="best-of-n-winning-diff-toggle"
          >
            <ChevronDownIcon className="size-4 transition-transform group-data-[state=open]:rotate-180" />
            Winning diff
            <span className="font-mono text-xs">{formatDiffChars(winner.diffChars)}</span>
          </Button>
        </CollapsibleTrigger>
        <CopyButton text={winner.diff} label="Copy winning diff" testId="best-of-n-copy-diff" />
      </div>
      <CollapsibleContent>
        <DiffView diff={winner.diff} />
      </CollapsibleContent>
    </Collapsible>
  );
}

/** Tiny copy-to-clipboard icon button with the shared "Copied" toast. */
function CopyButton({ text, label, testId }: { text: string; label: string; testId: string }) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard
          .writeText(text)
          .then(() => showToast("Copied"))
          .catch(() => {
            // Clipboard denied (permissions / insecure context) — stay quiet.
          });
      }}
      className="text-muted-foreground hover:text-foreground"
      data-testid={testId}
    >
      <CopyIcon className="size-3" />
    </Button>
  );
}

// ── Launch readiness ─────────────────────────────────────────────────────────

interface Readiness {
  /** One entry per ENABLED proposer, in config order. */
  proposers: { harness: string; available: boolean }[];
  keyPresent: boolean;
}

/** True when the run cannot possibly succeed: nothing to propose, or no verifier. */
function readinessBlocked(readiness: Readiness | null): boolean {
  if (readiness === null) return false; // Config still loading/failed — don't block.
  return !readiness.proposers.some((p) => p.available) || !readiness.keyPresent;
}

/** One plain sentence naming exactly what's missing (readiness must be blocked). */
function readinessProblem(readiness: Readiness): string {
  const noProposer = !readiness.proposers.some((p) => p.available);
  if (noProposer && !readiness.keyPresent) {
    return "No enabled proposal harness is installed and the verifier API key is missing — fix both in";
  }
  if (noProposer) {
    return "No enabled proposal harness is installed on this server — enable or install one in";
  }
  return "The verifier API key is missing — set its environment variable, or change it in";
}

/**
 * Compact pre-run readiness row: a chip per enabled proposer plus the
 * verifier-key chip, so a doomed run is caught before the POST. A proposer
 * with a custom launch command counts as available (the availability probe
 * only checks the stock CLI).
 */
function ReadinessRow({ readiness }: { readiness: Readiness }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="best-of-n-readiness">
      {readiness.proposers.map((p) => (
        <span
          key={p.harness}
          className={cn(
            "inline-flex h-5 items-center gap-1 rounded-4xl border px-2 text-xs",
            p.available
              ? "border-border text-muted-foreground"
              : "border-warning/40 bg-warning/10 text-warning",
          )}
          data-testid={`best-of-n-readiness-${p.harness}`}
          data-available={p.available || undefined}
        >
          {p.available && <CheckIcon className="size-3 text-success" />}
          {harnessDisplayName(p.harness)}
          {!p.available && <span className="opacity-80">not installed</span>}
        </span>
      ))}
      <span
        className={cn(
          "inline-flex h-5 items-center gap-1 rounded-4xl border px-2 text-xs",
          readiness.keyPresent
            ? "border-border text-muted-foreground"
            : "border-destructive/40 bg-destructive/10 text-destructive",
        )}
        data-testid="best-of-n-readiness-verifier-key"
      >
        {readiness.keyPresent ? (
          <CheckIcon className="size-3 text-success" />
        ) : (
          <XIcon className="size-3" />
        )}
        Verifier key
        <span className="opacity-80">{readiness.keyPresent ? "loaded" : "missing"}</span>
      </span>
    </div>
  );
}

// ── Launch form ──────────────────────────────────────────────────────────────

function LaunchForm({
  initialPrompt,
  initialRepoPath,
  autoStart,
  onAutoStartConsumed,
  onStarted,
}: {
  /** Prefill from "Run again"; when absent the repo seeds from recents. */
  initialPrompt?: string;
  initialRepoPath?: string;
  /** Composer handoff: seed the fields AND fire the start once on mount. */
  autoStart?: BestOfNAutoStart | null;
  /** Reported the moment the auto-start fires so the owner drops the payload. */
  onAutoStartConsumed?: () => void;
  onStarted: (id: string) => void;
}) {
  const mutation = useStartBestOfNRun();
  // Snapshot the recents once per mount — the list only changes via this
  // form's own successful launch, which navigates away from it.
  const [recentRepos] = useState(() => readRecentRepoPaths());
  const [prompt, setPrompt] = useState(autoStart?.prompt ?? initialPrompt ?? "");
  // Most-recent repo prefills so repeat runs skip the retyping.
  const [repoPath, setRepoPath] = useState(
    () => autoStart?.repoPath ?? initialRepoPath ?? recentRepos[0] ?? "",
  );

  // Pre-run readiness from the config endpoint. While it loads (or fails),
  // `readiness` stays null: no extra chrome, and the form is NOT blocked —
  // the server re-validates on POST anyway.
  const configQuery = useBestOfNConfig();
  const readiness = useMemo<Readiness | null>(() => {
    const data = configQuery.data;
    if (!data) return null;
    return {
      proposers: data.config.proposers
        .filter((p) => p.enabled)
        .map((p) => ({
          harness: p.harness,
          // A custom launch command bypasses the stock CLI, so the harness
          // availability probe doesn't apply to it.
          available: p.command !== null || data.harnessAvailability[p.harness] !== false,
        })),
      keyPresent: data.verifierKeyPresent,
    };
  }, [configQuery.data]);
  const blocked = readinessBlocked(readiness);

  const canSubmit =
    prompt.trim() !== "" && repoPath.trim() !== "" && !mutation.isPending && !blocked;

  const submit = () => {
    if (!canSubmit) return;
    const repo = repoPath.trim();
    mutation.mutate(
      { prompt: prompt.trim(), repoPath: repo },
      {
        onSuccess: (run) => {
          // Only launches the server accepted become recents — a typo'd path
          // that 400s shouldn't haunt the chip row.
          pushRecentRepoPath(repo);
          onStarted(run.id);
        },
      },
    );
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  // Composer handoff: fire the start once on mount. It runs through the
  // normal submit gate, so a blocked preflight or empty repo leaves the user
  // on the prefilled form instead of firing a doomed POST; a server rejection
  // (e.g. dirty tree) surfaces as the usual inline error under the prefilled
  // form. The ref guards re-fire across re-renders; the owner drops the
  // payload via onAutoStartConsumed so a later form remount can't replay it.
  const autoFiredRef = useRef(false);
  useEffect(() => {
    if (!autoStart || autoFiredRef.current) return;
    autoFiredRef.current = true;
    onAutoStartConsumed?.();
    submit();
    // Mount-only by design: the payload is a one-shot handoff; `submit` is
    // re-created every render and must not re-trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cmd/Ctrl+Enter in the textarea submits — same muscle memory as the chat
  // composer; a plain Enter keeps inserting newlines in the task text.
  const onPromptKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form className="flex max-w-2xl flex-col gap-4" onSubmit={onSubmit}>
      <label className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">Task</span>
        <Textarea
          rows={4}
          placeholder="Fix the flaky retry test and add coverage for the timeout path"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={onPromptKeyDown}
          data-testid="best-of-n-run-prompt"
        />
        <span className="text-xs text-muted-foreground">⌘↵ to run</span>
      </label>
      {prompt === "" && (
        <div className="flex flex-wrap gap-1.5" data-testid="best-of-n-example-tasks">
          {EXAMPLE_TASKS.map((task, i) => (
            <button
              key={task}
              type="button"
              onClick={() => setPrompt(task)}
              className={cn(
                "rounded-4xl border border-border px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                CHIP_FOCUS_CLASS,
              )}
              data-testid={`best-of-n-example-task-${i}`}
            >
              {task}
            </button>
          ))}
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">Repository</span>
          <Input
            type="text"
            placeholder="/path/to/repo"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            className="font-mono"
            value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)}
            data-testid="best-of-n-run-repo"
          />
        </label>
        {recentRepos.length > 0 && (
          <div className="flex flex-wrap gap-1.5" data-testid="best-of-n-recent-repos">
            {recentRepos.map((path, i) => (
              <button
                key={path}
                type="button"
                onClick={() => setRepoPath(path)}
                title={path}
                className={cn(
                  "max-w-full truncate rounded-4xl border border-border px-2 py-0.5 font-mono text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                  CHIP_FOCUS_CLASS,
                )}
                data-testid={`best-of-n-recent-repo-${i}`}
              >
                {path}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="flex flex-col items-start gap-2">
        {readiness && <ReadinessRow readiness={readiness} />}
        {readiness && blocked && (
          <p className="text-sm text-muted-foreground" data-testid="best-of-n-preflight-problem">
            {readinessProblem(readiness)}{" "}
            <Link
              to="/settings/verifier"
              className="font-medium text-primary hover:underline"
              data-testid="best-of-n-preflight-settings-link"
            >
              Settings → Best-of-N Verifier
            </Link>
            .
          </p>
        )}
        <Button
          type="submit"
          disabled={!canSubmit}
          loading={mutation.isPending}
          data-testid="best-of-n-run-start"
        >
          Run best-of-N
        </Button>
        {mutation.isError && (
          <p className="text-sm text-destructive" data-testid="best-of-n-run-error">
            {mutation.error instanceof Error ? mutation.error.message : "Couldn't start the run."}
          </p>
        )}
      </div>
    </form>
  );
}

// ── Recent runs ──────────────────────────────────────────────────────────────

/** Status dot tone for a recent-run row. */
function runDotClass(status: BestOfNRunStatus): string {
  if (status === "done") return "bg-success";
  if (status === "failed") return "bg-destructive";
  return "animate-pulse bg-primary";
}

/**
 * Past runs under the launch form — one row each, newest first; clicking a
 * row reopens it in the run view. Renders nothing while loading or when
 * there's no history (no empty-state chrome).
 */
function RecentRuns({ onOpen }: { onOpen: (id: string) => void }) {
  const query = useBestOfNRuns();
  const runs = [...(query.data ?? [])].sort((a, b) => b.createdAt - a.createdAt);
  if (runs.length === 0) return null;
  return (
    <div className="mt-10 flex max-w-2xl flex-col gap-1" data-testid="best-of-n-recent-runs">
      <h2 className="px-2 text-sm font-medium text-muted-foreground">Recent runs</h2>
      {runs.map((run) => {
        const winner =
          run.status === "done" && run.winnerIndex !== null
            ? (run.proposals.find((p) => p.index === run.winnerIndex) ?? null)
            : null;
        return (
          <button
            key={run.id}
            type="button"
            onClick={() => onOpen(run.id)}
            // Explicit Enter/Space so activation is testable without a real
            // browser; preventDefault suppresses the native duplicate click.
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen(run.id);
              }
            }}
            className={cn(
              "flex items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors hover:bg-muted/50",
              CHIP_FOCUS_CLASS,
            )}
            data-testid={`best-of-n-recent-run-${run.id}`}
          >
            <span
              aria-hidden
              className={cn("size-2 shrink-0 rounded-full", runDotClass(run.status))}
            />
            <span className="min-w-0 flex-1 truncate text-sm" title={run.prompt}>
              {run.prompt}
            </span>
            <span className="shrink-0 font-mono text-xs text-muted-foreground">
              {repoBasename(run.repoPath)}
            </span>
            {winner && (
              <span className="shrink-0 text-xs text-success">
                {harnessDisplayName(winner.harness)}
                {winner.score !== null && (
                  <span className="font-mono tabular-nums"> {formatScore(winner.score)}</span>
                )}
              </span>
            )}
            <span className="w-14 shrink-0 text-right text-xs text-muted-foreground">
              {relativeTime(run.createdAt * 1000)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

// ── Run view ─────────────────────────────────────────────────────────────────

function RunView({
  runId,
  onNewRun,
  onRunAgain,
}: {
  runId: string;
  onNewRun: () => void;
  /** Back to the form, prefilled with this run's prompt + repo (no auto-submit). */
  onRunAgain: (prompt: string, repoPath: string) => void;
}) {
  const query = useBestOfNRun(runId);
  const live =
    query.data !== undefined && query.data.status !== "done" && query.data.status !== "failed";
  // One shared 1s clock drives the strip's elapsed timer and every pane's
  // running timer; it stops ticking the moment the run settles.
  const nowMs = useNowMs(live);

  if (query.isPending) {
    return (
      <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
        <Spinner />
        Loading run…
      </div>
    );
  }
  if (query.isError || !query.data) {
    return (
      <div className="flex flex-col items-start gap-3">
        <p className="text-sm text-destructive" data-testid="best-of-n-run-load-error">
          Couldn't load the run
          {query.error instanceof Error ? `: ${query.error.message}` : "."}
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
            Retry
          </Button>
          <Button variant="ghost" size="sm" onClick={onNewRun}>
            New run
          </Button>
        </div>
      </div>
    );
  }

  const run = query.data;
  const runDone = run.status === "done";
  const elapsedS = live ? nowMs / 1000 - run.createdAt : null;

  return (
    <div className="flex flex-col gap-4" data-testid="best-of-n-run-view">
      {/* Status strip: run status + live elapsed + prompt + repo, with the
          escape hatches back to the launch form on the right. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <RunStatusChip status={run.status} />
        {elapsedS !== null && (
          <span
            className="font-mono text-xs text-muted-foreground tabular-nums"
            data-testid="best-of-n-run-elapsed"
          >
            {formatElapsed(elapsedS)}
          </span>
        )}
        {/* max-w-full: in a wrap container an item can't shrink below its
            content width, so unbounded long text would force horizontal
            scroll on narrow screens — cap to the strip and truncate. */}
        <span
          className="max-w-full min-w-0 truncate text-sm md:max-w-md"
          title={run.prompt}
          data-testid="best-of-n-run-prompt-summary"
        >
          {run.prompt}
        </span>
        <span
          className="max-w-full min-w-0 truncate font-mono text-xs text-muted-foreground"
          title={run.repoPath}
        >
          {run.repoPath}
        </span>
        <div className="ml-auto flex items-center gap-1">
          {!live && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onRunAgain(run.prompt, run.repoPath)}
              data-testid="best-of-n-run-again"
            >
              Run again
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={onNewRun} data-testid="best-of-n-new-run">
            New run
          </Button>
        </div>
      </div>

      <ResultBanner run={run} />

      {/* The three live panes, index order, stacked on mobile. */}
      <div className="grid gap-3 md:grid-cols-3" data-testid="best-of-n-panes">
        {run.proposals.map((proposal) => (
          <ProposalPane
            key={proposal.index}
            proposal={proposal}
            runDone={runDone}
            isWinner={runDone && proposal.index === run.winnerIndex}
            elapsedS={elapsedS}
          />
        ))}
      </div>

      <WinningDiff run={run} />
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export function BestOfNRunPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  // "Run again" hands the finished run's prompt + repo back to the form;
  // cleared on any other way into the form so a stale prefill can't linger.
  const [prefill, setPrefill] = useState<{ prompt: string; repoPath: string } | null>(null);

  // Composer handoff (router state), read once at mount. Held in state so the
  // form can consume it exactly once; dropped from history immediately so a
  // refresh or back-navigation can't re-POST a duplicate run.
  const [autoStart, setAutoStart] = useState<BestOfNAutoStart | null>(() => {
    const state = location.state as { autoStart?: BestOfNAutoStart } | null;
    return state?.autoStart ?? null;
  });
  const clearedHistoryStateRef = useRef(false);
  useEffect(() => {
    if (autoStart === null || clearedHistoryStateRef.current) return;
    clearedHistoryStateRef.current = true;
    // Replace with the same pathname, no state. rebase-safe when embedded:
    // the routing seam skips paths already under the basename.
    navigate(location.pathname, { replace: true });
  }, [autoStart, navigate, location.pathname]);

  // Tab title while on this route, restored on leave. There is no shared
  // per-route title helper — ChatPage sets document.title directly too.
  useEffect(() => {
    const previous = document.title;
    document.title = "Best-of-N — Codify";
    return () => {
      document.title = previous;
    };
  }, []);

  return (
    <PageScroll maxWidthClassName="max-w-6xl" contentClassName="px-6">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Best-of-N</h1>
          <p className="text-sm text-muted-foreground">
            Every harness solves it. The verifier picks. The winner lands.
          </p>
        </div>
        {/* Cross-link to the verifier configuration (criteria, harnesses,
            tournament knobs) — subtle, out of the task flow. */}
        <Button asChild variant="ghost" size="sm" className="shrink-0 text-muted-foreground">
          <Link to="/settings/verifier" data-testid="best-of-n-settings-link">
            <Settings2Icon className="size-3.5" />
            Criteria &amp; tuning
          </Link>
        </Button>
      </div>
      {activeRunId === null ? (
        <>
          <LaunchForm
            initialPrompt={prefill?.prompt}
            initialRepoPath={prefill?.repoPath}
            autoStart={autoStart}
            onAutoStartConsumed={() => setAutoStart(null)}
            onStarted={(id) => {
              setPrefill(null);
              setActiveRunId(id);
            }}
          />
          <RecentRuns onOpen={setActiveRunId} />
        </>
      ) : (
        <RunView
          runId={activeRunId}
          onNewRun={() => {
            setPrefill(null);
            setActiveRunId(null);
          }}
          onRunAgain={(prompt, repoPath) => {
            setPrefill({ prompt, repoPath });
            setActiveRunId(null);
          }}
        />
      )}
    </PageScroll>
  );
}
