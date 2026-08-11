"""Best-of-N multi-harness proposal generation with LLM-verifier selection.

The Best-of-N engine fans one task out to several coding harnesses (Claude
Code, Codex, Pi, ...), each in its own git worktree + branch, collects the
resulting trajectories (transcript + final diff), ranks them with
``llm_verifier.select`` (a Probabilistic Pivot Tournament over pairwise
fine-grained rewards), and merges the winning proposal back into the base
branch — so the final commit on the user's branch is the selected
best-of-N proposal.

Worktrees (not copies) isolate the parallel proposals: each candidate gets
its own working tree and branch while sharing the object store, and the
losing branches are cheap to discard.

Configuration lives under the ``best_of_n:`` key of the user / project
config (see :mod:`codify.config`) and is editable via the web UI
(``/v1/best-of-n/config``) and ``omni config``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codify.host.git_worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
)

_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
# Proposals and the verifier both default to DeepSeek v4-flash: cheap and
# fast enough to sample widely, and its API returns the token-level
# logprobs the fine-grained verifier reward requires. The proposal model
# uses the provider-qualified gateway spelling; it applies to harnesses
# that can run arbitrary gateway models (pi), while vendor-locked CLIs
# (claude, codex) keep their native default unless a per-harness model
# override is set.
DEFAULT_PROPOSAL_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_VERIFIER_MODEL = "deepseek-v4-flash"

# The default selection criteria, written so a stranger could score with
# them: each says where to look, what scores HIGH/LOW, and what to ignore
# so one criterion doesn't leak into another.
DEFAULT_CRITERIA: dict[str, str] = {
    "Correctness": (
        "Compare the final change (the `diff --git ...` at the end of the "
        "trajectory) against what the user actually asked for. Does the code "
        "do the right thing — right files, right API, right types, right "
        "control flow, no off-by-one errors, no swapped arguments, no unbound "
        "or shadowed names, edge cases (empty input, error paths) handled "
        "where the task implies them? A change that fully implements the "
        "request and would behave correctly on realistic inputs scores HIGH; "
        "one that implements the wrong thing, special-cases the literal "
        "example in the prompt, silently drops part of the request, or would "
        "crash on plausible inputs scores LOW. When a task admits multiple "
        "correct outputs (ambiguous tie-breaks, equivalent minimal "
        "solutions), judge semantic correctness — does the behavior satisfy "
        "the stated properties — rather than textual equality with one "
        "reference implementation. Judge the diff on its technical merits; "
        "ignore how confident the narration sounds and ignore style "
        "(scored separately)."
    ),
    "Task Completeness": (
        "Enumerate every distinct requirement in the user's request "
        "(including implicit ones such as updating callers or keeping tests "
        "passing) and check each off against the final diff. All "
        "requirements addressed scores HIGH; requirements skipped, "
        "half-done, deferred with a TODO, or replaced with a stub scores "
        "LOW. IGNORE whether each piece is implemented correctly — only "
        "whether it was attempted and finished."
    ),
    "Empirical Verification": (
        "Look at the commands the agent actually ran and what they printed, "
        "not what the agent claimed. Reward trajectories that exercised the "
        "changed code — ran the relevant tests or executed the program — "
        "and whose observed output confirms the change works without "
        "breaking existing tests in the touched area. Penalize trajectories "
        "that declared success without running anything, misread their own "
        "output, or edited files after the last successful check so the "
        "final state is unverified."
    ),
    "Code Quality": (
        "Review the final diff as an experienced reviewer: consistency with "
        "the surrounding code's style and idioms, sensible naming, no dead "
        "or duplicated code, no debugging leftovers, no gratuitous refactors "
        "or unrelated edits mixed in, no new footguns (broad excepts, "
        "resource leaks). Clean, minimal, idiomatic diffs score HIGH; "
        "noisy, sprawling, or sloppy diffs score LOW. IGNORE functional "
        "correctness — judge only how well it is written."
    ),
}

GROUND_TRUTH_NOTE = (
    "Do NOT trust the agent's self-assessment or claims of success. Judge "
    "only the evidence in the trajectory: the user's request, the commands "
    "the agent actually ran, the raw output those commands printed, and the "
    "final diff."
)

# Headless launch templates per harness. "{prompt}" / "{model}" are
# substituted at launch; model args are appended only when a model is set.
# A ProposerSpec.command overrides the template wholesale, so any CLI that
# can run non-interactively can serve as a proposer.
_HARNESS_COMMANDS: dict[str, list[str]] = {
    # stream-json makes claude emit an event line per step, so the run
    # UI's pane streams live instead of staying blank until the end;
    # render_transcript_line() turns the events back into readable text.
    "claude": [
        "claude",
        "-p",
        "{prompt}",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ],
    "codex": [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "{prompt}",
    ],
    # json mode streams NDJSON events live (plain -p buffers until the
    # end), so pi's pane and progress tracker see work as it happens.
    "pi": ["pi", "-p", "{prompt}", "--mode", "json"],
}
_HARNESS_MODEL_ARGS: dict[str, list[str]] = {
    "claude": ["--model", "{model}"],
    "codex": ["-m", "{model}"],
    "pi": ["--model", "{model}"],
}
_HARNESS_BINARIES: dict[str, str] = {"claude": "claude", "codex": "codex", "pi": "pi"}
# Harnesses that accept arbitrary provider-qualified gateway models and so
# honor the run-level default proposal model. Vendor-locked CLIs are only
# given a model flag when the proposer sets an explicit override.
_GATEWAY_MODEL_HARNESSES: frozenset[str] = frozenset({"pi"})

KNOWN_PROPOSER_HARNESSES: tuple[str, ...] = tuple(_HARNESS_COMMANDS)

_BRANCH_PREFIX = "best-of-n"
_MAX_TRANSCRIPT_CHARS = 30_000
_MAX_DIFF_CHARS = 40_000
_GIT_IDENTITY = ["-c", "user.name=codify", "-c", "user.email=bot@codify.dev"]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class ProposerSpec:
    """One proposal-generating harness in the Best-of-N roster.

    :param harness: Harness key — one of :data:`KNOWN_PROPOSER_HARNESSES`
        for the built-in launch templates, or any name when ``command``
        is given explicitly.
    :param enabled: Whether this proposer participates in runs.
    :param count: How many independent proposals this harness contributes.
    :param model: Model override for this harness; ``None`` uses the
        run-level default (DeepSeek v4-flash) where the harness supports a
        model flag.
    :param command: Optional explicit argv template (with ``{prompt}`` /
        ``{model}`` placeholders) replacing the built-in one.
    :param env: Extra environment variables for the harness process.
    """

    harness: str
    enabled: bool = True
    count: int = 1
    model: str | None = None
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class VerifierSettings:
    """Tuning parameters forwarded to ``llm_verifier.select``.

    ``n_evaluations`` / ``pivots`` / ``seed`` / ``max_workers`` /
    ``on_error`` map 1:1 onto the ``select`` keyword arguments; ``model``,
    ``base_url`` and ``api_key_env`` describe the OpenAI-compatible
    verifier backend (which must expose token-level logprobs).
    """

    model: str = DEFAULT_VERIFIER_MODEL
    base_url: str = DEEPSEEK_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    # Direct key fallback for environments where the env var doesn't reach
    # the process (e.g. a session shell inside the runner). Env wins.
    api_key: str | None = None
    n_evaluations: int = 4
    pivots: int = 2
    seed: int = 0
    max_workers: int = 16
    on_error: str = "tie"


@dataclass
class BestOfNConfig:
    """Full Best-of-N configuration (the ``best_of_n:`` config block)."""

    proposers: list[ProposerSpec] = field(
        default_factory=lambda: [
            ProposerSpec(harness="claude"),
            ProposerSpec(harness="codex"),
            ProposerSpec(harness="pi"),
        ]
    )
    proposal_model: str = DEFAULT_PROPOSAL_MODEL
    criteria: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CRITERIA))
    verifier: VerifierSettings = field(default_factory=VerifierSettings)
    proposal_timeout_s: float = 900.0
    keep_proposal_branches: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the plain-dict shape stored in config.yaml."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BestOfNConfig:
        """Build a config from a (possibly partial) ``best_of_n:`` mapping.

        Unknown keys are ignored and missing keys keep their defaults, so
        older or hand-edited config files load without errors.
        """
        cfg = cls()
        if not isinstance(raw, dict):
            return cfg
        proposers = raw.get("proposers")
        if isinstance(proposers, list):
            parsed: list[ProposerSpec] = []
            for entry in proposers:
                if not isinstance(entry, dict) or "harness" not in entry:
                    continue
                parsed.append(
                    ProposerSpec(
                        harness=str(entry["harness"]),
                        enabled=bool(entry.get("enabled", True)),
                        count=max(1, int(entry.get("count", 1))),
                        model=entry.get("model") or None,
                        command=list(entry["command"]) if entry.get("command") else None,
                        env=dict(entry.get("env") or {}),
                    )
                )
            if parsed:
                cfg.proposers = parsed
        if isinstance(raw.get("proposal_model"), str):
            cfg.proposal_model = raw["proposal_model"]
        criteria = raw.get("criteria")
        if isinstance(criteria, dict) and criteria:
            cfg.criteria = {str(k): str(v) for k, v in criteria.items()}
        verifier = raw.get("verifier")
        if isinstance(verifier, dict):
            v = cfg.verifier
            v.model = str(verifier.get("model", v.model))
            v.base_url = str(verifier.get("base_url", v.base_url))
            v.api_key_env = str(verifier.get("api_key_env", v.api_key_env))
            v.api_key = verifier.get("api_key") or None
            v.n_evaluations = max(1, int(verifier.get("n_evaluations", v.n_evaluations)))
            v.pivots = max(1, int(verifier.get("pivots", v.pivots)))
            v.seed = int(verifier.get("seed", v.seed))
            v.max_workers = max(1, int(verifier.get("max_workers", v.max_workers)))
            if verifier.get("on_error") in ("tie", "raise"):
                v.on_error = verifier["on_error"]
        if "proposal_timeout_s" in raw:
            cfg.proposal_timeout_s = float(raw["proposal_timeout_s"])
        if "keep_proposal_branches" in raw:
            cfg.keep_proposal_branches = bool(raw["keep_proposal_branches"])
        return cfg


def load_best_of_n_config(repo_path: str | None = None) -> BestOfNConfig:
    """Load the effective Best-of-N config (global merged with project).

    With *repo_path* the project-local ``.codify/config.yaml`` is
    read from that repository rather than the process cwd, so
    server-driven runs honor per-repo overrides (e.g. a proposer
    ``command`` template) exactly like CLI runs started inside the repo.
    The two ``best_of_n:`` blocks merge shallowly, local keys winning.
    """
    from codify.config import load_global_config, load_local_config

    global_block = load_global_config().get("best_of_n")
    if repo_path is None:
        local_block = load_local_config().get("best_of_n")
    else:
        local_block = load_local_config(Path(repo_path) / ".codify" / "config.yaml").get(
            "best_of_n"
        )
    # Key-wise merge, local winning per key: a project that only overrides
    # ``proposers`` must not wipe the global ``verifier`` (and its stored
    # api_key) the way a top-level shallow replace would.
    merged: dict[str, Any] = {}
    for block in (global_block, local_block):
        if isinstance(block, dict):
            merged.update(block)
    return BestOfNConfig.from_dict(merged)


def save_best_of_n_config(config: BestOfNConfig, path: Path | None = None) -> Path:
    """Persist *config* under ``best_of_n:`` in the user-level config file.

    Other config keys are preserved.

    :returns: The path written.
    """
    import yaml

    from codify.config import global_config_path, load_global_config

    resolved = path or global_config_path()
    existing = load_global_config(resolved if path else None)
    existing["best_of_n"] = config.to_dict()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("w") as fh:
        yaml.safe_dump(existing, fh, sort_keys=False)
    return resolved


def harness_availability() -> dict[str, bool]:
    """Which known proposer harness CLIs resolve on PATH right now."""
    return {name: shutil.which(binary) is not None for name, binary in _HARNESS_BINARIES.items()}


# --------------------------------------------------------------------------
# Run engine
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    """One candidate produced by a proposer harness."""

    harness: str
    index: int
    branch: str
    worktree_path: str
    model: str | None = None
    transcript: str = ""
    diff: str = ""
    ok: bool = False
    error: str | None = None
    duration_s: float = 0.0

    def trajectory(self, problem: str) -> str:
        """Render the candidate as the trajectory string the verifier scores."""
        transcript = self.transcript[-_MAX_TRANSCRIPT_CHARS:]
        diff = self.diff[:_MAX_DIFF_CHARS] or "(no file changes)"
        return (
            f"# Task\n{problem}\n\n"
            f"# Agent transcript ({self.harness})\n{transcript}\n\n"
            f"# Final diff\n```diff\n{diff}\n```\n"
        )


@dataclass
class BestOfNResult:
    """Outcome of one Best-of-N run."""

    run_id: str
    winner_index: int
    winner: Proposal
    scores: list[float]
    ranking: list[int]
    n_comparisons: int
    proposals: list[Proposal]
    merge_commit: str | None = None


class BestOfNError(Exception):
    """A Best-of-N run could not produce a selected proposal."""


def render_transcript_line(harness: str, line: str) -> str | None:
    """Turn one raw stdout line into readable transcript text.

    claude's ``--output-format stream-json`` and pi's ``--mode json``
    emit one JSON event per line; this renders assistant text, tool
    calls, and the final result as plain text and drops the noisy
    payload events (``None`` = skip the line). Every other harness's
    output passes through unchanged.
    """
    if harness not in ("claude", "pi"):
        return line
    stripped = line.strip()
    if not stripped.startswith("{"):
        return line
    try:
        event = json.loads(stripped)
    except ValueError:
        return line
    etype = event.get("type")
    if harness == "pi":
        if etype == "session":
            return "[pi session started]\n"
        if etype == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                return None
            parts: list[str] = []
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif btype in ("toolCall", "tool_call", "tool_use"):
                    arguments = block.get("arguments") or block.get("input") or {}
                    detail = ""
                    if isinstance(arguments, dict):
                        detail = str(
                            arguments.get("command")
                            or arguments.get("file_path")
                            or arguments.get("path")
                            or ""
                        )[:120]
                    parts.append(f"→ {block.get('name', 'tool')}({detail})")
            return "\n".join(parts) + "\n" if parts else None
        return None
    if etype == "system":
        model = event.get("model")
        return f"[claude session started{' · ' + model if model else ''}]\n"
    if etype == "assistant":
        parts: list[str] = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                detail = (
                    tool_input.get("command")
                    or tool_input.get("file_path")
                    or tool_input.get("pattern")
                    or ""
                )
                detail = str(detail)[:120]
                parts.append(f"→ {block.get('name', 'tool')}({detail})")
        return "\n".join(parts) + "\n" if parts else None
    if etype == "result":
        result = event.get("result")
        return f"\n[result] {result}\n" if isinstance(result, str) else None
    return None


def render_transcript(harness: str, text: str) -> str:
    """Render a whole captured stdout through :func:`render_transcript_line`."""
    rendered = (render_transcript_line(harness, line + "\n") for line in text.splitlines())
    return "".join(chunk for chunk in rendered if chunk)


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def ensure_repo_ready(repo_path: str) -> str | None:
    """Make *repo_path* a git repository with at least one commit.

    Best-of-N needs a base commit to branch worktrees from and to merge
    the winner onto. Three cases:

    - Not a git repository → ``git init`` and commit everything present
      as a baseline ("Initial commit" — empty commit if the folder is
      empty), so brand-new project folders work out of the box.
    - Repository with an unborn HEAD (init'd, never committed) → same
      baseline commit of whatever is staged/present.
    - Normal repository → untouched (the caller's clean-tree check still
      applies, so uncommitted work in an established repo is never
      swept up silently).

    :returns: A human-readable note describing what was done, or
        ``None`` when the repository was already ready.
    :raises BestOfNError: When the path is missing or git init/commit
        fails.
    """
    if not Path(repo_path).is_dir():
        raise BestOfNError(f"repository path does not exist: {repo_path}")
    is_repo = _run_git(["rev-parse", "--git-dir"], cwd=repo_path).returncode == 0
    note: str | None = None
    if not is_repo:
        init = _run_git(["init", "-q"], cwd=repo_path)
        if init.returncode != 0:
            raise BestOfNError(f"git init failed: {init.stderr.strip()}")
        note = "initialized a new git repository"
    if _run_git(["rev-parse", "HEAD"], cwd=repo_path).returncode == 0:
        return note
    _run_git(["add", "-A"], cwd=repo_path)
    commit = _run_git(
        [
            *_GIT_IDENTITY,
            "commit",
            "--allow-empty",
            "-m",
            "Initial commit (codify best-of-n baseline)",
        ],
        cwd=repo_path,
    )
    if commit.returncode != 0:
        raise BestOfNError(f"could not create the baseline commit: {commit.stderr.strip()}")
    return (note + " and " if note else "") + "committed the existing files as the baseline"


def _build_command(spec: ProposerSpec, prompt: str, default_model: str) -> list[str]:
    """Expand a proposer's argv template with the prompt and model.

    The run-level default model only reaches gateway-capable harnesses;
    a per-proposer ``model`` override is always honored.
    """
    if spec.model:
        model = spec.model
    elif spec.harness in _GATEWAY_MODEL_HARNESSES or spec.command is not None:
        model = default_model
    else:
        model = ""
    template = list(spec.command) if spec.command else list(_HARNESS_COMMANDS[spec.harness])
    argv = [arg.replace("{prompt}", prompt).replace("{model}", model) for arg in template]
    if spec.command is None and model:
        model_args = _HARNESS_MODEL_ARGS.get(spec.harness, [])
        argv += [arg.replace("{model}", model) for arg in model_args]
    return argv


def ensure_verifier_key_env() -> None:
    """Export the stored verifier API key into this process's environment.

    Agent bundles reference the key as ``${DEEPSEEK_API_KEY}`` (the
    configured ``api_key_env``); spec parsing expands that against the
    *process* env at turn setup, so every process that loads specs (CLI,
    server, runner) calls this at startup. Best-effort and idempotent: a
    key already present in the environment always wins, and any config
    read failure leaves the environment untouched.
    """
    try:
        verifier = load_best_of_n_config().verifier
        if verifier.api_key and not os.environ.get(verifier.api_key_env):
            os.environ[verifier.api_key_env] = verifier.api_key
    except Exception:  # noqa: BLE001 — never block startup on config issues
        _logger.debug("could not auto-load verifier key into env", exc_info=True)


def proposal_env(spec: ProposerSpec, config: BestOfNConfig) -> dict[str, str]:
    """Build the environment for one proposer harness process.

    Auto-loads the stored verifier API key: when the configured key env
    var isn't already set in the process environment, the key persisted
    at ``best_of_n.verifier.api_key`` is injected under that name, so
    server-launched runs (web UI) work without any shell exports —
    proposers that ride the same key (pi's DeepSeek provider, claude's
    ``ANTHROPIC_AUTH_TOKEN`` env template) all see it.
    """
    env = {**os.environ, **spec.env}
    key_env = config.verifier.api_key_env
    key = env.get(key_env) or config.verifier.api_key
    if key:
        env[key_env] = key
    return env


def _run_proposal(
    proposal: Proposal,
    spec: ProposerSpec,
    prompt: str,
    base_commit: str,
    config: BestOfNConfig,
) -> Proposal:
    """Run one harness headlessly in its worktree and commit its changes."""
    import time

    argv = _build_command(spec, prompt, config.proposal_model)
    env = proposal_env(spec, config)
    start = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=proposal.worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=config.proposal_timeout_s,
        )
        proposal.transcript = render_transcript(spec.harness, completed.stdout) + (
            f"\n[stderr]\n{completed.stderr}" if completed.stderr.strip() else ""
        )
        if completed.returncode != 0:
            proposal.error = f"{spec.harness} exited with code {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        proposal.transcript = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        proposal.error = f"{spec.harness} timed out after {config.proposal_timeout_s:.0f}s"
    except OSError as exc:
        proposal.error = f"failed to launch {spec.harness}: {exc}"
    proposal.duration_s = time.monotonic() - start
    snapshot_proposal(proposal, base_commit)
    return proposal


def snapshot_proposal(proposal: Proposal, base_commit: str) -> None:
    """Commit whatever the harness produced and capture its diff.

    Runs even on a nonzero harness exit — a partial proposal can still
    win if every other candidate is worse. Sets ``diff`` and ``ok``.
    """
    _run_git(["add", "-A"], cwd=proposal.worktree_path)
    commit = _run_git(
        [
            *_GIT_IDENTITY,
            "commit",
            "-m",
            f"best-of-n proposal: {proposal.harness} #{proposal.index}",
        ],
        cwd=proposal.worktree_path,
    )
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout + commit.stderr:
        proposal.error = proposal.error or f"git commit failed: {commit.stderr.strip()}"
    diff = _run_git(["diff", base_commit, "HEAD"], cwd=proposal.worktree_path)
    proposal.diff = diff.stdout if diff.returncode == 0 else ""
    proposal.ok = proposal.error is None or bool(proposal.diff or proposal.transcript.strip())


class _DeepSeekPrefillCompletions:
    """Adapt ``llm_verifier``'s vLLM-style prefill calls to DeepSeek.

    The verifier reads score-letter distributions by *continuing* a
    prefilled assistant message (vLLM/SGLang ``continue_final_message``).
    The DeepSeek API ignores that hint and would start a fresh reasoning
    block instead — with ``max_tokens=1`` the content comes back empty and
    every score silently falls back to 0.5. DeepSeek's equivalent is the
    beta "chat prefix completion": ``{"prefix": true}`` on the trailing
    assistant message against the ``/beta`` base URL, which continues the
    prefill and returns real logprobs at the score position.
    """

    def __init__(self, main: Any, beta: Any) -> None:
        self._main = main
        self._beta = beta

    def create(self, **kwargs: Any) -> Any:
        extra = dict(kwargs.pop("extra_body", None) or {})
        messages = kwargs.get("messages") or []
        if (
            extra.get("continue_final_message")
            and messages
            and (messages[-1].get("role") == "assistant")
        ):
            rewritten = [dict(m) for m in messages]
            rewritten[-1]["prefix"] = True
            kwargs["messages"] = rewritten
            return self._beta.chat.completions.create(**kwargs)
        # The remaining extra hints (chat_template_kwargs etc.) are
        # vLLM-only; DeepSeek's strict parser rejects unknown fields.
        return self._main.chat.completions.create(**kwargs)


class _DeepSeekVerifierClient:
    """OpenAI-compatible facade routing prefill calls to the beta API."""

    def __init__(self, main: Any, beta: Any) -> None:
        self.chat = type("_Chat", (), {"completions": _DeepSeekPrefillCompletions(main, beta)})()


def create_verifier_client(settings: VerifierSettings) -> Any:
    """Build the OpenAI-compatible client the verifier scores with.

    DeepSeek endpoints get a prefill adapter (see
    :class:`_DeepSeekPrefillCompletions`); other OpenAI-compatible
    backends (vLLM, SGLang, OpenAI) are used as-is.
    """
    api_key = os.environ.get(settings.api_key_env) or settings.api_key
    if not api_key:
        raise BestOfNError(
            f"verifier API key not found: set {settings.api_key_env} in the environment "
            f"or best_of_n.verifier.api_key in the config"
        )
    from openai import OpenAI

    main = OpenAI(base_url=settings.base_url, api_key=api_key)
    if "api.deepseek.com" not in settings.base_url:
        return main
    beta_url = settings.base_url.rstrip("/").removesuffix("/v1") + "/beta"
    beta = OpenAI(base_url=beta_url, api_key=api_key)
    return _DeepSeekVerifierClient(main, beta)


def select_best(
    problem: str,
    candidates: list[str],
    config: BestOfNConfig | None = None,
) -> Any:
    """Rank candidate trajectories with ``llm_verifier.select``.

    Thin wrapper that applies the configured criteria and verifier
    settings; returns the ``llm_verifier.VerifierResult``.
    """
    config = config or load_best_of_n_config()
    try:
        import llm_verifier
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise BestOfNError(
            "llm_verifier is not installed. Install it with "
            "`pip install git+https://github.com/llm-as-a-verifier/llm-as-a-verifier`"
        ) from exc
    v = config.verifier
    return llm_verifier.select(
        problem=problem,
        candidates=candidates,
        criteria=config.criteria,
        ground_truth_note=GROUND_TRUTH_NOTE,
        n_evaluations=v.n_evaluations,
        pivots=v.pivots,
        seed=v.seed,
        max_workers=v.max_workers,
        model=v.model,
        client=create_verifier_client(v),
        progress=False,
        on_error=v.on_error,
    )


def run_best_of_n(
    problem: str,
    repo_path: str,
    config: BestOfNConfig | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> BestOfNResult:
    """Run the full Best-of-N flow against *repo_path*.

    Fans the task out to every enabled proposer (one git worktree +
    branch per candidate), scores the resulting trajectories with the
    LLM verifier, squash-merges the winning branch into the repo's
    current branch, and removes the losing worktrees (branches are kept
    only when ``keep_proposal_branches`` is set).

    :raises BestOfNError: On a dirty base repo, no launchable proposers,
        or no usable candidate trajectories.
    """
    config = config or load_best_of_n_config()
    say = progress or (lambda _msg: None)

    if note := ensure_repo_ready(repo_path):
        say(note)
    head = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    if head.returncode != 0:
        raise BestOfNError(f"could not resolve HEAD in {repo_path}: {head.stderr.strip()}")
    base_commit = head.stdout.strip()
    dirty = _run_git(["status", "--porcelain"], cwd=repo_path)
    if dirty.stdout.strip():
        raise BestOfNError(
            "the repository has uncommitted changes; commit or stash them first "
            "so the winning proposal can be merged cleanly"
        )

    available = harness_availability()
    slots: list[ProposerSpec] = []
    for spec in config.proposers:
        if not spec.enabled:
            continue
        if spec.command is None and spec.harness not in _HARNESS_COMMANDS:
            say(f"skipping unknown harness {spec.harness!r} (no command template)")
            continue
        if spec.command is None and not available.get(spec.harness, False):
            say(f"skipping {spec.harness}: CLI not found on PATH")
            continue
        slots.extend([spec] * spec.count)
    if not slots:
        raise BestOfNError("no enabled proposer harness is launchable on this machine")

    run_id = uuid.uuid4().hex[:8]
    proposals: list[Proposal] = []
    for i, spec in enumerate(slots):
        branch = f"{_BRANCH_PREFIX}/{run_id}/{spec.harness}-{i}"
        created = create_worktree(repo_path=repo_path, branch_name=branch)
        proposals.append(
            Proposal(
                harness=spec.harness,
                index=i,
                branch=created.branch,
                worktree_path=created.worktree_path,
                model=spec.model or config.proposal_model,
            )
        )
    say(
        f"run {run_id}: {len(proposals)} proposal(s) from "
        f"{', '.join(dict.fromkeys(s.harness for s in slots))}"
    )

    with ThreadPoolExecutor(max_workers=len(proposals)) as pool:
        proposals = list(
            pool.map(
                lambda pair: _run_proposal(pair[0], pair[1], problem, base_commit, config),
                zip(proposals, slots, strict=True),
            )
        )
    for p in proposals:
        status = "ok" if p.ok else f"failed ({p.error})"
        say(f"  {p.harness}-{p.index}: {status}, {len(p.diff)} diff chars, {p.duration_s:.0f}s")

    usable = [p for p in proposals if p.ok]
    if not usable:
        _cleanup(proposals, config, keep_winner=None)
        raise BestOfNError(
            "no proposal produced a usable trajectory; errors: "
            + "; ".join(f"{p.harness}-{p.index}: {p.error}" for p in proposals)
        )

    say(f"scoring {len(usable)} candidate(s) with {config.verifier.model}")
    winner: Proposal | None = None
    try:
        result = select_best(problem, [p.trajectory(problem) for p in usable], config)
        winner = usable[result.index]
        scores = [0.0] * len(proposals)
        for p, score in zip(usable, result.scores, strict=True):
            scores[p.index] = score
        ranking = [usable[i].index for i in result.ranking]
        say(
            f"winner: {winner.harness}-{winner.index} "
            f"(score {result.scores[result.index]:.3f}, {result.n_comparisons} comparisons)"
        )
        merge_commit = _merge_winner(repo_path, winner, result, config)
        return BestOfNResult(
            run_id=run_id,
            winner_index=winner.index,
            winner=winner,
            scores=scores,
            ranking=ranking,
            n_comparisons=result.n_comparisons,
            proposals=proposals,
            merge_commit=merge_commit,
        )
    finally:
        # Selection or merge failing must not strand the proposal
        # worktrees; on failure every branch is treated as a loser.
        _cleanup(proposals, config, keep_winner=winner)


def _merge_winner(
    repo_path: str, winner: Proposal, result: Any, config: BestOfNConfig
) -> str | None:
    """Squash-merge the winning branch so the base branch gains one commit."""
    if not winner.diff.strip():
        return None
    merged = _run_git(["merge", "--squash", winner.branch], cwd=repo_path)
    if merged.returncode != 0:
        raise BestOfNError(f"failed to merge winning branch {winner.branch}: {merged.stderr}")
    message = (
        f"Best-of-N: apply selected proposal from {winner.harness}\n\n"
        f"Selected by llm_verifier.select ({config.verifier.model}) with score "
        f"{result.scores[result.index]:.3f} over {len(result.scores)} candidates "
        f"({result.n_comparisons} pairwise comparisons).\n"
        f"Criteria: {', '.join(result.criteria)}\n"
        f"Proposal branch: {winner.branch}"
    )
    committed = _run_git([*_GIT_IDENTITY, "commit", "-m", message], cwd=repo_path)
    if committed.returncode != 0:
        raise BestOfNError(f"failed to commit winning proposal: {committed.stderr}")
    return _run_git(["rev-parse", "HEAD"], cwd=repo_path).stdout.strip()


def _cleanup(
    proposals: list[Proposal], config: BestOfNConfig, keep_winner: Proposal | None
) -> None:
    """Remove proposal worktrees; drop losing branches unless configured."""
    for p in proposals:
        keep_branch = config.keep_proposal_branches or p is keep_winner
        try:
            remove_worktree(
                worktree_path=p.worktree_path,
                branch=p.branch,
                delete_branch=not keep_branch,
            )
        except WorktreeError:
            _logger.debug("failed to clean up worktree %s", p.worktree_path, exc_info=True)


__all__ = [
    "DEFAULT_CRITERIA",
    "GROUND_TRUTH_NOTE",
    "KNOWN_PROPOSER_HARNESSES",
    "BestOfNConfig",
    "BestOfNError",
    "BestOfNResult",
    "Proposal",
    "ProposerSpec",
    "VerifierSettings",
    "harness_availability",
    "load_best_of_n_config",
    "run_best_of_n",
    "save_best_of_n_config",
    "select_best",
]
