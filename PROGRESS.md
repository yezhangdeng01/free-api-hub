# API Hub · 项目进度

> 大模型聚合网关：把多个免费大模型 API 聚合成一个 OpenAI 兼容 `/v1` 接口 + 本地管理界面 + 桌面托盘。开源（MIT），仓库 `github.com/yezhangdeng01/free-api-hub`。
> **接手先读这里**（当前状态 + 待办 + 机制要点）；「怎么改、有哪些坑」读 `api-hub` skill，逐轮细节看 `git log`。

## 一句话状态

**v1.0.4 已发版（2026-09-11）：tag + Release + Actions 自动打包 Windows 绿色版（28.06 MB），CI 内 52 tests passed。**

**本轮（2026-09-12，未发版，已本地 commit）：稳定分重做（只认真实调用 / 近 10 次窗口 / 启动回填）+
429 四档分类（分钟限流 / 免费日额度 / 付费余额不足→down / 未知）+ 地域封锁不再误标 down +
视觉漏标补修（agnes 全系实测支持视觉，实测表 `_VISION_VERIFIED` 优先级最高）。56 → **58 tests passed**。**

v1.0.4 = 重启 404 根因修复 + 托盘左键恢复 + 能力档位改「AA 榜分优先」+ 排序改「主维度优先 + 容忍带宽」
+ 视觉分组 `auto:vision`（视觉标记 111 → 359）+ 「模型」页 5 视图 + 设置页精简 + 删除裸 `auto`。
无已知阻塞；未决事项见「待办」。

## 版本时间线

| 版本 | 主题 |
|---|---|
| v1.0.0 | 首发：网关 + 管理界面 + 托盘 |
| v1.0.1 | README 完善 + gitignore |
| v1.0.2 | 可用性语义（`available = state == "ok"`）+ 故障切换优化 + 状态持久化/单实例锁 |
| v1.0.3 | 挂起切换（read 120s）+ 魔搭账户级冷却 + 统计页 + 网关用法页去硬编码 |
| **v1.0.4** | 重启 404 根因 + 托盘左键 + AA 榜分档位 + 策略带宽排序 + 视觉分组 + 视图/设置页精简 + 删裸 `auto` |

（各版本逐条细节看 `git log`；关键机制已固化进下方各节与 skill。）

## 机制要点

### 能力档位（`app/capability.py`）

判定顺序，先命中先返回：

1. `config.json` 的 `model_tiers` 正则覆盖（用户手调，最高优先级）；
2. 非对话模型（image/video/audio/embed/rerank/guard/translate/transcribe/ocr…）→ 轻量；
3. **AA 榜分**：≥ `_bench_hi`(p72) → 智能、≥ `_bench_mid`(p40) → 中档、否则轻量（阈值随观测分布自适应，样本 <20 用默认 33/17）；
4. 小尺寸 SKU（mini/nano/tiny/haiku/micro/small）或名字写明 ≤9B → 轻量；
5. 已知家族（gpt/o/qwen/gemini/claude/grok/deepseek/kimi/glm/minimax）：同代**且次版本在 `_FRONTIER_BAND=0.05` 内**才算前沿旗舰，
   否则降一档；加速档 flash/turbo → 中档、缩水档 lite/air → 轻量；例外 `_FLASH_OK_FAMILIES = {gemini, glm, deepseek}`
   的 flash 是同代主力不降档；名字写明 10~200B 封顶中档；
6. 无名家族**默认轻量**，只有「旗舰像」（ultra/super/max/pro/≥200B）给中档。

- **AA 榜分是白拿的**：`providers.fetch_models()` 调 OpenRouter `/models` 时顺带收割
  `benchmarks.artificial_analysis.intelligence_index`（零额外请求、无需密钥）；跨渠道对齐用
  `capability.norm_id()`（去厂商前缀 / `:free`·`:batch` 后缀 / 转小写）。
- 用户 `model_tiers` 覆盖**同时**影响档位标签与排序分（覆盖 → 该档顶值 `_OVERRIDE_ANCHOR`）。
- 家族版本正则的分隔符必须与厂商命名一致，写宽了会把参数量当版本号（`DeepSeek-R1-Distill-Qwen-14B` 曾污染整个 qwen 家族）。

