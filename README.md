# API Hub · 大模型聚合网关

自用免费token小工具，发出来给有需要的朋友。如果有帮助的话给点个star给个反馈。

把多个免费的大模型 API 聚合成一个 OpenAI 兼容接口：统一模型列表、自动排序可用模型、剩余额度显示、失败自动切换渠道、调用量统计。桌面程序（Windows），本地运行，数据不出本机。

## 功能

- **统一网关**：统一API地址 `http://127.0.0.1:8787/v1`，OpenAI 兼容（`/v1/models`、`/v1/chat/completions`、`/v1/embeddings`），任何支持自定义 OpenAI 地址的工具都能直连
- **智能路由**：四种路由策略（均衡/智能优先/稳定优先/速度优先），按成功率评分 + 延迟 EMA + 能力档位排序，失败自动换路；429/401/5xx 分类冷却（429 尊重 Retry-After，401 自动停用渠道）
- **模型别名**：把不同平台同一模型归并成组，组内故障切换
- **模型主动探测**：定期对近期用过的模型发 1-token 探测，提前发现下线模型
- **可用性排序**：健康检查定期拉取各渠道模型列表，可用模型置顶，收藏模型优先
- **额度显示**：OpenRouter（官方 `/credits`）、智谱（社区逆向接口）、硅基流动（`/user/info`）；其余平台显示本地统计用量
- **用量统计**：SQLite 记录每次调用的模型、渠道、token、延迟、成败，界面图表 + 请求明细
- **密钥加密**：API Key 与网关 Token 经 Windows DPAPI 加密后落盘
- **桌面体验**：pywebview 原生窗口 + 系统托盘常驻 + 开机自启；`API Hub.vbs` 静默启动（无控制台）
- **支持平台（预设 15 个）**：智谱 GLM、OpenRouter、Google Gemini（兼容层）、OpenAI、NVIDIA NIM、硅基流动、Groq、Cerebras、Mistral、Cohere、魔搭 ModelScope、HuggingFace、Together AI、智谱国际 Z.ai、可添加任意 OpenAI 兼容自定义源

## 平台支持

- **Windows**：完整支持 —— DPAPI 密钥加密、pywebview 原生窗口、系统托盘常驻
- **macOS / Linux**：网关后端可运行（依赖见 requirements.txt），两处自动降级：
  - 密钥**不加密落盘**（无 Windows DPAPI，`app/vault.py` 自动降级为明文存储并记录日志——自行权衡）
  - 桌面壳需按 pywebview 官方文档装对应系统依赖；无桌面/无托盘环境可改用 `server.py` 或 `desktop.py` 的浏览器降级模式

## 快速开始

1. 双击 `API Hub.vbs`（首次运行前先双击一次 `run.bat` 完成依赖安装）
2. 打开「渠道」标签页，添加至少一个渠道（选平台 → 粘贴 API Key → 保存）
3. 在其他工具里把 Base URL 设为 `http://127.0.0.1:8787/v1`，API Key 填界面顶部的**网关 Token**（点击复制）

- 关闭窗口 = 最小化到系统托盘（服务不中断），托盘右键可显示/隐藏、开关开机自启、退出
- 调试模式用 `run.bat`（保留控制台日志）；纯后台常驻用 `server.bat`
- 排障/看日志：`data/api-hub.log`

## 配置

所有配置存在 `config.json`（密钥字段为 DPAPI 密文），界面改不了的可直接编辑后重启。全新部署：首次启动会自动生成默认 `config.json`，也可参考仓库内的 `config.example.json`（两者结构一致，后者为空模板）：

```json
{
  "port": 8787,
  "check_interval_minutes": 10,
  "quota_interval_minutes": 5,
  "auth_enabled": true,
  "route_strategy": "balanced",
  "probe_used_models": true,
  "model_tiers": {}
}
```

- `route_strategy`：`balanced` / `quality` / `stability` / `speed`
- `model_tiers`：能力档位正则覆盖，如 `{"qwen.*max": 3, ".*-flash": 1}`
- `auth_enabled` 设为 `false` 可关闭网关 Token 鉴权（不建议）

## 目录结构

```
api-hub/
├── app/
│   ├── main.py        # FastAPI：对外 API + 管理 API + 后台健康检查/探测
│   ├── gateway.py     # 运行时状态：健康、评分、failover 排序、分类冷却
│   ├── providers.py   # 各平台模型列表与额度查询
│   ├── config.py      # 配置读写（加解密）与平台预设
│   ├── vault.py       # Windows DPAPI 密钥加密
│   ├── capability.py  # 模型能力档位启发式（供智能路由）
│   └── store.py       # SQLite 用量统计
├── frontend/index.html  # 界面（单文件，暗色主题）
├── desktop.py         # 桌面入口：pywebview 窗口 + 系统托盘 + 开机自启
├── server.py          # 无窗口模式
├── API Hub.vbs        # 静默启动（无控制台，日常使用双击它）
├── run.bat            # 调试模式（首次安装/带控制台）
├── tests/             # pytest 单元测试（离线）
├── config.json        # 配置（密钥为 DPAPI 密文）
└── data/              # usage.db 用量记录 + api-hub.log 运行日志
```

## 安全说明

- 服务只监听 `127.0.0.1`，不对外网开放；
- 网关接口（`/v1/*`）需要 Bearer Token 鉴权，Token 首次启动自动生成，界面上可一键复制
- **密钥加密存储**：渠道 API Key 与网关 Token 落盘前经 Windows DPAPI 加密（见 `app/vault.py`），config.json 中只有 `dpapi:` 密文。加密绑定当前用户与本机，文件被复制到别的机器解不开（届时需重新填写 Key）；非 Windows 环境自动降级为明文
- 拒绝跨域请求与异常 Host 头（防浏览器侧 CSRF / DNS rebinding 攻击）
- 运行日志滚动写入 `data/api-hub.log`（保留 3MB），日志中不含任何密钥，排障时可直接发送

## 已知限制

- 智谱额度接口为社区逆向，平台改版会失效（失效时自动降级为本地统计，不影响网关功能）
- Gemini 免费层无余额概念，显示的是本地统计的调用量
- 流式请求的 token 统计依赖上游响应中的 `usage` 字段，部分渠道不返回时记 0
- 「能力档位」是按模型名关键词的启发式估算，不是跑分数据；免费层模型是实验资源，无 SLA，可能随时变动

## 开发与测试

```bash
.venv\Scripts\python.exe -m pytest tests/ -q   # 离线单元测试
```

## 许可

MIT License —— 详见 [LICENSE](LICENSE)。欢迎 issue / PR。
