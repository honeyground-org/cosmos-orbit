"""Context measurement and mid-task handoff, for attached sessions and unattended work.

A long session re-reads its whole context on every turn. Measured on 99 real Claude Code sessions over
14 days, the median session started at 78k tokens and peaked at 506k. Replaying those sessions with a
fresh-context handoff at 200k saved 31-51% of cost-equivalent tokens (input x1, cache write x1.25, cache
read x0.1) across re-read assumptions of 15k-80k tokens per handoff; 150k thrashed when re-reading was
heavy. The built-in default is 350k: replayed again on 113 sessions (2026-11-04) it still saved 32-38% while
handing off 3-6x less often than 200k (150-177 handoffs instead of 444-1,075), so a session is interrupted far
less for a few points of saving. scripts/measure_context_growth.py repeats the comparison locally.

The point is a setting, not a constant: `<home>/settings.json` holds it (section "handoff"), the
`cosmos-orbit context` command writes it, a hand edit takes effect at the next hook event, and
ORBIT_HANDOFF_AT overrides it for one terminal.

Nothing here calls a model. A handoff is rebuilt from the ledger plus one short checkpoint the executor
writes about what the ledger cannot know: the approach in progress and the next concrete step.

A hook cannot clear its own session. So a handoff ends in one of three ways: the user types /clear (or /new),
the host compacts the session itself, or - with `cosmos-orbit context auto` in cmux or tmux - Orbit types it
(see typist.py). A compaction closes the open handoff and measures again from the compacted size, and an
open handoff expires: an invitation nobody took must never hand a task to an unrelated session later.
"""
import json
import os
from pathlib import Path
import re
import subprocess

from .store import now

DEFAULT_HANDOFF_AT = 350_000
MIN_GROWTH = 60_000          # never hand off a session that has not grown this much since it started
SETTINGS_FILE = 'settings.json'
LEGACY_GLOBAL_KEY = 'handoff_at'        # before the settings file these lived in the ledger's preferences table
LEGACY_PROJECT_PREFIX = 'handoff_at:'
LEGACY_MODE_KEY = 'handoff_mode'
TAIL_BYTES = 4_000_000
RECORD_STEP = 10_000          # store a measurement when it moved this much (keeps the ledger readable)
INVITE_TTL_SECONDS = 12 * 3600  # an open handoff nobody took expires (observed: taken 18 hours later by unrelated work)
OPEN_STATES = ('recommended', 'asked', 'checkpointed', 'told', 'auto', 'auto_failed')
INVITING_STATES = ('checkpointed', 'told', 'auto', 'auto_failed')
MODES = ('tell', 'auto')      # 'tell' (default): ask the user to type /clear · 'auto': type it where a typist exists


# ---------------------------------------------------------------- settings

def parse_tokens(value, strict=True):
    """'200000', '200k', '1.5m', 'on' (default) or 'off' (0)."""
    text = str(value).strip().lower().replace(',', '').replace('_', '')
    if text in ('off', 'no', 'false', '0'):
        return 0
    if text in ('on', 'yes', 'true', 'default'):
        return DEFAULT_HANDOFF_AT
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([km]?)', text)
    if not match:
        raise ValueError('Give a token count such as 200000, 200k or 1.5m, or on / off')
    tokens = int(float(match[1]) * {'': 1, 'k': 1000, 'm': 1_000_000}[match[2]])
    if strict and 0 < tokens < 20_000:
        raise ValueError('A handoff point below 20k tokens would hand off on almost every turn')
    return tokens


def settings_path(store):
    return Path(store.home) / SETTINGS_FILE


