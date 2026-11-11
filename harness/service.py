"""Start Orbit again at login, without a person: the dashboard as a kept-alive service, and one recovery pass.

Two definitions, on macOS as launchd LaunchAgents (``~/Library/LaunchAgents``), on Linux as systemd user units
(``~/.config/systemd/user``). Other platforms are refused with the commands to start at login by hand.

- ``dashboard`` runs ``cosmos-orbit dashboard --no-open`` on a fixed port. It starts at login and again whenever it
  exits. Observation never starts model work.
- ``recover`` runs ``cosmos-orbit worker --idle-exit 120`` once at login. It continues only work that was already
  authorised before the shutdown (a review a completion claim requested, a call the shutdown interrupted, an unattended
  task that was running) and exits when quiet. It is deliberately not a resident worker: Orbit's hooks, dashboard and
  ``start``/``resume`` already start a worker when work appears, and a resident one re-reads every task every 3 s
  (35 ms of CPU a pass with 55 tasks, measured 2026-11-11) to wait for work that is started on demand anyway.

Installing never runs the recovery pass. It is loaded at the next login, so installing or updating Orbit starts no
model work; the dashboard is started at once.

Both definitions carry the store (``--home``) and the ``PATH`` of the shell that installed them: a service manager
starts programs with a bare ``PATH``, where the provider CLIs the recovery pass runs are not found.
"""
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import time

LANGS = ('en', 'ja', 'de', 'es', 'fr', 'ko')
DEFAULT_PORT = 55050
DEFAULT_LANG = 'en'
RECOVER_IDLE_EXIT = 120
WAIT = 10.0          # seconds to wait for the dashboard to answer after it is (re)started
LABELS = {'dashboard': 'com.cosmos-orbit.dashboard', 'recover': 'com.cosmos-orbit.recover'}
UNITS = {'dashboard': 'cosmos-orbit-dashboard.service', 'recover': 'cosmos-orbit-recover.service'}
TIMEOUT = 15


def manager(platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return 'launchd'
    if platform.startswith('linux'):
        return 'systemd'
    return None


def _program():
    """The command that runs this installation: its console script when there is one, else this interpreter."""
    script = Path(sys.executable).with_name('cosmos-orbit')
    if script.is_file() and os.access(str(script), os.X_OK):
        return [str(script)], {}
    package_root = str(Path(__file__).resolve().parent.parent)
    return [sys.executable, '-m', 'harness.cli'], {'PYTHONPATH': package_root}


def _commands(home, port, lang):
    program, env = _program()
    base = program + ['--home', str(home)]
    return {'dashboard': base + ['dashboard', '--port', str(port), '--lang', lang, '--no-open'],
            'recover': base + ['worker', '--idle-exit', str(RECOVER_IDLE_EXIT)]}, env


def _option(arguments, name):
    if name in arguments[:-1]:
        return arguments[arguments.index(name) + 1]
    return None


def _run(argv):
    try:
        process = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=TIMEOUT)
    except FileNotFoundError:
        raise ValueError('%s is not available here' % argv[0])
    except subprocess.TimeoutExpired:
        raise ValueError('%s did not answer within %d s' % (' '.join(argv[:3]), TIMEOUT))
    return process


def _port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):
            return True
    except OSError:
        return False


def _wait_port(port, want_open, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if _port_open(port) == want_open:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.2)


def _check(port, lang):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('The dashboard service needs a fixed port between 1 and 65535')
    if lang not in LANGS:
        raise ValueError('Unknown dashboard language: %s' % lang)


def _by_hand(commands):
    return ('Start these at login with your system\'s own tool (an autostart entry, a scheduled task or cron @reboot): '
            + ' ; '.join(shlex.join(argv) for argv in commands.values()))


# ---------------------------------------------------------------------------------------------------- macOS: launchd

def _agents():
    return Path.home() / 'Library' / 'LaunchAgents'


def _domain():
    return 'gui/%d' % os.getuid()


def _launchd_read(name):
    path = _agents() / (LABELS[name] + '.plist')
    try:
        with path.open('rb') as handle:
            return path, plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return path, None


def _launchd_loaded(label):
    process = _run(['launchctl', 'print', _domain() + '/' + label])
    if process.returncode != 0:
        return None
    state = {}
    for line in process.stdout.splitlines():
        key, _, value = line.strip().partition(' = ')
        if key in ('state', 'pid', 'last exit code') and key not in state:
            state[key] = value.strip()
    return state


