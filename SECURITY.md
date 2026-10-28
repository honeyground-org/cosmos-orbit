# Security and data boundaries

Cosmos Orbit is a local, single-user pilot, not a sandbox against a hostile local account.
The browser service binds to loopback and must not be exposed through a public proxy.

Runtime state includes task text, artifact content, handoff packets and provider logs. It defaults
to `~/.local/share/local-harness` and must remain private. Installation files are separate from state.
Selected CLIs process task context using their existing authentication; Cosmos Orbit does not store
provider credentials in its source repository.

Do not include credentials, task transcripts or private file contents in public issue reports.
Use GitHub private vulnerability reporting for security disclosures:
https://github.com/honeyground-org/cosmos-orbit/security/advisories/new
Do not post exploitable details in public issues.