### 排序（FE `compScore` / BE `_model_composite` / 路由 `_composite` 三处必须同口径）

三维都归一 0~1：**cap 智能**（AA 归一化连续分）/ **stab 稳定**（`eff_score = 0.7 + (score-0.7)·n/(n+3)`，样本量打折）
/ **spd 速度**（`400/(400+毫秒)`，流式优先用 TTFT）。

| 策略 | 主维度 | 容忍带宽 | 档内加权 |
|---|---|---|---|
| `auto:quality` | cap | 0.10 | 稳定 .60 / 速度 .40 |
| `auto:stability` | stab | 0.08 | 智能 .60 / 速度 .40 |
| `auto:speed` | spd | 0.10 | 智能 .55 / 稳定 .45 |
| `auto:vision` | cap | 0.10 | 稳定 .60 / 速度 .40 |
| `auto:balanced` | —（三维直接加权） | — | .35 / .40 / .25 |

- **为什么用带宽分档 + 档内加权**：连续分做严格「同等再比次要」几乎永不成立（AA 34.5 vs 34.6 就算不同）→ 等于只看主键；
  而「主维度给大权重」也压不住主维度的极端值。`_score_dims()` 返回「档号 + 档内加权∈[0,1)」，天然实现「先分档、同档比次要」。
- 无 AA 榜分 → 档位锚点 `_TIER_ANCHOR = {3: 0.76, 2: 0.28, 1: 0.10}`（取该档下沿，不让没上榜的挤到榜上前）。
- 排序**硬分组**（在策略分之前）：**收藏置顶 → 可用(ok) → 受限(limited) → 策略分 → 版本号 → 名称**；
  路由候选 / `/v1/models` / 界面 `modelSort` 三处同口径（`/v1/models` 曾漏「可用优先」，已补）。
- 「收藏置顶」是用户拍板的产品决策（「收藏就是优先使用」）：收藏模型一律排未收藏之前、组内才比策略分。

### auto 路由 = 按排序逐个试

`RESERVED_AUTO` **5 个**，与界面 5 个视图一一对应：`auto:balanced` / `auto:quality` / `auto:stability` / `auto:speed` / `auto:vision`。
**裸 `auto` 已删除**（用户拍板：与 `auto:balanced` 重复）→ 现在填 `auto` 会被当普通模型名查、查不到就 404。
`/v1/chat/completions` 收到这 5 个名字 → `candidates_for_auto(strategy, cfg)` 出候选 → **for 循环逐个尝试**（失败即冷却该组合、试下一个）。
候选层已过滤掉冷却中 / 渠道熔断 / 429 预判 / 模型级 down 的 (模型×渠道)，所以「**能用的排前面**」在候选层就保证了。

### 视觉分组 `auto:vision`

只保留 `meta_of()["vision"]` 为真的模型，再按视觉策略排。**视觉能力用平台数据判定**：OpenRouter `/models` 的
`architecture.input_modalities` 含 `image` 即支持（与 AA 榜分同一次请求白拿）；没数据才回退名字启发式
（`_VISION` + `_VISION_NEG` 排除 TTS/音频/绘图）。实测 750 个渠道模型：**视觉标记 111 → 359**。
⚠️ 没有「视觉能力」专用榜单——**排强弱**用 AA 文本分当代理（强模型的视觉一般也强），汇报时要说清这个边界。
用途：Hermes 三个 profile 的「辅助视觉模型」都填 `auto:vision`（连不上自动换下一个能看图的）。
**Hermes 侧接线**：`provider: apihub` + `key_env: HERMES_CUSTOM_APIHUB_API_KEY`（网关 Token，不是上游渠道 key，详见 skill）。

### 「模型」页视图与设置页

- 排序标签 5 个（均衡 / 智能优先 / 稳定优先 / 速度优先 / 视觉）= **纯视图**：只决定列表排序，**不发** `/api/settings`、
  不写 `route_strategy`（路由策略由客户端模型名决定）；选择存 localStorage、刷新保持，首次打开用 `route_strategy` 兜底。
  视图名 ↔ 客户端模型名一一对应，所以**列表顺序 = 该策略的实际切换顺序**。
- 「可用 / 受限」计数写在两个开关上（随当前视图统计），搜索框旁只显示「共 N 个」；工具栏下方**没有**解释行、用法页**没有**别名对照表
  （用户嫌冗余：「自动切换规则」那几句已说清）。
