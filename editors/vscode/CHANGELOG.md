# Changelog

All notable changes to the Codify VS Code extension are documented here.

## [0.1.0]

Initial release — a minimal, iframe-only client for a locally running Codify
server.

- Open a running local Codify server in an editor-beside panel.
- **Codify: Open** command, available from the editor-title bar and the
command palette, plus an activity-bar view with an "Open Codify" button.
- Automatically discovers a local server via `~/.codify/local_server.pid`, or
point the extension at one with the `codify.serverUrl` setting. Localhost
servers only in this build.

