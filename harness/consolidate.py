"""Handoff summary: rewritten at every checkpoint from the previous one plus what is new since.

A fresh session used to get the first request, the last four corrections and the last checkpoint, cut longest line
first when the brief was full. On a long task (10 checkpoints, 25-58 corrections) that keeps the oldest line (the
first request) and the newest lines, and loses what lies between: the decision taken on day one that still holds,
the result of the subject finished yesterday, the fact that the conversation changed subject at all.

The summary has three bounded sections:
  now     what matters at this moment: the current goal, what remains, the next step
  keep    what stays important across the whole task regardless of age: decisions, rules, constraints
  shifts  where the conversation changed subject: when, from what to what; an earlier subject as 'goal -> result'
Everything else stays in the ledger only (status, handoff --write) and is looked up when needed.

The session writes it (checkpoint --now/--keep/--shift). Orbit checks it without a model call: each section is
bounded, a keep item of the previous summary that the new one silently lost is carried over (and named) unless the
session retires it, and prompts with little in common with the ones before them are offered as shift candidates.
When the session writes none, Orbit builds one from the ledger by rule.
"""
import re

NOW_LIMIT = 600
KEEP_ITEM = 220
KEEP_MAX = 8
SHIFT_ITEM = 220
SHIFT_MAX = 4
SAME_ITEM = 0.6          # overlap at which two keep items say the same thing
SHIFT_OVERLAP = 0.10     # under this share of its content seen in the prompts before it, a prompt starts a new subject
                         # (2026-11-16, 7 real long tasks: changes of subject 0.00-0.09, continuing requests 0.13-0.54)
SHIFT_WINDOW = 3         # prompts before a candidate it is compared with
SHIFT_MIN_CHARS = 25     # a prompt with fewer letters than this is not judged as a subject change
MIN_PROMPT = 12          # shorter prompts ('다시 시작해', 'ok') carry no subject
CONTINUATIONS = ('Continue with the request Orbit carried over', 'Continue the unfinished task')
# Words that mark a prohibition in the user's own corrections (Korean and English). A rule-built summary keeps such
# corrections in `keep`, the oldest first: what is recent is already in `now` and in the corrections listed since
# the summary; what is old survives only here. Requirements ('해야', 'must') are not taken: measured 2026-11-16 on
# 7 long tasks, they were 1-8 per task and mostly the request of the moment; prohibitions were 0-2 and mostly lasting
# ('다른 기능들에 영향을 주면 안돼'). The session's own summary decides for itself.
PROHIBITIONS = ('⛔', '금지', '절대', '하지 마', '하지마', '하지 말', '쓰지 마', '쓰지마', '안 된다', '안된다',
                '안 돼', '안돼', '안 됨', '안됨', 'never', "don't", 'do not', 'must not')
STOPWORDS = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'not', 'but', 'are', 'was', 'were', 'has',
             'have', 'had', 'its', 'also', 'when', 'then', 'than', 'them', 'they', 'you', 'your', 'our', 'can', 'will',
             'would', 'should', 'could', 'just', 'only', 'more', 'again', 'all', 'any', 'one', 'two', 'please', 'now',
             'too', 'there', 'their', 'what', 'which', 'who', 'how', 'does', 'did', 'out', 'about', 'after', 'before'}


def one(text, limit):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _grams(text):
    """What a text is about, without a tokenizer: Latin words (3+ letters, common words left out) and syllable pairs
    of Korean words. Letter pairs would not do for English: the common ones occur in any two sentences."""
    text = str(text or '').lower()
    out = {w for w in re.findall(r'[a-z0-9][a-z0-9_#./-]*[a-z0-9]', text) if len(w) >= 3 and w not in STOPWORDS}
    for run in re.findall(r'[가-힣]+', text):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def overlap(a, b):
    """Shared terms over the smaller side: 1.0 when one text is contained in the other (two keep items alike)."""
    ga, gb = (a if isinstance(a, set) else _grams(a)), (b if isinstance(b, set) else _grams(b))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def share(new, before):
    """How much of `new` was already talked about in `before`: the part of its content that appeared there. The
    denominator is the new text, so a short history does not make every new request look like more of the same."""
    gn, gb = (new if isinstance(new, set) else _grams(new)), (before if isinstance(before, set) else _grams(before))
    return len(gn & gb) / len(gn) if gn else 1.0


