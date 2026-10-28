"""Shared, model-free command reference for terminal and browser."""
from html import escape

GROUPS = [
    ('Start and install', [
        ('cosmos-orbit', 'Open the interactive launcher.'),
        ('cosmos-orbit commands', 'Show this command reference.'),
        ('cosmos-orbit doctor', 'Check installed provider CLIs.'),
        ('cosmos-orbit config --provider codex', 'Set the default engine; claude is also supported.'),
        ('cosmos-orbit start --provider codex --workspace /path/to/project "Your task"', 'Start a managed task.'),
        ('cosmos-orbit start --provider claude --workspace /path/to/project --handoff-file /path/to/HANDOFF.md --plan-only', 'Import a handoff and inspect the plan first (uses the selected AI).')]),
    ('Observe', [
        ('cosmos-orbit dashboard', 'Open the engine room in your browser.'),
        ('cosmos-orbit watch', 'Watch managed tasks in the terminal.'),
        ('cosmos-orbit watch --once', 'Print one status snapshot.'),
        ('cosmos-orbit list', 'List task IDs.'),
        ('cosmos-orbit status TASK_ID', 'Inspect a task and recheck its evidence.'),
        ('cosmos-orbit events TASK_ID', 'Read the recorded execution events.'),
        ('cosmos-orbit messages TASK_ID', 'Read task-owned peer exchanges.')]),
    ('Queue and continue', [
        ('cosmos-orbit start --provider codex --workspace /path/to/project --queue "Your task"', 'Save work for the worker.'),
        ('cosmos-orbit worker', 'Run queued work; Ctrl-C stops the worker.'),
        ('cosmos-orbit worker --once', 'Process the current queue once.'),
        ('cosmos-orbit pause TASK_ID', 'Pause at a safe boundary.'),
        ('cosmos-orbit resume TASK_ID', 'Continue the task in this terminal.'),
        ('cosmos-orbit feedback TASK_ID --queue "Updated instruction"', 'Queue a correction for the next safe boundary.')]),
    ('Sessions and recovery', [
        ('cosmos-orbit switch TASK_ID --provider claude', 'Queue an engine switch using its CLI default.'),
        ('cosmos-orbit switch TASK_ID --provider codex --model MODEL_ID', 'Queue an explicit model selection; use the dashboard for suggestions.'),
        ('cosmos-orbit handoff TASK_ID', 'Save a checkpoint for a fresh session on the next continuation.'),
        ('cosmos-orbit recover TASK_ID --keep-current-files', 'Abandon a conflicting pending proposal and preserve current files.')]),
    ('Options and help', [
        ('cosmos-orbit --home /path/to/private-state dashboard', 'Select a private store; use the same --home for the worker.'),
        ('cosmos-orbit start --help', 'Show all start options, including model, peers, handoff and context budget.'),
        ('cosmos-orbit --help', 'List every subcommand.')])
]
NOTE = 'Replace TASK_ID, MODEL_ID and /path/to/... with your values. Commands shown here are examples; viewing this guide executes nothing.'


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
