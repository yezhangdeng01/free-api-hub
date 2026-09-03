"""无窗口模式：只启动网关服务（供调试或后台常驻使用）"""
import uvicorn

from app import config as cfgmod
from app.main import app

if __name__ == "__main__":
    cfg = cfgmod.load_config()
    print(f"[API Hub] 网关已运行: http://127.0.0.1:{cfg['port']}（Ctrl+C 停止）")
    uvicorn.run(app, host="127.0.0.1", port=cfg["port"], log_level="info")