def _migrate_legacy(store, path):
    """Move handoff settings from the ledger's preferences table into the file, once, so one place holds them."""
    if path.exists():
        return
    rows = [row for row in store.db.execute("SELECT key, value FROM preferences WHERE key LIKE 'handoff%'")
            if row[0] in (LEGACY_GLOBAL_KEY, LEGACY_MODE_KEY) or row[0].startswith(LEGACY_PROJECT_PREFIX)]
    if not rows:
        return
    section = {}
    for key, value in rows:
        if key == LEGACY_MODE_KEY:
            if value in MODES:
                section['mode'] = value
        elif key == LEGACY_GLOBAL_KEY:
            section['at'] = int(value)
        else:
            section.setdefault('projects', {})[key[len(LEGACY_PROJECT_PREFIX):]] = int(value)
    _write_file(path, {'handoff': section})
    with store.db:
        store.db.executemany('DELETE FROM preferences WHERE key=?', [(row[0],) for row in rows])


def _write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('%s.%d.tmp' % (path.name, os.getpid()))
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_settings(store):
    """(the whole settings file, problem or None). A missing file is empty; a broken one never raises in a hook."""
    path = settings_path(store)
    _migrate_legacy(store, path)
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, 'cannot read %s: %s' % (path, exc)
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        return {}, '%s is not valid JSON (%s)' % (path, exc)
    if not isinstance(data, dict):
        return {}, '%s must hold a JSON object' % path
    return data, None


def _section(data):
    section = data.get('handoff')
    return section if isinstance(section, dict) else {}


def _projects(section):
    projects = section.get('projects')
    return projects if isinstance(projects, dict) else {}


def _tokens(value):
    """A value from the file: a number, '350k', 'on' or 'off'; None when absent or unreadable."""
    if value is None or isinstance(value, (list, dict)):
        return None
    try:
        return parse_tokens(value)
    except ValueError:
        return None


def update_settings(store, change):
    """Read, change and write the file under a lock. A file that cannot be parsed is the user's to fix: never overwrite it."""
    with store.lock('settings-file', timeout=2.0):
        data, problem = read_settings(store)
        if problem:
            raise ValueError(problem + ' - fix or remove it, then run the command again')
        section = data.get('handoff')
        if not isinstance(section, dict):
            section = data['handoff'] = {}
        change(section)
        _write_file(settings_path(store), data)
    return data


def resolve_threshold(store, root):
    """Winner first: ORBIT_HANDOFF_AT environment → settings file project → settings file default → built-in. 0 means off."""
    env = os.environ.get('ORBIT_HANDOFF_AT')
    if env:
        try:
            return parse_tokens(env, strict=False), 'environment'
        except ValueError:
            pass
    section = _section(read_settings(store)[0])
    value = _tokens(_projects(section).get(str(Path(root).resolve())))
    if value is not None:
        return value, 'project'
    value = _tokens(section.get('at'))
    if value is not None:
        return value, 'default'
    return DEFAULT_HANDOFF_AT, 'built-in'


def set_threshold(store, value, root=None, global_default=False):
    tokens = parse_tokens(value)
    if global_default:
        update_settings(store, lambda section: section.__setitem__('at', tokens))
        return {'scope': 'default', 'handoff_at': tokens, 'settings_file': str(settings_path(store))}
    project = str(Path(root).resolve())

    def change(section):
        projects = section.get('projects')
        if not isinstance(projects, dict):
            projects = section['projects'] = {}
        projects[project] = tokens
    update_settings(store, change)
    return {'scope': 'project', 'root': project, 'handoff_at': tokens, 'settings_file': str(settings_path(store))}


def clear_threshold(store, root=None, global_default=False):
    """Remove a stored point so the next scope applies again (`cosmos-orbit context reset [--global]`)."""
    project = str(Path(root).resolve()) if root is not None else None

    def change(section):
        if global_default:
            section.pop('at', None)
        else:
            _projects(section).pop(project, None)
    update_settings(store, change)
    return {'scope': 'default' if global_default else 'project', 'root': project, 'handoff_at': None,
            'settings_file': str(settings_path(store))}


def handoff_mode(store):
    return 'auto' if _section(read_settings(store)[0]).get('mode') == 'auto' else 'tell'


def set_handoff_mode(store, mode):
    if mode not in MODES:
        raise ValueError('The handoff mode is auto or tell')
    update_settings(store, lambda section: section.__setitem__('mode', mode))
    return mode


