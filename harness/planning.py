"""Explicit, persisted development plans shared by CLI and browser controls."""
import json
from . import providers

SCHEMA = providers.obj({'goal': providers.STRING, 'steps': providers.STRINGS,
                        'constraints': providers.STRINGS, 'risks': providers.STRINGS,
                        'acceptance': providers.STRINGS})


def prepare(engine, task):
    data = task['data']
    prompt = ('Produce a concrete execution plan, not implementation. Preserve the full user goal. '
              'Separate collection, analysis, verification and presentation where relevant. '
              'Acceptance must restate user outcomes, not add mandatory logs, reports or optional checks. '
              'Do not prescribe commands the executor cannot run. Orbit performs fixed checks after execution. '
              'Do not claim any step is done. State capability/data limits. Reply in the language of the request. '
              'Fixed verification commands cannot be replaced by the executor. A check passing is not objective completion.\n'
              + json.dumps({'request': data.get('mission_goal', task['prompt']), 'title': data.get('presentation', {}).get('title'),
                            'feedback': data['feedback'], 'latest_result': data.get('last_summary'),
                            'reason': data.get('reason'), 'checks': data['development']['checks'],
                            'public_web_allowed': data['options'].get('public_web', False),
                            'executor_capabilities': providers.native_capabilities(task['provider'], data['options'].get('public_web', False))}, ensure_ascii=False))
    answer = engine._call(task, 'planning', prompt, SCHEMA)
    if (not isinstance(answer.get('goal'), str) or not answer['goal'].strip()
            or any(not isinstance(answer.get(k), list) or any(not isinstance(v, str) for v in answer[k])
                   for k in ('steps', 'constraints', 'risks', 'acceptance')) or not answer['steps']):
        raise ValueError('Planner returned an invalid plan')
    data = task['data']
    review = data.get('review_plan', True)
    data['execution_plan'] = dict(answer, state='awaiting_review' if review else 'approved')
    data['plan_requested'] = False
    data['development']['hold'] = review
    data['development']['phase'] = 'ready'
    data['reason'] = 'Review the execution plan, then approve or send a correction.' if review else ''
    engine.store.save(task, 'needs_input' if review else 'ready', 'execution_plan_ready', {'layer': 'control', 'note': answer['goal']})


def approve(store, task):
    plan = task['data'].get('execution_plan')
    if not plan or plan.get('state') != 'awaiting_review' or task['status'] != 'needs_input':
        raise ValueError('This task has no plan awaiting review')
    plan['state'] = 'approved'
    task['data']['development']['hold'] = False
    task['data']['development']['phase'] = 'ready'
    task['data']['reason'] = ''
    store.save(task, 'ready', 'execution_plan_approved', {'layer': 'control', 'direction': 'out', 'note': 'Plan approved; queued for execution'})