- 特点列收窄：渠道名短写（魔搭 / HF / NIM / Gemini / GLM，hover 保留全名）、档位与 AA 合成一个 chip（`智能 34.5`）、
  视觉用 `👁`、渠道 chip ≤4 同行。实测 1320px 下 100 行**全部单行**（含「所有模型都标视觉」的最坏情况）。
- 设置页只剩**一个设置项**：健康检查间隔（下拉 10 分钟~6 小时，写 `check_interval_minutes`，后台循环每轮重读 config →
  **保存即生效、不用重启**）。「路由策略」下拉已删（`auto:*` 由模型名决定）；「模型主动探测」「429 预判」开关已删
  （默认开启，要关去 config.json 改 `probe_used_models` / `adaptive_preemption`）。

## 状态持久化（改可用性 / 恢复逻辑前必读）

三态：`ok` 绿·可路由 / `limited` 黄·限流或冷却（**不可路由**，但展示上仍属「可用」范畴）/ `down` 红·硬不可用（402/403/404/余额不足）。

- **`available` 由 `state` 唯一决定**（只有 ok 可路由）；`restore_model_status()` 启动时归正旧数据并一次性回写磁盘。
- `model_status`（模型级）→ `model_status.json`；`channel_down`（渠道级硬失败）即时落盘；
  `cooldown` / `unverified` / `ratelimit` / `channel_cool`（含账户级当天额度冷却到明天）/ `throttle._LEARN` /
  `stats`（渠道评分：分数/延迟/TTFT/样本数，跨重启累计）→ `runtime_state.json`。
- 恢复顺序：`store.init()` → `restore_model_status()` → `restore_runtime_state()`；后者给仍在有效期内的 (渠道,模型)
  seed 300s 保守冷却，避免重启即回绿再撞限。
- 落盘节流：`mark_result` 走 1s 节流 + 后台每 30s 兜底 flush；托盘退出 / 停机前手动 `save_runtime_state()`（`os._exit` 会跳过 lifespan 清理）。

## 平台保护机制（代码已有，勿误删）

| 平台 | 保护点 |
|---|---|
| OpenRouter | ① 只读 endpoints 查上游提供方/免费/可用率（不耗额度）；② 扫描前预判无免费提供方直接 skip，不烧余额；③ 付费 skip 时标 down |
| 魔搭 | ① 主动探测每轮 ≤5 个（其他渠道 ≤20）+ 24h 去重；② daily 型 429 → 整渠道冷却到明天（账户级，不等第二个模型）；③ 渠道级 429 熔断 |
| 按模型 RPM（Gemini/NIM/OpenRouter） | 扫描单 worker 串行 + 按渠道限速（Gemini 350ms / NIM 300ms / OpenRouter 200ms / 其他 150ms） |
| 通用 | read 超时 120s、connect 8s；connect 失败 0.5 衰减快速沉底；429 分类冷却 + throttle 自学预判 |

## 启动 / 端口（改前必读，别再改回「在 `uvicorn.run` 外面重试」）

```
main() → _acquire_single_instance_lock(9787) → Thread(_serve)
_serve → _bind_listen(8787) 先抢监听 socket → uvicorn.Server(Config(app,...)).run(sockets=[sock])
```

- uvicorn 的 lifespan startup 在 bind **之前**执行 → 在 `uvicorn.run()` 外面重试会反复跑 lifespan
  （`aclose()` 掉 `shared_client`）→ 后台线程拿着已关闭的 client → **全渠道 `client has been closed`**
  （v1.0.3 的真实故障，v1.0.4 改成先抢端口再交给 uvicorn）。
- Windows 的 `SO_REUSEADDR` 既能绕过 TIME_WAIT（**必须用**，asyncio 在 Windows 上 `reuse_address=False`），
  **也**允许抢「活跃监听者」的端口（Linux 不允许）→ 抢端口前先 `_port_has_listener()` 探测；**单实例锁绝不能用 `SO_REUSEADDR`**。