def invitation_open(state, at=None):
    """A checkpointed handoff a fresh session may still take: not compacted, not adopted, not expired."""
    if (state or {}).get('state') not in INVITING_STATES:
        return False
    from datetime import datetime, timezone
    stamp = state.get('auto_at') or state.get('told_at') or state.get('checkpoint_at') or state.get('at')
    if not stamp:
        return True  # a record from before handoffs carried times: never expires, as before
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    current = at or datetime.now(timezone.utc)
    return (current - then).total_seconds() <= INVITE_TTL_SECONDS


def close_on_compaction(data, session):
    """The host compacted the session itself: that was the handoff. Close it and measure again from here."""
    state = data.get('handoff_state') or {}
    if state.get('session') == session and state.get('state') in OPEN_STATES:
        state.update(state='compacted', compacted_at=now())
        return True
    return False


def threshold_view(store, root):
    tokens, source = resolve_threshold(store, root)
    data, problem = read_settings(store)
    section = _section(data)
    problems = [problem] if problem else []
    projects = {}
    for project, value in _projects(section).items():
        projects[project] = _tokens(value)
        if projects[project] is None:
            problems.append('handoff.projects["%s"] = %r is not a token count (ignored)' % (project, value))
    default = _tokens(section.get('at'))
    if section.get('at') is not None and default is None:
        problems.append('handoff.at = %r is not a token count (ignored)' % (section.get('at'),))
    if section.get('mode') is not None and section.get('mode') not in MODES:
        problems.append('handoff.mode = %r is not auto or tell (tell applies)' % (section.get('mode'),))
    path = settings_path(store)
    return {'here': {'handoff_at': tokens, 'source': source, 'off': tokens == 0},
            'default': default if default is not None else DEFAULT_HANDOFF_AT, 'built_in': DEFAULT_HANDOFF_AT,
            'settings_file': {'path': str(path), 'exists': path.exists(), 'problems': problems},
            'projects': projects, 'min_growth': min_growth(),
            'environment': os.environ.get('ORBIT_HANDOFF_AT'),
            'handoff_mode': handoff_mode(store), 'typist': (__import__('harness.typist', fromlist=['detect']).detect() or {}).get('kind')}


def min_growth():
    try:
        return max(0, int(os.environ.get('ORBIT_HANDOFF_MIN_GROWTH', MIN_GROWTH)))
    except ValueError:
        return MIN_GROWTH


WINDOW_SHARE = 0.8  # of the model's window: hand off before the host compacts by itself near its window


def window_cap(threshold, window):
    """The handoff point for a session whose model window is known: min(setting, 80% of the window); 0 stays off.

    Measured 2026-11-05: Codex reports a 258,400-token window, below the 350k default, so a Codex session was never
    asked to hand off; it compacted by itself instead (five times in one session, no Codex handoff ever). Claude
    Code transcripts carry no window, so their setting stands as it is."""
    if not threshold or not isinstance(window, int) or window <= 0:
        return threshold
    return min(threshold, int(window * WINDOW_SHARE))


def limit_for_session(threshold, start):
    """The point at which this session should hand off; 0 when handoff is off."""
    if not threshold:
        return 0
    return max(threshold, (start or 0) + min_growth())


# ---------------------------------------------------------------- measurement

def tail_lines(path, limit=TAIL_BYTES):
    with open(path, 'rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - limit))
        data = stream.read()
    lines = data.decode('utf-8', 'replace').splitlines()
    return lines[1:] if size > limit else lines


def measure_transcript(path):
    """Latest context size in a Claude Code transcript or a Codex rollout file; None when unknown."""
    try:
        lines = tail_lines(path)
    except OSError:
        return None
    for line in reversed(lines):
        if '"usage"' in line and '"assistant"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') != 'assistant' or event.get('isSidechain'):
                continue
            message = event.get('message') or {}
            usage = message.get('usage') or {}
            tokens = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
            if tokens > 0:
                return {'tokens': tokens, 'source': 'claude_transcript', 'model': message.get('model'), 'window': None}
        elif '"token_count"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get('payload') or {}
            if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            info = payload.get('info') or {}
            tokens = (info.get('last_token_usage') or {}).get('total_tokens')
            if isinstance(tokens, int) and tokens > 0:
                return {'tokens': tokens, 'source': 'codex_rollout', 'model': None, 'window': info.get('model_context_window')}
    return None


