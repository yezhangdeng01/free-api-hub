"""API 聚合网关 — FastAPI 入口

对外：OpenAI 兼容接口（/v1/models、/v1/chat/completions）
对内：管理 API（/api/*）+ 前端页面
"""
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import capability
from . import config as cfgmod
from . import gateway, providers, store, throttle
from .config import PROVIDER_PRESETS

if getattr(sys, "frozen", False):
    # PyInstaller 打包：静态资源（frontend/）在 _internal 内，数据文件（config.json/data）在 exe 同目录
    FRONTEND = os.path.join(sys._MEIPASS, "frontend", "index.html")
else:
    FRONTEND = os.path.join(cfgmod.ROOT, "frontend", "index.html")


def _hdr(v):
    """HTTP 头只允许 ASCII：渠道/模型名含中文时转成 ? 避免 latin-1 编码 500"""
    if v is None:
        return ""
    try:
        v.encode("latin-1")
        return v
    except UnicodeEncodeError:
        return v.encode("ascii", "replace").decode("ascii")

# 文件日志：滚动保留 3MB（pythonw 无控制台时 sys.stderr 为 None，StreamHandler 会失效，需跳过）
import sys as _sys
LOG_PATH = os.path.join(cfgmod.ROOT, "data", "api-hub.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_log_handlers = [RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
if _sys.stderr is not None:
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=_log_handlers)
logger = logging.getLogger("api-hub")


