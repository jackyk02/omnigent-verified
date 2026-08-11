"""Server-side Best-of-N runs with live per-proposal streaming.

The engine in :mod:`codify.best_of_n` runs one blocking CLI flow;
this module wraps the same pieces in an in-process run manager the web
UI drives: ``start_run`` launches every enabled proposer harness
directly on the user's task — no orchestrating "brain" in between —
with each harness process streamed line-by-line into its proposal's
transcript, so the UI can render one live pane per harness while they
work the same task in parallel worktrees. When all proposals land, the
run scores them with ``llm_verifier.select`` and squash-merges the
winner; ``get`` returns a JSON-ready snapshot at any point.

Runs are held in memory (this is a per-server, single-process feature;
a restart forgets finished runs but never leaves worktrees behind —
cleanup runs in ``finally``).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from codify.best_of_n import (
    BestOfNConfig,
    BestOfNError,
    Proposal,
    ProposerSpec,
    _build_command,
    _cleanup,
    _run_git,
    ensure_repo_ready,
    harness_availability,
    load_best_of_n_config,
    proposal_env,
    render_transcript_line,
    select_best,
    snapshot_proposal,
)
from codify.best_of_n import (
    _merge_winner as merge_winner,
)
from codify.host.git_worktree import create_worktree

_logger = logging.getLogger(__name__)

# Transcript tail cap per proposal in API snapshots: enough for a live
# pane, small enough to poll every second or two.
_SNAPSHOT_TRANSCRIPT_CHARS = 20_000
_MAX_FINISHED_RUNS = 20
# Online progress scoring cadence: sample each proposal's new transcript
# as one "step" at most this often (each update costs one verifier call),
# and only once at least this much new text accumulated.
_PROGRESS_INTERVAL_S = 20.0
_PROGRESS_MIN_NEW_CHARS = 200
_PROGRESS_STEP_CHARS = 6_000


@dataclass
class _RunState:
    """One Best-of-N run: inputs, live proposals, and the outcome."""

    id: str
    prompt: str
    repo_path: str
    config: BestOfNConfig
    status: str = "preparing"  # preparing|proposing|scoring|merging|done|failed
    proposals: list[Proposal] = field(default_factory=list)
    proposal_status: dict[int, str] = field(default_factory=dict)  # index -> running|ok|failed
    # index -> online progress curve ([{"at": epoch_s, "score": 0..1}, ...])
    # scored by llm_verifier.ProgressTracker over the streaming transcript.
    proposal_progress: dict[int, list[dict[str, float]]] = field(default_factory=dict)
    scores: list[float] | None = None
    ranking: list[int] | None = None
    winner_index: int | None = None
    n_comparisons: int | None = None
    merge_commit: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready view of the run for the API."""
        with self.lock:
            return {
                "id": self.id,
                "object": "best_of_n_run",
                "status": self.status,
                "prompt": self.prompt,
                "repo_path": self.repo_path,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "proposals": [
                    {
                        "harness": p.harness,
                        "index": p.index,
                        "model": p.model,
                        "branch": p.branch,
                        "status": self.proposal_status.get(p.index, "pending"),
                        "transcript": p.transcript[-_SNAPSHOT_TRANSCRIPT_CHARS:],
                        "diff": p.diff,
                        "diff_chars": len(p.diff),
                        "duration_s": p.duration_s,
                        "error": p.error,
                        "progress": (
                            self.proposal_progress[p.index][-1]["score"]
                            if self.proposal_progress.get(p.index)
                            else None
                        ),
                        "progress_history": [
                            entry["score"] for entry in self.proposal_progress.get(p.index, [])
                        ],
                        "score": (
                            self.scores[p.index]
                            if self.scores is not None and p.index < len(self.scores)
                            else None
                        ),
                    }
                    for p in self.proposals
                ],
                "ranking": self.ranking,
                "winner_index": self.winner_index,
                "n_comparisons": self.n_comparisons,
                "merge_commit": self.merge_commit,
                "error": self.error,
            }


