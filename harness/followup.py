"""Follow-up: the technical background of a stop, one line first, a card only when asked.

Where it comes from (Sean, 2026-11-08): working with Claude and Codex, a person can miss what an explanation means —
the field is new, or it was said in a hard way — and then decides on a guess (2026-08-28: "I don't know what 'lock'
and 'contract' mean; I've been imagining it"). So when a task stops to ask the user, or a review verifies it, the
executor names the concepts the moment rests on, and Orbit shows them as one line. A card (three sentences: what it
is, where it is in this work, what it changes for the decision) comes only when the user asks for it.

Rules settled with Astra (docs/cosmos-orbit/PLAN_FOLLOWUP_2026-11-08.md §6):
- Asking for background never moves the task. A follow-up prompt is answered by the hook itself and blocked, so it
  is neither the answer to the pending question nor a new requirement, and no turn runs.
- Only one line shows by itself, in every mode, at most three concepts, once per stop.
- Orbit never guesses that the user knows a concept. "이건 알아 N" / "known N" hides its basics until restored.
- Modes: off (nothing by itself; asking still works) · auto (default: concepts not hidden; none → silent) ·
  always (hidden ones too, as "how this work used it").
"""
import json
import re

from .store import now

MODES = ('off', 'auto', 'always')
MODE_KEY = 'followup.mode'
HIDDEN_KEY = 'followup.hidden'
MAX_SHOWN = 3
MAX_CONCEPTS = 5
FIELD_LIMIT = 160

_WORD = r'(?:팔로업|followup|follow-up)'
_SHOW = re.compile(r'^\s*' + _WORD + r'\s*(\d+)?\s*[.!?]?\s*$', re.I)
_KNOWN = re.compile(r'^\s*(?:이건\s*알아|알아|known|i know(?: this)?)\s*(\d+)\s*[.!?]?\s*$', re.I)
_RESTORE = re.compile(r'^\s*' + _WORD + r'\s*(?:복원|restore)\s*(\d+|.+)?\s*$', re.I)
_MODE = re.compile(r'^\s*' + _WORD + r'\s*(끄기|꺼|off|켜기|켜|on|자동|auto|항상|always)\s*$', re.I)
_SOURCE = re.compile(r'^\s*' + _WORD + r'\s*(\d+)\s*(?:근거|source)\s*$', re.I)
_CLOSE = re.compile(r'^\s*(?:닫기|close)\s*$', re.I)
_MODE_WORDS = {'끄기': 'off', '꺼': 'off', 'off': 'off', '켜기': 'auto', '켜': 'auto', 'on': 'auto',
               '자동': 'auto', 'auto': 'auto', '항상': 'always', 'always': 'always'}


def korean(task):
    return bool(re.search(r'[가-힣]', task.get('prompt') or ''))


def _norm(name):
    return re.sub(r'\s+', ' ', name).strip().lower()


def parse_concept(raw):
    """'name | plain words | where in this work | what it changes' → a concept (the last two may be empty)."""
    parts = [p.strip() for p in str(raw or '').split('|')]
    if not parts or not parts[0] or len(parts) < 2 or not parts[1]:
        raise ValueError("A concept is 'name | what it is, in plain words | where in this work | what it changes for "
                         "the decision' (name and plain words required)")
    if len(parts) > 4:
        raise ValueError('A concept has at most four parts separated by |')
    parts += [''] * (4 - len(parts))
    return {'name': _one_line(parts[0], 60), 'plain': _one_line(parts[1], FIELD_LIMIT), 'here': _one_line(parts[2], FIELD_LIMIT),
            'impact': _one_line(parts[3], FIELD_LIMIT)}


