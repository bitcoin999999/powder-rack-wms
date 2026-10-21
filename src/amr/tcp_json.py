"""업체 TCP/JSON API 용 드라이버 템플릿. 계약: 공개 시그니처 유지.

저가 AMR 은 업체 고유 API (TCP 소켓 JSON, HTTP REST, ROS2 등)를 주는 경우가 많다. 이 파일을 복사해
encode_step / decode_message 두 함수만 업체 문서대로 바꾸면 FleetManager 에 붙는다.
기본 구현: 한 줄에 JSON 하나 (\n 구분). 받는 쪽은 별도 스레드, 이벤트는 _emit 으로.
'# TODO(업체)' 표시가 업체 문서로 채울 곳.

구현 메모
- step_started 는 보내기 직전에 낸다 (AMR 응답이 아주 빨라도 started → done 순서가 지켜지게).
- 연결이 끊기면 진행 중 단계는 step_failed('연결 끊김') — 다시 붙어도 그 작업 결과를 알 방법이 없기 때문.
- 수신 메시지 해석 오류(값 형식 등)는 'error' 이벤트만 내고 무시한다. 상태는 메시지 전체가 해석됐을 때만 바뀐다.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import replace

from core.config import RobotConfig

from .driver import AmrDriver
from .models import DriverEvent, RobotMode, RobotStatus, Step

# 업체 상태 문자열 → RobotMode. 표준 이름(IDLE, MOVING …)은 그대로 통한다.
_VENDOR_MODE = {  # TODO(업체): 업체 문서의 상태 코드로 바꾸기
    'idle': RobotMode.IDLE, 'ready': RobotMode.IDLE,
    'moving': RobotMode.MOVING, 'running': RobotMode.MOVING,
    'loading': RobotMode.LOADING, 'unloading': RobotMode.UNLOADING,
    'charging': RobotMode.CHARGING, 'paused': RobotMode.PAUSED,
    'error': RobotMode.ERROR, 'fault': RobotMode.ERROR, 'estop': RobotMode.ERROR,
}


def encode_step(step: Step, robot_id: str) -> dict:
    """Step → 업체 명령 JSON. 기본 형식(예시): {'cmd': 'task', 'id': step.id, 'target': step.node, 'path': [...],
    'operation': step.action, 'height': step.height_m, 'tray': step.tray, 'ref': step.ref}  # TODO(업체)"""
    return {
        'cmd': 'task',                 # TODO(업체): 작업 명령 이름
        'robot': robot_id,             # TODO(업체): 업체가 시리얼 번호를 요구하면 바꾸기 (필요 없으면 지워도 됨)
        'id': step.id,
        'target': step.node,           # TODO(업체): 업체 지도 위치 이름과 다르면 변환표 적용
        'path': list(step.path),
        'operation': step.action,      # TODO(업체): none/pick/drop/charge → 업체 동작 코드
        'height': step.height_m,       # TODO(업체): 단위 (m / mm) 확인
        'tray': step.tray,
        'ref': step.ref,
    }


