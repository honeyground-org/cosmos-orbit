"""Shared, model-free command reference for terminal and browser."""
from html import escape

GROUPS = [
    ('Start and install', [
        ('harness', 'Open the interactive launcher.'),
        ('harness commands', 'Show this command reference.'),
        ('harness doctor', 'Check installed provider CLIs.'),
        ('harness config --provider codex', 'Set the default engine; claude is also supported.'),
        ('harness start --provider codex --workspace /path/to/project "Your task"', 'Start a managed task.'),
        ('harness start --provider claude --workspace /path/to/project --handoff-file /path/to/HANDOFF.md --plan-only', 'Import a handoff and inspect the plan first (uses the selected AI).')]),
    ('Observe', [
        ('harness dashboard', 'Open the engine room in your browser.'),
        ('harness watch', 'Watch managed tasks in the terminal.'),
        ('harness watch --once', 'Print one status snapshot.'),
        ('harness list', 'List task IDs.'),
        ('harness status TASK_ID', 'Inspect a task and recheck its evidence.'),
        ('harness events TASK_ID', 'Read the recorded execution events.'),
        ('harness messages TASK_ID', 'Read task-owned peer exchanges.')]),
    ('Queue and continue', [
        ('harness start --provider codex --workspace /path/to/project --queue "Your task"', 'Save work for the worker.'),
        ('harness worker', 'Run queued work; Ctrl-C stops the worker.'),
        ('harness worker --once', 'Process the current queue once.'),
        ('harness pause TASK_ID', 'Pause at a safe boundary.'),
        ('harness resume TASK_ID', 'Continue the task in this terminal.'),
        ('harness feedback TASK_ID --queue "Updated instruction"', 'Queue a correction for the next safe boundary.')]),
    ('Sessions and recovery', [
        ('harness switch TASK_ID --provider claude', 'Queue an engine switch using its CLI default.'),
        ('harness switch TASK_ID --provider codex --model MODEL_ID', 'Queue an explicit model selection; use the dashboard for suggestions.'),
        ('harness handoff TASK_ID', 'Save a checkpoint for a fresh session on the next continuation.'),
        ('harness recover TASK_ID --keep-current-files', 'Abandon a conflicting pending proposal and preserve current files.')]),
    ('Options and help', [
        ('harness --home /path/to/private-state dashboard', 'Select a private store; use the same --home for the worker.'),
        ('harness start --help', 'Show all start options, including model, peers, handoff and context budget.'),
        ('harness --help', 'List every subcommand.')])
]
NOTE = 'Replace TASK_ID, MODEL_ID and /path/to/... with your values. Commands shown here are examples; viewing this guide executes nothing.'


def terminal_guide():
    lines = ['LOCAL HARNESS / COMMAND REFERENCE', NOTE]
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
