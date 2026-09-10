# API Hub · 项目进度

> 大模型聚合网关：把多个免费大模型 API 聚合成一个 OpenAI 兼容 `/v1` 接口 + 本地管理界面 + 桌面托盘。开源，MIT。
> 本文档记录**项目当前状态与待办**，下次调整前先读这里，再读 `app/` 里的代码。程序性"怎么修"的知识在 api-hub skill，别混。

## 一句话状态

本地已完成「不可用/受限模型重启后冒充可用」+「魔搭受限时故障切换慢」修复，**尚未发版**。等用户实测确认后再 commit + release。

## 最近一次工作（2026-09-10，第二轮）

修复「重启后大量不可用模型变回可用」核心 bug（`mark_model_status` 里 `available = state != "down"` 导致 273 条「受限」模型冒充可用），并优化故障切换（魔搭受限时快速切路）。

### 核心根因

1. **`available` 字段语义错误**：`mark_model_status` 里 `available = state != "down"` → 273 条 `limited`（限流）的 `available=True`，全冒充「可用」，重启原样恢复。其中 242 条是「余额不足 / 404 batch 专用」等**永久不可用**被错标成「暂时受限」。
2. **connect 失败降权不够**：`_update_score` 失败统一 `0.8*旧分`，魔搭受限后从 0.7→0.56，不够沉底；connect 超时 15 秒，每次要等很久才切换。

### 修复清单

| # | 机制 | 修复 | 文件 |
|---|---|---|---|
| 1 | `available` 语义错误 → limited 冒充可用 | `mark_model_status` 改为 `available = state == "ok"`，只有 ok 可路由；新增 `_PERMANENT_KEYWORDS` + `is_permanent_failure()`，统一判定「余额不足/402/403/404/405」→ `down` | `gateway.py` |
| 2 | 旧数据归正（limited→down，available 修正） | `restore_model_status()` 启动时按 state 重新推导 available；永久型 limited 归正为 down（余额不足/404）；一次性回写磁盘 | `gateway.py` |
| 3 | 扫描/真实请求的 4xx/429 判定接入统一 `is_permanent_failure` | `model_test` 里 `is_permanent_failure(404, ...)`；真实请求 `_classify` 里 429 余额不足 + `is_permanent_failure(429, ...)` | `main.py` |
| 4 | connect 失败降权太慢（0.8）→ 魔搭受限不沉底 | `_update_score` 失败按 `kind` 区分：connect 失败用 0.5 衰减，快速沉底 | `gateway.py` |
| 5 | connect 超时 15 秒太长→ 魔搭受限时白等 | `shared_client` connect 超时改为 8 秒 | `main.py` |

### 验证结果

- 单测 34 通过（含 2 个新增防回归用例）
- 离线验证：归正后 `limited+available=True` 从 272 → **0 条**，state 分布 `down 441 / ok 334 / limited 48`
- 预期效果：重启后「可用 N」数字真实反映现状（OpenRouter 可用从 378→约 334），魔搭受限时快速切路

## 上一轮修复（2026-09-10，第一轮）

修复「重启后不可用模型回绿」，解决 5 个机制断裂点：

| # | 机制 | 修复 | 文件 |
|---|---|---|---|
| 1 | OpenRouter 模型 ID 命名变更（`CohereLabs/xxx`→`cohere/xxx`、`MiniMaxAI/xxx`→`minimax/xxx`），旧 down 状态失联 | 刷新时做**确定等价**迁移 `_migrate_named_models()`（归一化后完全一致才迁，异名/换版本不误迁） | `gateway.py` |
| 2 | throttle 429 自学水位纯内存，重启清零 | `snapshot()/restore()/learned_pairs()` 落盘，恢复时给仍有效 (渠道,模型) seed 300s 保守冷却 | `throttle.py` |
| 3 | channel_down 持久化不可靠 + 托盘 `os._exit` 跳过落盘 + 多实例覆盖 | `mark_channel_down/up` **即时落盘**；`_atomic_write` 原子写；托盘退出先 `save_runtime_state()`；加单实例锁 | `gateway.py`/`store.py`/`desktop.py` |
| 4 | 渠道概况与渠道内模型 available 两套口径矛盾 | 渠道内模型 `available` 纳入模型级状态 | `gateway.py` |
| 5 | 3 并发集中扫描打爆免费档限流 | 扫描改单 worker 串行 + 按渠道限速（Gemini 350ms/NIM 300ms/OpenRouter 200ms/其他 150ms） | `frontend/index.html` |

完整排查见 `api-hub模型扫描异常排查报告.html`。

## Git / 版本

- 分支 `main`；最新 tag `v1.0.1`（HEAD `df8a367`）。
- **工作区有未提交改动**（本次修复 + 更早的 opencode 渠道预设、OpenRouter endpoints 只读查询等），提交前需一起过一遍、分 commit。
- **未发版**：等用户实测通过再 push + release。发布流程参考 PlanFlow 项目（删旧 tag + gh release 重建）。

## 运行与测试

```bash
cd E:\文档\workbuddy\api-hub
.venv\Scripts\python.exe -m pytest tests/ -q   # 34 passed，全部离线、不打外部 API
```

- 日常运行：双击 `API Hub.vbs`（静默托盘）；调试用 `run.bat`（带控制台）；源码态 `.venv\Scripts\python.exe desktop.py`。
- 端口 8787（`config.json` 的 `port`）；日志 `data/api-hub.log`；密钥 DPAPI 密文存在 `config.json`。

