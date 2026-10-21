"""exe 빌드: python build.py  →  app\\완분적재대.exe (+ _internal). app\\data (재고 DB·설정)는 지우지 않는다.
바로가기 '완분적재대 실행.lnk' 를 이 폴더에 만든다.
완분적재대.exe 가 실행 중이면 (윈도가 exe·DLL 을 잠가 app 폴더가 반쯤 지워지므로) 아무것도 하지 않고 끝낸다."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = '완분적재대'


def exe_in_use(exe: Path) -> bool:
    """exe 가 실행 중인가 (윈도는 실행 중인 exe 를 쓰기용으로 열 수 없다). 파일이 없으면 False."""
    try:
        with open(exe, 'r+b'):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True


def main() -> int:
    import PyInstaller.__main__

    built_name, app = NAME, ROOT / 'app'
    if exe_in_use(app / f'{NAME}.exe'):
        print(f'{app / (NAME + ".exe")} 가 실행 중입니다 — 프로그램을 모두 닫고 다시 빌드하세요. (아무것도 바꾸지 않았습니다)')
        return 1

    tmp_dist, work = ROOT / 'build' / 'dist', ROOT / 'build' / 'work'
    args = [
        str(ROOT / 'src' / 'main.py'), '--name', NAME, '--onedir', '--windowed', '--noconfirm', '--clean',
        '--distpath', str(tmp_dist), '--workpath', str(work), '--specpath', str(ROOT / 'build'),
        '--paths', str(ROOT / 'src'),
        '--hidden-import', 'amr.vda5050', '--hidden-import', 'amr.tcp_json',
        '--exclude-module', 'tkinter', '--exclude-module', 'matplotlib', '--exclude-module', 'numpy',
        '--exclude-module', 'pandas', '--exclude-module', 'PIL', '--exclude-module', 'pytest',
    ]
    icon = ROOT / 'assets' / 'app.ico'
    if icon.exists():
        args += ['--icon', str(icon), '--add-data', f'{icon};assets']
    png = ROOT / 'assets' / 'app.png'          # 창 아이콘 (ui/resources.py 가 _internal/assets 에서 찾는다)
    if png.exists():
        args += ['--add-data', f'{png};assets']
    PyInstaller.__main__.run(args)

    built = tmp_dist / built_name
    if exe_in_use(app / f'{NAME}.exe'):        # 빌드하는 동안 켰을 수도 있다
        print(f'{app / (NAME + ".exe")} 가 실행 중입니다 — 프로그램을 닫고 다시 빌드하세요. (새 exe 는 {built} 에 있습니다)')
        return 1
    app.mkdir(exist_ok=True)
    try:
        for item in app.iterdir():          # data 폴더만 남기고 교체
            if item.name.lower() == 'data':
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in built.iterdir():
            target = app / item.name
            shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)
    except PermissionError as exc:
        print(f'app 폴더를 바꾸지 못했습니다 ({exc}) — 열린 프로그램·탐색기 창을 닫고 python build.py 를 다시 실행하세요.')
        return 1

    exe = app / f'{NAME}.exe'
    lnk = ROOT / f'{NAME} 실행.lnk'
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{app}';$s.Save()")
    subprocess.run(['powershell', '-NoProfile', '-Command', ps], check=False)
    print(f'완료: {exe}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
