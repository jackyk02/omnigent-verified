"""Built-in policy functions shipped with Codify.

Each submodule exports a ``POLICY_REGISTRY`` list — a catalog of
policy callables with their handler paths, descriptions, and
parameter schemas. The server discovers these at startup and
exposes them via ``GET /v1/policy-registry`` so users can browse
available policies and attach them to sessions.

The ``POLICY_REGISTRY`` convention::

    POLICY_REGISTRY = [
        {
            "handler": "codify.policies.builtins.safety.max_tool_calls_per_session",
            "kind": "factory",  # called with factory_params to produce evaluator
            "description": "Limits tool calls per session",
            "params_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max calls allowed per turn",
                        "default": 10,
                    }
                },
                "required": ["limit"],
            },
        },
    ]

Modules to scan are listed in :data:`BUILTIN_POLICY_MODULES`.
"""

from __future__ import annotations

# Modules scanned at startup for POLICY_REGISTRY entries.
# Add new builtin modules here.
BUILTIN_POLICY_MODULES = [
    "codify.policies.builtins.safety",
    "codify.policies.builtins.cost",
    "codify.policies.builtins.google",
    "codify.policies.builtins.github",
    "codify.policies.builtins.working_dir",
    "codify.policies.builtins.risk_score",
    "codify.policies.builtins.routing",
    "codify.policies.builtins.cel",
    "codify.policies.builtins.prompt",
    "codify.policies.builtins.context",
    "codify.policies.builtins.orchestration",
    # Legacy alias module — registers old codify.inner.nessie.policies.*
    # handler paths so deployed bundles that pre-date the rename still work.
    "codify.inner.nessie.policies",
]
