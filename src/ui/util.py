"""화면 공용 도우미: 시간·글자 표기, 사용자 동작 실행(오류 → 알림 상자), 예외 기록(data/app.log)."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
import traceback
from pathlib import Path

from PyQt5.QtCore import QLibraryInfo, Qt, QThread, QTranslator
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget

from core.inventory import InventoryError

log = logging.getLogger('ui')

TITLE = '완분 적재대 관리'


# ---------------------------------------------------------------- 표기
def parse_ts(text: str | None) -> float | None:
    """'YYYY-MM-DD HH:MM:SS' (로컬) → epoch 초."""
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text, '%Y-%m-%d %H:%M:%S'))
    except (ValueError, OverflowError):
        return None


def age_s(ts_text: str | None, now: float | None = None) -> float | None:
    t = parse_ts(ts_text)
    if t is None:
        return None
    return max(0.0, (time.time() if now is None else now) - t)


def fmt_age(seconds: float | None) -> str:
    """경과 시간 짧게: 방금 / 12분 / 3시간 / 2일."""
    if seconds is None:
        return '-'
    s = int(seconds)
    if s < 60:
        return '방금'
    if s < 3600:
        return f'{s // 60}분'
    if s < 86400 * 2:
        h, m = divmod(s // 60, 60)
        return f'{h}시간' if h >= 10 or m == 0 else f'{h}시간 {m}분'
    return f'{s // 86400}일'


def fmt_sim(t: float | None) -> str:
    """시뮬레이션 시각(s) → H:MM:SS."""
    if t is None:
        return '-'
    s = int(max(0.0, t))
    h, rem = divmod(s, 3600)
    return f'{h}:{rem // 60:02d}:{rem % 60:02d}'


def short_ts(text: str | None) -> str:
    """'2026-10-20 13:02:11' → '10-20 13:02:11'."""
    if not text:
        return ''
    return text[5:] if len(text) >= 19 else text


def compact_ts(text: str | None) -> str:
    """오늘이면 'HH:MM:SS', 아니면 'MM-DD HH:MM' (표의 좁은 시각 열)."""
    if not text or len(text) < 19:
        return text or ''
    if text[:10] == time.strftime('%Y-%m-%d'):
        return text[11:19]
    return f'{text[5:10]} {text[11:16]}'


def lot_tail(lot: str | None, n: int = 4) -> str:
    """로트 끝자리 (예: 0001015661578 → 1578)."""
    if not lot:
        return ''
    tail = lot.replace(' ', '').split('-')[-1]
    return tail[-n:] if len(tail) >= n else lot[-n:]


def fmt_kg(w: float | None) -> str:
    if w is None:
        return ''
    return f'{w:g} kg'


# ---------------------------------------------------------------- 사용자 동작
def warn(parent: QWidget | None, text: str, title: str = '확인') -> None:
    QMessageBox.warning(parent, title, text)


def info(parent: QWidget | None, text: str, title: str = TITLE) -> None:
    QMessageBox.information(parent, title, text)


def ask(parent: QWidget | None, text: str, title: str = '확인', default_yes: bool = False) -> bool:
    default = QMessageBox.Yes if default_yes else QMessageBox.No
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, default) == QMessageBox.Yes


def run_action(parent: QWidget | None, fn, *args, **kwargs):
    """사용자 동작 실행. InventoryError → 경고 상자(메시지 그대로), 그 밖의 예외 → 기록 + 오류 상자.
    (성공 여부, 결과) 를 돌려준다. 이벤트 루프를 죽이지 않는다."""
    try:
        return True, fn(*args, **kwargs)
    except InventoryError as exc:
        warn(parent, str(exc))
    except Exception as exc:   # 예상 못 한 오류: 기록하고 알린다
        show_unexpected(parent, exc)
    return False, None


def show_unexpected(parent: QWidget | None, exc: BaseException) -> None:
    log.error('화면 동작 오류', exc_info=(type(exc), exc, exc.__traceback__))
    QMessageBox.critical(parent, '오류', f'처리 중 오류가 났습니다.\n\n{type(exc).__name__}: {exc}\n\n'
                                       '자세한 내용은 data\\app.log 에 기록했습니다.')


# ---------------------------------------------------------------- Qt 기본 문구 한국어
_translators: list = []


def install_korean_qt(app) -> bool:
    """Qt 기본 대화상자·메뉴 문구(Yes/No/OK/Cancel, 색 고르기, 붙여넣기 등)를 한국어로 (qtbase_ko.qm).
    PyQt5 에 들어 있는 번역 파일을 쓴다 (PyInstaller 도 Qt5/translations 로 함께 묶는다). 없으면 영어 그대로."""
    if app is None or _translators:
        return bool(_translators)
    folder = QLibraryInfo.location(QLibraryInfo.TranslationsPath)
    for name in ('qtbase_ko', 'qt_ko'):
        tr = QTranslator(app)
        if tr.load(name, folder):
            app.installTranslator(tr)
            _translators.append(tr)
            break
    return bool(_translators)


def _can_show_box() -> bool:
    """알림 상자를 띄워도 되는가: 앱이 살아 있고, 이벤트 루프 안이고, 화면 없는(offscreen) 실행이 아닐 때.
    루프 밖(시작·종료 중)이나 offscreen 에서 QMessageBox 를 띄우면 접근 위반으로 프로그램이 죽는다."""
    app = QApplication.instance()
    if app is None:
        return False
    if QApplication.platformName() in ('offscreen', 'minimal'):
        return False
    try:
        return QThread.currentThread().loopLevel() > 0
    except Exception:
        return False


# ---------------------------------------------------------------- 예외 기록
_handler: logging.Handler | None = None
_prev_hook = None
_hook_shown = False
_hook_box: QMessageBox | None = None


def setup_logging(data_dir: Path) -> Path:
    """data/app.log 에 경고 이상 기록 (여러 번 불러도 처리기 하나)."""
    global _handler
    path = Path(data_dir) / 'app.log'
    root = logging.getLogger()
    if _handler is not None and getattr(_handler, 'baseFilename', None) == str(path.resolve()):
        return path
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
        _handler = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    except OSError:
        return path
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root.addHandler(h)
    if root.level > logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.WARNING)
    _handler = h
    return path


def install_excepthook(on_error=None) -> None:
    """처리 안 된 예외: 기록 + 첫 번째 것만 알림 상자(모달 아님 — 이벤트 루프를 막지 않는다).
    PyQt5 는 excepthook 이 없으면 슬롯 예외에서 프로그램을 끝내 버리므로 반드시 건다."""
    global _prev_hook
    if _prev_hook is None:
        _prev_hook = sys.excepthook

    def hook(etype, value, tb):
        global _hook_shown, _hook_box
        if issubclass(etype, KeyboardInterrupt):
            if _prev_hook:
                _prev_hook(etype, value, tb)
            return
        text = ''.join(traceback.format_exception(etype, value, tb))
        log.error('처리 안 된 예외:\n%s', text)
        if on_error is not None:
            try:
                on_error(f'{etype.__name__}: {value}')
            except Exception:
                pass
        if not _can_show_box():
            sys.__excepthook__(etype, value, tb)      # 콘솔에라도 남긴다 (상자를 띄우면 죽는 상황)
            return
        if not _hook_shown:
            _hook_shown = True
            try:
                box = QMessageBox(QMessageBox.Critical, '오류',
                                  f'예상하지 못한 오류가 났습니다. 프로그램은 계속 동작합니다.\n\n{etype.__name__}: {value}\n\n'
                                  '자세한 내용은 data\\app.log 에 기록했습니다. (같은 오류는 다시 띄우지 않습니다)')
                box.setWindowModality(Qt.NonModal)
                box.setAttribute(Qt.WA_DeleteOnClose, True)
                box.show()
                _hook_box = box
            except Exception:
                pass

    sys.excepthook = hook


def restore_excepthook() -> None:
    global _prev_hook
    if _prev_hook is not None:
        sys.excepthook = _prev_hook
        _prev_hook = None