class BestOfNRunManager:
    """Owns in-flight and recently finished Best-of-N runs."""

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}
        self._lock = threading.Lock()

    def start_run(
        self,
        prompt: str,
        repo_path: str,
        config: BestOfNConfig | None = None,
    ) -> dict[str, Any]:
        """Validate inputs, register the run, and launch it on a thread.

        :raises BestOfNError: On a non-repo path, dirty tree, or an
            empty launchable roster — before anything is created.
        """
        config = config or load_best_of_n_config(repo_path)
        # Folders that were never `git init`-ed (or have no commits yet)
        # are initialized with a baseline commit so any project works.
        ensure_repo_ready(repo_path)
        head = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
        if head.returncode != 0:
            raise BestOfNError(f"could not resolve HEAD in {repo_path}: {head.stderr.strip()}")
        dirty = _run_git(["status", "--porcelain"], cwd=repo_path)
        if dirty.stdout.strip():
            raise BestOfNError(
                "the repository has uncommitted changes; commit or stash them "
                "first so the winning proposal can be merged cleanly"
            )
        slots = self._launchable_slots(config)
        if not slots:
            raise BestOfNError("no enabled proposer harness is launchable on this machine")

        run = _RunState(
            id=uuid.uuid4().hex[:12],
            prompt=prompt,
            repo_path=repo_path,
            config=config,
        )
        with self._lock:
            self._prune_finished_locked()
            self._runs[run.id] = run
        thread = threading.Thread(
            target=self._execute,
            args=(run, slots, head.stdout.strip()),
            name=f"best-of-n-{run.id}",
            daemon=True,
        )
        thread.start()
        return run.snapshot()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
        return run.snapshot() if run else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        summaries = []
        for run in runs:
            snap = run.snapshot()
            for proposal in snap["proposals"]:
                proposal.pop("transcript", None)
                proposal.pop("diff", None)
            summaries.append(snap)
        return summaries

    # ------------------------------------------------------------------

    @staticmethod
    def _launchable_slots(config: BestOfNConfig) -> list[ProposerSpec]:
        available = harness_availability()
        slots: list[ProposerSpec] = []
        for spec in config.proposers:
            if not spec.enabled:
                continue
            if spec.command is None and not available.get(spec.harness, False):
                continue
            slots.extend([spec] * spec.count)
        return slots

    def _prune_finished_locked(self) -> None:
        finished = [
            r for r in self._runs.values() if r.status in ("done", "failed") and r.finished_at
        ]
        finished.sort(key=lambda r: r.finished_at or 0.0)
        while len(finished) > _MAX_FINISHED_RUNS:
            stale = finished.pop(0)
            self._runs.pop(stale.id, None)

    def _execute(self, run: _RunState, slots: list[ProposerSpec], base_commit: str) -> None:
        """Thread body: fan out, stream, score, merge, clean up."""
        proposals: list[Proposal] = []
        winner: Proposal | None = None
        try:
            with run.lock:
                run.status = "proposing"
            for i, spec in enumerate(slots):
                created = create_worktree(
                    repo_path=run.repo_path,
                    branch_name=f"best-of-n/{run.id}/{spec.harness}-{i}",
                )
                proposal = Proposal(
                    harness=spec.harness,
                    index=i,
                    branch=created.branch,
                    worktree_path=created.worktree_path,
                    model=spec.model or run.config.proposal_model,
                )
                proposals.append(proposal)
                with run.lock:
                    run.proposals.append(proposal)
                    run.proposal_status[i] = "running"

            threads = [
                threading.Thread(
                    target=self._stream_proposal,
                    args=(run, proposals[i], slots[i], base_commit),
                    name=f"best-of-n-{run.id}-p{i}",
                    daemon=True,
                )
                for i in range(len(proposals))
            ]
            progress_threads = [
                threading.Thread(
                    target=self._track_progress,
                    args=(run, proposal),
                    name=f"best-of-n-{run.id}-progress{proposal.index}",
                    daemon=True,
                )
                for proposal in proposals
            ]
            for t in threads + progress_threads:
                t.start()
            for t in threads:
                t.join()
            for t in progress_threads:
                # Each sampler does at most one final (post-finish) update.
                t.join(timeout=90.0)

            usable = [p for p in proposals if p.ok]
            if not usable:
                raise BestOfNError(
                    "no proposal produced a usable trajectory; errors: "
                    + "; ".join(f"{p.harness}-{p.index}: {p.error}" for p in proposals)
                )

            with run.lock:
                run.status = "scoring"
            result = select_best(
                run.prompt, [p.trajectory(run.prompt) for p in usable], run.config
            )
            winner = usable[result.index]
            with run.lock:
                run.scores = [0.0] * len(proposals)
                for p, score in zip(usable, result.scores, strict=True):
                    run.scores[p.index] = score
                run.ranking = [usable[i].index for i in result.ranking]
                run.winner_index = winner.index
                run.n_comparisons = result.n_comparisons
                run.status = "merging"
            merge_commit = merge_winner(run.repo_path, winner, result, run.config)
            with run.lock:
                run.merge_commit = merge_commit
                run.status = "done"
        except Exception as exc:
            _logger.exception("best-of-n run %s failed", run.id)
            with run.lock:
                run.status = "failed"
                run.error = str(exc)
        finally:
            _cleanup(proposals, run.config, keep_winner=winner)
            with run.lock:
                run.finished_at = time.time()

    def _track_progress(self, run: _RunState, proposal: Proposal) -> None:
        """Score the proposal's live transcript with an online verifier.

        Samples the streaming transcript on a throttle, feeding each new
        slice to ``llm_verifier.ProgressTracker`` as one step — the
        tracker only ever sees the prefix produced so far, so the curve
        cannot be influenced by the future. One extra update runs after
        the proposal finishes, with the final diff appended, so the last
        point reflects the completed attempt. Progress is advisory UI
        signal only; selection still uses the full-trajectory tournament.
        """
        from codify.best_of_n import create_verifier_client

        try:
            import llm_verifier

            tracker = llm_verifier.ProgressTracker(
                run.prompt,
                n_evaluations=1,
                model=run.config.verifier.model,
                client=create_verifier_client(run.config.verifier),
            )
        except Exception:  # noqa: BLE001 — progress is best-effort
            _logger.debug("progress tracker unavailable for run %s", run.id, exc_info=True)
            return

        consumed = 0
        last_update = time.monotonic()
        while True:
            with run.lock:
                status = run.proposal_status.get(proposal.index, "running")
                text = proposal.transcript
                diff = proposal.diff
            finished = status != "running"
            new = text[consumed:]
            due = (
                len(new) >= _PROGRESS_MIN_NEW_CHARS
                and time.monotonic() - last_update >= _PROGRESS_INTERVAL_S
            )
            if due or (finished and new.strip()):
                step = new[-_PROGRESS_STEP_CHARS:]
                if finished and diff:
                    step += "\n# Final diff\n" + diff[:8_000]
                consumed = len(text)
                last_update = time.monotonic()
                try:
                    score = tracker.update(step)
                except Exception:  # noqa: BLE001 — one failed sample is not fatal
                    _logger.debug(
                        "progress update failed for %s-%s",
                        proposal.harness,
                        proposal.index,
                        exc_info=True,
                    )
                else:
                    # ProgressTracker falls back to 0.5 when no repeat produced
                    # a readable score — recording that would fake "50%
                    # progress" on the bar, so drop unreadable samples.
                    reps = tracker._per_step_reps[-1] if tracker._per_step_reps else []
                    if any(v is not None for v in reps):
                        with run.lock:
                            run.proposal_progress.setdefault(proposal.index, []).append(
                                {"at": time.time(), "score": round(score, 4)}
                            )
            if finished:
                return
            time.sleep(2.0)

    def _stream_proposal(
        self,
        run: _RunState,
        proposal: Proposal,
        spec: ProposerSpec,
        base_commit: str,
    ) -> None:
        """Run one harness with line-streamed output, then snapshot it."""
        argv = _build_command(spec, run.prompt, run.config.proposal_model)
        env = proposal_env(spec, run.config)
        start = time.monotonic()
        deadline = start + run.config.proposal_timeout_s
        try:
            process = subprocess.Popen(
                argv,
                cwd=proposal.worktree_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                rendered = render_transcript_line(spec.harness, line)
                if rendered:
                    with run.lock:
                        proposal.transcript += rendered
                if time.monotonic() > deadline:
                    process.kill()
                    proposal.error = (
                        f"{spec.harness} timed out after {run.config.proposal_timeout_s:.0f}s"
                    )
                    break
            returncode = process.wait(timeout=30)
            if returncode != 0 and proposal.error is None:
                proposal.error = f"{spec.harness} exited with code {returncode}"
        except OSError as exc:
            proposal.error = f"failed to launch {spec.harness}: {exc}"
        except subprocess.TimeoutExpired:
            proposal.error = f"{spec.harness} did not exit after being killed"
        proposal.duration_s = time.monotonic() - start
        snapshot_proposal(proposal, base_commit)
        with run.lock:
            run.proposal_status[proposal.index] = "ok" if proposal.ok else "failed"


_manager: BestOfNRunManager | None = None
_manager_lock = threading.Lock()


def get_run_manager() -> BestOfNRunManager:
    """Process-wide singleton run manager."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BestOfNRunManager()
        return _manager