def _new_client() -> httpx.AsyncClient:
    """网关共享上游客户端：read 120s / connect 8s（上游挂起 2 分钟就换路）。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=8.0))


shared_client = _new_client()


# ---------------- 后台健康检查 ----------------
_bg = {"last_check": 0.0, "last_quota": 0.0}


async def probe_used_models():
    """模型级主动探测：对近期真实用过的模型发 1-token 请求，提前发现已下线的模型。

    成本控制：只探测过去 7 天成功调用过的模型，每渠道每轮最多 20 个、max_tokens=1。
    """
    cfg = cfgmod.load_config()
    if not cfg.get("probe_used_models", True):
        return
    used = store.distinct_models(days=7)
    if not used:
        return
    now = time.time()
    for ch in cfg["channels"]:
        if not ch.get("enabled", True):
            continue
        cs = gateway.channels.get(ch["id"])
        if not cs or not cs.valid or gateway.channel_cooling(ch["id"]):
            continue
        # 魔搭等按每日请求次数计额度的平台：探测也在烧额度。
        # 每轮最多探 5 个，且 24 小时内探测成功过的不再重复探
        is_ms = ch.get("type") == "modelscope"
        cap = 5 if is_ms else 20
        targets = []
        for m in used:
            key = (m, ch["id"])
            if m not in cs.models:
                continue
            if gateway.cooldown.get(key, 0) > now or key in gateway.channel_down:
                continue
            if is_ms and now - gateway.last_probe_ok.get(key, 0) < 86400:
                continue
            targets.append(m)
            if len(targets) >= cap:
                break
        if not targets:
            continue
        url = ch["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ch['api_key']}",
                   "Content-Type": "application/json"}
        ok_cnt = 0
        for m in targets:
            try:
                t0 = time.time()
                r = await shared_client.post(
                    url, json={"model": m, "messages": [{"role": "user", "content": "ping"}],
                               "max_tokens": 16 if ch.get("type") in ("openrouter", "opencode") else 1},
                    headers=headers)
                latency = int((time.time() - t0) * 1000)
                gateway.note_ratelimit_headers(ch["id"], m, r.headers)
                if r.status_code == 200:
                    gateway.mark_result(m, ch["id"], True, latency)
                    gateway.mark_model_status(m, True, "", ch.get("name"))
                    gateway.last_probe_ok[(m, ch["id"])] = time.time()
                    ok_cnt += 1
                else:
                    kind = _kind_of(r.status_code)
                    if r.status_code == 429:
                        label, secs, _ = gateway.classify_429(r.text[:300], ch.get("type"),
                                                              _parse_retry_after(r.headers))
                        if label == "paid_balance":
                            # 探测发现账户欠费：按渠道级硬不可用处理（探测只碰可用性，不写稳定分）
                            cs = gateway.get_cs(ch["id"])
                            cs.valid = False
                            cs.error = "账户余额/额度不足（探测 429）"
                            secs = 0
                        gateway.mark_result(m, ch["id"], False, kind=kind,
                                            cooldown_seconds=secs)
                    else:
                        gateway.mark_result(m, ch["id"], False, kind=kind,
                                            retry_after=_parse_retry_after(r.headers))
                    if gateway.is_geo_block(r.text[:300]):
                        # 地域封锁：本机出口网络问题，不是模型不可用 → 不标 down、不记硬失败
                        gateway.mark_model_status(m, True, "地域限制（本机出口网络）",
                                                  ch.get("name"), state="limited")
                        logger.warning("渠道[%s] 模型[%s] 被上游地域限制（非模型问题）",
                                       ch.get("name"), m)
                        continue
                    if r.status_code in (400, 402, 403):
                        # 付费/权限/下线 → 该渠道上此模型硬不可用（按渠道级记录），
                        # 模型级也记为 down（与扫描口径一致），避免探测把硬失败覆盖成可用
                        gateway.mark_channel_down(m, ch["id"],
                            f"HTTP {r.status_code}: {r.text[:160]}")
                        gateway.mark_model_status(m, False,
                            f"渠道[{ch.get('name')}] 不可用 (HTTP {r.status_code})",
                            ch.get("name"), state="down")
                    elif r.status_code == 429:
                        # 渠道级 429 计数（账号级限流熔断）
                        gateway.note_channel_429(ch["id"], m,
                                                 _parse_retry_after(r.headers))
                        gateway.mark_model_status(m, True,
                            f"暂时不可用 (HTTP {r.status_code})", ch.get("name"), state="limited")
                    else:
                        gateway.mark_model_status(m, True,
                            f"暂时不可用 (HTTP {r.status_code})", ch.get("name"), state="limited")
                    if r.status_code in (401, 403):
                        cs.valid = False
                        cs.error = f"运行时检测: Key 无效 (HTTP {r.status_code})"
                        logger.warning("渠道[%s] 探测时发现 Key 无效 (%s)，暂停使用", ch.get("name"), r.status_code)
                        break
                    logger.warning("渠道[%s] 模型[%s] 探测失败 (HTTP %s)",
                                   ch.get("name"), m, r.status_code)
            except Exception as e:
                gateway.mark_result(m, ch["id"], False, kind="connect")
                gateway.mark_model_status(m, True, "连接失败（暂时）", ch.get("name"), state="limited")
                logger.warning("渠道[%s] 模型[%s] 探测连接失败: %s", ch.get("name"), m, str(e)[:120])
        logger.info("渠道[%s] 模型探测完成: %d/%d 可用", ch.get("name"), ok_cnt, len(targets))


async def refresh_all():
    cfg = cfgmod.load_config()
    gateway.sync_channels(cfg)
    chs = [ch for ch in cfg["channels"] if ch.get("enabled", True)]
    # 并行健康检查（限并发 4），避免串行等待几十秒导致启动后长时间空白
    sem = asyncio.Semaphore(4)

    async def one(ch):
        async with sem:
            try:
                cs = await gateway.refresh_channel(shared_client, ch)
            except Exception as e:
                logger.warning("渠道[%s] 检查异常: %s", ch.get("name"), e)
                return
            if cs.valid:
                logger.info("渠道[%s] 健康检查通过，%d 个模型，%dms",
                            ch.get("name"), len(cs.models), cs.latency_ms or 0)
            else:
                logger.warning("渠道[%s] 健康检查失败: %s", ch.get("name"), cs.error)

    await asyncio.gather(*(one(c) for c in chs), return_exceptions=True)
    # 自适应评级：用当前全部可见模型刷新「前沿代际」，
    # 新旗舰一出现，旧代自动降档（capability._observed_frontier）
    all_ids = {m for cs in gateway.channels.values() for m in cs.models}
    capability.update_frontier(all_ids)


async def _bg_loop():
    # 启动先刷一轮（健康检查 + 探测 + 额度）
    try:
        await refresh_all()
        await probe_used_models()
        await gateway.refresh_quotas(shared_client, cfgmod.load_config())
        _bg["last_check"] = _bg["last_probe"] = _bg["last_quota"] = time.time()
    except Exception:
        logger.exception("启动时初始检查失败")
    while True:
        try:
            await asyncio.sleep(30)
            gateway.flush_runtime_state()  # 把挂起的冷却/失败状态落盘（脏了才写）
            cfg = cfgmod.load_config()
            check_iv = max(1, cfg.get("check_interval_minutes", 30)) * 60
            probe_iv = max(1, cfg.get("probe_interval_minutes", 60)) * 60
            quota_iv = max(1, cfg.get("quota_interval_minutes", 15)) * 60
            now = time.time()
            if now - _bg["last_check"] >= check_iv:
                await refresh_all()
                _bg["last_check"] = time.time()
            if now - _bg["last_probe"] >= probe_iv:
                await probe_used_models()   # 1-token 探测频率独立（默认 60 分钟）
                _bg["last_probe"] = time.time()
            if now - _bg["last_quota"] >= quota_iv:
                await gateway.refresh_quotas(shared_client, cfg)
                _bg["last_quota"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("后台检查循环异常")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global shared_client
    # aclose() 过的 client 无法复用，之后所有上游请求都会报
    # "Cannot send a request, as the client has been closed."（表现为全部渠道连接失败）。
    # 正常生命周期里不会走到这里，作为启动兜底重建。
    if shared_client.is_closed:
        shared_client = _new_client()
    store.init()
    gateway.restore_model_status()  # 恢复已测模型状态，重启不丢
    gateway.restore_runtime_state()  # 恢复冷却/待验证/渠道级硬失败（扫描结果重启不丢）
    # 稳定分回填：稳定分只认真实调用，而历史真实调用都在 usage.db 里。重启后窗口是空的，
    # 不回填的话「稳定优先」要等很久才有样本（旧口径的 score/n 已按新口径丢弃）。
    try:
        n = gateway.backfill_stab(store.recent_outcomes(days=120, per_pair=gateway.STAB_WIN))
        if n:
            logger.info("稳定分回填：%d 个(模型,渠道)用历史真实调用补齐窗口", n)
    except Exception as e:
        logger.warning("稳定分回填失败（不影响启动）: %s", e)
    task = asyncio.create_task(_bg_loop())
    yield
    task.cancel()
    gateway.save_runtime_state()  # 停机前把内存状态落盘
    await shared_client.aclose()


app = FastAPI(title="API Hub", lifespan=lifespan)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """安全层：Host 校验（防 DNS rebinding）+ 跨域拒绝 + 网关 Token 鉴权"""
    path = request.url.path
    host = request.headers.get("host", "")
    # 1) Host 必须是本机（防 DNS rebinding 把内网服务映射到公网域名）
    if host and not (host.startswith("127.0.0.1") or host.startswith("localhost") or host.startswith("[::1]")):
        logger.warning("拒绝异常 Host 请求: %s %s", host, path)
        return JSONResponse({"detail": "服务只允许本机访问"}, 403)
    # 2) 带 Origin 头的请求必须是同源（跨域 JS 一律拒绝，不再提供 CORS）
    origin = request.headers.get("origin")
    if origin and not origin.startswith((f"http://{host}", f"https://{host}")):
        logger.warning("拒绝跨域请求: Origin=%s path=%s", origin, path)
        return JSONResponse({"detail": "拒绝跨域请求"}, 403)
    # 3) /v1/* 网关接口需要 Bearer Token
    if path.startswith("/v1/"):
        cfg = cfgmod.load_config()
        if cfg.get("auth_enabled", True):
            token = cfg.get("api_token", "")
            auth = request.headers.get("authorization", "")
            if not token or auth != f"Bearer {token}":
                return JSONResponse({"detail": "缺少或错误的 API Token（见 API Hub 界面顶部）"}, 401)
    return await call_next(request)


# ---------------- OpenAI 兼容接口 ----------------
@app.get("/v1/models")
async def v1_models():
    cfg = cfgmod.load_config()
    strategy = cfg.get("route_strategy", "balanced")
    pinned = list(cfg.get("pinned") or [])
    reserved = list(gateway.list_reserved_auto())
    models = gateway.model_view(cfg)
    aliases = gateway.alias_view(cfg)

    def sort_key(item_id: str, comp: float, usable: int = 0) -> tuple:
        """与前端 modelSort 对齐：置顶 → **可用优先** → 综合分 → 版本号 → 名称

        「可用优先」不能省：auto 是按这个顺序逐个试的，受限/不可用的排前面等于白等一轮。"""
        is_pin = 1 if item_id in pinned else 0
        ver = gateway._last_version(item_id) or -1.0
        return (-is_pin, usable, -comp, -ver, item_id)

    items = []
    for m in models:
        items.append((m["id"], gateway._model_composite(m, strategy),
                      0 if m.get("status") == "ok" else 1))
    for a in aliases:
        items.append((a["name"], gateway._model_composite({"tier": 2, "channels": []}, strategy), 0))
    items.sort(key=lambda x: sort_key(x[0], x[1], x[2]))
    ids = reserved + [x[0] for x in items]
    return {"object": "list",
            "data": [{"id": mid, "object": "model", "owned_by": "api-hub"} for mid in ids]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    model = body.get("model")
    if not model:
        raise HTTPException(400, "缺少 model 字段")

    cfg = cfgmod.load_config()
    # 「auto / auto:strategy」特殊名 → 网关自动选最优真实模型（且失败时自动跨模型切换）
    if gateway.is_reserved_auto(model):
        strategy = gateway.auto_strategy_of(model)
        candidates = gateway.candidates_for_auto(strategy, cfg)
        if not candidates:
            raise HTTPException(404, "当前没有任何可用模型（渠道未配置、健康检查未通过，或扫描后发现全部不可用）")
    else:
        candidates = gateway.candidates_for(model, cfg)
        if not candidates:
            raise HTTPException(404, f"模型 {model} 当前无可用渠道（未配置或健康检查未通过）")

    errors = []
    is_stream = bool(body.get("stream"))
    for cand in candidates:
        ch = cand["channel"]
        upstream_model = cand["model"]
        if upstream_model != model:
            body["model"] = upstream_model  # 别名切换时改写上游模型名
        cid = ch["id"]
        name = ch.get("name") or ch["type"]
        url = ch["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ch['api_key']}",
                   "Content-Type": "application/json"}
        t0 = time.time()
        throttle.record_call(cid, upstream_model)

        if is_stream:
            try:
                req = shared_client.build_request("POST", url, json=body, headers=headers)
                resp = await shared_client.send(req, stream=True)
            except Exception as e:
                _fail(cid, name, upstream_model, model, t0, errors, f"connect: {e}", exc=e)
                continue
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "ignore")[:300]
                ra = _parse_retry_after(resp.headers)
                gateway.note_ratelimit_headers(cid, upstream_model, resp.headers)
                await resp.aclose()
                _classify(cid, name, upstream_model, model, t0, errors,
                          resp.status_code, raw, ra, ch_type=ch.get("type"))
                continue
            # 收到 200 = 「上游接受了这次请求」→ 只更新可用性（解冷却/转绿），**不写稳定分**：
            # 真正的成败要等 _stream_gen 跑完（中途断流算失败）。
            gateway.mark_result(upstream_model, cid, True, source="probe")
            gateway.note_ratelimit_headers(cid, upstream_model, resp.headers)
            ms = gateway.get_model_status(upstream_model)
            if ms is not None and ms.get("state") == "down":
                gateway.mark_model_status(upstream_model, True, "", name)  # 真实成功 → 解除遗留 down
            return StreamingResponse(
                _stream_gen(resp, cid, name, model, t0, upstream_model),
                media_type="text/event-stream",
                headers={"X-Api-Hub-Channel": _hdr(name), "X-Api-Hub-Model": _hdr(upstream_model)})
        else:
            try:
                r = await shared_client.post(url, json=body, headers=headers)
            except Exception as e:
                _fail(cid, name, upstream_model, model, t0, errors, f"connect: {e}", exc=e)
                continue
            latency = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                gateway.note_ratelimit_headers(cid, upstream_model, r.headers)
                _classify(cid, name, upstream_model, model, t0, errors,
                          r.status_code, r.text[:300], _parse_retry_after(r.headers),
                          ch_type=ch.get("type"))
                continue
            gateway.note_ratelimit_headers(cid, upstream_model, r.headers)
            try:
                data = r.json()
            except Exception:
                gateway.mark_result(upstream_model, cid, False, latency, kind="bad_json", source="real")
                store.log_usage(cid, name, model, 0, 0, latency, False, "响应不是 JSON",
                                upstream_model=upstream_model)
                logger.warning("渠道[%s] 模型[%s] 响应不是 JSON", name, upstream_model)
                errors.append(f"{name}: 响应不是 JSON")
                continue
            gateway.mark_result(upstream_model, cid, True, latency, source="real")
            ms = gateway.get_model_status(upstream_model)
            if ms is not None and ms.get("state") == "down":
                gateway.mark_model_status(upstream_model, True, "", name)  # 真实成功 → 解除遗留 down
            u = data.get("usage") or {}
            store.log_usage(cid, name, model, u.get("prompt_tokens", 0),
                            u.get("completion_tokens", 0), latency, True,
                            upstream_model=upstream_model)
            logger.info("模型[%s] 由渠道[%s] 服务，%dms，%d tokens",
                        model, name, latency, u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
            return JSONResponse(data, headers={"X-Api-Hub-Channel": _hdr(name), "X-Api-Hub-Model": _hdr(upstream_model)})

    raise HTTPException(502, "所有渠道均失败。详情: " + " | ".join(errors[:5]))


def _conn_kind(exc) -> str:
    """连接类失败的细分。

    `local_net` = 本地 DNS/代理问题（getaddrinfo 失败等）——**不是上游的错**，所以既不该
    进稳定分，也不该当成模型不可用；`timeout` = 连上了但不响应；其余归 `connect`。"""
    t = str(exc).lower()
    if any(k in t for k in ("getaddrinfo", "name or service not known", "nodename nor servname",
                            "name resolution", "proxy")):
        return "local_net"
    if "timeout" in t or "timed out" in t:
        return "timeout"
    return "connect"


def _parse_retry_after(headers) -> int:
    try:
        return max(0, int(headers.get("retry-after", 0)))
    except (TypeError, ValueError):
        return 0


def _kind_of(status: int) -> str:
    """HTTP 状态 → 冷却分类"""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return "client"


def _quota_exhausted(cid: str) -> bool:
    """渠道账户余额是否已知 ≤ 0（只看平台余额接口查到的数据，没有就不猜）"""
    cs = gateway.get_cs(cid)
    q = getattr(cs, "quota", None)
    if isinstance(q, dict) and q.get("remaining") is not None:
        try:
            return float(q["remaining"]) <= 0
        except (TypeError, ValueError):
            return False
    return False


def _classify(cid, name, upstream_model, requested_model, t0, errors, status, raw,
              retry_after=None, ch_type=None):
    """按 HTTP 状态分类处理：鉴权失败直接停用渠道；429 分三档（余额不足→down /
    免费额度用尽→冷却到明天 / 每分钟限流→冷却几十秒）；其它 4xx → 该渠道上硬不可用。"""
    latency = int((time.time() - t0) * 1000)
    note = f" (上游模型 {upstream_model})" if upstream_model != requested_model else ""
    kind = _kind_of(status)
    cooldown_seconds = None
    if kind == "auth":
        cs = gateway.get_cs(cid)
        cs.valid = False
        cs.error = f"运行时检测: Key 无效或无权限 (HTTP {status})"
        logger.warning("渠道[%s] Key 鉴权失败 (HTTP %s)，暂停使用，等待下次健康检查", name, status)
        # 按 (模型, 渠道) 记录，不再把同名模型在其他渠道的状态一起标红
        gateway.mark_channel_down(upstream_model, cid, f"Key 无效: HTTP {status}")
    elif kind == "rate_limit":
        # 429 分三档：余额不足（要充值）/ 免费额度用尽（等明天）/ 每分钟限流（等一会）。
        # 判别必须结合渠道类型——魔搭的「每日免费额度用完」正文就是 insufficient balance，
        # 光看字面会误判成欠费；平台余额接口查得到时以它为准（_quota_exhausted）。
        label, secs, text429 = gateway.classify_429(raw, ch_type, retry_after)
        if label == "paid_balance" or _quota_exhausted(cid):
            # 账户余额/额度不足：等不来自愈，要充值或换 key → 按渠道级「硬不可用」处理
            # （与 Key 无效同一条路径：先挂掉 cs.valid，下次健康检查再试）
            cs = gateway.get_cs(cid)
            cs.valid = False
            cs.error = f"账户余额/额度不足（HTTP 429）：{raw[:120]}"
            gateway.mark_channel_down(upstream_model, cid, "账户余额不足，需充值")
            gateway.mark_model_status(requested_model, False, "账户余额不足，需充值（等不会恢复）",
                                      name, state="down")
            gateway.mark_result(upstream_model, cid, False, kind="balance")
            store.log_usage(cid, name, requested_model, 0, 0, latency, False,
                            f"HTTP 429 余额/额度不足(需充值): {raw[:200]}", upstream_model=upstream_model)
            logger.warning("渠道[%s] 模型[%s] 账户余额/额度不足，按硬不可用处理（需充值或换 key）", name, upstream_model)
            errors.append(f"{name}: 账户余额不足，需充值{note}")
            return
        if label == "free_daily":
            logger.warning("渠道[%s] 模型[%s] 免费额度用完 (free_daily 429)，"
                           "整个渠道冷却到明天，该渠道下所有模型一并受限", name, upstream_model)
            gateway.mark_channel_quota_exhausted(cid, text429)
            gateway.mark_model_status(requested_model, False, text429, name, state="limited")
            gateway.mark_result(upstream_model, cid, False, kind="rate_limit",
                                cooldown_seconds=secs)
            store.log_usage(cid, name, requested_model, 0, 0, latency, False,
                            f"HTTP 429 免费额度用完: {raw[:200]}", upstream_model=upstream_model)
            errors.append(f"{name}: 免费额度用完 (HTTP 429){note}")
            return
        logger.warning("渠道[%s] 模型[%s] 限流 (429, %s)，冷却 %ds，Retry-After=%s",
                       name, upstream_model, label, secs, retry_after)
        throttle.observe_429(cid, upstream_model)   # 供自学水位
        if gateway.note_channel_429(cid, upstream_model, retry_after):
            logger.warning("渠道[%s] 窗口内多个模型连续 429，判定账号级限流，整个渠道熔断 10 分钟", name)
        gateway.mark_model_status(requested_model, True, text429, name, state="limited")
    elif kind == "server":
        logger.warning("渠道[%s] 模型[%s] 上游服务错误 (%s)", name, upstream_model, status)
        gateway.mark_model_status(requested_model, True, f"上游暂时故障 (HTTP {status})", name, state="limited")
    else:  # 4xx 客户端类（含 400/402/403/404）—— 通常是付费/权限/参数/已下线
        if gateway.is_geo_block(raw):
            # 地域封锁：本机出口网络的问题，不是模型不可用 → 不标 down、不记硬失败
            logger.warning("渠道[%s] 模型[%s] 被上游地域限制（非模型问题）: %s",
                           name, upstream_model, raw[:120])
            gateway.mark_model_status(requested_model, True, "地域限制（本机出口网络）",
                                      name, state="limited")
            kind = "geo"
        else:
            gateway.mark_channel_down(upstream_model, cid, f"HTTP {status}: {raw[:160]}")
            logger.warning("渠道[%s] 模型[%s] 请求被拒 (%s): %s", name, upstream_model, status, raw[:150])
    gateway.mark_result(upstream_model, cid, False, None, kind=kind,
                        retry_after=retry_after, cooldown_seconds=cooldown_seconds,
                        source="real")
    store.log_usage(cid, name, requested_model, 0, 0, latency, False,
                    f"HTTP {status}{note}: {raw[:200]}", upstream_model=upstream_model)
    errors.append(f"{name}: HTTP {status}"
                  + (f" (Retry-After {retry_after}s)" if retry_after else "") + note)


def _fail(cid, name, upstream_model, requested_model, t0, errors, msg, exc=None):
    """请求阶段就失败（连不上/本地网络）。

    本地 DNS/代理问题（`local_net`）**不算模型的账**：不改模型状态、不进稳定分、不冷却——
    否则你自己网络一抖，所有渠道的模型都被打成「受限」。其余失败（超时/断连）才记。"""
    kind = _conn_kind(exc) if exc is not None else "connect"
    if kind != "local_net":
        gateway.mark_result(upstream_model, cid, False, None, kind=kind, source="real")
        gateway.mark_model_status(requested_model, True, "连接失败（可能暂时不可达）", name, state="limited")
    else:
        logger.warning("渠道[%s] 本地网络/DNS 问题，不改模型状态: %s", name, msg[:160])
    store.log_usage(cid, name, requested_model, 0, 0, int((time.time() - t0) * 1000), False, msg,
                    upstream_model=upstream_model)
    logger.warning("渠道[%s] 模型[%s] 连接失败 (%s): %s", name, upstream_model, kind, msg[:200])
    errors.append(f"{name}: {msg[:120]}")


async def _stream_gen(resp, cid, name, model, t0, upstream_model=""):
    """流式转发，同时从 SSE 里抽 usage 记账，并记录**首字节时间(TTFT)**与**是否完整收尾**。

    成功只有在整条流正常读完时才算：中途断流/超时记 `kind="stream_break"` 的失败
    （旧版一收到 200 就记成功，断流被当成功，稳定分完全看不到这种最真实的失败）。
    客户端主动断开（GeneratorExit/CancelledError）不是模型的错 → 不记失败、只落用量。"""
    usage = {}
    buf = b""
    ttft = None
    completed = False
    aborted = False
    err = None
    try:
        async for chunk in resp.aiter_bytes():
            if ttft is None and chunk:
                ttft = max(1, int((time.time() - t0) * 1000))
            yield chunk
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"data:") and b'"usage"' in line:
                    try:
                        j = json.loads(line[5:].strip())
                        u = j.get("usage")
                        if isinstance(u, dict):
                            usage = u
                    except Exception:
                        pass
        completed = True
    except (GeneratorExit, asyncio.CancelledError):
        aborted = True       # 客户端断开：不算上游失败
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.warning("渠道[%s] 模型[%s] 流式中断: %s", name, upstream_model, err[:180])
        raise
    finally:
        if ttft is not None and upstream_model:
            gateway.mark_ttft(upstream_model, cid, ttft)
        await resp.aclose()
        if upstream_model and not aborted:
            gateway.mark_result(upstream_model, cid, completed,
                                int((time.time() - t0) * 1000) if completed else None,
                                kind=None if completed else "stream_break", source="real")
        store.log_usage(cid, name, model, usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                        int((time.time() - t0) * 1000), completed,
                        error=("客户端断开" if aborted else (err[:200] if err else None)),
                        upstream_model=upstream_model)


@app.post("/api/models/test")
async def model_test(req: Request):
    """主动探测某个模型的可用性（1-token 请求）。

    不传 channel_id：按路由策略从候选渠道里挑；传了 channel_id：强制走该渠道（供渠道级扫描用）。
    """
    b = await req.json()
    model = (b.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    cfg = cfgmod.load_config()
    cid = (b.get("channel_id") or "").strip()

    if cid:
        ch = next((c for c in cfg["channels"] if c["id"] == cid), None)
        cs = gateway.channels.get(cid)
        if (not ch or not ch.get("enabled", True) or not cs or not cs.valid
                or model not in cs.models):
            # 该渠道没这个模型 ≠ 模型全局不可用，不做全局标记（避免跨渠道污染）
            return {"available": False, "error": "渠道无效、已停用或未提供该模型"}
        candidates = [{"channel": ch, "model": model}]
    else:
        # 手动测试专用候选：绕过所有冷却（渠道级/模型级/预判），用户主动点测试
        # 愿意承担额度，测成功即恢复（mark_result ok 会解除 channel_cool）。
        candidates = gateway.candidates_for_test(model, cfg)
        if not candidates:
            gateway.mark_model_status(model, False, "没有任何可用渠道（渠道未配置或健康检查未通过）")
            raise HTTPException(404, "该模型当前没有任何可用渠道")

    last_err = ""
    for cand in candidates:
        ch = cand["channel"]
        upstream_model = cand["model"]
        is_or = ch.get("type") == "openrouter"
        # 深探测渠道：OpenRouter / OpenCode 免费模型里推理类多，
        # max_tokens=1 会被部分模型以 400 拒绝（0 可用的假象），改用 16-token 真生成 + 重试一次
        deep = ch.get("type") in ("openrouter", "opencode")
        url = ch["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ch['api_key']}",
                   "Content-Type": "application/json"}
        # OpenRouter 付费模型真探测会烧余额（余额还可能变成负数）。
        # 渠道级扫描时先查免费的提供方元数据（只读、零成本），确认有免费提供方才发真请求
        if is_or and cid:
            eps = await providers.openrouter_endpoints(
                shared_client, upstream_model, ch.get("api_key"))
            if eps is not None and not any(e.get("free") for e in eps):
                gateway.mark_channel_down(upstream_model, ch["id"], "非免费模型（所有提供方均收费）")
                # 补模型级状态：非免费对免费网关即不可用，且必须写 model_status，
                # 否则 tested 仍为 false，前端会反复把该模型当"未测"重新扫描（死循环）
                gateway.mark_model_status(model, False, "非免费模型（所有提供方均收费）",
                                          ch.get("name"), state="down")
                return {"available": False, "skipped": True, "channel": ch.get("name"),
                        "error": "跳过：非免费模型（所有提供方均收费），未消耗额度"}
        attempts = 2 if deep else 1
        result = None
        for attempt in range(attempts):
            try:
                t0 = time.time()
                r = await shared_client.post(
                    url,
                    json={"model": upstream_model,
                          "messages": [{"role": "user", "content": "ping"}],
                          "max_tokens": 16 if deep else 1},
                    headers=headers)
                latency = int((time.time() - t0) * 1000)
            except Exception as e:
                last_err = f"connect: {e}"
                gateway.mark_result(upstream_model, ch["id"], False, kind="connect")
                if attempt + 1 < attempts:
                    continue
                if cid:
                    gateway.mark_model_status(model, False, last_err, ch.get("name"), state="limited")
                    return {"available": False, "channel": ch.get("name"), "error": last_err}
                result = {"available": False, "channel": ch.get("name"), "error": last_err}
                break
            if r.status_code == 200:
                gateway.note_ratelimit_headers(ch["id"], upstream_model, r.headers)
                gateway.mark_model_status(model, True, "", ch.get("name"))
                gateway.mark_result(upstream_model, ch["id"], True, latency)
                out = {"available": True, "channel": ch.get("name"), "latency_ms": latency}
                if ch.get("type") == "openrouter":
                    eps = await providers.openrouter_endpoints(
                        shared_client, upstream_model, ch.get("api_key"))
                    note = providers.endpoints_note(eps)
                    if note:
                        out["note"] = note
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            gateway.note_ratelimit_headers(ch["id"], upstream_model, r.headers)
            if r.status_code == 429:
                label429, secs429, text429 = gateway.classify_429(
                    r.text[:300], ch.get("type"), _parse_retry_after(r.headers))
                # 余额/额度不足（付费渠道）→ 硬不可用，等也不会恢复
                if label429 == "paid_balance" or (_quota_exhausted(ch["id"]) and label429 != "minute"):
                    gateway.mark_channel_down(upstream_model, ch["id"],
                                              "账户余额不足，需充值 (HTTP 429)")
                    gateway.mark_model_status(model, False, "账户余额不足，需充值",
                                              ch.get("name"), state="down")
                    gateway.mark_result(upstream_model, ch["id"], False, kind="balance")
                    return {"available": False, "skipped": True, "channel": ch.get("name"),
                            "error": "余额不足 (HTTP 429)，按不可用处理"}
                # 免费额度用完：账户级限额，整个渠道所有模型一起受限，冷却到明天
                if label429 == "free_daily":
                    gateway.mark_channel_quota_exhausted(ch["id"], text429)
                    gateway.mark_model_status(model, False, text429,
                                              ch.get("name"), state="limited")
                    gateway.mark_result(upstream_model, ch["id"], False, kind="rate_limit",
                                        cooldown_seconds=secs429)
                    return {"available": False, "skipped": True, "channel": ch.get("name"),
                            "error": "免费额度用完 (HTTP 429)，整个渠道冷却到明天"}
                gateway.note_channel_429(ch["id"], upstream_model,
                                         _parse_retry_after(r.headers))
                gateway.mark_result(upstream_model, ch["id"], False,
                                    kind=_kind_of(r.status_code), cooldown_seconds=secs429)
            else:
                gateway.mark_result(upstream_model, ch["id"], False,
                                    kind=_kind_of(r.status_code),
                                    retry_after=_parse_retry_after(r.headers))
            # 深探测渠道 429/5xx 通常是单次上游抽风，重试一次再定论
            if (deep and attempt + 1 < attempts
                    and r.status_code in (429, 500, 502, 503, 504)):
                await asyncio.sleep(1.5)
                continue
            # 400/402/403/404/405（付费/权限/参数/已下线）+ 余额不足 429 → 永久不可用，记 down
            # 例外：地域封锁是本机网络问题（is_permanent_failure 内部已排除），按 limited 处理
            hard = gateway.is_permanent_failure(r.status_code, r.text[:200])
            if hard:
                gateway.mark_channel_down(upstream_model, ch["id"], last_err)
            state = "down" if hard else "limited"
            gateway.mark_model_status(model, False, last_err, ch.get("name"), state=state)
            out = {"available": False, "channel": ch.get("name"),
                   "error": last_err, "status": r.status_code}
            if ch.get("type") == "openrouter" and not hard:
                # 附加免费只读的上游提供方体检信息，帮用户判断是模型问题还是提供方问题
                eps = await providers.openrouter_endpoints(
                    shared_client, upstream_model, ch.get("api_key"))
                note = providers.endpoints_note(eps)
                if note:
                    out["note"] = note
            if hard or cid:
                return out
            result = out  # 非强制渠道且暂时受限 → 继续尝试下一候选
            break
    return result or {"available": False, "error": last_err or "所有渠道均失败"}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """向量化代理：与 chat 相同的 failover 逻辑"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    model = body.get("model")
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    cfg = cfgmod.load_config()
    candidates = gateway.candidates_for(model, cfg)
    if not candidates:
        raise HTTPException(404, f"模型 {model} 当前无可用渠道")
    errors = []
    for cand in candidates:
        ch = cand["channel"]
        upstream_model = cand["model"]
        if upstream_model != model:
            body["model"] = upstream_model
        cid = ch["id"]
        name = ch.get("name") or ch["type"]
        url = ch["base_url"].rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {ch['api_key']}",
                   "Content-Type": "application/json"}
        t0 = time.time()
        throttle.record_call(cid, upstream_model)
        try:
            r = await shared_client.post(url, json=body, headers=headers)
        except Exception as e:
            _fail(cid, name, upstream_model, model, t0, errors, f"connect: {e}", exc=e)
            continue
        latency = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            _classify(cid, name, upstream_model, model, t0, errors,
                      r.status_code, r.text[:300], _parse_retry_after(r.headers),
                      ch_type=ch.get("type"))
            continue
        try:
            data = r.json()
        except Exception:
            gateway.mark_result(upstream_model, cid, False, latency, kind="bad_json", source="real")
            store.log_usage(cid, name, model, 0, 0, latency, False, "响应不是 JSON",
                            upstream_model=upstream_model)
            errors.append(f"{name}: 响应不是 JSON")
            continue
        gateway.mark_result(upstream_model, cid, True, latency, source="real")
        u = data.get("usage") or {}
        store.log_usage(cid, name, model, u.get("prompt_tokens", 0), 0, latency, True,
                        upstream_model=upstream_model)
        logger.info("Embedding[%s] 由渠道[%s] 服务，%dms", model, name, latency)
        return JSONResponse(data, headers={"X-Api-Hub-Channel": _hdr(name), "X-Api-Hub-Model": _hdr(upstream_model)})
    raise HTTPException(502, "所有渠道均失败。详情: " + " | ".join(errors[:5]))


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------- 管理 API ----------------
def _mask(k: str) -> str:
    return (k[:6] + "****" + k[-4:]) if k and len(k) > 10 else "****"


