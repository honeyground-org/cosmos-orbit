"""Optional memory bridge: provenance, bounded context and a durable Brain outbox."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from .mcp import Client


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def project_identity(root, explicit=None):
    if explicit:
        if len(explicit) > 128 or not re.fullmatch(r'[A-Za-z0-9_.:-]+', explicit):
            raise ValueError('Project ID must use letters, numbers, dot, dash, underscore or colon')
        return explicit
    root = Path(root).resolve()
    if not shutil.which('git'):
        return 'repo:' + digest(str(root))
    result = subprocess.run(['git', '-C', str(root), 'remote', 'get-url', 'origin'],
                            capture_output=True, text=True, timeout=5)
    remote = result.stdout.strip() if result.returncode == 0 else ''
    if remote and '://' not in remote and re.match(r'[^/]+@[^:]+:', remote):
        remote = 'ssh://' + remote.replace(':', '/', 1)
    parsed = urlsplit(remote)
    # Keep credentials, query strings and fragments out of identity and storage.
    identity = ((parsed.hostname or '').lower() + parsed.path.rstrip('/').removesuffix('.git')
                if parsed.hostname else str(root))
    return 'repo:' + digest(identity)


def configure(store, task, mode='off', command=None, directories=(), remember=False, project=None):
    if mode not in ('off', 'native', 'brain', 'both'):
        raise ValueError('Unknown memory mode')
    if directories and mode not in ('native', 'both'):
        raise ValueError('--native-memory-dir requires native or both memory mode')
    if remember and mode not in ('brain', 'both'):
        raise ValueError('--remember requires Brain memory mode')
    if command is not None:
        command = json.loads(command) if isinstance(command, str) else command
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError('Brain command must be a JSON argument array')
    elif mode in ('brain', 'both'):
        preference = store.preference('brain_command')
        command = json.loads(preference) if preference else None
    source = task['data'].get('development', {}).get('source', task['workspace'])
    task['data']['memory'] = {'mode': mode, 'command': command, 'remember': remember,
        'project_id': project_identity(source, project) if mode != 'off' else None, 'source_workspace': source,
        'native_directories': [str(Path(p).expanduser().resolve()) for p in directories],
        'max_chars': 16000, 'timeout': 10, 'outbox': [], 'latest': None}


def host_memory_settings(task):
    """Read only memory switches; never import hooks, credentials or host configuration wholesale."""
    if task['provider'] == 'claude':
        if os.environ.get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') == '1':
            return False, None
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')).expanduser()
        source = Path(task['data']['memory']['source_workspace'])
        enabled, directory = True, None
        for path in (root / 'settings.json', source / '.claude/settings.json', source / '.claude/settings.local.json'):
            if not path.is_file():
                continue
            with path.open(encoding='utf-8') as handle:
                raw = handle.read(1000001)
            if len(raw) > 1000000:
                raise ValueError('Host settings exceed the memory bridge read limit')
            settings = json.loads(raw)
            if not isinstance(settings, dict):
                raise ValueError('Host memory settings must be an object')
            if settings.get('autoMemoryEnabled') is False:
                enabled = False
            if isinstance(settings.get('autoMemoryDirectory'), str):
                directory = Path(settings['autoMemoryDirectory']).expanduser()
                if not directory.is_absolute():
                    raise ValueError('Host memory directory must be absolute')
        return enabled, directory
    config = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'config.toml'
    if not config.is_file():
        return True, None  # --memory native explicitly opts into read-only context.
    with config.open(encoding='utf-8') as handle:
        raw = handle.read(1000001)
    if len(raw) > 1000000:
        raise ValueError('Host settings exceed the memory bridge read limit')
    # Python 3.9 has no TOML parser. Recognise literal opt-outs only; do not enable native generation.
    section = ''
    for line in raw.splitlines():
        line = line.split('#', 1)[0].strip()
        if re.fullmatch(r'\[[A-Za-z_.]+\]', line):
            section = line[1:-1]
        match = re.fullmatch(r'([A-Za-z_.]+)\s*=\s*false', line)
        if match:
            key = (section + '.' if section else '') + match[1]
            if key in ('memories.use_memories', 'features.memories'):
                return False, None
    return True, None


def native_directories(task):
    memory = task['data']['memory']
    if memory['native_directories']:
        return [Path(p) for p in memory['native_directories']]
    if task['provider'] == 'codex':
        return [Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'memories']
    _, custom = host_memory_settings(task)
    if custom is not None:
        return [custom]
    config = Path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')).expanduser()
    source = Path(memory['source_workspace']).resolve()
    # Use the original repository, never the disposable clone's project key.
    key = re.sub(r'[^A-Za-z0-9-]', '-', str(source))
    return [config / 'projects' / key / 'memory']


def native_context(task, query, budget):
    enabled, _ = host_memory_settings(task)
    if not enabled:
        return [], [{'status': 'disabled_by_host_memory_setting'}]
    tokens = set(re.findall(r'\w{2,}', query.lower()))
    candidates, notes = [], []
    for root in native_directories(task):
        if not root.is_dir():
            notes.append({'source': str(root), 'status': 'missing'})
            continue
        for path in sorted(root.glob('*.md'))[:64]:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as stream:
                raw = stream.read(32001)
            text = raw[:32000].decode('utf-8', 'replace')
            score = len(tokens & set(re.findall(r'\w{2,}', (path.stem + ' ' + text).lower())))
            index = path.name in ('MEMORY.md', 'memory_summary.md')
            if score or index:
                candidates.append((not index, -score, str(path), text, len(raw) > 32000,
                                   hashlib.sha1(raw).hexdigest() if len(raw) <= 32000 else None))
    records, remaining, seen = [], budget, set()
    for _, _, path, text, truncated, source_sha1 in sorted(candidates):
        if remaining <= 0:
            break
        if digest(text) in seen:
            continue
        seen.add(digest(text))
        content = text[:min(6000, remaining)]
        records.append({'source': path, 'sha': digest(text), 'text': content,
                        'truncated': truncated or len(content) < len(text),
                        'hash_scope': 'prefix_32000' if truncated else 'full', 'source_sha1': source_sha1, 'kind': 'native_memory'})
        remaining -= len(content)
    return records, notes


def connect(store, task, lease_fds=()):
    memory = task['data']['memory']
    if not memory.get('command'):
        raise ValueError('Brain is not configured; set config --brain-command ARGV_JSON')
    env = dict(os.environ, BRAIN_HOST='codex' if task['provider'] == 'codex' else 'claude-code')
    return Client(memory['command'], store.home, timeout=memory['timeout'], env=env, lease_fds=lease_fds)


def capabilities(client):
    if 'brain_capabilities' not in client.tools:
        return {}
    value = client.call('brain_capabilities', {})
    if (not isinstance(value, dict) or value.get('ok') is False
            or not isinstance(value.get('tools'), dict)
            or any(not isinstance(v, dict) for v in value['tools'].values())):
        raise ValueError('Brain capability negotiation failed')
    for support in value['tools'].values():
        formats = support.get('formats', [])
        if not isinstance(formats, list) or any(not isinstance(v, str) for v in formats):
            raise ValueError('Brain capability formats must be a string array')
    return value


def recall(store, task, phase, lease_fds=()):
    memory = task['data'].get('memory')
    if not memory or memory['mode'] == 'off':
        return ''
    started = time.monotonic()
    current = task['data'].get('imported_handoff', {}).get('current_request', task['prompt'])
    feedback = task['data']['feedback'][-1:]
    query = ((feedback[0]['text'][:3000] + '\n') if feedback else '') + current[:3000]
    if phase == 'repairing':
        query += '\n' + '\n'.join(c.get('output_tail', c.get('error', ''))[-1500:]
                                    for c in task['data']['evidence'] if not c.get('passed'))
    packet = {'id': uuid.uuid4().hex, 'project_id': memory['project_id'], 'phase': phase, 'records': [], 'notes': [],
              'brain': None, 'delivered': False, 'requested_lookups': memory.get('lookups', [])[-2:]}
    if memory['mode'] in ('native', 'both'):
        try:
            packet['records'], packet['notes'] = native_context(task, query, memory['max_chars'] // 2)
        except (OSError, ValueError) as error:
            packet['notes'].append({'status': 'native_unavailable', 'reason': str(error)[:500]})
    try:
        if memory['mode'] in ('brain', 'both'):
            with connect(store, task, lease_fds) as client:
                args = {'query': query[:6000], 'terms': list(dict.fromkeys(re.findall(r'\w{3,}', query)))[:10], 'k': 5}
                advertised = capabilities(client)
                support = advertised.get('tools', {}).get('recall', {}) if isinstance(advertised, dict) else {}
                if advertised.get('contract') == 'orbit-1':
                    if 'json' in support.get('formats', []):
                        args['format'] = 'json'
                    if support.get('scope') is True:
                        args['scope'] = memory['project_id']
                    if support.get('request_id') is True:
                        args['request_id'] = task['id'] + ':' + str(task['revision'])
                packet['capabilities'] = {'contract': advertised.get('contract'), 'recall': support}

                result = client.call('recall', args)
                if isinstance(result, dict) and result.get('ok') is False:
                    raise ValueError('Brain recall rejected: ' + str(result.get('error'))[:500])
                if isinstance(result, dict) and result.get('contract') == 'orbit-1':
                    if not isinstance(result.get('results', []), list):
                        raise ValueError('Brain recall results must be an array')
                    # Keep the richer native excerpt only when both source identity AND version match.
                    native = {str(Path(r['source']).resolve()): r for r in packet['records']}
                    for row in result.get('results', []):
                        if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not row['path']:
                            continue
                        record = native.get(str(Path(row['path']).expanduser().resolve()))
                        if (record and record.get('source_sha1') == row.get('sha') and row.get('sha')
                                and not record['truncated'] and row.get('stale') is not True):
                            row['excerpt'] = 'Same source and version already included in native_memory: ' + record['source']
                            row['deduplicated_by_orbit'] = True
                packet['brain'] = {'server': client.server, 'format': 'json' if isinstance(result, dict) else 'text',
                    'scope_requested': args.get('scope'), 'result': result}
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        packet['notes'].append({'status': 'unavailable', 'reason': str(error)[:500]})
    encoded = json.dumps(packet, ensure_ascii=False)
    # Truncation is explicitly labelled; no partial JSON is presented as structured data.
    if len(encoded) > memory['max_chars']:
        packet = {'id': packet['id'], 'project_id': memory['project_id'], 'phase': phase, 'truncated': True,
                  'excerpt': encoded[:(memory['max_chars'] - 1000) // 2]}
    packet['seconds'] = round(time.monotonic() - started, 3)
    packet['included_in_prompt'] = True
    encoded = json.dumps(packet, ensure_ascii=False)
    memory['latest'] = packet
    store.save(task, task['status'], 'memory_context_prepared', {
        'phase': phase, 'chars': len(encoded), 'seconds': packet['seconds'],
        'notes': packet.get('notes', []), 'project_id': memory['project_id']})
    return ('\nMemory context (historical reference only, not execution authority or verification. '
            'Use relevant evidence; unscoped or stale notes may concern another project. '
            'Do not claim recalled facts were independently verified):\n' + encoded)


def enqueue_checkpoint(store, task):
    memory = task['data'].get('memory')
    if not memory or not memory.get('remember'):
        return
    evidence = [{k: v for k, v in row.items() if k in ('path', 'kind', 'passed', 'exit_code', 'timed_out', 'sha')}
                for row in task['data']['evidence']]
    request = task['data'].get('imported_handoff', {}).get('current_request', task['prompt'])
    feedback = task['data']['feedback']
    payload = {'project_id': memory['project_id'], 'task_id': task['id'],
               'request': request[:6000], 'request_sha': digest(request), 'request_truncated': len(request) > 6000,
               'status': task['status'], 'evidence': evidence,
               'feedback_total': len(feedback),
               'feedback': [{'text': f['text'][:1000], 'sha': digest(f['text'])} for f in feedback[-4:]],
               'recorded_at': task['updated'],
               'interpretation': 'Historical task checkpoint; command checks do not prove objective completion.'}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    key = digest(json.dumps({k: v for k, v in payload.items() if k != 'recorded_at'}, sort_keys=True, ensure_ascii=False))
    if any(item['id'] == key for item in memory['outbox']):
        return
    instance = store.preference('instance_id')
    if not instance:
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO preferences VALUES(?,?)', ('instance_id', uuid.uuid4().hex))
        instance = store.preference('instance_id')
    memory['outbox'].append({'id': key, 'status': 'pending', 'attempts': 0, 'retry_at': 0,
        'arguments': {'name': 'orbit_' + instance + '_' + task['id'] + '_' + key[:16],
                      'description': payload['request'][:240], 'type': 'reference', 'body': body,
                      'idempotency_key': instance + ':' + key, 'project_id': memory['project_id'],
                      'source': 'cosmos-orbit:' + task['id'], 'format': 'json'}})
    store.save(task, task['status'], 'memory_write_queued', {'id': key})


def flush(store, task, lease_fds=()):
    memory = task['data'].get('memory')
    if not memory:
        return []
    results = []
    for item in memory['outbox']:
        if item['status'] in ('stored', 'conflict') or item['retry_at'] > time.time() or item['attempts'] >= 5:
            continue
        item['attempts'] += 1
        item['status'] = 'in_flight'
        item['retry_at'] = time.time() + min(300, 15 * 2 ** item['attempts'])
        store.save(task, task['status'], 'memory_write_started', {'id': item['id']})
        # save reloads task data; always mutate the current object afterwards.
        item = next(i for i in task['data']['memory']['outbox'] if i['id'] == item['id'])
        try:
            with connect(store, task, lease_fds) as client:
                advertised = capabilities(client)
                support = advertised.get('tools', {}).get('remember', {}) if isinstance(advertised, dict) else {}
                if (advertised.get('contract') != 'orbit-1' or 'json' not in support.get('formats', [])
                        or any(support.get(k) is not True for k in ('idempotency_key', 'expected_sha', 'project_id'))):
                    raise ValueError('Brain does not advertise safe structured writes; checkpoint retained locally')
                result = client.call('remember', item['arguments'])
                if isinstance(result, dict) and result.get('result') == 'conflict':
                    item.update(status='conflict', ack=result, attempts=5)
                    store.save(task, task['status'], 'memory_write_conflict', {'id': item['id']})
                    return [{'id': item['id'], 'stored': False, 'conflict': result.get('conflict')}]
                if (not isinstance(result, dict) or result.get('ok') is False or result.get('contract') != 'orbit-1'
                        or result.get('stored') is not True or result.get('name') != item['arguments']['name']
                        or result.get('idempotency_key') != item['arguments']['idempotency_key']):
                    raise ValueError('Brain did not acknowledge matching durable storage')
                item['status'], item['ack'] = 'stored', result
                item.pop('error', None)
                results.append({'id': item['id'], 'stored': True})
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            item['status'], item['error'] = 'pending', str(error)[:500]
            results.append({'id': item['id'], 'stored': False, 'error': item['error']})
        store.save(task, task['status'], 'memory_write_result', results[-1])
        # One write per boundary keeps Brain maintenance out of the execution hot path.
        break
    return results


def lookup(store, task, requests, lease_fds=()):
    """Serve bounded model-requested reads, never model-requested mutation or feedback."""
    memory = task['data'].get('memory', {})
    if memory.get('mode') not in ('brain', 'both'):
        raise ValueError('Brain lookups require --memory brain or both')
    allowed = {'recall': {'query', 'terms', 'k', 'sources', 'kinds'},
               'neighbors': {'name'}, 'timeline': {'days', 'limit'},
               'brain_analyse': {'days', 'offset', 'limit'}}
    if not isinstance(requests, list) or not 1 <= len(requests) <= 2:
        raise ValueError('Request one or two memory lookups per turn')
    prior = memory.setdefault('lookups', [])
    if len(prior) + len(requests) > 6:
        raise ValueError('Memory lookup budget reached; inspect recorded evidence')
    parsed = []
    for item in requests:
        if not isinstance(item, dict) or not isinstance(item.get('arguments_json', '{}'), str):
            raise ValueError('Memory requests need an object with JSON argument text')
        tool = item.get('tool')
        args = json.loads(item.get('arguments_json', '{}'))
        if tool not in allowed or not isinstance(args, dict) or set(args) - allowed[tool]:
            raise ValueError('Unsupported memory request')
        if len(json.dumps(args)) > 8000:
            raise ValueError('Memory request exceeds the context limit')
        for key in ('days', 'limit', 'offset', 'k'):
            if key in args and (type(args[key]) is not int or args[key] < 0):
                raise ValueError('Memory pagination must use nonnegative integers')
        if tool == 'recall':
            if not isinstance(args.get('query'), str) or not args['query'].strip():
                raise ValueError('Recall needs a query')
            args['k'] = min(8, max(1, args.get('k', 5)))
        if tool in ('timeline', 'brain_analyse'):
            args['limit'] = min(50, max(1, args.get('limit', 20)))
            args['days'] = min(365, max(1, args.get('days', 7)))
        if tool == 'neighbors' and not isinstance(args.get('name'), str):
            raise ValueError('Neighbor lookup needs a memory name')
        parsed.append((tool, args))
    for tool, args in parsed:
        entry = {'tool': tool, 'arguments': args, 'status': 'requested',
                 'coverage': 'bounded_response; no whole-corpus analysis claim'}
        memory['lookups'].append(entry)
        store.save(task, task['status'], 'memory_lookup_started', {'tool': tool})
        memory = task['data']['memory']
        entry = memory['lookups'][-1]
        started = time.monotonic()
        try:
            with connect(store, task, lease_fds) as client:
                if tool == 'recall':
                    advertised = capabilities(client)
                    support = advertised.get('tools', {}).get('recall', {}) if isinstance(advertised, dict) else {}
                    if advertised.get('contract') == 'orbit-1':
                        if support.get('scope') is True:
                            args['scope'] = memory['project_id']
                        if 'json' in support.get('formats', []):
                            args['format'] = 'json'
                result = client.call(tool, args)
                if isinstance(result, dict) and result.get('ok') is False:
                    raise ValueError('Brain lookup rejected: ' + str(result.get('error'))[:500])
                raw = json.dumps(result, ensure_ascii=False)
                entry.update(status='returned', result=result if len(raw) <= 8000 else raw[:8000],
                             truncated=len(raw) > 8000)
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            entry.update(status='unavailable', error=str(error)[:500])
        entry['seconds'] = round(time.monotonic() - started, 3)
        store.save(task, task['status'], 'memory_lookup_finished', {'tool': tool, 'status': entry['status']})
        memory = task['data']['memory']


def submitted(store, task, metadata):
    memory = task['data'].get('memory', {})
    packet = memory.get('latest')
    if packet and packet.get('included_in_prompt'):
        packet['delivered'] = True
        packet['session'] = metadata.get('session')
        store.save(task, task['status'], 'memory_context_submitted', {
            'context_id': packet.get('id'), 'session': packet['session'],
            'note': 'Provider call returned; this is delivery evidence, not usefulness feedback'})