def measure(payload, host):
    path = payload.get('transcript_path')
    if path and Path(path).is_file():
        found = measure_transcript(path)
        if found:
            return found
    if host == 'codex' and payload.get('session_id'):
        from .context import codex_measurement
        found = codex_measurement(payload['session_id'])
        if found.get('tokens'):
            return dict(found, source='codex_session', model=None)
    return None


# ---------------------------------------------------------------- attached sessions

def observe(store, task, payload, host, event, source=None):
    """Record the session's context size, session changes and the handoff point. Returns the fresh task."""
    measured = measure(payload, host)
    session = payload.get('session_id')
    root = Path(task['workspace'])
    threshold, threshold_source = resolve_threshold(store, root)
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        data = task['data']
        live = data.setdefault('context_live', {})
        log = data.setdefault('session_log', [])
        kind, payload_out = None, {}
        if session and live.get('session') and session != live['session']:
            if log and log[-1].get('session') == live['session']:
                log[-1].update(ended=now(), peak=live.get('peak'))
            payload_out = {'from': live['session'], 'to': session, 'from_peak': live.get('peak'),
                           'reason': source or event, 'checkpoint': bool(_checkpoint_since(data, (log[-1] if log else {}).get('started')))}
            kind = 'session_rotated'
            live.clear()
        if session and not live.get('session'):
            live.update(session=session, started=now(), start=None, peak=0)
            log.append({'session': session, 'started': live['started'], 'start': None, 'peak': 0, 'reason': source or event})
            del log[:-20]
            kind = kind or 'session_observed'
        if measured:
            tokens = measured['tokens']
            previous = live.get('tokens')
            if previous and tokens < previous * 0.6 and kind != 'session_rotated':
                kind, payload_out = 'compacted', {'trigger': 'observed', 'from': previous, 'to': tokens}
                live['start'] = tokens
                data['context_dropped'] = True
                close_on_compaction(data, live.get('session'))
            if live.get('start') is None:
                live['start'] = tokens
                if log and log[-1].get('session') == live.get('session'):
                    log[-1]['start'] = tokens
            moved = previous is None or abs(tokens - previous) >= RECORD_STEP
            live.update(tokens=tokens, at=now(), source=measured['source'], window=measured.get('window'),
                        model=measured.get('model') or live.get('model'), peak=max(live.get('peak') or 0, tokens))
            if log and log[-1].get('session') == live.get('session'):
                log[-1]['peak'] = live['peak']
            kind = kind or ('context_measured' if moved else None)
        capped = window_cap(threshold, live.get('window'))
        live.update(threshold=capped, threshold_source='window' if capped != threshold else threshold_source,
                    configured_threshold=threshold, limit=limit_for_session(capped, live.get('start')))
        state = data.get('handoff_state') or {}
        if (live.get('limit') and live.get('tokens') and live['tokens'] >= live['limit']
                and (state.get('session') != live.get('session') or state.get('state') == 'compacted')
                and task['status'] not in ('stalled', 'interrupted')):
            data['handoff_state'] = {'session': live.get('session'), 'state': 'recommended', 'tokens': live['tokens'],
                                     'limit': live['limit'], 'at': now()}
            kind, payload_out = 'handoff_recommended', {'tokens': live['tokens'], 'limit': live['limit'],
                                                        'threshold_source': threshold_source}
        if kind:
            store.save(task, task['status'], kind, dict(payload_out, tokens=live.get('tokens'), session=live.get('session'), layer='context'))
    return task


