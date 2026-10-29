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


Opt-in repository development uses private Git clones with no origin remote. Codex native tools use
its workspace-write sandbox; Claude native file tools use restricted mode. Orbit does not bypass
provider permissions. This is not equivalent to a container or a hostile-code sandbox. Explicit
`--public-web` (or the matching new-task checkbox) permits public research: Codex receives live
search and sandbox network access; Claude receives WebSearch/WebFetch. Network access is broader
than a URL allowlist. It does not authorise authenticated account access, orders or publication.
Without this option, native Codex network access remains disabled. Explicit
`--check` commands execute locally with the caller's environment and permissions, and may themselves
run repository code. They are user-owned capabilities, not model-generated commands. Trust the
repository and commands before opting in. Do not use production credentials in development checks.
Native edits remain in the clone for review and do not use the artifact-mode write journal. Private
patches, command output and provider logs may contain source or sensitive data; do not publish them.
