"""CLI surface for the Best-of-N verifier (``omni best-of-n``).

Kept out of :mod:`codify.cli` so the 10k-line root CLI module stays
navigable; :func:`register_best_of_n_commands` is called once at import
time from ``cli.py``, mirroring ``cli_native.register_native_commands``.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import click


def register_best_of_n_commands(cli: click.Group) -> None:
    """Attach the ``best-of-n`` command (alias ``bon``) to the root group."""

    @cli.command(name="best-of-n")
    @click.argument("prompt", required=False)
    @click.option(
        "--repo",
        "repo_path",
        default=".",
        show_default=True,
        help="Git repository the proposals run against.",
    )
    @click.option(
        "--harness",
        "harnesses",
        multiple=True,
        help="Restrict proposers to these harnesses (repeatable; default: configured roster).",
    )
    @click.option("--model", default=None, help="Override the proposal model for this run.")
    @click.option(
        "--keep-branches",
        is_flag=True,
        default=False,
        help="Keep every proposal branch instead of deleting the losers.",
    )
    @click.option("--show-config", is_flag=True, default=False, help="Print the effective config.")
    @click.option(
        "--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON."
    )
    @click.option(
        "--select",
        "select_only",
        is_flag=True,
        default=False,
        help="Selection only: rank pre-built candidate files, no fan-out or merge.",
    )
    @click.option(
        "--problem-file",
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="File holding the task description (with --select).",
    )
    @click.option(
        "--candidate",
        "candidates",
        multiple=True,
        type=click.Path(exists=True, dir_okay=False),
        help="Candidate trajectory file (repeatable, order = candidate index; with --select).",
    )
    def best_of_n(
        prompt: str | None,
        repo_path: str,
        harnesses: tuple[str, ...],
        model: str | None,
        keep_branches: bool,
        show_config: bool,
        as_json: bool,
        select_only: bool,
        problem_file: str | None,
        candidates: tuple[str, ...],
    ) -> None:
        """Run one task through the Best-of-N multi-harness verifier.

        Fans PROMPT out to the configured proposer harnesses (each in its
        own git worktree + branch), ranks the trajectories with
        ``llm_verifier.select``, and squash-merges the winning proposal
        into the current branch — the final commit is the selected one.

        \b
        Examples:
          omni best-of-n "add input validation to the signup form"
          omni best-of-n --harness claude --harness pi "fix the flaky test"
          omni best-of-n --show-config
        """
        from codify.best_of_n import (
            BestOfNError,
            load_best_of_n_config,
            run_best_of_n,
            select_best,
        )

        config = load_best_of_n_config(repo_path)
        if show_config:
            click.echo(json.dumps(config.to_dict(), indent=2))
            return
        if select_only:
            # Selection-as-a-step: the Best-of-N session agent fans the task
            # out to real harness sub-sessions itself, then calls this to rank
            # the collected trajectories deterministically.
            if problem_file is None or len(candidates) < 2:
                raise click.UsageError(
                    "--select needs --problem-file and at least two --candidate files"
                )
            problem = _read_text(problem_file)
            texts = [_read_text(path) for path in candidates]
            try:
                result = select_best(problem, texts, config)
            except BestOfNError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(
                json.dumps(
                    {
                        "index": result.index,
                        "winner": candidates[result.index],
                        "scores": result.scores,
                        "ranking": result.ranking,
                        "n_comparisons": result.n_comparisons,
                    },
                    indent=2,
                )
            )
            return
        if not prompt:
            raise click.UsageError("PROMPT is required unless --show-config or --select is given")
        if harnesses:
            config.proposers = [p for p in config.proposers if p.harness in harnesses]
            for name in harnesses:
                if not any(p.harness == name for p in config.proposers):
                    raise click.UsageError(f"harness {name!r} is not in the configured roster")
        if model:
            config.proposal_model = model
        if keep_branches:
            config.keep_proposal_branches = True

        try:
            result = run_best_of_n(
                prompt,
                repo_path,
                config,
                progress=lambda msg: click.echo(msg, err=True),
            )
        except BestOfNError as exc:
            raise click.ClickException(str(exc)) from exc

        if as_json:
            payload = asdict(result)
            for proposal in payload["proposals"]:
                proposal.pop("transcript", None)
            click.echo(json.dumps(payload, indent=2))
            return
        click.echo(
            f"selected {result.winner.harness}-{result.winner_index} "
            f"(score {result.scores[result.winner_index]:.3f}) "
            f"after {result.n_comparisons} verifier comparisons"
        )
        if result.merge_commit:
            click.echo(f"merged as {result.merge_commit[:12]}")
        else:
            click.echo("winning proposal had no file changes; nothing merged", err=True)

    # Short alias, mirroring `omni polly` / `omni debby` ergonomics.
    cli.add_command(best_of_n, name="bon")


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()
