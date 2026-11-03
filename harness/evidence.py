"""Bounded workspace and native tool observations; never private reasoning."""
import json
from pathlib import Path

def evidence_bundle(task):
    data = task['data']
    root = Path(task['workspace']).resolve()
    files, left = [], 48000
    for name in data['development'].get('changed_paths', [])[:20]:
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
    return {'checks': data.get('evidence', []), 'files': files,
            'workspace_changes': {'source': 'Orbit Git comparison against original base',
                                  'base': data['development']['base'],
                                  'changed_paths': data['development'].get('changed_paths', []),
                                  'untracked_paths': data['development'].get('untracked_paths', []),
                                  'verified_tree': data['development'].get('verified_tree'),
                                  'limits': 'Excludes ignored files and external paths; not a full access audit.'},
            'native_tool_trace': native_tool_trace(task),
            'file_limits': 'At most 20 files, 8000 characters per file and 48000 total. Omitted content is not verified.',
            'executor_evidence': {key: [{k: v for k, v in row.items() if k != 'output_tail'} | {'output_tail': str(row.get('output_tail') or '')[-1200:]}
                                        for row in rows[-5:]] for key, rows in (data.get('done_evidence') or {}).items()},
            'executor_evidence_note': 'Records the executor made with cosmos-orbit evidence; claims to check, not proof.',
            'report': data.get('presentation', {}).get('report'),
            'executor_report': data.get('last_summary'),
            'executor_progress': data.get('objective_progress'),
            'executor_blocker': data.get('reason')}



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
