# -*- mode: python ; coding: utf-8 -*-

import os
import re
from pathlib import Path


project_dir = Path(os.environ["WEIX_MANAGER_PROJECT_ROOT"]).resolve()

a = Analysis(
    [str(project_dir / "backend" / "launcher.py")],
    pathex=[str(project_dir / "backend")],
    binaries=[],
    datas=[],
    hiddenimports=["psutil._psutil_windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)


def is_conflicting_root_icu(entry: tuple[str, str, str]) -> bool:
    destination = entry[0].replace("\\", "/").lower()
    if "/" in destination:
        return False
    return destination == "icuuc.dll" or bool(
        re.fullmatch(r"icudt\d+\.dll", destination)
    )


# Qt 6 使用 Windows 系统 ICU。Codex/Poppler 等开发工具也可能在 PATH 中
# 提供同名但导出接口不同的 ICU；PyInstaller 误收集后会导致 QtCore.pyd
# 在启动时出现 WinError 127。只剔除应用根目录的冲突副本，不影响未来
# PyQt 自己在 PyQt6/Qt6/bin 中附带的合法运行库。
a.binaries = [entry for entry in a.binaries if not is_conflicting_root_icu(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WeixManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
