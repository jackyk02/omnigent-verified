"""End-to-end: ``codify run`` starts the REPL without leaking
server boot chatter onto the terminal (mock LLM).

Migrated to mock LLM: the test only checks boot output, not LLM
responses, so mock credentials suffice.

**What breaks if this fails:**
- ``_quiet_codify_server_logging`` stops suppressing DBOS /
  alembic / mlflow loggers.
- A new chatty dependency gets pulled in during ``create_app``.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.codify._pexpect_harness import (
    clean_exit,
    spawn_codify_run,
    strip_ansi,
    wait_for_ready,
)
from tests.e2e.codify._repl_test_helpers import drain_for

_YAML_REL = "tests/resources/examples/coding_supervisor.yaml"
_MODEL = "mock-quiet-startup-model"
_HARNESS = "openai-agents"

_FORBIDDEN_BOOT_MARKERS: tuple[str, ...] = (
    "(dbos:",
    "Initializing DBOS",
    "Applying DBOS SQLite",
    "console.dbos.dev",
    "alembic.runtime",
)

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 120.0
_EXIT_TIMEOUT = 15.0
_POST_READY_DRAIN = 1.5


def test_run_codify_startup_does_not_leak_server_logs(
    codify_python: Path,
    codify_repo_root: Path,
    mock_credentials_env: dict[str, str],
) -> None:
    """
    Boot the REPL and verify none of the server's chatty init
    loggers leak to the terminal before the REPL takes over.
    """
    yaml_path = codify_repo_root / _YAML_REL

    child = spawn_codify_run(
        codify_python=codify_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=codify_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)
        assert child.before is not None, "wait_for_ready populated no pre-match text"
        pre_ready = child.before
        post_ready_tail = drain_for(child, _POST_READY_DRAIN)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    combined_stripped = strip_ansi(pre_ready) + strip_ansi(post_ready_tail)

    leaked = [marker for marker in _FORBIDDEN_BOOT_MARKERS if marker in combined_stripped]
    assert not leaked, (
        f"``codify run`` leaked server boot output onto "
        f"the terminal. Leaked markers: {leaked}. "
        f"Combined stripped output (last 4000 chars):\n"
        f"{combined_stripped[-4000:]}"
    )
