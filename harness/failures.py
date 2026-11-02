"""사용자에게 보일 중단 사유와 원본 진단 정보를 분리한다."""
from .store import now


def describe(data):
    reason = data.get('reason', '')
    if reason.startswith('TimeoutExpired:'):
        return '이전 시간 제한 정책으로 호출이 중단됐습니다. 현재 정책은 경과 시간만으로 작업을 종료하지 않습니다.'
    return reason


def record(task, error):
    data = task['data']
    data['failure'] = {'type': type(error).__name__, 'detail': str(error), 'at': now(),
                       'phase': data.get('development', {}).get('phase', task['status'])}
    phase = '관리자 평가' if data['failure']['phase'] == 'evaluating' else '작업 실행'
    data['reason'] = (phase + '이 중단됐습니다. 프로세스 관측과 기술 정보를 확인한 뒤 재개 여부를 결정하세요.')
    if isinstance(error, KeyboardInterrupt):
        data['reason'] = '실행 중단 요청을 받았습니다. 저장된 작업 상태를 확인한 뒤 재개할 수 있습니다.'


def details(data):
    if data.get('failure'):
        return data['failure']
    if data.get('reason', '').startswith('TimeoutExpired:'):
        return {'type': 'TimeoutExpired', 'detail': data['reason'], 'historical': True}
    return None
