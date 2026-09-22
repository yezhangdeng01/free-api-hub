"""`scripts/gen_third_party_licenses.py` 的集合口径测试（离线）。

守的是一条**曾经错过**的规矩：清单的集合由 `requirements.txt` 决定，不由「跑脚本那台机器上
装了什么」决定。之前按环境取，结果是：

- 提交版只有 15 项，漏掉 `pystray`（**LGPLv3**）、`pywebview`、`pillow` —— 都在 requirements.txt 里；
- 在装过 pyinstaller 的机器上跑，又会把 `pyinstaller` / `altgraph` / `pefile` 一起列进来，
  而它们是构建期工具，并不随包分发。

这条要是再被改回去，这里应当红。
"""
import importlib.metadata
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PNF = importlib.metadata.PackageNotFoundError


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "gen_third_party_licenses", ROOT / "scripts" / "gen_third_party_licenses.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load_script()


class _FakeMD:
    """替掉 importlib.metadata：给一张固定的依赖图，别碰真实环境"""

    def __init__(self, graph):
        self.graph = graph

    def requires(self, name):
        if name not in self.graph:
            raise _PNF(name)
        return self.graph[name]

    def version(self, name):
        if name not in self.graph:
            raise _PNF(name)
        return "1.0"

    PackageNotFoundError = _PNF


def test_root_names_ignores_comments_and_option_lines(tmp_path):
    f = tmp_path / "requirements.txt"
    f.write_text(
        "fastapi>=0.110\n"
        "uvicorn>=0.29   # 行尾注释\n"
        "# 开发用\n"
        "pytest>=8.0\n"
        "-r other.txt\n"
        "pillow>=10.0\n",
        encoding="utf-8")
    assert gen._root_names(f) == ["fastapi", "uvicorn", "pytest", "pillow"]


def test_closure_follows_requires(monkeypatch):
    graph = {"app": ["web>=1"], "web": ["core", "idna"], "core": [], "idna": []}
    monkeypatch.setattr(gen, "_md", _FakeMD(graph))
    keep, missing = gen._closure(["app"])
    assert keep == {"app", "web", "core", "idna"}
    assert missing == []


def test_closure_drops_build_tools_and_their_subtree(monkeypatch):
    """构建期工具不能进清单，它拉来的东西也不能"""
    graph = {
        "app": ["web"],
        "web": ["pyinstaller", "packaging"],
        "pyinstaller": ["altgraph", "pefile"],
        "altgraph": [],
        "pefile": [],
        "packaging": [],
    }
    monkeypatch.setattr(gen, "_md", _FakeMD(graph))
    keep, _ = gen._closure(["app"])
    assert keep == {"app", "web"}


def test_closure_reports_missing_instead_of_dropping_silently(monkeypatch):
    """没装上的依赖要报出来 —— 静默漏项正是 LGPLv3 那次翻车的方式"""
    monkeypatch.setattr(gen, "_md", _FakeMD({"app": ["pystray"]}))
    keep, missing = gen._closure(["app"])
    assert keep == {"app"}
    assert missing == ["pystray"]


def test_requires_drops_extras_and_foreign_platforms(monkeypatch):
    if gen._PkgReq is None:
        pytest.skip("环境里没有 packaging")
    graph = {"demo": ['bottle', 'PyQt6; extra == "qt"',
                      'weirdone; sys_platform == "sunos5"']}
    monkeypatch.setattr(gen, "_md", _FakeMD(graph))
    assert sorted(gen._requires("demo")) == ["bottle"]


def test_annotated_doc_is_not_skipped():
    """`annotated-doc` 名字像内部件，其实是 fastapi 的硬依赖，不能挡掉"""
    assert "annotated-doc" not in gen._SKIP_NORM


# ---------------------------------------------------------------- --check（CI 用的清单核查）

def _pkgs(*pairs):
    return [{"name": n, "version": v, "license": "MIT", "files": [("LICENSE", "MIT")]}
            for n, v in pairs]


def test_table_entries_reads_only_the_dependency_table():
    """只认「依赖清单」那张表，表头与分隔行不能混进来"""
    assert gen.table_entries(gen.render(_pkgs(("fastapi", "0.141.1")))) == {"fastapi": "0.141.1"}


def test_check_passes_on_its_own_output(tmp_path):
    """自洽：render 出来的东西，check 得认"""
    pkgs = _pkgs(("fastapi", "0.141.1"), ("pystray", "0.19.5"))
    f = tmp_path / "THIRD-PARTY-LICENSES.md"
    f.write_text(gen.render(pkgs), encoding="utf-8")
    assert gen.check(f, pkgs, []) == 0


def test_check_passes_when_only_versions_differ(tmp_path):
    """版本号变了不算失败 —— requirements.txt 全是 `>=`，CI 装的总是当时最新版"""
    f = tmp_path / "L.md"
    f.write_text(gen.render(_pkgs(("anyio", "4.14.2"))), encoding="utf-8")
    assert gen.check(f, _pkgs(("anyio", "4.15.1")), []) == 0


def test_check_fails_when_listing_misses_a_dependency(tmp_path):
    """清单漏项要红 —— #2 翻车就是这个形态"""
    f = tmp_path / "L.md"
    f.write_text(gen.render(_pkgs(("fastapi", "1"))), encoding="utf-8")
    assert gen.check(f, _pkgs(("fastapi", "1"), ("pystray", "0.19.5")), []) == 1


def test_check_fails_when_listing_keeps_a_removed_dependency(tmp_path):
    """依赖被移除了、清单没重跑，也要红"""
    f = tmp_path / "L.md"
    f.write_text(gen.render(_pkgs(("fastapi", "1"), ("bottle", "0.13.4"))), encoding="utf-8")
    assert gen.check(f, _pkgs(("fastapi", "1")), []) == 1


def test_check_handles_crlf_listing(tmp_path):
    """仓库里那份是 CRLF（Windows 上生成的），解析不能被行尾绊倒"""
    pkgs = _pkgs(("fastapi", "0.141.1"))
    f = tmp_path / "L.md"
    f.write_bytes(gen.render(pkgs).replace("\n", "\r\n").encode("utf-8"))
    assert gen.check(f, pkgs, []) == 0


def test_check_fails_when_listing_absent(tmp_path):
    assert gen.check(tmp_path / "nope.md", _pkgs(("x", "1")), []) == 1


def test_check_fails_when_environment_incomplete(tmp_path):
    """环境没装全时算出来的集合不可信，不能当通过"""
    f = tmp_path / "L.md"
    f.write_text(gen.render(_pkgs(("fastapi", "1"))), encoding="utf-8")
    assert gen.check(f, _pkgs(("fastapi", "1")), ["pystray"]) == 1


# ---------------------------------------------------------------- 控制台编码

def test_script_prints_under_cp1252_console():
    """CI 的 Windows 控制台编码是 cp1252，脚本不能崩在打印上。

    曾经崩过：检查逻辑通过了，`print("✓ …")` 却抛 UnicodeEncodeError，
    退出码非 0，看起来像"清单不一致"。脚本自己把 stdout 改到 UTF-8 才对。
    """
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_third_party_licenses.py"), "--help"],
        capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
