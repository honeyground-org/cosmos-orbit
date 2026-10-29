"""Persist explicit user-facing result snapshots shared by terminal and browser."""
from pathlib import Path
from .store import now


def capture(task):
    presentation = task['data'].setdefault('presentation', {})
    relative = presentation.get('result_file')
    if not relative:
        return
    root = Path(task['workspace']).resolve()
    path = root / relative
    try:
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
            raise ValueError('Result must be a regular file within the task workspace')
        path.resolve().relative_to(root)
        with path.open(encoding='utf-8') as stream:
            content = stream.read(16001)
        presentation['report'] = content[:16000]
        presentation['report_source'] = relative + ' · saved ' + now() + (' · excerpt (16000 characters)' if len(content) > 16000 else '')
        presentation.pop('report_error', None)
    except (OSError, ValueError, UnicodeError) as error:
        presentation['report_error'] = str(error)[:500]
        presentation.pop('report', None)
