"""生成 THIRD-PARTY-LICENSES.md：把随包分发的第三方依赖的许可与版权声明合成一个文件。

为什么需要：本项目打包成 Windows 桌面程序（PyInstaller，见 `api-hub.spec`）分发，
而 fastapi / starlette / uvicorn / httpx / pydantic 等依赖是 MIT、BSD-3-Clause、Apache-2.0 许可 ——
这几类都要求**以二进制形式分发时附上许可文本与版权声明**。PyInstaller 只打包模块代码，
`*.dist-info/licenses/` 不会自动进包，所以在打包前生成一份汇总，并让它随包一起发。

**集合口径**：以 `requirements.txt` 里的依赖为根，顺着各包元数据里的 `Requires-Dist`
求传递闭包。**不按「这台机器上装了什么」取集合** —— 那样两头都会偏：

- 机器上多装了构建期工具（`pip install ... pyinstaller` 之后就有 pyinstaller / pefile /
  altgraph / pywin32-ctypes / pyinstaller-hooks-contrib），它们不随包分发，列进去是噪音；
- 机器上少装了运行时依赖（例如在一台没装 `pystray` 的机器上跑），清单就会漏项 ——
  而 `pystray` 是 **LGPLv3**，漏它有实际后果，比漏一个 MIT 依赖重得多。

用法（在装有依赖的环境里跑，虚拟环境或系统环境都行）:

    python scripts/gen_third_party_licenses.py            # 写入仓库根目录
    python scripts/gen_third_party_licenses.py --out xxx.md
    python scripts/gen_third_party_licenses.py --check    # 只核对，不写（CI 用）

`--check` 守的是**仓库里那份清单会不会静默过期**：改了 `requirements.txt` 却忘了重跑，
清单就悄悄对不上了（#2 就是这样漏掉 10 个运行时依赖的）。它只把**依赖集合**的变化判为失败，
版本号变了只提示 —— 见 `check()` 的注释。

产物是给 **Windows 绿色版**用的，所以清单也该在能装上 Windows 专属依赖的机器上生成
（`pythonnet` / `clr_loader` 只在 Windows 上装得到）；在别的平台上跑会缺这几项，脚本会提醒。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.parser
import importlib.metadata as _md
import pathlib
import re
import sys
import sysconfig

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _force_utf8_stdout() -> None:
    """把输出流改到 UTF-8。

    Windows 的 GitHub Runner 上 stdout 编码是 cp1252，而本脚本的输出里满是中文（还有 ✓ / ✗ / ⚠），
    不改就会在**打印结果**这一步抛 UnicodeEncodeError —— 检查逻辑明明通过了，却因为打不出那一行
    而退出码非 0，看起来像"清单不一致"。本地控制台通常已是 UTF-8，所以这个坑只在 CI 上现形。

    退路用 `errors="replace"`：宁可少几个字符，也不能让脚本崩在打印上。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:               # 不是 TextIOWrapper（被重定向或替换过）
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

#: 开发/构建工具：项目自己的工具链与打包器，不随包分发，不进清单。
#: （pytest 连它的传递依赖一起挡在这里 —— 它在 requirements.txt 里，但只在开发时用。
#: `annotated-doc` 名字看着像内部件，其实是 fastapi ≥0.14x 的硬依赖，**不能**挡。）
_SKIP = {"pip", "setuptools", "wheel", "pytest", "iniconfig", "pluggy", "pygments",
         "packaging", "colorama",
         "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
         "pywin32-ctypes"}

#: 许可文件名（各家叫法不一）
_LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "AUTHORS*")

_LICENSE_FILE_KINDS = {"license", "licence", "copying", "notice"}

try:                                    # 判 Requires-Dist 里的环境标记用；没有就保守处理
    from packaging.requirements import Requirement as _PkgReq
except Exception:                       # pragma: no cover - 依赖缺失时的降级路径
    _PkgReq = None