def decode_message(msg: dict, robot_id: str, status: RobotStatus) -> list[DriverEvent]:
    """업체 수신 JSON → 이벤트 목록 + status 갱신. 기본 형식(예시):
    {'type': 'status', 'x':…, 'y':…, 'battery':…, 'node':…, 'mode':…} / {'type': 'task', 'id':…, 'state': 'done'|'failed'|'running', 'error':…}
    # TODO(업체)"""
    kind = msg.get('type')   # TODO(업체): 메시지 종류 필드 이름
    if kind == 'status':
        # TODO(업체): 필드 이름·단위 매핑
        for key, attr in (('x', 'x'), ('y', 'y'), ('battery', 'battery')):
            if key in msg:
                setattr(status, attr, float(msg[key]))
        if 'theta' in msg or 'heading' in msg:
            status.heading = float(msg.get('theta', msg.get('heading', 0.0)))
        if 'fork' in msg:
            status.fork_height = float(msg['fork'])
        if 'node' in msg:
            status.node = str(msg['node'] or '')
        if 'trays' in msg and isinstance(msg['trays'], list):
            status.trays = list(msg['trays'])
        if 'mode' in msg:
            status.mode = _parse_mode(msg['mode'], status.mode)
        if 'message' in msg:
            status.message = str(msg['message'])
        status.connected = True
        return [DriverEvent(robot_id, 'status')]
    if kind == 'task':
        step_id = msg.get('id')
        state = str(msg.get('state', '')).lower()   # TODO(업체): 작업 상태 값
        if state == 'done':
            return [DriverEvent(robot_id, 'step_done', step_id)]
        if state == 'failed':
            return [DriverEvent(robot_id, 'step_failed', step_id, str(msg.get('error') or '업체 AMR 작업 실패'))]
        if state == 'cancelled':
            return [DriverEvent(robot_id, 'step_failed', step_id, 'cancelled')]
        return []   # 'running' 등: step_started 는 send_step 에서 이미 냈다
    if kind == 'error':
        return [DriverEvent(robot_id, 'error', None, str(msg.get('error') or msg.get('message') or ''))]
    return []       # 모르는 메시지는 무시


def _parse_mode(value, default: RobotMode) -> RobotMode:
    text = str(value)
    try:
        return RobotMode(text.upper())
    except ValueError:
        return _VENDOR_MODE.get(text.lower(), default)


