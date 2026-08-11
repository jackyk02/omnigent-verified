"""
Unit tests for ``codify pane-picker``'s argv normalization.

The picker is exec'd as the new tmux pane's initial command after a
``pane-split``. It reads the parent pane's launch context, strips
flags that don't make sense for a sibling pane (resume modes,
one-shot prompts), then ``os.execvp``\\s into a fresh REPL.

These tests pin the strip helpers — the real exec path is exercised
manually in the design's § 6 phase 5 verification.
"""

from __future__ import annotations

import pytest

from codify.cli import _strip_one_shot_flags, _strip_resume_flags


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Bare ``--resume`` (picker mode): drop the single token.
        (
            ["codify", "run", "a.yaml", "--profile", "prf", "--resume"],
            ["codify", "run", "a.yaml", "--profile", "prf"],
        ),
        # ``--resume`` with a conversation id: drop both tokens.
        (
            ["codify", "run", "a.yaml", "--resume", "conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        # ``--resume=conv_id`` long-form: drop the combined token.
        (
            ["codify", "run", "a.yaml", "--resume=conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        # ``-r`` short form, no value: drop the single token.
        (
            ["codify", "run", "a.yaml", "-r"],
            ["codify", "run", "a.yaml"],
        ),
        # ``-r conv_id`` short form with value: drop both tokens.
        (
            ["codify", "run", "a.yaml", "-r", "conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        # Continue forms (always boolean).
        (
            ["codify", "run", "a.yaml", "-c"],
            ["codify", "run", "a.yaml"],
        ),
        (
            ["codify", "run", "a.yaml", "--continue"],
            ["codify", "run", "a.yaml"],
        ),
        # Legacy ``--session`` / ``-s`` shapes still strip cleanly so
        # a parent argv saved before the resume/session consolidation
        # sanitizes without errors.
        (
            ["codify", "run", "a.yaml", "--session", "conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        (
            ["codify", "run", "a.yaml", "-s", "conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        (
            ["codify", "run", "a.yaml", "--session=conv_abc"],
            ["codify", "run", "a.yaml"],
        ),
        # Multiple resume flags in one argv: all dropped.
        (
            [
                "codify",
                "run",
                "a.yaml",
                "--profile",
                "prf",
                "--resume",
                "--continue",
                "--resume",
                "conv_x",
            ],
            ["codify", "run", "a.yaml", "--profile", "prf"],
        ),
        # Non-resume flags survive intact even when sandwiched
        # between resume flags. Bare ``--resume`` followed by
        # another flag must NOT swallow that flag as its value.
        (
            [
                "codify",
                "run",
                "a.yaml",
                "--resume",
                "--profile",
                "prf",
                "--resume",
                "x",
                "--model",
                "m",
            ],
            ["codify", "run", "a.yaml", "--profile", "prf", "--model", "m"],
        ),
        # Empty argv → empty.
        ([], []),
        # Non-resume argv: identity.
        (
            ["codify", "run", "a.yaml", "--model", "m", "--profile", "prf"],
            ["codify", "run", "a.yaml", "--model", "m", "--profile", "prf"],
        ),
    ],
)
def test_strip_resume_flags(argv: list[str], expected: list[str]) -> None:
    """
    The strip helper must remove every shape of resume flag
    (bare ``--resume`` for the picker, ``--resume <id>`` for an
    explicit pin, the ``--resume=<id>`` long form, short ``-r``
    variants, and ``--continue`` / ``-c``) and leave every other
    flag untouched. Legacy ``--session`` / ``-s`` are still
    handled for backwards compatibility with parent argvs saved
    before the consolidation.

    Claim: each input → its expected pruned argv. Live regression
    that prompted this helper: the live pane's argv had
    ``--resume``, the click ``run`` subcommand at the time didn't
    accept that option, so exec'ing the parent's verbatim argv
    exited with a click ``Error: No such option: --resume``
    immediately, closing the new pane within seconds.
    """
    assert _strip_resume_flags(argv) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # ``-p`` short form: drop the flag and its value.
        (
            ["codify", "run", "a.yaml", "-p", "hello there"],
            ["codify", "run", "a.yaml"],
        ),
        # ``--prompt`` long form.
        (
            ["codify", "run", "a.yaml", "--prompt", "hello"],
            ["codify", "run", "a.yaml"],
        ),
        # ``--prompt=value``.
        (
            ["codify", "run", "a.yaml", "--prompt=hello"],
            ["codify", "run", "a.yaml"],
        ),
        # ``--system-prompt`` (note: spans both an arg-bearing flag
        # and a similarly named flag — make sure we don't strip
        # ``--system`` or ``--prompt-foo`` accidentally).
        (
            ["codify", "run", "a.yaml", "--system-prompt", "be terse"],
            ["codify", "run", "a.yaml"],
        ),
    ],
)
def test_strip_one_shot_flags(argv: list[str], expected: list[str]) -> None:
    """
    One-shot flags (``-p``, ``--prompt``, ``--system-prompt``) tied
    to the parent's first turn must be removed before exec'ing in
    the new pane — otherwise the new pane silently auto-sends the
    parent's prompt, surprising the user.

    Claim: every variant of one-shot flag is removed; everything
    else passes through.
    """
    assert _strip_one_shot_flags(argv) == expected