def substantive(text):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return len(text) >= MIN_PROMPT and not text.startswith(CONTINUATIONS)


def _stamp(at):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(at).replace('Z', '+00:00')).astimezone().strftime('%m-%d %H:%M')
    except (ValueError, TypeError):
        return str(at or '')[:16]


def previous(data):
    """The summary of the latest checkpoint that carries one."""
    for row in reversed(data.get('checkpoints') or []):
        if row.get('summary'):
            return row['summary']
    return None


def _words(data, task=None):
    """The user's own requests in order: the original request, then every substantive prompt."""
    rows = []
    goal = data.get('goal') or (task or {}).get('prompt')
    first = (data.get('prompts') or [{}])[0]
    if goal:
        rows.append({'at': data.get('attached_at') or first.get('at') or '', 'text': goal})
    for p in data.get('prompts') or []:
        if substantive(p.get('text')) and not (rows and overlap(p['text'], rows[-1]['text']) >= 0.95
                                                 and len(_grams(p['text'])) <= len(_grams(rows[-1]['text'])) + 2):
            rows.append({'at': p.get('at') or '', 'text': p['text']})
    return rows


def shift_points(data, task=None):
    """Where the subject changed, model-free: a prompt that shares little with the prompts just before it, an
    objective that was rewritten, or a verified task that a new request reopened. Returns rows in time order."""
    words = _words(data, task)
    points = []
    for i in range(1, len(words)):
        grams = _grams(words[i]['text'])
        if len(re.sub(r'\W', '', words[i]['text'])) < SHIFT_MIN_CHARS:  # 'what are you doing?' is too little to judge
            continue
        window = set().union(*(_grams(w['text']) for w in words[max(0, i - SHIFT_WINDOW):i]))
        score = share(grams, window)
        if score >= SHIFT_OVERLAP:
            continue
        if i + 1 < len(words):                             # a new subject persists: the next request does not go back
            following = _grams(words[i + 1]['text'])
            back = share(following, window)
            if back >= SHIFT_OVERLAP and back > share(following, grams):
                continue
        if points and points[-1].get('index') == i - 1:   # two in a row: the first subject did not last
            points[-1].update(at=words[i]['at'], to=words[i]['text'], index=i)
            continue
        points.append({'at': words[i]['at'], 'to': words[i]['text'], 'from': words[i - 1]['text'], 'index': i,
                       'why': 'overlap %.2f with the prompts before it' % score})
    for row in data.get('objective_history') or []:
        if row.get('previous'):
            points.append({'at': row['at'], 'to': row['text'], 'from': row['previous'], 'why': 'objective rewritten'})
    for row in data.get('reopens') or []:
        if row.get('text') and not any(abs(_seconds(p['at']) - _seconds(row['at'])) < 120 for p in points):
            before = [w for w in words if w['at'] < row['at']]
            points.append({'at': row['at'], 'to': row['text'], 'from': before[-1]['text'] if before else '',
                           'why': 'reopened after %s' % row.get('from')})
    points.sort(key=lambda p: p['at'])
    return points


def _seconds(at):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(at).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _opened_by(data, stamp):
    """Item IDs that existed in the checklist at `stamp` (from the revision history)."""
    ids = set()
    for rev in data.get('done_history') or []:
        if rev.get('at', '') <= stamp:
            ids.update(rev.get('added') or [])
    return ids


