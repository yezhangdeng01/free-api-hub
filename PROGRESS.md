# API Hub · 项目进度

> 大模型聚合网关：把多个免费大模型 API 聚合成一个 OpenAI 兼容 `/v1` 接口 + 本地管理界面 + 桌面托盘。开源（MIT），仓库 `github.com/yezhangdeng01/free-api-hub`。
> **下次继续前读这里**（当前状态 + 待办 + 机制要点），具体"怎么修"看 `api-hub` skill，逐轮修复细节看 `git log`。

## 一句话状态

**v1.0.3 已发版（2026-09-11）；v1.0.4 待发（本地已修未 push）**。v1.0.4 修的是 v1.0.3 引入/遗留的两个真实故障：① 「重启后前端 404」根因修复（端口竞态，见下方机制节）；② 托盘左键点图标不弹窗口。无已知阻塞，见下方「待办」。

## 版本时间线（本次交接涉及）

| 版本 | 主题 | 关键改动 |
|---|---|---|
| v1.0.0 | 首发 | 聚合网关 + 管理界面 + 托盘，GitHub Actions 自动构建 Windows 绿色版 |
| v1.0.1 | 措辞 + gitignore | README 完善；排除本地 venv |
| v1.0.2 | 可用性语义 + 故障切换 | `available=state==ok`（limited 不再冒充可用）；`is_permanent_failure` 统一 4xx/429 判定；`restore_model_status` 归正旧数据 + 回写磁盘；connect 0.5 衰减降权；connect 超时 15s→8s；持久化（throttle/channel_down/原子写/托盘优雅退出/单实例锁）；OpenRouter 命名迁移；扫描串行限速 |
| v1.0.3 | 挂起切换 + 账户级限额 + 统计 + 用法页 | read 超时 300s→120s（挂起 2min 切换）；魔搭 daily 型 429 → 整渠道冷却到明天（`mark_channel_quota_exhausted` + `channel_cool` 持久化）；柱状图数值标签 + 最小高度；请求日志按钮反馈 + 统计页 3s 轮询；网关用法页去硬编码 |
| **v1.0.4（未发）** | **重启 404 根因 + 托盘左键** | ① `_serve` 改为「先 `_bind_listen` 抢端口（SO_REUSEADDR 绕过 TIME_WAIT + 探测活跃监听者），再 `uvicorn.Server(config).run(sockets=[sock])`」——**删掉了 v1.0.3 那版在 `uvicorn.run` 外面重试的做法**（它每次都重跑 lifespan，`aclose()` 掉 `shared_client` → 全渠道 "client has been closed"）；② 单实例锁去掉 SO_REUSEADDR（Windows 上会让锁失效）+ 5s 重试；③ `wait_port` 20s→45s；④ 托盘「显示窗口」改为 default+invisible（恢复左键唤起，且不占右键菜单），删「隐藏窗口」（X 就是隐藏）；⑤ `app/main.py` lifespan 兜底重建被 `aclose()` 过的 `shared_client`；新增 `tests/test_desktop.py` 固化这两处回归 |

## 待办（下次从这里接手）

- [ ] **GitHub Actions 自动打包发版**：当前**没有** `.github/workflows/`，Windows 绿色版是手动 PyInstaller 打包 + 手动上传 asset。规模还小（自用小工具 + 分享给朋友），暂不必加。若将来发版频繁 / 要支持 mac/Linux 多平台，再上 Actions（`.github/workflows/build-windows.yml`：build + PyInstaller + 上传 asset + 打 tag 触发）。
- [ ] **流式请求中途挂起**：现在 read 超时 120s 覆盖了「首字节前挂起」（截图 301s 场景）。但「流式已返回 200 + 吐了几个 chunk 后中途卡住」的场景，`_stream_gen` 的 `aiter_bytes()` 抛 ReadTimeout 会**断流而非干净切换**，Hermes 端看到的是断流。若实测遇到，需做「流中断后由客户端重试」或「网关预缓冲 N 字节再转发」。
- [ ] **魔搭受限仍可能被选中**（历史遗留）：`candidates_for` 按 `channel_down`/`channel_cooling`/`cooldown` 过滤，魔搭"受限"若没触发这些（比如只是评分低但没冷却），仍可能进候选。已用 connect 0.5 衰减 + 账户级渠道冷却缓解；根因要靠健康检查更积极地把受限渠道降权。
- [ ] **OpenRouter 改名无法自动迁移的模型**（`aya-expanse-32b`→`command-a`、`MiniMax-M2`→`minimax-m2.7` 等本体/版本也变的）：迁移只做确定等价，这几个需用户手动重扫。
- [ ] **`model_status` 幽灵数据**（旧名、已不在任何渠道）：暂保留（避免误删 HF 渠道还在用的旧名），保守清理留待以后。
- [ ] **`classify_429` 对「余额不足」误判为 daily**：GLM 余额不足的 429 会被判成 daily → `mark_channel_quota_exhausted` 把整渠道冷却到明天（截图里的「限流熔断中（账号级限流）」就是它）。永久性欠费应走 `is_permanent_failure` → `down`，不该占用 channel_cool。观察到即可修。
- [ ] **扫描按钮命名**：按钮叫「扫描全部模型」，实为「只扫未测过的」。已加 toast + skip 原因提示缓解，按钮名本身可考虑改「扫描未测」。

