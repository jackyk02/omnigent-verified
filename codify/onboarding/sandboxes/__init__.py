"""Sandbox launchers: run Codify hosts in remote sandboxes.

Public API for the ``codify sandbox`` CLI and anything else that
bootstraps a sandbox-backed host. Core Codify contributes built-in
providers directly; third-party packages can contribute providers through
the ``codify.sandbox_providers`` entry point group.
"""

from __future__ import annotations

import importlib
import importlib.util
import warnings

import click

from codify.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxCapabilityError,
    SandboxLauncher,
)
from codify.onboarding.sandboxes.bootstrap import (
    DEFAULT_SANDBOX_NAME,
    DerivedWorkspace,
    bootstrap_sandbox_host,
    build_wheels,
    connect_sandbox_host,
    derive_workspace,
    login_app_oauth_in_sandbox,
    set_sandbox_host_name,
    ship_wheels,
)
from codify.onboarding.sandboxes.registry import (
    COMMUNITY_MODULE_PREFIX,
    SandboxProviderContribution,
    SandboxProviderMetadata,
    SandboxProviderPluginState,
    SandboxRegistryError,
    available_providers,
    get_provider_metadata,
    instantiate,
    plugin_state,
    reset_plugin_state_for_tests,
)
from codify.onboarding.sandboxes.types import (
    HostContext,
    SandboxCapabilities,
    SandboxCommandError,
    SandboxConfigError,
    SandboxError,
    SandboxInfo,
    SandboxSpec,
)

__all__ = [
    "COMMUNITY_MODULE_PREFIX",
    "DEFAULT_SANDBOX_NAME",
    "DerivedWorkspace",
    "HostContext",
    "RemoteCommandResult",
    "RemoteProcess",
    "SandboxCapabilities",
    "SandboxCapabilityError",
    "SandboxCommandError",
    "SandboxConfigError",
    "SandboxError",
    "SandboxInfo",
    "SandboxLauncher",
    "SandboxProviderContribution",
    "SandboxProviderMetadata",
    "SandboxProviderPluginState",
    "SandboxRegistryError",
    "SandboxSpec",
    "available_providers",
    "bootstrap_sandbox_host",
    "build_wheels",
    "connect_sandbox_host",
    "derive_workspace",
    "get_launcher",
    "get_provider_metadata",
    "login_app_oauth_in_sandbox",
    "plugin_state",
    "reset_plugin_state_for_tests",
    "set_sandbox_host_name",
    "ship_wheels",
]

# Legacy registration surface. It is no longer used by the registry, but is kept
# as a fallback path so any provider not yet loaded through the entrypoint
# mechanism still resolves for one release cycle.
_LAUNCHERS: dict[str, str] = {
    "lakebox": "codify.onboarding.sandboxes.lakebox:LakeboxLauncher",
    "modal": "codify.onboarding.sandboxes.modal:ModalSandboxLauncher",
    "daytona": "codify.onboarding.sandboxes.daytona:DaytonaSandboxLauncher",
    "boxlite": "codify.onboarding.sandboxes.boxlite:BoxliteSandboxLauncher",
    # CoreWeave Sandbox via the official cwsandbox SDK (the
    # `codify[cwsandbox]` extra), imported lazily like modal/daytona.
    "cwsandbox": "codify.onboarding.sandboxes.cwsandbox:CWSandboxLauncher",
    "islo": "codify.onboarding.sandboxes.islo:IsloSandboxLauncher",
    # E2B (https://e2b.dev) via the official `e2b` SDK (the
    # `codify[e2b]` extra), imported lazily like modal/daytona.
    "e2b": "codify.onboarding.sandboxes.e2b:E2BSandboxLauncher",
    "openshell": "codify.onboarding.sandboxes.openshell:OpenShellSandboxLauncher",
    # On-demand Kubernetes runner Pod via the official kubernetes client (the
    # `codify[kubernetes]` extra), imported lazily like modal/daytona.
    "kubernetes": "codify.onboarding.sandboxes.kubernetes:KubernetesSandboxLauncher",
}


def get_launcher(provider: str, *, workspace_host: str | None = None) -> SandboxLauncher:
    """
    Resolve a provider name to a launcher instance.

    :param provider: Provider name, e.g. ``"lakebox"``.
    :param workspace_host: Databricks workspace fronting the target
        server (derived from ``--server`` via
        :func:`~codify.onboarding.sandboxes.bootstrap.derive_workspace`),
        e.g. ``"https://example.databricks.com"``. Consumed only by
        the lakebox launcher, which pins its local ``databricks
        lakebox`` calls to it so sandboxes are created in the server's
        workspace. Other providers' sandboxes don't live in a
        Databricks workspace, so the value is meaningless for them and
        deliberately not forwarded — the CLI derives it from --server
        for every provider, so rejecting it here would break them.
    :returns: A fresh launcher for the provider.
    :raises click.ClickException: If the provider is unknown or its
        launcher module is not present in this build.
    """
    if provider in plugin_state():
        try:
            return instantiate(provider, workspace_host=workspace_host)
        except SandboxRegistryError as exc:
            raise click.ClickException(str(exc)) from exc

    # Legacy fallback for any provider not yet loaded by the registry.
    warnings.warn(
        f"Sandbox provider '{provider}' was resolved through the legacy "
        f"_LAUNCHERS registry. Use the SandboxProviderContribution or "
        f"codify.sandbox_providers entrypoints instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    target = _LAUNCHERS.get(provider)
    if target is None:
        offered = ", ".join(available_providers()) or "(none in this build)"
        raise click.ClickException(
            f"Unknown or unavailable sandbox provider '{provider}'. Available: {offered}."
        )
    module_name = target.partition(":")[0]
    if importlib.util.find_spec(module_name) is None:
        offered = ", ".join(available_providers()) or "(none in this build)"
        raise click.ClickException(
            f"Unknown or unavailable sandbox provider '{provider}'. Available: {offered}."
        )
    if provider == "lakebox" and workspace_host is not None:
        # Imported here (not at module top) because the lakebox module
        # may be absent from a distribution; the availability check above
        # guarantees it exists in this one.
        import codify.onboarding.sandboxes.lakebox as lakebox  # type: ignore[import-not-found]

        return lakebox.LakeboxLauncher(workspace_host=workspace_host)
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    launcher_cls: type[SandboxLauncher] = getattr(module, class_name)
    return launcher_cls()
