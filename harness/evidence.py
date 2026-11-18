"""Bounded workspace and native tool observations; never private reasoning."""
import collections
import json
from pathlib import Path

# The changed-path lists went into the review prompt whole. An attached task shares its folder: observed 2026-11-18,
# a task's record listed 28,657 changed paths (25,823 of them another project's untracked build output, 30 its own),
# 6.3 million characters of a 6.45 million character prompt, and all seven reviews of that task ended in 2 seconds
# with "Prompt is too long" (about 1.1 million tokens) — the work was never reviewed, and the user was asked a
# question nobody could answer. Each list is now held to PATH_ROWS rows and PATH_CHARS characters, what the task
# itself most plainly changed first, and what was left out is counted by top-level folder so the reviewer knows.
PATH_ROWS, PATH_CHARS = 300, 30000


def _named_in_evidence(data, root):
    """Files in the folder that the executor's evidence names: fingerprinted files and command arguments."""
    named = set()
    for rows in (data.get('done_evidence') or {}).values():
        for row in rows:
            bases = [Path(row['cwd'])] if row.get('cwd') else []
            for token in [row.get('path')] + list(row.get('argv') or []):
                if not isinstance(token, str) or not token or len(token) > 1000:
                    continue
                for base in bases + [root]:
                    try:
                        path = (base / token).resolve()
                        if path.is_file():                    # '-m' or 'pytest' is not a file in the folder
                            named.add(path.relative_to(root).as_posix())
                    except (OSError, ValueError):
                        continue
    return named


def bounded_paths(paths, rank):
    """(shown, omitted). A list within the budget is returned as recorded with omitted None; a longer one keeps the
    lowest-ranked paths that fit and reports how many were left out, by top-level folder."""
    paths = list(paths)
    if len(paths) <= PATH_ROWS and sum(len(p) for p in paths) <= PATH_CHARS:
        return paths, None
    ordered = sorted(paths, key=lambda p: (rank(p), p))
    shown, used = [], 0
    for path in ordered:
        if len(shown) >= PATH_ROWS or used + len(path) > PATH_CHARS:
            break
        shown.append(path)
        used += len(path)
    rest = ordered[len(shown):]
    folders = collections.Counter(p.split('/', 1)[0][:120] if '/' in p else '(top level)' for p in rest)
    top = folders.most_common(15)
    by_folder = dict(top)
    if len(folders) > len(top):
        by_folder['(other folders)'] = len(rest) - sum(count for _, count in top)
    return shown, {'omitted': len(rest), 'of': len(paths), 'by_top_level_folder': by_folder}


def workspace_paths(data, root):
    """The changed and untracked path lists as the reviewer gets them: (changed, untracked, omitted or None).
    First what differs in tracked files, then untracked files the evidence names, then their neighbours, then the rest."""
    dev = data['development']
    changed, untracked = dev.get('changed_paths', []), dev.get('untracked_paths', [])
    loose, named, beside = set(untracked), None, None

    def rank(path):
        nonlocal named, beside
        if path not in loose:
            return 0
        if named is None:                                     # only a list over the budget is ever ranked
            named = _named_in_evidence(data, root)
            beside = {name.rsplit('/', 1)[0] for name in named if '/' in name}
        if path in named:
            return 1
        return 2 if '/' in path and path.rsplit('/', 1)[0] in beside else 3

    shown, cut = bounded_paths(changed, rank)
    shown_untracked, cut_untracked = bounded_paths(untracked, rank)
    omitted = {key: value for key, value in (('changed_paths', cut), ('untracked_paths', cut_untracked)) if value}
    return shown, shown_untracked, omitted or None


