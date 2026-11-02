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


Native execution uses a private clone with no origin remote. Original files, ignored data and
credentials are not copied by Orbit. The native executor retains its installed CLI configuration,
skills, hooks and optional MCP servers; those remain user-controlled. Shell sandboxing does not
sandbox custom hooks or external MCP services. Keep those configurations and repositories trusted.

The completion reviewer uses a fresh context with read-only workspace tools and a sandboxed shell.
Codex uses its read-only sandbox. Claude enables its Bash sandbox, requires it to be available,
forbids unsandboxed retry and denies writes to the workspace and Orbit state. The host independently
rejects changed workspace evidence after review. Native CLI and managed policy behaviour remain
part of the trust boundary; this is not protection against a hostile installed CLI or local account.

Explicit --check commands execute with the caller's permissions and environment. The model cannot
change their argument lists. They are not sandboxed by Orbit and must be safe to repeat after an
interruption. --public-web permits public research, not account access, external writes or trading.

Call receipts and process identities are saved before native launch. A supervisor holds task and
workspace leases while the native process runs. Recovery checks owned processes and saved terminal
responses before continuing; it never treats file changes alone as a successful model response.
Explicit user pauses survive restarts. Temporary provider failures use bounded retries; silence
alone never kills a process or authorises replay. Arbitrarily detached jobs and externally visible
side effects are outside an exactly-once guarantee.

Historical proposals and events remain private and readable. Removed artifact execution paths are
not replayed with new native permissions. Retained patches, tool traces, command logs, receipts and
worker output can contain private source and user requests; do not publish them.
