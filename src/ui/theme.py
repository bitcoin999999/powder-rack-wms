"""화면 색·글꼴·스타일시트 (밝은 산업용 톤). 색은 여기 한 곳에서만 정한다."""
from __future__ import annotations

from PyQt5.QtGui import QColor

# ---------------------------------------------------------------- 기본 색
BG = '#eef2f4'          # 창 바탕
PANEL = '#ffffff'       # 패널
BORDER = '#d5dde2'      # 패널 테두리
BORDER_STRONG = '#b9c6ce'
HEADER = '#18303d'      # 상단 바 (짙은 남색)
HEADER_2 = '#21414f'    # 상단 바 안 구역
ACCENT = '#1da994'      # 강조 (청록)
ACCENT_DARK = '#15806f'
ACCENT_SOFT = '#d6f1ec'
WARN = '#d2a450'        # 경고 (호박색)
WARN_SOFT = '#f8eed9'
ERROR = '#d64545'       # 오류
ERROR_SOFT = '#fbe3e3'
TEXT = '#1d2a31'
TEXT_2 = '#5c6b73'      # 보조 글자
TEXT_3 = '#8a979e'      # 흐린 글자
EMPTY_CELL = '#e3e8eb'  # 빈 칸
EMPTY_ID = '#7d8a92'    # 빈 칸 안 칸 번호 (흐리지만 읽히게, 대비 2.9:1)
CELL_SUB = '#46555d'    # 재종 색 칸 안 둘째 줄 (짙은 재종 바탕에서도 대비 4.8:1 이상)
RESIDUAL = '#704b06'    # 잔량 박스 글자 (재종 바탕에서 대비 4.8:1 이상)
ROW_ALT = '#f7f9fa'
SELECT = '#cfeee8'

FONT = 'Malgun Gothic'

# ISO 513 분류 색 (재종 색과 같은 계열)
ISO_COLOR = {'P': '#2f74d6', 'M': '#d9a400', 'K': '#d64545', 'N': '#2e9d4e', 'S': '#c8641e', 'H': '#7a7f87'}


def iso_text(iso_class: str) -> str:
    """ISO 분류 표시 글자. '-' 는 바코드로 자동 추가된 미분류 재종."""
    return '미분류' if iso_class == '-' else iso_class


# AMR 모드 색 (미니맵·카드)
MODE_COLOR = {
    'IDLE': '#6b7d87', 'MOVING': ACCENT, 'LOADING': '#2f74d6', 'UNLOADING': '#2f74d6', 'WAITING': WARN,
    'CHARGING': '#2e9d4e', 'PAUSED': WARN, 'ERROR': ERROR, 'OFFLINE': '#a0a8ad',
}
LEVEL_COLOR = {'info': ACCENT, 'warn': WARN, 'error': ERROR}
ORDER_STATE_COLOR = {'QUEUED': TEXT_2, 'ASSIGNED': '#2f74d6', 'RUNNING': ACCENT_DARK, 'DONE': TEXT_3,
                     'FAILED': ERROR, 'CANCELLED': '#a07a2c'}
SLOT_STATE_COLOR = {'EMPTY': '#8a979e', 'RESERVED': ACCENT, 'OCCUPIED': '#2f74d6', 'BLOCKED': '#7a7f87'}


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
    """재종 색을 흰색 쪽으로 밝힌 칸 바탕 (글자가 잘 읽히게)."""
    return mix(qc(hex_color), QColor('#ffffff'), amount)


def text_on(color: QColor) -> QColor:
    """배경 위에 읽히는 글자색 (흰색 / 짙은 글자)."""
    lum = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor('#ffffff') if lum < 150 else QColor(TEXT)


STYLESHEET = f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {HEADER}; color: #ffffff; border: none; padding: 6px 8px; }}