def _norm(name: str) -> str:
    """包名归一化（PEP 503）：`Pydantic_Core` / `pydantic-core` 是同一个包"""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


_SKIP_NORM = {_norm(s) for s in _SKIP}

#: 拆掉版本约束 / extras / 环境标记，只留包名（没有 packaging 时的兜底解析）
_NAME_SPLIT = re.compile(r"[<>=!~;\[\s(]")


def _root_names(req_file: pathlib.Path) -> list[str]:
    """从 requirements.txt 取依赖名（丢掉版本约束、extras、注释与 -r/-e 之类的选项行）"""
    names = []
    for raw in req_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _NAME_SPLIT.split(line, 1)[0].strip()
        if name:
            names.append(name)
    return names


def _requires(name: str) -> list[str]:
    """某包声明的依赖名（已按当前平台/解释器筛掉不生效的环境标记）"""
    try:
        reqs = _md.requires(name) or []
    except _md.PackageNotFoundError:
        return []
    out = []
    for raw in reqs:
        if _PkgReq is None:
            out.append(_NAME_SPLIT.split(raw, 1)[0].strip())
            continue
        try:
            req = _PkgReq(raw)
        except Exception:
            continue
        if req.marker is not None:
            try:
                if not req.marker.evaluate():
                    continue
            except Exception:
                pass                    # 认不出的标记：当它生效（宁可多列，不能漏）
        out.append(req.name)
    return out


def _closure(roots: list[str]) -> tuple[set[str], list[str]]:
    """求 roots 的传递闭包。

    返回 `(keep, missing)`：`keep` 是**环境里装上了、且在闭包内、且不是构建工具**的包名集合；
    `missing` 是闭包内但没装上的包名 —— 单独报出来，不静默跳过（LGPLv3 那次就是静默漏项）。
    """
    visited: set[str] = set()
    keep: set[str] = set()
    missing: list[str] = []
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        key = _norm(name)
        if not key or key in visited:
            continue
        visited.add(key)
        if key in _SKIP_NORM:
            continue                    # 跳过的包，它的传递依赖也一并不看
        try:
            _md.version(name)
        except _md.PackageNotFoundError:
            missing.append(name)        # 没装 → 它的依赖也无从得知，到此为止
            continue
        keep.add(key)
        queue.extend(_requires(name))
    return keep, missing


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
                license_expr = classifiers[-1].split("::")[-1].strip()
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


def collect(keep: set[str]) -> list[dict]:
    """收集 keep 里的包（只认环境里装上的那些，文件从它们的 dist-info 里取）"""
    out = []
    for info in sorted(_purelib().glob("*.dist-info")):
        name, version, license_expr = _metadata(info)
        if _norm(name) not in keep:
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
        "- 清单集合：`requirements.txt` 的依赖 + 其传递依赖（构建期工具不计入）",
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


# ---------------------------------------------------------------- 清单核查

_TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")


def table_entries(text: str) -> dict[str, str]:
    """从清单正文的「依赖清单」表里取 `{归一化包名: 版本}`"""
    out: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        name, version = m.group(1).strip(), m.group(2).strip()
        if name == "库" or set(name) <= {"-"}:
            continue                    # 表头与分隔行
        out[_norm(name)] = version
    return out