## 端口 / 重启机制（**改之前务必读，别再改回「重试 uvicorn.run」**）

`desktop.py` 的启动路径只有一条：

```
main() → _acquire_single_instance_lock(8787+1000) → threading.Thread(_serve)
_serve(port) → _bind_listen(port) 抢到监听 socket → uvicorn.Server(Config(app,...)).run(sockets=[sock])
```

三个 Windows 硬事实（都踩过）：

1. **uvicorn 的 lifespan startup 在 bind 之前执行**。所以「bind 失败就在 `uvicorn.run()` 外面重试」是灾难：每次重试都跑一遍 app 启动（建 `shared_client` + 起 `_bg_loop`），失败退出又跑一遍 shutdown（`await shared_client.aclose()`）→ 后台线程还活着却拿着已关闭的 client → **全渠道 `Cannot send a request, as the client has been closed.`，看起来就是「所有渠道无法连接、没有模型可用」**（2026-09-11 19:15 现场：日志里 `[Errno 10048]` 后紧跟 7 条 client closed）。
2. **asyncio.create_server 在 Windows 上 `reuse_address=False`**（`os.name == "posix"` 才 True）。旧实例被 `os._exit` 硬杀后，前端那条 8787 连接留在 TIME_WAIT，naive bind 直接 `[Errno 10048]`。→ 必须自己建 socket 并 `SO_REUSEADDR` 后再交给 uvicorn（`Config.bind_socket()` 也做了同样的事，但它 bind 失败会 `sys.exit`，无法自控重试）。
3. **Windows 的 SO_REUSEADDR 也允许抢「活跃监听者」的端口**（Linux 不允许）。所以抢端口前先 `_port_has_listener()`（connect 探测）确认没人监听，否则新旧实例会同时监听 8787。同理**单实例锁不能用 SO_REUSEADDR**（否则第二个实例照样绑得上，锁形同虚设）。

重启流程 `_restart()`：先 `Popen` 新进程 → 自己 `os._exit(0)`。新进程靠 `_bind_listen` 等旧进程让出端口（最多 25s），`wait_port` 给 45s。

## 运行与测试

```bash
cd E:\文档\workbuddy\api-hub
.venv\Scripts\python.exe -m pytest tests/ -q   # 37 passed，全部离线、不打外部 API
```

- 日常：双击 `API Hub.vbs`（静默托盘）；调试 `run.bat`（带控制台）；源码态 `.venv\Scripts\python.exe desktop.py`。
- 端口 8787（`config.json` 的 `port`）；日志 `data/api-hub.log`；启动/退出流水 `data/launch.log`；密钥 DPAPI 密文存 `config.json`。
- **改完持久化/路由/启动逻辑后务必彻底重启托盘**（任务管理器确认 `pythonw.exe desktop.py` 全消失再启动），否则跑的是旧代码。
- 托盘交互：**左键单击图标 = 显示并置前窗口**（pystray 的 default 项，`visible=False`）；右键菜单 = 打开配置文件夹 / 重启服务 / 开机自启 / 退出。

## 代码地图

- `app/gateway.py` — 核心状态机 + 候选路由 + `model_view`（三态）+ 429 分类 + 账户级渠道冷却。
- `app/main.py` — FastAPI 入口：`/v1/*` 网关 + 故障切换循环、`/api/*` 管理、`model_test`（扫描）、lifespan（启动恢复/停机落盘）、`_bg_loop` 后台健康/探测循环、`shared_client`（`_new_client()` 造，120s/8s）。
- `app/throttle.py` — 429 自学限流水位，预判式换路。
- `app/store.py` — SQLite 用量 + `model_status.json`/`runtime_state.json` 落盘（`_atomic_write`）。
- `app/providers.py` — 各平台 `/models` 列表、额度查询、OpenRouter 只读 endpoints。
- `app/config.py` — 渠道预设 + 敏感字段 DPAPI 加密（`vault.py`）。
- `desktop.py` — pywebview 窗口 + pystray 托盘 + 单实例锁 + `_bind_listen`/`_serve`（端口竞态）。`frontend/index.html` — 全部前端（单文件，CRLF 行尾）。

## 状态持久化要点（改可用性/恢复逻辑前必读）

三态：`ok` 绿·可调 / `limited` 黄·暂时受限（限流/冷却，仍算可用范畴但**不可路由**）/ `down` 红·硬不可用（402/403/404/余额不足）。

