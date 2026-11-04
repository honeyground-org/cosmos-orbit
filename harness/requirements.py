"""Requirements that change while the work runs.

Real work keeps growing: the user adds a case, widens a feature, changes a rule. Each such change must revise
the result goals (completion definitions) and the verification goals (fixed checks) before a completion claim
is reviewed, or the review checks yesterday's task. This module classifies a new user message, records the
pending change, and keeps an append-only revision history. It never calls a model: the executor, which
understands the change, writes the revised definitions; Orbit makes sure that happens and records it.

Orbit decides only the clear cases. A message that names work without asking for it ("README 업데이트?") is
'unsure': the executor, which reads the whole conversation, decides, and its decision becomes a label that
teaches this installation (see `learn`). What one person's phrasing turns out to mean stays in that person's
store; the code holds the mechanism, never anyone's phrases.
"""
import re

from .store import now

MAX_ITEMS = 12
_TRIVIAL = {
    'ok', 'okay', 'k', 'yes', 'yep', 'go', 'go on', 'continue', 'proceed', 'thanks', 'thank you', 'next', 'done',
    '계속', '계속해', '계속해줘', '계속 진행', '계속 진행해', '진행', '진행해', '진행해줘', '진행해 줘', '이어서', '이어서 진행',
    '이어서 진행해', '좋아', '좋아요', '응', '네', '예', '그래', '고마워', '감사', '감사합니다', 'ㅇㅋ', 'ㄱㄱ', '확인', '알겠어',
}


# Scope added with no request verb at all: Korean "PDF도?" / "탭도 되나요?" ("…too?") and "PDF는요?" ("what about…?"),
# English "And PDF?". The word before 도 must be a word plus the particle, not a noun that ends in 도 (정도, 속도 …).
_ALSO_KO = re.compile(r'(\S+)도\s*(?:[?？]|(?:되|돼|가능|있|할|해|같이|포함|넣)[^?？]*[?？])')
_NOT_PARTICLE = {'정도', '속도', '온도', '빈도', '용도', '의도', '시도', '각도', '밀도', '농도', '한도', '고도', '강도', '태도',
                 '제도', '지도', '포도', '적도', '보도', '난이도', '정확도', '만족도', '신뢰도', '완성도', '중요도', '우선도',
                 '인지도', '해상도', '민감도', '선명도', '어느정도'}
# "PDF는?" / "다크 모드는요?" ("what about…?") is scope in a short message; "결과는?" ("the result?") asks for information.
_WHAT_ABOUT_KO = re.compile(r'(\S+?)(?:은|는)(?:요)?\s*[?？]\s*$')
_INFO_NOUNS = {'결과', '이유', '원인', '상태', '상황', '진행', '진행상황', '현황', '속도', '시간', '비용', '가격', '차이', '방법',
               '문제', '에러', '오류', '로그', '출력', '답', '값', '크기', '용량', '버전', '경로', '위치', '이름', '날짜', '기간',
               '순서', '의미', '뜻', '범위', '기준', '근거', '목적', '목표', '계획', '일정', '성능', '정확도', '점수', '개수',
               '뭐', '무엇', '어디', '언제', '누구', '이건', '그건', '저건', '지금', '다음', '나머지', '전체', '총합', '합계'}
_AND_ELLIPSIS = re.compile(r'^and\s+(?!(?:is|are|was|were|do|does|did|has|have|had|can|could|will|would|should|how|what|why|'
                           r'when|where|who|which|whose|whom)\b)[^?]{1,60}\?\s*$')
_SYSTEM = re.compile(r'^\s*<(task-notification|system-reminder|local-command-[a-z-]+|command-name|command-message|command-args|'
                     r'user-prompt-submit-hook|bash-input|bash-stdout|bash-stderr)\b', re.I)


# A go-ahead is any combination of these, ignoring spaces, punctuation and politeness endings.
_GO = sorted({
    'ok', 'okay', 'k', 'yes', 'yep', 'yeah', 'sure', 'go', 'goahead', 'goon', 'on', 'continue', 'proceed', 'next', 'done',
    'soundsgood', 'lgtm', 'thanks', 'thankyou', 'please', 'keepgoing', 'carryon',
    '좋아', '좋아요', '좋습니다', '응', '네', '예', '그래', '그래요', '오케이', 'ㅇㅋ', 'ㄱㄱ', '확인', '알겠어', '알겠습니다',
    '계속', '계속해', '계속해줘', '계속해요', '계속해주세요', '계속진행', '계속진행해', '진행', '진행해', '진행해줘', '진행해요',
    '진행해주세요', '진행부탁해', '진행부탁해요', '진행부탁합니다', '부탁해', '부탁해요', '부탁합니다', '이어서', '이어서진행',
    '이어서진행해', '이어서해', '이어서해줘', '그렇게해', '그렇게해줘', '그렇게해주세요', '고마워', '고마워요', '감사', '감사합니다',
}, key=len, reverse=True)
_GO_ONLY = re.compile('^(?:' + '|'.join(re.escape(w) for w in _GO) + ')+$')


