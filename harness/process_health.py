"""실행 소유자가 관측하는 프로세스 상태. 무출력은 장애 판정이 아니다."""
import subprocess
import time
from .store import now


class Monitor:
    def __init__(self, process, output, error, emit, call_id, interval=5):
        self.process, self.output, self.error = process, output, error
        self.emit, self.call_id, self.interval = emit, call_id, interval
        self.started = self.last_change = time.monotonic()
        self.next_sample, self.previous = 0, (0, 0)

    def sample(self, force=False):
        clock = time.monotonic()
        if not force and clock < self.next_sample:
            return
        self.next_sample = clock + self.interval
        sizes = tuple(p.stat().st_size if p and p.exists() else 0 for p in (self.output, self.error))
        changed = sizes != self.previous
        if changed:
            self.last_change = clock
        code = self.process.poll()
        row = {'call_id': self.call_id, 'pid': self.process.pid, 'checked_at': now(),
               'alive': code is None, 'exit_code': code, 'elapsed_seconds': int(clock-self.started),
               'stdout_bytes': sizes[0], 'stderr_bytes': sizes[1],
               'stdout_changed': sizes[0] != self.previous[0], 'stderr_changed': sizes[1] != self.previous[1],
               'output_silent_seconds': int(clock-self.last_change),
               'remote_status': 'unverified', 'progress_status': 'unverified',
               'status': 'exited' if code is not None else 'alive_output' if changed else 'alive_silent',
               'os_states': [], 'cpu_percent': None, 'group_process_count': None,
               'child_process_count': None, 'tcp_states': None, 'connection_status': 'unverified'}
        self.previous = sizes
        try:
            # 진단 명령만 짧게 제한한다. 진단 실패/무출력으로 대상 작업을 종료하지 않는다.
            result = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat=,pcpu='],
                                    capture_output=True, text=True, timeout=2)
            if result.returncode:
                raise ValueError('ps failed')
            members = [line.split() for line in result.stdout.splitlines()]
            members = [r for r in members if len(r) == 4 and r[1] == str(self.process.pid)]
            row.update(os_states=[r[2] for r in members], group_process_count=len(members),
                       child_process_count=sum(r[0] != str(self.process.pid) for r in members),
                       cpu_percent=round(sum(float(r[3]) for r in members), 2))
            if members and code is None:
                try:
                    sockets = subprocess.run(['lsof', '-nP', '-a', '-p', ','.join(r[0] for r in members),
                                              '-iTCP', '-FT'], capture_output=True, text=True, timeout=2)
                    if sockets.returncode in (0, 1):
                        states = [v[4:] for v in sockets.stdout.splitlines() if v.startswith('TST=')]
                        row['tcp_states'] = {state: states.count(state) for state in set(states)}
                        row['connection_status'] = 'tcp_observed' if states else 'no_tcp_observed'
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if code is None and any('T' in r[2] for r in members):
                row['status'] = 'process_stopped'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            row['probe_error'] = 'process_details_unavailable'
        self.emit('process_observation', row)
        return row
