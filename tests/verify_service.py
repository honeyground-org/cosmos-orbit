#!/usr/bin/env python3
"""Model-free check of `cosmos-orbit service`: Orbit starts again at login without a person (§harness.service).

Everything runs in a throwaway HOME against stand-ins for `launchctl` and `systemctl` placed first on PATH. The stand-in
launchd really starts the kept-alive dashboard definition as a process listening on its port (and stops it on bootout),
so "the dashboard answers after install" is measured, not assumed. Nothing touches the real ~/Library/LaunchAgents or
the user's systemd.
"""
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix='orbit-service-'))
os.environ['HOME'] = str(TMP / 'home')
os.environ.pop('XDG_CONFIG_HOME', None)
os.environ.pop('HARNESS_HOME', None)
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
(TMP / 'home').mkdir()
sys.path.insert(0, str(ROOT))
from harness import service  # noqa: E402

FAILED = []
STATE = TMP / 'fake-state.json'
FAKES = TMP / 'bin'
FAKES.mkdir()
EMPTY = TMP / 'empty-bin'
EMPTY.mkdir()


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:400]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def is_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.3):
            return True
    except OSError:
        return False


FAKE = r'''#!%(python)s
"""Stand-in for launchctl/systemctl: records calls; a kept-alive dashboard definition really listens on its port."""
import json, os, plistlib, signal, socket, subprocess, sys, time
from pathlib import Path
STATE = Path(%(state)r)
LISTEN = "import socket,sys,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\ns.bind(('127.0.0.1',int(sys.argv[1])));s.listen()\ntime.sleep(120)"
state = json.loads(STATE.read_text()) if STATE.exists() else {'calls': [], 'loaded': {}}
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
state['calls'].append([tool] + args)
code = 0
def start(arguments):
    if '--port' in arguments:
        port = arguments[arguments.index('--port') + 1]
        process = subprocess.Popen([sys.executable, '-c', LISTEN, port], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(('127.0.0.1', int(port)), timeout=.2).close(); break
            except OSError:
                time.sleep(.05)
        return process.pid
    return None
def stop(pid):
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
if tool == 'launchctl':
    if args[0] == 'print':
        label = args[1].split('/', 2)[2]
        if label in state['loaded']:
            print('%%s = {\n\tstate = running\n\tpid = %%s\n\tlast exit code = (never exited)\n}' %% (args[1], state['loaded'][label]))
        else:
            print('Could not find service "%%s" in domain' %% label, file=sys.stderr); code = 113
    elif args[0] == 'bootout':
        label = args[1].split('/', 2)[2]
        if label in state['loaded']:
            stop(state['loaded'].pop(label))
        else:
            print('Boot-out failed: 3: No such process', file=sys.stderr); code = 3
    elif args[0] == 'bootstrap':
        with open(args[2], 'rb') as handle:
            definition = plistlib.load(handle)
        state['loaded'][definition['Label']] = start(definition['ProgramArguments']) if definition.get('KeepAlive') else 0
else:  # systemctl --user ...
    args = args[1:]
    if args[0] == 'show-environment':
        print('PATH=/usr/bin')
    elif args[:2] == ['enable', '--now']:
        unit = Path(os.environ['HOME']) / '.config/systemd/user' / args[2]
        line = [l for l in unit.read_text().splitlines() if l.startswith('ExecStart=')][0]
        import shlex
        state['loaded'][args[2]] = start(shlex.split(line[len('ExecStart='):]))
    elif args[0] == 'disable' and args[1] == '--now':
        stop(state['loaded'].pop(args[2], None))
    elif args[0] == 'stop':
        stop(state['loaded'].pop(args[1], None))
    elif args[0] in ('is-active', 'is-enabled'):
        print('active' if args[0] == 'is-active' and args[1] in state['loaded'] else 'enabled')
STATE.write_text(json.dumps(state))
sys.exit(code)
''' % {'python': sys.executable, 'state': str(STATE)}
for tool in ('launchctl', 'systemctl'):
    (FAKES / tool).write_text(FAKE)
    (FAKES / tool).chmod(0o755)
REAL_PATH = os.environ.get('PATH', '')
os.environ['PATH'] = str(FAKES) + os.pathsep + REAL_PATH
service.WAIT = 3.0


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {'calls': [], 'loaded': {}}


def reset():
    for pid in state()['loaded'].values():
        if pid:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    STATE.unlink() if STATE.exists() else None


def calls(verb):
    return [c for c in state()['calls'] if verb in c]


def plist(name):
    with (Path(os.environ['HOME']) / 'Library/LaunchAgents' / (service.LABELS[name] + '.plist')).open('rb') as handle:
        return plistlib.load(handle)


