import json
import queue
import socket
import threading
import time

import pytest

from amr.models import RobotMode, RobotStatus, Step
from amr.tcp_json import TcpJsonDriver, decode_message, encode_step
from core.config import RobotConfig


class FakeAmrServer:
    """업체 AMR 흉내: 한 줄 JSON 을 받아 기록하고, task 명령이면 status + task done 으로 답한다."""

    def __init__(self, reply: str | None = 'done', close_after_task: bool = False, gate: threading.Event | None = None):
        self.reply = reply
        self.close_after_task = close_after_task
        self.gate = gate          # 주어지면 set() 될 때까지 답을 미룬다 (시험이 '진행 중' 상태를 확인할 수 있게)
        self.received: 'queue.Queue[dict]' = queue.Queue()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _send(self, conn, obj):
        conn.sendall((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))

    def _run(self):
        conn, _ = self.sock.accept()
        with conn:
            buf = b''
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    msg = json.loads(line.decode('utf-8'))
                    self.received.put(msg)
                    if msg.get('cmd') != 'task':
                        continue
                    if self.close_after_task:
                        return
                    if self.gate is not None:
                        self.gate.wait(timeout=3.0)
                    if self.reply:
                        # 상태 + 결과를 한 번에 (두 줄이 한 패킷에 붙어 와도 처리되는지)
                        conn.sendall((json.dumps({'type': 'status', 'x': 1.5, 'y': -1.17, 'battery': 88.0,
                                                  'node': msg['target'], 'mode': 'idle'}) + '\n' +
                                      json.dumps({'type': 'task', 'id': msg['id'], 'state': self.reply,
                                                  'error': '경로 막힘'}) + '\n').encode('utf-8'))

    def close(self):
        self.sock.close()


def wait_events(drv, kind, timeout=3.0):
    got = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got += [(e.kind, e.step_id, e.detail) for e in drv.poll()]
        if any(k == kind for k, _, _ in got):
            return got
        time.sleep(0.01)
    raise AssertionError(f'{kind} 이벤트가 오지 않음: {got}')


def cfg(port: int) -> RobotConfig:
    return RobotConfig(id='AMR-T1', driver='tcp_json', host='127.0.0.1', port=port)


# ---------------------------------------------------------------------------- 순수 함수
def test_encode_step():
    step = Step('O0001-S2', 'N03', 'drop', height_m=1.0, tray=2, ref='N-03-3', path=['N01', 'N02', 'N03'])
    msg = encode_step(step, 'AMR-T1')
    assert msg['cmd'] == 'task' and msg['id'] == 'O0001-S2' and msg['target'] == 'N03'
    assert msg['path'] == ['N01', 'N02', 'N03'] and msg['operation'] == 'drop'
    assert msg['height'] == 1.0 and msg['tray'] == 2 and msg['ref'] == 'N-03-3'
    json.dumps(msg)
    msg['path'].append('X')
    assert step.path == ['N01', 'N02', 'N03']      # 복사본


def test_decode_message():
    st = RobotStatus('AMR-T1', mode=RobotMode.OFFLINE, connected=False)
    evs = decode_message({'type': 'status', 'x': 1.0, 'y': 2.0, 'battery': 55, 'node': 'S04', 'mode': 'moving',
                          'theta': 0.3, 'fork': 0.8, 'trays': ['B1', None]}, 'AMR-T1', st)
    assert [e.kind for e in evs] == ['status']
    assert (st.x, st.y, st.battery, st.node, st.heading, st.fork_height) == (1.0, 2.0, 55.0, 'S04', 0.3, 0.8)
    assert st.mode == RobotMode.MOVING and st.connected and st.trays == ['B1', None]
    decode_message({'type': 'status', 'mode': 'CHARGING'}, 'AMR-T1', st)
    assert st.mode == RobotMode.CHARGING
    decode_message({'type': 'status', 'mode': '???'}, 'AMR-T1', st)
    assert st.mode == RobotMode.CHARGING           # 모르는 값은 유지

    (done,) = decode_message({'type': 'task', 'id': 'O1-S1', 'state': 'done'}, 'AMR-T1', st)
    assert (done.robot_id, done.kind, done.step_id) == ('AMR-T1', 'step_done', 'O1-S1')
    (failed,) = decode_message({'type': 'task', 'id': 'O1-S1', 'state': 'failed', 'error': '장애물'}, 'AMR-T1', st)
    assert failed.kind == 'step_failed' and failed.detail == '장애물'
    (failed2,) = decode_message({'type': 'task', 'id': 'O1-S1', 'state': 'failed'}, 'AMR-T1', st)
    assert failed2.detail                          # 사유 없으면 기본 문구
    (cancelled,) = decode_message({'type': 'task', 'id': 'O1-S1', 'state': 'cancelled'}, 'AMR-T1', st)
    assert cancelled.detail == 'cancelled'
    assert decode_message({'type': 'task', 'id': 'O1-S1', 'state': 'running'}, 'AMR-T1', st) == []
    (err,) = decode_message({'type': 'error', 'error': '모터 과열'}, 'AMR-T1', st)
    assert err.kind == 'error' and err.detail == '모터 과열'
    assert decode_message({'type': 'whatever'}, 'AMR-T1', st) == []


