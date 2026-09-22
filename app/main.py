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
from . import dialects
from . import gateway, providers, store, throttle
from . import responses as responses_tr
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


async def _reset_client():
    """重建共享客户端：睡眠/休眠唤醒后 keep-alive 池里的连接全是死的，
    继续用它们只会让唤醒后的第一次请求白挂一轮（DNS 失败同理，重连等于重新解析）。"""
    global shared_client
    old, shared_client = shared_client, _new_client()
    try:
        await old.aclose()
    except Exception:
        pass


# ---------------- 后台健康检查 ----------------
# net_down：连续「整轮全挂且全是本机网络错误」的次数（退避用，成功一轮即清零）；
# next_check：网络降级期的下一次检查时间（0 = 按 check_interval_minutes 正常排期）
# （last_probe 也在这里初始化：启动那一轮若抛异常，循环里再取它能少一次 KeyError）
_bg = {"last_check": 0.0, "last_probe": 0.0, "last_quota": 0.0, "last_bench": 0.0,
       "net_down": 0, "next_check": 0.0}


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
                    bad = _probe_body_ok(r)
                    if bad:
                        # 200 但内容不可信（错误对象/结构不对）→ 别标绿。
                        # 标 limited（不参与路由、但可恢复），不标 down，下轮探测还会再验一次
                        gateway.mark_result(m, ch["id"], False, kind="bad_json")
                        gateway.mark_model_status(m, True, f"响应异常（{bad}）",
                                                  ch.get("name"), state="limited")
                        logger.warning("渠道[%s] 模型[%s] 探测返回 200 但内容不可信: %s",
                                       ch.get("name"), m, bad)
                    else:
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
                        # 同 _classify：401 或「渠道最近没成功过」才认定账号级、停用整渠道；
                        # 否则只是这个模型没权限（付费/地区），不该把整渠道一起停掉
                        if r.status_code == 401 or not gateway.channel_recently_ok(ch["id"]):
                            cs.valid = False
                            cs.error = f"运行时检测: Key 无效 (HTTP {r.status_code})"
                            logger.warning("渠道[%s] 探测时发现 Key/账号级问题 (%s)，暂停使用",
                                           ch.get("name"), r.status_code)
                            break
                        logger.warning("渠道[%s] 模型[%s] 被拒 (HTTP %s)：渠道其他模型正常 → "
                                       "只标该模型，不停渠道", ch.get("name"), m, r.status_code)
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
    states = []          # [(渠道配置, 渠道状态)]，供整轮体检用（见 _note_sweep_health）

    async def one(ch):
        async with sem:
            try:
                cs = await gateway.refresh_channel(shared_client, ch)
            except Exception as e:
                logger.warning("渠道[%s] 检查异常: %s", ch.get("name"), e)
                return
            states.append((ch, cs))
            if cs.valid:
                logger.info("渠道[%s] 健康检查通过，%d 个模型，%dms",
                            ch.get("name"), len(cs.models), cs.latency_ms or 0)
            else:
                logger.warning("渠道[%s] 健康检查失败: %s", ch.get("name"), cs.error)

    # 榜分兜底、渠道健康检查**同时发起**：兜底只要 2~3 秒，渠道检查要 30~40 秒。
    # 若按顺序（先渠道后兜底）走，冷启动后要等全部渠道查完才有分，界面会有半分钟的
    # 「没分了」空窗（2026-09-15 实测：15:47:21 起服务，15:47:58 才补上分）。
    await asyncio.gather(
        *(one(c) for c in chs),
        _topup_bench_cache(cfg),
        return_exceptions=True,
    )
    # 自适应评级：用当前全部可见模型刷新「前沿代际」，
    # 新旗舰一出现，旧代自动降档（capability._observed_frontier）
    all_ids = {m for cs in gateway.channels.values() for m in cs.models}
    capability.update_frontier(all_ids)
    _note_sweep_health(chs, states)


def _note_sweep_health(chs: list, states: list):
    """整轮体检：所有渠道一起挂、且全是本机网络类错误 → 按「本机网络未就绪」处理。

    为什么需要（2026-09-16 实测事故）：睡眠唤醒后 DNS 还没起来，一轮健康检查七个渠道
    一起 `[Errno 11001] getaddrinfo failed`；而网络 13:51 早已恢复 —— 健康检查却要等
    下一个 `check_interval_minutes`（用户设的间隔，默认 30 分钟）才再来。用户看到的就是
    「所有渠道都报失败、没有模型可用，必须重启服务或手动测试一遍才行」。

    现在的处理：判定为本机问题 → 10s/20s/…/60s 退避重试（`_bg["next_check"]`），
    并在渠道错误里写明「本机网络未就绪 · Ns 后自动重试」，让界面上一眼能区分
    「上游挂了」和「本机没网」。成功一轮即清零退避。

    保守边界：只有**收齐了本轮全部渠道结果**、**全部失败**、且**每条错误都是本机网络类**
    才走这条路；任何一个渠道正常，或错误是 4xx/额度/权限，都按原来的排期走 ——
    否则会把「上游真的挂了」误判成本机问题而频繁重试。
    """
    fails = [cs for _ch, cs in states if not cs.valid]
    whole_sweep_down = bool(chs) and len(states) == len(chs) and len(fails) == len(states)
    if not whole_sweep_down or not all(gateway.is_local_net_error(cs.error) for cs in fails):
        _bg["net_down"] = 0
        _bg["next_check"] = 0.0
        return
    _bg["net_down"] += 1
    delay = min(60, 10 * _bg["net_down"])
    _bg["next_check"] = time.time() + delay
    for cs in fails:
        cs.error = f"本机网络未就绪（{cs.error or '连接失败'}）· {delay}s 后自动重试"
    logger.warning("整轮全挂且全是本机网络错误（第 %d 次）：判定为本机网络未就绪"
                   "（睡眠唤醒后 DNS 未就绪是典型），%d 秒后自动重试；"
                   "本轮只按「路由不可用」处理，不写模型状态/冷却，恢复后自动回绿",
                   _bg["net_down"], delay)


