"""桌面入口：本地服务 + 原生窗口 + 系统托盘 + 开机自启

- 双击启动（无控制台）：API Hub.vbs
- 关闭窗口 → 最小化到托盘常驻（服务不中断）
- 托盘菜单：显示窗口 / 隐藏窗口 / 开机自启 / 退出
"""
import os
import socket
import subprocess
import sys
import threading
import time

import uvicorn

from app import config as cfgmod
from app.main import app

_window_ref = {"w": None}
_lock_sock = None  # 单实例锁 socket（进程退出自动释放）


def _startup_cmd() -> str:
    """注册表开机自启用命令（pythonw 无控制台 + --silent 不弹主窗口，仅托盘常驻）"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-4] + "w.exe"
    return f'"{exe}" "{os.path.abspath(__file__)}" --silent'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, "APIHub")
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        with key:
            if enable:
                winreg.SetValueEx(key, "APIHub", 0, winreg.REG_SZ, _startup_cmd())
            else:
                try:
                    winreg.DeleteValue(key, "APIHub")
                except OSError:
                    pass
        return True
    except OSError as e:
        print(f"[API Hub] 开机自启设置失败: {e}")
        return False


def _show_window(initial_hidden: bool = False):
    import webview
    w = _window_ref["w"]
    # 窗口已存在且仍存活 → 只显示 + 聚焦，绝不重复创建。
    # （旧判断用 w.destroyed 属性，但 pywebview 的 Window 类根本没有该属性，
    #   getattr(w, "destroyed", True) 恒为 True，导致每点一次托盘就多一个窗口）
    if w is not None and w in webview.windows:
        w.show()
        _bring_to_front(w)
        return w
    w = webview.create_window(
        "API Hub · 大模型聚合网关", f"http://127.0.0.1:{cfgmod.load_config()['port']}",
        width=1320, height=900, min_size=(960, 640), hidden=initial_hidden)
    _window_ref["w"] = w

    def _on_closing():
        # 拦截「关闭窗口」→ 隐藏到托盘，服务与托盘保持常驻
        try:
            w.hide()
        except Exception:
            pass
        return False

    try:
        w.events.closing += _on_closing
    except Exception:
        pass  # 旧版 pywebview 不支持取消关闭 → 走下方 keep-alive 兜底
    return w


def window_is_destroyed():
    import webview
    w = _window_ref["w"]
    return w is None or w not in webview.windows


def _bring_to_front(w):
    """把窗口可靠地置前并聚焦。

    Windows 前台锁会拦截后台线程（如 pystray 回调）的 Activate()，导致
    任务栏图标抖动但窗口不上浮。用 Win32 AttachThreadInput 把当前线程挂接到
    前台窗口所属线程，即可让 SetForegroundWindow 生效（微软允许的破解方式）。
    """
    import ctypes
    from ctypes import wintypes
    try:
        hwnd = int(w.native.Handle)
    except Exception:
        try:
            w.show()
        except Exception:
            pass
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    # 1) 最小化则先恢复
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    else:
        user32.ShowWindow(hwnd, 5)   # SW_SHOW

    # 2) 挂接到前台线程破解前台锁，再设前台
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_pid = wintypes.DWORD()
    fg_tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
    if fg_tid and fg_tid != cur_tid:
        user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(cur_tid, fg_tid, False)
    else:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)


def _launch_cmd() -> list[str]:
    """本次启动进程对应的命令行（用于重启/唤起）。exe 打包态直接 exe 路径。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-4] + "w.exe"
    return [exe, os.path.abspath(__file__)]


def _restart():
    """重启服务：落盘当前状态 → 起新进程 → 立即退出本进程。

    新进程由单实例锁接管（本进程退出后端口才释放，新进程最多重试等端口）。
    用 pythonw（无控制台）拉起可完全脱离托盘退出后的控制台句柄。"""
    try:
        from app import gateway
        gateway.save_runtime_state()  # 最后一批扫描/冷却状态别丢
    except Exception:
        pass
    try:
        subprocess.Popen(_launch_cmd(), close_fds=True)
    except Exception as e:
        print(f"[API Hub] 重启失败: {e}")
        _launch_log(f"重启失败: {e}")
        return
    os._exit(0)


