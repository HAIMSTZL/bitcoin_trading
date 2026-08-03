#!/usr/bin/env python3
"""自动化交易系统入口。

用法：
    # 模拟盘（默认，不发生真实交易）
    .venv/bin/python run.py

    # 实盘（双重确认，真实下单，谨慎！）
    TRADING_MODE=live LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK .venv/bin/python run.py

启动后访问 http://127.0.0.1:8000 查看实时面板，Ctrl+C 停止。
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, ".")  # 允许从项目根目录直接运行

import uvicorn  # noqa: E402

from trading import config  # noqa: E402
from trading.engine import Engine  # noqa: E402
from trading.web.app import app  # noqa: E402


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = Engine()
    engine.start_background()
    app.state.engine = engine
    try:
        uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
