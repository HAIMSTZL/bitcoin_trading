"""全市场币种筛选打分（槽位补位用）。

两阶段：
1. 粗筛（一次全市场 ticker 调用）：USDT 交易对按硬性条件排除妖币/死币
   （成交额、振幅、单日涨跌），取成交额 Top N；
2. 精筛打分（0~100）：流动性、点差、深度、ATR 波动甜区、热度稳定性，
   达到 SCREEN_MIN_SCORE 才算合格候选。

只读操作，不涉及任何交易。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from gate_api import SpotAPI

from . import config
from .indicators import atr_percent

log = logging.getLogger("trading.screener")


def _hourly_volume_cv(candles_1h: list[list]) -> float:
    """24h 逐小时成交额的变异系数（CV）：越小热度越持续，越大越是脉冲爆量。"""
    vols = [float(r[1]) for r in sorted(candles_1h, key=lambda r: int(r[0]))]
    if len(vols) < 6:
        return 1.0
    mean = sum(vols) / len(vols)
    if mean <= 0:
        return 1.0
    var = sum((v - mean) ** 2 for v in vols) / len(vols)
    return math.sqrt(var) / mean


def coarse_filter(
    spot: SpotAPI, exclude: set[str], top_n: int = config.SCREEN_TOP_N
) -> list[dict]:
    """粗筛：返回 [{pair, quote_volume, amplitude, change_pct, last}] 按成交额降序。"""
    tickers = spot.list_tickers()
    rows = []
    exclude = exclude | config.SCREEN_EXCLUDE  # 锚定型资产永不相入
    for t in tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT") or pair in exclude:
            continue
        try:
            last = float(t["last"])
            high = float(t["high_24h"])
            low = float(t["low_24h"])
            qv = float(t.get("quote_volume") or 0)
            chg = float(t.get("change_percentage") or 0)
        except (ValueError, TypeError):
            continue
        if last <= 0 or low <= 0:
            continue
        amplitude = (high - low) / low * 100
        if qv < config.SCREEN_MIN_QUOTE_VOL:       # 死币
            continue
        if amplitude > config.SCREEN_MAX_AMPLITUDE:  # 妖币（暴拉暴跌）
            continue
        if abs(chg) > config.SCREEN_MAX_CHANGE:      # 单日异动
            continue
        rows.append({"pair": pair, "quote_volume": qv, "amplitude": amplitude,
                     "change_pct": chg, "last": last})
    rows.sort(key=lambda r: -r["quote_volume"])
    return rows[:top_n]


def score_candidate(spot: SpotAPI, pair: str) -> Optional[dict]:
    """精筛打分：0~100。不满足硬条件返回 None。"""
    # 盘口：点差 + ±0.5% 深度
    ob = spot.list_order_book(pair, 20)
    bids = [(float(p), float(a)) for p, a in ob["bids"]]
    asks = [(float(p), float(a)) for p, a in ob["asks"]]
    if not bids or not asks:
        return None
    spread = (asks[0][0] - bids[0][0]) / bids[0][0] * 100
    if spread > config.SCREEN_MAX_SPREAD:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    depth = sum(p * a for p, a in bids if p >= mid * 0.995) \
        + sum(p * a for p, a in asks if p <= mid * 1.005)

    # 波动甜区：15m ATR%
    candles = spot.list_candlesticks(pair, config.INDICATOR_KLINE, 60)
    atr = atr_percent(candles)
    if atr <= 0 or atr > config.SCREEN_MAX_ATR:
        return None

    # 热度稳定性：1h K 线 24 根的成交额 CV
    candles_1h = spot.list_candlesticks(pair, "1h", 24)
    cv = _hourly_volume_cv(candles_1h)

    # ---- 打分 ----
    liq = min(math.log10(max(depth, 1) / 1e4) / 2, 1.0) * 30          # 深度 30 分
    spread_score = max(0.0, 1 - spread / config.SCREEN_MAX_SPREAD) * 25  # 点差 25 分
    if 0.2 <= atr <= 1.5:                                  # 甜区满分
        vol = 25.0
    elif atr < 0.2:                                        # 太平静
        vol = max(0.0, (atr - 0.05) / 0.15) * 25
    else:                                                  # 偏亢奋
        vol = max(0.0, 1 - (atr - 1.5) / (config.SCREEN_MAX_ATR - 1.5)) * 25
    stability = max(0.0, 1 - min(cv, 1.5) / 1.5) * 20      # 稳定性 20 分

    total = liq + spread_score + vol + stability
    return {
        "pair": pair, "score": round(total, 1),
        "spread_pct": round(spread, 4), "depth_usdt": round(depth, 0),
        "atr_pct": round(atr, 3), "volume_cv": round(cv, 2),
    }


def screen_top(spot: SpotAPI, exclude: set[str], n: int = 1) -> list[dict]:
    """执行完整筛选，返回按分数降序的合格候选（最多 n 个）；无合格返回空表。"""
    candidates = coarse_filter(spot, exclude)
    log.info("粗筛通过 %d 个候选: %s", len(candidates),
             [c["pair"] for c in candidates[:10]])
    scored = []
    for c in candidates:
        try:
            s = score_candidate(spot, c["pair"])
            if s:
                s["quote_volume"] = c["quote_volume"]
                scored.append(s)
        except Exception as e:
            log.warning("精筛 %s 失败: %s", c["pair"], e)
    scored.sort(key=lambda s: -s["score"])
    log.info("精筛打分: %s", [(s["pair"], s["score"]) for s in scored[:5]])
    qualified = [s for s in scored if s["score"] >= config.SCREEN_MIN_SCORE]
    if scored and not qualified:
        log.info("最优候选 %s 得分 %.1f < 及格线 %.1f，宁缺毋滥",
                 scored[0]["pair"], scored[0]["score"], config.SCREEN_MIN_SCORE)
    return qualified[:n]


def screen(spot: SpotAPI, exclude: set[str]) -> Optional[dict]:
    """执行完整筛选，返回最优合格候选；无合格候选返回 None。"""
    top = screen_top(spot, exclude, n=1)
    return top[0] if top else None