def _find_channel(cfg: dict, cid: str):
    for ch in cfg["channels"]:
        if ch["id"] == cid:
            return ch
    return None


@app.get("/api/overview")
async def overview():
    cfg = cfgmod.load_config()
    chans = []
    for ch in cfg["channels"]:
        cs = gateway.channels.get(ch["id"]) or gateway.ChannelState()
        chans.append({
            "id": ch["id"], "name": ch.get("name") or ch["type"], "type": ch["type"],
            "base_url": ch["base_url"], "enabled": ch.get("enabled", True),
            "key_masked": _mask(ch["api_key"]),
            "valid": cs.valid, "error": cs.error, "last_check": cs.last_check,
            "latency_ms": cs.latency_ms, "models_count": len(cs.models),
            "models_available_count": gateway.channel_available_models(ch["id"]),
            "cool_until": gateway.channel_cool.get(ch["id"], 0),
            "quota": cs.quota, "quota_ts": cs.quota_ts, "quota_error": cs.quota_error,
        })
    models = gateway.model_view(cfg)
    usage_map = store.model_usage_24h()
    for m in models:
        meta = capability.meta_of(m["id"])
        m["vision"] = meta["vision"]
        m["context"] = meta["context"]
        m["usage24"] = usage_map.get(m["id"], {"requests": 0, "ok": 0, "avg_latency": 0})
    return {"gateway": {"port": cfg["port"],
                        "base_url": f"http://127.0.0.1:{cfg['port']}/v1",
                        "auth_enabled": cfg.get("auth_enabled", True),
                        "token": cfg.get("api_token", "")},
            "settings": {"route_strategy": cfg.get("route_strategy", "balanced"),
                         "check_interval_minutes": cfg.get("check_interval_minutes", 30),
                         "probe_used_models": cfg.get("probe_used_models", True),
                         "adaptive_preemption": cfg.get("adaptive_preemption", True)},
            "channels": chans,
            "models": models,
            "aliases": gateway.alias_view(cfg),
            "pinned": cfg.get("pinned", [])}


