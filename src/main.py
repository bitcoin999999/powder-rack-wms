"""완분 적재대 관리 — 실행 진입점.

  완분적재대.exe                      일반 실행 (데이터: exe 옆 data\\)
  완분적재대.exe --selftest log.txt   화면 없이 2시간 시뮬레이션 + 불변식 검사 + selftest.png 저장, 종료 코드 0/1
  완분적재대.exe --shot out.png       창을 띄운 뒤 잠시 시뮬레이션하고 창 자체를 캡처해서 종료 (화면 확인용)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def base_dir() -> Path:
    """exe 로 실행하면 exe 폴더, 개발 실행이면 프로젝트 폴더."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return SRC.parent


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).write_text(text, encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='완분 적재대 관리')
    ap.add_argument('--selftest', metavar='LOG', help='화면 없이 자가 시험')
    ap.add_argument('--hours', type=float, default=2.0, help='자가 시험 시뮬레이션 시간')
    ap.add_argument('--shot', metavar='PNG', help='창 캡처 후 종료')
    ap.add_argument('--shot-seconds', type=float, default=1800.0, help='캡처 전 시뮬레이션 진행 시간(s)')
    ap.add_argument('--data', metavar='DIR', help='데이터 폴더 (기본: 실행 폴더)')
    args = ap.parse_args(argv)

    if args.selftest:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    if os.environ.get('QT_QPA_PLATFORM', '').startswith('offscreen') and sys.platform == 'win32':
        # offscreen 플랫폼은 윈도 글꼴 폴더를 모른다 → 지정하지 않으면 캡처에 글자가 하나도 안 나온다
        os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'))

    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QApplication

    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    offscreen = os.environ.get('QT_QPA_PLATFORM', '').startswith('offscreen')
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy') and not offscreen:   # 캡처(offscreen)는 1:1 그대로
        # Qt 5.15 기본(Round)은 윈도 150% 배율을 2배로 올려 1920×1080 화면이 960×540 처럼 좁아진다 → 배율 그대로
        from PyQt5.QtGui import QGuiApplication
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName('완분 적재대 관리')
    app.setFont(QFont('Malgun Gothic', 10))
    # Qt 기본 문구(예/아니요/확인 버튼, 색 고르기, 붙여넣기 메뉴)를 한국어로 + 모든 창(시작 오류 상자 포함) 같은 아이콘
    from ui.resources import app_icon
    from ui.util import install_korean_qt
    install_korean_qt(app)
    app.setWindowIcon(app_icon())

    # 자가 시험·캡처는 실제 데이터를 건드리지 않도록 임시 폴더를 쓴다 (끝나면 지운다).
    made_tmp: Path | None = None
    if args.selftest or args.shot:
        root = Path(args.data) if args.data else Path(tempfile.mkdtemp(prefix='rackwms-'))
        made_tmp = None if args.data else root
    else:
        root = Path(args.data) if args.data else base_dir()
    try:
        return _run(args, app, root)
    finally:
        _remove_tmp(made_tmp)


def _remove_tmp(path: Path | None) -> None:
    """자가 시험·캡처용 임시 데이터 폴더 지우기 (그 안의 app.log 처리기를 먼저 닫는다)."""
    if path is None:
        return
    import logging
    import shutil
    root_log = logging.getLogger()
    for h in list(root_log.handlers):
        name = getattr(h, 'baseFilename', None)
        if name and Path(name).resolve().is_relative_to(path.resolve()):
            root_log.removeHandler(h)
            h.close()
    shutil.rmtree(path, ignore_errors=True)


def _run(args, app, root: Path) -> int:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QMessageBox

    from controller import Controller
    from ui.main_window import MainWindow

    # 같은 데이터 폴더로 두 번 실행하면 두 앱이 같은 AMR 에 지시를 보낸다 → 두 번째는 안내 후 종료.
    from PyQt5.QtCore import QLockFile
    (root / 'data').mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / 'data' / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if args.selftest:
            _write(args.selftest, 'FAIL 이미 실행 중\n')
            return 1
        QMessageBox.information(None, '완분 적재대 관리', '이미 실행 중입니다. 작업 표시줄에서 열린 창을 확인하세요.')
        return 0

    try:
        ctrl = Controller(root)
    except Exception as exc:  # 데이터 파일 손상 등 — 사용자에게 보여 주고 종료
        if args.selftest:
            _write(args.selftest, 'FAIL 시작 오류\n' + traceback.format_exc())
            return 1
        QMessageBox.critical(None, '시작 오류', f'프로그램을 시작하지 못했습니다.\n\n{exc}')
        return 1

    win = MainWindow(ctrl)

    if args.selftest:
        win.resize(1440, 900)
        win.show()
        app.processEvents()
        try:
            problems = ctrl.selftest(args.hours)
        except Exception:
            problems = ['자가 시험 중 예외:\n' + traceback.format_exc()]
        win.stop()          # 타이머를 멈추고 끝난 상태 그대로 캡처
        win.refresh_all()
        app.processEvents()
        shot = Path(args.selftest).with_name('selftest.png')
        win.grab().save(str(shot))
        snap = ctrl.inventory.snapshot()
        lines = [('OK' if not problems else 'FAIL') + f' · 적재 {snap.occupied}/{snap.capacity} · 컨베이어 {snap.conveyor}'
                 f' · 작업 {len(ctrl.fleet.orders())}건 · 확인 필요 {snap.needs_check}']
        lines += problems
        _write(args.selftest, '\n'.join(lines) + '\n')
        win.stop()
        ctrl.shutdown()
        return 0 if not problems else 1

    win.show()
    if args.shot:
        win.resize(1440, 900)
        ctrl.set_sim_speed(20)
        ctrl.start_overbridge()
        steps = int(args.shot_seconds / 20 / 0.1)

        def advance(n=[0]):
            for _ in range(50):
                if n[0] >= steps:
                    break
                ctrl.tick(0.1)
                n[0] += 1
            if n[0] >= steps:
                win.refresh_all()
                QTimer.singleShot(600, finish)
            else:
                QTimer.singleShot(0, advance)

        def finish():
            win.grab().save(args.shot)
            if os.environ.get('RACKWMS_SHOT_TABS') == '1':   # 개발용: 탭마다 + 재종 검색 화면도 저장
                win.dev_save_shots(Path(args.shot).resolve().parent)
            win.stop()
            ctrl.shutdown()
            app.quit()

        QTimer.singleShot(300, advance)
    code = app.exec_()
    if not args.shot:
        win.stop()
        ctrl.shutdown()
    return code


if __name__ == '__main__':
    sys.exit(main())
