"""Routes for the Best-of-N verifier configuration and runs.

``GET/PUT /v1/best-of-n/config`` read and write the server-wide
``best_of_n:`` block of the user-level config file — the selection
criteria, the ``llm_verifier.select`` tuning parameters, and the roster
of proposal-generating harnesses.

``POST /v1/best-of-n/runs`` starts a run: every enabled harness solves
the task directly in its own worktree (no orchestrating brain), and
``GET /v1/best-of-n/runs/{id}`` returns the live snapshot — one
streaming transcript per harness — that the web UI's three-pane run
screen polls, plus the verifier scores and merge commit at the end.

Reads require authentication in multi-user mode; config writes
additionally require admin privileges (the config is server-wide, like
default policies).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from codify.best_of_n import (
    DEFAULT_CRITERIA,
    KNOWN_PROPOSER_HARNESSES,
    BestOfNConfig,
    BestOfNError,
    harness_availability,
    load_best_of_n_config,
    save_best_of_n_config,
)
from codify.best_of_n_runs import get_run_manager
from codify.errors import CodifyError, ErrorCode
from codify.server.auth import AuthProvider
from codify.server.routes._auth_helpers import get_user_id
from codify.stores.permission_store import PermissionStore


def _config_response() -> dict[str, Any]:
    """The full GET payload: config + roster metadata the UI renders."""
    import os

    cfg = load_best_of_n_config()
    key_present = bool(os.environ.get(cfg.verifier.api_key_env) or cfg.verifier.api_key)
    return {
        "object": "best_of_n_config",
        "config": cfg.to_dict(),
        "verifier_key_present": key_present,
        "known_harnesses": list(KNOWN_PROPOSER_HARNESSES),
        "harness_availability": harness_availability(),
        "default_criteria": dict(DEFAULT_CRITERIA),
    }


def create_best_of_n_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the Best-of-N config router (mounted under ``/v1``)."""
    router = APIRouter()

    def _require_user(request: Request) -> str | None:
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise CodifyError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        return user_id

    async def _require_admin(request: Request) -> str | None:
        user_id = _require_user(request)
        if permission_store is None:
            return user_id
        assert user_id is not None
        is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
        if not is_admin:
            raise CodifyError(
                "Admin privileges required to change the Best-of-N configuration",
                code=ErrorCode.FORBIDDEN,
            )
        return user_id

    @router.get("/best-of-n/config")
    async def get_config(request: Request) -> dict[str, Any]:
        """Return the effective Best-of-N config plus roster metadata."""
        _require_user(request)
        return _config_response()

    @router.put("/best-of-n/config")
    async def put_config(request: Request) -> dict[str, Any]:
        """Replace the ``best_of_n:`` block of the user-level config.

        The body is the ``config`` object shape returned by GET; unknown
        keys are ignored and missing keys reset to defaults. At least one
        criterion and one proposer must remain, so a run is always
        possible.
        """
        await _require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise CodifyError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        raw = body.get("config", body)
        # Validate the raw payload: from_dict is tolerant (an empty
        # criteria mapping silently resets to defaults), but an explicit
        # empty submission from the UI should fail loud instead.
        if isinstance(raw, dict) and "criteria" in raw and not raw["criteria"]:
            raise CodifyError(
                "at least one selection criterion is required", code=ErrorCode.INVALID_INPUT
            )
        config = BestOfNConfig.from_dict(raw)
        if not any(p.enabled for p in config.proposers):
            raise CodifyError(
                "at least one proposer harness must be enabled", code=ErrorCode.INVALID_INPUT
            )
        await asyncio.to_thread(save_best_of_n_config, config)
        return _config_response()

    @router.post("/best-of-n/runs")
    async def start_run(request: Request) -> dict[str, Any]:
        """Start a Best-of-N run: fan the task out to every enabled harness.

        Body: ``{"prompt": str, "repo_path": str}``. Returns the initial
        run snapshot; poll ``GET /best-of-n/runs/{id}`` for live state.
        """
        _require_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise CodifyError("body must be a JSON object", code=ErrorCode.INVALID_INPUT)
        prompt = body.get("prompt")
        repo_path = body.get("repo_path")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodifyError("prompt is required", code=ErrorCode.INVALID_INPUT)
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise CodifyError("repo_path is required", code=ErrorCode.INVALID_INPUT)
        try:
            return await asyncio.to_thread(
                get_run_manager().start_run, prompt.strip(), repo_path.strip()
            )
        except BestOfNError as exc:
            raise CodifyError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

    @router.get("/best-of-n/runs")
    async def list_runs(request: Request) -> dict[str, Any]:
        """List known runs, newest first (transcripts/diffs omitted)."""
        _require_user(request)
        return {"object": "list", "data": get_run_manager().list()}

    @router.get("/best-of-n/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> dict[str, Any]:
        """Live snapshot of one run: per-harness transcripts, scores, result."""
        _require_user(request)
        snapshot = get_run_manager().get(run_id)
        if snapshot is None:
            raise CodifyError("run not found", code=ErrorCode.NOT_FOUND)
        return snapshot

    return router
