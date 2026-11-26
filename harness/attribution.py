"""Whether a session's tool call could have changed files, and whether it leaves a process running after it returns.

A folder several sessions share moves all the time; a task's own session is held only to what it moved. A call that
only reads (ls, git status, cosmos-orbit status, a sleep) moved nothing, whatever it printed or named. A call that may
write moved what it names and also what changed while it ran: a `make` at the root writes build output it never names
(2026-11-10, the risk left open when only names counted). Unknown programs and unreadable command lines may write: a
read is only ever assumed for what is known to read."""
import json
import os
import re
import shlex

# Programs that do not change files, unless a flag below says they do. Anything not listed may write.
READ_PROGRAMS = frozenset('''
    ls cat head tail less more grep egrep fgrep rg ag ack wc cut tr nl file stat du df pwd echo printf true false
    test [ [[ sleep date whoami id uname hostname printenv which type whereis ps pgrep lsof jq diff cmp comm md5
    md5sum shasum sha1sum sha256sum basename dirname realpath readlink tree column fold expand seq uptime hexdump
    xxd od strings netstat ping dig nslookup host man export unset set local read wait jobs kill pkill cd pushd
    popd tput clear nproc sw_vers : sort sed awk gawk find curl git gh cosmos-orbit
    break continue exit return shift getopts umask ulimit shopt alias unalias hash times
'''.split())
# The last line: shell builtins that steer the shell and touch no file (2026-11-10: a session waited for its review
# with `cosmos-orbit status … | grep -q … || break`; `break` counted as a write, so every file another session wrote
# while the loop ran was held to the task, twice in one hour). trap, source and . run other commands: not listed.
WRAPPERS = frozenset('sudo time nice command builtin exec env timeout gtimeout caffeinate stdbuf xargs watch nohup setsid'.split())
KEYWORDS = frozenset('if then else elif fi do done while until ! { } time'.split())
HEADERS = frozenset('for select case function'.split())
SHELLS = frozenset('sh bash zsh dash ksh fish'.split())
HARMLESS_TARGETS = frozenset(('/dev/null', '/dev/stdout', '/dev/stderr', '/dev/tty', '-'))
GIT_READ = frozenset('''
    status diff log show rev-parse ls-files ls-tree ls-remote cat-file branch remote describe blame grep merge-base
    rev-list shortlog config fetch tag reflog for-each-ref name-rev check-ignore check-attr count-objects var help
    version whatchanged show-ref symbolic-ref
'''.split())
GH_WRITE = frozenset((('pr', 'checkout'), ('repo', 'clone'), ('run', 'download'), ('release', 'download'),
                      ('gist', 'clone'), ('repo', 'fork')))
ORBIT_WRITE = frozenset('evidence on off settings hooks start evolve'.split())  # evidence runs a command in the folder
FIND_WRITE = frozenset('-delete -exec -execdir -ok -okdir -fprint -fprint0 -fprintf -fls'.split())
# Tools that never write a file. Edit/Write name what they write; every other tool may write while it runs.
READ_TOOLS = frozenset('''
    Read Grep Glob LS WebFetch WebSearch TodoWrite TodoRead ToolSearch Skill AskUserQuestion ListAgents SendMessage
    ScheduleWakeup Monitor TaskStop TaskOutput BashOutput KillShell KillBash ExitPlanMode EnterPlanMode TaskList TaskGet
    TaskCreate TaskUpdate sleep wait update_plan view_image clock__curr_time clock__sleep web__run web_search
    Poll searchTools describeTool
'''.split())
# Poll (Claude Code's REPL harness, 2.1.330): waits for the events of scripts already dispatched and returns them; it
# runs no command. Measured 2026-11-26: a session waited for its review with REPL scripts and Poll, Poll counted as
# a write for the whole wait, and every file other sessions wrote in that time was held to the task (two passing
# reviews set aside as its own change). What a script does is in its own records (verifier._repl_scripts).
BACKGROUND_TOOLS = frozenset(('Agent', 'Task', 'Workflow'))  # subagents work on after the call returns
# An MCP tool (mcp__server__tool, from Claude Code; tools.mcp__server__tool( from a Codex exec script) is told by the
# words of its last part. A read verb and no write verb: it reads. Anything else may write, unknown words included
# (2026-11-10: recall, search_graph and get_code_snippet calls counted as writes, so a session that looked something
# up while another session wrote a file was held to that file).
MCP_READ_VERBS = frozenset('''
    recall search get list read query describe status view find fetch trace neighbors timeline history suggest
    capabilities
'''.split())
MCP_WRITE_VERBS = frozenset('remember create update delete write apply send upload screenshot index reindex'.split())
HEREDOC = re.compile(r'<<-?\s*([\'"]?)([A-Za-z_][\w.-]*)\1')
BACKGROUND = re.compile(r'(?<![&<>|])&(?![&>])')
CODEX_RUNNING = re.compile(r'session[_ ]id\W{0,4}\d', re.I)  # exec_command returned while its process still runs
JS_TOOL = re.compile(r'\btools\.(\w+)\s*\(')
JS_FIELD = re.compile(r'["\']?\b(cmd|workdir)["\']?\s*:\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`)')


