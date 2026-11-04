"""Model-free projections of runtime-owned tasks and sessions."""
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
import unicodedata
import webbrowser

from .store import Store, now
from .commands import browser_guide
from .context import limit_for
from .workspace import verify
from . import failures

# Runtime v2 (artifact / development with planner and manager) and runtime v3 (goal → executor → fixed checks → one
# verifier; redesign 2026-11-01 §4.4) share one projection. v3 states: received · working · checking · verifying ·
# verified · needs_input · stalled · interrupted · paused. Old states stay readable through LABELS (legacy mapping).
ACTIVE = {'planning', 'running', 'applying', 'verifying', 'evaluating', 'repairing', 'working', 'checking'}
ATTENTION = {'needs_review', 'blocked', 'interrupted', 'needs_input', 'stale', 'unattended', 'stalled'}
V3_STATES = ('received', 'working', 'checking', 'verifying', 'verified', 'needs_input', 'stalled', 'interrupted', 'paused')
V3_STAGE = {'received': 0, 'working': 1, 'checking': 2, 'verifying': 3, 'verified': 4}
LABELS = {
    'en': {'received': 'Queued', 'ready': 'Ready', 'planning': 'Planning', 'running': 'Working',
           'evaluating': 'Manager evaluation', 'applying': 'Applying', 'verifying': 'Verifying', 'repairing': 'Repairing',
           'waiting_peer': 'Waiting for peer', 'completed': 'Verified', 'already_satisfied': 'Already satisfied',
           'needs_review': 'Review changes', 'needs_input': 'Input needed', 'blocked': 'Blocked', 'interrupted': 'Interrupted',
           'stale': 'Evidence changed', 'paused': 'Paused', 'pause_pending': 'Pause requested',
           'working': 'Working', 'checking': 'Checking', 'verified': 'Verified', 'stalled': 'Work remains',
           'unattended': 'Executor absent', 'idle': 'Idle', 'unbound': 'Not started',
           'title': 'COSMOS ORBIT / OBSERVER', 'scope': 'Runtime-owned sessions only; external sessions are not scanned',
           'worker': 'Worker', 'on': 'online', 'off': 'offline', 'task': 'TASK', 'state': 'STATE',
           'provider': 'AI', 'goal': 'GOAL', 'attention': 'need attention', 'active': 'executing',
           'empty': 'No tasks in this store. Start a task with cosmos-orbit start.',
           'changes': 'RECENT CHANGES', 'stop': 'Ctrl-C closes the monitor; tasks keep running',
           'pending': 'Queued feedback', 'sessions': 'SESSIONS', 'reason': 'Reason'},

}


def age_seconds(stamp):
    try:
        return max(0, int((datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp)).total_seconds()))
    except (ValueError, TypeError):
        return 0