def _launchd_write(name, arguments, env, log, keep_alive):
    path = _agents() / (LABELS[name] + '.plist')
    definition = {'Label': LABELS[name], 'ProgramArguments': arguments, 'RunAtLoad': True,
                  'KeepAlive': keep_alive, 'StandardOutPath': str(log), 'StandardErrorPath': str(log),
                  'EnvironmentVariables': env, 'ProcessType': 'Background'}
    if keep_alive:
        definition['ThrottleInterval'] = 10
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as handle:
        plistlib.dump(definition, handle)
    os.chmod(str(temporary), 0o644)
    os.replace(str(temporary), str(path))
    return path


def _launchd_bootout(label):
    if _launchd_loaded(label) is None:
        return False
    _run(['launchctl', 'bootout', _domain() + '/' + label])
    for _ in range(25):                       # bootout returns before the job is gone; bootstrap would fail with EIO
        if _launchd_loaded(label) is None:
            return True
        time.sleep(.2)
    raise ValueError('launchd still holds %s after bootout' % label)


def _launchd_bootstrap(path):
    error = ''
    for _ in range(5):
        process = _run(['launchctl', 'bootstrap', _domain(), str(path)])
        if process.returncode == 0:
            return
        error = (process.stderr or process.stdout).strip()
        time.sleep(.5)
    raise ValueError('launchctl bootstrap %s failed: %s' % (path.name, error[:200]))


# ---------------------------------------------------------------------------------------------------- Linux: systemd

def _units():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd' / 'user'


def _systemd_quote(argument):
    escaped = argument.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return '"' + escaped + '"'


def _systemd_read(name):
    path = _units() / UNITS[name]
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return path, None
    for line in text.splitlines():
        if line.startswith('ExecStart='):
            return path, {'ProgramArguments': [part.replace('%%', '%').replace('$$', '$')
                                               for part in shlex.split(line[len('ExecStart='):])]}
    return path, {}


def _systemctl(*arguments):
    return _run(['systemctl', '--user'] + list(arguments))


def _systemd_write(name, arguments, env, log, keep_alive):
    path = _units() / UNITS[name]
    lines = ['# Written by cosmos-orbit service install; remove with cosmos-orbit service uninstall.',
             '[Unit]', 'Description=Cosmos Orbit ' + ('dashboard' if name == 'dashboard' else 'recovery pass at login'), '',
             '[Service]', 'Type=simple', 'ExecStart=' + ' '.join(_systemd_quote(part) for part in arguments)]
    lines += ['Environment=' + _systemd_quote(key + '=' + value) for key, value in sorted(env.items())]
    lines += ['Restart=always', 'RestartSec=10'] if keep_alive else ['Restart=no']
    lines += ['StandardOutput=append:' + str(log), 'StandardError=append:' + str(log), '',
              '[Install]', 'WantedBy=default.target', '']
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    os.replace(str(temporary), str(path))
    return path


def _systemd_available():
    if not shutil.which('systemctl'):
        return False
    return _systemctl('show-environment').returncode == 0


# ---------------------------------------------------------------------------------------------------- commands

def _read(kind, name):
    return _launchd_read(name) if kind == 'launchd' else _systemd_read(name)


def registered(platform=None):
    kind = manager(platform)
    if kind is None:
        return False
    return any(_read(kind, name)[1] is not None for name in LABELS)