def _checkpoint_since(data, stamp):
    rows = data.get('checkpoints') or []
    if not stamp:
        return rows[-1] if rows else None
    return next((row for row in reversed(rows) if row.get('at', '') >= stamp), None)


def record_checkpoint(store, task_id, next_step, notes='', files=(), by=None, kind='executor'):
    """The executor's working-memory snapshot: what the ledger cannot know. Bounded; no file contents."""
    next_step = (next_step or '').strip()
    if not next_step:
        raise ValueError('Give the next concrete step with --next')
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        root = Path(task['workspace']).resolve()
        listed = []
        for name in list(files)[:20]:
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError('Checkpoint files must be inside the workspace: ' + name)
            listed.append({'path': str(target.relative_to(root)), 'exists': target.is_file()})
        changed = []
        base = (data.get('development') or {}).get('base')
        if base:
            try:
                result = subprocess.run(['git', '-C', str(root), 'diff', '--name-only', base, '--'], capture_output=True, text=True, timeout=10)
                untracked = subprocess.run(['git', '-C', str(root), 'ls-files', '--others', '--exclude-standard'], capture_output=True, text=True, timeout=10)
                changed = sorted({p for p in (result.stdout + untracked.stdout).splitlines() if p})[:40]
            except (OSError, subprocess.TimeoutExpired):
                changed = []
        live = data.get('context_live') or {}
        row = {'at': now(), 'by': by or data.get('host') or task['provider'], 'kind': kind,
               'next': next_step[:1200], 'notes': (notes or '').strip()[:2000], 'files': listed,
               'changed': changed, 'tokens': live.get('tokens'), 'session': live.get('session'),
               'turn': data.get('turns', 0)}
        rows = data.setdefault('checkpoints', [])
        rows.append(row)
        del rows[:-10]
        state = data.get('handoff_state') or {}
        if state.get('state') in ('recommended', 'asked'):
            state['state'] = 'checkpointed'
            state['checkpoint_at'] = row['at']
        store.save(task, task['status'], 'checkpoint_recorded',
                   {'kind': kind, 'tokens': row['tokens'], 'files': len(listed), 'changed': len(changed), 'layer': 'context'})
    try:
        from .attach import write_handoff
        write_handoff(store, store.get(task_id), store.home / 'runs' / task_id / 'handoff.md')
    except (OSError, ValueError, RuntimeError):
        pass
    return row


def auto_checkpoint(store, task, trigger):
    """Before native compaction: keep a ledger-built checkpoint when the executor wrote none this session."""
    data = task['data']
    live = data.get('context_live') or {}
    if _checkpoint_since(data, live.get('started')):
        return None
    executor = data.get('executor_result') or {}
    remaining = [r for r in executor.get('remaining') or [] if r.strip()]
    prompts = data.get('prompts') or []
    next_step = ('; '.join(remaining) if remaining else
                 'Continue the latest request: ' + (prompts[-1]['text'] if prompts else data.get('goal') or task['prompt']))
    return record_checkpoint(store, task['id'], next_step,
                             notes='Written by Orbit from the ledger before native compaction (%s); the executor wrote no checkpoint this session.' % trigger,
                             by='orbit', kind='auto')