def _compact(text):
    return re.sub(r'[\W_]+', '', text.lower())


def _adds_scope(text):
    lowered = text.lower()
    if _AND_ELLIPSIS.match(lowered):
        return True
    about = _WHAT_ABOUT_KO.search(text)
    if about and len(text.split()) <= 3 and about.group(1) not in _INFO_NOUNS:
        return True
    return any(m.group(1).lstrip('(\'"') + '도' not in _NOT_PARTICLE for m in _ALSO_KO.finditer(text))


# An explicit ask is a request even when the sentence ends like a question ("수정해줄 수 있어?", "Can you add …?").
_ASK = re.compile(
    r'(해\s?줘|해\s?줄|해\s?주세요|해\s?주시|주세요|줄래|줄\s?수|주실|주라|수\s?있을까|수\s?있나|수\s?있어|될까|'
    r'해\s?볼래|해\s?보자|해\s?봐|하자|할래|면\s?어때|게\s?어때|건\s?어때|'
    r"\bcan you\b|\bcould you\b|\bwould you\b|\bwill you\b|\bplease\b|\blet's\b|\bhow about\b|\bwhat about\b|"
    r"\btoo\s*\?|\bas well\b|\binstead\b)")
# Imperative verbs and obligations: a request, unless the sentence ends as a status question ("고쳐졌어?").
_IMPERATIVE = re.compile(
    r'(고쳐(?![졌진져])|고치자|바꿔(?!졌)|바꾸자|넣어(?!졌)|빼\s?줘|빼고|만들어(?![졌진져])|추가\s?해|수정\s?해|업데이트\s?해|'
    r'반영\s?해|적용\s?해|지원\s?해|처리\s?해|해야|하도록|되도록|되게|줘|'
    r"\badd\b|\bmake\b|\bsupport\b|\bchange\b|\bshould\b|\bfix\b|\bupdate\b|\binclude\b|\bremove\b|\bhandle\b|\balso\b)")
# Words that only name work. Without an ask or an imperative the message may be a question about that work.
_WORK = re.compile(r'(수정|업데이트|추가|반영|지원|적용|변경|개선|삭제|구현|고침|implement)')
# Endings that make a Korean sentence a question even without "?" ("…된 것인지").
_ASKING_END = re.compile(r'(인지|는지|은지|건지|나요|습니까|거니|건가|인가요|는가요)$')
# Status questions: about what is or was, not a request for new work.
_STATUS_END = re.compile(r'(인지|는지|은지|건지|던지|거야|거지|거니|거예요|건가|건가요|인가|인가요|는가|나요|습니까|'
                         r'맞아|맞나|맞지|맞죠|맞니|아니야|아닌가|아냐|되나|되니|돼|돼요|됩니까)$')
_PAST_TAIL = {'', '어', '어요', '나', '나요', '니', '지', '죠', '지요', '는지', '는가', '냐', '습니까', '을까', '던가', '네', '군', '구나', '음'}
_EN_STATUS = re.compile(r"^(did|does|do|is|are|am|was|were|has|have|had|isn't|wasn't|didn't|doesn't|hasn't|haven't|aren't|weren't)\b")


def _last_word(text):
    words = re.sub(r'[\s?？!.~。…]+$', '', text).split()
    return words[-1] if words else ''


def _past(word):
    """A Korean past-tense ending: a syllable with the ㅆ final consonant, then at most a short sentence ending."""
    for index in range(len(word) - 1, -1, -1):
        code = ord(word[index]) - 0xAC00
        if 0 <= code < 11172 and code % 28 == 20:
            return word[index + 1:] in _PAST_TAIL
    return False


def _status_question(text):
    if _EN_STATUS.match(text.lower()):
        return True
    word = _last_word(text)
    return bool(word) and (bool(_STATUS_END.search(word)) or _past(word))


def ending_key(text):
    """What this installation learns from: the last word and whether the message asked something."""
    stripped = (text or '').strip()
    return _last_word(stripped).lower()[:24] + ('?' if stripped.endswith(('?', '？')) else '')


def _asking(text):
    return text.endswith(('?', '？')) or bool(_ASKING_END.search(_last_word(text)))


