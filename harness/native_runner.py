"""Keep task leases and write a native exit receipt even when the UI/worker owner dies."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness.recovery import atomic_json, process_identity


def main():
    path = Path(sys.argv[1])
    record = json.loads(path.read_text())
    started = time.monotonic()
    record.update(status='running', process=process_identity(os.getpid()))
    atomic_json(path, record)
    try:
        # This supervisor keeps the inherited leases. Native tools need not preserve them.
        process = subprocess.Popen(sys.argv[2:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        code = process.wait()
    except Exception as error:
        record['launch_error'] = str(error)
        code = 127
    if record.get('provider') == 'check':
        from harness.development import fingerprint
        record['tree_after'] = fingerprint(Path.cwd())
    record.update(status='returned', exit_code=code, seconds=round(time.monotonic() - started, 3))
    atomic_json(path, record)
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
