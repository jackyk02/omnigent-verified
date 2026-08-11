"""End-to-end test for ``examples/agent_with_os_env.yaml``.

The example wires an ``os_env:`` block onto the agent and exposes
the built-in ``sys_os_read`` / ``sys_os_write`` / ``sys_os_edit`` /
``sys_os_shell`` tools. The YAML now ships with
``sandbox: type: none`` so it runs on macOS too — flip back to
``linux_bwrap`` on Linux to exercise the actual sandbox.

**What breaks if this fails:**
- The spec parser regresses on ``os_env.sandbox`` blocks.
- The ``sys_os_*`` builtin registration breaks for YAML-declared
  agents with an ``os_env:`` field.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.codify._example_helpers import (
    assert_completed_one_shot,
    run_one_shot,
)
from tests.e2e.codify.conftest import configure_mock_llm


def test_agent_with_os_env_one_shot(
    codify_python: Path,
    codify_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``codify run agent_with_os_env -p <prompt>`` completes
    cleanly and streams a reply.

    Uses the mock LLM server for deterministic responses.

    :param codify_python: Interpreter with codify +
        openai-agents installed.
    :param codify_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}])
    result = run_one_shot(
        codify_python=codify_python,
        codify_repo_root=codify_repo_root,
        codify_credentials_env=mock_credentials_env,
        example_name="agent_with_os_env",
        model="mock-model",
    )
    assert_completed_one_shot(result, "agent_with_os_env")
