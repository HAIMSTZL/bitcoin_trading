#!/usr/bin/env python3
"""自动化交易系统入口。

用法：
    # 模拟盘（默认，不发生真实交易）
    .venv/bin/python run.py

    # 实盘（双重确认，真实下单，谨慎！）
    TRADING_MODE=live LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK .venv/bin/python run.py

启动后访问 http://127.0.0.1:8000 查看实时面板，点击"开始"正式交易，Ctrl+C 停止。

日志：控制台 + trading/data/trading.log（轮转，单文件 5MB，保留 3 个备份）。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

sys.path.insert(0, ".")  # 允许从项目根目录直接运行

import uvicorn  # noqa: E402

from trading import config  # noqa: E402
from trading.web.app import app  # noqa: E402


def setup_logging() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        os.path.join(config.DATA_DIR, "trading.log"),
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def main() -> None:
    setup_logging()
    log = logging.getLogger("run")

    from trading.engine import Engine  # 延迟导入，先配好日志

    try:
        engine = Engine()
    except Exception:
        # 初始化阶段的异常（网络、密钥、行情获取失败等）也必须留下记录
        log.critical("引擎初始化失败", exc_info=True)
        raise

    engine.start_background()
    app.state.engine = engine
    try:
        uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
