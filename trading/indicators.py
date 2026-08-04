"""技术指标计算：MACD、KDJ、买卖盘热度。

数据源（Gate 公共接口）：
- K 线  /spot/candlesticks  -> MACD(12,26,9)、KDJ(9,3,3)
- 盘口  /spot/order_book    -> 买卖盘量比（前 N 档挂单量之比）
- 逐笔  /spot/trades        -> 主动买/卖成交量比

所有指标由本地 K 线数据计算，不依赖第三方库。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from gate_api import SpotAPI

log = logging.getLogger("trading.indicators")


# ----------------------------------------------------------------------
# 纯函数（可单测）
# ----------------------------------------------------------------------
def ema(values: list[float], period: int) -> list[float]:
    """指数移动平均，返回与输入等长的序列（首值为 SMA 种子）。"""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, float]:
    """MACD：返回 DIF(快线)、DEA(慢线/信号线)、HIST(柱=2*(DIF-DEA))。"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    # 对齐尾部（slow 序列更短）
    dif_series = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    dea_series = ema(dif_series, signal)
    if not dea_series:
        return {"dif": 0.0, "dea": 0.0, "hist": 0.0}
    dif, dea = dif_series[-1], dea_series[-1]
    return {"dif": dif, "dea": dea, "hist": 2 * (dif - dea)}


def kdj(
    highs: list[float], lows: list[float], closes: list[float], period: int = 9
) -> dict[str, float]:
    """KDJ(9,3,3)：RSV -> K -> D -> J=3K-2D。"""
    if len(closes) < period:
        return {"k": 50.0, "d": 50.0, "j": 50.0}
    k = d = 50.0
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1: i + 1])
        ll = min(lows[i - period + 1: i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        k = rsv / 3 + k * 2 / 3
        d = k / 3 + d * 2 / 3
    return {"k": k, "d": d, "j": 3 * k - 2 * d}


def book_pressure(order_book: dict, depth: int = 10) -> float:
    """买卖盘量比：前 depth 档买单量 / 卖单量。>1 买盘占优，<1 卖盘占优。"""
    bids = sum(float(a) for _, a in order_book.get("bids", [])[:depth])
    asks = sum(float(a) for _, a in order_book.get("asks", [])[:depth])
    return bids / asks if asks > 0 else 1.0


def atr_percent(candles: list[list], period: int = 14) -> float:
    """平均真实波幅占价格的百分比（ATR%），衡量近期波动率。

    candles: Gate K 线 [ts, 成交额, 收, 高, 低, 开, 量, 完结]，按时间升序。
    TR = max(高-低, |高-前收|, |低-前收|)
    """
    rows = sorted(candles, key=lambda r: int(r[0]))
    if len(rows) < period + 1:
        return 0.0
    closes = [float(r[2]) for r in rows]
    highs = [float(r[3]) for r in rows]
    lows = [float(r[4]) for r in rows]
    trs = []
    for i in range(1, len(rows)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr = sum(trs[-period:]) / period
    return atr / closes[-1] * 100 if closes[-1] else 0.0


def trade_pressure(trades: list[dict]) -> float:
    """主动买卖量比：主动买入量 / 主动卖出量。"""
    buy = sum(float(t["amount"]) for t in trades if t.get("side") == "buy")
    sell = sum(float(t["amount"]) for t in trades if t.get("side") == "sell")
    return buy / sell if sell > 0 else 1.0


def combine_signal(
    hist: float, k: float, d: float, ob_ratio: float, trade_ratio: float
) -> tuple[int, str]:
    """汇总为趋势信号：+1 偏多 / 0 中性 / -1 偏空，附中文说明。

    用作网格的趋势过滤：偏空时暂停挂买单（不接飞刀），偏多时暂停挂卖单
    （不卖飞上涨）。中性时双向正常。
    """
    score = 0
    score += 1 if hist > 0 else -1                      # MACD 柱方向
    score += 1 if k > d else -1                         # KDJ 金叉/死叉
    score += 1 if ob_ratio > 1.2 else (-1 if ob_ratio < 0.8 else 0)   # 盘口
    score += 1 if trade_ratio > 1.2 else (-1 if trade_ratio < 0.8 else 0)  # 主动成交
    if score >= 2:
        return 1, f"偏多(MACD{'↑' if hist>0 else '↓'} KDJ{'金叉' if k>d else '死叉'} 盘口{ob_ratio:.2f} 主动买{trade_ratio:.2f})"
    if score <= -2:
        return -1, f"偏空(MACD{'↑' if hist>0 else '↓'} KDJ{'金叉' if k>d else '死叉'} 盘口{ob_ratio:.2f} 主动买{trade_ratio:.2f})"
    return 0, f"中性(MACD{'↑' if hist>0 else '↓'} KDJ{'金叉' if k>d else '死叉'} 盘口{ob_ratio:.2f} 主动买{trade_ratio:.2f})"


# ----------------------------------------------------------------------
class IndicatorEngine:
    """按周期刷新各币对指标（独立于行情 tick 的低频任务，默认 30s）。"""

    def __init__(self, spot: SpotAPI, interval: str = "5m", kline_limit: int = 60):
        self._spot = spot
        self.interval = interval
        self.kline_limit = kline_limit
        self.data: dict[str, dict] = {}
        self.last_update: Optional[float] = None
        self.last_error: Optional[str] = None

    def update(self, pairs: tuple[str, ...]) -> None:
        for pair in pairs:
            try:
                self.data[pair] = self._compute(pair)
            except Exception as e:
                self.last_error = f"{pair}: {type(e).__name__}: {e}"
                log.warning("指标计算失败 %s", self.last_error)
        self.last_update = time.time()

    def _compute(self, pair: str) -> dict:
        candles = self._spot.list_candlesticks(pair, self.interval, self.kline_limit)
        # [ts, 成交额, 收, 高, 低, 开, 量, 完结标记] —— 按时间升序
        rows = sorted(candles, key=lambda r: int(r[0]))
        closes = [float(r[2]) for r in rows]
        highs = [float(r[3]) for r in rows]
        lows = [float(r[4]) for r in rows]

        m = macd(closes)
        j = kdj(highs, lows, closes)
        ob = book_pressure(self._spot.list_order_book(pair, 10))
        tp = trade_pressure(self._spot.list_public_trades(pair, 60))
        signal, signal_text = combine_signal(m["hist"], j["k"], j["d"], ob, tp)
        return {
            "macd": m, "kdj": j,
            "book_ratio": ob, "trade_ratio": tp,
            "atr_pct": atr_percent(candles),
            "signal": signal, "signal_text": signal_text,
        }

    def get(self, pair: str) -> dict:
        return self.data.get(pair, {
            "macd": {"dif": 0, "dea": 0, "hist": 0},
            "kdj": {"k": 50, "d": 50, "j": 50},
            "book_ratio": 1.0, "trade_ratio": 1.0,
            "signal": 0, "signal_text": "暂无数据",
        })