def evidence_bundle(task):
    data = task['data']
    root = Path(task['workspace']).resolve()
    changed, untracked, omitted = workspace_paths(data, root)
    files, left = [], 48000
    for name in changed[:20]:
        path = root / name
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            with path.open(encoding='utf-8') as stream:
                content = stream.read(min(8000, left) + 1)
            limit = min(8000, left)
            files.append({'path': name, 'text': content[:limit], 'truncated': len(content) > limit})
            left -= min(len(content), limit)
            if left <= 0:
                break
        except (OSError, UnicodeError, ValueError):
            files.append({'path': name, 'unavailable': True})
    changes = {'source': 'Orbit Git comparison against original base',
               'base': data['development']['base'],
               'changed_paths': changed,
               'untracked_paths': untracked,
               'verified_tree': data['development'].get('verified_tree'),
               'limits': 'Excludes ignored files and external paths; not a full access audit.'}
    if omitted:
        changes['omitted'] = omitted
        changes['limits'] += (' The path lists are bounded (%d rows, %d characters each): changes to tracked files come '
                              'first, then untracked files the executor\'s evidence names, then files beside those, then '
                              'the rest in name order. omitted counts what was left out by top-level folder; a path left '
                              'out is not shown to be unchanged — list the folder yourself when an item depends on it.'
                              % (PATH_ROWS, PATH_CHARS))
    return {'checks': data.get('evidence', []), 'files': files,
            'workspace_changes': changes,
            'native_tool_trace': native_tool_trace(task),
            'file_limits': 'At most 20 files, 8000 characters per file and 48000 total. Omitted content is not verified.',
            'host_observations': host_observations(data),
            'host_observations_note': 'Commands Orbit itself ran on the host (runner "orbit") when the executor recorded '
                                      'evidence: argv, exit code, time and output tail as observed. The executor chose the '
                                      'argv; a note recorded with the command is still a claim.',
            'executor_evidence': {key: [_claim(row) for row in rows[-5:]] for key, rows in (data.get('done_evidence') or {}).items()},
            'executor_evidence_note': 'Notes and file fingerprints the executor recorded with cosmos-orbit evidence; claims to check, not proof.',
            'report': data.get('presentation', {}).get('report'),
            'executor_report': data.get('last_summary'),
            'executor_progress': data.get('objective_progress'),
            'executor_blocker': data.get('reason')}



def host_observations(data):
    """Evidence commands Orbit ran itself, per item (the latest five). Rows from before runners were recorded have
    no runner and stay claims."""
    rows = {}
    for key, entries in (data.get('done_evidence') or {}).items():
        ran = [{'at': e.get('at'), 'argv': e.get('argv'), 'exit_code': e.get('exit_code'), 'seconds': e.get('seconds'),
                'output_tail': str(e.get('output_tail') or '')[-1200:], 'output_truncated': bool(e.get('output_truncated'))
                or len(str(e.get('output_tail') or '')) > 1200}
               for e in entries if e.get('kind') == 'command' and e.get('runner') == 'orbit']
        if ran:
            rows[key] = ran[-5:]
    return rows


def _claim(row):
    """An evidence row as a claim: an Orbit-run command's output is reported under host_observations instead."""
    if row.get('runner') == 'orbit':
        return {k: v for k, v in row.items() if k not in ('output_tail',)} | {'output': 'see host_observations'}
    return {k: v for k, v in row.items() if k != 'output_tail'} | {'output_tail': str(row.get('output_tail') or '')[-1200:]}


def native_tool_trace(task, alias=None):
    """Bounded public tool evidence from the latest owned native call, excluding reasoning."""
    metadata = task['data'].get('session_metadata', {}).get(alias or task['provider'], {})
    path = metadata.get('log')
    records, scanned, left, tool_names = [], 0, 24000, {}
    if not path:
        return {'records': [], 'limits': 'No native log available.'}
    try:
        with Path(path).open(encoding='utf-8') as stream:
            while scanned < 4000000 and left > 0 and len(records) < 40:
                line = stream.readline(1000001)
                if not line:
                    break
                scanned += len(line)
                if len(line) > 1000000:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                item = event.get('item') or {}
                candidates = []
                if event.get('type') == 'item.completed' and isinstance(item, dict):
                    if item.get('type') in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
                        candidates.append({k: item[k] for k in ('id', 'type', 'command', 'aggregated_output', 'exit_code', 'status', 'changes') if k in item})
                message = event.get('message') or {}
                blocks = message.get('content', []) if isinstance(message, dict) else []
                if event.get('type') in ('assistant', 'user') and isinstance(blocks, list):
                    for block in blocks:
                        if isinstance(block, dict) and block.get('type') in ('tool_use', 'tool_result'):
                            candidates.append({k: block[k] for k in ('type', 'id', 'name', 'input', 'tool_use_id', 'is_error', 'content') if k in block})
                for candidate in candidates:
                    if candidate.get('type') == 'tool_use':
                        tool_names[candidate.get('id')] = candidate.get('name')
                    elif candidate.get('type') == 'tool_result':
                        candidate['name'] = tool_names.get(candidate.get('tool_use_id'))
                    text = json.dumps(candidate, ensure_ascii=False)
                    limit = min(left, 2400)
                    records.append({'record': text[:limit], 'truncated': len(text) > limit,
                                    'tool_type': candidate.get('type'), 'tool_id': candidate.get('id', candidate.get('tool_use_id')),
                                    'name': candidate.get('name'), 'exit_code': candidate.get('exit_code'),
                                    'is_error': candidate.get('is_error')})
                    left -= min(len(text), limit)
                    if left <= 0 or len(records) >= 40:
                        break
    except (OSError, UnicodeError):
        return {'records': records, 'limits': 'Native log unavailable or unreadable; partial trace only.'}
    return {'records': records, 'limits': 'Latest native call only; first 4M characters, 40 records, 2400 characters per record, 24000 total. Not a complete absence-of-action audit.'}