# ---------------------------------------------------------------------------- 드라이버
def test_round_trip_with_local_server():
    gate = threading.Event()
    server = FakeAmrServer(gate=gate)
    drv = TcpJsonDriver(cfg(server.port))
    try:
        assert drv.status().mode == RobotMode.OFFLINE
        drv.connect()
        assert drv.status().connected
        assert [e.kind for e in drv.poll()] == ['connected']
        step = Step('O0001-S1', 'S03', path=['S01', 'S02', 'S03'])
        drv.send_step(step)
        assert drv.busy
        with pytest.raises(RuntimeError):
            drv.send_step(Step('O0001-S2', 'S04'))
        gate.set()                                # 이제 AMR 이 '완료'를 답한다
        got = wait_events(drv, 'step_done')
        assert ('step_started', 'O0001-S1', '') in got and ('step_done', 'O0001-S1', '') in got
        assert got.index(('step_started', 'O0001-S1', '')) < got.index(('step_done', 'O0001-S1', ''))
        assert server.received.get(timeout=2) == encode_step(step, 'AMR-T1')
        assert not drv.busy
        st = drv.status()
        assert st.node == 'S03' and st.battery == 88.0 and st.mode == RobotMode.IDLE

        # 일시정지·취소·정지 명령도 전달
        drv.pause()
        assert server.received.get(timeout=2)['cmd'] == 'pause'
        server.reply = None                       # 이번엔 답하지 않음 → 진행 중으로 남음
        drv.send_step(Step('O0001-S3', 'S05'))
        assert server.received.get(timeout=2)['cmd'] == 'task'
        drv.cancel()
        assert server.received.get(timeout=2) == {'cmd': 'cancel', 'id': 'O0001-S3'}
        assert ('step_failed', 'O0001-S3', 'cancelled') in wait_events(drv, 'step_failed')
        drv.estop()
        assert server.received.get(timeout=2)['cmd'] == 'estop'
        assert drv.status().mode == RobotMode.ERROR
        drv.resume()
        assert server.received.get(timeout=2)['cmd'] == 'resume'
        assert drv.status().mode != RobotMode.ERROR
    finally:
        drv.disconnect()
        server.close()
    assert not drv.status().connected


def test_failed_task_maps_to_step_failed():
    server = FakeAmrServer(reply='failed')
    drv = TcpJsonDriver(cfg(server.port))
    try:
        drv.connect()
        drv.send_step(Step('O0002-S1', 'N01'))
        got = wait_events(drv, 'step_failed')
        assert ('step_failed', 'O0002-S1', '경로 막힘') in got
        assert not drv.busy
    finally:
        drv.disconnect()
        server.close()


def test_server_drop_fails_inflight_step():
    server = FakeAmrServer(close_after_task=True)
    drv = TcpJsonDriver(cfg(server.port))
    try:
        drv.connect()
        drv.send_step(Step('O0003-S1', 'N01'))
        got = wait_events(drv, 'disconnected')
        assert ('step_failed', 'O0003-S1', '연결 끊김') in got
        st = drv.status()
        assert not st.connected and st.mode == RobotMode.OFFLINE
        with pytest.raises(RuntimeError):
            drv.send_step(Step('O0003-S2', 'N01'))
    finally:
        drv.disconnect()
        server.close()


def test_connection_refused_raises_runtime_error():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()                                 # 아무도 듣지 않는 포트
    drv = TcpJsonDriver(cfg(port), connect_timeout=1.0)
    with pytest.raises(RuntimeError) as exc:
        drv.connect()
    assert '연결 실패' in str(exc.value)
    with pytest.raises(RuntimeError):
        TcpJsonDriver(RobotConfig(id='AMR-T2', driver='tcp_json')).connect()   # host·port 없음


def test_send_step_when_not_connected():
    drv = TcpJsonDriver(cfg(1))
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O0004-S1', 'N01'))
    assert not drv.busy
    drv.pause()     # 연결 없음: 조용히 무시
    drv.estop()
    assert drv.status().mode == RobotMode.OFFLINE