def _tray_loop(port: int):
    try:
        import pystray
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (64, 64), "#17150f")
        d = ImageDraw.Draw(img)
        d.ellipse([14, 14, 50, 50], outline="#d0a75c", width=5)
        d.polygon([(27, 38), (37, 32), (37, 44)], fill="#d0a75c")

        def on_show(icon, item):
            _show_window()

        def on_settings(icon, item):
            # 打开 api-hub 目录（config.json / data 数据文件所在地），
            # 便于手动改 config.json（端口、model_tiers 能力档位覆盖等），
            # 或查看 data/api-hub.log / launch.log。只做前端做不到的事。
            try:
                os.startfile(cfgmod.ROOT)  # type: ignore[attr-defined]  # Windows 脚本
            except Exception as e:
                _launch_log(f"打开配置目录失败: {e}")

        def on_restart(icon, item):
            _restart()

        def on_hide(icon, item):
            if _window_ref["w"] is not None and not window_is_destroyed():
                _window_ref["w"].hide()

        def on_autostart(icon, item):
            set_autostart(not autostart_enabled())
            icon.update_menu()

        def on_quit(icon, item):
            icon.stop()
            try:
                from app import gateway
                # os._exit(0) 会跳过 FastAPI lifespan 的停机落盘，这里手动把
                # 冷却/渠道级硬失败/429 预判等状态写盘，避免最后一批扫描结果丢失
                gateway.save_runtime_state()
            except Exception:
                pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("打开配置文件夹", on_settings),
            pystray.MenuItem("重启服务", on_restart),
            pystray.MenuItem("隐藏窗口", on_hide),
            pystray.MenuItem("开机自启", on_autostart,
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )
        pystray.Icon("api-hub", img, "API Hub", menu).run()
    except Exception as e:
        print(f"[API Hub] 托盘不可用（不影响使用）: {e}")


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _acquire_single_instance_lock(port: int) -> bool:
    """单实例锁：绑定一个固定本地端口，失败说明已有实例在跑。

    避免多实例各自拉起 uvicorn / 后台循环，互相覆盖 data/runtime_state.json 等状态文件。"""
    global _lock_sock
    lock_port = port + 1000  # 与网关端口错开，避免冲突
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", lock_port))
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        s.close()
        return False


def _launch_log(msg: str):
    """启动失败时写 data/launch.log（pythonw 无控制台，必须有落盘错误信息）"""
    try:
        p = os.path.join(cfgmod.ROOT, "data", "launch.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _serve(port: int):
    # log_config=None：跳过 uvicorn 内部日志 dictConfig。
    # pythonw（无控制台，sys.stderr 为 None）下它会在配置 formatter 时抛
    # ValueError: Unable to configure formatter 'default'，直接禁用最稳。
    #
    # 端口占用重试：Windows 下 asyncio.create_server 默认 reuse_address=False，
    # 重启时旧进程被杀但 webview 前端还保持 8787 活跃连接，进程退出后这些
    # TCP 连接进入 TIME_WAIT（系统层面 15~60s 才真正释放），新进程立即
    # bind 撞 [Errno 10048]，uvicorn sys.exit(3)，服务挂 → 前端 404。
    # 这里重试（延迟递增）给 TIME_WAIT 释放窗口，最长约 30s。
    import time as _time
    for attempt in range(8):
        try:
            uvicorn.run(app, host="127.0.0.1", port=port,
                        log_level="warning", log_config=None)
            return  # 正常退出（被外部关闭）
        except SystemExit as e:
            # uvicorn 启动失败 sys.exit(STARTUP_FAILURE=3)。
            # 端口 TIME_WAIT 重试：第一次直接试，之后延迟递增
            if attempt < 7:
                delay = 2.0 * (attempt + 1)
                _launch_log(f"端口 {port} 占用（TIME_WAIT），{delay:.0f}s 后重试（第 {attempt + 2}/8 次）")
                _time.sleep(delay)
                continue
            _launch_log(f"uvicorn 端口 {port} 重试 8 次仍失败，退出: {e}")
            return
        except BaseException as e:
            _launch_log(f"uvicorn 异常退出: {type(e).__name__}: {e}")
            return


def main():
    # --silent：开机自启静默模式——窗口以 hidden 创建（不弹窗），仅托盘常驻；
    # 需要界面时右键托盘「显示窗口」。
    silent = "--silent" in sys.argv
    cfg = cfgmod.load_config()
    port = cfg["port"]
    if not _acquire_single_instance_lock(port):
        _launch_log("检测到已有 API Hub 实例在运行，本实例退出（请留意右下角托盘图标）。")
        print("[API Hub] 已有实例在运行，退出。")
        return
    _launch_log("尝试启动服务…" + ("（静默模式）" if silent else ""))
    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    if not wait_port(port):
        msg = (f"服务启动失败：端口 {port} 未能监听。"
               "可能原因：端口被其他程序/旧实例占用，或依赖缺失。"
               "请先确认右下角托盘是否有旧图标并退出，再双击 run.bat 查看详细报错。")
        print(f"[API Hub] {msg}")
        _launch_log(msg)
        return

    print(f"[API Hub] 网关已运行: http://127.0.0.1:{port}")
    _launch_log("服务已就绪。" + ("（静默模式，窗口待托盘唤起）" if silent else ""))
    try:
        import webview
        _show_window(initial_hidden=silent)
        # 托盘常驻（独立线程）
        threading.Thread(target=_tray_loop, args=(port,), daemon=True).start()
        webview.start()
        # 到达这里 = 窗口关闭未被拦截（旧版 pywebview/异常路径）→ 保持进程存活，
        # 让托盘「显示窗口」能重建窗口，服务不中断
        print("[API Hub] 窗口已关闭，转入托盘后台运行（右键托盘图标恢复窗口）")
        _launch_log("窗口关闭，后台托盘模式。")
        while True:
            time.sleep(3600)
    except Exception as e:
        msg = f"原生窗口不可用（{e}），改用浏览器打开"
        print(f"[API Hub] {msg}")
        _launch_log(msg)
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _launch_log(f"未捕获异常: {e}")
