"""基于 RSI 的现货均值回归策略及无前视 K 线回测。

这是网格策略之外的候选：在超卖后下一根 K 线开盘买入，以固定止盈、
固定止损或最长持仓期退出。它只做多现货，未满足信号时持有 USDT。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Protocol, Sequence

from . import config


class OhlcCandle(Protocol):
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class MeanReversionSettings:
    pairs: tuple[str, ...] = config.PAIRS
    total_quote_budget: float = config.TOTAL_QUOTE_BUDGET
    fee_rate: float = config.PAPER_FEE_RATE
    rsi_period: int = 14
    rsi_threshold: float = 30.0
    take_profit_pct: float = 0.02
    stop_loss_pct: float = 0.06
    max_hold_bars: int = 24


@dataclass
class MeanReversionPairResult:
    pair: str
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    total_fees: float
    trade_count: int
    stop_count: int


@dataclass
class MeanReversionResult:
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    total_fees: float
    trade_count: int
    stop_count: int
    pairs: list[MeanReversionPairResult]


def _max_drawdown_pct(equity_curve: Sequence[float]) -> float:
    peak = -math.inf
    drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak * 100)
    return drawdown


def _validate(settings: MeanReversionSettings) -> None:
    if not settings.pairs:
        raise ValueError("至少需要一个交易对")
    if settings.total_quote_budget <= 0 or settings.fee_rate < 0:
        raise ValueError("预算必须为正，手续费不能为负")
    if settings.rsi_period < 1 or not 0 < settings.rsi_threshold < 100:
        raise ValueError("RSI 周期至少为 1，阈值应在 (0, 100) 内")
    if not 0 < settings.take_profit_pct < 1 or not 0 < settings.stop_loss_pct < 1:
        raise ValueError("止盈、止损比例应在 (0, 1) 内")
    if settings.max_hold_bars < 1:
        raise ValueError("最大持仓 K 线数至少为 1")


def _run_pair(
    pair: str,
    candles: Sequence[OhlcCandle],
    budget: float,
    settings: MeanReversionSettings,
) -> MeanReversionPairResult:
    quote = budget
    base = 0.0
    entry_price = 0.0
    held_bars = 0
    entry_pending = False
    fees = 0.0
    trade_count = 0
    stop_count = 0
    previous_close: float | None = None
    gains: deque[float] = deque(maxlen=settings.rsi_period)
    losses: deque[float] = deque(maxlen=settings.rsi_period)
    equity_curve: list[float] = []

    for candle in candles:
        # 由上一根收盘信号产生的时间退出/入场，均在本根开盘执行。
        if base > 0 and held_bars >= settings.max_hold_bars:
            gross = base * candle.open
            fee = gross * settings.fee_rate
            quote += gross - fee
            fees += fee
            base = 0.0
            trade_count += 1
            entry_pending = False

        entered_this_bar = False
        if base == 0 and entry_pending:
            fee = quote * settings.fee_rate
            base = (quote - fee) / candle.open
            fees += fee
            quote = 0.0
            entry_price = candle.open
            held_bars = 0
            trade_count += 1
            entry_pending = False
            entered_this_bar = True

        if base > 0:
            path = (
                (candle.open, candle.low, candle.high, candle.close)
                if candle.close >= candle.open
                else (candle.open, candle.high, candle.low, candle.close)
            )
            stop_price = entry_price * (1 - settings.stop_loss_pct)
            target_price = entry_price * (1 + settings.take_profit_pct)
            exited = False
            # 已有仓位需要把开盘跳空纳入判断；本根刚开仓则从随后路径开始。
            for index, price in enumerate(path[1 if entered_this_bar else 0:],
                                          start=1 if entered_this_bar else 0):
                if price <= stop_price:
                    fill_price = price if index == 0 else stop_price
                    gross = base * fill_price
                    fee = gross * settings.fee_rate
                    quote += gross - fee
                    fees += fee
                    base = 0.0
                    trade_count += 1
                    stop_count += 1
                    exited = True
                    break
                if price >= target_price:
                    fill_price = price if index == 0 else target_price
                    gross = base * fill_price
                    fee = gross * settings.fee_rate
                    quote += gross - fee
                    fees += fee
                    base = 0.0
                    trade_count += 1
                    exited = True
                    break
            if not exited:
                held_bars += 1

        # 仅在当前 K 线收盘后更新 RSI，生成下一根的入场信号。
        if previous_close is not None:
            change = candle.close - previous_close
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        previous_close = candle.close
        if base == 0 and len(gains) == settings.rsi_period:
            avg_gain = sum(gains) / settings.rsi_period
            avg_loss = sum(losses) / settings.rsi_period
            rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
            if rsi < settings.rsi_threshold:
                entry_pending = True

        equity_curve.append(quote + base * candle.close)

    final_equity = equity_curve[-1]
    buy_hold_equity = (
        budget * (1 - settings.fee_rate) * candles[-1].close / candles[0].open
    )
    return MeanReversionPairResult(
        pair=pair,
        initial_equity=budget,
        final_equity=final_equity,
        return_pct=(final_equity / budget - 1) * 100,
        buy_hold_return_pct=(buy_hold_equity / budget - 1) * 100,
        max_drawdown_pct=_max_drawdown_pct(equity_curve),
        total_fees=fees,
        trade_count=trade_count,
        stop_count=stop_count,
    )


def run_mean_reversion_backtest(
    candles_by_pair: dict[str, Sequence[OhlcCandle]],
    settings: MeanReversionSettings = MeanReversionSettings(),
) -> MeanReversionResult:
    """运行等权分配的 RSI 超卖反弹策略，并返回组合与逐币对指标。"""
    _validate(settings)
    series = {pair: list(candles_by_pair.get(pair, ())) for pair in settings.pairs}
    missing = [pair for pair, candles in series.items() if not candles]
    if missing:
        raise ValueError(f"缺少 K 线: {', '.join(missing)}")

    common = {candle.ts for candle in series[settings.pairs[0]]}
    for pair in settings.pairs[1:]:
        common.intersection_update(candle.ts for candle in series[pair])
    if not common:
        raise ValueError("K 线没有共同时间点")
    aligned = {
        pair: [candle for candle in candles if candle.ts in common]
        for pair, candles in series.items()
    }

    budget = settings.total_quote_budget / len(settings.pairs)
    results = [_run_pair(pair, aligned[pair], budget, settings) for pair in settings.pairs]
    return MeanReversionResult(
        initial_equity=settings.total_quote_budget,
        final_equity=sum(result.final_equity for result in results),
        return_pct=(sum(result.final_equity for result in results)
                    / settings.total_quote_budget - 1) * 100,
        buy_hold_return_pct=sum(result.buy_hold_return_pct for result in results) / len(results),
        max_drawdown_pct=max(result.max_drawdown_pct for result in results),
        total_fees=sum(result.total_fees for result in results),
        trade_count=sum(result.trade_count for result in results),
        stop_count=sum(result.stop_count for result in results),
        pairs=results,
    )
