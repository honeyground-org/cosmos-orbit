# Contributing

Cosmos Orbit is an early executable pilot. Keep changes small and describe the observable
behavior, verification performed, and remaining limitations.

## Development

Use Python 3.9+ on macOS or Linux. No runtime Python dependencies are required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/cosmos-orbit --help
.venv/bin/cosmos-orbit doctor
```

Use a disposable workspace and a dedicated `--home` for verification. Real CLI calls can consume
subscription usage. Do not run live provider checks as an incidental installation step.

All source comments, documentation, UI strings and examples are written in English. User-provided
requests and private runtime data retain their original language. Do not commit real task stores,
provider transcripts, credentials, personal paths, or screenshots containing private task data.

## Runtime boundaries

- Provider output is a proposal, not execution authority or completion evidence.
- Preserve fixed acceptance checks, explicit write scope and compare-before-write reconciliation.
- Keep state transitions recoverable. Observation must not start models or mutate task results.
- Keep providers and external memory integrations optional and independently usable.
- Report unknown model identity or context measurements honestly; do not infer them from billing totals.

Before proposing a change, run syntax checks and exercise the affected command with an isolated state
store. Document the procedure and result in the pull request. Installation must include the browser
asset and work from outside the source checkout. Do not add personal CLI configuration or auth files.

## Interactive preview

After changing `harness/observer.html` or the command reference, run
`python3 scripts/build_demo.py` and commit `docs/index.html`. Use
`python3 scripts/build_demo.py --check` to detect drift. GitHub Pages publishes `main:/docs`.
The builder reads only the UI template and the static command reference. Never add private
state, native model caches, real tasks, credentials or runtime network requests to the demo.