def auto_handoff(store, task, session, text, kind, host, closed=False, terminal=None, spawn=None, during_turn=False):
    """Auto mode: the user's next request after a checkpointed handoff starts the fresh context by itself.

    The request is carried in the ledger, the prompt is blocked here, and a detached typist types /clear and one
    fixed line; the fresh session takes the task over and finds the request in its brief. Only Claude Code (its
    prompt hook can block a prompt), only in cmux or tmux (something can type), only a request or a go-ahead: a
    question about the last answer needs that answer, so it is answered in place and the next request hands off.
    Never during a turn: a prompt typed while the session works goes into that turn, and keys typed then wait in
    the host's input queue (2026-11-05: the blocked request was lost to that turn and the queued /clear ran four
    minutes later, after the typist had given up). The handoff waits for the first request after the turn ends.
    Returns the hook reply, or None to let the prompt through."""
    if closed or host != 'claude' or kind not in ('requirement', 'trivial') or handoff_mode(store) != 'auto':
        return None
    state = task['data'].get('handoff_state') or {}
    if state.get('session') != session or state.get('state') not in ('checkpointed', 'told'):
        return None
    if during_turn:
        try:
            with store.lock('task:' + task['id']):
                current = store.get(task['id'])
                store.save(current, current['status'], 'handoff_deferred',
                           {'reason': 'the prompt arrived during a turn; the next request after it hands off',
                            'session': session, 'layer': 'context'})
        except (RuntimeError, ValueError):
            pass
        return None
    from . import typist
    terminal = typist.detect() if terminal is None else terminal
    if not terminal:
        return None
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        data = task['data']
        state = data.get('handoff_state') or {}
        if state.get('session') != session or state.get('state') not in ('checkpointed', 'told'):
            return None
        data['carried_prompt'] = {'text': text[:4000], 'at': now(), 'from_session': session}
        state.update(state='auto', auto_at=now(), panel=terminal['target'])  # only the session in this pane takes it
        store.save(task, task['status'], 'handoff_auto', {'terminal': terminal['kind'], 'layer': 'context'})
    (spawn or typist.spawn)(terminal, typist.handoff_steps(host, typist.clear_presses(text)), store.home / 'hooks.log',
                            home=store.home, task=task['id'])
    live = data.get('context_live') or {}
    return {'decision': 'block', 'reason': (
        'Orbit: handing off to a fresh context (context %s tokens >= handoff point %s). Your request is carried over '
        'and runs there by itself; there is nothing to retype.' % (_k(live.get('tokens')), _k(state.get('limit'))))}


def auto_failed(store, task_id, reason):
    """The typist could not hand off: fall back to telling. The request stays carried for a manual /clear."""
    try:
        with store.lock('task:' + task_id):
            task = store.get(task_id)
            state = task['data'].get('handoff_state') or {}
            if state.get('state') == 'auto':
                state.update(state='auto_failed', failed_at=now())
                store.save(task, task['status'], 'handoff_auto_failed', {'reason': str(reason)[:200], 'layer': 'context'})
    except (RuntimeError, ValueError):
        pass


ADOPT_GRACE_SECONDS = 3.0  # the fresh session's own hook decides first; the typist steps in only after it


def adopt_in_panel(store, task_id, panel, grace=ADOPT_GRACE_SECONDS):
    """The typist's side of an auto handoff: True once the task is taken over in `panel`.

    Orbit typed /clear in that pane, so the session whose hooks appear there afterwards is the fresh one; a pane
    holds one session at a time. Its own SessionStart hook normally takes the task. When it did not (it was
    marked as having no task, or the task lock was busy that instant), the typist hands the task to it here,
    so the carry line follows. A session already working on another task of its own is never taken from it."""
    from .attach import NO_TASK, SESSION_LOCK_WAIT
    try:
        task = store.get(task_id)
    except ValueError:
        return False
    state = task['data'].get('handoff_state') or {}
    if state.get('state') == 'adopted':
        return True
    if state.get('state') != 'auto' or state.get('panel') != panel or not state.get('auto_at'):
        return False
    row = store.db.execute('SELECT session, at FROM terminals WHERE target=? AND session!=? AND at>=? '
                           'ORDER BY at DESC LIMIT 1', (panel, state.get('session') or '', state['auto_at'])).fetchone()
    if not row:
        return False                                     # nothing started in the pane yet
    fresh = row['session']
    bound = store.session_task(fresh)
    if bound and bound['task_id'] and bound['task_id'] != task_id:
        return False                                     # it works on a task of its own: never take that away
    if bound is None:
        from datetime import datetime, timezone
        try:
            first = datetime.fromisoformat(row['at'])
        except ValueError:
            return False
        if (datetime.now(timezone.utc) - first).total_seconds() < grace:
            return False                                 # its own hook may still be deciding
    try:
        with store.lock('task:' + task_id), store.lock('sessions:' + task_id, timeout=SESSION_LOCK_WAIT):
            task = store.get(task_id)
            state = task['data'].get('handoff_state') or {}
            if state.get('state') == 'adopted':
                return True
            if state.get('state') != 'auto' or state.get('panel') != panel:
                return False
            bound = store.session_task(fresh)
            if bound and bound['task_id'] not in (NO_TASK, task_id):
                return False
            state.update(state='adopted', adopted_by=fresh, adopted_from='auto')
            store.save(task, task['status'], 'handoff_adopted', {'session': fresh, 'by': 'typist', 'layer': 'context'})
            store.bind_session(fresh, task_id, (bound or {}).get('workspace') or task['workspace'])
            if state.get('session') and state['session'] != fresh:
                store.end_session(state['session'])
            return True
    except (RuntimeError, ValueError):
        return False                                     # busy this instant: the typist asks again