def _one_line(text, limit):
    """One line, one sentence's worth: a card is three sentences, so a field never brings a paragraph."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


_PATH = re.compile(r'(?:~|\.{0,2})?/?(?:[\w.-]+/){2,}[\w.-]+')


def _short_paths(text):
    """A long path reads as noise in a card: show its last part; the whole path stays for '근거'/'source'."""
    return _PATH.sub(lambda m: '…/' + m.group(0).rstrip('/').rsplit('/', 1)[-1], text)


def mode(store, task=None):
    own = ((task or {}).get('data') or {}).get('followup', {}).get('mode') if task else None
    if own in MODES:
        return own
    value = store.preference(MODE_KEY)
    return value if value in MODES else 'auto'


def set_mode(store, value, task=None):
    """Everywhere by default; with a task, for that task only (it wins over the default)."""
    if value not in MODES:
        raise ValueError('Follow-up mode is one of: ' + ', '.join(MODES))
    if task is None:
        store.preference(MODE_KEY, value)
        return {'scope': 'default', 'mode': value}
    with store.lock('task:' + task['id']):
        current = store.get(task['id'])
        current['data'].setdefault('followup', {})['mode'] = value
        store.save(current, current['status'], 'followup_mode', {'mode': value, 'layer': 'followup'})
    return {'scope': 'task', 'task': task['id'], 'mode': value}


def hidden(store):
    try:
        value = json.loads(store.preference(HIDDEN_KEY) or '[]')
    except ValueError:
        value = []
    return [v for v in value if isinstance(v, str)]


def _set_hidden(store, names):
    store.preference(HIDDEN_KEY, json.dumps(sorted(set(names)), ensure_ascii=False))


def record(data, concepts, moment, about=''):
    """Attach the executor's concepts to the stop they belong to (data is saved by the caller, under its lock).

    Every claim moves `latest`, with or without concepts: a new question never shows the last one's background.
    moment: 'question' (the claim asked the user) or 'completion' (shown once the review verifies). Card numbers
    only grow, so "팔로업 61" always means one card even after old ones are dropped."""
    if len(concepts or ()) > MAX_CONCEPTS:
        raise ValueError('At most %d concepts per stop' % MAX_CONCEPTS)
    state = data.get('followup')
    if not concepts and not state:
        return
    state = data.setdefault('followup', {})
    cards = state.setdefault('cards', [])
    key = '%s:%s' % (moment, data.get('turns', 0))
    state['latest'] = key
    state['about'] = _one_line(about, 120)
    state.pop('open', None)
    for concept in concepts or ():
        state['next'] = state.get('next') or (max([c.get('n', 0) for c in cards] or [0]) + 1)
        cards.append(dict(concept, n=state['next'], moment=moment, key=key, about=state['about'], at=now()))
        state['next'] += 1
    del cards[:-60]


def _moment_ready(task, key):
    moment = key.split(':', 1)[0]
    if moment == 'question':
        return task['status'] == 'needs_input'
    return task['status'] == 'verified'


def _latest_cards(task):
    state = task['data'].get('followup') or {}
    key = state.get('latest')
    return key, [c for c in state.get('cards') or [] if c.get('key') == key]


def stop_line(store, task):
    """The one line a stop shows by itself, or None. Once per stop; never in off; auto leaves hidden concepts out."""
    key, cards = _latest_cards(task)
    state = task['data'].get('followup') or {}
    if not key or not cards or key in (state.get('announced') or []) or not _moment_ready(task, key):
        return None
    chosen = mode(store, task)
    if chosen == 'off':
        return None
    known = set(hidden(store))
    if chosen == 'auto':
        cards = [c for c in cards if _norm(c['name']) not in known]
    if not cards:
        return None
    ko = korean(task)
    numbers = ' '.join('%s %s' % (_circled(c['n']), c['name']) for c in cards[:MAX_SHOWN])
    more = (' +%d' % (len(cards) - MAX_SHOWN)) if len(cards) > MAX_SHOWN else ''
    if ko:
        return '배경 · %s%s — "팔로업 %d"' % (numbers, more, cards[0]['n'])
    return 'Background · %s%s — "followup %d"' % (numbers, more, cards[0]['n'])


def _last_prompt(data):
    prompts = data.get('prompts') or []
    return prompts[-1].get('at') if prompts else None


def mark_announced(store, task, key=None):
    key = key or (task['data'].get('followup') or {}).get('latest')
    if not key:
        return
    with store.lock('task:' + task['id']):
        current = store.get(task['id'])
        state = current['data'].setdefault('followup', {})
        announced = state.setdefault('announced', [])
        if key not in announced:
            announced.append(key)
            del announced[:-30]
        state['open'] = {'after': _last_prompt(current['data'])}
        store.save(current, current['status'], 'followup_announced', {'key': key, 'layer': 'followup'})


def _mark_open(store, task, value):
    """Background is open on screen until the user's next own words (or a close). Only then is a bare 닫기/close ours."""
    with store.lock('task:' + task['id']):
        current = store.get(task['id'])
        state = current['data'].setdefault('followup', {})
        if value:
            state['open'] = {'after': _last_prompt(current['data'])}
        else:
            state.pop('open', None)
        store.save(current, current['status'], 'followup_open' if value else 'followup_closed', {'layer': 'followup'})


def is_open(task):
    state = (task['data'].get('followup') or {}).get('open')
    return bool(state) and state.get('after') == _last_prompt(task['data'])


def _circled(n):
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else '(%d)' % n


def card(task, n):
    ko = korean(task)
    found = [c for c in (task['data'].get('followup') or {}).get('cards') or [] if c.get('n') == n]
    if not found:
        return ('팔로업 %d번 카드가 없습니다. "팔로업"으로 목록을 보세요.' % n) if ko else \
            ('There is no follow-up card %d. Type "followup" for the list.' % n)
    c = found[-1]
    lines = ['%s %s' % (_circled(c['n']), c['name']),
             ('뜻: ' if ko else 'What: ') + c['plain']]
    if c.get('here'):
        lines.append(('이번 작업: ' if ko else 'In this work: ') + _short_paths(c['here']))
    if c.get('impact'):
        lines.append(('영향: ' if ko else 'What it changes: ') + c['impact'])
    lines.append('')
    has_source = c.get('here') and _short_paths(c['here']) != c['here']
    lines.append((('이건 알아 %d · 닫기' + (' · 팔로업 %d 근거' if has_source else '')) % ((c['n'],) * (2 if has_source else 1))) if ko
                 else (('known %d · close' + (' · followup %d source' if has_source else '')) % ((c['n'],) * (2 if has_source else 1))))
    lines.append(_still_waiting(task, ko))
    return '\n'.join(l for l in lines if l is not None)


