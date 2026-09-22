"""生成 THIRD-PARTY-LICENSES.md：把随包分发的第三方依赖的许可与版权声明合成一个文件。

为什么需要：本项目打包成 Windows 桌面程序（PyInstaller，见 `api-hub.spec`）分发，
而 fastapi / starlette / uvicorn / httpx / pydantic 等依赖是 MIT、BSD-3-Clause、Apache-2.0 许可 ——
这几类都要求**以二进制形式分发时附上许可文本与版权声明**。PyInstaller 只打包模块代码，
`*.dist-info/licenses/` 不会自动进包，所以在打包前生成一份汇总，并让它随包一起发。

用法（在装有依赖的环境里跑，虚拟环境或系统环境都行）:

    python scripts/gen_third_party_licenses.py            # 写入仓库根目录
    python scripts/gen_third_party_licenses.py --out xxx.md

来源是**当前环境里实际安装的包**（不是 requirements.txt 里写的），所以生成结果和打进包的内容一致。
依赖升级后重新跑一次即可。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.parser
import pathlib
import re
import sys
import sysconfig

#: 这些是本项目自己的代码/工具链，不算"随包分发的第三方依赖"
_SKIP = {"pip", "setuptools", "wheel", "pytest", "iniconfig", "pluggy", "pygments",
         "packaging", "colorama", "annotated-doc"}

#: 许可文件名（各家叫法不一）
_LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "AUTHORS*")

_LICENSE_FILE_KINDS = {"license", "licence", "copying", "notice"}


def _purelib() -> pathlib.Path:
    """当前环境的 site-packages 路径（venv 里跑就是 venv 的）"""
    return pathlib.Path(sysconfig.get_paths()["purelib"])


def _metadata(info: pathlib.Path) -> tuple[str, str, str]:
    """从 METADATA 里取 (包名, 版本, 许可声明)"""
    meta = info / "METADATA"
    name = info.name.replace(".dist-info", "")
    version = ""
    license_expr = ""
    if meta.is_file():
        msg = email.parser.Parser().parsestr(meta.read_text(encoding="utf-8", errors="replace"))
        name = msg.get("Name") or name
        version = msg.get("Version") or ""
        license_expr = (msg.get("License-Expression") or "").strip()
        if not license_expr:
            # 老式写法：License 字段可能是一整段文本，只取首行判断
            raw = (msg.get("License") or "").strip()
            first = raw.splitlines()[0].strip() if raw else ""
            license_expr = first if len(first) <= 120 else "(见下方全文)"
        if not license_expr:
            classifiers = [v for k, v in msg.items() if k == "Classifier"
                           and v.startswith("License ::")]
            if classifiers:
                license_expr = classifiers[0].split("::")[-1].strip()
    return name, version, license_expr or "未声明"


def _license_files(info: pathlib.Path) -> list[pathlib.Path]:
    """该包自带的许可/声明文件（新 wheel 在 dist-info/licenses/，老的散在 dist-info/ 里）"""
    found: list[pathlib.Path] = []
    for base in (info / "licenses", info):
        if not base.is_dir():
            continue
        for pattern in _LICENSE_GLOBS:
            for f in sorted(base.rglob(pattern)):
                if not f.is_file():
                    continue
                # 老的散落写法：只认"文件名就是许可/声明"的，别把 RECORD 之类收进来
                stem = re.sub(r"\.[a-z]+$", "", f.name, flags=re.I).lower()
                if base == info and stem not in _LICENSE_FILE_KINDS:
                    continue
                if f not in found:
                    found.append(f)
    return found


def collect() -> list[dict]:
    out = []
    for info in sorted(_purelib().glob("*.dist-info")):
        name, version, license_expr = _metadata(info)
        base = re.sub(r"[-_.]+", "-", name).lower()
        if base in {re.sub(r"[-_.]+", "-", s).lower() for s in _SKIP}:
            continue
        files = _license_files(info)
        texts = []
        for f in files:
            try:
                texts.append((f.name, f.read_text(encoding="utf-8", errors="replace").strip()))
            except OSError:
                continue
        out.append({"name": name, "version": version, "license": license_expr,
                    "files": texts})
    return out


def render(pkgs: list[dict], own_license: str = "MIT") -> str:
    today = _dt.date.today().isoformat()
    lines = [
        "# 第三方许可证（Third-Party Licenses）",
        "",
        "本程序以 **Windows 桌面程序（PyInstaller 打包）** 的形式分发，包内含以下第三方开源库。",
        "按各自的许可要求，这里汇总它们的许可文本与版权声明。",
        "",
        f"- 本项目自身的许可：**{own_license}**（见仓库根目录 `LICENSE`）",
        f"- 本文件由 `scripts/gen_third_party_licenses.py` 生成（生成日期 {today}）；"
        "依赖升级后请重新生成",
        "",
        "## 依赖清单",
        "",
        "| 库 | 版本 | 许可 |",
        "|---|---|---|",
    ]
    for p in sorted(pkgs, key=lambda x: x["name"].lower()):
        lines.append(f"| {p['name']} | {p['version']} | {p['license']} |")
    lines += ["", "## 许可全文", ""]
    for p in sorted(pkgs, key=lambda x: x["name"].lower()):
        lines.append(f"### {p['name']} {p['version']}")
        lines.append("")
        lines.append(f"- 许可：**{p['license']}**")
        if not p["files"]:
            lines.append("- ⚠ 该发行版未在包元数据里附带许可文件，请以"
                         "其项目仓库/官方发布页为准")
            lines.append("")
            continue
        for fname, text in p["files"]:
            lines.append("")
            lines.append(f"**{fname}**")
            lines.append("")
            lines.append("```text")
            lines.append(text.rstrip())
            lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成第三方许可证汇总文件")
    ap.add_argument("--out", default="THIRD-PARTY-LICENSES.md", help="输出路径（默认仓库根目录）")
    args = ap.parse_args()

    pkgs = collect()
    target = pathlib.Path(args.out)
    if not target.is_absolute():
        target = pathlib.Path(__file__).resolve().parent.parent / target
    target.write_text(render(pkgs), encoding="utf-8")
    print(f"已生成 {target}（{len(pkgs)} 个依赖）")
    for p in sorted(pkgs, key=lambda x: x["name"].lower()):
        flag = "" if p["files"] else "  ⚠ 无许可文件"
        print(f"  {p['name']:<28} {p['version']:<12} {p['license']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
