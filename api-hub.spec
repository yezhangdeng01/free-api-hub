# -*- mode: python ; coding: utf-8 -*-
"""API Hub 绿色版打包配置（PyInstaller onedir）
用法：pyinstaller --noconfirm api-hub.spec
产物：dist/API Hub/（含 exe + _internal），整个目录压缩即绿色版
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_all

# uvicorn 运行时按名称动态加载协议/循环实现，必须整包收集
hidden = []
for m in ("uvicorn", "webview", "pystray"):
    hidden += collect_submodules(m)

# pythonnet（pywebview 的 WebView2 后端依赖）只在 Windows 上存在：
# 无条件 collect_all 会让非 Windows 上的打包直接失败（虽然是 Windows 专用产物，
# 但脚本本身不该挑平台，否则以后想跑个交叉检查都跑不了）。
if sys.platform == "win32":
    py_datas, py_bins, py_hidden = collect_all("pythonnet")
else:
    py_datas, py_bins, py_hidden = [], [], []

# 许可合规：本项目以二进制形式分发（PyInstaller 包），依赖里的 MIT / BSD-3-Clause /
# Apache-2.0 / MPL-2.0 都要求随包附上许可与版权声明。PyInstaller 只打包模块代码，
# `*.dist-info/licenses/` 不会自动进来，所以显式带上汇总文件与自己的 LICENSE。
# 汇总文件由 scripts/gen_third_party_licenses.py 生成（CI 里打包前会跑一次）。
#
# ⚠️ dest 写 "." 并**不会**落到包根：PyInstaller 6 的 onedir 把所有 `datas` 一律放进
# 内容目录 `_internal/`，zip 根只剩 exe 和 `_internal/`（09-22 下载 v1.0.7 实测）。
# 解压的人从包根看不出来 —— 所以在包根再放一份这件事由 release.yml 的
# 「许可文件在包根再放一份」那步做；这里这份留着，运行时按相对路径找得到。
_licenses = [(p, ".") for p in ("LICENSE", "THIRD-PARTY-LICENSES.md") if os.path.exists(p)]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=py_bins,
    datas=[("frontend", "frontend")] + _licenses + py_datas,
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