async def _topup_bench_cache(cfg: dict):
    """榜分缓存的兜底刷新：渠道那边没喂到新分时才去 OpenRouter 公开端点补一次。

    这一步存在的意义就是「OpenRouter 挂着也不该丢分」：
      · 渠道健康 → fetch_models 已经顺带收割了榜分，缓存是新鲜的，这里直接跳过（零额外请求）；
      · 渠道挂了/停用/Key 失效 → 缓存变旧，从**免密钥**的公开端点补齐；
      · 公开端点也够不着 → 记一条 warning 继续用本地缓存里的旧分，档位不受影响。
    """
    try:
        await _topup_bench_cache_inner(cfg)
    except Exception as e:      # 兜底路径绝不能反过来把主流程带崩
        logger.warning("榜分兜底异常（继续用本地缓存）: %s", str(e)[:160])


async def _topup_bench_cache_inner(cfg: dict):
    ttl = max(1, cfg.get("bench_cache_hours", 24))
    age = capability.cache_age_hours()
    if age is not None and age < ttl:
        return
    now = time.time()
    if now - _bg.get("last_bench", 0.0) < 600:   # 失败重试闸：10 分钟，别每轮都撞
        return
    _bg["last_bench"] = now
    st_before = capability.cache_status()
    info = await providers.fetch_bench_public(shared_client)
    if info["ok"]:
        logger.info("榜分兜底：公开源补入 %d 个榜分 / %d 个视觉标记（缓存此前 %s 小时未更新）",
                    info["bench"], info["vision"],
                    "∞" if st_before["age_h"] is None else st_before["age_h"])
    else:
        logger.warning("榜分兜底失败：继续使用本地缓存（%d 个榜分 / %d 个视觉标记）",
                       st_before["bench"], st_before["vision_ok"] + st_before["vision_no"])


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
            # 网络降级期改用 5s 心跳，让 10s 起的退避真正按点执行（否则会被 30s 的
            # 睡眠周期吃掉，「10 秒后重试」实际变成 30 秒）
            interval = 5 if _bg.get("next_check") else 30
            t_sleep = time.time()
            await asyncio.sleep(interval)
            # 睡眠/休眠唤醒检测：挂起期间墙钟照走、这一觉就睡过头了。唤醒后本机网络
            # 往往还没起来（DNS 尤其），所以立刻重查一轮，而不是等 check_interval 到点。
            drift = time.time() - t_sleep - interval
            if drift > 20:
                logger.info("检测到系统休眠唤醒（挂起约 %.0f 秒）：重建连接池并立即重查渠道",
                            drift)
                await _reset_client()
                _bg["last_check"] = 0.0
                _bg["next_check"] = 0.0
                _bg["net_down"] = 0
            gateway.flush_runtime_state()  # 把挂起的冷却/失败状态落盘（脏了才写）
            capability.save_cache()        # 榜分/视觉能力脏了才写
            cfg = cfgmod.load_config()
            check_iv = max(1, cfg.get("check_interval_minutes", 30)) * 60
            probe_iv = max(1, cfg.get("probe_interval_minutes", 60)) * 60
            quota_iv = max(1, cfg.get("quota_interval_minutes", 15)) * 60
            now = time.time()
            next_check = _bg.get("next_check") or 0.0
            if next_check:
                # 网络降级期：按短退避重试（10s~60s），不听 check_interval
                if now >= next_check:
                    await refresh_all()
                    _bg["last_check"] = time.time()
            elif now - _bg["last_check"] >= check_iv:
                await refresh_all()
                _bg["last_check"] = time.time()
            # 本机网络没就绪时别去烧探测/额度：那两样只会得到同样的解析失败，
            # 还会把模型状态写成一片「暂时不可用」
            if not _bg.get("next_check"):
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
    # 榜分/视觉能力缓存：**必须在渠道健康检查之前**恢复。榜分只有一个来源（OpenRouter 的
    # /models），OpenRouter 一挂或本轮还没刷完时，档位就会退回名字启发式 —— 先把上一个进程
    # 存下来的分装回去，评分不会因为「没连上上游」而消失。
    try:
        c = capability.load_cache()
        if c["loaded"]:
            logger.info("已恢复能力榜单缓存：%d 个 AA 榜分（阈值 %.1f / %.1f），%d 个视觉标记",
                        c["bench"], c["hi"], c["mid"], c["vision"])
        else:
            # 别静默：这里为空 = 本轮启动先按名字启发式定档，界面会短暂「没分」，
            # 排查时先看这行（2026-09-15 的「重启后没分了」就是缓存被清空导致的）。
            logger.warning("能力榜单缓存为空/不存在：先按名字启发式定档，"
                           "等渠道检查或公开榜分源补数据（不影响启动）")
    except Exception as e:
        logger.warning("能力榜单缓存恢复失败（不影响启动，将重新收割）: %s", e)
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
    capability.save_cache(force=True)  # 榜分/视觉能力落盘，下次启动直接可用
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
    # 3) /v1/* 网关接口需要 Token
    #    两种写法都认：`Authorization: Bearer <token>`（OpenAI 那套，也是本项目界面里给的），
    #    以及 Anthropic 客户端专用的 `x-api-key`（Claude Code / Anthropic SDK 只发这个头）
    if path.startswith("/v1/"):
        cfg = cfgmod.load_config()
        if cfg.get("auth_enabled", True):
            token = cfg.get("api_token", "")
            auth = request.headers.get("authorization", "")
            xkey = request.headers.get("x-api-key", "")
            if not token or (auth != f"Bearer {token}" and xkey != token):
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
    """OpenAI 兼容 chat 入口：**只负责解析请求体**，逻辑全在 `_chat_v1`。

    拆开是因为 `/v1/responses` 也要走这条路（翻译成 chat body 后复用同一套
    路由 / 冷却 / failover / 会话粘性 / 记账），复制一遍那段循环迟早会两边跑偏。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    return await _chat_v1(body, request)


async def _chat_v1(body: dict, request: Request, cfg: dict | None = None):
    model = body.get("model")
    if not model:
        raise HTTPException(400, "缺少 model 字段")

    cfg = cfg or cfgmod.load_config()
    sticky_scope = None   # (策略, 会话键, 可读标签)：只有 auto-* 才有「会话粘性」
    # 「auto-<strategy>」特殊名 → 网关自动选最优真实模型（且失败时自动跨模型切换）
    if gateway.is_reserved_auto(model):
        strategy = gateway.auto_strategy_of(model)
        hdr_id = next((request.headers.get(h) for h in gateway.STICKY_HEADERS
                       if request.headers.get(h)), "")
        skey, ssrc, slabel = gateway.session_key_of(body, hdr_id or "")
        candidates = gateway.candidates_for_auto(strategy, cfg)
        if not candidates:
            raise HTTPException(404, "当前没有任何可用模型（渠道未配置、健康检查未通过，或扫描后发现全部不可用）")
        hit = None
        if skey:
            sticky_scope = (strategy, skey, slabel)
            # 会话粘性：本会话上一次成功产出的模型优先（压过收藏）——避免同一对话里来回换模型
            candidates, hit = gateway.prefer_sticky(candidates, strategy, skey)
        # 每个 auto 请求一行：「为什么又换模型」直接看这行（会话指纹 + 首位 + 是否沿用了粘性）
        logger.info("auto 选路[%s] 会话=%s(%s)%s 候选=%d 首位=%s/%s%s", model, skey or "-",
                    ssrc or "认不出", f"「{slabel}」" if ssrc == "first_user" else "",
                    len(candidates), candidates[0]["channel"].get("name"), candidates[0]["model"],
                    (f" ← 沿用本会话上次成功的模型（{hit['idle_s']}s 前"
                     + ("，原渠道不可用已换渠道" if hit["switched_channel"] else "") + "）"
                     ) if hit else "")
    else:
        candidates = gateway.candidates_for(model, cfg)
        if not candidates:
            raise HTTPException(404, f"模型 {model} 当前无可用渠道（未配置或健康检查未通过）")

    errors = []
    is_stream = bool(body.get("stream"))
    for idx, cand in enumerate(candidates):
        # 候选列表里除最后一条外的失败都会被「换下一个渠道」接管 → 记账标 superseded，
        # 统计上不算客户端可见的失败（2026-09-17：用户看到 Hermes 一切正常，日志里却有失败）。
        superseded = idx < len(candidates) - 1
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
                _fail(cid, name, upstream_model, model, t0, errors, f"connect: {e}", exc=e,
                      superseded=superseded)
                continue
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "ignore")[:300]
                ra = _parse_retry_after(resp.headers)
                gateway.note_ratelimit_headers(cid, upstream_model, resp.headers)
                await resp.aclose()
                _classify(cid, name, upstream_model, model, t0, errors,
                          resp.status_code, raw, ra, ch_type=ch.get("type"),
                          superseded=superseded)
                continue
            # 收到 200 = 「上游接受了这次请求」→ 只更新可用性（解冷却/转绿），**不写稳定分**：
            # 真正的成败要等 _stream_gen 跑完（中途断流算失败）。
            gateway.mark_result(upstream_model, cid, True, source="probe")
            gateway.note_ratelimit_headers(cid, upstream_model, resp.headers)
            ms = gateway.get_model_status(upstream_model)
            if ms is not None and ms.get("state") == "down":
                gateway.mark_model_status(upstream_model, True, "", name)  # 真实成功 → 解除遗留 down
            return StreamingResponse(
                _stream_gen(resp, cid, name, model, t0, upstream_model, sticky_scope=sticky_scope),
                media_type="text/event-stream",
                headers={"X-Api-Hub-Channel": _hdr(name), "X-Api-Hub-Model": _hdr(upstream_model)})
        else:
            try:
                r = await shared_client.post(url, json=body, headers=headers)
            except Exception as e:
                _fail(cid, name, upstream_model, model, t0, errors, f"connect: {e}", exc=e,
                      superseded=superseded)
                continue
            latency = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                gateway.note_ratelimit_headers(cid, upstream_model, r.headers)
                _classify(cid, name, upstream_model, model, t0, errors,
                          r.status_code, r.text[:300], _parse_retry_after(r.headers),
                          ch_type=ch.get("type"), superseded=superseded)
                continue
            gateway.note_ratelimit_headers(cid, upstream_model, r.headers)
            try:
                data = r.json()
            except Exception:
                gateway.mark_result(upstream_model, cid, False, latency, kind="bad_json", source="real")
                store.log_usage(cid, name, model, 0, 0, latency, False, "响应不是 JSON",
                                upstream_model=upstream_model, superseded=superseded)
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
            if sticky_scope:
                # 会话粘性只认「真产出成功」（这里已经是 200 + JSON 可解析）
                gateway.mark_sticky(sticky_scope[0], sticky_scope[1], upstream_model, cid,
                                    label=sticky_scope[2])
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


def _probe_body_ok(r) -> str:
    """探测响应的**内容**校验：通过返回空串，否则返回原因（用作 limited 的理由）。

    只看 HTTP 200 会被两类「假可用」骗到：① 上游拿 200 包一个错误对象；
    ② 上游返回了结构不对的东西。这类模型会被标绿、进 auto 候选，真用起来才失败。

    注意**不要求 content 非空**：推理模型在 `max_tokens=1` 时可能只输出思考、
    content 为空，但 `choices` 本身是有的 —— 那不代表模型不可用，
    真用起来 max_tokens 大得多。所以判据只要「有 choices 且不是错误对象」。"""
    try:
        d = r.json()
    except Exception:
        return "响应不是 JSON"
    if not isinstance(d, dict):
        return "响应结构异常"
    if d.get("error"):
        return f"响应内含错误对象: {str(d['error'])[:80]}"
    choices = d.get("choices")
    if not isinstance(choices, list) or not choices:
        return "响应没有 choices"
    return ""


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
              retry_after=None, ch_type=None, superseded=False):
    """按 HTTP 状态分类处理：鉴权失败直接停用渠道；429 分三档（余额不足→down /
    免费额度用尽→冷却到明天 / 每分钟限流→冷却几十秒）；其它 4xx → 该渠道上硬不可用。

    `superseded=True` 表示这次失败之后网关还会换下一个渠道（客户端最终拿到结果），
    记账里标出来，统计上不算客户端可见的失败 —— 但**渠道/模型的健康信号照记**
    （mark_result / mark_channel_down 一个不落），两者是两回事。"""
    latency = int((time.time() - t0) * 1000)
    note = f" (上游模型 {upstream_model})" if upstream_model != requested_model else ""
    kind = _kind_of(status)
    cooldown_seconds = None
    if kind == "auth":
        # 401/403 有两种来源，**不能只看状态码就把整个渠道停掉**：
        #   账号级（Key 失效/权限被撤）→ 所有模型都会失败 → 停渠道是对的；
        #   模型级（该模型要付费订阅/无权限/地区限制）→ 渠道其他模型还好好的
        #     → 只标这一个 (模型,渠道)，别让一个模型拖垮整渠道（HF 有 139 个模型）。
        # 判据用「这个渠道最近有没有成功过」：刚成功过 → Key 没问题，是模型级。
        # 401 例外：连鉴权都没过，几乎必然是 Key 级。
        account_level = status == 401 or not gateway.channel_recently_ok(cid)
        if account_level:
            cs = gateway.get_cs(cid)
            cs.valid = False
            cs.error = f"运行时检测: Key 无效或无权限 (HTTP {status})"
            logger.warning("渠道[%s] Key 鉴权失败 (HTTP %s)，暂停使用，等待下次健康检查", name, status)
        else:
            logger.warning("渠道[%s] 模型[%s] 被拒 (HTTP %s)：渠道其他模型刚成功过 → 按模型级权限问题处理，"
                           "不停用渠道", name, upstream_model, status)
        # 按 (模型, 渠道) 记录，不再把同名模型在其他渠道的状态一起标红
        gateway.mark_channel_down(
            upstream_model, cid,
            f"Key 无效: HTTP {status}" if account_level else f"该模型无权限/需付费: HTTP {status}")
    elif kind == "rate_limit":
        # 429 分三档：余额不足（要充值）/ 免费额度用尽（等明天）/ 每分钟限流（等一会）。
        # 判别必须结合渠道类型——魔搭的「每日免费额度用完」正文就是 insufficient balance，
        # 光看字面会误判成欠费；平台余额接口查得到时以它为准（_quota_exhausted）。
        label, secs, text429 = gateway.classify_429(raw, ch_type, retry_after)
        # `_quota_exhausted` 只能**补强**「正文没写清楚」的 429，不能覆盖正文的明确分类：
        # 正文已判 free_daily（每日额度、次日自动回来）时，余额接口说 0 也拦不住它 —— 两者
        # 说的不是同一件事。2026-09-21 实测：OpenRouter 账户余额 0（免费层，从未充值）、
        # 免费模型 429 正文是「free-models-per-day. Add 10 credits…」，被这条判成
        # 「余额不足」→ 永久 down，而它其实每天都会自己恢复。
        if label == "paid_balance" or (_quota_exhausted(cid) and label not in ("minute", "free_daily")):
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
                            f"HTTP 429 余额/额度不足(需充值): {raw[:200]}", upstream_model=upstream_model,
                            superseded=superseded)
            logger.warning("渠道[%s] 模型[%s] 账户余额/额度不足，按硬不可用处理（需充值或换 key）", name, upstream_model)
            errors.append(f"{name}: 账户余额不足，需充值{note}")
            return
        if label == "free_daily":
            # 额度 429 先按**模型级**处理：只冷这个 (模型,渠道)（下面 mark_result 的
            # cooldown_seconds=到明天 已经做了这件事）。只有短窗口内同渠道 ≥4 个不同模型
            # 一起撞额度 429，才升级成账号级、整渠道停到明天。详见 note_channel_quota_429。
            # 旧行为是「一见就整渠道停到明天」：魔搭大模型日额度只有 100，先耗完的那个撞一次
            # 就把整条渠道封十几个小时 —— 近三周 62 次魔搭 429 里至少 4 天是这种误伤。
            account_level, n_models = gateway.note_channel_quota_429(cid, upstream_model)
            if account_level:
                logger.warning("渠道[%s] 30 分钟内已有 %d 个不同模型撞额度 429 → 判定账号级额度耗尽，"
                               "整渠道冷却到明天（本次模型=%s）", name, n_models, upstream_model)
                msg = (f"免费额度用完 (HTTP 429)：该渠道 30 分钟内 {n_models} 个模型均受限，"
                       "判账号级，整渠道冷却到明天")
            else:
                logger.warning("渠道[%s] 模型[%s] 免费额度用完 (free_daily 429)，按**模型级**处理："
                               "只冷这个模型到明天，渠道其余模型照常路由（窗口内累计 %d 个模型）",
                               name, upstream_model, n_models)
                msg = "免费额度用完 (HTTP 429)：该模型额度已用完，冷却到明天（渠道其它模型不受影响）"
            gateway.mark_model_status(requested_model, False, msg, name, state="limited")
            gateway.mark_result(upstream_model, cid, False, kind="rate_limit",
                                cooldown_seconds=secs)
            store.log_usage(cid, name, requested_model, 0, 0, latency, False,
                            f"HTTP 429 免费额度用完: {raw[:200]}", upstream_model=upstream_model,
                            superseded=superseded)
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
                    f"HTTP {status}{note}: {raw[:200]}", upstream_model=upstream_model,
                    superseded=superseded)
    errors.append(f"{name}: HTTP {status}"
                  + (f" (Retry-After {retry_after}s)" if retry_after else "") + note)


def _fail(cid, name, upstream_model, requested_model, t0, errors, msg, exc=None,
          superseded=False):
    """请求阶段就失败（连不上/本地网络）。

    本地 DNS/代理问题（`local_net`）**不算模型的账**：不改模型状态、不进稳定分、不冷却——
    否则你自己网络一抖，所有渠道的模型都被打成「受限」。其余失败（超时/断连）才记。
    `superseded=True` 表示后面还会换渠道重试（只影响 usage 记账口径，不影响上面的健康信号）。"""
    kind = _conn_kind(exc) if exc is not None else "connect"
    if kind != "local_net":
        gateway.mark_result(upstream_model, cid, False, None, kind=kind, source="real")
        gateway.mark_model_status(requested_model, True, "连接失败（可能暂时不可达）", name, state="limited")
    else:
        logger.warning("渠道[%s] 本地网络/DNS 问题，不改模型状态: %s", name, msg[:160])
    store.log_usage(cid, name, requested_model, 0, 0, int((time.time() - t0) * 1000), False, msg,
                    upstream_model=upstream_model, superseded=superseded)
    logger.warning("渠道[%s] 模型[%s] 连接失败 (%s): %s", name, upstream_model, kind, msg[:200])
    errors.append(f"{name}: {msg[:120]}")


def _sse_line_marks(line: bytes):
    """解析一行 SSE，返回 `(usage 字典或 None, 是否结束标记, 本行内容字符数)`。

    「结束标记」= 上游已经明确说「这条回答完了」：`finish_reason` 非空 / 带 usage 的收尾块 /
    `[DONE]`。**这才是流式请求的成败依据**（见 `_stream_ok`），不要依赖 TCP 流自然关闭。

    第三个返回值是「模型到底吐了东西没有」的证据（字符数）——客户端中途断开时，
    靠它区分「模型正常输出、客户端先走」（算成功）和「一句话都没出来就断了」（已中断）。
    推理模型的 `reasoning_content` 也算输出：它确实在干活。
    """
    line = line.strip()
    if not line.startswith(b"data:"):
        return None, False, 0
    payload = line[5:].strip()
    if payload in (b"[DONE]", b"[done]"):
        return None, True, 0
    try:
        j = json.loads(payload)
    except Exception:
        return None, False, 0
    if not isinstance(j, dict):
        return None, False, 0
    u = j.get("usage")
    if isinstance(u, dict) and u:
        return u, True, 0          # usage 收尾块：内容已在它之前全部送出
    chars, is_end = 0, False
    for ch in (j.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        # 流式在 delta，个别平台用 message / text
        for box in (ch.get("delta"), ch.get("message")):
            if isinstance(box, dict):
                for k in ("content", "reasoning_content", "reasoning"):
                    v = box.get(k)
                    if isinstance(v, str):
                        chars += len(v)
        v = ch.get("text")
        if isinstance(v, str):
            chars += len(v)
        if ch.get("finish_reason"):
            is_end = True
    return None, is_end, chars


def _stream_ok(completed: bool, saw_end: bool, aborted: bool, out_chars: int = 0) -> tuple:
    """流式请求的成败判定 → `(ok, cancelled)`。

    成功 = 拿到了结束标记（`finish_reason` / usage 收尾块 / `[DONE]`），**不是**「上游把 TCP
    流关干净了」。两者不等价：客户端读完完整答案立刻关连接是正常行为，而有些平台（魔搭最典型）
    发完数据后拖一会儿才关连接，网关就卡在 `aiter_bytes` 等最后几个字节时被取消 —— 旧版据此
    把大量正常完成的请求记成「客户端断开」的失败。

    **客户端中途断开（`aborted`）时再看一眼「模型有没有在正常输出」**（`out_chars`）：
    - 输出过内容 → **模型这一跳是正常的**，是客户端先走（用户点停止 / 客户端自己不等了）
      → 记成功；`cancelled=1` 只作为「断在结束标记之前」的事实标注；
    - 一个字都没输出就断了 → 既不是成功也不是失败，记 `success=0 + cancelled=1`（「已中断」）。
      （上游真卡住/断流走的是异常分支 → `stream_break` 失败，不在此列。）
    `cancelled=0` 的情况里还有一种是「答案已经完整送到、客户端顺手关连接」——那不算中断。
    口径来源：用户 2026-09-17「模型如果正常输出 token，不应该是成功吗」。
    """
    finished = bool(completed or saw_end)          # 结束标记到手 = 这一跳完整跑完
    ok = bool(finished or (aborted and out_chars > 0))
    return ok, bool(aborted and not finished)


async def _stream_gen(resp, cid, name, model, t0, upstream_model="", sticky_scope=None):
    """流式转发，同时从 SSE 里抽 usage 记账，并记录**首字节时间(TTFT)**与**是否正常收尾**。

    成败看语义结束信号（见 `_stream_ok`）：收到 `finish_reason`/usage/`[DONE]` 就是成功。
    客户端中途断开时**再看模型有没有在正常输出**（`out_chars`）：输出过就记成功（客户端先走），
    一个字都没出才是「已中断」。上游中途断流/超时走异常分支，记 `kind="stream_break"` 的失败。

    `sticky_scope=(策略, 会话键, 标签)` 时，本次成功产出会给这个会话**续上会话粘性**
    （下次 auto 请求优先还用这个模型）。⚠️ 只在这里判成功才续：上游返回 200 只是
    「收下了请求」，流中断 / 一个字没吐都不算成功产出，不能拿它粘住一个会话。

    ⚠️ **同一个 chunk 必须「先解析再 yield」**（2026-09-17 修）：客户端往往收到带
    `finish_reason` 的那一块就断开，顺序反了的话解析那一步永远轮不到，
    账上会把一次完整应答记成「已中断」。改这里前先看
    `tests/test_core.py::test_stream_gen_accounts_end_marker_before_client_closes`。"""
    usage = {}
    buf = b""
    ttft = None
    out_chars = 0            # 模型实际吐出来的内容字符数（客户端断开时用它证明「模型没问题」）
    completed = False
    saw_end = False
    aborted = False
    err = None
    try:
        async for chunk in resp.aiter_bytes():
            if ttft is None and chunk:
                ttft = max(1, int((time.time() - t0) * 1000))
            buf += chunk
            # **先解析再转发**（2026-09-17 修）：客户端常常「收到带 finish_reason 的那一块
            # 就断开」（Hermes 实测如此）。若先把 chunk 交给客户端、再解析，客户端一断，
            # 这个生成器就被关掉，解析那一步永远轮不到 —— 已经**送给客户端的结束标记**
            # 在账上等于不存在，于是好好的一次应答被记成「已中断（无结束标记）」。
            # 回归用例：test_stream_gen_accounts_end_marker_before_client_closes
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                u, is_end, chars = _sse_line_marks(line)
                if u is not None:
                    usage = u
                out_chars += chars
                if is_end:
                    saw_end = True
            yield chunk
        completed = True
    except (GeneratorExit, asyncio.CancelledError):
        aborted = True       # 消费方断开：上游没错；成不成看模型有没有正常吐内容
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.warning("渠道[%s] 模型[%s] 流式中断: %s", name, upstream_model, err[:180])
        raise
    finally:
        if ttft is not None and upstream_model:
            gateway.mark_ttft(upstream_model, cid, ttft)
        try:
            await resp.aclose()
        except BaseException:      # 取消路径下 aclose 可能再抛，不能让它吞掉下面的记账
            pass
        ok, cancelled = _stream_ok(completed, saw_end, aborted, out_chars)
        latency = int((time.time() - t0) * 1000)
        if upstream_model and (ok or not aborted):
            gateway.mark_result(upstream_model, cid, ok, latency if ok else None,
                                kind=None if ok else "stream_break", source="real")
        if sticky_scope and ok and upstream_model:
            # 成功产出 → 把这个会话粘在这个模型上（`ok` 已包含「客户端断开了但模型正常输出」）
            gateway.mark_sticky(sticky_scope[0], sticky_scope[1], upstream_model, cid,
                                label=sticky_scope[2])
        # 中断也留证据：模型吐没吐东西，直接写进备注（tokens 列拿不到 usage 只能是 0，
        # 光看 0 会误以为模型没干活 —— 2026-09-17 用户就是这么质疑的）。
        # 注意只写「断在结束标记之前」的情况：答完了客户端顺手关连接不算中断，不该留备注。
        # out_chars 单独入库（不只塞在备注文案里）：界面要在 Tokens 那一列照实显示
        # 「N 字」，从备注里正则抠数字太脆（文案一改就废）。
        if cancelled:
            note = (f"客户端中断（模型已正常输出 {out_chars} 字符后断开）" if ok
                    else "客户端中断（模型未输出内容）")
        else:
            note = err[:200] if err else None
        store.log_usage(cid, name, model, usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                        latency, ok, error=note,
                        upstream_model=upstream_model, cancelled=cancelled,
                        out_chars=out_chars)


@app.post("/v1/responses")
async def responses_api(request: Request):
    """OpenAI Responses API 兼容层（Codex CLI、新版 OpenAI SDK 用的是这个端点）。

    只做协议翻译：请求译成 chat/completions 的 body 后交给 `_chat_v1`，
    回包（JSON 或 SSE）再由 `app.responses` 译回 Responses 的形态。所以渠道路由、
    冷却分类、失败换路、会话粘性、用量记账与 chat 入口**完全同一套** —— 这里不碰
    任何渠道逻辑，也不自己发上游请求。

    ⚠️ 无状态：不实现 `store` 的服务端会话记忆，`previous_response_id` 会被忽略并记日志
    （客户端每轮把完整历史放进 `input` 就不受影响，Responses 协议本来也是这么用的）。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    cfg = cfgmod.load_config()
    try:
        chat_body, ctx = responses_tr.to_chat(body, cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = await _chat_v1(chat_body, request, cfg)
    return await responses_tr.convert(resp, ctx)


@app.post("/v1/messages")
async def messages_api(request: Request):
    """Anthropic Messages API 兼容层（Claude Code、Anthropic SDK、只认这个协议的 agent）。

    和 `/v1/responses` 同一个套路：只做协议翻译 —— 请求译成 chat/completions 的 body 后交给
    `_chat_v1`，回包（JSON 或 SSE）再由 `app.dialects.messages` 译回 Anthropic 的形状。
    所以渠道路由、冷却分类、失败换路、会话粘性、用量记账与 chat 入口**完全同一套**。

    两点与 chat 入口不同：
    - `max_tokens` **必填**（Anthropic 协议如此），缺了直接 400
    - 流式事件是**有状态**的（message_start → content_block_* → message_delta → message_stop），
      顺序错了官方 SDK 会把事件静默丢掉，见 `app/dialects/messages.py` 的说明
    """
    try:
        body = await request.json()
    except Exception:
        return dialects.messages.error_response(400, "请求体不是合法 JSON")
    cfg = cfgmod.load_config()
    try:
        chat_body, ctx = dialects.messages.to_chat(body, cfg)
    except ValueError as e:
        return dialects.messages.error_response(400, str(e))
    return await dialects.messages.convert(await _chat_v1(chat_body, request, cfg), ctx)


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
            # 一个上游请求都没发 → 就不该动状态。旧版在这里写一句笼统的
            # "没有任何可用渠道（渠道未配置或健康检查未通过）"，后果是：
            #   ① 把**原来更具体的原因**（如 403 / 余额不足 / 额度用完）冲掉；
            #   ② 没传 state → mark_model_status 默认按 down 记，一个 `limited`
            #      （等得到、会自愈）的模型被降级成 `down`（等不到、永不自愈）。
            # 现在只记日志、原样报回已有的原因，状态由真实探测结果说话。
            prev = gateway.model_status.get(model) or {}
            prev_reason = (prev.get("reason") or "").strip()
            prev_state = prev.get("state") or ""
            logger.info("模型[%s] 手动测试：没有任何候选渠道（原状态 %s / %s），不改状态",
                        model, prev_state or "无", prev_reason or "无记录")
            detail = (f"（现有记录：{prev_reason[:120]}）"
                      if prev_reason else "（渠道未配置或健康检查未通过）")
            raise HTTPException(404, f"该模型当前没有任何可用渠道{detail}")

    last_err = ""
    result = None
    # 走过的候选：模型级测试要试完所有渠道才定论（2026-09-21 口径），界面得能看到都试了哪几条。
    # 收集点在循环开头 —— 只有 `result` 有值却**又进了下一轮**，才说明上一候选是「失败但未定论」。
    tried = []
    for cand in candidates:
        if result is not None:
            tried.append(result)
            result = None
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
                bad = _probe_body_ok(r)
                if bad:
                    # 200 但内容不可信（错误对象/结构不对）→ 别标绿（见 _probe_body_ok）
                    last_err = f"HTTP 200 但内容不可信：{bad}"
                    gateway.mark_result(upstream_model, ch["id"], False, kind="bad_json")
                    if cid:
                        gateway.mark_model_status(model, True, last_err, ch.get("name"),
                                                  state="limited")
                        return {"available": False, "channel": ch.get("name"), "error": last_err}
                    result = {"available": False, "channel": ch.get("name"), "error": last_err}
                    break
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
                # 余额/额度不足（付费渠道）→ 硬不可用，等也不会恢复。
                # 正文已判 free_daily 时不再被余额接口覆盖（同 `_classify` 里的说明）：
                # OpenRouter 免费层余额恒为 0，而免费模型的日额度 429 **次日自愈**。
                if label429 == "paid_balance" or (
                        _quota_exhausted(ch["id"]) and label429 not in ("minute", "free_daily")):
                    gateway.mark_channel_down(upstream_model, ch["id"],
                                              "账户余额不足，需充值 (HTTP 429)")
                    gateway.mark_model_status(model, False, "账户余额不足，需充值",
                                              ch.get("name"), state="down")
                    gateway.mark_result(upstream_model, ch["id"], False, kind="balance")
                    out = {"available": False, "skipped": True, "channel": ch.get("name"),
                           "error": "余额不足 (HTTP 429)，按不可用处理"}
                    if cid:
                        # 用户点名了渠道（渠道级测试）→ 这一条就是结论，立刻返回
                        return out
                    # 模型级测试：余额不足只说明**这一条渠道**不能用，同模型的别的渠道可能好使
                    # → 继续试下一候选（2026-09-21 用户口径：所有渠道都失败才报不可用）
                    result = out
                    break
                # 免费额度用完：先按**模型级**（只冷这一个模型到明天）；短窗口内同渠道
                # ≥4 个不同模型一起撞才升级成账号级、整渠道停到明天（同 _classify）
                if label429 == "free_daily":
                    account_level, n_models = gateway.note_channel_quota_429(
                        ch["id"], upstream_model)
                    if account_level:
                        msg = (f"免费额度用完 (HTTP 429)：该渠道 30 分钟内 {n_models} 个模型均受限，"
                               "判账号级，整渠道冷却到明天")
                    else:
                        msg = ("免费额度用完 (HTTP 429)：该模型额度已用完，冷却到明天"
                               "（渠道其它模型不受影响）")
                    gateway.mark_model_status(model, False, msg,
                                              ch.get("name"), state="limited")
                    gateway.mark_result(upstream_model, ch["id"], False, kind="rate_limit",
                                        cooldown_seconds=secs429)
                    out = {"available": False, "channel": ch.get("name"),
                           "error": msg, "status": 429}
                    if account_level or cid:
                        out["skipped"] = True
                        return out
                    # 只是这一个模型在这条渠道上额度用完了 → 该模型在别的渠道可能还好使，
                    # 继续试下一候选（旧版在这里直接 return，会把「别处可用」误报成不可用）
                    result = out
                    break
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
            if cid:
                # 渠道级测试（用户点名了渠道）→ 这一条就是结论，立刻返回
                return out
            # 模型级测试：**任何失败都继续试下一候选**，走完所有渠道才定论
            # （2026-09-21 用户口径：一个渠道失败就该测另一个，所有渠道都失败才算模型不可用）。
            # 旧写法是 `if hard or cid` —— 一条渠道硬失败就立刻返回，于是
            # `z-ai/glm-5.3-flash` 点一次测试只拿到 OpenRouter 的 402，而它在 NIM 上可用；
            # 界面报「不可用」、agent 调用却是好的，两边口径打架。
            # 死渠道的教训不丢：`mark_channel_down` 已在上面写好，agent 调用与自动路由
            # 都会跳过它 —— 「这一条不用」和「这个模型不用」从此分开记。
            result = out
            break
    if result is None:
        result = {"available": False, "error": last_err or "所有渠道均失败"}
    # 走完所有候选仍失败 → 把每次尝试都带给界面。只报最后一条会让人以为「只试了一条」，
    # 而口径是「所有渠道都失败才算这个模型不可用」，界面必须撑得住这句话。
    if tried:
        result["attempts"] = [
            {"channel": t.get("channel"), "error": (t.get("error") or "")[:200],
             "status": t.get("status")} for t in (tried + [result])]
    return result


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
            # 额度型 429 的「爆发度」：最近 30 分钟撞额度 429 的不同模型数。
            # ≥4 就会被判账号级、整渠道停到明天（见 gateway.note_channel_quota_429）
            "quota429_models": gateway.quota_429_models(ch["id"]),
            # 平台在响应头里自报的「账号级当天剩余额度」（目前只有魔搭给：
            # modelscope-ratelimit-requests-remaining，跨该 Key 所有模型共享）
            "quota_day": gateway.user_quota.get(ch["id"]) or None,
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
            "pinned": cfg.get("pinned", []),
            # 视觉专属收藏：界面「视觉」视图里那颗 ★ 读写的是这一份（与主收藏互不影响）
            "pinned_vision": cfg.get("pinned_vision", []),
            # 官方限流头解析结果（排障两块）：
            #   ratelimit      = 平台自报的「模型级」剩余额度，键 "上游模型|渠道id"
            #   ratelimit_hits = 各口径被读到的累计次数。`modelscope` 长期为 0
            #                    = 魔搭那 4 个头压根没到（流式不带？）→ 别怀疑解析代码
            "ratelimit": {f"{k[0]}|{k[1]}": v for k, v in gateway.ratelimit.items()},
            "ratelimit_hits": dict(gateway.ratelimit_hits)}


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
    """收藏/取消收藏模型（持久化在 config.json，跨窗口/重启不丢）

    `list` 选收藏表（2026-09-20 起分两套）：
      - `"main"`（默认）→ `pinned`：主收藏，管 均衡/智能/稳定/速度 四个视图与对应 auto 策略；
      - `"vision"`     → `pinned_vision`：视觉专属收藏，只管「视觉」视图与 `auto-vision`。
    """
    b = await req.json()
    model = (b.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    scope = (b.get("list") or "main").strip().lower()
    key = "pinned_vision" if scope in ("vision", "pinned_vision") else "pinned"
    cfg = cfgmod.load_config()
    pinned = cfg.setdefault(key, [])
    if b.get("pinned"):
        if model not in pinned:
            pinned.append(model)
            cfgmod.save_config(cfg)
    else:
        if model in pinned:
            pinned.remove(model)
            cfgmod.save_config(cfg)
    return {"ok": True, "list": key}


@app.post("/api/model-tier")
async def set_model_tier(req: Request):
    """手动指定模型档位（界面点档位 chip 调的），落在 config.json 的 `model_tier_exact`。

    - `tier` = 1/2/3 指定；`0`（或 null）= 清除，交回自动判定；
    - 这是**点名表**，与手改的 `model_tiers`（正则批量规则）分开存：模型 id 里的 `.` 是正则通配符、
      带 `+`/`(` 还会直接让规则失效，点名不该走正则（见 `capability.tier_overrides`）；
    - 立即生效，**不用重启**：配置每次请求现读，前端拿到新 overview 就重排了。
    """
    b = await req.json()
    model = (b.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    raw = b.get("tier")
    try:
        tier = 0 if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "tier 必须是 1 / 2 / 3，或 0 表示清除")
    if tier not in (0, 1, 2, 3):
        raise HTTPException(400, "tier 必须是 1 / 2 / 3，或 0 表示清除")
    cfg = cfgmod.load_config()
    exact = cfg.setdefault("model_tier_exact", {})
    if tier:
        exact[model] = tier
        # 同一模型在正则表里也命中时，点名仍然赢（tier_overrides 里精确项排在前面）
        logger.info("手动档位: %s → %d", model, tier)
    else:
        exact.pop(model, None)
        logger.info("手动档位清除: %s", model)
    cfgmod.save_config(cfg)
    return {"ok": True, "tier": tier or None, "manual": bool(tier),
            "tier_auto": capability.tier_of(model)}   # 自动判定值，给界面提示「自动判定：X」


@app.get("/api/sticky")
async def sticky_list():
    """当前的**会话粘性**表（只读）：哪个会话现在粘在哪个模型上、粘了多久。

    `auto-*` 请求会「沿用本会话上一次成功产出的模型」，避免同一个对话里来回换模型
    （见 `gateway.prefer_sticky`）。这里只给排障用：`idle_s` 超过 `ttl_s` 就自动失效。"""
    return {"sticky": gateway.sticky_view(), "ttl_s": int(gateway.STICKY_TTL)}


@app.delete("/api/sticky")
async def sticky_clear(strategy: str = ""):
    """清掉会话粘性（`strategy` 不传 = 全清）。

    用途：想立刻让某个视图/会话重新按策略选模型（不然要等它失败或闲置 2 小时）。"""
    n = gateway.clear_sticky(strategy.strip())
    logger.info("清会话粘性: %s（%d 条）", strategy or "全部", n)
    return {"ok": True, "cleared": n}


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
    # 前端是单文件、随改随生效（这里现读磁盘）。**必须带 no-store**：不给 Cache-Control 时
    # WebView2 会按「启发式新鲜度」继续用缓存里的旧页面，界面按 F5 也刷不出来
    # （2026-09-17 用户报「改了前端必须重启服务才看得到」就是这个）。加上之后每次刷新都重新取盘。
    return FileResponse(FRONTEND, headers={"Cache-Control": "no-store, must-revalidate"})
