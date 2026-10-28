# Security and data boundaries

Local Harness is a local, single-user pilot, not a sandbox against a hostile local account.
The browser service binds to loopback and must not be exposed through a public proxy.

Runtime state includes task text, artifact content, handoff packets and provider logs. It defaults
to `~/.local/share/local-harness` and must remain private. Installation files are separate from state.
Selected CLIs process task context using their existing authentication; Local Harness does not store
provider credentials in its source repository.

Do not include credentials, task transcripts or private file contents in public issue reports.
A public vulnerability-reporting contact will be documented when the public repository is established.