def event_view(row):
    data = json.loads(row['payload'])
    note = data.get('note') or data.get('reason') or data.get('text') or data.get('question') or data.get('phase') or ''
    if row['kind'] == 'action_prepared':
        note = ', '.join(c['path'] for c in data.get('changes', []))
    if row['kind'] in ('verified', 'checks_recorded'):
        checks = data.get('checks', [])
        note = '%d / %d' % (sum(bool(c.get('passed')) for c in checks), len(checks))
    # Runtime v3 events carry structured payloads; project one readable line each (the view translates the title).
    elif row['kind'] == 'goal_fixed' and not data.get('rev'):
        note = '%s · %d' % (data.get('origin', ''), len(data.get('done') or []))
    elif row['kind'] == 'resumed':
        note = (data.get('because') or '') + ': ' + ' · '.join(str(s).splitlines()[0][:160] if str(s).strip() else '' for s in data.get('sent') or [])
    elif row['kind'] == 'stalled':
        note = data.get('because') or ''
    elif row['kind'] == 'needs_input':
        note = (data.get('reason') or 'question') + ': ' + str(data.get('question') or '')
    elif row['kind'] == 'verdict':
        items = data.get('items') or []
        note = '%s · met %d / %d' % (data.get('outcome', ''), sum(1 for i in items if i.get('status') == 'met'), len(items))
        for key in ('extra', 'missing', 'fix'):
            if data.get(key):
                note += ' · %s %d' % (key, len(data[key]))
    elif row['kind'] == 'verifier_rejected':
        note = data.get('reason') or ''
    elif row['kind'] == 'executor_recorded':
        note = 'finished' if data.get('finished') else 'unfinished'
    elif row['kind'] == 'completion_verified':
        note = 'turn %s' % data.get('turn', '')
    elif row['kind'] == 'context_warning':
        note = '%s / %s · %s' % (data.get('tokens'), data.get('limit'), data.get('note', ''))
    elif row['kind'] in ('handoff_recommended', 'handoff_asked', 'handoff_told'):
        note = '%sk / %sk' % ((data.get('tokens') or 0) // 1000, (data.get('limit') or 0) // 1000) if data.get('limit') else '%sk' % ((data.get('tokens') or 0) // 1000)
    elif row['kind'] == 'session_rotated':
        note = '%s · previous peak %sk%s' % (data.get('reason') or '', (data.get('from_peak') or 0) // 1000, ' · checkpoint' if data.get('checkpoint') else '')
    elif row['kind'] in ('requirement_added', 'requirement_asked'):
        note = ('rev %s · ' % data.get('rev')) + str(data.get('text') or '')[:300]
    elif row['kind'] in ('goal_fixed', 'done_kept') and data.get('rev'):
        note = 'rev %s · %s%s%s' % (data.get('rev'), ('+' + ','.join(data.get('added') or [])) if data.get('added') else ('kept' if row['kind'] == 'done_kept' else ''),
                                    (' · checks +%d' % len(data.get('checks_added') or [])) if data.get('checks_added') else '',
                                    (' · ' + str(data.get('because'))[:200]) if data.get('because') else '')
    elif row['kind'] == 'checkpoint_recorded':
        note = '%s · %sk tokens · files %s' % (data.get('kind') or '', (data.get('tokens') or 0) // 1000, data.get('files', 0))
    elif row['kind'] == 'compacted':
        note = '%s · %sk' % (data.get('trigger') or '', (data.get('tokens') or data.get('from') or 0) // 1000)
    elif row['kind'] == 'automatic_checkpoint':
        note = 'fresh session from the ledger · %s' % ((data.get('measurement') or {}).get('tokens') or '')
    elif row['kind'] == 'handoff_auto':
        note = 'typing /clear in %s' % (data.get('terminal') or 'the terminal')
    elif row['kind'] == 'handoff_auto_failed':
        note = data.get('reason') or ''
    elif row['kind'] == 'message_wake':
        note = '%s%s · %s' % (data.get('outcome') or '', (' · from ' + data['sender']) if data.get('sender') else '', data.get('terminal') or '')
    return {'seq': row['seq'], 'task_id': row['task_id'], 'kind': row['kind'], 'at': row['at'],
            'note': str(note)[:600], 'age': age_seconds(row['at']),
            'layer': data.get('layer'), 'direction': data.get('direction'),
            'provider': data.get('provider'), 'alias': data.get('alias'),
            'tool': data.get('tool'), 'call_id': data.get('call_id'),
            'exit_code': data.get('exit_code'), 'failed': data.get('failed', False),
            'tool_id': data.get('tool_id')}


def model_options():
    """Read display-only suggestions without auth, network requests or model execution."""
    choices = {'codex': [], 'claude': [
        {'id': 'sonnet', 'label': 'Sonnet — everyday coding', 'source': 'CLI alias'},
        {'id': 'opus', 'label': 'Opus — complex reasoning', 'source': 'CLI alias'},
        {'id': 'haiku', 'label': 'Haiku — lightweight tasks', 'source': 'CLI alias'}]}
    cache = Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')) / 'models_cache.json'
    try:
        with cache.open(encoding='utf-8') as handle:
            raw = handle.read(2_000_001)
        if len(raw) > 2_000_000:
            return choices
        payload = json.loads(raw)
        models = payload.get('models', []) if isinstance(payload, dict) else []
        if not isinstance(models, list):
            return choices
        for model in models[:100]:
            if not isinstance(model, dict) or model.get('visibility') != 'list':
                continue
            name = model.get('slug')
            if isinstance(name, str) and 0 < len(name) <= 200:
                title = model.get('display_name')
                choices['codex'].append({'id': name, 'label': title[:200] if isinstance(title, str) else name,
                                         'source': 'Local CLI cache'})
    except (OSError, ValueError, UnicodeError):
        pass
    return choices


def snapshot(store, task_id=None):
    """Observe persisted facts and lock ownership; never invoke a model or repair files."""
    tasks = []
    summaries = store.list()
    selected = [r for r in summaries if r['id'] == task_id] if task_id else summaries[:100]
    if task_id and not selected:
        raise ValueError('Unknown task: ' + task_id)
    store.db.execute('BEGIN')
    try:
        for summary in selected:
            task = store.get(summary['id'])
            data, state = task['data'], task['status']
            busy = store.locked('task:' + task['id'])
            paused = store.paused(task['id'])
            checks = data.get('evidence', [])
            issue = ''
            if state in ('completed', 'already_satisfied') and data.get('contract'):
                try:
                    checks = verify(task['workspace'], data['contract'])
                    if not all(c.get('passed') for c in checks):
                        state = 'stale'
                except (OSError, ValueError) as error:
                    state, issue = 'stale', str(error)
            if state == 'needs_review' and data.get('development'):
                try:
                    from .development import evidence_current
                    if not evidence_current(task):
                        state = 'stale'
                except (OSError, ValueError) as error:
                    state, issue = 'stale', str(error)
            runtime_version = data.get('runtime_version', 2)
            reason_code = data.get('reason_code') or ''
            mode = data.get('mode', 'managed')
            if runtime_version == 3 and state == 'verified' and data.get('development') and mode != 'attached':
                # Same rule as the CLI status projection: files changed after verification → a human decision.
                try:
                    from .development import evidence_current
                    if not evidence_current(task):
                        state, reason_code = 'needs_input', 'stale_evidence'
                except (OSError, ValueError) as error:
                    state, reason_code, issue = 'needs_input', 'stale_evidence', str(error)
            if state in ACTIVE and not busy and not (mode == 'attached' and state == 'working'):
                state = 'unattended'
                if runtime_version == 3:
                    reason_code = 'unattended'
            if paused and state not in ('completed', 'already_satisfied', 'stale', 'verified'):
                state = 'pause_pending' if busy and task['status'] != 'paused' else 'paused'
            messages = store.messages(task['id'])
            sessions = []
            bindings = dict(data.get('sessions', {}))
            if not any(k == task['provider'] or k.startswith(task['provider'] + ':') for k in bindings):
                bindings[task['provider']] = None
            for name in data.get('peers', {}):
                bindings.setdefault('peer:' + name, None)
            for alias, session_id in bindings.items():
                peer = alias.startswith('peer:')
                name = alias[5:] if peer else alias
                provider = data.get('peers', {}).get(name, {}).get('provider', 'unknown') if peer else alias.split(':')[0]
                in_flight = any(m['target'] == name and m['status'] == 'in_flight' for m in messages) if peer else (
                    task['status'] in ('planning', 'running', 'working') and alias == task['provider']
                    or task['status'] == 'evaluating' and alias == task['provider'] + ':manager'
                    or task['status'] == 'verifying' and alias == task['provider'] + ':verifier')
                session_state = 'running' if in_flight and busy else 'idle' if session_id else 'unbound'
                sessions.append({'name': name, 'provider': provider, 'id': session_id, 'peer': peer,
                                 'state': session_state, 'managed': True,
                                 'requested_model': data.get('session_metadata', {}).get(alias, {}).get('requested_model',
                                     data.get('peers', {}).get(name, {}).get('model') if peer else
                                     data.get('options', {}).get('model') if alias == task['provider'] else None),
                                 'reported_models': data.get('session_metadata', {}).get(alias, {}).get('reported_models', []),
                                 'context_tokens': data.get('context_measurements', {}).get(alias, {}).get('tokens'),
                                 'context_limit': limit_for(data, alias, provider),
                                 'context_source': data.get('context_measurements', {}).get(alias, {}).get('source', 'unavailable'),
                                 'handoff_count': data.get('handoff_counts', {}).get(alias, 0)})
            rows = store.db.execute("SELECT * FROM events WHERE task_id=? AND kind NOT IN ('process_observation','context_measured','brief_skipped') ORDER BY seq DESC LIMIT 24", (task['id'],)).fetchall()
            events = [event_view(r) for r in rows]
            event_count = store.db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind NOT IN ('process_observation','context_measured','brief_skipped')", (task['id'],)).fetchone()[0]
            response_rows = store.db.execute("SELECT * FROM events WHERE task_id=? AND kind='provider_message' ORDER BY seq DESC LIMIT 6", (task['id'],)).fetchall()
            layer_activity = {}
            for layer, kinds_for_layer in {
                'control': ('evaluation_started', 'evaluation_finished', 'received', 'execution_plan_ready', 'feedback_applied', 'routing_applied', 'continuation_scheduled',
                            'goal_fixed', 'resumed', 'stalled', 'needs_input', 'verdict', 'verifier_rejected', 'completion_verified', 'executor_recorded'),
                'execution': ('model_started', 'model_finished', 'provider_message'),
                'context': ('memory_context_prepared', 'memory_context_submitted', 'session_checkpoint', 'automatic_checkpoint', 'context_warning'),
                'evidence': ('verified', 'result_published', 'baseline_recorded', 'checks_recorded', 'check_finished')}.items():
                placeholders = ','.join('?' for _ in kinds_for_layer)
                row = store.db.execute("SELECT * FROM events WHERE task_id=? AND (json_extract(payload,'$.layer')=? OR kind IN (" + placeholders + ")) ORDER BY seq DESC LIMIT 1", (task['id'], layer, *kinds_for_layer)).fetchone()
                layer_activity[layer] = event_view(row) if row else None
            call_state = store.db.execute("SELECT seq,kind,at FROM events WHERE task_id=? AND kind IN ('model_started','model_finished','interrupted') ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
            runtime = None
            if busy and call_state and call_state['kind'] == 'model_started':
                elapsed = age_seconds(call_state['at'])
                runtime = {'started': call_state['at'], 'elapsed_seconds': elapsed,
                           'timeout_seconds': None, 'remaining_seconds': None}
            health_row = store.db.execute("SELECT seq,at,payload FROM events WHERE task_id=? AND kind='process_observation' ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
            process_health = json.loads(health_row['payload']) if health_row else None
            if process_health:
                process_health['observation_age_seconds'] = age_seconds(health_row['at'])
                process_health['fresh'] = (busy and process_health['observation_age_seconds'] <= 15
                                           and (not call_state or call_state['kind'] != 'model_started'
                                                or health_row['seq'] > call_state['seq']))
                if not process_health['fresh']:
                    process_health['status'] = 'not_current'
            if runtime:
                runtime['process_health'] = process_health
            contract = data.get('contract') or {}
            call_rows = store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind IN ('model_finished','peer_response_received')", (task['id'],)).fetchall()
            calls = [json.loads(r['payload']) for r in call_rows]
            kinds = {r[0] for r in store.db.execute('SELECT DISTINCT kind FROM events WHERE task_id=?', (task['id'],))}
            visited = [0]
            if 'contract_created' in kinds:
                visited.append(1)
            if any(c.get('phase') == 'running' for c in calls):
                visited.append(2)
            if 'action_applied' in kinds:
                visited.append(3)
            if 'verified' in kinds:
                visited.append(4)
            usage_input = sum(c.get('usage', {}).get('input_tokens', 0) for c in calls)
            usage_output = sum(c.get('usage', {}).get('output_tokens', 0) for c in calls)
            by_phase = {}
            for c in calls:
                bucket = by_phase.setdefault(c.get('phase') or '-', {'calls': 0, 'seconds': 0.0})
                bucket['calls'] += 1
                bucket['seconds'] = round(bucket['seconds'] + (c.get('seconds') or 0), 1)
            stage = {'received': 0, 'planning': 1, 'ready': 2, 'running': 2, 'waiting_peer': 2,
                     'applying': 3, 'verifying': 4, 'evaluating': 4, 'repairing': 2, 'completed': 5,
                     'already_satisfied': 5, 'stale': 4, 'needs_review': 4}.get(task['status'], 1 if not contract else 2)
            if state == 'stale':
                stage = 4
            if data.get('pending'):
                stage = 3
            if data.get('development') and state in ('blocked', 'interrupted', 'unattended', 'paused', 'pause_pending'):
                stage = 1 if data.get('plan_requested') else {'verifying': 4, 'applying': 3}.get(data['development'].get('phase'), 2)
            if runtime_version == 3:
                # Five stations: goal · working · checking · verifying · verified. A stopped task keeps the furthest station reached.
                visited = [0]
                if 'model_started' in kinds or 'executor_recorded' in kinds:
                    visited.append(1)
                if 'checks_recorded' in kinds:
                    visited.append(2)
                if 'verdict' in kinds or any(c.get('phase') == 'verifying' for c in calls):
                    visited.append(3)
                if 'completion_verified' in kinds:
                    visited.append(4)
                stage = V3_STAGE.get(state, V3_STAGE.get(task['status'], max(visited)))
                if state in ('verifying',) or (state == 'unattended' and task['status'] == 'verifying'):
                    stage = 3
            started = datetime.datetime.fromisoformat(task['created'])
            ended = datetime.datetime.now(datetime.timezone.utc) if busy else datetime.datetime.fromisoformat(task['updated'])
            executor = data.get('executor_result') or {}
            goal_text = data.get('goal') if runtime_version == 3 else None
            done_evidence = {key: [{'at': e.get('at'), 'kind': e.get('kind'), 'note': str(e.get('note') or '')[:300],
                                    'exit_code': e.get('exit_code'), 'argv': ' '.join(e.get('argv') or [])[:160],
                                    'path': e.get('path'), 'sha256': (e.get('sha256') or '')[:12], 'seconds': e.get('seconds')}
                                   for e in rows[-5:]] for key, rows in (data.get('done_evidence') or {}).items()} if mode == 'attached' else None
            tasks.append({'id': task['id'], 'provider': task['provider'], 'status': state,
                          'runtime_version': runtime_version, 'reason_code': reason_code,
                          # Entrance: 'attached' = the user's own CLI session is the executor (no clone, no owned call while working).
                          'mode': mode, 'host': data.get('host'), 'attached_at': data.get('attached_at'),
                          'done_evidence': done_evidence, 'prompts_count': len(data.get('prompts') or []),
                          'briefs': data.get('briefs') if mode == 'attached' else None,
                          'verification_pending': bool(data.get('verification_pending')),
                          'context_live': __import__('harness.continuity', fromlist=['context_view']).context_view(data) if mode == 'attached' else None,
                          'handoff_state': data.get('handoff_state') if mode == 'attached' else None,
                          'checkpoints': [dict(c, next=str(c.get('next'))[:600], notes=str(c.get('notes'))[:800])
                                          for c in (data.get('checkpoints') or [])[-5:]],
                          'session_log': (data.get('session_log') or [])[-8:] if mode == 'attached' else None,
                          'handoff_total': sum((data.get('handoff_counts') or {}).values()),
                          'retry_at': data.get('retry_at'), 'active_call': data.get('active_call'),
                          'recoverable': data.get('recoverable', False),
                          # v3 contract (§4.1): goal · done · done_fixed · turns · verdicts · resumes; absent (None) on v2 tasks.
                          'done': data.get('done') if runtime_version == 3 else None,
                          'done_rev': data.get('done_rev'),
                          'done_history': [dict(h, because=str(h.get('because') or '')[:300], removed=[str(x)[:160] for x in h.get('removed') or []])
                                           for h in (data.get('done_history') or [])[-10:]],
                          'requirements_pending': data.get('requirements_pending'),
                          'done_fixed': data.get('done_fixed') if runtime_version == 3 else None,
                          'turns': data.get('turns') if runtime_version == 3 else None,
                          'max_turns': data.get('max_turns') if runtime_version == 3 else None,
                          'verdicts': [dict(v, items=[dict(i, evidence=[str(x)[:1200] for x in (i.get('evidence') or [])][:12])
                                                      for i in (v.get('items') or [])],
                                            extra=[str(x)[:600] for x in (v.get('extra') or [])][:12],
                                            missing=[str(x)[:600] for x in (v.get('missing') or [])][:12],
                                            fix=[str(x)[:600] for x in (v.get('fix') or [])][:12],
                                            question=str(v.get('question') or '')[:2000])
                                       for v in (data.get('verdicts') or [])[-6:]] if runtime_version == 3 else None,
                          'resumes': [dict(r, sent=[str(x)[:600] for x in (r.get('sent') or [])][:12])
                                      for r in (data.get('resumes') or [])[-12:]] if runtime_version == 3 else None,
                          'executor_result': {'summary': str(executor.get('summary') or '')[:4000],
                                              'finished': executor.get('finished'),
                                              'remaining': [str(x)[:600] for x in (executor.get('remaining') or [])][:20],
                                              'question': str(executor.get('question') or '')[:2000]} if runtime_version == 3 and executor else None,
                          'stage': stage, 'visited': visited, 'requested_model': data.get('options', {}).get('model'),
                          'routing': store.routing(task['id']), 'last_summary': data.get('last_summary', ''),
                          'files': data.get('development', {}).get('changed_paths', contract.get('files', [])),
                          'development': data.get('development'), 'execution_plan': data.get('execution_plan'),
                          'process_health': process_health, 'objective_progress': data.get('objective_progress'), 'evaluation': data.get('evaluation'),
                          'memory': {'mode': data.get('memory', {}).get('mode', 'off'),
                                     'project_id': data.get('memory', {}).get('project_id'),
                                     'seconds': (data.get('memory', {}).get('latest') or {}).get('seconds'),
                                     'notes': (data.get('memory', {}).get('latest') or {}).get('notes', []),
                                     'outbox': [{'id': m['id'], 'status': m['status'], 'attempts': m['attempts'],
                                                 'error': m.get('error'), 'ack': m.get('ack')}
                                                for m in data.get('memory', {}).get('outbox', [])]},
                          'metrics': {'calls': len(calls), 'model_seconds': round(sum(c.get('seconds') or 0 for c in calls), 1),
                                      'input_tokens': usage_input, 'output_tokens': usage_output, 'by_phase': by_phase,
                                      'elapsed_seconds': max(0, round((ended-started).total_seconds()))},
                          'recorded_status': task['status'], 'revision': task['revision'],
                          'goal': (data.get('presentation', {}).get('title') or (goal_text.strip().splitlines() or [''])[0][:240] if goal_text
                                   else data.get('presentation', {}).get('title') or contract.get('goal') or task['prompt']),
                          'request': goal_text if goal_text else data.get('mission_goal', task['prompt']),
                          'project': str(Path(data.get('development', {}).get('source', task['workspace'])).name),
                          'presentation': {k: str(v)[:16000] for k, v in data.get('presentation', {}).items()
                                           if k in ('title', 'summary', 'report', 'report_source', 'report_error')},
                          'workspace': task['workspace'], 'updated': task['updated'], 'runtime': runtime,
                          'age': age_seconds(task['updated']), 'busy': busy, 'pause_requested': paused,
                          'reason': issue or failures.describe(data), 'failure': failures.details(data), 'questions': contract.get('questions', []),
                          'constraints': contract.get('constraints', []), 'steps': contract.get('steps', []),
                          'attempt': data.get('turns', 0) if runtime_version == 3 else data.get('attempt', 0),
                          'budget': data.get('max_turns') if runtime_version == 3 else data.get('options', {}).get('max_attempts', 3),
                          'checks': checks, 'sessions': sessions, 'feedback': store.inbox(task['id']),
                          'messages': [{k: m[k] for k in ('id', 'target', 'question', 'reply', 'status', 'attempts', 'retry_at', 'error', 'updated')}
                                       for m in messages[-30:]], 'events': events, 'event_count': event_count,
                          'responses': [event_view(r) for r in reversed(response_rows)],
                          'layer_activity': layer_activity,
                          'attention': state in ATTENTION})
        rows = store.db.execute("SELECT * FROM events WHERE kind NOT IN ('process_observation','context_measured','brief_skipped') ORDER BY seq DESC LIMIT 20").fetchall()
        global_events = [event_view(r) for r in rows]
        cursor = store.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
    finally:
        store.db.rollback()
    tasks.sort(key=lambda t: t['updated'], reverse=True)
    tasks.sort(key=lambda t: (not t['attention'], not t['busy']))
    from .operations import describe
    worker_running = store.locked('worker')
    for task in tasks:
        task['operation'] = describe(task, worker_running)
    health = None
    try:
        # Read-only engine/session readiness (local --version and session record only; no model calls).
        # Adds session['health'] to each session and returns the project-level summary.
        from .health import project_health
        health = project_health(store.home, tasks)
    except ImportError:
        pass
    except (OSError, ValueError, TypeError, KeyError) as error:
        health = {'checked_at': now(), 'engines': {}, 'policy': 'unavailable', 'error': str(error)}
    return {'at': now(), 'scope': 'managed', 'home': str(store.home), 'cursor': cursor,
            'worker_running': worker_running, 'tasks': tasks, 'total': len(summaries),
            'health': health, 'continuity': continuity_summary(store),
            'events': global_events, 'labels': LABELS, 'model_options': model_options()}


def continuity_summary(store):
    """Who may type into a terminal, which panes a message can wake, and the self-evolution switch (read-only)."""
    from . import continuity, evolve, wake
    try:
        value = evolve.state(store)
        last = (value.get('history') or [{}])[-1]
        evolution = {'mode': value['mode'], 'candidates': len(value['candidates']), 'due': len(evolve.due(value)),
                     'last': {k: last.get(k) for k in ('at', 'key', 'outcome', 'status') if last.get(k)} or None}
    except Exception as error:  # noqa: BLE001 - the view never fails because of one panel
        evolution = {'mode': 'unknown', 'error': str(error)[:200]}
    try:
        woken = wake.summary(store)
    except Exception as error:  # noqa: BLE001
        woken = {'reachable': 0, 'recent': [], 'error': str(error)[:200]}
    return {'handoff_mode': continuity.handoff_mode(store), 'wake': woken, 'evolve': evolution}


def clean(text):
    return ''.join(c if c.isprintable() else ' ' for c in str(text))


def fit(text, width):
    output, used = '', 0
    for char in clean(text):
        size = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > width:
            break
        output += char
        used += size
    return output + ' ' * max(0, width - used)


def terminal(data, lang='en', width=110):
    t = LABELS[lang]
    rows = [t['title'] + '  |  ' + t['worker'] + ': ' + t['on' if data['worker_running'] else 'off'],
            t['scope'], '%s %d  ·  %s %d  ·  %d / %d' %
            (t['active'], sum(x['busy'] for x in data['tasks']), t['attention'],
             sum(x['attention'] for x in data['tasks']), len(data['tasks']), data['total'])]
    flow = data.get('continuity') or {}
    if flow:
        wake_rows = (flow.get('wake') or {})
        last = (wake_rows.get('recent') or [{}])[0]
        rows.append('handoff: %s  ·  message wake: %d pane(s)%s  ·  self-evolution: %s (%d due)' % (
            flow.get('handoff_mode'), wake_rows.get('reachable', 0),
            (' · last ' + str(last.get('outcome'))) if last.get('outcome') else '',
            (flow.get('evolve') or {}).get('mode'), (flow.get('evolve') or {}).get('due') or 0))
    rows.append('')
    goal_width = max(12, width - 48)
    rows.append(fit(t['task'], 14) + fit(t['state'], 22) + fit(t['provider'], 12) + t['goal'])
    rows.append('─' * min(width, 150))
    for task in data['tasks'][:18]:
        marker = '! ' if task['attention'] else '> ' if task['busy'] else '  '
        rows.append(marker + fit(task['id'][:10], 12) + fit(t.get(task['status'], task['status']), 22)
                    + fit(task['provider'], 12) + fit(task['goal'], goal_width))
        operation = task['operation']
        if task.get('runtime_version') == 3:
            stations = ['Goal', 'Working', 'Checking', 'Verifying', 'Verified']
            rows.append('  ' + stations[min(task['stage'], 4)] + ' · ' + operation['title'] + ' · runtime v3')
        else:
            rows.append('  ' + operation['phase'] + ' · ' + operation['title'])
        rows.append('  ' + clean(operation['reason']))
        if operation.get('next_action'):
            rows.append('  다음: ' + clean(operation['next_action']))
        if task.get('runtime_version') == 3:
            # Goal checklist as the terminal twin of the view: ○ not yet reviewed · ✓ met · ✗ unmet · ? unclear
            last = (task.get('verdicts') or [None])[-1]
            marks = {'met': '✓', 'unmet': '✗', 'unclear': '?'}
            statuses = {i['id']: i['status'] for i in (last or {}).get('items', [])}
            done_line = ' '.join(marks.get(statuses.get(d['id']), '○') + d['id'] for d in task.get('done') or []) or '(no done yet)'
            if task.get('mode') == 'attached':
                evidence_counts = ' '.join(k + ':' + str(len(v)) for k, v in (task.get('done_evidence') or {}).items()) or '-'
                live = task.get('context_live') or {}
                if live.get('tokens'):
                    rows.append('  context: %dk / handoff %s | sessions %d | checkpoints %d%s' % (
                        live['tokens'] // 1000, ('%dk' % (live['limit'] // 1000)) if live.get('limit') else 'off',
                        len(task.get('session_log') or []), len(task.get('checkpoints') or []),
                        ' | ' + (task.get('handoff_state') or {}).get('state', '') if task.get('handoff_state') else ''))
                rows.append('  entrance: attached · ' + clean(task['workspace']) + ' | prompts ' + str(task.get('prompts_count', 0))
                            + ' | briefs ' + str((task.get('briefs') or {}).get('served', 0)) + ' | evidence ' + evidence_counts
                            + (' | verification pending' if task.get('verification_pending') else ''))
            if task.get('requirements_pending'):
                rows.append('  request changed: ' + clean(task['requirements_pending'].get('text', ''))[:160] + ' → revise the goals (done --add / --keep)')
            if task.get('done_rev'):
                done_line = 'rev ' + str(task['done_rev']) + ' ' + done_line
            rows.append('  done: ' + done_line + ' | turns ' + str(task.get('turns')) + '/' + str(task.get('max_turns'))
                        + ' | resumes ' + str(len(task.get('resumes') or [])) + ' | verifier calls '
                        + str((task.get('metrics') or {}).get('by_phase', {}).get('verifying', {}).get('calls', 0)))
            if task.get('reason_code') or (task['status'] in ('needs_input', 'stalled', 'interrupted')):
                rows.append('  stop: ' + (task.get('reason_code') or task['status']) + ' — ' + clean(task.get('reason') or ''))
            if last and last.get('extra'):
                rows.append('  extra: ' + clean(' · '.join(last['extra']))[:200])
        if task.get('process_health'):
            h = task['process_health']
            rows.append('  process: ' + h['status'] + ' | PID ' + str(h['pid'])
                        + ' | observed ' + str(h['observation_age_seconds']) + 's ago'
                        + ' | silent ' + str(h['output_silent_seconds']) + 's'
                        + ' | TCP: ' + str(h.get('tcp_states')) + ' | remote: unverified')
        if (task['reason'] or task['questions']) and task.get('runtime_version') != 3:
            rows.append('  ' + fit(task['reason'] or task['questions'][0], max(20, width - 2)))
        for session in task['sessions']:
            rows.append('    ' + fit(session['name'], 18) + fit(t[session['state']], 12)
                        + clean(session['id'] or '—'))
            rows.append('      ' + clean(session['provider']) + ' | requested: ' + clean(session['requested_model'] or 'CLI default')
                        + ' | reported: ' + clean(', '.join(session['reported_models']) or 'unreported'))
            rows.append('      context: ' + str(session['context_tokens'] if session['context_tokens'] is not None else '?')
                        + ' / ' + str(session['context_limit']) + ' | handoffs: ' + str(session['handoff_count']))
        if task['routing']:
            rows.append('    → ' + clean(task['routing']['provider']) + ' / ' + clean(task['routing']['model'] or 'CLI default'))
        if task['feedback']:
            rows.append('    %s: %d' % (t['pending'], len(task['feedback'])))
    if not data['tasks']:
        rows.append(t['empty'])
    rows.extend(['', t['changes']])
    for event in data['events'][:6]:
        rows.append(fit(event['at'][11:19], 10) + fit(event['task_id'][:10], 12)
                    + fit(event['kind'], 29) + fit(event['note'], max(10, width - 51)))
    rows.extend(['', t['stop']])
    return '\n'.join(rows)


def watch(store, interval=2, once=False, task_id=None, lang='en'):
    if not 0.5 <= interval <= 60:
        raise ValueError('Monitor interval must be between 0.5 and 60 seconds')
    interactive = sys.stdout.isatty() and not once
    previous = None
    try:
        if interactive:
            print('\033[?1049h\033[?25l', end='', flush=True)
        while True:
            data = snapshot(store, task_id)
            rendered = terminal(data, lang, shutil.get_terminal_size((110, 30)).columns)
            if rendered != previous:
                if interactive:
                    print('\033[H\033[2J', end='')
                print(rendered, flush=True)
                previous = rendered
            if once:
                break
            time.sleep(interval)
    finally:
        if interactive:
            print('\033[?25h\033[?1049l', end='', flush=True)


def serve(home, port=0, open_browser=True, lang='en'):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if not 0 <= port <= 65535:
        raise ValueError('Invalid port')
    token = secrets.token_urlsafe(32)
    template = Path(__file__).with_name('observer.html').read_text(encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code, content, kind='application/json'):
            raw = content.encode('utf-8')
            self.send_response(code)
            for key, value in {'Content-Type': kind + '; charset=utf-8', 'Content-Length': str(len(raw)),
                               'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                               'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                               'Cross-Origin-Resource-Policy': 'same-origin',
                               'Content-Security-Policy': "default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % token}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def valid_token(self):
            provided = self.headers.get('X-Harness-Token', '')
            return provided.isascii() and secrets.compare_digest(provided, token)

        def do_GET(self):
            if self.headers.get('Host') != authority:
                self.respond(403, '{}')
                return
            if self.path.startswith('/events?') and self.valid_token():
                from urllib.parse import urlsplit, parse_qs
                store = Store(home)
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    task_id = query.get('task_id', [''])[0]
                    before = int(query.get('before', ['0'])[0])
                    if before <= 0:
                        raise ValueError('Invalid event cursor')
                    store.get(task_id)
                    rows = store.db.execute("SELECT * FROM events WHERE task_id=? AND seq<? AND kind NOT IN ('process_observation','context_measured','brief_skipped') ORDER BY seq DESC LIMIT 100", (task_id, before)).fetchall()
                    self.respond(200, json.dumps({'events': [event_view(row) for row in rows]}, ensure_ascii=False))
                except (ValueError, KeyError):
                    self.respond(400, json.dumps({'error': '작업 또는 기록 위치를 확인할 수 없습니다.'}, ensure_ascii=False))
                finally:
                    store.close()
            elif self.path == '/':
                self.respond(200, template.replace('__TOKEN__', token).replace('__LANG__', lang).replace('__COMMAND_GUIDE__', browser_guide()), 'text/html')
            elif self.path == '/state' and self.valid_token():
                store = Store(home)
                try:
                    self.respond(200, json.dumps(snapshot(store), ensure_ascii=False))
                finally:
                    store.close()
            else:
                self.respond(404, '{}')

        def do_POST(self):
            if self.headers.get('Host') != authority or self.headers.get('Origin') != origin or not self.valid_token():
                self.respond(403, '{}')
                return
            if self.path not in ('/feedback', '/pause', '/continue', '/switch', '/start', '/attach', '/verify', '/assess'):
                self.respond(404, '{}')
                return
            store = Store(home)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32000:
                    raise ValueError('Invalid body size')
                body = json.loads(self.rfile.read(size))
                if self.path == '/start':
                    prompt, root, provider = body.get('prompt'), body.get('workspace'), body.get('provider')
                    checks = body.get('checks')
                    done = body.get('done', [])
                    max_turns = body.get('max_turns', 8)
                    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24000
                            or not isinstance(root, str) or provider not in ('codex', 'claude')
                            or not isinstance(checks, list) or len(checks) > 12
                            or type(body.get('public_web', False)) is not bool
                            or not isinstance(done, list) or type(max_turns) is not int or not 1 <= max_turns <= 100):
                        raise ValueError('Provide a request, repository, provider and optional verification commands')
                    if not shutil.which(provider):
                        raise ValueError('Selected CLI is not installed')
                    from .simple_loop import completion_items
                    completion_items(done, [], 'user')
                    from .development import create
                    task = create(store, root, provider, prompt, [json.dumps(c) for c in checks], done or None, max_turns)
                    task['data']['options'].update(public_web=body.get('public_web', False))
                    task['data']['presentation'] = {'title': prompt.strip().splitlines()[0][:240]}
                    store.save(task, 'received', 'options_recorded', task['data']['options'])
                    self.respond(200, json.dumps({'task_id': task['id'], 'worker_running': store.locked('worker')}))
                    return
                if self.path == '/attach':
                    prompt, root, host = body.get('prompt'), body.get('workspace'), body.get('provider')
                    checks, done = body.get('checks') or [], body.get('done') or []
                    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24000 or not isinstance(root, str)
                            or host not in ('codex', 'claude') or not isinstance(checks, list) or not isinstance(done, list)):
                        raise ValueError('Provide a request, repository and host')
                    from .attach import create, set_switch
                    from .simple_loop import completion_items
                    completion_items(done, [], 'user')
                    task = create(store, root, prompt, host, None, [json.dumps(c) for c in checks], done or None)
                    switched = set_switch(store, 'on', root)
                    self.respond(200, json.dumps({'task_id': task['id'], 'workspace': switched['root'], 'orbit': 'on'}))
                    return
                task_id = body.get('task_id')
                if not isinstance(task_id, str):
                    raise ValueError('Task ID required')
                # Hold the write reservation through revision validation and enqueue/control.
                store.db.execute('BEGIN IMMEDIATE')
                task = store.get(task_id)
                if self.path in ('/feedback', '/verify', '/assess') and body.get('revision') != task['revision']:
                    self.respond(409, json.dumps({'error': 'Task changed; refresh and review before submitting'}))
                    return
                if self.path == '/feedback':
                    text = body.get('text')
                    done = body.get('done')
                    if done is not None and (task['data'].get('runtime_version') != 3 or not isinstance(done, list)):
                        raise ValueError('Completion definitions apply to simplified (runtime v3) tasks only')
                    if (not isinstance(text, str) or len(text) > 24000) or (not text.strip() and not done):
                        raise ValueError('Nonempty feedback required')
                    if done:
                        # Explicit replacement of the frozen completion definition (§10 ②): the only way `done` changes.
                        store.db.rollback()
                        from .engine import Engine
                        try:
                            Engine(store).configure(task_id, {'done': done})
                        except RuntimeError:
                            self.respond(409, json.dumps({'error': 'Task is executing; completion definitions change at a safe boundary. Pause first or retry.'}))
                            return
                    message_id = store.enqueue(task_id, text, None) if text.strip() else None
                    self.respond(200, json.dumps({'queued': message_id, 'done': bool(done), 'worker_running': store.locked('worker')}))
                elif self.path in ('/verify', '/assess'):
                    from .verifier import request_verification
                    from .simple_loop import migrate
                    try:
                        with store.lock('task:' + task_id):
                            if task['data'].get('mode') == 'attached':
                                if not (task['data'].get('executor_result') or {}).get('finished'):
                                    raise ValueError('Nothing claimed yet; the executor runs claim first')
                                task['data'].update(verification_pending=True, reason='', reason_code='')
                                task['data'].pop('checks_done_for', None)
                                task['data']['development']['phase'] = 'checking'
                                store.save(task, 'checking', 'verification_requested', {'turn': task['data'].get('turns', 0)})
                            elif migrate(store, task):
                                request_verification(store, task)
                    except RuntimeError:
                        self.respond(409, json.dumps({'error': 'Task is executing; pause and wait for a safe boundary before verifying.'}))
                        return
                    if task['data'].get('mode') == 'attached' and not store.locked('worker'):
                        from .recovery import ensure_worker
                        ensure_worker(store)
                    self.respond(200, json.dumps({'queued': True, 'worker_running': store.locked('worker')}))
                elif self.path == '/switch':
                    request = store.route(task_id, body.get('provider'), body.get('model') or None)
                    self.respond(200, json.dumps({'queued': request, 'worker_running': store.locked('worker')}))
                else:
                    store.set_paused(task_id, self.path == '/pause')
                    if self.path == '/continue':
                        try:
                            with store.lock('task:' + task_id):
                                task = store.get(task_id)
                                from .simple_loop import migrate
                                if task['data'].get('runtime_version') != 3:
                                    migrate(store, task)
                                elif (task['status'] == 'interrupted' or task['status'] == 'needs_input'
                                      and task['data'].get('reason_code') in ('provider_wait', 'connection_wait', 'process_error')):
                                    task['data'].pop('retry_at', None)
                                    task['data'].update(recoverable=True, retry_count=0, reason='', reason_code='')
                                    store.save(task, 'received', 'continue_requested')
                        except RuntimeError:
                            pass  # The current owner sees the persisted Continue control.
                    self.respond(200, json.dumps({'saved': True, 'worker_running': store.locked('worker')}))
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                self.respond(400, json.dumps({'error': str(error)}))
            finally:
                store.close()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    authority = '127.0.0.1:%d' % server.server_port
    origin = 'http://' + authority
    print(origin, flush=True)
    if open_browser:
        webbrowser.open(origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
