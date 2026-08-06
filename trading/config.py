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
# true : 模拟盘初始仓位完全镜像真实现货账户（USDT 与各基础币按实际可用余额）；
# false: 使用下方 TOTAL_QUOTE_BUDGET 作为虚拟 USDT 总预算。
PAPER_MIRROR_REAL = os.environ.get("PAPER_MIRROR_REAL", "false").lower() == "true"

# ---- 虚拟资金总预算与动态分配 ----
# 当前设定（用户指定）：纯 233 USDT 起步、无基础币持仓。
TOTAL_QUOTE_BUDGET = float(os.environ.get("TOTAL_QUOTE_BUDGET", "233"))
# true: 按各币对近期波动率（ATR%）动态分配总预算——网格赚波动的钱，
#       波动越大分越多；ALLOC_MIN/MAX_W 限制单币对占比，防极端。
# false: 三币对均分。
DYNAMIC_ALLOCATION = os.environ.get("DYNAMIC_ALLOCATION", "true").lower() == "true"
ALLOC_MIN_W = float(os.environ.get("ALLOC_MIN_W", "0.15"))
ALLOC_MAX_W = float(os.environ.get("ALLOC_MAX_W", "0.60"))

# ---- 定期再平衡（子弹仓位调整，仅模拟盘）----
# 每隔 REBALANCE_INTERVAL 秒，按当时 ATR%×信号倾斜重算权重，
# 把各币对【未被买单占用的空闲 USDT】按权重重新分配——只挪子弹，绝不动持仓；
# 偏离不足池子的 REBALANCE_MIN_DRIFT 比例（且 <1U）时不动作，避免无效折腾。
REBALANCE_INTERVAL = float(os.environ.get("REBALANCE_INTERVAL", "600"))
REBALANCE_MIN_DRIFT = float(os.environ.get("REBALANCE_MIN_DRIFT", "0.1"))
# 信号倾斜系数：偏多币对权重上调、偏空下调（0.25 = 信号 ±1 时权重 ±25%）
REBALANCE_SIGNAL_TILT = float(os.environ.get("REBALANCE_SIGNAL_TILT", "0.25"))

# ---- 网格参数（按交易对）----
# range_pct   : 以启动时刻价格为中轴，网格区间上下浮动比例
# grids       : 网格层数（价格档位数量）
# base_budget : （仅 PAPER_MIRROR_REAL=false 时生效）该交易对初始基础币数量
#
# 区间选取经验：档位间距 = 2*range_pct/(grids-1)，必须显著大于双边手续费
# （约 0.2%），且与币种日常波动匹配。
GRID_CONFIG: dict = {
    "BTC_USDT": {"range_pct": 0.03, "grids": 21, "base_budget": 0.0},
    "DOGE_USDT": {"range_pct": 0.04, "grids": 21, "base_budget": 0.0},
    "ETH_USDT": {"range_pct": 0.035, "grids": 21, "base_budget": 0.0},
}
# 补位新币对的默认网格参数（未在 GRID_CONFIG 中配置的币对使用）
GRID_DEFAULT: dict = {"range_pct": 0.04, "grids": 21, "base_budget": 0.0}

# ---- 指标信号过滤 ----
# true: MACD/KDJ/盘口/主动成交 汇总为趋势信号，偏空时暂停挂买单（不接飞刀）、
#       偏多时暂停挂卖单（不卖飞）；false: 不使用信号，双向正常挂单。
USE_SIGNAL_FILTER = os.environ.get("USE_SIGNAL_FILTER", "true").lower() == "true"
# 指标刷新周期（秒），独立于行情 tick（K线+盘口+逐笔 3 个接口/币对/次）
INDICATOR_INTERVAL = float(os.environ.get("INDICATOR_INTERVAL", "120"))
# 指标所用 K 线周期
INDICATOR_KLINE = os.environ.get("INDICATOR_KLINE", "15m")
# 盘口买卖量比 / 主动买卖量比的打分阈值（±20% 太敏感，放宽到 ±50%）
DEPTH_RATIO_THRESHOLD = float(os.environ.get("DEPTH_RATIO_THRESHOLD", "1.5"))
TRADE_RATIO_THRESHOLD = float(os.environ.get("TRADE_RATIO_THRESHOLD", "1.5"))
# 信号确认：连续 N 次同向才翻转 + 翻转后冷却期（秒）内不再翻转（防抖）
SIGNAL_CONFIRM_COUNT = int(os.environ.get("SIGNAL_CONFIRM_COUNT", "2"))
SIGNAL_COOLDOWN = float(os.environ.get("SIGNAL_COOLDOWN", "180"))

# ---- 模拟盘手续费 ----
# 每笔成交按成交金额扣除该费率（单边），让模拟盘利润接近实盘
PAPER_FEE_RATE = float(os.environ.get("PAPER_FEE_RATE", "0.001"))

# ---- 自适应网格区间 ----
# true: 网格区间不再固定，range = clamp(ADAPTIVE_RANGE_MULT × ATR%, MIN, MAX)
#       波动大自动加宽间距（每格利润厚、成交少而稳），波动小自动收窄。
ADAPTIVE_RANGE = os.environ.get("ADAPTIVE_RANGE", "true").lower() == "true"
ADAPTIVE_RANGE_MULT = float(os.environ.get("ADAPTIVE_RANGE_MULT", "10"))
# 注意：下限 3% = 21 档时间距约 0.3%，必须显著大于双边手续费（约 0.2%）才有净利润
RANGE_PCT_MIN = float(os.environ.get("RANGE_PCT_MIN", "0.03"))
RANGE_PCT_MAX = float(os.environ.get("RANGE_PCT_MAX", "0.15"))

