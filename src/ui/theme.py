"""화면 색·글꼴·스타일시트 (터미널형 어두운 톤 — 검정 바탕, 호박색 강조, 숫자는 고정폭). 색은 여기 한 곳에서만 정한다."""
from __future__ import annotations

from PyQt5.QtGui import QColor

# ---------------------------------------------------------------- 기본 색
BG = '#000000'          # 창 바탕
PANEL = '#0c0d0f'       # 패널
PANEL_2 = '#15171a'     # 패널 안 칸 (요약 타일·AMR 카드·입력칸)
BORDER = '#2a2e33'      # 패널 테두리
BORDER_STRONG = '#3e444b'
HEADER = '#000000'      # 상단 바
HEADER_2 = '#17191c'    # 상단 바 안 구역
ACCENT = '#ff9f1a'      # 강조 (호박색)
ACCENT_DARK = '#ffb547' # 강조 글자 (어두운 바탕에서 읽히는 밝은 호박색)
ACCENT_SOFT = '#33220a'
WARN = '#ffd23f'        # 경고 (노랑)
WARN_SOFT = '#2e2608'
ERROR = '#ff5252'       # 오류
ERROR_SOFT = '#3a1214'
OK = '#3ccf6e'          # 정상·충전 (초록)
TEXT = '#f0ede8'
TEXT_2 = '#aaa49b'      # 보조 글자
TEXT_3 = '#6f6a64'      # 흐린 글자
CELL_BASE = '#08090a'   # 재종 칸 바탕을 섞는 바닥색
EMPTY_CELL = '#121417'  # 빈 칸
EMPTY_ID = '#5c636a'    # 빈 칸 안 칸 번호 (흐리지만 읽히게)
RESERVED_CELL = '#101317'
CELL_SUB = '#c9c3b9'    # 재종 칸 둘째 줄
RESIDUAL = '#ffd23f'    # 잔량 박스 글자
ROW_ALT = '#111316'
SELECT = '#3a2608'
SELECT_OUTLINE = '#ffffff'
GRID = '#1b1e22'        # 셀 지도 바닥 격자
LANE = '#24282c'        # 셀 지도 통로
MUTED_FILL = '#1a1d21'  # 셀 지도 적재대·컨베이어 몸체
NODE = '#5d646b'
NODE_2 = '#4a5057'

FONT = 'Malgun Gothic'
MONO = 'Consolas'       # 숫자·코드 (한글은 맑은 고딕으로 대체 표시)

# ISO 513 분류 색 (재종 색과 같은 계열)
ISO_COLOR = {'P': '#3d84e6', 'M': '#e6b000', 'K': '#e04848', 'N': '#36b25a', 'S': '#d8722a', 'H': '#8a9098'}

# AMR 모드 색 (미니맵·카드)
MODE_COLOR = {
    'IDLE': '#7d858c', 'MOVING': ACCENT, 'LOADING': '#3d8bfd', 'UNLOADING': '#3d8bfd', 'WAITING': WARN,
    'CHARGING': OK, 'PAUSED': WARN, 'ERROR': ERROR, 'OFFLINE': '#4f555b',
}
LEVEL_COLOR = {'info': ACCENT, 'warn': WARN, 'error': ERROR}
ORDER_STATE_COLOR = {'QUEUED': TEXT_2, 'ASSIGNED': '#3d8bfd', 'RUNNING': ACCENT_DARK, 'DONE': TEXT_3,
                     'FAILED': ERROR, 'CANCELLED': '#c99a3c'}
SLOT_STATE_COLOR = {'EMPTY': '#6f6a64', 'RESERVED': ACCENT, 'OCCUPIED': '#3d84e6', 'BLOCKED': '#8a9098'}