def _strip_heredocs(text):
    """The command line without heredoc bodies: their lines are data, not commands (a claim summary often is one)."""
    lines, kept, index = text.split('\n'), [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC.finditer(line):
            if line[max(0, match.start() - 1):match.start()] == '<':
                continue                                   # <<< is a here-string
            while index < len(lines) and lines[index].strip() != match.group(2):
                index += 1
            index += 1                                     # the delimiter line
    return '\n'.join(kept)


def _substitutions(text):
    """The command lines inside $( ) and backticks that the shell runs: not inside single quotes, not escaped (a
    summary in single quotes often holds `names` in backticks; they are text)."""
    found, index, quote = [], 0, None
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == '\\':
            index += 1                                     # the next character is literal
        elif char == "'" and quote is None:
            quote = "'"
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif char == '`':
            end = text.find('`', index + 1)
            end = len(text) if end == -1 else end
            found.append(text[index + 1:end])
            index = end
        elif text.startswith('$(', index) and not text.startswith('$((', index):
            depth, end = 0, index + 1
            while end < len(text):
                depth += {'(': 1, ')': -1}.get(text[end], 0)
                if depth == 0:
                    break
                end += 1
            found.append(text[index + 2:end])
            index = end
        index += 1
    return found


def shell_effect(command, depth=0):
    """(may it change files, does it leave a process running) for one shell command line.

    A job started with `&` by the command's own shell is waited for by a later bare `wait`: the call returns only when
    every such job has ended, so it leaves nothing running (2026-11-11: `for t in tests/*.py; do (python3 $t ...) &
    done; wait` ran 60 s, but was held as a job still running, so three passing reviews in a row were set aside for
    files other sessions wrote hours later). A job a subshell starts (`(make &)`), one started after the `wait`, and
    `nohup`/`setsid` are not waited for and still count; so does `wait PID`, which waits for one job only."""
    if depth > 4:
        return True, False
    text = _strip_heredocs(str(command))
    writes = background = False
    jobs, parens = False, 0                                # `&` jobs of this shell · how deep in subshells we are
    for inner in _substitutions(text):
        inner_writes, inner_background = shell_effect(inner, depth + 1)
        writes, background = writes or inner_writes, background or inner_background
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|()<>\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return True, False                                 # unbalanced quotes: unknown
    segment, index = [], 0
    while index <= len(tokens):
        token = tokens[index] if index < len(tokens) else ';'
        index += 1
        operator = token and all(c in ';&|()<>\n' for c in token)
        if operator and ('>' in token or '<' in token) and '(' not in token:   # <( ) is a command, not a file
            target = tokens[index] if index < len(tokens) else ''
            index += 1
            if '>' in token and not (target in HARMLESS_TARGETS or target.isdigit()):
                writes = True                              # output to a file
            continue
        if not operator:
            segment.append(token)
            continue
        for match in BACKGROUND.finditer(token):
            before = token[:match.start()]
            if parens + before.count('(') - before.count(')') > 0:
                background = True                          # a subshell's job outlives it: nobody waits for it
            else:
                jobs = True
        parens = max(0, parens + token.count('(') - token.count(')'))
        if segment:
            if [word for word in segment if word not in KEYWORDS] == ['wait'] and not parens:
                jobs = False                               # a bare `wait` waits for every job started so far
            seg_writes, seg_background = _segment_effect(segment, depth)
            writes, background = writes or seg_writes, background or seg_background
        segment = []
    return writes, background or jobs


def _segment_effect(words, depth):
    """(writes, background) of one simple command, its words already split."""
    writes = background = False
    while words and (words[0] in KEYWORDS or re.match(r'^[A-Za-z_]\w*=', words[0])):
        words = words[1:]
    if not words or words[0] in HEADERS:
        return writes, background                          # a loop or case header runs nothing itself
    while words and os.path.basename(words[0]) in WRAPPERS:
        wrapper, words = os.path.basename(words[0]), words[1:]
        background = background or wrapper in ('nohup', 'setsid')
        while words and (words[0].startswith('-') or re.match(r'^[A-Za-z_]\w*=', words[0])):
            takes_value = wrapper == 'xargs' and words[0] in ('-I', '-n', '-P', '-L', '-s', '-d', '-E', '-J')
            words = words[2:] if takes_value else words[1:]
        if wrapper in ('timeout', 'gtimeout') and words:
            words = words[1:]                              # the duration
    if not words:
        return writes, background
    program, args = os.path.basename(words[0]), words[1:]
    if program in SHELLS:
        for position, arg in enumerate(args):
            if arg.startswith('-') and not arg.startswith('--') and 'c' in arg and position + 1 < len(args):
                inner_writes, inner_background = shell_effect(args[position + 1], depth + 1)
                return writes or inner_writes, background or inner_background
        return True, background                            # a script file: unknown
    if program == 'eval':
        inner_writes, inner_background = shell_effect(' '.join(args), depth + 1)
        return writes or inner_writes, background or inner_background
    return writes or _program_writes(program, args), background


def _program_writes(program, args):
    if program not in READ_PROGRAMS:
        return True
    if program == 'sed':
        return any(a == '--in-place' or a.startswith('--in-place=') or re.match(r'^-[A-Za-z]*i', a) for a in args)
    if program == 'sort':
        return any(a == '-o' or a.startswith('--output') or re.match(r'^-[A-Za-z]*o', a) for a in args)
    if program in ('awk', 'gawk'):
        return any('>' in a or 'system(' in a or '|' in a for a in args)
    if program == 'find':
        return any(a in FIND_WRITE for a in args)
    if program == 'curl':
        return any(a in ('--output', '--remote-name', '--remote-name-all', '--output-dir')
                   or (re.match(r'^-[A-Za-z]+$', a) and ('o' in a or 'O' in a)) for a in args)
    if program == 'git':
        rest = list(args)
        while rest and rest[0].startswith('-'):
            rest = rest[2:] if rest[0] in ('-C', '-c') else rest[1:]
        if not rest:
            return False
        sub = rest[0]
        if sub in ('stash', 'worktree'):
            return not (len(rest) > 1 and rest[1] in ('list', 'show'))
        return sub not in GIT_READ
    if program == 'gh':
        words = [a for a in args if not a.startswith('-')]
        return tuple(words[:2]) in GH_WRITE
    if program == 'cosmos-orbit':
        words = [a for a in args if not a.startswith('-')]
        sub = words[0] if words else ''
        return sub in ORBIT_WRITE or (sub == 'handoff' and '--write' in args)
    return False


def tool_writes(name):
    """Whether a tool that is not a shell may change files: a known reading tool or an MCP tool whose name reads
    (MCP_READ_VERBS without MCP_WRITE_VERBS) does not; every other tool may."""
    name = str(name or '')
    if name in READ_TOOLS:
        return False
    if name.startswith('mcp__'):
        last = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name.rsplit('__', 1)[-1]).lower()
        words = set(re.findall(r'[a-z]+', last))
        return bool(words & MCP_WRITE_VERBS) or not words & MCP_READ_VERBS
    return True


