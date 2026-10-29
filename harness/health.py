"""유휴 엔진/세션의 로컬 준비 상태를 비동기로 점검한다. 모델 호출은 하지 않는다."""
import datetime
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

TTL = 30
VALID_FOR = 45
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='orbit-health')
_LOCK = threading.Lock()
_CACHE = {}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cached(key, check):
    """관측 요청을 막지 않고 점검을 병합한다. 대시보드 폴링은 결과만 읽는다."""
    with _LOCK:
        entry = _CACHE.setdefault(key, {'value': None, 'expires': 0, 'pending': False})
        if time.monotonic() >= entry['expires'] and not entry['pending']:
            entry['pending'] = True
            def run():
                try:
                    value = check()
                except Exception:
                    value = {'status': 'warning', 'detail': '로컬 상태를 확인하지 못했습니다.'}
                value.update(checked_at=_now(), valid_for_seconds=VALID_FOR)
                with _LOCK:
                    entry.update(value=value, expires=time.monotonic() + TTL, pending=False)
            _POOL.submit(run)
        value = dict(entry['value'] or {'status': 'checking', 'checked_at': None,
                                      'valid_for_seconds': VALID_FOR, 'detail': '로컬 상태 점검 중'})
        value['refreshing'] = entry['pending']
        if value.get('checked_at'):
            age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(value['checked_at'])).total_seconds()
            value['stale'] = age > VALID_FOR
            if value['stale']:
                value.update(status='checking', detail='이전 점검이 만료되어 새 확인을 기다리고 있습니다.')
        # 오래된 세션 캐시가 대시보드 실행 기간 내내 쌓이지 않게 한다.
        if len(_CACHE) > 512:
            stale = [k for k, v in _CACHE.items() if k != key and not v['pending'] and v['expires'] < time.monotonic()]
            for old in stale[:128]:
                _CACHE.pop(old, None)
        return value


def _engine(provider):
    executable = shutil.which(provider) if provider in ('codex', 'claude') else None
    if not executable:
        return {'status': 'unavailable', 'version': None, 'detail': '엔진 실행 파일을 찾을 수 없습니다.', 'remote': 'unverified'}
    process = subprocess.Popen([executable, '--version'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return {'status': 'warning', 'version': None, 'detail': '엔진의 로컬 응답 확인 시간이 초과됐습니다.', 'remote': 'unverified'}
    if process.returncode:
        return {'status': 'warning', 'version': None, 'detail': '엔진 로컬 확인 명령이 실패했습니다.', 'remote': 'unverified'}
    version = output.decode('utf-8', 'replace').strip().splitlines()
    return {'status': 'ready', 'version': version[0][:160] if version else None,
            'detail': '엔진 로컬 응답 확인. 로그인·원격 API 상태는 다음 실제 호출에서 확인합니다.', 'remote': 'unverified'}


def _record(provider, session_id, workspace, root):
    available = Path(workspace).is_dir()
    result = {'workspace_available': available, 'record': 'unbound',
              'connection': 'on_demand', 'remote': 'unverified'}
    if not available:
        return dict(result, status='unavailable', detail='작업 공간에 접근할 수 없습니다.')
    if not session_id:
        return dict(result, status='warning', detail='아직 연결된 세션 기록이 없습니다. 첫 실행 때 생성합니다.')
    # 자기 세션 파일의 메타데이터만 확인한다. 다른 대화나 인증 파일을 읽지 않는다.
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
        return dict(result, status='warning', detail='저장된 세션 식별자를 확인할 수 없습니다.')
    pattern = '*/*/*/*' + session_id + '*.jsonl' if provider == 'codex' else '*/' + session_id + '.jsonl'
    result['record'] = 'missing'
    try:
        for path in Path(root).glob(pattern):
            if path.is_file() and path.stat().st_size > 0 and os.access(path, os.R_OK):
                return dict(result, status='ready', record='found', detail='작업 공간과 저장된 세션 기록을 확인했습니다. 원격 연결은 호출할 때 열립니다.')
    except OSError:
        pass
    return dict(result, status='warning', detail='세션 ID는 저장돼 있으나 엔진의 세션 파일을 확인하지 못했습니다. 재개 성공은 미확인입니다.')


def project_health(home, tasks):
    """projected tasks의 session.health와 대시보드 공통 health를 구성한다."""
    roots = {'codex': str(Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'sessions'),
             'claude': str(Path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')).expanduser() / 'projects')}
    providers = {'codex', 'claude'} | {s['provider'] for t in tasks for s in t['sessions']}
    engines = {p: _cached(('engine', p, os.environ.get('PATH', '')), lambda p=p: _engine(p)) for p in providers}
    for task in tasks:
        for session in task['sessions']:
            provider, session_id = session['provider'], session.get('id')
            root = roots.get(provider)
            if root is None:
                health = {'status': 'warning', 'checked_at': None, 'valid_for_seconds': VALID_FOR,
                          'detail': '이 엔진의 로컬 점검을 지원하지 않습니다.', 'record': 'unbound'}
            else:
                health = _cached(('record', str(home), provider, session_id, task['workspace'], root),
                                 lambda p=provider, sid=session_id, w=task['workspace'], r=root: _record(p, sid, w, r))
            engine = engines[provider]
            health['record_status'] = health['status']
            if engine['status'] != 'ready':
                health['status'] = engine['status']
                health['detail'] = engine['detail']
            # 신선도는 엔진 확인과 세션 확인 중 더 오래된 쪽으로 제한한다.
            checked = [x.get('checked_at') for x in (health, engine)]
            health['checked_at'] = min(checked) if all(checked) else None
            health.update(remote='unverified', connection='on_demand')
            session['health'] = health
    return {'checked_at': _now(), 'engines': engines,
            'policy': '30초마다 로컬 엔진 응답·작업 공간·자기 세션 파일을 확인합니다. 모델 호출이나 원격 상시 연결 점검은 하지 않습니다.'}