@app.post("/api/refresh")
async def refresh():
    await refresh_all()
    await probe_used_models()
    await gateway.refresh_quotas(shared_client, cfgmod.load_config())
    return {"ok": True}


def _validate_channel(b: dict, allow_empty_key: bool = False):
    """校验并补全渠道字段，返回 (channel, error)"""
    t = b.get("type")
    if t not in PROVIDER_PRESETS:
        return None, f"未知平台类型: {t}"
    if not allow_empty_key and not (b.get("api_key") or "").strip():
        return None, "API Key 不能为空"
    base = (b.get("base_url") or "").strip() or PROVIDER_PRESETS[t]["base_url"]
    if not base:
        return None, "自定义类型必须填写 Base URL"
    ch = {
        "name": (b.get("name") or "").strip() or PROVIDER_PRESETS[t]["label"],
        "type": t,
        "base_url": base,
        "api_key": (b.get("api_key") or "").strip(),
        "enabled": bool(b.get("enabled", True)),
    }
    if not ch["api_key"]:
        ch["enabled"] = False  # 导入的渠道缺 Key 时先停用，补 Key 后再启用
    return ch, None


@app.post("/api/channels/test-connection")
async def test_connection(req: Request):
    """不落盘测试：用表单里的 平台/URL/Key 直接验证连通性（供「测试连接」按钮用）"""
    ch, err = _validate_channel(await req.json())
    if err:
        raise HTTPException(400, err)
    tmp_id = cfgmod.new_channel_id()
    ch["id"] = tmp_id
    try:
        cs = await gateway.refresh_channel(shared_client, ch)
        return {"valid": cs.valid, "error": cs.error,
                "latency_ms": cs.latency_ms, "models_count": len(cs.models)}
    finally:
        gateway.channels.pop(tmp_id, None)  # 不留状态残留