## 代码地图

- `app/gateway.py` — 核心状态机 + 候选路由 + `model_view`（三态视图）。全局状态表都在这。
- `app/main.py` — FastAPI 入口：`/v1/*` 网关、`/api/*` 管理、`model_test`（扫描）、lifespan（启动恢复/停机落盘）、后台健康循环。
- `app/throttle.py` — 429 自学限流水位（`_LEARN`/`_CALLS`），预判式换路。
- `app/store.py` — SQLite 用量 + `model_status.json`/`runtime_state.json` 落盘（`_atomic_write`）。
- `app/providers.py` — 各平台 `/models` 列表、额度查询、OpenRouter 只读 endpoints。
- `app/config.py` — 渠道预设 `PROVIDER_PRESETS`；敏感字段 DPAPI 加密（`vault.py`）。
- `desktop.py` — pywebview 窗口 + pystray 托盘 + 单实例锁。`frontend/index.html` — 全部前端（单文件）。

## 状态持久化要点（改可用性/恢复逻辑前必读）

模型可用性三态：`ok` 绿·可调 / `limited` 黄·限流冷却但仍属可用范畴 / `down` 红·402/403/下线等硬不可用。

- `model_status`（模型级，key=model，跨渠道）→ `model_status.json`。`available` **由 state 唯一决定**（只有 `ok` 可路由）。
- `channel_down`（渠道级，(模型,渠道)→理由）→ 硬不可用。`mark_channel_down/up` 即时落盘。
- `cooldown`/`unverified`/`ratelimit`/`throttle._LEARN` → `runtime_state.json`。
- 恢复顺序（lifespan）：`store.init()` → `restore_model_status()`（归正旧数据 + 回写磁盘）→ `restore_runtime_state()`（恢复 cooldown/unverified/channel_down/ratelimit/throttle，并 seed 保守冷却）。

## 已知问题 / 待办

- [ ] **扫描按钮语义**：按钮叫「扫描全部模型」，实为「只扫未测过的」。本次已加 toast 提示 + skip 原因显示 + 非免费模型标记已测（死循环已修），但按钮名本身仍易误解，可考虑更名「扫描未测」。
- [ ] OpenRouter 改名**无法自动迁移**的模型（`aya-expanse-32b`→`command-a`、`MiniMax-M2`→`minimax-m2.7` 等本体/版本也变的）需用户手动重扫那几个；迁移只做确定等价，不硬猜。
- [ ] `model_status` 幽灵数据（旧名、已不在任何渠道）未做保守清理（保留是为避免误删 HF 渠道还在用的旧名）。
- [ ] 魔搭受限后**候选列表仍然包含魔搭**（`candidates_for` 按 `channel_down`/`channel_cooling`/`cooldown` 过滤，但魔搭"受限"可能没触发这些）。现已通过 connect 失败 0.5 衰减快速沉底缓解，但根因可能需要魔搭自检/健康检查更积极降级。

## 平台保护机制（Q 额度/限流相关，代码已有，勿误删）

| 平台 | 保护点 | 落点 |
|---|---|---|
| OpenRouter | ① 模型状态查询接口 `openrouter_endpoints()`（只读、免费、不耗额度，查上游提供方/是否免费/可用率）；② 扫描前预判：无免费提供方直接 skip 不烧余额；③ 付费模型 skip 时 `mark_channel_down` + `mark_model_status(down)` | `providers.py` / `main.py:model_test` |
| 魔搭 ModelScope | ① 主动探测每轮≤5 个（其他渠道≤20）；② `last_probe_ok` 24h 去重（探测成功过不再重复探）；③ 渠道级 429 熔断（窗口内≥2 模型 429 → 账号级限流，整渠道冷却）；④ 前端扫描前弹确认框 | `main.py:probe_used_models` / `gateway.py` / `frontend` |
| Gemini/NIM/OpenRouter 等按模型 RPM 限流 | 扫描单 worker 串行 + 按渠道限速（Gemini 350ms/NIM 300ms/OpenRouter 200ms/其他 150ms） | `frontend/index.html:scanChannel` |
| 通用 429 | 分类冷却（分钟级/每日额度/余额不足）+ 渠道冷却 + throttle 自学水位预判换路 | `gateway.py` |

## 关键坑（每条都踩过）

1. **改持久化逻辑后不要启动真实服务验证**——启动会 refresh_all + probe_used_models，烧免费额度/触发 429。用一次性离线脚本 `monkeypatch store.RUNTIME_STATE_PATH/MODEL_STATUS_PATH` 到临时目录验证，跑完删除。
2. **Windows 上 `.py` 是 LF**；patch 工具模糊匹配可能把整文件行尾改成 CRLF（`git diff` 显 `\r`）。改完用 `python -c` 数 `b"\r\n"` 核对，混了就 `raw.replace(b"\r\n", b"\n")` 转回。`frontend/index.html` 本就是 CRLF，别误统一。
3. **模型级 vs 渠道级粒度不同**：`model_status` 是 model 级，`channel_down`/`cooldown` 是 (model,channel) 级，两边口径必须一致，否则 UI 矛盾。
4. **OpenRouter 模型 ID 会变**：迁移只做归一化后完全一致的确定等价；模型本体/版本也变的不迁（会错配），宁让用户重扫。
5. **`available` 必须由 state 唯一决定**：之前 `available = state != "down"` 导致 limited 冒充可用。修复后 `available = state == "ok"`，`restore_model_status` 启动归正 + 回写磁盘。