"""自动化交易系统配置。

所有敏感信息（API 密钥）仍由 gate_api 从环境变量读取，本文件不含任何密钥。
"""

from __future__ import annotations

import os

# ---- 交易币对白名单（硬性限制，系统只允许这三个现货交易对）----
PAIRS: tuple[str, ...] = ("BTC_USDT", "DOGE_USDT", "ETH_USDT")

# ---- 运行模式 ----
# paper: 模拟盘（默认），用真实行情 + 虚拟资金撮合，不发生真实交易
# live : 实盘，真实下单。除 TRADING_MODE=live 外，还必须设置
#        LIVE_TRADING_CONFIRM=YES_I_ACCEPT_RISK 双重确认才会生效。
TRADING_MODE = os.environ.get("TRADING_MODE", "paper").lower()
LIVE_CONFIRM = os.environ.get("LIVE_TRADING_CONFIRM", "")

# ---- 行情轮询间隔（秒）----
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL", "3"))

# ---- 健康心跳间隔（秒）：每隔多久在运行日志里报一条"运行正常" ----
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "600"))

# ---- 模拟盘仓位 ----
# true: 模拟盘初始仓位完全镜像真实现货账户（USDT 与各基础币按实际可用余额）；
# false: 使用下方 GRID_CONFIG 中配置的虚拟 quote_budget / base_budget。
PAPER_MIRROR_REAL = os.environ.get("PAPER_MIRROR_REAL", "true").lower() == "true"

# ---- 网格参数（按交易对）----
# range_pct   : 以启动时刻价格为中轴，网格区间上下浮动比例
# grids       : 网格层数（价格档位数量）
# quote_budget: （仅 PAPER_MIRROR_REAL=false 时生效）该交易对用于买入的 USDT 预算
# base_budget : （仅 PAPER_MIRROR_REAL=false 时生效）该交易对用于卖出的基础币数量
GRID_CONFIG: dict = {
    "BTC_USDT": {"range_pct": 0.06, "grids": 21, "quote_budget": 100.0, "base_budget": 0.0},
    "DOGE_USDT": {"range_pct": 0.12, "grids": 21, "quote_budget": 50.0, "base_budget": 0.0},
    "ETH_USDT": {"range_pct": 0.08, "grids": 21, "quote_budget": 100.0, "base_budget": 0.0},
}

# ---- 实盘风控 ----
# 单笔订单最大 USDT 金额（实盘模式下强制约束）
MAX_ORDER_QUOTE = float(os.environ.get("MAX_ORDER_QUOTE", "50"))

# ---- 数据目录 ----
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "trading.db")

# ---- Web 服务 ----
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))