@app.post("/api/channels")
async def add_channel(req: Request):
    ch, err = _validate_channel(await req.json())
    if err:
        raise HTTPException(400, err)
    cfg = cfgmod.load_config()
    ch["id"] = cfgmod.new_channel_id()
    cfg["channels"].append(ch)
    cfgmod.save_config(cfg)
    gateway.sync_channels(cfg)
    if ch["enabled"]:
        await gateway.refresh_channel(shared_client, ch)
        await gateway.refresh_quotas(shared_client, cfg)
    return {"ok": True, "id": ch["id"]}


@app.put("/api/channels/{cid}")
async def update_channel(cid: str, req: Request):
    cfg = cfgmod.load_config()
    old = _find_channel(cfg, cid)
    if not old:
        raise HTTPException(404, "渠道不存在")
    ch, err = _validate_channel({**old, **await req.json()})
    if err:
        raise HTTPException(400, err)
    ch["id"] = cid
    cfg["channels"][cfg["channels"].index(old)] = ch
    cfgmod.save_config(cfg)
    if ch["enabled"]:
        await gateway.refresh_channel(shared_client, ch)
    return {"ok": True}


@app.delete("/api/channels/{cid}")
async def delete_channel(cid: str):
    cfg = cfgmod.load_config()
    old = _find_channel(cfg, cid)
    if not old:
        raise HTTPException(404, "渠道不存在")
    cfg["channels"].remove(old)
    cfgmod.save_config(cfg)
    gateway.sync_channels(cfg)
    return {"ok": True}