# ---- 趋势市识别 ----
# 3 小时涨跌幅超过该值且确认信号同向 = 趋势市：
# 趋势下跌→冻结买单（握 USDT 等待）；趋势上涨→冻结卖单（握住筹码）。
TREND_MOVE_PCT = float(os.environ.get("TREND_MOVE_PCT", "4.0"))

# ---- 币种筛选与补位（槽位制）----
# 交易序列固定 3 个槽位，初始为 PAIRS。某槽位曾持仓且绝对空仓（base=0 且无卖单）
# → 触发全市场筛选，最优合格候选补位；筛不到则维持原币对，每小时重筛。
SCREEN_INTERVAL = float(os.environ.get("SCREEN_INTERVAL", "3600"))  # 空仓重筛周期（秒）
SCREEN_TOP_N = int(os.environ.get("SCREEN_TOP_N", "20"))            # 粗筛后精筛数量
SCREEN_MIN_SCORE = float(os.environ.get("SCREEN_MIN_SCORE", "60"))  # 合格分数线
# 硬性排除（妖币/死币过滤）
SCREEN_MIN_QUOTE_VOL = float(os.environ.get("SCREEN_MIN_QUOTE_VOL", "5000000"))  # 24h成交额(USDT)
SCREEN_MAX_AMPLITUDE = float(os.environ.get("SCREEN_MAX_AMPLITUDE", "15"))       # 24h振幅%
SCREEN_MAX_CHANGE = float(os.environ.get("SCREEN_MAX_CHANGE", "20"))             # 24h涨跌%
SCREEN_MAX_SPREAD = float(os.environ.get("SCREEN_MAX_SPREAD", "0.05"))           # 盘口点差%
SCREEN_MAX_ATR = float(os.environ.get("SCREEN_MAX_ATR", "3.0"))                  # ATR% 上限
# 收尾：卖出后剩余持仓价值低于该值（USDT）时，直接全部扫尾卖出，让仓位归 0
SWEEP_DUST_USDT = float(os.environ.get("SWEEP_DUST_USDT", "5"))
# 几何网格：等百分比间距（跨价位更均匀，每格收益率一致），false 为等价差
GRID_GEOMETRIC = os.environ.get("GRID_GEOMETRIC", "true").lower() == "true"
# 锚定型资产（稳定币/金银代币）不参与补位筛选——波动被人为锚定，网格无利可图
SCREEN_EXCLUDE = {
    "USDC_USDT", "TUSD_USDT", "USD1_USDT", "FDUSD_USDT", "DAI_USDT",
    "USDE_USDT", "USDY_USDT", "PYUSD_USDT", "XAUT_USDT", "PAXG_USDT",
}

# ---- 熔断机制（tick 级快速断路器）----
# 单币对：现价相对 CB_WINDOW_MIN 分钟窗口内最高点回撤 ≥ CB_DROP_PCT% → 冻结该币对双侧挂单；
# 大盘：BTC 同窗口回撤 ≥ CB_GLOBAL_BTC_PCT% → 全系统停止撮合（行情与熔断检测继续运行）。
# 企稳判定：不再创新低持续 CB_RESUME_STABLE_MIN 分钟 → 自动恢复（CB_AUTO_RESUME）。
CB_ENABLED = os.environ.get("CB_ENABLED", "true").lower() == "true"
CB_DROP_PCT = float(os.environ.get("CB_DROP_PCT", "3.0"))
CB_WINDOW_MIN = float(os.environ.get("CB_WINDOW_MIN", "15"))
CB_GLOBAL_BTC_PCT = float(os.environ.get("CB_GLOBAL_BTC_PCT", "4.0"))
CB_AUTO_RESUME = os.environ.get("CB_AUTO_RESUME", "true").lower() == "true"
CB_RESUME_STABLE_MIN = float(os.environ.get("CB_RESUME_STABLE_MIN", "30"))

# ---- API 中断告警 ----
# 连续失败超过该秒数 → ERROR 事件 + 面板横幅；恢复后记 api_recovered 事件
API_OUTAGE_ALERT_SEC = float(os.environ.get("API_OUTAGE_ALERT_SEC", "30"))

# ---- 网格自动重建 ----
# true: 价格涨破/跌破网格区间时，以当前价格为中心自动重建网格（保留持仓与利润累计）
AUTO_RECENTER = os.environ.get("AUTO_RECENTER", "true").lower() == "true"

# ---- 实盘风控 ----
# 单笔订单最大 USDT 金额（实盘模式下强制约束）
MAX_ORDER_QUOTE = float(os.environ.get("MAX_ORDER_QUOTE", "50"))

# ---- 数据目录 ----
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "trading.db")

# ---- Web 服务 ----
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))


def validate() -> None:
    """启动时配置校验，非法配置直接 fail-fast。"""
    assert 0 < TICK_INTERVAL <= 60, "TICK_INTERVAL 应在 (0, 60] 秒"
    assert TOTAL_QUOTE_BUDGET > 0, "TOTAL_QUOTE_BUDGET 必须为正"
    assert 0 < ALLOC_MIN_W < ALLOC_MAX_W <= 1, "ALLOC_MIN_W/MAX_W 区间非法"
    assert PAPER_FEE_RATE >= 0, "PAPER_FEE_RATE 不能为负"
    assert RANGE_PCT_MIN < RANGE_PCT_MAX, "RANGE_PCT_MIN 必须小于 MAX"
    for p, c in GRID_CONFIG.items():
        assert c["grids"] >= 3, f"{p} grids 至少 3"
        assert 0 < c["range_pct"] < 0.5, f"{p} range_pct 应在 (0, 0.5)"
    if TRADING_MODE not in ("paper", "live"):
        raise ValueError(f"非法 TRADING_MODE: {TRADING_MODE}")