def _result(data, start, end):
    """What a finished subject produced: the items opened during it and how they closed."""
    opened = _opened_by(data, end) - _opened_by(data, start)
    closed = {d['id']: d for d in data.get('done_closed') or []}
    open_ids = {d['id'] for d in data.get('done') or []}
    parts = []
    met = [i for i in sorted(opened, key=_num) if closed.get(i, {}).get('state') == 'met']
    gone = [i for i in sorted(opened, key=_num) if closed.get(i, {}).get('state') in ('dropped', 'superseded')]
    still = [i for i in sorted(opened, key=_num) if i in open_ids]
    if met:
        evidence = (closed[met[-1]].get('evidence') or [''])[0]
        parts.append('met ' + _ids(met) + (' (' + one(evidence, 90) + ')' if evidence else ''))
    if gone:
        parts.append('withdrawn ' + _ids(gone))
    if still:
        parts.append('still open ' + _ids(still))
    return '; '.join(parts) or 'no item recorded'


def _ids(ids, most=8):
    return ' '.join(ids[:most]) + (' (+%d)' % (len(ids) - most) if len(ids) > most else '')


def _num(item_id):
    digits = re.sub(r'\D', '', item_id or '')
    return int(digits) if digits else 0


def shift_line(data, point, start, subject):
    """'when → the new subject (before: the earlier subject → its result)'."""
    return one('%s → "%s" (before: "%s" → %s)' % (
        _stamp(point['at']), one(point['to'], 60), one(subject, 50), _result(data, start, point['at'])), SHIFT_ITEM)


def _prohibition(text):
    """Korean markers as written; Latin ones as whole words ('whenever' is not 'never')."""
    lowered = str(text or '').lower()
    return any(re.search(r'(?<![a-z])' + re.escape(marker) + r'(?![a-z])', lowered) if marker.isascii() else marker in lowered
               for marker in PROHIBITIONS)


def new_since(data, summary):
    """What the ledger gained after `summary` was written: requests, corrections, closed items with their result
    and verdicts. With no summary: everything."""
    at = (summary or {}).get('at') or ''
    seen = (summary or {}).get('verdicts_seen') or 0
    prompts = [p for p in data.get('prompts') or [] if p.get('at', '') > at and substantive(p.get('text'))]
    feedback = [f for f in data.get('feedback') or [] if f.get('at', '') > at]
    closed = [d for d in data.get('done_closed') or [] if d.get('at', '') > at]
    verdicts = (data.get('verdicts') or [])[seen:]
    return {'prompts': prompts, 'feedback': feedback, 'closed': closed, 'verdicts': verdicts}


def _now_from_ledger(data, task):
    """Open item IDs, the work the last claim left and the latest request. Not the items' text (the DONE line has it)
    nor the next step (the CHECKPOINT line has it): measured 2026-11-16, repeating both filled this section to its
    cap and pushed `keep` out of a full brief."""
    parts = []
    open_items = [d['id'] for d in data.get('done') or [] if d['id'] != 'G0' and not d['id'].startswith('C')]
    if open_items:
        parts.append('open ' + _ids(open_items))
    remaining = [r for r in (data.get('executor_result') or {}).get('remaining') or [] if str(r).strip()]
    if remaining:
        parts.append('remaining: ' + one(' · '.join(remaining), 160))
    words = _words(data, task)
    if len(words) > 1:
        parts.append('latest request %s: "%s"' % (_stamp(words[-1]['at']), one(words[-1]['text'], 160)))
    return one(' · '.join(parts) or 'continue the original request', NOW_LIMIT)


def build(data, task=None, base=None):
    """The rule-built summary: open items, remaining work and the latest request as `now`; the previous summary's
    keep items plus corrections that read like a rule as `keep`; every detected subject change as `shifts`."""
    base = previous(data) if base is None else base
    # What a session wrote stays until a session retires it; what Orbit inferred is inferred again from the ledger.
    keep = [dict(k) for k in (base or {}).get('keep') or [] if k.get('by') in ('session', 'carried')]
    rules = [f for f in data.get('feedback') or [] if _prohibition(f.get('text'))]
    for f in sorted(rules, key=lambda f: f.get('at') or ''):  # the oldest first: the newest are in now and below
        if not any(overlap(f['text'], k['text']) >= SAME_ITEM for k in keep):
            keep.append({'text': '%s "%s"' % (_stamp(f.get('at')), one(f['text'], KEEP_ITEM - 16)), 'by': 'orbit',
                         'at': f.get('at')})
    return _bounded({'by': 'orbit', 'now': _now_from_ledger(data, task), 'keep': keep,
                     'shifts': _shifts(data, task, base, [])}, [])


