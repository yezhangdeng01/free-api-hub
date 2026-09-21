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

第二个坑（2026-09-21 定位）：**内存里的 capability 全局，路径重定向管不着**。见下面
`_isolate_capability_globals` 的说明。
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import capability, store  # noqa: E402

# capability 的模块级全局：`update_bench_scores()` / `update_frontier()` / `update_vision_models()`
# 会直接改写它们，且**不还原**。清单要随 capability.py 的新增全局一起补。
_CAP_GLOBALS = ("_bench", "_bench_hi", "_bench_mid", "_bench_p10", "_bench_p90",
                "_bench_last_ok", "_observed_frontier", "_vision_ok", "_vision_no")

# **进程初始快照**：在 conftest 被 import 时采集（pytest 最早的时机），保证基准是干净的。
# 注意不能改成「逐测试互相比对」—— 那样第一个测试拿到的基准可能已经被污染。
_CAP_BASELINE = {n: copy.deepcopy(getattr(capability, n)) for n in _CAP_GLOBALS}


@pytest.fixture(autouse=True)
def _isolate_capability_globals():
    """每个测试收尾把 capability 的模块级全局还原到**进程初始值**。

    为什么需要（2026-09-21 实测定位）：`test_api.py` 的 TestClient 触发 lifespan →
    后台 `_bg_loop` → `refresh_all()` → `_topup_bench_cache()` →
    `providers.fetch_bench_public()` **去 OpenRouter 免密钥公开端点抓榜分** →
    `capability.update_bench_scores()` 改写 `_bench / _bench_hi / _bench_mid / _bench_p10 /
    _bench_p90`；`refresh_all()` 末尾还有 `capability.update_frontier(all_ids)` 改写
    `_observed_frontier`。这些数据**不经过 `store` 的落盘路径**，所以上面那套路径重定向
    一点也拦不住 —— 抓回来的真实榜分就留在进程里。

    症状（跑在后面的 `test_core.py` 被误伤）：
      · `test_tier_scale_not_generation`：`deepseek-v4-flash` 拿到了真实榜分 24.2，
        落在 mid(19.1)~hi(33.7) 之间 → 返回档位 2，而不是按名字规则的 3；
      · `test_capability_score_is_continuous`：真实分位 p10=9.0 / p90=43.6 把
        `(42.3-9.0)/(43.6-9.0)` 算成 0.9624，而不是默认阈值下的 1.0。

    它还是 **flaky** 的 —— 后台任务跑没跑完取决于事件循环时序，所以同一份代码
    可能这次过、下次挂（2026-09-21 就是这么发现的：上一轮全绿，下一轮两个失败）。

    顺带说明：这个夹具只隔离**内存**污染。测试进程仍会向 OpenRouter 公开端点发一次
    只读、免密钥的请求（渠道健康检查失败时的榜单兜底路径），零成本，暂不拦。
    """
    yield
    for n, v in _CAP_BASELINE.items():
        setattr(capability, n, copy.deepcopy(v))
    capability._dirty["v"] = False


@pytest.fixture(autouse=True)
def _isolate_data_files(tmp_path, monkeypatch):
    """把所有落盘路径重定向到本次测试的临时目录（每个测试独立）。"""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "usage.db"), raising=False)
    monkeypatch.setattr(store, "MODEL_STATUS_PATH", str(tmp_path / "model_status.json"), raising=False)
    monkeypatch.setattr(store, "RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"), raising=False)
    monkeypatch.setattr(store, "CAPABILITY_CACHE_PATH", str(tmp_path / "capability_cache.json"), raising=False)
    store._ms_cache = None          # 关键：缓存必须跟着路径一起失效
    yield
    store._ms_cache = None          # 收尾再清一次，别把测试缓存带进下一个测试
    # 能力榜单同理：脏标记若留着，测试收尾写盘时会把生产缓存覆盖成测试态（空）
    capability._dirty["v"] = False