class TcpJsonDriver(AmrDriver):
    kind = 'tcp_json'

    def __init__(self, cfg: RobotConfig, connect_timeout: float = 3.0):
        super().__init__(cfg.id)
        self.cfg = cfg
        self._timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()         # 상태·진행 단계 (수신 스레드 ↔ 메인 스레드)
        self._send_lock = threading.Lock()
        self._status = RobotStatus(cfg.id, mode=RobotMode.OFFLINE, node=cfg.start_node, connected=False)
        self._step: Step | None = None
        self._estopped = False
        self._closing = False

    # ------------------------------------------------------------------ 연결
    def connect(self) -> None:
        if self._sock is not None:
            return
        host, port = self.cfg.host, self.cfg.port
        if not host or not port:
            raise RuntimeError(f'{self.robot_id}: TCP 접속 정보(host·port)가 설정에 없습니다')
        try:
            sock = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as exc:
            raise RuntimeError(f'{self.robot_id}: AMR TCP 연결 실패 ({host}:{port}) — 주소·포트·AMR 전원을 '
                               f'확인하세요 ({exc})') from exc
        sock.settimeout(None)
        self._closing = False
        self._sock = sock
        with self._lock:
            self._status.connected = True
            if self._status.mode == RobotMode.OFFLINE:
                self._status.mode = RobotMode.IDLE
        self._reader = threading.Thread(target=self._read_loop, args=(sock,), name=f'tcp-{self.robot_id}',
                                        daemon=True)
        self._reader.start()
        self._emit('connected')
        # TODO(업체): 로그인·상태 구독 명령이 필요하면 여기서 self._send({...})

    def disconnect(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        self._closing = True
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        self._reader = None
        with self._lock:
            step, self._step = self._step, None
            self._status.connected = False
            self._status.mode = RobotMode.OFFLINE
        if step is not None:
            self._emit('step_failed', step.id, '연결 끊김')
        self._emit('disconnected')

    # ------------------------------------------------------------------ 명령
    def send_step(self, step: Step) -> None:
        try:   # 업체 매핑 오류는 아무 상태도 바꾸기 전에 거절 (드라이버가 busy 로 남지 않게)
            payload = encode_step(step, self.robot_id)
        except Exception as exc:
            raise RuntimeError(f'{self.robot_id}: 명령 변환 실패 ({step.id}) — {exc}') from exc
        with self._lock:
            if self._sock is None or not self._status.connected:
                raise RuntimeError(f'{self.robot_id}: AMR 에 TCP 연결되어 있지 않습니다 (connect 먼저)')
            if self._step is not None:
                raise RuntimeError(f'{self.robot_id}: 이미 진행 중인 단계가 있습니다 ({self._step.id})')
            self._step = step
        self._emit('step_started', step.id)
        try:
            self._send(payload)
        except Exception:
            with self._lock:
                if self._step is step:
                    self._step = None
            raise

    def pause(self) -> None:
        self._send_quiet({'cmd': 'pause'})             # TODO(업체)

    def resume(self) -> None:
        self._estopped = False
        self._send_quiet({'cmd': 'resume'})            # TODO(업체)

    def cancel(self) -> None:
        with self._lock:
            step, self._step = self._step, None
        if step is not None:
            self._send_quiet({'cmd': 'cancel', 'id': step.id})   # TODO(업체)
            self._emit('step_failed', step.id, 'cancelled')

    def estop(self) -> None:
        self._estopped = True
        with self._lock:
            step, self._step = self._step, None
        self._send_quiet({'cmd': 'estop'})             # TODO(업체): 하드웨어 비상정지와 별개인 소프트 정지
        if step is not None:
            self._emit('step_failed', step.id, 'estop')

    # ------------------------------------------------------------------ 상태
    def status(self) -> RobotStatus:
        with self._lock:
            st = replace(self._status, trays=list(self._status.trays))
        if self._estopped:
            st.mode = RobotMode.ERROR
            st.message = '비상정지'
        if not st.connected:
            st.mode = RobotMode.OFFLINE
        return st

    @property
    def busy(self) -> bool:
        return self._step is not None

    # ------------------------------------------------------------------ 내부
    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise RuntimeError(f'{self.robot_id}: AMR 에 TCP 연결되어 있지 않습니다')
            try:
                sock.sendall(data)
            except OSError as exc:
                raise RuntimeError(f'{self.robot_id}: AMR 로 보내기 실패 — {exc}') from exc

    def _send_quiet(self, obj: dict) -> None:
        """일시정지·취소·정지 명령: 연결이 없으면 로컬 상태만 바꾸고 조용히 넘어간다."""
        if self._sock is None:
            return
        try:
            self._send(obj)
        except RuntimeError as exc:
            self._emit('error', detail=str(exc))

    def _read_loop(self, sock: socket.socket) -> None:
        buf = b''
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)
        except OSError:
            pass
        finally:
            if not self._closing:   # AMR 쪽에서 끊김
                with self._lock:
                    step, self._step = self._step, None
                    self._status.connected = False
                    self._status.mode = RobotMode.OFFLINE
                if self._sock is sock:
                    self._sock = None
                sock.close()
                if step is not None:
                    self._emit('step_failed', step.id, '연결 끊김')
                self._emit('disconnected', detail='AMR 쪽에서 연결이 끊어졌습니다')

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            self._emit('error', detail=f'수신 데이터 해석 실패: {line[:80]!r}')
            return
        if not isinstance(msg, dict):
            return
        with self._lock:
            # 사본에 적용하고 성공했을 때만 바꾼다: 값 하나가 잘못돼도 상태가 반쯤 바뀌거나 수신 스레드가 죽지 않게
            st = replace(self._status, trays=list(self._status.trays))
            try:
                events = decode_message(msg, self.robot_id, st)
            except Exception as exc:   # 업체 매핑 코드의 어떤 오류도 연결을 끊지 않는다
                events, problem = None, exc
            else:
                self._status = st
                for ev in events:
                    if ev.kind in ('step_done', 'step_failed') and self._step is not None and ev.step_id == self._step.id:
                        self._step = None
        if events is None:
            self._emit('error', detail=f'수신 메시지 형식 오류: {problem} ({line[:80]!r})')
            return
        for ev in events:
            self._emit(ev.kind, ev.step_id, ev.detail)
