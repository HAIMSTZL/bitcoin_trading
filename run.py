#!/usr/bin/env python3
"""自动化交易系统入口。

用法：
    # 模拟盘（默认，不发生真实交易）
    .venv/bin/python run.py

    # 实盘（双重确认，真实下单，谨慎！）
    TRADING_MODE=live LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK .venv/bin/python run.py

启动后访问 http://127.0.0.1:8000 查看实时面板，点击"开始"正式交易，Ctrl+C 停止。

日志：控制台 + log/ 目录（按天分文件，如 log/trading_2026-08-09.log）。
每行日志包含：时间、级别、源文件、代码行号。
"""

from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, ".")  # 允许从项目根目录直接运行

import uvicorn  # noqa: E402

from trading import config  # noqa: E402
from trading.web.app import app  # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")


class DailyFileHandler(logging.FileHandler):
    """按天分文件写日志：log/trading_YYYY-MM-DD.log。

    每次写日志前检查日期，跨天自动切换到新文件（当天的日志就在以当天
    日期命名的文件里，不再有"活动文件 + 历史后缀"两套命名）；每天最多
    清理一次超过 keep_days 的旧文件。
    """

    def __init__(self, log_dir: str, prefix: str = "trading", keep_days: int = 30):
        self._log_dir = log_dir
        self._prefix = prefix
        self._keep_days = keep_days
        self._day = time.strftime("%Y-%m-%d")
        super().__init__(self._path_for(self._day), encoding="utf-8")

    def _path_for(self, day: str) -> str:
        return os.path.join(self._log_dir, f"{self._prefix}_{day}.log")

    def emit(self, record: logging.LogRecord) -> None:
        day = time.strftime("%Y-%m-%d")
        if day != self._day:
            with self.lock:
                self._day = day
                if self.stream:
                    self.stream.close()
                self.baseFilename = self._path_for(day)
                self.stream = self._open()
                self._purge_old()
        super().emit(record)

    def _purge_old(self) -> None:
        cutoff = time.time() - self._keep_days * 86400
        for name in os.listdir(self._log_dir):
            if not (name.startswith(f"{self._prefix}_") and name.endswith(".log")):
                continue
            path = os.path.join(self._log_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    # 时间 | 级别 | 文件:代码行 | 模块 | 内容
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(name)s | %(message)s"
    )
    # 按天分文件：log/trading_2026-08-09.log，跨天自动切换，保留 30 天
    file_handler = DailyFileHandler(LOG_DIR)
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def main() -> None:
    setup_logging()
    log = logging.getLogger("run")

    from trading.engine import Engine  # 延迟导入，先配好日志
    from trading.doge_trend_engine import DogeTrendPaperEngine
    from trading.predictive_engine import PredictivePaperEngine
    from trading.profiles import enabled_profiles

    config.validate()
    from trading import settings
    settings.load_overrides()  # 加载本地保存的参数覆盖
    profiles = enabled_profiles()
    # 实盘防呆：live 模式必须显式指定且仅运行一个策略
    if config.TRADING_MODE == "live" and len(profiles) != 1:
        raise RuntimeError(
            "实盘模式只允许运行一个策略，请用 STRATEGIES=策略名 显式指定"
        )
    engines = {}
    for name, profile in profiles.items():
        try:
            if profile.kind == "predictive":
                engine = PredictivePaperEngine(profile)
            elif profile.kind == "doge_trend":
                engine = DogeTrendPaperEngine(profile)
            else:
                engine = Engine(profile)
        except Exception:
            # 初始化阶段的异常（网络、密钥、行情获取失败等）也必须留下记录
            log.critical("引擎初始化失败: %s", name, exc_info=True)
            raise
        engine.start_background()
        engines[name] = engine
        log.info("策略已启动: %s (%s)", name, profile.label)

    app.state.engines = engines
    app.state.default = next(iter(engines))
    try:
        uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        for engine in engines.values():
            engine.stop()


if __name__ == "__main__":
    main()