def qc(hex_color: str, alpha: int | None = None) -> QColor:
    c = QColor(hex_color)
    if not c.isValid():
        c = QColor('#9aa5ab')
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    """a 와 b 를 섞는다 (t=0 → a, t=1 → b)."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t), round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


def tint(hex_color: str, amount: float = 0.74) -> QColor:
    """재종 색을 어두운 바닥색 쪽으로 낮춘 칸 바탕 (밝은 글자가 잘 읽히게)."""
    return mix(qc(hex_color), QColor(CELL_BASE), amount)


def text_on(color: QColor) -> QColor:
    """배경 위에 읽히는 글자색 (흰색 / 검정)."""
    lum = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor('#ffffff') if lum < 150 else QColor('#0b0c0d')


STYLESHEET = f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: #1b1d20; color: {TEXT}; border: 1px solid {ACCENT}; padding: 6px 8px; }}

QFrame#Panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 2px; }}
QLabel#PanelTitle {{ font-weight: bold; font-size: 11pt; color: {ACCENT_DARK}; }}
QLabel#Muted {{ color: {TEXT_2}; }}
QLabel#Faint {{ color: {TEXT_3}; }}
QLabel#Big {{ font-size: 16pt; font-weight: bold; font-family: {MONO}; }}
QLabel#FieldName {{ color: {TEXT_2}; }}
QLabel#FieldValue {{ color: {TEXT}; font-weight: bold; }}

/* 상단 바 */
QFrame#Header {{ background: {HEADER}; border: none; border-bottom: 1px solid {ACCENT}; }}
QFrame#Header QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#AppTitle {{ font-size: 13pt; font-weight: bold; color: {ACCENT}; }}
QLabel#ModeBadge {{ background: {HEADER_2}; color: {ACCENT_DARK}; border: 1px solid {ACCENT}; border-radius: 2px;
                   padding: 2px 10px; font-size: 9pt; }}
QLabel#KpiName {{ color: {TEXT_3}; font-size: 8pt; }}
QLabel#KpiValue {{ color: {ACCENT_DARK}; font-size: 13pt; font-weight: bold; font-family: {MONO}; }}
QLabel#KpiWarn {{ color: {ERROR}; font-size: 13pt; font-weight: bold; font-family: {MONO}; }}
QFrame#HeaderSep {{ background: {BORDER}; border: none; }}
QFrame#Header QToolButton {{ color: {TEXT_2}; background: transparent; border: 1px solid {BORDER_STRONG};
                            padding: 3px 9px; font-weight: bold; font-family: {MONO}; }}
QFrame#Header QToolButton:hover {{ background: {HEADER_2}; color: {TEXT}; }}
QFrame#Header QToolButton:checked {{ background: {ACCENT}; color: #000000; border-color: {ACCENT}; }}
QFrame#Header QToolButton#SegFirst {{ border-top-left-radius: 2px; border-bottom-left-radius: 2px; }}
QFrame#Header QToolButton#SegLast {{ border-top-right-radius: 2px; border-bottom-right-radius: 2px; }}
QFrame#Header QPushButton {{ color: {TEXT}; background: {HEADER_2}; border: 1px solid {BORDER_STRONG}; border-radius: 2px;
                            padding: 4px 12px; font-weight: bold; }}
QFrame#Header QPushButton:hover {{ border-color: {ACCENT}; }}
QFrame#Header QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: #000000; }}
QFrame#Header QPushButton#ObRunning:checked {{ background: {WARN}; border-color: {WARN}; color: #000000; }}

/* 버튼 */
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER_STRONG}; border-radius: 2px; padding: 4px 12px;
              min-height: 20px; color: {TEXT}; }}
QPushButton:hover {{ background: #1d2024; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT}; }}
QPushButton:disabled {{ color: #4f555b; background: #0f1113; border-color: #22262a; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; color: #000000; border-color: {ACCENT}; font-weight: bold; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_DARK}; }}
QPushButton[kind="primary"]:disabled {{ background: #4a3412; border-color: #4a3412; color: #8a7350; }}
QPushButton[kind="danger"] {{ background: {ERROR}; color: #000000; border-color: {ERROR}; font-weight: bold; }}
QPushButton[kind="danger"]:hover {{ background: #ff7a7a; }}
QPushButton[kind="warn"] {{ background: {WARN_SOFT}; border-color: {WARN}; color: {WARN}; font-weight: bold; }}
QPushButton[kind="warn"]:hover {{ background: #3d3210; }}
QPushButton[kind="flat"] {{ background: transparent; border: none; color: {ACCENT_DARK}; padding: 2px 6px; }}
QPushButton[kind="flat"]:hover {{ text-decoration: underline; }}
QPushButton[kind="chip"] {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 2px; padding: 1px 8px;
                           min-height: 16px; text-align: left; font-size: 9pt; font-family: {MONO}; }}
QPushButton[kind="chip"]:hover {{ border-color: {ACCENT}; }}
QPushButton[kind="chip"]:checked {{ border: 1px solid {ACCENT}; background: {ACCENT_SOFT}; color: {ACCENT_DARK}; }}

/* 입력 */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {PANEL_2}; border: 1px solid {BORDER_STRONG};
    border-radius: 2px; padding: 3px 6px; min-height: 20px; color: {TEXT};
    selection-background-color: {ACCENT}; selection-color: #000000; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ background: #0f1113;
    color: #4f555b; border-color: #22262a; }}
QLineEdit#ScanEdit {{ font-size: 14pt; font-family: {MONO}; padding: 6px 10px; border: 2px solid {ACCENT};
                     border-radius: 2px; background: #000000; color: {ACCENT_DARK}; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url(rackui:down.png); width: 10px; height: 6px; }}
QComboBox::down-arrow:disabled {{ image: url(rackui:down_off.png); }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {ACCENT};
                              selection-background-color: {SELECT}; selection-color: {ACCENT_DARK}; outline: none; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; font-family: {MONO}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px;
    border: none; border-left: 1px solid {BORDER}; background: #1b1e22; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; border-left: 1px solid {BORDER}; border-top: 1px solid {BORDER}; background: #1b1e22; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {ACCENT_SOFT}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(rackui:up.png); width: 8px; height: 5px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(rackui:down_small.png); width: 8px; height: 5px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image: url(rackui:up_off.png); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: url(rackui:down_small_off.png); }}
QCheckBox {{ spacing: 6px; color: {TEXT}; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {BORDER_STRONG}; background: {PANEL_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* 표 */
QTableView {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 0px; gridline-color: #1c1f23;
             alternate-background-color: {ROW_ALT}; selection-background-color: {SELECT};
             selection-color: {ACCENT_DARK}; font-family: {MONO}; }}
QTableView::item {{ padding: 0px 4px; }}
QHeaderView::section {{ background: #000000; color: {ACCENT_DARK}; border: none; border-bottom: 1px solid {ACCENT};
                       border-right: 1px solid {BORDER}; padding: 4px 6px; font-weight: bold; }}
QTableCornerButton::section {{ background: #000000; border: none; }}

/* 탭 */
QTabWidget::pane {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 0px; top: -1px; }}
QTabBar::tab {{ background: #000000; color: {TEXT_2}; border: 1px solid {BORDER}; border-bottom: none;
               padding: 6px 14px; margin-right: 2px; border-radius: 0px; }}
QTabBar::tab:selected {{ background: {PANEL}; color: {ACCENT}; font-weight: bold; border-top: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: #121417; }}

QGroupBox {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 0px; margin-top: 12px;
            padding: 10px 8px 8px 8px; font-weight: bold; color: {TEXT}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT_DARK}; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}

QStatusBar {{ background: #000000; border-top: 1px solid {BORDER}; color: {TEXT}; }}
QStatusBar::item {{ border: none; }}

QProgressBar {{ background: #1b1e22; border: none; border-radius: 0px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {OK}; border-radius: 0px; }}
QProgressBar[level="warn"]::chunk {{ background: {WARN}; }}
QProgressBar[level="error"]::chunk {{ background: {ERROR}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: #000000; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a3f45; border-radius: 0px; min-height: 24px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar:horizontal {{ background: #000000; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #3a3f45; border-radius: 0px; min-width: 24px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 0px; font-family: {MONO}; }}
QListWidget::item {{ padding: 4px 6px; border-bottom: 1px solid #1c1f23; }}
QListWidget::item:selected {{ background: {SELECT}; color: {ACCENT_DARK}; }}
QMessageBox {{ background: {PANEL}; }}
QMessageBox QLabel {{ min-width: 280px; color: {TEXT}; }}
QMenu {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {ACCENT}; }}
QMenu::item:selected {{ background: {SELECT}; color: {ACCENT_DARK}; }}
"""