- 托盘：**左键单击 = 显示并置前窗口**（pystray default 项 + `visible=False`，不占右键菜单）；右键 = 打开配置文件夹 / 重启服务 / 开机自启 / 退出。
- **改完 `.py` 必须彻底重启托盘**（任务管理器确认 `pythonw.exe desktop.py` 全消失再启动），否则跑的是旧代码。
  排查「改了没生效」先看进程启动时间 vs fix commit 时间（`Get-CimInstance Win32_Process` / `git log --format='%h %ci'`）。

## 运行 / 测试 / 代码地图

```bash
cd E:\文档\workbuddy\api-hub
.venv\Scripts\python.exe -m pytest tests/ -q     # 52 passed，全部离线、不打外部 API
```

- 日常：双击 `API Hub.vbs`（静默托盘）；调试 `run.bat`（带控制台）；源码 `.venv\Scripts\python.exe desktop.py`。
  端口 8787（`config.json` 的 `port`）；日志 `data/api-hub.log`；启动/退出流水 `data/launch.log`；密钥 DPAPI 密文存 `config.json`。
- `app/gateway.py` 状态机 + 候选路由 + 三态视图 + 429 分类 + 策略分；`app/capability.py` 档位 + AA 榜分 + 视觉判定；
  `app/main.py` FastAPI（`/v1/*`、`/api/*`、扫描、lifespan、后台健康/探测循环）；`app/throttle.py` 429 自学水位；
  `app/store.py` SQLite 用量 + 原子落盘；`app/providers.py` 各平台 `/models` + AA/视觉收割 + 额度查询；
  `desktop.py` pywebview 窗口 + pystray 托盘 + 单实例锁 + 端口预绑定；`frontend/index.html` 全部前端（单文件，CRLF）。
- 改动套路与 19 条踩过的坑见 `api-hub` skill（含前端 stub 验证、离线验证状态逻辑、Windows 行尾、pystray/pywebview 细节等）。

## 待办

- [ ] **策略参数按手感微调**（等用户实测后定）：主维度带宽 0.10/0.08/0.10、档内加权、均衡 0.35/0.40/0.25、档位锚点。
      可选的进阶：用 pairwise 容差比较替代「分档标量」以消除档位边界效应。
- [ ] **流式中途挂起**：read 超时覆盖了「首字节前挂起」，但「已 200 + 吐了几个 chunk 后卡住」时 `aiter_bytes()` 抛 ReadTimeout 会**断流而非干净切换**。实测遇到再做「预缓冲 N 字节」或客户端重试。
- [x] ~~**`classify_429` 把「余额不足」误判为 daily**~~ → **已修（2026-09-12）**：改成四档
      `paid_balance`（→down，要充值）/ `free_daily`（账户级冷却到明天）/ `minute`（90s）/ `unknown`（300s），
      并**结合渠道类型**判定（魔搭「每日免费额度用完」的正文就是 `insufficient balance`，光看字面会误判成欠费）。
- [ ] **魔搭「受限但未冷却」时仍可能进候选**（已用 connect 0.5 衰减 + 账户级渠道冷却缓解）。
- [ ] **OpenRouter 改名且本体/版本也变的模型**（`aya-expanse-32b`→`command-a`、`MiniMax-M2`→`minimax-m2.7`）无法自动迁移，需手动重扫。
- [ ] **`model_status` 幽灵数据**（旧名、已不在任何渠道）：暂保留（避免误删 HF 渠道仍在用的旧名）。
- [ ] **「扫描全部模型」按钮名不符实**（实为只扫未测过的），可改名「扫描未测」。

## 发版流程

1. 改代码 → `pytest` 全绿 + `node --check` 通过 + `.py` 行尾 LF 核对。
2. 分主题 commit（fix / feat / docs，用 `git add <显式路径>`，别 `-A`）→ `git push origin main`。
3. `git tag vX.Y.Z && git push origin vX.Y.Z` → **触发 `.github/workflows/release.yml`**：CI 内跑 pytest →
   PyInstaller 打包 Windows 绿色版 → 压缩 zip → 挂到对应 Release（同名旧 tag 需先 `git tag -d` + 删远端 tag）。
4. `gh release create vX.Y.Z --title vX.Y.Z --notes-file <临时md>` 写发行说明（重建先 `gh release delete`；删 release 不删 tag）。
5. 用户约定：**先本地 commit 攒着，实测通过再 push + 发版**；`gh release` 只能 attach 到已 push 的 tag。
