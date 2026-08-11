"""Backward-compat shim — policy handler paths in deployed configs still reference
``codify.inner.nessie.policies.*``.  Real implementation lives at
``codify.policies.builtins.orchestration``.
"""

from codify.policies.builtins.orchestration import *  # noqa: F403
from codify.policies.builtins.orchestration import POLICY_REGISTRY as _new_registry

# Re-advertise under the legacy handler paths so the policy registry accepts
# bundles that were deployed before the module was renamed.
_OLD = "codify.inner.nessie.policies."
_NEW = "codify.policies.builtins.orchestration."


def _legacy_entry(entry: dict[str, object]) -> dict[str, object]:
    handler = entry.get("handler")
    if not isinstance(handler, str):
        raise TypeError("policy registry handler must be a string")
    return {**entry, "handler": handler.replace(_NEW, _OLD), "internal_only": True}


POLICY_REGISTRY = [_legacy_entry(entry) for entry in _new_registry]