QFrame#Panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#PanelTitle {{ font-weight: bold; font-size: 11pt; color: {TEXT}; }}
QLabel#Muted {{ color: {TEXT_2}; }}
QLabel#Faint {{ color: {TEXT_3}; }}
QLabel#Big {{ font-size: 16pt; font-weight: bold; }}
QLabel#FieldName {{ color: {TEXT_2}; }}
QLabel#FieldValue {{ color: {TEXT}; font-weight: bold; }}

/* 상단 바 */
QFrame#Header {{ background: {HEADER}; border: none; }}
QFrame#Header QLabel {{ color: #ffffff; background: transparent; }}
QLabel#AppTitle {{ font-size: 13pt; font-weight: bold; }}
QLabel#ModeBadge {{ background: {HEADER_2}; color: #bfeee5; border: 1px solid {ACCENT}; border-radius: 10px;
                   padding: 2px 10px; font-size: 9pt; }}
QLabel#KpiName {{ color: #9fb4bf; font-size: 8pt; }}
QLabel#KpiValue {{ color: #ffffff; font-size: 12pt; font-weight: bold; }}
QLabel#KpiWarn {{ color: #ffd27a; font-size: 12pt; font-weight: bold; }}
QFrame#HeaderSep {{ background: #34505e; border: none; }}
QFrame#Header QToolButton {{ color: #dce8ee; background: transparent; border: 1px solid #47636f;
                            padding: 3px 9px; font-weight: bold; }}
QFrame#Header QToolButton:hover {{ background: {HEADER_2}; }}
QFrame#Header QToolButton:checked {{ background: {ACCENT}; color: #ffffff; border-color: {ACCENT}; }}
QFrame#Header QToolButton#SegFirst {{ border-top-left-radius: 4px; border-bottom-left-radius: 4px; }}
QFrame#Header QToolButton#SegLast {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px; }}
QFrame#Header QPushButton {{ color: #ffffff; background: {HEADER_2}; border: 1px solid #47636f; border-radius: 4px;
                            padding: 4px 12px; font-weight: bold; }}
QFrame#Header QPushButton:hover {{ background: #2a5163; }}
QFrame#Header QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QFrame#Header QPushButton#ObRunning:checked {{ background: {WARN}; border-color: {WARN}; color: {HEADER}; }}

/* 버튼 */
QPushButton {{ background: #ffffff; border: 1px solid #c3ced5; border-radius: 4px; padding: 4px 12px;
              min-height: 20px; color: {TEXT}; }}
