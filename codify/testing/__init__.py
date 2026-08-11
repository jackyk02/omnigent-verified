"""Test-environment safety helpers for the Codify suite.

Houses additive guardrails that assert a test run is pointed at
throwaway resources (a tmp/in-memory SQLite DB, no dev/prod ports)
rather than a developer's real local instance. See
:mod:`codify.testing.guardrails`.
"""

from __future__ import annotations
