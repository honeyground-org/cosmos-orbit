"""Shared, model-free command reference for terminal and browser.

Ten commands (redesign 2026-11-01 §5: "commands ≤ 10"). The simplified loop — goal → executor → fixed checks →
one independent review — is the preview path selected explicitly with --simplified; the legacy planner/manager
path remains the default until the shared measurement (§7) closes. Legacy subcommands still exist in `--help`.
"""
from html import escape

GROUPS = [
    ('Start', [
        ('cosmos-orbit doctor', 'Check installed provider CLIs. Nothing is installed or changed.'),
        ('cosmos-orbit start --simplified --development --provider codex --workspace /path/to/repo --check \'["python3", "-m", "unittest", "discover"]\' --done "D1 …" --done "D2 …" "Your request"',
         'Simplified loop (preview): fix the goal and completion definition, run the executor in a private clone, run your fixed checks, then one independent review. Omit --done to let the executor propose it on its first turn. --max-turns 8 by default; there is no elapsed-time cutoff.'),
        ('cosmos-orbit start --development --provider claude --workspace /path/to/repo --check \'["python3", "verify.py"]\' "Your request"',
         'Legacy development path (planner and manager). Still the default while the simplified loop is measured.')]),
    ('Observe', [
        ('cosmos-orbit dashboard', 'Open the engine room: goal, completion checklist (✓ met · ✗ unmet · ? unclear · ○ not reviewed), fixed-check exit codes, the session stream and the stop reason.'),
        ('cosmos-orbit watch --once', 'Print one terminal snapshot of the same projection.'),
        ('cosmos-orbit status TASK_ID', 'Inspect a task: completion definition, verdicts, resumes, evidence and whether the verified tree is still current.')]),
    ('Steer', [
        ('cosmos-orbit feedback TASK_ID --queue "Updated instruction"', 'Queue a correction for the next safe boundary; the same session resumes. Add --done "…" to replace the completion definition explicitly, or --max-turns N to raise the turn limit.'),
        ('cosmos-orbit pause TASK_ID', 'Pause at a safe boundary; resume TASK_ID continues in this terminal.'),
        ('cosmos-orbit worker', 'Run queued work until Ctrl-C. Use the same --home as the dashboard.'),
        ('cosmos-orbit --home /path/to/private-state dashboard', 'Select a private task store; the worker, watch and dashboard must share it.')])
]
NOTE = ('Replace TASK_ID and /path/to/... with your values. Viewing this guide executes nothing. '
        'Every subcommand, including legacy ones, is listed by cosmos-orbit --help.')


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
