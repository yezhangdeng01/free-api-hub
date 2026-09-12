"""全局测试隔离：**绝不允许测试碰真实 data/ 下的生产文件**。

事故背景（2026-09-13）：跑一次全量 pytest，`data/model_status.json` 从 182KB（千余条模型状态）
被覆盖成 1KB（7 条测试夹具数据），`data/runtime_state.json` 同步被覆盖。

成因（两个坑叠在一起）：
1. `store.MODEL_STATUS_PATH` 等是模块级变量，只有 `test_api.py` 的 module fixture 临时改过路径，
   直接调 `gateway.mark_model_status()` 的 test_core 测试没改 → 写真实文件；
2. 更隐蔽的是 `store._ms_cache`（模型状态的文件缓存）**不随路径切换失效** ——
   test_api 把路径改到 tmp（文件不存在 → 缓存成 `{}`），fixture 把路径改回来后缓存仍是 `{}`，
   后续 `persist_model_status()` 直接拿这个空缓存 + 一条新数据写回**真实路径**，
   于是真实文件被压成几条。

所以这里在 setup 和 teardown **两头都清 `_ms_cache`**：只在 setup 清、靠 monkeypatch 自动还原的话，
测试期间产生的缓存会留在进程里，继续影响后续测试与收尾写盘。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_data_files(tmp_path, monkeypatch):
    """把所有落盘路径重定向到本次测试的临时目录（每个测试独立）。"""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "usage.db"), raising=False)
    monkeypatch.setattr(store, "MODEL_STATUS_PATH", str(tmp_path / "model_status.json"), raising=False)
    monkeypatch.setattr(store, "RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"), raising=False)
    store._ms_cache = None          # 关键：缓存必须跟着路径一起失效
    yield
    store._ms_cache = None          # 收尾再清一次，别把测试缓存带进下一个测试