def listing(store, task):
    ko = korean(task)
    key, cards = _latest_cards(task)
    if not cards:
        return ('이 작업에는 아직 배경 카드가 없습니다. 모드: %s' % mode(store, task)) if ko else \
            ('No background cards on this task yet. Mode: %s' % mode(store, task))
    known = set(hidden(store))
    rows = ['%s %s%s' % (_circled(c['n']), c['name'], (' (숨김)' if ko else ' (hidden)') if _norm(c['name']) in known else '')
            for c in cards]
    head = ('배경 카드 — "팔로업 N" · 모드 %s' if ko else 'Background cards — "followup N" · mode %s') % mode(store, task)
    about = (task['data'].get('followup') or {}).get('about')
    if about:
        head += '\n' + ('이 멈춤: ' if ko else 'This stop: ') + about
    return '\n'.join([head] + rows + [_still_waiting(task, ko)])


def _still_waiting(task, ko):
    if task['status'] == 'needs_input':
        return '기다리는 질문은 그대로입니다 — 답은 평소처럼 입력하세요.' if ko else \
            'The question is still waiting — answer it as usual.'
    return '작업 상태는 바뀌지 않았습니다.' if ko else 'Nothing about the task changed.'


def hide(store, task, n):
    found = [c for c in (task['data'].get('followup') or {}).get('cards') or [] if c.get('n') == n]
    ko = korean(task)
    if not found:
        return ('%d번 카드가 없습니다.' % n) if ko else ('There is no card %d.' % n)
    name = found[-1]['name']
    _set_hidden(store, hidden(store) + [_norm(name)])
    return (('"%s"의 기초 설명을 숨겼습니다(자동 모드). 되돌리기: 팔로업 복원 %d\n' % (name, n)) if ko else
            ('Basics of "%s" hidden (auto mode). Undo: followup restore %d\n' % (name, n))) + _still_waiting(task, ko)


def restore(store, task, which):
    ko = korean(task)
    names = hidden(store)
    if not which:
        return (('숨긴 개념: ' + (', '.join(names) or '없음')) if ko else ('Hidden: ' + (', '.join(names) or 'none')))
    if which.isdigit():
        found = [c for c in (task['data'].get('followup') or {}).get('cards') or [] if c.get('n') == int(which)]
        target = _norm(found[-1]['name']) if found else None
    else:
        target = _norm(which)
    if target not in names:
        return ('숨긴 개념이 아닙니다.' if ko else 'That concept is not hidden.')
    _set_hidden(store, [n for n in names if n != target])
    return ('"%s"을(를) 다시 보입니다.' % target) if ko else ('"%s" shows again.' % target)


def intercept(store, task, text):
    """The reply to a follow-up prompt, or None when the prompt is not one (it then goes on as the user's words).

    Only "팔로업 …" / "followup …" / "이건 알아 N" always count; a bare "닫기"/"close" counts only right after this
    task showed background, so a person closing something else is not swallowed."""
    if task is None:
        return None
    m = _MODE.match(text)
    if m:
        value = _MODE_WORDS[m.group(1).lower()]
        set_mode(store, value)
        ko = korean(task)
        return (('팔로업 모드: %s (전체 기본값). 이 작업만: cosmos-orbit followup --mode %s --here --task %s' % (value, value, task['id']))
                if ko else ('Follow-up mode: %s (default everywhere). This task only: cosmos-orbit followup --mode %s --here --task %s'
                            % (value, value, task['id'])))
    m = _RESTORE.match(text)
    if m:
        return restore(store, task, (m.group(1) or '').strip())
    m = _SOURCE.match(text)
    if m:
        _mark_open(store, task, True)
        return source(task, int(m.group(1)))
    m = _SHOW.match(text)
    if m:
        _mark_open(store, task, True)
        return card(store.get(task['id']), int(m.group(1))) if m.group(1) else listing(store, store.get(task['id']))
    m = _KNOWN.match(text)
    if m and (task['data'].get('followup') or {}).get('cards'):
        return hide(store, task, int(m.group(1)))
    if _CLOSE.match(text) and is_open(task):
        _mark_open(store, task, False)
        ko = korean(task)
        return ('닫았습니다. ' if ko else 'Closed. ') + _still_waiting(task, ko)
    return None


def source(task, n):
    ko = korean(task)
    found = [c for c in (task['data'].get('followup') or {}).get('cards') or [] if c.get('n') == n]
    if not found:
        return ('%d번 카드가 없습니다.' % n) if ko else ('There is no card %d.' % n)
    c = found[-1]
    return '\n'.join([('%s %s — 근거' if ko else '%s %s — source') % (_circled(c['n']), c['name']),
                      c.get('here') or ('(없음)' if ko else '(none)'),
                      ('이 멈춤: ' if ko else 'This stop: ') + (c.get('about') or ''), _still_waiting(task, ko)])
