# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：闲鱼工具安装程序（单文件，内含整个 dist 目录）"""
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH)
DIST_DIR = PROJECT_ROOT / "dist" / "闲鱼数据采集分析工具"

# 收集整个 dist 目录树
dist_datas = []
if DIST_DIR.exists():
    for f in DIST_DIR.rglob("*"):
        if f.is_file():
            rel = f.relative_to(DIST_DIR)
            dest = str(rel.parent) if str(rel.parent) != '.' else '.'
            dist_datas.append((str(f), dest))

a = Analysis(
    [str(PROJECT_ROOT / "installer.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=dist_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "unittest", "test", "matplotlib", "PIL", "frida", "frida_tools",
              "uiautomator2", "transformers", "sentence_transformers", "text2vec", "tokenizers",
              "openpyxl", "huggingface_hub"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="闲鱼工具安装程序",
    icon=str(PROJECT_ROOT / "assets" / "app_icon.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    [],
    name="闲鱼工具安装程序",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