def _shifts(data, task, base, written):
    """The previous summary's shifts, then the session's (or, when it wrote none, the detected ones)."""
    rows = [dict(s) for s in (base or {}).get('shifts') or []]
    for text in written:
        if not any(overlap(text, s['text']) >= SAME_ITEM for s in rows):
            rows.append({'text': one(text, SHIFT_ITEM), 'by': 'session'})
    if not written:
        start, subject = data.get('attached_at') or '', data.get('goal') or (task or {}).get('prompt') or ''
        for point in shift_points(data, task):
            if not any(s.get('at') == point['at'] for s in rows):
                rows.append({'text': shift_line(data, point, start, subject), 'by': 'orbit', 'at': point['at']})
            start, subject = point['at'], point['to']
    return rows


def _bounded(summary, warnings):
    if len(summary['now']) > NOW_LIMIT:
        warnings.append('now was %d characters; kept the first %d' % (len(summary['now']), NOW_LIMIT))
        summary['now'] = one(summary['now'], NOW_LIMIT)
    for key, item, most in (('keep', KEEP_ITEM, KEEP_MAX), ('shifts', SHIFT_ITEM, SHIFT_MAX)):
        rows = summary[key]
        for row in rows:
            if len(row['text']) > item:
                warnings.append('a %s item was %d characters; kept the first %d' % (key, len(row['text']), item))
                row['text'] = one(row['text'], item)
        if len(rows) > most:
            if key == 'keep':                              # what the session wrote outlives what Orbit inferred
                order = sorted(range(len(rows)), key=lambda i: (rows[i].get('by') != 'session', rows[i].get('by') == 'orbit', i))
                kept = sorted(order[:most])
                dropped = len(rows) - most
                rows = [rows[i] for i in kept]
            else:                                          # the oldest subject changes leave first
                dropped = len(rows) - most
                rows = rows[-most:]
            warnings.append('%s held %d items; the %d least important stay in the ledger' % (key, most + dropped, dropped))
        summary[key] = rows
    summary['warnings'] = warnings
    return summary


def write(data, task, now_text='', keep=(), shifts=(), retire=()):
    """The summary for a new checkpoint. The session's sections win; Orbit fills what it left out, carries over a
    previous keep item the session dropped without retiring it, and names shift candidates it did not mark."""
    base = previous(data)
    keep = [one(k, 10_000) for k in keep if str(k).strip()]
    shifts = [one(s, 10_000) for s in shifts if str(s).strip()]
    now_text = one(now_text, 10_000)
    if not (now_text or keep or shifts):
        summary = build(data, task, base)
    else:
        warnings = []
        retired = set()
        old = (base or {}).get('keep') or []
        for name in retire:
            name = str(name).strip()
            index = _num(name) - 1 if re.fullmatch(r'[Kk]\d+', name) else -1
            if 0 <= index < len(old):
                retired.add(index)
            else:
                warnings.append('--retire %s: the previous summary has K1..K%d' % (name, len(old)))
        rows = [{'text': text, 'by': 'session'} for text in keep]
        missing = []
        for index, row in enumerate(old):
            if index in retired or any(overlap(row['text'], k) >= SAME_ITEM for k in keep):
                continue
            rows.append(dict(row, by='carried'))
            missing.append(index + 1)
        if missing:
            warnings.append('%s of the previous summary not in yours; Orbit kept %s (first: "%s") — if one no longer '
                            'holds: --retire K%d' % (' '.join('K%d' % n for n in missing),
                                                     'it' if len(missing) == 1 else 'them',
                                                     one(old[missing[0] - 1]['text'], 60), missing[0]))
        if not keep and not old:
            rows = build(data, task, base)['keep']
        since = (base or {}).get('at') or ''
        unmarked = [p for p in shift_points(data, task) if p['at'] > since]
        if unmarked and not shifts:
            warnings.append('Orbit marked a possible subject change at %s ("%s"); if it reads wrong, write --shift' % (
                ', '.join(_stamp(p['at']) for p in unmarked[:3]), one(unmarked[-1]['to'], 60)))
        summary = _bounded({'by': 'session', 'now': now_text or _now_from_ledger(data, task), 'keep': rows,
                            'shifts': _shifts(data, task, base, shifts)}, warnings)
    summary['verdicts_seen'] = len(data.get('verdicts') or [])
    return summary


