"""Shared, model-free command reference for terminal and browser.

Two entrances, one ledger: attach to your own Claude Code or Codex session (hooks), or run unattended in a private clone.
"""
from html import escape

GROUPS = [
    ('Your own session (hooks)', [
        ('cosmos-orbit hooks install', 'Add the hooks (SessionStart, UserPromptSubmit, Stop, PreCompact, and SessionEnd and StopFailure for Claude Code) once. Inert until something is switched on. Run it again after an update that adds a hook.'),
        ('cosmos-orbit on --global   ·   cosmos-orbit on   ·   export ORBIT=on',
         'Three scopes, each with off: every project (--global), this project (Git root), this terminal session (environment). Precedence: terminal → project → global → off. Stored switches persist.'),
        ('cosmos-orbit context [350k | on | off | reset] [--global]   ·   export ORBIT_HANDOFF_AT=250k',
         'Mid-task handoff point, same three scopes as on/off (built-in 350k). Stored in <home>/settings.json (section "handoff"): '
         'the command writes it, a hand edit applies at the next hook event, reset removes a stored point. Without a value: '
         'the point, where it came from, the settings file and the live context here.'),
        ('cosmos-orbit context auto   ·   cosmos-orbit context tell',
         'Who types into a session\'s terminal (cmux, tmux): auto = Orbit types /clear at a handoff and wakes an idle session for a message; tell (default) = Orbit only tells.'),
        ('cosmos-orbit patrol --once   ·   cosmos-orbit supervision auto|tell',
         "Orbit's round, every minute inside the dashboard: a question waiting for you is told at once (terminal, and the desktop when the terminal posts no banner), again after 30 min and 2 h, and an hour before the 12-hour close; a session idle 5 min with work left and no supervisor is sent back (auto), at most twice per idle stretch, then you are told. ORBIT_PATROL=off stops the round."),
        ('cosmos-orbit wake --session ID [--surface PANE] [--from NAME] [--when-busy wait|skip] [--check]',
         'A sender calls this right after delivering a message: the receiving session is woken when its input line is empty. Your own typing is never erased.'),
        ('cosmos-orbit route list   ·   cosmos-orbit route add NAME --base-url URL --model MODEL[,MODEL…] [--api anthropic|openai] [--auth bearer|x-api-key] [--key env:VAR|keychain:SERVICE] [--until YYYY-MM-DD] [--label \'…\']',
         'Where a session can run when the one in use hits its usage limit: claude and codex on your accounts, plus routes you add to any service — Claude Code on an Anthropic-compatible endpoint (--api anthropic, the default: Ollama\'s cloud, a local Ollama, a gateway) or Codex on an OpenAI Responses-compatible one (--api openai). A route may serve several models, the first its default. The key is your own, read only when a route starts; past --until a route is not offered.'),
        ('cosmos-orbit route key NAME [--clipboard | --remove]',
         'Put your own key for a route into your macOS keychain (typed without echo, piped, or from the clipboard), or delete it. Never printed; each user keeps their own.'),
        ('cosmos-orbit route on|off [NAME]',
         'The switches. Without NAME: the whole offer (off = a limited session is left alone). With NAME: one route, built-in or added, kept out of the offer and of route run.'),
        ('cosmos-orbit route run NAME [--model MODEL] [--carry] [-- ARGS]',
         'Start that route in this terminal, on its default model or the one you name. A session stopped at its limit offers the usable routes (and each other model of theirs) at your next prompt, numbered; so does a session where you ask for it ("kimi로 바꿔줘", "다른 모델로", "switch to codex"). Type a number or a name and Orbit quits it and runs this in the same pane, carrying the conversation\'s context (and your request) to the new session, Claude Code or Codex.'),
        ('cosmos-orbit settings', 'Hook installation, switches, handoff points and what applies in this folder.'),
        ('claude --dangerously-skip-permissions   ·   codex',
         'Your normal command. The first substantial prompt becomes the goal; every prompt, correction, evidence and verdict lands in the ledger; the brief returns after compaction and at the next session.')]),
    ('Inside the session (the executor calls these)', [
        ("cosmos-orbit done TASK_ID 'D1 …' [--add] [--keep] [--check ARGV_JSON]",
         'Write a revision of the result goals and verification goals. Required after every requirement change before a claim; fixed checks are append-only.'),
        ("cosmos-orbit evidence TASK_ID D1 --note '…' [--command '[\"python3\",\"-m\",\"pytest\"]'] [--file path]",
         'Evidence per item: a command actually run (exit code and output tail), a file fingerprint, or a note.'),
        ("cosmos-orbit checkpoint TASK_ID --next '…' [--notes '…'] [--now '…'] [--keep '…'] [--shift '…'] [--retire K2] [--file path]",
         'At the handoff point: the next step, what the ledger cannot know, and the handoff summary rewritten from the previous one (now · keep · subject shifts; Orbit builds one from the ledger if you write none). Then /clear or /new (typed by the user, or by Orbit in auto mode); the fresh session starts from it.'),
        ("cosmos-orbit claim TASK_ID --summary '…' [--remaining '…'] [--wait]",
         'Completion claim: fixed checks run now; a failure returns to you; a pass queues one independent read-only review.'),
        ('cosmos-orbit feedback TASK_ID "Correction"', 'Keep the original goal and add this correction. Repeated --done explicitly replaces the checklist.'),
        ('cosmos-orbit brief [TASK_ID]', 'Print what the hooks inject: goal, corrections, checklist, evidence, last verdict, stop reason.'),
        ('cosmos-orbit handoff TASK_ID --write docs/HANDOFF.md', 'Render the ledger as a Markdown handoff. Nothing is written by hand.')]),
    ('Unattended (private clone)', [
        ('cosmos-orbit start --provider codex --workspace /path/to/repo "Your request"',
         'Fix the goal, execute with the native CLI in an isolated clone, run fixed checks and independently test the completion claim.'),
        ('cosmos-orbit worker --background', 'Continue authorised queued work, pending reviews and interrupted calls after the terminal closes.'),
        ('cosmos-orbit service install [--port 55050] [--lang ko]   ·   service status   ·   service uninstall',
         'Start again at login: the dashboard is kept running, and one recovery pass continues work authorised before the shutdown. macOS launchd, Linux systemd --user.'),
        ('cosmos-orbit resume TASK_ID   ·   cosmos-orbit pause TASK_ID', 'Continue after a pause, or stop at a safe boundary. A user pause is never cleared by recovery.')]),
    ('Inspect', [
        ('cosmos-orbit dashboard', 'The same goals, evidence, checks, reviews and stop reasons as the terminal, for both entrances.'),
        ('cosmos-orbit watch --once   ·   cosmos-orbit status TASK_ID', 'One model-free snapshot, or the full record of one task.'),
        ('cosmos-orbit verify TASK_ID', 'Recheck a claimed result now: fixed checks plus one independent review. Missing evidence is not completion.'),
        ('cosmos-orbit lessons', 'Corrections repeated across tasks: candidates for a guard rule, counted from the ledger.'),
        ('cosmos-orbit evolve   ·   cosmos-orbit evolve --reset-learned', 'What this installation learned locally from the executor\'s decisions (never in the code), or forget it.'),
        ('cosmos-orbit evolve --mode propose|merge --repo PATH', 'Maintainers: repeated Orbit defects fixed in a worktree behind four gates (checks, no personal traces, no vocabulary, general review).')])
]
NOTE = ('Use --home PATH (or HARNESS_HOME) consistently for hooks, worker and observers. '
        'Viewing help, briefs or the dashboard never starts model work; the only model call after a claim is the one review.')


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
