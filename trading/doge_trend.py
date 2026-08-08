"""DOGE 专用的低频趋势恢复策略及可复现回测。

策略只交易 ``DOGE_USDT``，始终为 long/flat：RSI-20 超卖时先以小仓试探；
只有价格重新上穿短 EMA、且已高于持仓成本时，才把仓位加至满仓。退出由固定
获利目标、硬止损和最长持仓时间共同约束。所有收盘信号均在下一根 K 线开盘成交，
K 线内止盈/止损按方向性 OHLC 路径处理，不使用未来数据。

这不是收益承诺。它是一个可复用的研究候选，必须继续经受前向模拟和真实滑点检验。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

from gate_api import GatePublicClient

from . import config
from .backtest import Candle, fetch_gate_candles
from .predictive import load_market_snapshot


class OhlcCandle(Protocol):
    """回测所需的最小 OHLC 接口。"""

    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class DogeTrendSettings:
    """DOGE 趋势恢复候选的参数。

    ``initial_fraction`` 是超卖时的试探仓位。短 EMA 恢复向上且价格高于平均成本时，
    策略会在下一根开盘加到 100%，因此允许但不盲目 all-in。
    """

    pair: str = "DOGE_USDT"
    total_quote_budget: float = config.TOTAL_QUOTE_BUDGET
    fee_rate: float = config.PAPER_FEE_RATE
    slippage_bps: float = config.DOGE_TREND_SLIPPAGE_BPS
    rsi_period: int = 20
    rsi_threshold: float = 25.0
    initial_fraction: float = 0.5
    confirmation_ema_period: int = 6
    take_profit_pct: float = 0.05
    stop_loss_pct: float = 0.05
    max_hold_bars: int = 72
    # 止损后必须先看到一个后续收盘 RSI 回到阈值上方，才会允许下一次超卖试探。
    # 这是状态机保护，而不是经回测搜索出的“最佳冷却小时数”。
    require_rsi_rearm_after_stop: bool = True


@dataclass
class DogeTrendResult:
    start_ts: int
    end_ts: int
    settings: DogeTrendSettings
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_equity: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    total_fees: float
    total_slippage: float
    total_turnover: float
    trade_count: int
    entry_count: int
    add_count: int
    exit_count: int
    take_profit_count: int
    stop_count: int
    time_exit_count: int
    closed_trade_count: int
    win_rate_pct: float
    exposure_pct: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = _format_ts(self.start_ts)
        data["end"] = _format_ts(self.end_ts)
        return data


@dataclass
class DogeWalkForwardResult:
    """同一参数在早期开发段和后期独立验证段的结果。"""

    split_ts: int
    development: DogeTrendResult
    validation: DogeTrendResult
    full: DogeTrendResult

    def to_dict(self) -> dict:
        return {
            "split_ts": self.split_ts,
            "split": _format_ts(self.split_ts),
            "development": self.development.to_dict(),
            "validation": self.validation.to_dict(),
            "full": self.full.to_dict(),
        }


def _validate(settings: DogeTrendSettings) -> None:
    if settings.pair != "DOGE_USDT":
        raise ValueError("DogeTrendStrategy 仅允许 DOGE_USDT")
    if settings.total_quote_budget <= 0 or settings.fee_rate < 0:
        raise ValueError("预算必须为正，手续费不能为负")
    if settings.slippage_bps < 0:
        raise ValueError("滑点不能为负")
    if settings.rsi_period < 2 or not 0 < settings.rsi_threshold < 100:
        raise ValueError("RSI 周期至少为 2，阈值应在 (0, 100) 内")
    if not 0 < settings.initial_fraction <= 1:
        raise ValueError("试探仓位应在 (0, 1] 内")
    if settings.confirmation_ema_period < 2:
        raise ValueError("确认 EMA 周期至少为 2")
    if not 0 < settings.take_profit_pct < 1 or not 0 < settings.stop_loss_pct < 1:
        raise ValueError("止盈和止损应在 (0, 1) 内")
    if settings.max_hold_bars < 1:
        raise ValueError("最大持仓 K 线数至少为 1")
    if not isinstance(settings.require_rsi_rearm_after_stop, bool):
        raise ValueError("止损后 RSI 重新武装开关必须为布尔值")


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    value: float | None = None
    output: list[float] = []
    for close in values:
        value = close if value is None else alpha * close + (1 - alpha) * value
        output.append(value)
    return output


def _simple_rsi(values: Sequence[float], period: int) -> list[float | None]:
    """与已有均值回归候选一致的滚动简单 RSI，只使用已收盘价格。"""
    gains: list[float] = []
    losses: list[float] = []
    output: list[float | None] = []
    for index, close in enumerate(values):
        if index:
            change = close - values[index - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
            if len(gains) > period:
                gains.pop(0)
                losses.pop(0)
        if len(gains) < period:
            output.append(None)
            continue
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        output.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return output


def _max_drawdown_pct(curve: Sequence[float]) -> float:
    peak = -math.inf
    drawdown = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak * 100)
    return drawdown


def _path(candle: OhlcCandle) -> tuple[float, ...]:
    """保守的方向性 K 线内路径，和经典网格回测采用相同约定。"""
    if candle.close >= candle.open:
        return candle.open, candle.low, candle.high, candle.close
    return candle.open, candle.high, candle.low, candle.close


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def run_doge_trend_backtest(
    candles: Sequence[OhlcCandle],
    settings: DogeTrendSettings = DogeTrendSettings(),
    *,
    start_index: int = 0,
    end_index: int | None = None,
) -> DogeTrendResult:
    """运行 DOGE staged long/flat 回测。

    ``start_index`` 用于独立验证：指标仍可读取此前已发生的 K 线，但账户在该点以
    全新 USDT 重新开始，绝不携带开发段的持仓或收益。
    """
    _validate(settings)
    series = list(candles)
    if end_index is None:
        end_index = len(series)
    if not 0 <= start_index < end_index <= len(series):
        raise ValueError("回测切片范围非法")
    if any(candle.open <= 0 or candle.low <= 0 or candle.high < candle.low
           for candle in series):
        raise ValueError("K 线 OHLC 非法")

    closes = [candle.close for candle in series]
    rsi = _simple_rsi(closes, settings.rsi_period)
    confirmation_ema = _ema(closes, settings.confirmation_ema_period)
    slippage = settings.slippage_bps / 10_000

    quote = settings.total_quote_budget
    base = 0.0
    average_cost = 0.0  # 每个 DOGE 的实际综合买入成本（含买入手续费、滑点）
    position_cost = 0.0
    held_bars = 0
    waiting_for_recovery = False
    pending_target: float | None = None
    # 防止 RSI 在同一段深跌中长期趴在阈值下方时，止损后下一根又立即接刀。
    # 重新武装必须发生在止损 K 线之后，随后一次新的超卖才可重试。
    rsi_rearmed = True
    stop_candle_index = -1
    total_fees = 0.0
    total_slippage = 0.0
    total_turnover = 0.0
    trade_count = entry_count = add_count = exit_count = 0
    take_profit_count = stop_count = time_exit_count = 0
    closed_trade_count = winning_trade_count = 0
    exposed_bars = 0
    curve: list[float] = []

    def sell_all(price: float, reason: str) -> None:
        nonlocal quote, base, average_cost, position_cost, total_fees
        nonlocal total_slippage, total_turnover, trade_count, exit_count
        nonlocal take_profit_count, stop_count, time_exit_count, closed_trade_count
        nonlocal winning_trade_count, waiting_for_recovery, pending_target
        nonlocal rsi_rearmed, stop_candle_index
        if base <= 0:
            return
        raw_notional = base * price
        fill_notional = raw_notional * (1 - slippage)
        fee = fill_notional * settings.fee_rate
        net_proceeds = fill_notional - fee
        quote += net_proceeds
        total_fees += fee
        total_slippage += raw_notional - fill_notional
        total_turnover += raw_notional
        trade_count += 1
        exit_count += 1
        closed_trade_count += 1
        if net_proceeds > position_cost:
            winning_trade_count += 1
        if reason == "take_profit":
            take_profit_count += 1
        elif reason == "stop":
            stop_count += 1
            if settings.require_rsi_rearm_after_stop:
                rsi_rearmed = False
                stop_candle_index = index
        elif reason == "time":
            time_exit_count += 1
        base = average_cost = position_cost = 0.0
        waiting_for_recovery = False
        pending_target = None

    for index in range(start_index, end_index):
        candle = series[index]
        entered_this_bar = False

        # 最长持仓在当前开盘离场；信号仅来自此前收盘，故不存在同 K 线偷看。
        if base > 0 and held_bars >= settings.max_hold_bars:
            sell_all(candle.open, "time")

        # 上一根收盘产生的建仓/加仓信号，在本根开盘按目标风险暴露成交。
        if pending_target is not None:
            equity_at_open = quote + base * candle.open
            current_notional = base * candle.open
            desired_notional = equity_at_open * pending_target
            if desired_notional > current_notional + 1e-12:
                # 买入预算包含手续费；成交价另加不利滑点。
                spend = min(
                    quote,
                    (desired_notional - current_notional) / (1 + settings.fee_rate),
                )
                fee = spend * settings.fee_rate
                fill_price = candle.open * (1 + slippage)
                quantity = (spend - fee) / fill_price
                previous_base = base
                base += quantity
                quote -= spend
                total_fees += fee
                total_slippage += quantity * (fill_price - candle.open)
                total_turnover += quantity * candle.open
                trade_count += 1
                position_cost += spend
                average_cost = position_cost / base
                entered_this_bar = True
                if previous_base <= 0:
                    entry_count += 1
                    held_bars = 0
                    waiting_for_recovery = pending_target < 1.0
                else:
                    add_count += 1
                    waiting_for_recovery = False
            pending_target = None

        # 已有仓位才用本根 OHLC 检查止盈/止损。若刚开仓，路径从开盘后的点开始，
        # 避免把已发生在开盘前的跳空重复撮合一次。
        if base > 0:
            stop_price = average_cost * (1 - settings.stop_loss_pct)
            take_price = average_cost * (1 + settings.take_profit_pct)
            path = _path(candle)
            first_path_index = 1 if entered_this_bar else 0
            for path_index, price in enumerate(path[first_path_index:], start=first_path_index):
                if price <= stop_price:
                    sell_all(price if path_index == 0 else stop_price, "stop")
                    break
                if price >= take_price:
                    sell_all(price if path_index == 0 else take_price, "take_profit")
                    break

        # 收盘后生成下一根开盘的信号。
        if base <= 0:
            if (settings.require_rsi_rearm_after_stop and not rsi_rearmed
                    and index > stop_candle_index and rsi[index] is not None
                    and rsi[index] >= settings.rsi_threshold):
                rsi_rearmed = True
            if (rsi_rearmed and rsi[index] is not None
                    and rsi[index] < settings.rsi_threshold):
                pending_target = settings.initial_fraction
        elif waiting_for_recovery and index > 0:
            recovered = (
                closes[index - 1] <= confirmation_ema[index - 1]
                and candle.close > confirmation_ema[index]
                and candle.close > average_cost
            )
            if recovered:
                pending_target = 1.0

        if base > 0:
            held_bars += 1
            exposed_bars += 1
        curve.append(quote + base * candle.close)

    first = series[start_index]
    last = series[end_index - 1]
    buy_hold_base = (
        settings.total_quote_budget * (1 - settings.fee_rate)
        / (first.open * (1 + slippage))
    )
    final_equity = curve[-1]
    buy_hold_equity = buy_hold_base * last.close
    return DogeTrendResult(
        start_ts=first.ts,
        end_ts=last.ts,
        settings=settings,
        initial_equity=settings.total_quote_budget,
        final_equity=final_equity,
        return_pct=(final_equity / settings.total_quote_budget - 1) * 100,
        buy_hold_equity=buy_hold_equity,
        buy_hold_return_pct=(buy_hold_equity / settings.total_quote_budget - 1) * 100,
        max_drawdown_pct=_max_drawdown_pct(curve),
        total_fees=total_fees,
        total_slippage=total_slippage,
        total_turnover=total_turnover,
        trade_count=trade_count,
        entry_count=entry_count,
        add_count=add_count,
        exit_count=exit_count,
        take_profit_count=take_profit_count,
        stop_count=stop_count,
        time_exit_count=time_exit_count,
        closed_trade_count=closed_trade_count,
        win_rate_pct=(winning_trade_count / closed_trade_count * 100
                      if closed_trade_count else 0.0),
        exposure_pct=exposed_bars / len(curve) * 100,
    )


def run_doge_walk_forward(
    candles: Sequence[OhlcCandle],
    settings: DogeTrendSettings = DogeTrendSettings(),
    *,
    development_fraction: float = 2 / 3,
) -> DogeWalkForwardResult:
    """早期 2/3 用于开发、末段 1/3 作为独立的资本重置验证。"""
    if not 0.5 <= development_fraction < 1:
        raise ValueError("development_fraction 应在 [0.5, 1) 内")
    split = int(len(candles) * development_fraction)
    if split <= max(settings.rsi_period, settings.confirmation_ema_period):
        raise ValueError("K 线不足以进行 walk-forward 切分")
    return DogeWalkForwardResult(
        split_ts=candles[split].ts,
        development=run_doge_trend_backtest(candles, settings, end_index=split),
        validation=run_doge_trend_backtest(candles, settings, start_index=split),
        full=run_doge_trend_backtest(candles, settings),
    )


def _print_result(name: str, result: DogeTrendResult) -> None:
    print(
        f"{name}: {result.start_ts} 至 {result.end_ts} | "
        f"策略 {result.return_pct:+.2f}% ({result.final_equity:.2f}U) | "
        f"DOGE 持有 {result.buy_hold_return_pct:+.2f}% | 回撤 {result.max_drawdown_pct:.2f}%"
    )
    print(
        f"  边数 {result.trade_count}（试探 {result.entry_count} / 加仓 {result.add_count} / "
        f"退出 {result.exit_count}）| 胜率 {result.win_rate_pct:.1f}% | "
        f"敞口 {result.exposure_pct:.1f}% | 手续费 {result.total_fees:.2f}U | "
        f"滑点估计 {result.total_slippage:.2f}U"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DOGE staged trend-recovery 策略回测")
    parser.add_argument("--days", type=int, default=150, help="读取末尾多少天 1h K 线")
    parser.add_argument("--cache", default=config.PREDICTIVE_CACHE_PATH,
                        help="优先读取的本地 1h K 线快照路径")
    parser.add_argument("--fetch", action="store_true", help="忽略缓存，向 Gate 下载数据")
    parser.add_argument("--slippage-bps", type=float, default=config.DOGE_TREND_SLIPPAGE_BPS,
                        help="每侧不利滑点（默认 10 bps）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    if args.days <= 0 or args.slippage_bps < 0:
        parser.error("days 必须为正，slippage-bps 不能为负")

    cache_path = Path(args.cache)
    if not args.fetch and cache_path.exists():
        candles = load_market_snapshot(cache_path, ("DOGE_USDT",))["DOGE_USDT"]
    else:
        end_ts = int(time.time())
        start_ts = end_ts - args.days * 24 * 60 * 60
        client = GatePublicClient(timeout=20.0)
        try:
            candles = fetch_gate_candles("DOGE_USDT", "1h", start_ts, end_ts, client=client)
        finally:
            client.close()
    candles = candles[-args.days * 24:]
    settings = DogeTrendSettings(slippage_bps=args.slippage_bps)
    report = run_doge_walk_forward(candles, settings)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("DOGE staged trend-recovery | 1h 已收盘 K 线 | 仅 long/flat")
        _print_result("开发段", report.development)
        _print_result("验证段", report.validation)
        _print_result("全样本", report.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