def current(data, task=None):
    """The summary a fresh session starts from: the latest checkpoint's, or for a checkpoint written before
    summaries existed, a rule-built one (computed, not stored)."""
    stored = previous(data)
    rows = data.get('checkpoints') or []
    if not rows:
        return None
    if stored and stored is rows[-1].get('summary'):
        return stored
    summary = build(data, task, stored)
    summary['at'] = rows[-1].get('at')
    return summary


def brief_lines(data, task, summary):
    """NOW, KEEP and SHIFTS lines for the brief (the corrections line after them says where the rest is)."""
    if not summary:
        return []
    lines = ['NOW (summary %s by %s · %s): %s' % (
        _stamp(summary.get('at')), 'the session' if summary.get('by') == 'session' else 'Orbit',
        'ROUND above is the request being worked on' if data.get('rounds') else 'GOAL above is the first request',
        summary['now'])]
    if summary.get('keep'):
        lines.append('KEEP (still holds, whatever its age): ' + ' · '.join(
            'K%d %s' % (i + 1, k['text']) for i, k in enumerate(summary['keep'])))
    if summary.get('shifts'):
        lines.append('SHIFTS (subject changes; an earlier subject as goal → result): ' + ' · '.join(s['text'] for s in summary['shifts']))
    return lines


def request_text(data, task, prefix, tid):
    """What the checkpoint request shows: the previous summary and what is new since, to be rewritten."""
    base = previous(data)
    news = new_since(data, base)
    parts = ['Rewrite the handoff summary from the previous one plus what is new: --now \'<current goal · what remains · '
             'next step>\' --keep \'<a decision, rule or constraint that still holds>\' (repeat; a previous one you leave out '
             'is carried over unless you --retire K2) --shift \'<when: from what → to what; the earlier subject as goal → '
             'result>\' (repeat). Anything else stays in the ledger.']
    if base:
        parts.append('PREVIOUS SUMMARY (%s): NOW %s' % (_stamp(base.get('at')), one(base.get('now'), 300)))
        if base.get('keep'):
            parts.append('KEEP ' + ' · '.join('K%d %s' % (i + 1, one(k['text'], 120)) for i, k in enumerate(base['keep'])))
        if base.get('shifts'):
            parts.append('SHIFTS ' + ' · '.join(one(s['text'], 120) for s in base['shifts']))
    else:
        parts.append('No summary yet: write the first one.')
    new = []
    if news['prompts']:
        new.append('%d request(s), latest %s "%s"' % (len(news['prompts']), _stamp(news['prompts'][-1]['at']),
                                                    one(news['prompts'][-1]['text'], 160)))
    if news['feedback']:
        new.append('%d correction(s)' % len(news['feedback']))
    for d in news['closed'][-4:]:
        new.append('%s %s%s' % (d['id'], d.get('state'), ' (' + one((d.get('evidence') or [''])[0], 100) + ')'
                               if d.get('evidence') else ''))
    for v in news['verdicts'][-2:]:
        new.append('verdict turn %s %s' % (v.get('turn'), v.get('outcome')))
    if new:
        parts.append('NEW SINCE: ' + ' · '.join(new))
    candidates = [p for p in shift_points(data, task) if p['at'] > ((base or {}).get('at') or '')]
    if candidates:
        parts.append('SHIFT CANDIDATES (little in common with what came before): ' + ' · '.join(
            '%s "%s"' % (_stamp(p['at']), one(p['to'], 80)) for p in candidates[-3:]))
    return '\n'.join(parts)
