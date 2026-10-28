"""Bounded text artifacts, compare-before-write and deterministic evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MAX_FILE = 100_000
MAX_BATCH = 300_000
EXCLUDED = {'.git', '.harness', '.claude', '.codex', '.agents', 'node_modules',
            '.venv', 'venv', '__pycache__', '.ssh', '.aws'}
PROTECTED = {'AGENTS.md', 'CLAUDE.md', '.mcp.json', '.gitignore'}


def within(target, ancestor):
    """Also recognise case aliases on case-insensitive filesystems."""
    target, ancestor = Path(target), Path(ancestor)
    if target == ancestor or ancestor in target.parents:
        return True
    for path in (target,) + tuple(target.parents):
        if path.exists() and ancestor.exists() and os.path.samefile(path, ancestor):
            return True
    return False


def digest(content):
    return hashlib.sha256(content).hexdigest()


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key: ' + key)
            value[key] = item
        return value
    def constant(value):
        raise ValueError('Non-JSON numeric constant: ' + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def path_for(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('A relative file path is required')
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('Path escapes workspace')
    if any(p.casefold() in {x.casefold() for x in EXCLUDED} or p.startswith('.') for p in parts) or parts[-1].casefold() in {x.casefold() for x in PROTECTED}:
        raise ValueError('Protected path: ' + relative)
    if parts[-1].lower().endswith(('.pem', '.key', '.p12', '.sqlite', '.sqlite3', '.db')):
        raise ValueError('Private or binary path: ' + relative)
    root = Path(root).resolve()
    target = root.joinpath(*parts)
    runtime = Path(__file__).resolve().parents[1]
    if within(target, runtime):
        raise ValueError('The running harness checkout cannot modify itself')
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink paths are not supported: ' + relative)
    if root not in target.resolve().parents:
        raise ValueError('Path escapes workspace')
    if target.exists() and not target.is_file():
        raise ValueError('Not a regular file: ' + relative)
    if target.exists() and target.stat().st_nlink > 1:
        raise ValueError('Hard-linked files are not supported')
    return target


def read_file(root, relative):
    path = path_for(root, relative)
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE:
        raise ValueError('File exceeds the pilot size limit: ' + relative)
    raw = path.read_bytes()
    if b'\0' in raw:
        raise ValueError('Binary file: ' + relative)
    raw.decode('utf-8')
    return raw


def snapshot(root):
    """Limit initial context; never crawl excluded directories or follow links."""
    files, skipped, total, visited = [], 0, 0, 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.')
                         and not (Path(parent) / d).is_symlink())
        for name in sorted(names):
            visited += 1
            if visited > 500:
                return {'files': files, 'truncated': True, 'skipped': skipped}
            relative = str((Path(parent) / name).relative_to(root))
            try:
                raw = read_file(root, relative)
            except (ValueError, UnicodeError, OSError):
                skipped += 1
                continue
            if total + len(raw) > 48_000 or len(files) >= 30:
                skipped += 1
                continue
            files.append({'path': relative, 'sha': digest(raw), 'content': raw.decode('utf-8')})
            total += len(raw)
    return {'files': files, 'truncated': skipped > 0, 'skipped': skipped}


def validate_contract(contract, root):
    if not isinstance(contract, dict):
        raise ValueError('Contract must be an object')
    if set(contract) != {'goal', 'permission_basis', 'local_write', 'constraints', 'questions', 'steps', 'files', 'checks'}:
        raise ValueError('Unknown or missing contract fields')
    for key in ('goal', 'permission_basis'):
        if not isinstance(contract.get(key), str) or len(contract[key]) > 8000:
            raise ValueError('Invalid contract text: ' + key)
    if not contract['goal'].strip():
        raise ValueError('Goal is empty')
    if type(contract.get('local_write')) is not bool:
        raise ValueError('local_write must be boolean')
    for key in ('constraints', 'questions', 'steps'):
        value = contract.get(key)
        if not isinstance(value, list) or len(value) > 30 or any(not isinstance(v, str) for v in value):
            raise ValueError('Invalid contract list: ' + key)
    paths = contract.get('files')
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths) or len(paths) > 15 or len(set(paths)) != len(paths):
        raise ValueError('Specify 1-15 unique files')
    for path in paths:
        path_for(root, path)
    checks = contract.get('checks')
    if not isinstance(checks, list) or len(checks) > 40:
        raise ValueError('Invalid checks list')
    if contract['questions'] or not contract['local_write']:
        return contract
    if not paths or not checks:
        raise ValueError('At least one concrete check is required')
    covered = set()
    for check in checks:
        if not isinstance(check, dict) or check.get('path') not in paths:
            raise ValueError('Check references an undeclared file')
        if set(check) != {'path', 'kind', 'expected', 'pointer', 'reason'}:
            raise ValueError('Unknown or missing check fields')
        if check.get('kind') not in ('contains', 'not_contains', 'equals', 'json_equals'):
            raise ValueError('Unsupported deterministic check')
        if not isinstance(check.get('expected'), str) or not check['expected']:
            raise ValueError('Check needs a nonempty expected value')
        if not isinstance(check.get('pointer'), str) or not isinstance(check.get('reason'), str):
            raise ValueError('Check needs pointer and reason fields')
        if check['kind'] != 'not_contains':
            covered.add(check['path'])
    if covered != set(paths):
        raise ValueError('Each file requires a positive acceptance check')
    return contract


def prepare(root, changes, contract):
    if not isinstance(changes, list) or not 1 <= len(changes) <= 15:
        raise ValueError('Expected 1-15 file changes')
    batch, seen, size = [], set(), 0
    for item in changes:
        relative, content = item.get('path'), item.get('content')
        if relative not in contract['files'] or relative in seen or not isinstance(content, str):
            raise ValueError('Undeclared, duplicate or invalid file edit')
        seen.add(relative)
        before = read_file(root, relative)
        if item.get('before_sha') != (digest(before) if before is not None else ''):
            raise ValueError('File changed since the model read it: ' + relative)
        encoded = content.encode('utf-8')
        size += len(encoded)
        if len(encoded) > MAX_FILE or size > MAX_BATCH or '\0' in content:
            raise ValueError('Artifact exceeds supported size or contains binary data')
        batch.append({'path': relative, 'before_sha': item['before_sha'],
                      'after_sha': digest(encoded), 'content': content,
                      'before': before.decode('utf-8') if before is not None else None})
    return batch


def apply(root, batch):
    # Preflight all files before any write. On restart an already-applied hash is a no-op.
    for edit in batch:
        raw = read_file(root, edit['path'])
        current = digest(raw) if raw is not None else ''
        if current not in (edit['before_sha'], edit['after_sha']):
            raise ValueError('External edit conflicts with pending action: ' + edit['path'])
    for edit in batch:
        raw = read_file(root, edit['path'])
        if raw is not None and digest(raw) == edit['after_sha']:
            continue
        target = path_for(root, edit['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.harness-', dir=str(target.parent))
        try:
            if target.exists():
                os.fchmod(fd, target.stat().st_mode & 0o777)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(edit['content'])
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def verify(root, contract):
    results = []
    for check in contract['checks']:
        try:
            raw = read_file(root, check['path'])
            text = raw.decode('utf-8') if raw is not None else None
            kind, expected = check['kind'], check['expected']
            passed = False
            if text is not None:
                if kind == 'contains':
                    passed = expected in text
                elif kind == 'not_contains':
                    passed = expected not in text
                elif kind == 'equals':
                    passed = expected == text
                elif kind == 'json_equals':
                    value = strict_json(text)
                    pointer = check['pointer']
                    if pointer and not pointer.startswith('/'):
                        raise ValueError('Invalid JSON pointer')
                    for part in pointer.split('/')[1:] if pointer else []:
                        if re.search(r'~(?![01])', part):
                            raise ValueError('Invalid JSON pointer escape')
                        part = part.replace('~1', '/').replace('~0', '~')
                        if isinstance(value, list) and (not part.isascii() or not part.isdigit() or (len(part) > 1 and part.startswith('0'))):
                            raise ValueError('Invalid JSON array index')
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    passed = json.dumps(value, sort_keys=True, ensure_ascii=False) == json.dumps(
                        strict_json(expected), sort_keys=True, ensure_ascii=False)
            results.append(dict(check, passed=passed, sha=digest(raw) if raw is not None else None))
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            results.append(dict(check, passed=False, error=str(error)))
    return results