def held_back(data, session):
    """True when the carried request belongs back to the session it came from: the automatic /clear failed and
    that session ran another turn, so it does the request itself (2026-11-05: the old session did it, and the
    fresh session four minutes later was still told to do it 'now')."""
    state = data.get('handoff_state') or {}
    carried = data.get('carried_prompt') or {}
    return bool(session and session == carried.get('from_session') and state.get('state') == 'auto_failed'
                and state.get('session') == session)


def mark_carried_served(store, task, session):
    """The session has received the carried request in its brief: no other session receives it again."""
    carried = task['data'].get('carried_prompt') or {}
    if not carried.get('text') or not session or carried.get('served_to'):
        return
    if session == carried.get('from_session') and not held_back(task['data'], session):
        return
    try:
        with store.lock('task:' + task['id']):
            current = store.get(task['id'])
            row = current['data'].get('carried_prompt') or {}
            if row.get('at') == carried.get('at') and not row.get('served_to'):
                row['served_to'] = session
                store.save(current, current['status'], 'carried_prompt_served', {'session': session, 'layer': 'context'})
    except (RuntimeError, ValueError):
        pass


def handoff_lines(data, prefix, tid, closed=False):
    """Brief lines about context and continuation; the numbers stay out of the brief digest."""
    lines = []
    live = data.get('context_live') or {}
    state = data.get('handoff_state') or {}
    log = data.get('session_log') or []
    current = live.get('session')
    if not closed and len(log) >= 2 and log[-1].get('session') == current:
        previous = log[-2]
        lines.append('CONTINUED in a fresh context (previous session peaked at %s tokens). Start from the checkpoint; '
                     'open only the files you need; do not repeat completed work.' % _k(previous.get('peak')))
    checkpoint = (data.get('checkpoints') or [None])[-1]
    if checkpoint and not closed:
        line = 'CHECKPOINT (%s · %s tokens · %s): next: %s' % (_local(checkpoint['at']), _k(checkpoint.get('tokens')),
                                                            checkpoint.get('by'), _one(checkpoint['next'], 400))
        if checkpoint.get('notes'):
            line += ' · notes: ' + _one(checkpoint['notes'], 500)
        if checkpoint.get('files'):
            line += ' · files: ' + ', '.join(f['path'] for f in checkpoint['files'][:10])
        lines.append(line)
    if state.get('session') == current and current:
        if state.get('state') in ('recommended', 'asked'):
            lines.append("CONTEXT %s tokens ≥ handoff point %s. At the next natural boundary record a checkpoint: "
                         "%s checkpoint %s --next '…' --notes '…' [--file path]. A fresh session then continues from the ledger "
                         "instead of re-reading this context." % (_k(live.get('tokens')), _k(state.get('limit')), prefix, tid))
        elif state.get('state') == 'auto_failed':
            lines.append('HANDOFF READY, but the automatic /clear did not take effect: ask the user to type /clear; '
                         'the request they made is carried over to the fresh session.')
        elif state.get('state') in ('checkpointed', 'told'):
            lines.append('HANDOFF READY: checkpoint saved. Orbit cannot clear this session itself: the user types /clear '
                         '(Claude Code) or /new (Codex) and the fresh session takes this task over; otherwise the next '
                         'automatic compaction closes the handoff.')
    carried = data.get('carried_prompt') or {}
    if carried.get('text') and not closed and current and current != carried.get('from_session') \
            and carried.get('served_to') in (None, current):
        lines.append('CARRIED OVER (the request the user made just before this fresh context; do it now): '
                     + _one(carried['text'], 1200))
    elif carried.get('text') and not closed and held_back(data, current) and carried.get('served_to') in (None, current):
        lines.append('HELD REQUEST (Orbit held it at %s for a fresh context that did not start; the user did not '
                     'retype it: do it here, now): ' % _local(carried.get('at')) + _one(carried['text'], 1200))
    return lines