QPushButton:hover {{ background: #f2f7f8; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: #e2eaee; }}
QPushButton:disabled {{ color: #a7b3ba; background: #f4f6f7; border-color: #dde3e7; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; color: #ffffff; border-color: {ACCENT}; font-weight: bold; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_DARK}; }}
QPushButton[kind="primary"]:disabled {{ background: #a9d9d0; border-color: #a9d9d0; color: #f4fbf9; }}
QPushButton[kind="danger"] {{ background: {ERROR}; color: #ffffff; border-color: {ERROR}; font-weight: bold; }}
QPushButton[kind="danger"]:hover {{ background: #b93636; }}
QPushButton[kind="warn"] {{ background: {WARN_SOFT}; border-color: {WARN}; color: #6b4d12; font-weight: bold; }}
QPushButton[kind="warn"]:hover {{ background: #f3e2bd; }}
QPushButton[kind="flat"] {{ background: transparent; border: none; color: {ACCENT_DARK}; padding: 2px 6px; }}
QPushButton[kind="flat"]:hover {{ text-decoration: underline; }}
QPushButton[kind="chip"] {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px; padding: 1px 8px;
                           min-height: 16px; text-align: left; font-size: 9pt; }}
QPushButton[kind="chip"]:hover {{ border-color: {ACCENT}; background: #f4fbfa; }}
QPushButton[kind="chip"]:checked {{ border: 2px solid {HEADER}; background: #eef6f5; }}

/* 입력 */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: #ffffff; border: 1px solid #c3ced5; border-radius: 4px;
    padding: 3px 6px; min-height: 20px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ background: #f4f6f7; color: #9aa6ad; }}
QLineEdit#ScanEdit {{ font-size: 14pt; padding: 6px 10px; border: 2px solid {ACCENT}; border-radius: 6px;
                     background: #fbfefd; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url(rackui:down.png); width: 10px; height: 6px; }}
QComboBox::down-arrow:disabled {{ image: url(rackui:down_off.png); }}
QComboBox QAbstractItemView {{ border: 1px solid {BORDER}; selection-background-color: {SELECT};
                              selection-color: {TEXT}; outline: none; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px;
    border: none; border-left: 1px solid #dde4e8; border-top-right-radius: 4px; background: #f5f8f9; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; border-left: 1px solid #dde4e8; border-top: 1px solid #e8edf0;
    border-bottom-right-radius: 4px; background: #f5f8f9; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {ACCENT_SOFT}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(rackui:up.png); width: 8px; height: 5px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(rackui:down_small.png); width: 8px; height: 5px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image: url(rackui:up_off.png); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: url(rackui:down_small_off.png); }}
QCheckBox {{ spacing: 6px; }}

/* 표 */
QTableView {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 4px; gridline-color: #edf1f3;
             alternate-background-color: {ROW_ALT}; selection-background-color: {SELECT}; selection-color: {TEXT}; }}
QTableView::item {{ padding: 0px 4px; }}
QHeaderView::section {{ background: #f1f4f6; color: {TEXT_2}; border: none; border-bottom: 1px solid {BORDER};
                       border-right: 1px solid #e6ebee; padding: 4px 6px; font-weight: bold; }}
QTableCornerButton::section {{ background: #f1f4f6; border: none; }}

/* 탭 */
QTabWidget::pane {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: #e3e9ec; color: {TEXT_2}; border: 1px solid {BORDER}; border-bottom: none;
               padding: 6px 14px; margin-right: 2px; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
QTabBar::tab:selected {{ background: {PANEL}; color: {TEXT}; font-weight: bold; border-top: 3px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ background: #edf2f4; }}

QGroupBox {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 6px; margin-top: 12px;
            padding: 10px 8px 8px 8px; font-weight: bold; color: {TEXT}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_2}; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}

QStatusBar {{ background: #ffffff; border-top: 1px solid {BORDER}; color: {TEXT}; }}
QStatusBar::item {{ border: none; }}

QProgressBar {{ background: #e6ecef; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar[level="warn"]::chunk {{ background: {WARN}; }}
QProgressBar[level="error"]::chunk {{ background: {ERROR}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #c7d1d7; border-radius: 4px; min-height: 24px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #c7d1d7; border-radius: 4px; min-width: 24px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QListWidget {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 4px; }}
QListWidget::item {{ padding: 4px 6px; border-bottom: 1px solid #eef2f4; }}
QMessageBox QLabel {{ min-width: 280px; }}
"""


def _arrow_images(folder) -> None:
    """콤보·스핀 화살표 그림 (스타일시트가 쓰는 rackui:*.png). 1배·2배 크기."""
    from PyQt5.QtCore import QPointF, Qt as _Qt
    from PyQt5.QtGui import QPainter, QPixmap, QPolygonF
    shapes = {'down': (10, 6, True), 'down_small': (8, 5, True), 'up': (8, 5, False)}
    for name, (w, h, down) in shapes.items():
        for suffix, color in (('', TEXT_2), ('_off', '#b9c3c9')):
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
    folder = Path(tempfile.gettempdir()) / 'rackwms-ui'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'down_small_off@2x.png').exists():
            _arrow_images(folder)
        QDir.setSearchPaths('rackui', [str(folder)])
    except OSError:
        pass
    if app is not None:
        app.setStyleSheet(STYLESHEET)