@app.post("/api/channels/{cid}/test")
async def test_channel(cid: str):
    cfg = cfgmod.load_config()
    ch = _find_channel(cfg, cid)
    if not ch:
        raise HTTPException(404, "渠道不存在")
    cs = await gateway.refresh_channel(shared_client, ch)
    return {"valid": cs.valid, "error": cs.error,
            "latency_ms": cs.latency_ms, "models_count": len(cs.models)}


@app.post("/api/channels/{cid}/quota")
async def refresh_quota(cid: str):
    cfg = cfgmod.load_config()
    ch = _find_channel(cfg, cid)
    if not ch:
        raise HTTPException(404, "渠道不存在")
    cs = gateway.get_cs(cid)
    q = await gateway.providers.check_quota(shared_client, ch)
    cs.quota_ts = time.time()
    if q is None:
        cs.quota, cs.quota_error = None, None
    elif q.get("kind") == "error":
        cs.quota_error = q.get("detail")
    else:
        cs.quota, cs.quota_error = q, None
    return {"ok": True, "quota": cs.quota, "quota_error": cs.quota_error}


@app.get("/api/usage")
async def usage(days: int = 7):
    return store.summary(min(max(days, 1), 90))


@app.get("/api/logs")
async def api_logs(limit: int = 50, offset: int = 0):
    return store.recent(min(max(limit, 1), 200), max(offset, 0))