def _question_kind(text):
    """A single sentence that asks something: 'requirement' | 'question' | 'unsure'."""
    lowered = text.lower()
    if _ASK.search(lowered) or _adds_scope(text):
        return 'requirement'
    if _status_question(text):
        return 'question'      # "잘 수정된 것인지?", "Did you update the README?"
    if _IMPERATIVE.search(lowered):
        return 'requirement'
    return 'unsure' if _WORK.search(lowered) else 'question'


# Sentence boundaries: end punctuation followed by space, or a line break. "~/.claude" and "1.5" are not boundaries.
_SENTENCE = re.compile(r'(?<=[.!?？。])\s+|\n+')
# Orders that work in a plain statement too ("…해 주세요.", "…하자."). Question-shaped asks ("수 있어", "can you")
# are left out on purpose: in a statement they describe ("이런 분석을 확인할 수 있어." = "you can see this analysis").
_ORDER = re.compile(r"(주세요|주시|하자|해\s?봐|해\s?보자|\bplease\b|\blet's\b|\binstead\b)")


def _context_kind(sentence):
    """A statement that precedes the final question: context, unless it gives an order or names work."""
    lowered = sentence.lower()
    if _IMPERATIVE.search(lowered) or _ORDER.search(lowered):
        return 'requirement'
    return 'unsure' if _WORK.search(lowered) else 'question'


def classify(text, learned=None):
    """'system' | 'trivial' | 'question' | 'requirement' | 'unsure'.

    Only a person's words can set or change a goal: host notifications arrive through the same hook and are
    ignored. A question or a go-ahead does not change what must be delivered. 'unsure' goes to the executor;
    `learned` ({ending: {'change': n, 'no_change': m}}) resolves it once this installation has seen how the
    executor decided the same ending before.

    A message of several sentences that ends in a question is judged sentence by sentence: what is asked is the
    final question, and the statements before it are context unless they themselves give an order or name work.
    Matching the whole message at once let a describing clause in the context ("…확인할 수 있어.") read as an ask."""
    stripped = (text or '').strip()
    if _SYSTEM.match(stripped):
        return 'system'
    from .typist import CARRY_LINE
    from .wake import WAKE_LINE
    if stripped == CARRY_LINE:
        return 'trivial'      # typed by Orbit's auto handoff: the request itself is already in the ledger
    if stripped == WAKE_LINE:
        return 'trivial'      # typed by Orbit for a message from another session: not the user's words
    bare = re.sub(r'[\s.!~。…]+$', '', stripped.lower()).strip()
    if not bare or bare in _TRIVIAL or _GO_ONLY.match(_compact(stripped) or ' '):
        return 'trivial'
    if not _asking(stripped):
        return 'requirement'  # a statement that reaches the agent is an instruction; a missed change costs more
    parts = [part.strip() for part in _SENTENCE.split(stripped) if part and part.strip()]
    if len(parts) < 2:
        kinds = [_question_kind(stripped)]
    else:
        kinds = [_question_kind(part) if _asking(part) else _context_kind(part) for part in parts]
    if 'requirement' in kinds:
        return 'requirement'
    if 'unsure' not in kinds:
        return 'question'
    counts = (learned or {}).get(ending_key(stripped)) or {}
    change, no_change = counts.get('change', 0), counts.get('no_change', 0)
    if change + no_change >= 2 and not no_change:
        return 'requirement'
    if change + no_change >= 2 and not change:
        return 'question'
    return 'unsure'


def learn(learned, text, label):
    """Count how the executor decided an 'unsure' message (label 'change' or 'no_change'). Returns the table."""
    if label not in ('change', 'no_change'):
        raise ValueError('label must be change or no_change')
    key = ending_key(text)
    if not key:
        return learned
    row = learned.setdefault(key, {'change': 0, 'no_change': 0})
    row[label] = row.get(label, 0) + 1
    if len(learned) > 500:  # a bounded table: drop the least-used endings
        for stale in sorted(learned, key=lambda k: sum(learned[k].values()))[:len(learned) - 500]:
            learned.pop(stale, None)
    return learned


def mark_pending(data, text, source, unsure=False):
    previous = data.get('requirements_pending') or {}
    data['requirements_pending'] = {'at': now(), 'text': (text or '').strip()[:600], 'source': source,
                                    'count': (previous.get('count') or 0) + 1, 'rev': data.get('done_rev') or 0,
                                    'unsure': bool(unsure) and (not previous or bool(previous.get('unsure')))}
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
    if pending.get('unsure'):
        return ('POSSIBLE CHANGE (%s, after rev %s): "%s". Orbit is unsure this changes what must be delivered. If it does: '
                "%s done %s --add 'D… what it requires'; if it does not (a question): %s done %s --keep. Needed before claiming."
                % (_hhmm(pending['at']), pending.get('rev') or 0, re.sub(r'\s+', ' ', pending['text'])[:300], prefix, tid, prefix, tid))
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
