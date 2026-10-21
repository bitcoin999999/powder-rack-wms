"""주 창: 상단 바 + 왼쪽 적재대 + 오른쪽(슬롯 상세 / 탭) + 상태 표시줄.

갱신 방식
- Controller 구독 콜백은 '무엇이 바뀌었는지'만 적어 둔다 (그리기 없음 — 자가 시험·고배속에서도 가볍게).
- 250 ms 타이머(_flush)가 모아 둔 변경을 반영한다: 바뀐 칸만 다시 그림, 표는 초당 최대 4번, 안 보이는 탭은 보일 때.
- 100 ms 타이머가 ctrl.tick(실제 경과 시간) 을 부른다. AMR 탭이 보일 때만 미니맵을 초당 10번 다시 그린다.
- 30 s 타이머: 경과 시간 글자(입고 후 몇 분) 갱신.

바코드 리더 (키보드처럼 입력)
- 글 입력칸이 아닌 곳(버튼·표·선택 상자 등)에 포커스가 있을 때 들어온 글자 키는 입고 탭 스캔 칸으로 보낸다
  (탭도 입고로 바꾼다). 그렇지 않으면 스캔이 사라지고, 스캔 글자 사이의 공백이 포커스 버튼을 눌러 버린다
  (예: [수동 출고] → 사유 칸에 나머지 글자 → Enter 로 출고 실행).
- 상단 바·알림·범례 버튼은 마우스 전용(포커스 안 가짐). 작업 뒤에는 입고 탭이면 스캔 칸으로 포커스를 돌려준다.
- Esc: 칸 선택 해제(적재대 요약) → 검색 지우기.
"""
from __future__ import annotations

import html
import logging
import time
from collections import deque
from pathlib import Path

from PyQt5.QtCore import QElapsedTimer, QEvent, Qt, QTimer
from PyQt5.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QSizePolicy, QSplitter,
                             QTabWidget, QVBoxLayout, QWidget)

from . import theme
from .dialogs import NoticeDialog
from .header import HeaderBar
from .rack_view import RackPanel
from .resources import app_icon
from .slot_detail import SlotDetailPanel
from .tab_amr import AmrTab
from .tab_history import HistoryTab
from .tab_inbound import InboundTab
from .tab_master import MasterTab
from .tab_outbound import OutboundTab
from .util import ask, install_excepthook, restore_excepthook, run_action, setup_logging
from .widgets import button, mouse_only, scan_key_text

log = logging.getLogger(__name__)

TICK_MS = 100
FLUSH_MS = 250
MAP_MS = 100
AGE_MS = 30_000
NOTICE_KEEP = 100
STOCK_KINDS = {'STORE', 'OUT', 'CORRECT', 'RESERVE', 'UNRESERVE', 'PICK', 'ERROR', 'ARRIVE'}


