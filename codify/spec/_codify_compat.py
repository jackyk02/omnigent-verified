"""Codify compatibility surface — bundled for surgical removal.

🚨 **TECH DEBT — REMOVE WHEN CODIFY COMPAT WORKSTREAM ENDS.**
This entire module exists *only* to support the Codify
integration (see ``designs/CODIFY_INTEGRATION.md``). It
consolidates every codify-specific addition that would otherwise
be scattered across ``validator.py``, ``spec/__init__.py``, and
``runtime/workflow.py``.

When Codify is consolidated (phase 6 of the integration design),
deleting Codify support means:

1. Delete this file.
2. Remove the few lines in ``validator.py``,
   ``spec/__init__.py``, and ``runtime/workflow.py`` that import
   from it (each has a single import + a single call site —
   grep for ``_codify_compat`` to find them).
3. Delete ``codify/spec/codify.py`` (the bidirectional
   translator).
4. The Codify executor module is already gone (it held an
   experimental executor ABC that has since been removed), so
   there is nothing left to delete here.
5. Remove ``ExecutorSpec.config`` from
   ``codify/spec/types.py`` (the only field that couldn't
   move here because Python dataclasses don't support
   externally-added fields).

That's it. No grep-the-codebase exercise; the surface is
intentionally tiny.

**Do NOT** treat this module as a general-purpose extension point
for new executor types. Add concrete typed fields and dedicated
validator branches instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from codify.errors import CodifyError, ErrorCode
from codify.harness_aliases import canonicalize_harness
from codify.harness_plugins import (
    accepted_harnesses,
    missing_install_packages,
    valid_harnesses,
)
from codify.harness_plugins import (
    harness_aliases as registry_harness_aliases,
)

if TYPE_CHECKING:
    from codify.spec.types import AgentSpec
    from codify.spec.validator import ValidationResult


# ── Constants ──────────────────────────────────────────────────


# Value placed in :attr:`AgentSpec.executor.type` so the runtime
# selects ``CodifyExecutor``. Single source of truth — every
# codify-aware site imports from here, no string duplication.
CODIFY_EXECUTOR_TYPE = "codify"


# Harness identifiers accepted by ``executor.config.harness`` when
# ``executor.type == "codify"``. Matches the set of internal-loop
# harnesses ``CodifyExecutor`` wraps. ``databricks`` is
# intentionally excluded — codify has a native databricks
# adapter, so an codify+databricks
# pairing is a spec misconfiguration. See
# designs/CODIFY_INTEGRATION.md §1.
#
# ``open-responses`` is the OpenAI Responses-API harness that
# ``codify.inner.open_responses_sdk.OpenResponsesExecutor``
# implements; the executor_factory resolves it when the YAML
# declares ``harness: open-responses``, so the adapter must
# accept it too. It was missing from the initial allowlist, which
# made ``examples/terminal_workers.yaml``
# fail at spec-load with a "must be one of [...], got
# 'open-responses'" error.
#
# ``opencode-native`` is the native OpenCode server bridge (runner-owned
# ``opencode serve`` + SSE forwarder); its ``opencode`` / ``native-opencode``
# spellings are accepted aliases below.
CODIFY_HARNESSES = frozenset(
    {
        "antigravity",
        "antigravity-native",
        "claude-native",
        "claude-sdk",
        "codex",
        "codex-native",
        "copilot",
        "cursor",
        "kimi",
        "kimi-native",
        "cursor-native",
        "kiro-native",
        "goose",
        "goose-native",
        "hermes",
        "hermes-native",
        "openai-agents",
        "open-responses",
        "opencode-native",
        "pi",
        "pi-native",
        "qwen",
        "qwen-native",
    },
)
# User-facing aliases accepted in specs and normalized before runtime dispatch.
CODIFY_HARNESS_ALIASES = frozenset(
    {
        "claude",
        "native-kiro",
        "native-pi",
        "native-antigravity",
        "native-goose",
        "openai-agents-sdk",
        "agy",
        "google-antigravity",
        "kimi-code",
        "qwen-code",
        "opencode",
        "native-opencode",
        "native-hermes",
        "github-copilot",
        "native-kimi",
    }
)
_CODIFY_ACCEPTED_HARNESSES = CODIFY_HARNESSES | CODIFY_HARNESS_ALIASES

# Dynamic registry overlay. The literals above remain as readable documentation
# for the built-in set, while the exported constants reflect installed
# community harness plugins.
CODIFY_HARNESSES = valid_harnesses()
CODIFY_HARNESS_ALIASES = frozenset(registry_harness_aliases())
_CODIFY_ACCEPTED_HARNESSES = accepted_harnesses()


# Top-level YAML keys that identify an codify single-file
# agent spec. ``name`` is always required. The system-prompt key
# may be either ``prompt:`` (legacy codify) or
# ``instructions:`` (cross-format alias added to match native AP
# YAML). At least one must be present so the agent has a system
# prompt; YAMLs with neither still fail loud at translation time
# (the resulting agent would have no instructions, which is
# nearly always a typo).
_CODIFY_NAME_KEY = "name"
_CODIFY_SYSTEM_PROMPT_KEYS = frozenset({"prompt", "instructions"})
_CODIFY_DISCRIMINATOR_KEY = "spec_version"


# ── Validator: codify executor branch ──────────────────────


def validate_codify_executor(
    spec: AgentSpec,
    result: ValidationResult,
) -> None:
    """
    Validate fields for ``executor.type: codify``.

    The codify executor wraps an codify harness subprocess.
    ``executor.config.harness`` is optional — when absent, the
    codify factory selects a default. When set, it must be one
    of :data:`CODIFY_HARNESSES`. ``executor.config.profile`` is
    always optional and names a Databricks credential profile when
    the harness routes through Databricks.

    The codify harness manages its own context window, so
    ``compaction`` is invalid.

    :param spec: The agent spec to check.
    :param result: Accumulator for any validation errors found.
    """
    if spec.compaction is not None:
        result.add(
            "compaction",
            f"not supported when executor.type is {CODIFY_EXECUTOR_TYPE!r}"
            " — harness manages context internally",
        )
    harness = spec.executor.config.get("harness")
    if not harness:
        result.add(
            "executor.config.harness",
            f"required when executor.type is {CODIFY_EXECUTOR_TYPE!r} — "
            f"must be one of {sorted(_CODIFY_ACCEPTED_HARNESSES)}",
        )
    elif canonicalize_harness(harness) not in CODIFY_HARNESSES:
        package = missing_install_packages().get(harness) or missing_install_packages().get(
            canonicalize_harness(harness) or harness
        )
        install_hint = f"; install `{package}` to add this harness" if package else ""
        result.add(
            "executor.config.harness",
            f"must be one of {sorted(_CODIFY_ACCEPTED_HARNESSES)}, got {harness!r}{install_hint}",
        )


# ── YAML detection + loading ───────────────────────────────────


def is_codify_yaml(path: Path) -> bool:
    """
    Return ``True`` if *path* is an codify single-file YAML spec.

    Detection rule (from CODIFY_INTEGRATION design):

    - The file extension is ``.yaml`` or ``.yml``.
    - The top-level YAML document is a mapping.
    - The mapping has both ``name`` AND ``prompt`` keys.
    - The mapping does NOT have a ``spec_version`` key (which would
      identify an codify spec).

    Malformed YAML or non-mapping root documents return ``False`` —
    the caller (``load``) then takes its existing path and raises an
    informative error downstream.

    :param path: Path to a file on disk, already known to exist.
    :returns: ``True`` when *path* is an codify YAML per the rule
        above, ``False`` otherwise.
    """
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return False
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return False
    if not isinstance(raw, dict):
        return False
    if _CODIFY_DISCRIMINATOR_KEY in raw:
        return False
    if _CODIFY_NAME_KEY not in raw:
        return False
    # At least one system-prompt key must be present.
    return bool(_CODIFY_SYSTEM_PROMPT_KEYS.intersection(raw.keys()))


def diagnose_yaml_rejection(path: Path) -> str:
    """
    Explain why *path* failed :func:`is_codify_yaml`.

    Used by ``codify.spec.load`` to produce an actionable error
    message when a ``.yaml`` / ``.yml`` file is passed in but
    doesn't satisfy the codify-YAML detection rule. Without
    this, ``load`` falls through to the tarball-extraction branch
    and emits ``"dest is required when loading from a tarball"`` —
    technically correct (the path isn't a known YAML shape and
    isn't a directory) but useless to the user, who edited a YAML
    file and wants to know what's wrong with it.

    The return value is a single-line human-readable diagnosis
    suitable for embedding in an :class:`CodifyError` message.

    :param path: A file path that already failed
        :func:`is_codify_yaml`. Caller is responsible for
        ensuring ``path.exists()`` and ``path.is_file()``.
    :returns: A short diagnostic string explaining the rejection,
        e.g. ``"missing required key 'prompt'"``,
        ``"top-level YAML must be a mapping (got list)"``, or
        ``"YAML parse error at line 3"``.
    """
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return f"file extension is {path.suffix!r}, expected '.yaml' or '.yml'"
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        # Strip trailing whitespace so the message stays one line —
        # PyYAML embeds the source location in its error string,
        # which is exactly what the user needs to fix the typo.
        return f"YAML parse error: {exc!s}".replace("\n", " ").rstrip()
    if raw is None:
        return "file is empty (or contains only YAML comments / null)"
    if not isinstance(raw, dict):
        return (
            f"top-level YAML must be a mapping (got "
            f"{type(raw).__name__}); expected keys 'name' and 'prompt'"
        )
    if _CODIFY_DISCRIMINATOR_KEY in raw:
        return (
            "file declares 'spec_version' which marks it as an codify "
            "spec — codify specs must live in a directory with a "
            "'config.yaml' (and any bundled assets), not as a single "
            "YAML file. Either remove 'spec_version' (to use the "
            "codify single-file format) or move the YAML into a "
            "bundle directory named 'config.yaml'."
        )
    if _CODIFY_NAME_KEY not in raw:
        return "missing required key 'name'. An codify YAML must declare a top-level 'name'."
    if not _CODIFY_SYSTEM_PROMPT_KEYS.intersection(raw.keys()):
        return (
            "missing system-prompt key. An codify YAML must declare "
            "either 'prompt:' (inline text) or 'instructions:' (path to "
            "a sibling file or inline text) at the top level."
        )
    # Should be unreachable: if all checks pass, ``is_codify_yaml``
    # would have returned True. Guard against a future divergence
    # between the two functions.
    return "unknown reason — file passes all known checks (likely an internal bug)"


def load_codify_yaml(
    path: Path,
    *,
    enforce_handler_allowlist: bool = False,
    prune_invalid_sub_agents: bool = False,
) -> AgentSpec:
    """
    Load an codify YAML and translate it to an
    :class:`AgentSpec`.

    Pipeline: ``codify.loader.load_agent_def(path)`` →
    :func:`codify.spec.codify.agent_def_to_agent_spec` →
    :func:`codify.spec.validator.validate`. Validation failure
    raises :class:`CodifyError` so the caller sees the specific
    field that doesn't translate (per the fail-loud discipline).

    :param path: Path to an codify YAML file. Caller has
        already verified via :func:`is_codify_yaml`.
    :param enforce_handler_allowlist: Forwarded to
        :func:`codify.inner.loader.load_agent_def` — when ``True``,
        unregistered ``type: function`` policy handlers are rejected
        before the loader resolves/calls them (bundle-upload
        guard). See :func:`codify.spec.load`.
    :param prune_invalid_sub_agents: When ``True``, sub-agents that
        fail validation are dropped (and their ``tools.agents``
        references removed) instead of failing the whole load, with a
        WARNING logged per drop. The root agent must still validate.
        See :func:`codify.spec.load` for the full rationale — this
        is the execution-path backwards-compatibility guard.
    :returns: A validated :class:`AgentSpec` with
        ``executor.type == CODIFY_EXECUTOR_TYPE``.
    :raises CodifyError: If the synthesized spec fails
        validation (e.g. policy translation gap), or if the
        ``codify`` package is not installed in the current
        Python environment.
    """
    try:
        from codify.inner.loader import load_agent_def
    except ImportError as exc:
        # Agent-plane can be pip-installed without the codify
        # source alongside (the repo layout has them as siblings,
        # but editable installs of codify into a fresh env
        # don't pull codify in). Surface a clear install hint
        # instead of a bare ``ModuleNotFoundError``.
        raise CodifyError(
            "loading codify-format YAMLs requires the "
            "``codify`` package to be importable. Install it "
            "(``pip install -e <codify-root>`` from the "
            "repo, or add the root to PYTHONPATH) and retry. The "
            "failing import was: "
            f"{exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    import yaml as _yaml

    from codify.inner.loader import _CodifyYamlLoader
    from codify.spec.codify import agent_def_to_agent_spec
    from codify.spec.validator import validate

    agent_def = load_agent_def(path, enforce_handler_allowlist=enforce_handler_allowlist)
    # Read the raw YAML alongside so the translator can preserve
    # policy-level YAML fields that the codify loader drops
    # (label policies in particular compile to synthetic
    # FunctionPolicy callables, losing ``condition``,
    # ``match_tools``, ``action``, ``reason``, ``set_labels``).
    # Non-mapping roots are tolerated as an empty dict — the
    # codify loader would already have rejected them above.
    # Use _CodifyYamlLoader (not yaml.safe_load) so this raw
    # read resolves booleans the same way load_agent_def's YAML
    # parsing did — both loaders keep on/off as plain strings
    # instead of the YAML 1.1 bool aliases.
    raw = _yaml.load(path.read_text(), Loader=_CodifyYamlLoader) or {}
    if not isinstance(raw, dict):
        raw = {}
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw)
    if prune_invalid_sub_agents:
        # Local import avoids a module-load cycle: spec/__init__ imports
        # this module at import time, so it cannot be imported at the top
        # here. Drops sub-agents this client can't validate (version skew)
        # before the root validation gate below; see codify.spec.load.
        from codify.spec import _prune_invalid_sub_agents

        _prune_invalid_sub_agents(spec)
    result = validate(spec)
    if not result.valid:
        errors = "; ".join(f"{e.path}: {e.message}" for e in result.errors)
        message = f"invalid agent spec synthesized from codify YAML: {errors}"
        # An unrecognized harness *value* usually means this client
        # (the codify runner validating the spec) is older than the
        # server that produced it: the server knows a harness this
        # runner's allowlist doesn't. Surface that so the operator
        # checks for a version skew before assuming the spec is wrong.
        #
        # The ``"must be one of"`` prefix is the wording emitted by
        # ``validate_codify_executor`` (same module) for an
        # out-of-allowlist harness. It deliberately does NOT match the
        # sibling "required when executor.type is 'codify' — must be
        # one of ..." message for a *missing* harness, which is a plain
        # authoring mistake, not a version skew. Producer and matcher
        # live in this file, so the coupling stays local; if that
        # message is reworded, update both together.
        if any(
            e.path == "executor.config.harness" and e.message.startswith("must be one of")
            for e in result.errors
        ):
            message += (
                "\n\nNote: if this harness is valid on a newer Codify server, "
                "this client (runner) may be older than the server that produced "
                "the spec — upgrade the runner to pick up newer harnesses."
            )
        raise CodifyError(message, code=ErrorCode.INVALID_INPUT)
    return spec
