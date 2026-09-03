# -*- mode: python ; coding: utf-8 -*-
"""API Hub 绿色版打包配置（PyInstaller onedir）
用法：pyinstaller --noconfirm api-hub.spec
产物：dist/API Hub/（含 exe + _internal），整个目录压缩即绿色版
"""
from PyInstaller.utils.hooks import collect_submodules, collect_all

# uvicorn 运行时按名称动态加载协议/循环实现，必须整包收集
hidden = []
for m in ("uvicorn", "webview", "pystray"):
    hidden += collect_submodules(m)

# pythonnet（pywebview WebView2 后端依赖）：需要把 Python.Runtime.dll 等一起带出
py_datas, py_bins, py_hidden = collect_all("pythonnet")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=py_bins,
    datas=[("frontend", "frontend")] + py_datas,
    hiddenimports=hidden + py_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc", "doctest", "PyQt5", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="API Hub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序：无黑色控制台
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="API Hub",
)
