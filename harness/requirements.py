"""Requirements that change while the work runs.

Real work keeps growing: the user adds a case, widens a feature, changes a rule. Each such change must revise
the result goals (completion definitions) and the verification goals (fixed checks) before a completion claim
is reviewed, or the review checks yesterday's task. This module classifies a new user message, records the
pending change, and keeps an append-only revision history. It never calls a model: the executor, which
understands the change, writes the revised definitions; Orbit makes sure that happens and records it.
"""
import re

from .store import now

MAX_ITEMS = 12
_TRIVIAL = {
    'ok', 'okay', 'k', 'yes', 'yep', 'go', 'go on', 'continue', 'proceed', 'thanks', 'thank you', 'next', 'done',
    '계속', '계속해', '계속해줘', '계속 진행', '계속 진행해', '진행', '진행해', '진행해줘', '진행해 줘', '이어서', '이어서 진행',
    '이어서 진행해', '좋아', '좋아요', '응', '네', '예', '그래', '고마워', '감사', '감사합니다', 'ㅇㅋ', 'ㄱㄱ', '확인', '알겠어',
}


_REQUEST = re.compile(
    r'(해\s?줘|해\s?줄|해\s?주|주세요|줄래|줄\s?수|바꿔|바꾸|추가|넣어|빼\s?줘|빼고|지원|고쳐|고치|만들어|수정|업데이트|반영|'
    r'해야|하자|할래|해\s?볼래|되게|되도록|하도록|처리해|적용|'
    r"\bcan you\b|\bcould you\b|\bwould you\b|\bwill you\b|\bplease\b|\balso\b|\badd\b|\bmake\b|\bsupport\b|"
    r"\bchange\b|\bshould\b|\blet's\b|\bfix\b|\bupdate\b|\binclude\b|\bremove\b|\bhandle\b|\binstead\b)")
_SYSTEM = re.compile(r'^\s*<(task-notification|system-reminder|local-command-[a-z-]+|command-name|command-message|command-args|'
                     r'user-prompt-submit-hook|bash-input|bash-stdout|bash-stderr)\b', re.I)


def classify(text):
    """'system' | 'requirement' | 'question' | 'trivial'. Only a person's words can set or change a goal:
    host notifications (background tasks, monitors, slash-command echoes) arrive through the same hook and are
    ignored. A question or a go-ahead does not change what must be delivered."""
    stripped = (text or '').strip()
    if _SYSTEM.match(stripped):
        return 'system'
    bare = re.sub(r'[\s.!~。…]+$', '', stripped.lower()).strip()
    if not bare or bare in _TRIVIAL:
        return 'trivial'
    if stripped.endswith(('?', '？')) and not _REQUEST.search(stripped.lower()):
        return 'question'  # an information question; a request phrased as a question falls through
    return 'requirement'  # when unsure, count it: a missed change costs more than one `done --keep`


def mark_pending(data, text, source):
    previous = data.get('requirements_pending') or {}
    data['requirements_pending'] = {'at': now(), 'text': (text or '').strip()[:600], 'source': source,
                                    'count': (previous.get('count') or 0) + 1, 'rev': data.get('done_rev') or 0}
    return data['requirements_pending']


def current_texts(data):
    return [d['text'] for d in data.get('done') or [] if d['id'].startswith('D')]


def revise(data, texts=(), origin='executor', add=False, keep=False, checks=(), because=None):
    """Write a new revision of the result goals (D items) and verification goals (fixed checks).

    add: append to the current items · keep: confirm they still cover the request · checks: argv lists appended
    to the fixed checks (never replaced or removed). Returns the history row."""
    from .simple_loop import completion_items
    texts = [t.strip() for t in texts if t and t.strip()]
    if data.get('done_fixed') and not data.get('done_rev'):
        # Definitions written before revisions were recorded count as rev 1.
        data['done_rev'] = 1
        data.setdefault('done_history', []).insert(0, {
            'rev': 1, 'at': data['done_fixed'].get('at'), 'origin': data['done_fixed'].get('origin'), 'turn': data['done_fixed'].get('turn', 0),
            'because': 'initial (recorded before revisions were tracked)', 'kept': False,
            'added': [d['id'] for d in data.get('done') or [] if d['id'].startswith('D')], 'removed': [], 'checks_added': [],
            'items': len(current_texts(data)), 'checks': len(data['development']['checks'])})
    if not texts and not keep and not checks:
        raise ValueError('Give completion definitions, --keep, or --check')
    existing = [d for d in data.get('done') or [] if d['id'].startswith('D')]
    fixed = data.get('done_fixed') or {}
    if texts and not add and fixed.get('origin') == 'user' and origin != 'user' and existing:
        raise ValueError('The user fixed these completion definitions; extend them with --add (or the user replaces them with --done)')
    commands = list(data['development']['checks'])
    added_checks = []
    for argv in checks:
        if argv not in commands:
            commands.append(argv)
            added_checks.append(argv)
    if len(commands) > 12:
        raise ValueError('At most 12 fixed checks are supported')
    if add or (keep and not texts):
        known = {re.sub(r'\s+', ' ', t).lower() for t in current_texts(data)}
        fresh = [t for t in texts if re.sub(r'\s+', ' ', t).lower() not in known]
        final = current_texts(data) + fresh
        origins = [d.get('origin', origin) for d in existing] + [origin] * len(fresh)
    else:
        final, origins = texts, [origin] * len(texts)
    if len(final) > MAX_ITEMS:
        raise ValueError('At most %d completion definitions; merge items that test the same behaviour' % MAX_ITEMS)
    items = completion_items(final, commands, origin)
    for item, item_origin in zip([i for i in items if i['id'].startswith('D')], origins):
        item['origin'] = item_origin
    before = {d['id']: d['text'] for d in existing}
    after = {d['id']: d['text'] for d in items if d['id'].startswith('D')}
    revision = (data.get('done_rev') or 0) + 1
    pending = data.get('requirements_pending') or {}
    row = {'rev': revision, 'at': now(), 'origin': origin, 'turn': data.get('turns', 0),
           'because': (because or pending.get('text') or ('initial' if revision == 1 else 'revised'))[:600],
           'kept': bool(keep and not texts and not added_checks),
           'added': [i for i, t in after.items() if before.get(i) != t],
           'removed': [t[:200] for i, t in before.items() if i not in after or (not add and t not in after.values())],
           'checks_added': added_checks, 'items': len(after), 'checks': len(commands)}
    data['development']['checks'] = commands
    data['done'] = items
    data['done_rev'] = revision
    data['done_fixed'] = {'at': row['at'], 'origin': origin if not row['kept'] else fixed.get('origin', origin),
                          'turn': data.get('turns', 0), 'rev': revision}
    history = data.setdefault('done_history', [])
    history.append(row)
    del history[:-30]
    data.pop('requirements_pending', None)
    data.pop('done_stale', None)
    return row


def pending_line(data, prefix, tid):
    pending = data.get('requirements_pending')
    if not pending:
        return None
    return ('REQUIREMENTS CHANGED (%s, after rev %s): "%s". Revise the result and verification goals before claiming: '
            "%s done %s --add 'D… what the change requires' [--check '[\"python3\",\"-m\",\"pytest\",\"…\"]'] "
            "(or the same command with your rewritten items 'D1 …' 'D2 …', or with --keep if they already cover it)"
            % (_hhmm(pending['at']), pending.get('rev') or 0, re.sub(r'\s+', ' ', pending['text'])[:300], prefix, tid))


def _hhmm(stamp):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone().strftime('%H:%M')
    except (ValueError, TypeError):
        return str(stamp or '')[:16]
