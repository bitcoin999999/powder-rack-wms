"""그림·아이콘 파일 위치 — 개발 실행(프로젝트 폴더)과 PyInstaller onedir(sys._MEIPASS = _internal) 둘 다."""
from __future__ import annotations

import sys
from pathlib import Path


def base_path() -> Path:
    """자원 파일의 기준 폴더. exe 면 _internal(sys._MEIPASS), 개발 실행이면 프로젝트 폴더."""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]   # src/ui/resources.py → 프로젝트 폴더


def resource_path(rel: str) -> Path:
    """예) resource_path('assets/app.png'). exe 옆 폴더에 둔 파일도 찾는다 (없으면 기준 폴더 경로 그대로)."""
    p = base_path() / rel
    if p.exists():
        return p
    if getattr(sys, 'frozen', False):
        alt = Path(sys.executable).resolve().parent / rel
        if alt.exists():
            return alt
    return p


def app_icon():
    """창 아이콘 (assets/app.png, 없으면 app.ico, 둘 다 없으면 빈 아이콘)."""
    from PyQt5.QtGui import QIcon
    for name in ('assets/app.png', 'assets/app.ico'):
        p = resource_path(name)
        if p.exists():
            return QIcon(str(p))
    return QIcon()
