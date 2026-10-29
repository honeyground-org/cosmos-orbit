"""터미널과 뷰가 공유하는 실행 상태 설명. 기록된 사실만 투영한다."""

PHASES = {'received': '접수', 'ready': '실행 대기', 'planning': '계획', 'running': '실행',
          'applying': '변경 적용', 'verifying': '검증', 'repairing': '수정',
          'waiting_peer': '협업 응답', 'needs_review': '결과 검토',
          'completed': '검증 완료', 'already_satisfied': '기존 결과 확인'}


def describe(task, worker_running):
    state = task['status']
    events = task['events']
    latest = events[0] if events else None
    progress = task.get('objective_progress') or {}
    phase = PHASES.get(state, PHASES.get(task['recorded_status'],
                       ['접수', '계획', '실행', '변경 적용', '검증', '결과'][task['stage']]))
    tone, title = 'idle', '대기'
    reason = task.get('reason') or ''
    next_action = progress.get('next_action') or ''
    if state in ('completed', 'already_satisfied'):
        tone, title = 'complete', '검증 완료'
        reason = '기록된 완료 조건을 확인했습니다.'
        next_action = next_action or '결과와 검증 근거를 확인할 수 있습니다.'
    elif state == 'needs_review':
        tone, title = 'review', '결과 준비'
        reason = '실행과 고정 검사가 끝났습니다. 전체 목적 충족 여부는 결과와 남은 작업으로 확인합니다.'
        next_action = next_action or '최근 응답과 결과를 확인하세요. 추가 지시는 같은 세션으로 전달됩니다.'
    elif state == 'needs_input':
        tone, title = 'waiting', '입력 대기'
        if (task.get('execution_plan') or {}).get('state') == 'awaiting_review':
            title, phase = '계획 승인 대기', '계획'
            reason = '이 작업에서 선택한 계획 검토 옵션에 따라 실행 전에 기다리고 있습니다.'
            next_action = '계획을 승인하거나 수정 지시를 보내세요.'
        else:
            reason = reason or '작업을 이어가는 데 필요한 입력을 기다리고 있습니다.'
            next_action = next_action or '아래 요청 사항에 답하거나 작업 지시를 보완하세요.'
    elif state in ('blocked', 'interrupted', 'unattended', 'stale'):
        tone = 'problem'
        title = {'blocked': '진행 차단', 'interrupted': '실행 중단', 'unattended': '실행자 없음', 'stale': '검증 이후 변경'}[state]
        if latest and latest['kind'] == 'budget_reached':
            title = '실행 예산 소진'
        if state == 'unattended':
            reason = '실행 상태가 기록되어 있으나 현재 작업을 소유한 실행자가 없습니다.'
        reason = reason or '저장된 상태와 실행 기록을 확인해야 합니다.'
        next_action = next_action or '남은 파일과 기록을 확인한 뒤 재개 지시를 보내세요.'
    elif state in ('paused', 'pause_pending'):
        tone, title = 'waiting', '일시정지' if state == 'paused' else '일시정지 예약'
        reason = '사용자 요청으로 정지했습니다.' if state == 'paused' else '현재 호출의 안전한 경계에서 정지합니다.'
        next_action = '계속 버튼으로 정지를 해제할 수 있습니다.'
    elif state == 'waiting_peer':
        tone, title = 'waiting', '협업 응답 대기'
        waiting = [m['target'] for m in task.get('messages', []) if not m.get('reply')]
        reason = (' · '.join(dict.fromkeys(waiting)) + ' 세션의 응답을 기다리고 있습니다.') if waiting else '기록된 협업 요청의 후속 처리를 기다리고 있습니다.'
        next_action = next_action or '응답이 도착하면 같은 작업에 반영합니다.'
    elif task['busy']:
        tone, title = 'active', '실행 중'
        reason = '하네스 실행자가 이 작업을 처리하고 있습니다.'
        if latest and latest['kind'] == 'provider_tool_started':
            reason = (latest.get('tool') or '도구') + ' 응답을 기다리고 있습니다.'
        elif latest and latest['kind'] == 'check_started':
            reason = '고정 검증 명령을 실행하고 결과를 기다리고 있습니다.'
        elif task.get('runtime'):
            reason = '모델 호출이 진행 중입니다. 새 이벤트가 없다는 사실만으로 정지라고 판단하지 않습니다.'
        next_action = next_action or ('계획을 저장한 뒤 설정된 실행 정책에 따라 진행합니다.' if state == 'planning' else '응답 수신 → 고정 검증 → 남은 작업 또는 결과로 전환합니다.')
    else:
        tone, title = 'waiting', '실행 대기' if worker_running else '워커 대기'
        reason = '워커가 작업을 가져갈 차례를 기다립니다.' if worker_running else '워커가 꺼져 있어 접수된 작업을 실행할 수 없습니다.'
        next_action = next_action or ('워커가 자동으로 이어갑니다.' if worker_running else '같은 저장소 설정으로 cosmos-orbit worker를 실행하세요.')
    return {'phase': phase, 'tone': tone, 'title': title, 'reason': reason,
            'next_action': next_action, 'last_activity': latest,
            'worker_running': worker_running}
