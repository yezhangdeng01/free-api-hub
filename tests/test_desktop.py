"""桌面壳（desktop.py）回归测试：托盘菜单 + 端口预绑定。

这两点各踩过一次真实故障，别再退化：
- 去掉 default 项 → 左键点托盘图标不再弹窗口（default 项才是左键动作）；
- 在 uvicorn.run 外面重试端口 → 每次重试都跑一遍 lifespan startup，bind 失败后
  shutdown 里 aclose() 掉 shared_client → 全渠道 "client has been closed"。
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop  # noqa: E402


def test_tray_menu_default_item_hidden():
    """左键靠 default 项（显示窗口），但它不占右键菜单的位置。"""
    called = []

    def mk(key):
        def _cb(icon, item):
            called.append(key)
        return _cb

    cb = {k: mk(k) for k in ("show", "settings", "restart", "autostart", "quit")}
    menu = desktop._build_menu(cb)
    visible = [i.text for i in menu if i.text != "- - - -"]  # 迭代只含可见项
    assert visible == ["打开配置文件夹", "重启服务", "开机自启", "退出"]
    assert "显示窗口" not in visible and "隐藏窗口" not in visible
    menu(None)  # pystray 左键路径：Menu.__call__ → 第一个 default 项
    assert called == ["show"]


def test_bind_listen_waits_for_listener_then_binds():
    """端口被占时不许抢（Windows 的 SO_REUSEADDR 会双监听同端口），释放后必须绑得上。"""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(4)
    port = held.getsockname()[1]
    try:
        assert desktop._bind_listen(port, timeout=1.0) is None
    finally:
        held.close()
    s = desktop._bind_listen(port, timeout=3.0)
    try:
        assert s is not None
        assert s.getsockname() == ("127.0.0.1", port)
    finally:
        if s is not None:
            s.close()