@app.post("/api/aliases")
async def add_alias(req: Request):
    b = await req.json()
    name = (b.get("name") or "").strip()
    targets = [t.strip() for t in (b.get("targets") or []) if isinstance(t, str) and t.strip()]
    if not name or "/" in name:
        raise HTTPException(400, "别名不能为空且不能包含 /")
    if not targets:
        raise HTTPException(400, "至少填写一个目标模型 ID")
    cfg = cfgmod.load_config()
    cfg.setdefault("aliases", {})
    cfg["aliases"][name] = list(dict.fromkeys(targets))
    cfgmod.save_config(cfg)
    logger.info("添加模型别名[%s] -> %s", name, cfg["aliases"][name])
    return {"ok": True}


@app.delete("/api/aliases/{name}")
async def del_alias(name: str):
    cfg = cfgmod.load_config()
    if name in (cfg.get("aliases") or {}):
        cfg["aliases"].pop(name)
        cfgmod.save_config(cfg)
        logger.info("删除模型别名[%s]", name)
    return {"ok": True}


@app.post("/api/pins")
async def set_pin(req: Request):
    """收藏/取消收藏模型（持久化在 config.json，跨窗口/重启不丢）"""
    b = await req.json()
    model = (b.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    cfg = cfgmod.load_config()
    pinned = cfg.setdefault("pinned", [])
    if b.get("pinned"):
        if model not in pinned:
            pinned.append(model)
            cfgmod.save_config(cfg)
    else:
        if model in pinned:
            pinned.remove(model)
            cfgmod.save_config(cfg)
    return {"ok": True}


@app.post("/api/settings")
async def settings(req: Request):
    b = await req.json()
    cfg = cfgmod.load_config()
    if b.get("route_strategy") in ("balanced", "quality", "stability", "speed"):
        cfg["route_strategy"] = b["route_strategy"]
    if isinstance(b.get("check_interval_minutes"), (int, float)):
        iv = int(b["check_interval_minutes"])
        if 1 <= iv <= 1440:            # 健康检查间隔（分钟），越界忽略
            cfg["check_interval_minutes"] = iv
    if isinstance(b.get("probe_used_models"), bool):
        cfg["probe_used_models"] = b["probe_used_models"]
    if isinstance(b.get("adaptive_preemption"), bool):
        cfg["adaptive_preemption"] = b["adaptive_preemption"]
    cfgmod.save_config(cfg)
    logger.info("更新设置: %s", {k: b[k] for k in b
                                  if k in ("route_strategy", "check_interval_minutes",
                                           "probe_used_models", "adaptive_preemption")})
    return {"ok": True}


@app.post("/api/config/export")
async def config_export(req: Request):
    """导出配置到项目根目录。include_keys=False 时导出不含密钥的脱敏版"""
    b = await req.json()
    include_keys = bool(b.get("include_keys"))
    cfg = cfgmod.load_config()
    out = {
        "app": "api-hub", "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "port": cfg["port"], "route_strategy": cfg.get("route_strategy", "balanced"),
        "aliases": cfg.get("aliases", {}),
        "channels": [],
    }
    if include_keys:
        out["api_token"] = cfg["api_token"]
        out["_warning"] = "本文件包含密钥明文，请妥善保管，勿公开分享"
    for ch in cfg["channels"]:
        c = {"name": ch.get("name"), "type": ch["type"],
             "base_url": ch["base_url"], "enabled": ch.get("enabled", True)}
        if include_keys:
            c["api_key"] = ch["api_key"]
        out["channels"].append(c)
    tag = "full" if include_keys else "safe"
    path = os.path.join(cfgmod.ROOT, f"api-hub-export-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    logger.info("配置导出: %s (含密钥=%s)", path, include_keys)
    return {"ok": True, "path": path, "include_keys": include_keys}


@app.post("/api/config/import")
async def config_import(req: Request):
    """导入配置（渠道+别名合并追加）。缺 Key 的渠道会以停用状态导入"""
    b = await req.json()
    data = b.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            raise HTTPException(400, "导入内容不是合法 JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "导入内容不是合法 JSON")
    cfg = cfgmod.load_config()
    added, skipped = 0, 0
    for c in data.get("channels") or []:
        if not isinstance(c, dict):
            skipped += 1
            continue
        ch, err = _validate_channel(c, allow_empty_key=True)
        if err:
            skipped += 1
            continue
        ch["id"] = cfgmod.new_channel_id()
        cfg["channels"].append(ch)
        added += 1
    if isinstance(data.get("aliases"), dict):
        cfg.setdefault("aliases", {}).update(data["aliases"])
    cfgmod.save_config(cfg)
    gateway.sync_channels(cfg)
    logger.info("配置导入完成: 新增 %d 个渠道，跳过 %d 条", added, skipped)
    return {"ok": True, "added": added, "skipped": skipped}


@app.get("/")
async def index():
    return FileResponse(FRONTEND)