try:
    check('the definitions go into the throwaway HOME, never the real one',
          str(service._agents()).startswith(str(TMP)) and str(service._units()).startswith(str(TMP)), service._agents())
    store_a = TMP / 'store A'
    port = free_port()

    print('① macOS install: the dashboard runs now and at every login; the recovery pass waits for the next login')
    result = service.install(store_a, port, 'ko', platform='darwin')
    dash, recover = plist('dashboard'), plist('recover')
    check('both definitions start at login (RunAtLoad)', dash['RunAtLoad'] is True and recover['RunAtLoad'] is True)
    check('the dashboard is kept alive; the recovery pass runs once', dash['KeepAlive'] is True and recover['KeepAlive'] is False,
          (dash['KeepAlive'], recover['KeepAlive']))
    check('the dashboard command keeps the store, port and language',
          dash['ProgramArguments'][-8:] == ['--home', str(store_a), 'dashboard', '--port', str(port), '--lang', 'ko', '--no-open'],
          dash['ProgramArguments'])
    check('the recovery pass is a transient worker on the same store',
          recover['ProgramArguments'][-5:] == ['--home', str(store_a), 'worker', '--idle-exit', str(service.RECOVER_IDLE_EXIT)],
          recover['ProgramArguments'])
    check('both carry the installing shell PATH (launchd starts with a bare one)',
          dash['EnvironmentVariables']['PATH'] == os.environ['PATH'] == recover['EnvironmentVariables']['PATH'])
    check('logs live in the store', dash['StandardOutPath'] == str(store_a / 'service-dashboard.log')
          and recover['StandardErrorPath'] == str(store_a / 'service-recover.log'), dash['StandardOutPath'])
    booted = calls('bootstrap')
    check('only the dashboard is loaded now, in the login (gui) domain — installing starts no model work',
          len(booted) == 1 and booted[0][2] == 'gui/%d' % os.getuid() and booted[0][3].endswith(service.LABELS['dashboard'] + '.plist'),
          booted)
    check('the dashboard answers after install', result['reachable'] is True and is_open(port), result)
    check('the result says where it is', result['url'] == 'http://127.0.0.1:%d/' % port, result.get('url'))

    print('② install again without options: the registered port and language are kept, the old process replaced')
    first_pid = state()['loaded'][service.LABELS['dashboard']]
    again = service.install(store_a, platform='darwin')
    dash = plist('dashboard')
    check('port and language preserved', service._option(dash['ProgramArguments'], '--port') == str(port)
          and service._option(dash['ProgramArguments'], '--lang') == 'ko', dash['ProgramArguments'])
    order = [c[1] for c in state()['calls'] if c[0] == 'launchctl' and c[1] in ('bootout', 'bootstrap')]
    check('the running dashboard is booted out before the new definition is bootstrapped',
          order[-2:] == ['bootout', 'bootstrap'] or order[-3:] == ['bootout', 'bootout', 'bootstrap'], order)
    check('a new process answers', again['reachable'] is True and state()['loaded'][service.LABELS['dashboard']] != first_pid)

    print('③ install.sh keeps a registration current without moving its store')
    service.install(TMP / 'store B', home_explicit=False, platform='darwin', if_installed=True)
    check('a refresh without an explicit home keeps the registered store',
          service._option(plist('dashboard')['ProgramArguments'], '--home') == str(store_a),
          plist('dashboard')['ProgramArguments'])
    service.install(TMP / 'store B', home_explicit=True, platform='darwin')
    check('an explicit home moves it', service._option(plist('dashboard')['ProgramArguments'], '--home') == str(TMP / 'store B'))
    service.install(store_a, platform='darwin')

    print('④ status')
    seen = service.status('darwin')
    check('registered, dashboard loaded and answering, recovery pass installed for login',
          seen['registered'] and seen['services']['dashboard']['loaded'] and seen['reachable']
          and seen['services']['recover']['installed'] and not seen['services']['recover']['loaded'], seen)

    print('⑤ uninstall')
    gone = service.uninstall('darwin')
    agents = Path(os.environ['HOME']) / 'Library/LaunchAgents'
    check('both definitions removed', len(gone['removed']) == 2 and not list(agents.glob('com.cosmos-orbit.*')), gone)
    check('the dashboard stopped', service._wait_port(port, False, 3), port)
    check('status reports nothing registered', service.status('darwin')['registered'] is False)

    print('⑥ a dashboard started by hand holds the port: refused, nothing loaded')
    reset()
    with socket.socket() as squatter:
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(('127.0.0.1', port))
        squatter.listen()
        try:
            service.install(store_a, port, platform='darwin')
            refused = ''
        except ValueError as error:
            refused = str(error)
    check('refused with the port named', 'in use' in refused and str(port) in refused, refused)
    check('no bootstrap after a refusal', not calls('bootstrap'), state()['calls'])
    for leftover in agents.glob('com.cosmos-orbit.*'):
        leftover.unlink()

    print('⑦ --if-installed with nothing registered does nothing')
    reset()
    skipped = service.install(store_a, platform='darwin', if_installed=True)
    check('skipped', skipped.get('service') == 'not registered' and not list(agents.glob('com.cosmos-orbit.*'))
          and not calls('bootstrap'), skipped)

    print('⑧ Linux: systemd user units')
    reset()
    odd = TMP / 'store with space %h $HOME'
    linux_port = free_port()
    linux = service.install(odd, linux_port, 'fr', platform='linux')
    unit_dir = Path(os.environ['HOME']) / '.config/systemd/user'
    dash_unit = (unit_dir / service.UNITS['dashboard']).read_text()
    recover_unit = (unit_dir / service.UNITS['recover']).read_text()
    check('dashboard restarts always; recovery pass does not', 'Restart=always' in dash_unit and 'Restart=no' in recover_unit)
    check('both are wanted at login', 'WantedBy=default.target' in dash_unit and 'WantedBy=default.target' in recover_unit)
    path, parsed = service._systemd_read('dashboard')
    check('ExecStart round-trips a store path with a space, % and $ (systemd specifiers escaped)',
          service._option(parsed['ProgramArguments'], '--home') == str(odd) and '%%h' in dash_unit and '$$HOME' in dash_unit,
          parsed['ProgramArguments'])
    sequence = [c[2:] for c in state()['calls'] if c[0] == 'systemctl']
    check('daemon-reload, recovery enabled for login only, dashboard enabled and started',
          ['daemon-reload'] in sequence and ['enable', service.UNITS['recover']] in sequence
          and ['enable', '--now', service.UNITS['dashboard']] in sequence
          and not any(s[:2] == ['enable', '--now'] and s[2] == service.UNITS['recover'] for s in sequence), sequence)
    check('the dashboard answers', linux['reachable'] is True, linux)
    check('a refresh keeps the Linux port and language',
          service._option(service._systemd_read('dashboard')[1]['ProgramArguments'], '--port') == str(linux_port)
          and service.install(odd, platform='linux')['url'].endswith(':%d/' % linux_port)
          and service._option(service._systemd_read('dashboard')[1]['ProgramArguments'], '--lang') == 'fr')
    service.uninstall('linux')
    check('uninstall removes both units', not list(unit_dir.glob('cosmos-orbit-*')))

    print('⑨ no service manager: refused with the commands to run at login by hand')
    reset()
    os.environ['PATH'] = str(EMPTY)
    for platform in ('linux', 'win32'):
        try:
            service.install(store_a, 55999, platform=platform)
            message = ''
        except ValueError as error:
            message = str(error)
        check(platform + ': refused with the dashboard and worker commands',
              'No login service manager' in message and 'dashboard --port 55999' in message and 'worker --idle-exit' in message,
              message)
    check('status on an unsupported platform says so', service.status('win32')['service'] == 'unsupported')
    os.environ['PATH'] = str(FAKES) + os.pathsep + REAL_PATH

    print('⑩ the command line')
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    cli = subprocess.run([sys.executable, str(ROOT / 'bin/cosmos-orbit'), '--home', str(store_a), 'service', 'status'],
                         capture_output=True, text=True, env=env, timeout=60)
    try:
        shown = json.loads(cli.stdout)
    except ValueError:
        shown = {}
    check('cosmos-orbit service status answers', cli.returncode == 0 and 'registered' in shown, cli.stderr or cli.stdout)

    print('⑪ a transient worker yields to a running one instead of failing')
    from harness.store import Store
    store = Store(store_a)
    with store.lock('worker'):
        started = time.time()
        worker = subprocess.run([sys.executable, str(ROOT / 'bin/cosmos-orbit'), '--home', str(store_a), 'worker',
                                 '--idle-exit', '30'], capture_output=True, text=True, env=env, timeout=60)
    store.close()
    check('exit 0 at once with "already running"', worker.returncode == 0 and 'already running' in worker.stdout
          and time.time() - started < 10, (worker.returncode, worker.stdout, worker.stderr))

    print('⑫ install.sh')
    usage = subprocess.run(['sh', str(ROOT / 'install.sh'), '--bogus'], capture_output=True, text=True, timeout=30)
    check('install.sh offers --service', usage.returncode == 2 and '--service' in usage.stderr, usage.stderr)
    syntax = subprocess.run(['sh', '-n', str(ROOT / 'install.sh')], capture_output=True, text=True, timeout=30)
    check('install.sh parses', syntax.returncode == 0, syntax.stderr)
    text = (ROOT / 'install.sh').read_text()
    check('install.sh registers with --service and otherwise keeps an existing registration current',
          'service install' in text and 'service install --if-installed' in text, '')
finally:
    reset()

print('\n%d failed' % len(FAILED) if FAILED else '\nall service checks passed')
sys.exit(1 if FAILED else 0)
