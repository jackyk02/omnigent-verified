"""LLM-as-a-Verifier: fine-grained reward + pivot tournament selection.

The high-level entry points are `select` (pick the best of N agent
trajectories via a Probabilistic Pivot Tournament), `compare` (raw
fine-grained rewards for one pairwise comparison), and `track` (progress
curve over a single trajectory's steps). For batch / benchmark runs use the
config-driven launcher in `run.py` instead.
"""

from __future__ import annotations

import os
import random
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple, Union

from llm_verifier import pivot_tournament as ppt
from llm_verifier.fine_grained_reward import (
    DEFAULT_MODEL,
    GRANULARITY,
    LazyClient,
    MissingAPIKeyError,
    create_client,
    directed_reward,
    load_dotenv,
    score_directed_pairs,
    score_pair_criterion,
)
from llm_verifier.progress import ProgressResult, ProgressTracker, track
from llm_verifier.prompts import load_prompts, normalize_criteria

__all__ = [
    "DEFAULT_MODEL",
    "GRANULARITY",
    "MissingAPIKeyError",
    "ProgressResult",
    "ProgressTracker",
    "VerifierResult",
    "compare",
    "load_dotenv",
    "select",
    "track",
]

# A criteria argument: bundled benchmark name or criteria-file path (str),
# {name: description} mapping, or a sequence of strings / criterion dicts.
CriteriaArg = Union[str, Mapping[str, str], Sequence[str | Mapping[str, str]]]

# An images argument: one image or a sequence of images, where each image is
# a local file path, an http(s) URL, or raw image bytes.
ImagesArg = Union[str, bytes, "os.PathLike[str]", Sequence[Union[str, bytes, "os.PathLike[str]"]]]


@dataclass
class VerifierResult:
    """Outcome of `select`.

    Attributes:
        index: index of the winning trajectory in the input list.
        best: the winning trajectory itself.
        scores: per-trajectory mean preference (w_i / c_i) from the tournament.
        n_comparisons: number of directed verifier comparisons that were run.
        criteria: the criterion ids used for scoring.
    """

    index: int
    best: str
    scores: list[float] = field(repr=False)
    n_comparisons: int = 0
    criteria: list[str] = field(default_factory=list)

    @property
    def ranking(self) -> list[int]:
        """Trajectory indices sorted best-first by tournament score."""
        return sorted(range(len(self.scores)), key=lambda i: (-self.scores[i], i))

    def __repr__(self) -> str:
        return (
            f"VerifierResult(index={self.index}, "
            f"n_comparisons={self.n_comparisons}, "
            f"criteria={self.criteria})"
        )


def _resolve_criteria(criteria: CriteriaArg, ground_truth_note: str | None):
    """Turn any accepted `criteria` form into (note, criterion dicts)."""
    if isinstance(criteria, str):
        note, crits = load_prompts(criteria)
    else:
        note, crits = "", normalize_criteria(criteria)
    if ground_truth_note is not None:
        note = ground_truth_note
    return note, crits


def _default_progress(progress: bool | None) -> bool:
    """`progress=None` means auto: show progress only in an interactive
    terminal, stay silent in servers / pipelines."""
    if progress is None:
        return sys.stderr.isatty()
    return progress