- **`available` 由 `state` 唯一决定（只有 `ok` 可路由）**，`restore_model_status` 启动归正旧数据 + 回写磁盘。
- `model_status`（模型级）→ `model_status.json`；`channel_down`（渠道级硬失败）即时落盘；`cooldown`/`unverified`/`ratelimit`/`channel_cool`/`throttle._LEARN` → `runtime_state.json`（`channel_cool` 含账户级当天额度冷却到明天）。
- 恢复顺序：`store.init()` → `restore_model_status()` → `restore_runtime_state()`（恢复各状态 + 给仍有效的 (渠道,模型) seed 300s 保守冷却，避免重启回绿）。
- 渠道的 `valid`/`error`（红点「连接失败」）是**内存态**，重启后由 `refresh_all()` 健康检查重刷；由于异常路径会把探测过的模型写 `limited`+「连接失败（暂时）」，坏实例跑久了会留下一批假受限，重启后靠启动时那轮 `probe_used_models()` 逐条纠正（不行就手动扫一次）。

## 平台保护机制（额度/限流，代码已有，勿误删）

| 平台 | 保护点 | 落点 |
|---|---|---|
| OpenRouter | ① `openrouter_endpoints()` 只读查上游提供方/免费/可用率（不耗额度）；② 扫描前预判无免费提供方直接 skip 不烧余额；③ 付费 skip 时 `mark_channel_down` + `mark_model_status(down)` | `providers.py` / `main.py:model_test` |
| 魔搭 | ① 主动探测每轮≤5 个（其他渠道≤20）+ 24h 去重；② **daily 型 429（当天次数用完）→ `mark_channel_quota_exhausted` 整渠道冷却到明天**（账户级，不等第二个模型）；③ 渠道级 429 熔断；④ 前端扫描前确认框 | `main.py:probe_used_models/_classify` / `gateway.py` |
| 按模型 RPM（Gemini/NIM/OpenRouter） | 扫描单 worker 串行 + 按渠道限速（Gemini 350ms/NIM 300ms/OpenRouter 200ms/其他 150ms） | `frontend:scanChannel` |
| 通用 | read 超时 120s / connect 8s；connect 失败 0.5 衰减快速沉底；429 分类冷却 + throttle 自学预判 | `main.py` / `gateway.py` |

## 关键坑（每条都踩过）

1. **改持久化逻辑别启动真实服务验证**（会 refresh_all + probe_used_models，烧免费额度）。用一次性离线脚本 `monkeypatch` `store.RUNTIME_STATE_PATH`/`MODEL_STATUS_PATH` 到临时目录验证；**别用 execute_code 的持久 kernel 会话反复改全局态**（会污染，出现假阴性），用独立 `terminal` 进程复核。**验证启动/端口逻辑就用 stub app**（把 `desktop.app` 换成带 lifespan 计数的 FastAPI，不碰真实渠道）。
2. **Windows `.py` 是 LF**，patch 模糊匹配可能整文件变 CRLF（`git diff` 显 `\r`）；改完数 `b"\r\n"` 核对，混了转回。`frontend/index.html` 本就是 CRLF，别误统一。
3. **模型级 vs 渠道级粒度**：`model_status` 是 model 级，`channel_down`/`cooldown`/`channel_cool` 是 (model,channel) 级，口径必须一致。
4. **OpenRouter 模型 ID 会变**：迁移只做归一化后完全一致的确定等价；本体/版本变的不迁，宁可让用户重扫。
5. **`available` 必须由 state 唯一决定**：旧 `available = state != "down"` 导致 limited 冒充可用（300+ 假绿）。
6. **`httpx.Timeout(read, connect)` 第一参数是 read 不是 connect**：魔搭/deepseek「连接成功但挂起」场景是 read 超时（曾 300s 死等），不是 connect。
7. **账户级 vs 按模型限流两码事**：魔搭按每日次数，daily 型 429 是账户级（整 Key 都 429）；Gemini/NIM 按模型 RPM 是 minute 型。`classify_429` 返回的 label 决定走哪条冷却。
8. **pystray 的「左键动作」就是 default 项**：`Icon.__call__ → Menu.__call__ → 第一个 default 项`。删掉 default 项 = 左键点图标没反应。想让它不出现在右键菜单：`MenuItem(..., default=True, visible=False)`（`Menu.__call__` 遍历全部 items，`__iter__` 只给可见项）。

## 发版流程（本项目）

1. 改代码 → `pytest` 全绿 + `node --check` JS 通过 + `.py` 行尾 LF 核对。
2. 分主题 commit（fix/feat/docs），`git push origin main`。
3. 打 tag：`git tag vX.Y.Z && git push origin vX.Y.Z`（同名旧 tag 要先 `git tag -d` + push 删除）。
4. `gh release create vX.Y.Z --notes-file <临时md>`（或 `--generate-notes` 从 git log 自动生成）；重建前先 `gh release delete`（删 release 不会删 tag）。
5. Windows 绿色版（zip）**手动 PyInstaller 打包上传**（无 Actions），或让用户 clone 源码跑。