def _arguments(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _argv_effect(argv):
    if isinstance(argv, str):
        return shell_effect(argv)
    if not isinstance(argv, list) or not argv:
        return True, False
    argv = [str(a) for a in argv]
    if os.path.basename(argv[0]) in SHELLS and len(argv) >= 3 and argv[1].startswith('-') and 'c' in argv[1]:
        return shell_effect(argv[2])
    return shell_effect(' '.join(shlex.quote(a) for a in argv))


def _js_string(literal):
    quote, body = literal[0], literal[1:-1]
    if quote == '`':
        return None if '${' in body else body              # a template that builds the command: unknown
    if quote == "'":
        body = body.replace('\\\'', '\'').replace('"', '\\"')
    try:
        return json.loads('"' + body + '"')
    except ValueError:
        return None


def _codex_script(script, output):
    """A Codex `exec` script calls tools from JavaScript: (writes, background, workdir)."""
    called = JS_TOOL.findall(script)
    if not called:
        writes, background = shell_effect(script)        # no tool call in it: read as a command line
        return writes, background, None
    fields = [(name, _js_string(literal)) for name, literal in JS_FIELD.findall(script)]
    commands = [value for name, value in fields if name == 'cmd']
    workdir = next((value for name, value in fields if name == 'workdir' and value), None)
    writes = background = False
    if called.count('exec_command') > len(commands) or any(value is None for value in commands):
        writes = True                                      # a command the script builds: unknown
    for command in commands:
        if command is not None:
            command_writes, command_background = shell_effect(command)
            writes, background = writes or command_writes, background or command_background
    writes = writes or any(tool_writes(name) for name in called if name != 'exec_command')
    if 'exec_command' in called and CODEX_RUNNING.search(output or ''):
        background = True
    return writes, background, workdir


# Claude Code's REPL tool runs one JavaScript script per call. The script reaches the machine only through its
# globals: `$` (a shell command, as a template `$\`…\`` whose ${…} parts go in as quoted words, or as `$(text)`),
# write and edit (a file), tools.<Name>(…) and the same tools as bare globals; file(…), sleep and the rest read. A
# script reads when every call in it is known to read: a command the shell classifier reads, a tool that reads, a
# JavaScript builtin, a method of a value, a function the script defines itself. Anything else may write — a helper
# an earlier script defined (the REPL keeps its context) is not in the code and is not guessed.
REPL_READ_CALLS = frozenset('''
    file sleep preview log running searchTools describeTool atob btoa setTimeout clearTimeout setInterval
    clearInterval JSON Object Array Math String Number Boolean Date RegExp Promise Set Map WeakMap WeakSet Symbol
    Error TypeError RangeError SyntaxError parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
    encodeURI decodeURI structuredClone BigInt Intl TextEncoder TextDecoder URL URLSearchParams
'''.split())
JS_KEYWORDS = frozenset('''
    if for while switch catch function return typeof await async new do else try finally throw yield void delete
    in of instanceof case default with class extends super this let const var import export
'''.split())
JS_IDENT = re.compile(r'[A-Za-z_$][\w$]*')
JS_DEFINED = re.compile(r'\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*'
                        r'(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)')
JS_REGEX_BEFORE = frozenset('(,=:[!&|?{};+-*%<>~^')
JS_REGEX_WORDS = frozenset('return typeof case do else in of yield await void delete instanceof new'.split())
JS_THEN = re.compile(r'\.\s*then\s*\(')


def _js_scan(code):
    """(blanked, shells, tags) of a script, or None when it cannot be read to its end. `blanked`: the code with
    every string, template, comment and regular expression replaced by `0` (a template's ${…} parts stay: they
    are code); `shells`: each `$` template as (command, raw), its ${…} parts written ARG, raw when one splices
    its value unquoted (${{raw: …}}); `tags`: the tags of other tagged templates."""
    n, out, shells, tags = len(code), [], [], []

    def tail():
        return ''.join(out[-80:]).rstrip()

    def regex_allowed():
        text = tail()
        if not text or text[-1] in JS_REGEX_BEFORE:
            return True
        word = re.search(r'([A-Za-z_$][\w$]*)$', text)
        return bool(word and word.group(1) in JS_REGEX_WORDS)

    def string(i, quote):
        j = i + 1
        while j < n:
            if code[j] == '\\':
                j += 2
            elif code[j] == quote:
                return j + 1
            elif code[j] == '\n':
                return None
            else:
                j += 1
        return None

    def regex(i):
        j, within = i + 1, False
        while j < n:
            c = code[j]
            if c == '\\':
                j += 2
                continue
            if c == '\n':
                return None
            if within:
                within = c != ']'
            elif c == '[':
                within = True
            elif c == '/':
                j += 1
                while j < n and (code[j].isalnum() or code[j] == '_'):
                    j += 1
                return j
            j += 1
        return None

    def template(i):
        j, text, raw = i + 1, [], False
        while j < n:
            c = code[j]
            if c == '\\':
                text.append(code[j:j + 2])
                j += 2
            elif c == '`':
                return j + 1, ''.join(text), raw
            elif code.startswith('${', j):
                raw = raw or re.match(r'\$\{\s*\{\s*raw\s*:', code[j:]) is not None
                out.append(' ')
                j = scan(j + 2, True)
                if j is None:
                    return None
                out.append(' ')
                text.append('ARG')
            else:
                text.append(c)
                j += 1
        return None

    def scan(i, inner):
        depth = 0
        while i < n:
            c = code[i]
            if c in '"\'':
                j = string(i, c)
                if j is None:
                    return None
                out.append(' 0 ')
                i = j
            elif c == '`':
                tag = re.search(r'([A-Za-z_$][\w$.]*)$', tail())
                tag = tag.group(1) if tag else ''
                found = template(i)
                if found is None:
                    return None
                i, text, raw = found
                if tag == '$':
                    shells.append((text, raw))
                elif tag and tag not in JS_KEYWORDS and not tag.endswith('String.raw'):
                    tags.append(tag)
                out.append(' 0 ')
            elif code.startswith('//', i):
                j = code.find('\n', i)
                i = n if j < 0 else j
            elif code.startswith('/*', i):
                j = code.find('*/', i + 2)
                if j < 0:
                    return None
                out.append(' ')
                i = j + 2
            elif c == '/' and regex_allowed():
                j = regex(i)
                if j is None:
                    return None
                out.append(' 0 ')
                i = j
            else:
                if c == '{':
                    depth += 1
                elif c == '}':
                    if inner and depth == 0:
                        return i + 1
                    depth -= 1
                out.append(c)
                i += 1
        return None if inner else n

    try:
        if scan(0, False) is None:
            return None
    except RecursionError:
        return None
    return ''.join(out), shells, tags


def repl_effect(code):
    """(writes, background) of a Claude Code REPL script (see REPL_READ_CALLS); a script that cannot be read may
    write. background: it leaves work running (a shell job, a subagent, work chained with .then and not awaited)."""
    scanned = _js_scan(str(code or ''))
    if scanned is None:
        return True, False
    blanked, shells, tags = scanned
    writes, background = bool(tags) or re.search(r'\btools\s*\[', blanked) is not None, False
    for text, raw in shells:
        command_writes, command_background = (True, False) if raw else shell_effect(text)
        writes, background = writes or command_writes, background or command_background
    defined = {first or second for first, second in JS_DEFINED.findall(blanked)}
    for match in JS_IDENT.finditer(blanked):
        name, start, end = match.group(), match.start(), match.end()
        if not blanked[end:].lstrip().startswith('('):
            continue
        before = blanked[:start].rstrip()
        if before.endswith('.'):
            owner = re.search(r'(?<![\w$.])([A-Za-z_$][\w$]*)\s*\??\.$', before)
            if owner and owner.group(1) == 'tools':
                writes = writes or tool_writes(name)
                background = background or name in BACKGROUND_TOOLS
            continue                                     # a method of a value
        if name in JS_KEYWORDS or name in defined or name in REPL_READ_CALLS:
            continue
        if name in ('$', 'write', 'edit'):
            writes = True                                # a command built at run time, or a file written
            continue
        writes = writes or tool_writes(name)             # a tool as a bare global, or a helper not defined here
        background = background or name in BACKGROUND_TOOLS
    return writes, background or (writes and repl_leaves_work(blanked))


def repl_leaves_work(code):
    """Whether a REPL script chains work with .then: it may still run after the script has ended."""
    scanned = _js_scan(str(code or ''))
    return JS_THEN.search(scanned[0] if scanned else str(code or '')) is not None


def call_effect(provider, call, output=''):
    """(writes, background, folder it ran in or None) for one tool call from a transcript.

    provider 'claude': `call` is the tool_use part. provider 'codex': the response item."""
    if provider == 'claude':
        name, data = call.get('name'), call.get('input') if isinstance(call.get('input'), dict) else {}
        if name == 'Bash':
            writes, background = shell_effect(data.get('command') or '')
            return writes, background or bool(data.get('run_in_background')), None
        if name in BACKGROUND_TOOLS:
            return True, True, None
        if name == 'REPL':
            writes, background = repl_effect(data.get('code') or '')
            return writes, background, None
        return tool_writes(name), False, None
    kind, name = call.get('type'), call.get('name')
    if kind == 'local_shell_call':
        action = call.get('action') if isinstance(call.get('action'), dict) else {}
        writes, background = _argv_effect(action.get('command'))
        return writes, background, action.get('working_directory')
    if kind == 'custom_tool_call' and name == 'exec':
        return _codex_script(str(call.get('input') or ''), output)
    data = _arguments(call.get('arguments') if kind == 'function_call' else call.get('input'))
    if name in ('shell', 'container.exec', 'local_shell'):
        writes, background = _argv_effect(data.get('command'))
        return writes, background, data.get('workdir')
    if name == 'exec_command':
        writes, background = _argv_effect(data.get('cmd'))
        return writes, background or bool(CODEX_RUNNING.search(output or '')), data.get('workdir')
    return tool_writes(name), False, None