def select(
    problem: str,
    candidates: Sequence[str],
    *,
    criteria: CriteriaArg,
    images: ImagesArg | None = None,
    ground_truth_note: str | None = None,
    n_evaluations: int = 8,
    pivots: int = 2,
    seed: int = 0,
    max_workers: int = 50,
    model: str = DEFAULT_MODEL,
    cache: str | None = None,
    progress: bool | None = None,
    on_error: str = "tie",
    client: Any = None,
) -> VerifierResult:
    """Select the best of N agent trajectories for a single task.

    Scores directed pairs of trajectories with the fine-grained Gemini reward
    and aggregates them with a Probabilistic Pivot Tournament (PPT), so the
    cost is O(Nk) verifier comparisons rather than the O(N²) of a full
    round-robin. Identical inputs with the same `seed` run the identical
    tournament.

    Args:
        problem: the task description shown to the verifier.
        candidates: list of N agent trajectories (strings) to rank.
        criteria: a bundled benchmark name (e.g. ``"swe_bench"``), a path to a
            ``*.md`` criteria file, a ``{name: description}`` dict, or a list
            of strings / ``{"id", "name", "description"}`` dicts.
        images: task-context image(s) the verifier sees with every comparison
            — a single image or a list, each a local file path
            (``images="frame.png"``), an http(s) URL, or raw bytes. Requires
            a multimodal verifier model.
        ground_truth_note: optional note the verifier always sees; defaults to
            the note parsed from the prompt file (or empty).
        n_evaluations: repeated verifications K per criterion.
        pivots: number of pivots k in the tournament. Keep k small relative
            to len(trajectories) — cost grows as O(Nk), and k ≥ N degenerates
            to a full round-robin (k is clamped to N).
        seed: seed for the random ring pass.
        max_workers: concurrency for verifier calls.
        model: verifier model name (default ``gemini-2.5-flash``).
        cache: optional path to a JSON score cache. Re-running with the same
            cache re-scores only the comparisons not seen before.
        progress: show a progress bar / log lines. Default (``None``) shows
            progress only when stderr is a TTY.
        on_error: ``"tie"`` scores a failed verifier call 0.5/0.5 for this run
            (never persisted to the cache); ``"raise"`` re-raises it.
        client: a pre-built ``openai`` or ``google-genai`` client (optional);
            by default an OpenAI-compatible client is created when
            ``OPENAI_BASE_URL`` is set (vLLM / SGLang / OpenAI), otherwise a
            Gemini client from ``VERTEX_API_KEY``. Either way the backend
            must expose token-level logprobs.

    Returns:
        A `VerifierResult` whose ``.index`` / ``.best`` is the chosen
        trajectory and ``.ranking`` orders all trajectories best-first.

    Raises:
        MissingAPIKeyError: no credentials and no `client` given (only when
            uncached comparisons actually need scoring).
    """
    note, crits = _resolve_criteria(criteria, ground_truth_note)
    criteria_ids = [c["id"] for c in crits]
    show_progress = _default_progress(progress)

    n = len(candidates)
    if n == 0:
        raise ValueError("need at least one candidate")
    if n == 1:
        return VerifierResult(0, candidates[0], [1.0], 0, criteria_ids)

    if cache:
        cache_dir = os.path.dirname(os.path.abspath(cache))
        os.makedirs(cache_dir, exist_ok=True)

    # One synthetic task holding the N candidate trajectories.
    task = "task"
    tasks = {
        task: [{"problem": problem, "trace": t, "reward": 0, "images": images} for t in candidates]
    }

    lazy = LazyClient()
    if client is not None:
        lazy._client = client

    def directed_for(scores):
        return lambda a, b: directed_reward(scores, task, a, b, criteria_ids, n_evaluations)

    def score_pairs(pairs):
        return score_directed_pairs(
            lazy,
            tasks,
            {task: pairs},
            crits,
            note,
            n_evaluations,
            max_workers,
            cache,
            model=model,
            progress=show_progress,
            on_error=on_error,
        )

    # Phase A: ring pass (slot bias cancels around the cycle).
    rng = random.Random(seed)
    ring = ppt.ring_cycle(n, rng)
    score = directed_for(score_pairs(ring))

    # Pivots = empirical leaders from the ring pass.
    w, c = [0.0] * n, [0] * n
    ppt.accumulate(ring, score, w, c)
    pivot_set = ppt.select_pivots(w, c, pivots)
    pr_pairs = ppt.pivot_round_pairs(n, pivot_set)

    # Phase B: score the pivot rounds, then aggregate everything.
    score = directed_for(score_pairs(pr_pairs))
    w, c = [0.0] * n, [0] * n
    ppt.accumulate(ring, score, w, c)
    ppt.accumulate(pr_pairs, score, w, c)
    best = max(range(n), key=lambda i: (w[i] / c[i] if c[i] else 0.0, -i))
    mean_pref = [w[i] / c[i] if c[i] else 0.0 for i in range(n)]
    return VerifierResult(
        best, candidates[best], mean_pref, len(ring) + len(pr_pairs), criteria_ids
    )


def compare(
    problem: str,
    trace_a: str,
    trace_b: str,
    *,
    criteria: CriteriaArg,
    images: ImagesArg | None = None,
    ground_truth_note: str | None = None,
    n_evaluations: int = 1,
    max_workers: int = 8,
    model: str = DEFAULT_MODEL,
    client: Any = None,
) -> tuple[float, float]:
    """Fine-grained rewards (R_A, R_B) in [0, 1] for one directed comparison.

    The verifier sees `trace_a` in slot A and `trace_b` in slot B; rewards are
    averaged over all criteria and `n_evaluations` repeats. This is the raw
    pairwise reward `select` is built on — note the single directed call does
    not cancel slot bias the way `select`'s ring pass does.

    `criteria` and `images` accept the same forms as `select` (`images` is
    one image or a list — file paths, http(s) URLs, or raw bytes — attached
    as task context; requires a multimodal verifier model). A failed
    verifier call raises (there is no tie fallback here).

    Raises:
        MissingAPIKeyError: no credentials found and no `client` given.
    """
    if n_evaluations < 1:
        raise ValueError("n_evaluations must be >= 1")
    note, crits = _resolve_criteria(criteria, ground_truth_note)
    if client is None:
        client = create_client()

    jobs = [crit for crit in crits for _ in range(n_evaluations)]
    if len(jobs) == 1:
        results = [
            score_pair_criterion(client, problem, trace_a, trace_b, jobs[0], note, model, images)
        ]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as executor:
            results = list(
                executor.map(
                    lambda crit: score_pair_criterion(
                        client, problem, trace_a, trace_b, crit, note, model, images
                    ),
                    jobs,
                )
            )

    r_a = sum(r[0] for r in results) / len(results)
    r_b = sum(r[1] for r in results) / len(results)
    return r_a, r_b