def install(store_home, port=None, lang=None, home_explicit=True, platform=None, if_installed=False):
    """Write both definitions, start the dashboard now and load the recovery pass for the next login.

    Without ``port``/``lang`` the registered values are kept (else 55050 and en); without an explicit home the
    registered store is kept, so ``install.sh`` refreshing a registration never moves it to another store."""
    kind = manager(platform)
    previous = []
    if kind:
        previous = (_read(kind, 'dashboard')[1] or {}).get('ProgramArguments') or []
    if if_installed and not (kind and registered(platform)):
        return {'service': 'not registered', 'changed': False,
                'note': 'Nothing to keep current; register with cosmos-orbit service install'}
    port = port if port is not None else int(_option(previous, '--port') or DEFAULT_PORT)
    lang = lang or _option(previous, '--lang') or DEFAULT_LANG
    _check(port, lang)
    home = Path(store_home if home_explicit or not _option(previous, '--home') else _option(previous, '--home'))
    commands, extra_env = _commands(home, port, lang)
    if kind is None or (kind == 'systemd' and not _systemd_available()):
        raise ValueError('No login service manager here (macOS launchd or Linux systemd --user). ' + _by_hand(commands))
    env = dict(extra_env, PATH=os.environ.get('PATH') or os.defpath)
    home.mkdir(parents=True, exist_ok=True)
    logs = {name: home / ('service-%s.log' % name) for name in LABELS}
    written = {}
    if kind == 'launchd':
        _launchd_bootout(LABELS['dashboard'])
        if not _wait_port(port, False, 3):
            raise ValueError('Port %d is in use by another process (a dashboard started by hand?). Stop it or choose '
                             '--port, then run cosmos-orbit service install again.' % port)
        _launchd_bootout(LABELS['recover'])          # an old definition must not linger in launchd; reloaded at login
        written['dashboard'] = _launchd_write('dashboard', commands['dashboard'], env, logs['dashboard'], True)
        written['recover'] = _launchd_write('recover', commands['recover'], env, logs['recover'], False)
        _launchd_bootstrap(written['dashboard'])
    else:
        _systemctl('stop', UNITS['dashboard'])
        if not _wait_port(port, False, 3):
            raise ValueError('Port %d is in use by another process (a dashboard started by hand?). Stop it or choose '
                             '--port, then run cosmos-orbit service install again.' % port)
        written['dashboard'] = _systemd_write('dashboard', commands['dashboard'], env, logs['dashboard'], True)
        written['recover'] = _systemd_write('recover', commands['recover'], env, logs['recover'], False)
        for argv in (['daemon-reload'], ['enable', UNITS['recover']], ['enable', '--now', UNITS['dashboard']]):
            process = _systemctl(*argv)
            if process.returncode != 0:
                raise ValueError('systemctl --user %s failed: %s' % (' '.join(argv), (process.stderr or '').strip()[:200]))
    reachable = _wait_port(port, True, WAIT)
    result = status(platform)
    result.update(installed=True, url='http://127.0.0.1:%d/' % port, reachable=reachable,
                  note='The dashboard runs now and at every login; the recovery pass runs once at the next login '
                       'and continues only work authorised before the shutdown.')
    if not reachable:
        result['log'] = str(logs['dashboard'])
    return result


def uninstall(platform=None):
    kind = manager(platform)
    if kind is None:
        return {'service': 'none', 'removed': []}
    removed = []
    for name in LABELS:
        if kind == 'launchd':
            _launchd_bootout(LABELS[name])
        else:
            _systemctl('disable', '--now', UNITS[name])
        path, definition = _read(kind, name)
        if definition is not None or path.exists():
            path.unlink()
            removed.append(str(path))
    if kind == 'systemd' and shutil.which('systemctl'):
        _systemctl('daemon-reload')
    return {'service': kind, 'removed': removed}


def status(platform=None):
    kind = manager(platform)
    if kind is None:
        return {'service': 'unsupported', 'platform': platform or sys.platform}
    services, port = {}, None
    for name in LABELS:
        path, definition = _read(kind, name)
        arguments = (definition or {}).get('ProgramArguments') or []
        entry = {'file': str(path), 'installed': definition is not None, 'command': arguments}
        if name == 'dashboard' and arguments:
            port = int(_option(arguments, '--port') or 0) or None
        if kind == 'launchd':
            if shutil.which('launchctl'):
                state = _launchd_loaded(LABELS[name])
                entry.update(loaded=state is not None, state=(state or {}).get('state'),
                             pid=(state or {}).get('pid'), last_exit=(state or {}).get('last exit code'))
        elif shutil.which('systemctl'):
            entry.update(enabled=_systemctl('is-enabled', UNITS[name]).stdout.strip() or None,
                         state=_systemctl('is-active', UNITS[name]).stdout.strip() or None)
        services[name] = entry
    result = {'service': kind, 'registered': any(entry['installed'] for entry in services.values()),
              'services': services}
    if port:
        result.update(url='http://127.0.0.1:%d/' % port, reachable=_port_open(port))
    return result
