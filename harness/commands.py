"""Shared, model-free command reference for terminal and browser.

One native execution loop owns goal preservation, recovery and completion verification. Legacy subcommands still exist in `--help`.
"""
from html import escape

GROUPS = [
    ('Work', [
        ('cosmos-orbit start --provider codex --workspace /path/to/repo "Your request"',
         'Fix the original goal, execute with the native CLI, check the result and independently test the completion claim. --check and repeated --done are optional.'),
        ('cosmos-orbit worker --background', 'Continue authorised queued work and recover interrupted calls after the terminal closes.'),
        ('cosmos-orbit resume TASK_ID', 'Continue after a pause. Interrupted calls are reconciled with retained files and the owned native session.')]),
    ('Inspect', [
        ('cosmos-orbit dashboard', 'Inspect the same goals, calls, checks, recovery and conclusions shown in the terminal.'),
        ('cosmos-orbit watch --once', 'Read one model-free terminal snapshot.'),
        ('cosmos-orbit status TASK_ID', 'Read completion evidence, retry time, work location and process state.'),
        ('cosmos-orbit verify TASK_ID', 'Recheck a saved result without rerunning implementation. Missing evidence is not completion.')]),
    ('Steer', [
        ('cosmos-orbit feedback TASK_ID "Updated instruction"', 'Keep the original goal and add this correction. Repeated --done explicitly replaces the checklist.'),
        ('cosmos-orbit pause TASK_ID', 'Stop at a safe boundary. A user pause is never cleared by automatic recovery.'),
        ('cosmos-orbit handoff TASK_ID', 'Preserve goal, feedback and current evidence before choosing a fresh executor session.')])
]
NOTE = ('Use --home PATH consistently for the app, worker and observers. '
        'Viewing help or observing a task never starts model work. See --help for engine switching, memory and historical records.')


def terminal_guide():
    lines = ['COSMOS ORBIT / COMMAND REFERENCE', NOTE]
    for title, entries in GROUPS:
        lines.append('\n' + title.upper())
        for command, description in entries:
            lines.extend(['  ' + command, '    ' + description])
    return '\n'.join(lines)


def browser_guide():
    blocks = ['<p class="fine">' + escape(NOTE) + '</p>']
    for title, entries in GROUPS:
        blocks.append('<h3>' + escape(title) + '</h3>')
        for command, description in entries:
            blocks.append('<div class="command-item"><code>' + escape(command) + '</code><p class="fine">' + escape(description) + '</p></div>')
    return ''.join(blocks)