def resume_carried(store, task, session, host, terminal=None, spawn=None):
    """A fresh session took the task over after the automatic /clear had failed (the user cleared by hand, or a
    /clear queued in the host ran later). No typist waits for it any more, so it would sit at an empty input line
    with the request in its brief (2026-11-05 11:01: ten minutes, until the user asked what happened). Type the
    carry line once, when its input line is empty. Returns the spawned pid, or None."""
    data = task['data']
    state = data.get('handoff_state') or {}
    carried = data.get('carried_prompt') or {}
    if host != 'claude' or handoff_mode(store) != 'auto' or task['status'] in ('verified', 'stalled', 'interrupted'):
        return None
    if state.get('state') != 'adopted' or state.get('adopted_by') != session or state.get('adopted_from') != 'auto_failed':
        return None
    if not carried.get('text') or carried.get('served_to') not in (None, session) or carried.get('carry_typed_for'):
        return None
    from . import typist
    terminal = typist.detect() if terminal is None else terminal
    if not terminal:
        return None
    try:
        with store.lock('task:' + task['id']):
            current = store.get(task['id'])
            row = current['data'].get('carried_prompt') or {}
            if row.get('at') != carried.get('at') or row.get('carry_typed_for'):
                return None
            row['carry_typed_for'] = session              # once: a second SessionStart must not type it again
            store.save(current, current['status'], 'carry_line_scheduled', {'session': session, 'layer': 'context'})
    except (RuntimeError, ValueError):
        return None
    return (spawn or typist.spawn)(terminal, typist.carry_steps(), store.home / 'hooks.log', home=store.home, task=task['id'])


def context_view(data):
    live = data.get('context_live') or {}
    return {'tokens': live.get('tokens'), 'start': live.get('start'), 'peak': live.get('peak'),
            'limit': live.get('limit'), 'threshold': live.get('threshold'), 'threshold_source': live.get('threshold_source'),
            'session': live.get('session'), 'at': live.get('at'), 'model': live.get('model'), 'window': live.get('window')}


def _k(value):
    if not isinstance(value, (int, float)) or not value:
        return '?'
    return '%dk' % round(value / 1000)


def _one(text, limit):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


def _local(stamp):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone().strftime('%H:%M')
    except (ValueError, TypeError):
        return str(stamp or '')[:16]


# ---------------------------------------------------------------- unattended sessions

def rollover_due(store, task, key):
    """True when the owned executor session should restart from a ledger handoff before the next turn."""
    data = task['data']
    options = data.get('options') or {}
    if options.get('auto_handoff') is False:
        return False
    threshold = options.get('context_budget')
    if threshold is None:
        source = (data.get('development') or {}).get('source', task['workspace'])
        threshold, _ = resolve_threshold(store, source)
    measurement = (data.get('context_measurements') or {}).get(key, {})
    tokens = measurement.get('tokens')
    threshold = window_cap(threshold, measurement.get('window'))
    if not threshold or not isinstance(tokens, int) or not data.get('sessions', {}).get(key):
        return False
    start = (data.get('context_start') or {}).get(key)
    return tokens >= limit_for_session(threshold, start)
