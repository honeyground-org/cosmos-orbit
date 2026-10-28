#!/usr/bin/env python3
"""Build an offline, synthetic preview from the installed UI template. No private state is read."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.commands import browser_guide


def build():
    template = (ROOT / 'harness/observer.html').read_text(encoding='utf-8')
    html = template.replace('__LANG__', 'en').replace('__TOKEN__', 'visual-demo')
    html = html.replace('__COMMAND_GUIDE__', browser_guide())
    topics = [('Design the next interface', 'codex', 2), ('Implement session routing', 'claude', 2),
              ('Verify handoff recovery', 'codex', 4), ('Review permission boundary', 'claude', 1),
              ('Refine the onboarding flow', 'codex', 2), ('Build the export pipeline', 'claude', 2),
              ('Check recovery evidence', 'codex', 4), ('Prepare a context handoff', 'claude', 2),
              ('Improve keyboard navigation', 'codex', 2), ('Review the API contract', 'claude', 1),
              ('Validate the output format', 'codex', 4), ('Summarise the next steps', 'claude', 2)]
    tasks = []
    for i, (goal, provider, stage) in enumerate(topics):
        tasks.append(dict(id=f'demo-{i}', provider=provider, goal=goal, workspace='/demo/workspace',
            status='running', stage=stage, visited=list(range(stage)), busy=True, attention=i == 3,
            pause_requested=False, reason='Synthetic preview. No AI calls or workspace changes.',
            revision=1, requested_model=None, routing=None, attempt=1, budget=3,
            metrics=dict(calls=4+i, model_seconds=40+i*12, input_tokens=45000, output_tokens=3200, elapsed_seconds=180),
            steps=['Establish the contract', 'Implement the bounded change', 'Verify against the objective'],
            constraints=['Demonstration only'], last_summary='Previewing execution signals.',
            checks=[dict(passed=j<2, path=f'check-{j}', reason=reason) for j, reason in enumerate(
                ['Contract valid', 'Recovery checkpoint verified', 'Final verification pending'])],
            events=[], messages=[], feedback=[],
            sessions=[dict(name='executor', provider=provider, id=f'demo-session-{i}', peer=False,
                state='running', requested_model=None, reported_models=['Illustrative model'],
                context_tokens=[122000,310000,74000,440000][i%4], context_limit=500000,
                context_source='Synthetic fixture', handoff_count=i%3)]))
    state = dict(tasks=tasks, scope='demo', worker_running=True, at='', events=[], model_options={},
                 labels=dict(en=dict(running='Running', idle='Idle')))
    start = html.index('async function poll()')
    end = html.index('\nasync function action', start)
    html = html[:start] + 'const demoState=' + json.dumps(state) + ''';let demoTick=0;
const demoEvents=[['model_started','Orbit sends a bounded task to the engine.'],['model_finished','The engine returns a proposed result to Orbit.'],['session_checkpoint','Orbit records a context checkpoint.'],['verified','Orbit records verification evidence.'],['feedback_queued','A new instruction reaches the control layer.']];
async function poll(){
 demoTick++;data=demoState;data.at=new Date().toISOString();
 data.tasks.forEach((t,i)=>{const [kind,note]=demoEvents[(demoTick+i)%demoEvents.length];t.events.unshift({seq:demoTick,at:data.at,kind,note});t.events=t.events.slice(0,12)});
 data.events=[{...data.tasks[0].events[0],task_id:'demo-0'}];connected=true;render();
}
''' + html[end:]
    start = html.index('async function action(')
    end = html.index("\n$('showCommands')", start)
    html = html[:start] + "async function action(){ $('notice').textContent='Preview only. Install Cosmos Orbit to manage your own tasks.' }" + html[end:]
    # Keep the demo label accurate on every render, including pagination and language events.
    html = html.replace("connected?'● CONNECTED':'○ DISCONNECTED'", "'● INTERACTIVE DEMO'")
    html = html.replace("data.worker_running?'WORKER ONLINE':'WORKER OFFLINE'", "'SYNTHETIC SESSIONS'")
    html = html.replace("executing?'Work in motion.':attention?'A decision is waiting.':'The machine is at rest.'", "'Inside the living machine.'")
    html = html.replace("`${modules.length} managed sessions · ${data.tasks.length} tasks · ${data.scope==='managed'?'Owned sessions only':''}`", "'Explore Cosmos Orbit · Drag to rotate · Scroll to zoom · Click an engine'")
    html = html.replace('COSMOS ORBIT / LIVE SYSTEM', 'INTERACTIVE PREVIEW / NO REAL TASKS OR MODEL CALLS')
    html = html.replace('Glow indicates execution, not hardware temperature.', 'Synthetic demo signals, not hardware temperature.')
    html = html.replace('<title>Cosmos Orbit · Engine Room</title>', '<title>Cosmos Orbit · Interactive Demo</title><meta name="description" content="Explore the Cosmos Orbit engine room: AI session cores, context orbits, live signals and interactive inspection."><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'nonce-visual-demo\'; style-src \'unsafe-inline\'; img-src data:; connect-src \'none\'; base-uri \'none\'; form-action \'none\'">')
    html = html.replace('<button id="showCommands"', '<a class="demo-source" href="https://github.com/honeyground-org/cosmos-orbit">Install / GitHub ↗</a><button id="showCommands"')
    html = html.replace('</style>', '.demo-source{color:var(--blue);text-decoration:none;font-size:11px;margin-right:14px}@media(max-width:700px){.demo-source{display:inline-block;margin:5px}#connection{font-size:9px}}\n</style>')
    if 'fetch(' in html or '__COMMAND_GUIDE__' in html or '/Users/' in html:
        raise ValueError('Preview must be self-contained and contain no runtime requests or personal paths')
    return html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail if the committed preview is stale')
    args = parser.parse_args()
    output = ROOT / 'docs/index.html'
    html = build()
    if args.check:
        if not output.exists() or output.read_text(encoding='utf-8') != html:
            raise SystemExit('Preview is stale. Run python3 scripts/build_demo.py')
        print('Preview is current.')
    else:
        output.write_text(html, encoding='utf-8')
        print('Built docs/index.html with synthetic data only.')


if __name__ == '__main__':
    main()
