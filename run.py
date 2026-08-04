#!/usr/bin/env python3
"""自动化交易系统入口。

用法：
    # 模拟盘（默认，不发生真实交易）
    .venv/bin/python run.py

    # 实盘（双重确认，真实下单，谨慎！）
    TRADING_MODE=live LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK .venv/bin/python run.py

启动后访问 http://127.0.0.1:8000 查看实时面板，点击"开始"正式交易，Ctrl+C 停止。

日志：控制台 + log/ 目录（按日期滚动，如 log/trading.log.2026-08-04）。
每行日志包含：时间、级别、源文件、代码行号。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

sys.path.insert(0, ".")  # 允许从项目根目录直接运行

import uvicorn  # noqa: E402

from trading import config  # noqa: E402
from trading.web.app import app  # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    # 时间 | 级别 | 文件:代码行 | 模块 | 内容
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(name)s | %(message)s"
    )
    # 每天 0 点滚动，历史文件自动以日期后缀保存（trading.log.2026-08-04），保留 30 天
    file_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "trading.log"),
        when="midnight", backupCount=30, encoding="utf-8",
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