def _arrow_images(folder) -> None:
    """콤보·스핀 화살표 그림 (스타일시트가 쓰는 rackui:*.png). 1배·2배 크기."""
    from PyQt5.QtCore import QPointF, Qt as _Qt
    from PyQt5.QtGui import QPainter, QPixmap, QPolygonF
    shapes = {'down': (10, 6, True), 'down_small': (8, 5, True), 'up': (8, 5, False)}
    for name, (w, h, down) in shapes.items():
        for suffix, color in (('', TEXT_2), ('_off', '#4f555b')):
            for scale, tag in ((1, ''), (2, '@2x')):
                pm = QPixmap(w * scale, h * scale)
                pm.fill(_Qt.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.Antialiasing)
                p.setPen(_Qt.NoPen)
                p.setBrush(qc(color))
                W, H = w * scale, h * scale
                if down:
                    pts = [QPointF(0.5, 0.5), QPointF(W - 0.5, 0.5), QPointF(W / 2, H - 0.5)]
                else:
                    pts = [QPointF(0.5, H - 0.5), QPointF(W - 0.5, H - 0.5), QPointF(W / 2, 0.5)]
                p.drawPolygon(QPolygonF(pts))
                p.end()
                pm.save(str(folder / f'{name}{suffix}{tag}.png'))


def install(app) -> None:
    """스타일시트 + 화살표 그림 경로(rackui:) 등록. QApplication 이 있어야 한다."""
    import tempfile
    from pathlib import Path
    from PyQt5.QtCore import QDir
    folder = Path(tempfile.gettempdir()) / 'rack-ui-arrows-dark'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'down_small_off@2x.png').exists():
            _arrow_images(folder)
        QDir.setSearchPaths('rackui', [str(folder)])
    except OSError:
        pass
    if app is not None:
        app.setStyleSheet(STYLESHEET)