class MainWindow(QMainWindow):
    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self._stopped = False
        self._force_close = False
        setup_logging(ctrl.data_dir)
        install_excepthook(self._on_unhandled)
        theme.install(QApplication.instance())
        self.setWindowTitle('완분 적재대 관리 — 목업')
        icon = app_icon()
        self.setWindowIcon(icon)
        qapp = QApplication.instance()
        if qapp is not None and qapp.windowIcon().isNull():
            qapp.setWindowIcon(icon)          # 부모 없는 알림 상자·작업 표시줄도 같은 아이콘
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)

        # 변경 기록 (콜백 → _flush)
        self._dirty_slots: set[str] = set()
        self._dirty = {'rack_all': True, 'inv': True, 'stock': True, 'events': True, 'master': True, 'orders': True,
                       'config': False, 'notice': False}
        self._notices: deque[tuple[float, dict]] = deque(maxlen=NOTICE_KEEP)
        self._unread = 0
        self._last_error = ''
        self._stale_tabs: set[QWidget] = set()

        # ---- 화면 짜기
        root = QWidget()
        root.setObjectName('Root')
        root.setStyleSheet(f'QWidget#Root {{ background: {theme.BG}; }}')
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.header = HeaderBar()
        v.addWidget(self.header)
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(10, 10, 10, 8)
        bl.setSpacing(0)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.rack = RackPanel(ctrl)
        self.rack.setMinimumWidth(640)
        self.split.addWidget(self.rack)
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        self.slot_detail = SlotDetailPanel(self)
        right.addWidget(self.slot_detail)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        self.inbound = InboundTab(self)
        self.outbound = OutboundTab(self)
        self.amr_tab = AmrTab(self)
        self.history = HistoryTab(self)
        self.master = MasterTab(self)
        self.tabs.addTab(self.inbound, '입고')
        self.tabs.addTab(self.outbound, '출고 조회')
        self.tabs.addTab(self.amr_tab, 'AMR·작업')
        self.tabs.addTab(self.history, '이력')
        self.tabs.addTab(self.master, '기준정보')
        right.addWidget(self.tabs)
        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)
        right.setSizes([228, 620])
        self.right_split = right
        right.setMinimumWidth(500)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 55)
        self.split.setStretchFactor(1, 45)
        self.split.setSizes([550, 450])
        bl.addWidget(self.split)
        v.addWidget(body, 1)
        self.setCentralWidget(root)

        # 상태 표시줄
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.notice_label = QLabel('')
        self.notice_label.setTextFormat(Qt.RichText)
        self.notice_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.notice_label.setContentsMargins(8, 2, 8, 2)
        sb.addWidget(self.notice_label, 1)
        self.notice_btn = button('알림 0', 'flat', '최근 알림 100건 보기')
        mouse_only(self.notice_btn)
        sb.addPermanentWidget(self.notice_btn)
        self.notice_btn.clicked.connect(self.show_notices)

        # 신호
        self.rack.slot_selected.connect(self.select_slot)
        self.header.speed_changed.connect(self._set_speed)
        self.header.overbridge_toggled.connect(self._toggle_overbridge)
        self.header.dispatch_toggled.connect(self._toggle_dispatch)
        self.tabs.currentChanged.connect(self._tab_changed)
        # 글자 찾기를 하는 선택 상자(편집 불가)는 글자 키를 스캔 칸으로 넘긴다
        for cb in self.findChildren(QComboBox):
            if not cb.isEditable():
                cb.installEventFilter(self)

        # 구독 (시작 중 알림은 subscribe 할 때 다시 온다)
        ctrl.subscribe(self._on_ctrl)

        # 타이머
        self._clock = QElapsedTimer()
        self._clock.start()
        self.tick_timer = QTimer(self)
        self.tick_timer.setTimerType(Qt.PreciseTimer)
        self.tick_timer.timeout.connect(self._tick)
        self.flush_timer = QTimer(self)
        self.flush_timer.timeout.connect(self._flush)
        self.map_timer = QTimer(self)
        self.map_timer.timeout.connect(self._map_frame)
        self.age_timer = QTimer(self)
        self.age_timer.timeout.connect(self._ages)
        self.tick_timer.start(TICK_MS)
        self.flush_timer.start(FLUSH_MS)
        self.age_timer.start(AGE_MS)

        self.refresh_all()
        QTimer.singleShot(0, self.inbound.focus_scan)

    # ================================================================== Controller 이벤트 (가볍게)
    def _on_ctrl(self, kind: str, payload: dict) -> None:
        d = self._dirty
        if kind == 'tick':
            return
        if kind == 'notice':
            self._notices.append((time.time(), dict(payload)))
            self._unread += 1
            d['notice'] = True
            return
        if kind == 'order':
            d['orders'] = True
            return
        if kind == 'config':
            d['config'] = True
            if payload.get('rack_changed'):
                d['rack_all'] = True
            return
        # 재고 이벤트
        d['inv'] = True
        d['events'] = True
        if kind in STOCK_KINDS:
            d['stock'] = True
        if kind == 'MASTER':
            d['master'] = True
        if payload.get('reconfigured'):
            d['rack_all'] = True
            d['config'] = True
        for sid in payload.get('slots') or ():
            self._dirty_slots.add(sid)
        for key in ('slot', 'from_slot'):
            if payload.get(key):
                self._dirty_slots.add(payload[key])

    def _on_unhandled(self, text: str) -> None:
        self._notices.append((time.time(), {'text': f'내부 오류 — {text} (data\\app.log)', 'level': 'error'}))
        self._unread += 1
        self._dirty['notice'] = True

    # ================================================================== 타이머
    def _tick(self) -> None:
        if self._stopped:
            return
        dt = self._clock.restart() / 1000.0
        try:
            self.ctrl.tick(dt)
        except Exception:
            self._log_error('ctrl.tick 오류')

    def _flush(self) -> None:
        if self._stopped:
            return
        try:
            self._apply_changes()
        except Exception:   # 화면 갱신 오류가 이벤트 루프를 죽이지 않게 (기록만)
            self._log_error('화면 갱신 오류')

    def _log_error(self, what: str) -> None:
        """같은 오류가 타이머마다 되풀이돼도 기록·알림은 한 번."""
        import traceback
        text = traceback.format_exc()
        if text == self._last_error:
            return
        self._last_error = text
        log.error('%s\n%s', what, text)
        self._on_unhandled(what)

    def request_flush(self) -> None:
        """사용자 동작 직후: 다음 이벤트 루프에서 바로 반영."""
        QTimer.singleShot(0, self._flush)

    def _visible(self, tab: QWidget) -> bool:
        return self.tabs.currentWidget() is tab

    def _apply_changes(self) -> None:
        d = self._dirty
        if d['config']:
            d['config'] = False
            self.amr_tab.rebuild_cards()
            self.amr_tab.minimap.forget_boxes()
            self.master.refresh_settings()
            self.inbound.refresh_conveyor()
        if d['master']:
            d['master'] = False
            self.rack.reload_grades()
            self.inbound.refresh_grades()
            self.outbound.refresh_grades()
            self.history.refresh_grades()
            self.master.refresh_grades()
        if d['rack_all']:
            d['rack_all'] = False
            self._dirty_slots.clear()
            self.rack.reload()
            self.slot_detail.refresh()
        elif self._dirty_slots:
            sids, self._dirty_slots = self._dirty_slots, set()
            self.rack.update_slots(sids)
            if self.slot_detail.sid in sids:
                self.slot_detail.refresh()
        if d['inv']:
            d['inv'] = False
            self.inbound.refresh_conveyor()
            if self.slot_detail.sid is None:
                self.slot_detail.refresh()        # 적재대 요약
            self.amr_tab.minimap.forget_boxes()
            if self._visible(self.master):
                self.master.refresh_rack_state()
            else:
                self._stale_tabs.add(self.master)
        if d['stock']:
            d['stock'] = False
            if self._visible(self.outbound):
                self.outbound.refresh()
            else:
                self._stale_tabs.add(self.outbound)
        if d['events']:
            d['events'] = False
            if self._visible(self.history):
                self.history.add_new()
            else:
                self._stale_tabs.add(self.history)
        if d['orders']:
            if self._visible(self.amr_tab):
                d['orders'] = False
                self.amr_tab.refresh_orders()
        # 매번 (가벼움)
        self.header.update_from(self.ctrl)
        self.inbound.refresh_overbridge()
        if self._visible(self.amr_tab):
            self.amr_tab.refresh_robots()
            if not self.map_timer.isActive() and self.isVisible():
                self.map_timer.start(MAP_MS)
        self._refresh_status()

    def _map_frame(self) -> None:
        if self._stopped:
            return
        if self._visible(self.amr_tab) and self.isVisible():
            self.amr_tab.minimap.update()
        else:
            self.map_timer.stop()

    def _ages(self) -> None:
        if self._stopped:
            return
        try:
            self.rack.repaint_all()
            if self._visible(self.outbound):
                self.outbound.refresh()
            if self.slot_detail.sid:
                self.slot_detail.refresh()
        except Exception:
            self._log_error('경과 시간 갱신 오류')

    def _tab_changed(self, _index: int) -> None:
        w = self.tabs.currentWidget()
        try:
            if w in self._stale_tabs:
                self._stale_tabs.discard(w)
                if w is self.history:
                    self.history.add_new()
                elif w is self.outbound:
                    self.outbound.refresh_grades()
                    self.outbound.refresh()
                elif w is self.master:
                    self.master.refresh_rack_state()
                    self.master.refresh_grades()
            if w is self.amr_tab:
                self._dirty['orders'] = False
                self.amr_tab.refresh()
                if not self._stopped:
                    self.map_timer.start(MAP_MS)
            else:
                self.map_timer.stop()
            if w is self.inbound:
                self.inbound.focus_scan()
        except Exception:
            self._log_error('탭 전환 갱신 오류')

    # ================================================================== 공용 (탭에서 부름)
    def select_slot(self, sid: str | None) -> None:
        had = self.slot_detail.sid
        self.rack.set_selected(sid)
        self.slot_detail.set_slot(sid)
        if sid is None and had is not None:
            self._fit_summary()

    def restore_scan_focus(self) -> None:
        """작업 뒤 입고 탭이 보이면 스캔 칸으로 포커스 (다음 바코드가 바로 들어가게)."""
        if not self._stopped and self.tabs.currentWidget() is self.inbound:
            self.inbound.focus_scan()

    # ---- 바코드 리더 글자 → 스캔 칸
    def _redirect_to_scan(self, text: str) -> bool:
        scan = self.inbound.scan
        if self._stopped or not scan.isEnabled():
            return False
        if self.tabs.currentWidget() is not self.inbound:
            self.tabs.setCurrentWidget(self.inbound)
        scan.setFocus(Qt.OtherFocusReason)
        scan.insert(text)
        return True

    def keyPressEvent(self, ev) -> None:
        text = scan_key_text(ev)
        if text and self._redirect_to_scan(text):
            ev.accept()
            return
        if ev.key() == Qt.Key_Escape and not ev.modifiers():
            if self.slot_detail.sid is not None:
                self.select_slot(None)
                ev.accept()
                return
            if self.rack.model.searching:
                self.rack.clear_search()
                ev.accept()
                return
        super().keyPressEvent(ev)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.KeyPress and isinstance(obj, QComboBox) and not obj.isEditable():
            text = scan_key_text(ev)
            if text and self._redirect_to_scan(text):
                return True
        return super().eventFilter(obj, ev)

    def search_grade(self, code: str | None) -> None:
        self.rack.set_grade(code)

    def toast(self, text: str, level: str = 'info') -> None:
        self._notices.append((time.time(), {'text': text, 'level': level}))
        self._dirty['notice'] = True
        self._refresh_status(force_last=True)

    def show_notices(self) -> None:
        self._unread = 0
        self._dirty['notice'] = True
        self._refresh_status()
        NoticeDialog(self, list(self._notices)).exec_()
        self.restore_scan_focus()

    def _refresh_status(self, force_last: bool = False) -> None:
        if not (self._dirty['notice'] or force_last):
            return
        self._dirty['notice'] = False
        if self._notices:
            wall, payload = self._notices[-1]
            lvl = payload.get('level', 'info')
            color = theme.LEVEL_COLOR.get(lvl, theme.ACCENT)
            text_color = theme.ERROR if lvl == 'error' else '#8a5d0b' if lvl == 'warn' else theme.TEXT
            stamp = time.strftime('%H:%M:%S', time.localtime(wall))
            self.notice_label.setText(f'<span style="color:{color}; font-size:11pt;">●</span>&nbsp; '
                                      f'<span style="color:{theme.TEXT_3};">{stamp}</span>&nbsp; '
                                      f'<span style="color:{text_color};">{html.escape(payload.get("text", ""))}</span>')
            self.notice_label.setToolTip(payload.get('text', ''))
        else:
            self.notice_label.setText(f'<span style="color:{theme.TEXT_3};">알림 없음</span>')
        n = self._unread
        self.notice_btn.setText(f'알림 {n}' if n else '알림')
        self.notice_btn.setStyleSheet(f'color: {theme.ERROR}; font-weight: bold;' if n else '')

    # ================================================================== 상단 바 동작
    def _set_speed(self, speed: float) -> None:
        run_action(self, self.ctrl.set_sim_speed, speed)
        self.header.set_speed(self.ctrl.sim_speed)
        self.restore_scan_focus()

    def _toggle_overbridge(self, on: bool) -> None:
        if on:
            ok, _ = run_action(self, self.ctrl.start_overbridge)
            if ok:
                self.toast(f'오버브릿지 출고 시작 — {self.ctrl.config.sim.session_boxes}박스')
        else:
            run_action(self, self.ctrl.stop_overbridge)
            self.toast('오버브릿지 출고 정지')
        self.header.set_overbridge(self.ctrl.overbridge.running)
        self.inbound.refresh_overbridge()
        self.restore_scan_focus()

    def _toggle_dispatch(self, on: bool) -> None:
        run_action(self, self.ctrl.set_auto_dispatch, on)
        self.header.set_dispatch(self.ctrl.auto_dispatch)
        self.toast('자동 배차 켜짐' if self.ctrl.auto_dispatch else '자동 배차 꺼짐 — 새 입고 작업을 만들지 않습니다')
        self.restore_scan_focus()

    # ================================================================== 전체 다시 그리기
    def refresh_all(self) -> None:
        """현재 상태로 화면 전체를 다시 만든다 (main.py 가 자가 시험·캡처 뒤에 부른다)."""
        for k in self._dirty:
            self._dirty[k] = False
        self._dirty_slots.clear()
        self._stale_tabs.clear()
        self.rack.reload()
        self.slot_detail.refresh()
        self.inbound.refresh()
        self.outbound.refresh_grades()
        self.outbound.refresh()
        self.amr_tab.refresh()
        self.history.refresh_grades()
        self.history.reload()
        self.master.refresh()
        self.header.set_speed(self.ctrl.sim_speed)
        self.header.update_from(self.ctrl)
        self._dirty['notice'] = True
        self._refresh_status()

    # ================================================================== 처음 배치
    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if not getattr(self, '_split_done', False):
            self._split_done = True
            self._fit_screen()
            QTimer.singleShot(0, self.apply_split)

    def _fit_screen(self) -> None:
        """화면(배율 적용 후)이 최소 크기보다 작으면 최소 크기를 줄이고 최대화 (offscreen 캡처에서는 안 함)."""
        if QApplication.platformName().startswith('offscreen'):
            return
        screen = self.screen() if hasattr(self, 'screen') else None
        if screen is None:
            return
        avail = screen.availableGeometry()
        aw, ah = avail.width(), avail.height()
        if aw < 1280 or ah < 800:
            self.setMinimumSize(min(1280, aw), min(800, ah))
        if self.width() > aw or self.height() > ah:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)

    def ensure_detail_room(self) -> None:
        """칸 상세 내용이 늘어나면(칸 선택) 오른쪽 위를 그만큼 키운다 (아래 탭에서 가져옴)."""
        sizes = self.right_split.sizes()
        if len(sizes) != 2 or sum(sizes) <= 0:
            return
        total = sum(sizes)
        need = min(self.slot_detail.minimumSizeHint().height(), total - self.tabs.minimumSizeHint().height())
        if sizes[0] < need:
            self.right_split.setSizes([need, max(0, total - need)])

    def _fit_summary(self) -> None:
        """칸 선택을 풀어 요약으로 돌아오면 오른쪽 위를 요약 높이로 줄인다 (탭 쪽을 넓게)."""
        sizes = self.right_split.sizes()
        if len(sizes) != 2 or sum(sizes) <= 0:
            return
        total = sum(sizes)
        top = max(170, min(250, self.slot_detail.sizeHint().height()))
        if sizes[0] > top:
            self.right_split.setSizes([top, total - top])

    def apply_split(self) -> None:
        """왼쪽 적재대 55 : 오른쪽 45, 오른쪽 위 칸 상세는 내용 높이만큼."""
        total = self.split.width()
        if total > 200:
            left = int(total * 0.55)
            self.split.setSizes([left, total - left])
        rt = self.right_split.height()
        if rt > 200:
            top = max(170, min(250, self.slot_detail.sizeHint().height()))
            self.right_split.setSizes([top, rt - top])

    # ================================================================== 끝내기
    def stop(self) -> None:
        """타이머·구독을 멈춘다 (종료 전, 또는 Controller 를 닫기 전)."""
        if self._stopped:
            return
        self._stopped = True
        for t in (self.tick_timer, self.flush_timer, self.map_timer, self.age_timer):
            t.stop()
        for cb in self.findChildren(QComboBox):
            cb.removeEventFilter(self)
        try:
            self.ctrl.unsubscribe(self._on_ctrl)
        except Exception:
            pass
        restore_excepthook()

    def closeEvent(self, ev) -> None:
        if not self._force_close and not self._stopped:
            try:
                busy = [o for o in self.ctrl.fleet.orders(active_only=True) if o.kind != 'PARK']
            except Exception:
                busy = []
            if busy and not ask(self, '진행 중인 AMR 작업이 있습니다. 종료할까요?', '종료', default_yes=False):
                ev.ignore()
                return
        self.stop()
        ev.accept()

    def force_close(self) -> None:
        self._force_close = True
        self.close()

    # ================================================================== 개발용 캡처
    def dev_save_shots(self, out_dir: Path) -> list[Path]:
        """탭마다 창 캡처(shot_tab_<n>.png) + 재종 검색 캡처(shot_search.png). 위젯 grab 만 쓴다."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        app = QApplication.instance()
        saved: list[Path] = []
        cur = self.tabs.currentIndex()
        for i in range(self.tabs.count()):
            self.tabs.setCurrentIndex(i)
            self._flush()
            if self.tabs.widget(i) is self.outbound:
                snap = self.ctrl.inventory.snapshot()
                if snap.by_grade:
                    self.outbound.set_grade(max(snap.by_grade, key=snap.by_grade.get))
            if app is not None:
                app.processEvents()
            path = out_dir / f'shot_tab_{i + 1}.png'
            self.grab().save(str(path))
            saved.append(path)
        self.tabs.setCurrentIndex(cur)
        snap = self.ctrl.inventory.snapshot()
        if snap.by_grade:
            code = max(snap.by_grade, key=snap.by_grade.get)
            self.search_grade(code)
            rows = self.ctrl.inventory.find_grade(code)
            if rows:
                self.select_slot(rows[0].slot.id)
        if app is not None:
            app.processEvents()
        path = out_dir / 'shot_search.png'
        self.grab().save(str(path))
        saved.append(path)
        return saved
