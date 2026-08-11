"""Background daemon entry point for auto-launched host processes.

Spawned by ``_ensure_host_daemon`` in ``cli.py`` when ``run`` /
``claude`` / ``codex`` register this machine as a host. Runs the same
:class:`HostProcess` loop as ``codify host``.

Two modes:

- ``--server <url>``: connect to an existing (remote or local) Codify server.
- ``--local``: this daemon owns a local Codify server — start (or reuse) a
  persistent background ``codify server`` on loopback and connect to
  it. The CLI discovers the resulting URL via the local-server pidfile.
"""

from __future__ import annotations

import argparse


def main() -> None:
    """Parse args and run the host process.

    Exactly one of ``--server <url>`` or ``--local`` must be given. In
    ``--local`` mode the daemon starts/reuses the background local AP
    server itself and connects to that.

    :returns: None.
    :raises SystemExit: If neither / both of ``--server`` and ``--local``
        are provided.
    """
    # Bundle specs reference the verifier key as ${DEEPSEEK_API_KEY};
    # the daemon parses/materializes agents, so auto-load the stored key.
    from codify.best_of_n import ensure_verifier_key_env

    ensure_verifier_key_env()
    parser = argparse.ArgumentParser(
        description="Background host daemon",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="AP server URL to connect to (remote or local).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Start (or reuse) a local Codify server and connect to it.",
    )
    args = parser.parse_args()

    from codify.process_logging import configure_process_logging

    configure_process_logging("host", force=True)

    if args.local == bool(args.server):
        # Both or neither — the CLI always passes exactly one; fail loud.
        parser.error("exactly one of --server <url> or --local is required")

    if args.local:
        # The daemon owns the local server: start/reuse it, then connect.
        from codify.host.local_server import ensure_local_codify_server

        server_url = ensure_local_codify_server().url
    else:
        server_url = args.server

    from codify.host.connect import run_host_process

    run_host_process(server_url=server_url)


if __name__ == "__main__":
    main()
