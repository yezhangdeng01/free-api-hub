"""视觉能力复测：真图 + 必须答出颜色，用来核对 `capability._VISION_VERIFIED` 与平台数据。

为什么要「必须答出颜色」：**200 不等于支持视觉**——有的上游会静默丢掉图片照样回 200
（例：`openai/gpt-oss-20b`），只发一张纯红方块、要求它只回一个词，才能分辨真伪。

三个必须在报告里区分开的结论：
  YES / NO        = 可信的实测结论（NO = 上游明确拒绝图片输入）
  UNCERTAIN-200   = 200 但答不出 red（可能静默忽略图片）→ **不要**据此改标记
  GEO             = 本机出口被地域封锁（User location is not supported）→ **不是模型的结论**，
                    改标记前先修网络/代理；批量请求里往往时通时不通，别用这一批结果下判断

命中就改 `app/capability.py` 的 `_VISION_VERIFIED`（归一化名 → True/False，优先级最高），
或按家族补一条 `_VISION` / `_VISION_NEG` 规则——改完跑 `pytest tests/ -q`。

用法（仓库根目录，需托盘在跑）：
  .venv\\Scripts\\python.exe scripts\\verify_vision_models.py            # 只测 status=ok 的模型
  .venv\\Scripts\\python.exe scripts\\verify_vision_models.py --all      # 连 limited 也测
结果落盘：%LOCALAPPDATA%\\vision_probe_strict.json
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.request
import zlib

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import gateway, vault  # noqa: E402

PROMPT = "Look at the image. What color is it? Answer with exactly one word."
PORT = 8787


def png(w=48, h=48, rgb=(230, 30, 30)):
    """手搓一张纯色 PNG（不依赖 Pillow）：纯红 48×48。"""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) % 4294967296)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def verdict(status, text):
    """把一次探测结果归类（GEO/UNCLEAR 明确标出来，避免被当成模型结论）。"""
    low = text.lower().replace("'", '"')
    if gateway.is_geo_block(text):
        return "GEO"
    if status == 200:
        if '"red"' in low or "red" in low:
            return "YES"
        return "UNCERTAIN-200"
    if status in (400, 404, 415, 422):
        return "NO"
    return "UNCLEAR-%d" % status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连 limited/down 的模型一起测")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    img = base64.b64encode(png()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{a.port}/api/overview",
                                 headers={"Host": f"127.0.0.1:{a.port}"})
    d = json.load(urllib.request.urlopen(req, timeout=30))

    targets = {}
    for m in d["models"]:
        if not a.all and m.get("status") != "ok":
            continue
        for c in (m.get("channels") or []):
            if c.get("available"):
                targets[m["id"]] = (c.get("channel_name"), m.get("vision"))
                break

    out = {}
    with httpx.Client(timeout=90) as cl:
        for mid, (cname, cur_flag) in sorted(targets.items()):
            ch = next((c for c in cfg["channels"] if c.get("name") == cname), None)
            if not ch:
                continue
            body = {"model": mid, "max_tokens": 24, "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]}
            try:
                r = cl.post(ch["base_url"].rstrip("/") + "/chat/completions", json=body,
                            headers={"Authorization": "Bearer " + vault.decrypt(ch["api_key"])})
                txt = r.text[:400].replace("\n", " ")
                v = verdict(r.status_code, r.text[:400])
            except Exception as e:
                v, txt = "EXC", "%s: %s" % (type(e).__name__, str(e)[:120])
            flip = ""
            if v in ("YES", "NO") and ((v == "YES") != bool(cur_flag)):
                flip = "  <<< 与当前标记 vision=%s 不一致，需修正" % cur_flag
            elif v == "GEO":
                flip = "  <<< 地域封锁，别据此改标记"
            print("  %-14s %-46s %s%s" % (v, mid[:46], txt[:70], flip))
            out[mid] = {"channel": cname, "verdict": v, "current_flag": cur_flag, "note": txt[:200]}
            time.sleep(0.4)

    path = os.path.join(os.environ.get("LOCALAPPDATA", ROOT), "vision_probe_strict.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cnt = {}
    for v in out.values():
        cnt[v["verdict"]] = cnt.get(v["verdict"], 0) + 1
    print("\n汇总:", cnt)
    print("YES 且当前标记 False（漏标，该补）:",
          [k for k, v in out.items() if v["verdict"] == "YES" and not v["current_flag"]] or "无")
    print("NO 且当前标记 True（误标，该改）:",
          [k for k, v in out.items() if v["verdict"] == "NO" and v["current_flag"]] or "无")
    print("GEO（本机网络问题，忽略本批结论）:",
          [k for k, v in out.items() if v["verdict"] == "GEO"] or "无")
    print("\n已存:", path)


if __name__ == "__main__":
    main()