def check(target: pathlib.Path, pkgs: list[dict], missing: list[str]) -> int:
    """核对 `target` 与「按当前 requirements.txt 算出来的集合」，返回进程退出码。

    **只把依赖集合的变化判为失败**，版本号不进判定 —— `requirements.txt` 全用 `>=`，
    CI 每次 `pip install` 装的是当时最新版，上游一发版版本号就变。拿版本号当失败条件，
    CI 会周期性变红，而那种红并不说明清单错了。集合错了才拦：漏掉 `pystray`（LGPLv3）
    有实际后果，漏项也正是 #2 翻车的方式。
    """
    if missing:
        print("当前环境没装全，算出来的集合不可信 —— 先 pip install -r requirements.txt：",
              file=sys.stderr)
        for m in missing:
            print(f"    {m}", file=sys.stderr)
        return 1

    if not target.is_file():
        print(f"找不到 {target} —— 先在能装上 Windows 专属依赖的机器上跑一次 "
              f"`python scripts/gen_third_party_licenses.py` 并把它提交进来", file=sys.stderr)
        return 1

    have = table_entries(target.read_text(encoding="utf-8"))
    want = {_norm(p["name"]): p["version"] for p in pkgs}
    added = sorted(set(want) - set(have))       # 现在算得出来、清单里没有
    gone = sorted(set(have) - set(want))        # 清单里有、现在算不出来
    moved = sorted(k for k in set(have) & set(want) if have[k] != want[k])

    if not added and not gone:
        print(f"✓ {target.name} 与当前依赖一致（{len(want)} 项）")
        if moved:
            print(f"  ℹ 其中 {len(moved)} 项版本不同（不算失败；打包时会重新生成清单）：")
            for k in moved[:10]:
                print(f"      {k} {have[k]} → {want[k]}")
            if len(moved) > 10:
                print(f"      …… 另有 {len(moved) - 10} 项")
        return 0

    print(f"✗ {target.name} 与当前依赖不一致：", file=sys.stderr)
    if added:
        print("  当前依赖算得出来、清单里没有（漏项）：", file=sys.stderr)
        for k in added:
            print(f"      + {k} {want[k]}", file=sys.stderr)
    if gone:
        print("  清单里有、当前依赖算不出来（依赖已移除，或这个环境装漏了）：", file=sys.stderr)
        for k in gone:
            print(f"      - {k} {have[k]}", file=sys.stderr)
    print("  修法：在能装上 Windows 专属依赖的机器上重跑\n"
          "      python scripts/gen_third_party_licenses.py\n"
          f"  再把 {target.name} 一起提交。", file=sys.stderr)
    return 1


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="生成第三方许可证汇总文件")
    ap.add_argument("--out", default="THIRD-PARTY-LICENSES.md", help="输出路径（默认仓库根目录）")
    ap.add_argument("--requirements", default=str(_ROOT / "requirements.txt"),
                    help="依赖清单，集合作根（默认仓库根目录的 requirements.txt）")
    ap.add_argument("--check", action="store_true",
                    help="只核对现有清单与当前依赖是否一致（CI 用），不写文件")
    args = ap.parse_args()

    req_file = pathlib.Path(args.requirements)
    if not req_file.is_file():
        print(f"找不到依赖清单：{req_file}", file=sys.stderr)
        return 2
    roots = _root_names(req_file)
    if not roots:
        print(f"{req_file} 里没解析出任何依赖", file=sys.stderr)
        return 2

    keep, missing = _closure(roots)
    pkgs = collect(keep)

    target = pathlib.Path(args.out)
    if not target.is_absolute():
        target = _ROOT / target

    if args.check:
        return check(target, pkgs, missing)

    target.write_text(render(pkgs), encoding="utf-8")

    print(f"已生成 {target}（{len(pkgs)} 个依赖，根来自 {req_file.name}）")
    for p in sorted(pkgs, key=lambda x: x["name"].lower()):
        flag = "" if p["files"] else "  ⚠ 无许可文件"
        print(f"  {p['name']:<28} {p['version']:<12} {p['license']}{flag}")

    if missing:
        print("\n⚠ 下面这些没装上，已跳过 —— 先 pip install -r requirements.txt 再生成：")
        for m in missing:
            print(f"    {m}")
    if sys.platform != "win32":
        print("\n⚠ 当前不是 Windows：pythonnet / clr_loader / pywin32-ctypes 只在 Windows 上安装，"
              "清单会缺这几项。发布用的清单请在 Windows 上生成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
