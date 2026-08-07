"""可复现的 K 线级经典网格回测。

本模块刻意只回测可从 OHLC 历史数据可靠重建的部分：经典网格的下单、成交、
手续费、自动重心、限时止损及资金曲线。实时策略使用的盘口/逐笔成交信号和
全市场筛选没有完整历史数据，不能据此声称得到真实的 rotation/hunter 结果。

K 线不包含逐秒成交顺序。默认 ``directional`` 模式用常见的方向性路径近似：
阳线 O→L→H→C，阴线 O→H→L→C；结果应与 ``close`` 模式一起做敏感性比较，
不可视为逐笔成交回放。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Literal, Sequence

import requests

from . import config
from .grid import GridBot, PaperAccount


GATE_CANDLES_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
PathMode = Literal["directional", "close"]
RecenterMode = Literal["all", "up_only"]

_INTERVAL_SECONDS = {
    "10s": 10, "1m": 60, "5m": 5 * 60, "15m": 15 * 60,
    "30m": 30 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60,
    "8h": 8 * 60 * 60, "1d": 24 * 60 * 60, "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True)
class Candle:
    """Gate 已收盘 K 线的标准化表示。"""

    ts: int
    open: float
    high: float
    low: float
    close: float
    # Gate 原始记录的第 6 列是基础币成交量。为兼容已有调用，缺省为 0；
    # 新的预测策略会将它作为相对成交量特征，而不把它误当成未来可见信息。
    volume: float = 0.0


@dataclass(frozen=True)
class BacktestSettings:
    pairs: tuple[str, ...] = config.PAIRS
    total_quote_budget: float = config.TOTAL_QUOTE_BUDGET
    fee_rate: float = config.PAPER_FEE_RATE
    auto_recenter: bool = config.AUTO_RECENTER
    path_mode: PathMode = "directional"
    stoploss_hours: float = config.STUCK_STOPLOSS_HOURS
    range_scale: float = 1.0
    # 0 关闭。开启后，仅在上一根已收盘 K 线位于慢 EMA 上方时允许买入。
    trend_ema_period: int = 0
    # all 与生产 AUTO_RECENTER 语义一致；up_only 不在向下破位时追价重建。
    recenter_mode: RecenterMode = "all"
    # 以初始资金的该比例在首根 K 线开盘买入基础币。0 为当前 long-only 默认；
    # 0.5 是常规双向网格的中性库存起点。
    initial_base_fraction: float = 0.0
    # None 沿用交易对默认层数；研究时可统一覆盖，评估交易频率与手续费的权衡。
    grids: int | None = None
    # 跌破网格下界时立即撤掉买单，重新回到区间才允许补挂。
    downside_freeze: bool = False


@dataclass
class PairResult:
    pair: str
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_equity: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    realized_profit: float
    total_fees: float
    trade_count: int
    stoploss_count: int
    stoploss_profit: float
    candles: int


@dataclass
class BacktestResult:
    start_ts: int
    end_ts: int
    interval: str
    settings: BacktestSettings
    initial_equity: float
    final_equity: float
    return_pct: float
    buy_hold_equity: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    realized_profit: float
    total_fees: float
    trade_count: int
    stoploss_count: int
    stoploss_profit: float
    pairs: list[PairResult]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["settings"]["pairs"] = list(self.settings.pairs)
        data["start"] = _format_ts(self.start_ts)
        data["end"] = _format_ts(self.end_ts)
        return data


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def interval_seconds(interval: str) -> int:
    """返回 Gate K 线周期的秒数，未知周期立即报错。"""
    try:
        return _INTERVAL_SECONDS[interval]
    except KeyError as e:
        choices = ", ".join(_INTERVAL_SECONDS)
        raise ValueError(f"不支持的 K 线周期 {interval!r}，可选: {choices}") from e


def parse_gate_candles(rows: Iterable[Sequence[str]]) -> list[Candle]:
    """解析、去重、排序并移除未收盘的 Gate K 线。"""
    candles: dict[int, Candle] = {}
    for row in rows:
        if len(row) < 8 or row[7] != "true":
            continue
        try:
            candle = Candle(
                ts=int(row[0]), open=float(row[5]), high=float(row[3]),
                low=float(row[4]), close=float(row[2]), volume=float(row[6]),
            )
        except (TypeError, ValueError, IndexError) as e:
            raise ValueError(f"无效 Gate K 线: {row!r}") from e
        if candle.low <= 0 or candle.high < candle.low or not (candle.low <= candle.open <= candle.high and candle.low <= candle.close <= candle.high):
            raise ValueError(f"OHLC 范围非法: {row!r}")
        candles[candle.ts] = candle
    return [candles[ts] for ts in sorted(candles)]


def fetch_gate_candles(
    pair: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    *,
    request: Callable[..., requests.Response] = requests.get,
) -> list[Candle]:
    """分段读取 Gate 公共历史 K 线，不需要 API 密钥。"""
    seconds = interval_seconds(interval)
    if end_ts <= start_ts:
        raise ValueError("end_ts 必须晚于 start_ts")

    raw: list[Sequence[str]] = []
    cursor = start_ts
    max_rows = 1_000
    while cursor < end_ts:
        # Gate 的 from/to 都是包含端点，且非整点起始时会向上取整 K 线。
        # 留出两个周期，避免服务端将边界判为超过 1,000 个数据点。
        segment_end = min(end_ts, cursor + seconds * (max_rows - 2))
        response = request(
            GATE_CANDLES_URL,
            params={
                "currency_pair": pair, "interval": interval,
                "from": cursor, "to": segment_end, "limit": max_rows,
            },
            timeout=20,
        )
        response.raise_for_status()
        chunk = response.json()
        if not isinstance(chunk, list):
            raise ValueError(f"Gate K 线响应不是列表: {chunk!r}")
        raw.extend(chunk)
        cursor = segment_end + 1
        time.sleep(0.12)  # 公共接口节流；回测不应挤占实盘请求预算。

    return [c for c in parse_gate_candles(raw) if start_ts <= c.ts <= end_ts]


def _path(candle: Candle, mode: PathMode) -> tuple[float, ...]:
    if mode == "close":
        return (candle.close,)
    if mode != "directional":
        raise ValueError(f"未知路径模式: {mode}")
    if candle.close >= candle.open:
        return (candle.open, candle.low, candle.high, candle.close)
    return (candle.open, candle.high, candle.low, candle.close)


def _max_drawdown_pct(equity_curve: Sequence[float]) -> float:
    peak = -math.inf
    drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak * 100)
    return drawdown


def _build_bot(
    pair: str,
    start_price: float,
    quote: float,
    account: PaperAccount,
    clock: _Clock,
    settings: BacktestSettings,
    buy_blocked: bool = False,
    base_budget: float | None = None,
) -> GridBot:
    pair_config = config.GRID_CONFIG.get(pair, config.GRID_DEFAULT)
    range_pct = pair_config["range_pct"] * settings.range_scale
    grids = settings.grids or pair_config["grids"]
    # 保持与 Engine._build_bot 一致的费用间距保护。
    range_pct = max(range_pct, 2.2 * settings.fee_rate * (grids - 1) / 2)
    bot = GridBot(
        pair=pair,
        lower=start_price * (1 - range_pct),
        upper=start_price * (1 + range_pct),
        grids=grids,
        quote_budget=quote,
        base_budget=(float(pair_config["base_budget"])
                     if base_budget is None else base_budget),
        fee_rate=settings.fee_rate,
        geometric=config.GRID_GEOMETRIC,
        clock=clock,
        stoploss_hours=settings.stoploss_hours,
    )
    # 必须在 start 前写入，否则启动时会先挂出不应存在的买单。
    bot.signal = -1 if buy_blocked else 0
    bot.start(start_price, account)
    return bot


def _recenter(
    pair: str,
    bot: GridBot,
    account: PaperAccount,
    price: float,
    clock: _Clock,
    settings: BacktestSettings,
    buy_blocked: bool = False,
) -> GridBot:
    """回测版自动重心：与 Engine._recenter 保持相同的余额和盈亏继承语义。"""
    balance = account.get(pair)
    rebuilt = _build_bot(
        pair, price, balance["quote"], account, clock, settings, buy_blocked,
        base_budget=balance["base"],
    )
    rebuilt.realized_profit = bot.realized_profit
    rebuilt.trade_count = bot.trade_count
    rebuilt.blocked_count = bot.blocked_count
    rebuilt.total_fees = bot.total_fees
    rebuilt.ever_held = bot.ever_held
    rebuilt.avg_cost = bot.avg_cost
    if rebuilt.avg_cost is not None:
        rebuilt.orders = {
            idx: order for idx, order in rebuilt.orders.items()
            if not (order["side"] == "sell" and order["price"] <= rebuilt.avg_cost)
        }
    return rebuilt


def _apply_trend_filter(
    bot: GridBot,
    price: float,
    account: PaperAccount,
    buy_allowed: bool,
) -> None:
    """EMA 趋势门控：熊市撤掉未成交买单，转多后重建买单侧。

    已有卖单不会撤销，故已有库存仍可在反弹时按网格退出；这里只约束新增多头风险。
    """
    if not buy_allowed:
        bot.signal = -1
        bot.orders = {
            idx: order for idx, order in bot.orders.items() if order["side"] != "buy"
        }
        return
    was_blocked = bot.signal == -1
    bot.signal = 0
    if was_blocked and not any(order["side"] == "buy" for order in bot.orders.values()):
        bot.rebuild_buys(price, account)


def _should_recenter(
    bot: GridBot,
    price: float,
    settings: BacktestSettings,
    buy_allowed: bool,
    downside_frozen: bool = False,
) -> bool:
    """决定越界后的处理方式，避免下跌中把买入区间持续下移。"""
    if not settings.auto_recenter or not (price > bot.upper or price < bot.lower):
        return False
    if downside_frozen:
        return False
    if settings.recenter_mode == "all":
        return True
    if settings.recenter_mode == "up_only":
        return price > bot.upper and buy_allowed
    raise ValueError(f"未知重心模式: {settings.recenter_mode}")


def _next_ema(previous: float | None, close: float, period: int) -> float:
    """单值 EMA 更新；研究信号只使用此前已收盘 K 线。"""
    if previous is None:
        return close
    alpha = 2 / (period + 1)
    return close * alpha + previous * (1 - alpha)


def run_classic_backtest(
    candles_by_pair: dict[str, Sequence[Candle]],
    interval: str,
    settings: BacktestSettings = BacktestSettings(),
) -> BacktestResult:
    """运行纯 OHLC 可复现的经典 long-only 网格组合回测。

    各币对等额分配初始 USDT，不使用实时盘口/成交信号、动态分配或币种轮换。
    ``trend_ema_period`` 可开启纯 K 线可复现的趋势门控；它是候选策略，不替代
    生产版 rotation/hunter 的实时盘口与逐笔成交信号。
    """
    pairs = settings.pairs
    if not pairs:
        raise ValueError("至少需要一个交易对")
    if not 0 <= settings.initial_base_fraction <= 1:
        raise ValueError("initial_base_fraction 应在 [0, 1] 内")
    if settings.grids is not None and settings.grids < 3:
        raise ValueError("grids 至少为 3")
    series = {pair: list(candles_by_pair.get(pair, ())) for pair in pairs}
    missing = [pair for pair, candles in series.items() if not candles]
    if missing:
        raise ValueError(f"缺少 K 线: {', '.join(missing)}")

    # 只保留所有币对都有数据的时间点，不能把某币对的历史缺口悄悄当作持平。
    common_timestamps = {c.ts for c in series[pairs[0]]}
    for pair in pairs[1:]:
        common_timestamps.intersection_update(c.ts for c in series[pair])
    if not common_timestamps:
        raise ValueError("K 线没有共同的时间点")
    start_ts = min(common_timestamps)
    end_ts = max(common_timestamps)
    aligned = {
        pair: [c for c in candles if c.ts in common_timestamps]
        for pair, candles in series.items()
    }

    budget_per_pair = settings.total_quote_budget / len(pairs)
    account = PaperAccount()
    clock = _Clock()
    bots: dict[str, GridBot] = {}
    first_prices: dict[str, float] = {}
    equity_curves: dict[str, list[float]] = {pair: [] for pair in pairs}
    stoploss_counts = {pair: 0 for pair in pairs}
    stoploss_profits = {pair: 0.0 for pair in pairs}
    ema_values: dict[str, float | None] = {pair: None for pair in pairs}
    ema_samples = {pair: 0 for pair in pairs}
    downside_frozen = {pair: False for pair in pairs}

    for pair, candles in aligned.items():
        first = candles[0]
        clock.now = first.ts
        # 以开盘价买入初始库存，并计入买入手续费；这使 50/50 库存网格与
        # 买入持有基准都从同样的手续费起跑线开始。
        seed_quote = budget_per_pair * (1 - settings.initial_base_fraction)
        seed_base = (
            budget_per_pair * settings.initial_base_fraction * (1 - settings.fee_rate)
            / first.open
        )
        account.init_pair(pair, seed_quote, seed_base)
        # EMA 未形成时不建多头，防止在未知趋势下的第一根 K 线即开始接刀。
        bot = _build_bot(
            pair, first.open, seed_quote, account, clock, settings,
            buy_blocked=settings.trend_ema_period > 0,
            base_budget=seed_base,
        )
        bots[pair] = bot
        first_prices[pair] = first.open

    all_timestamps = sorted({c.ts for candles in aligned.values() for c in candles})
    by_timestamp = {
        pair: {c.ts: c for c in candles}
        for pair, candles in aligned.items()
    }
    last_close = dict(first_prices)
    portfolio_curve: list[float] = []

    for ts in all_timestamps:
        for pair in pairs:
            candle = by_timestamp[pair].get(ts)
            if candle is None:
                continue
            bot = bots[pair]
            clock.now = ts
            if settings.trend_ema_period > 0:
                buy_allowed = (
                    ema_samples[pair] >= settings.trend_ema_period
                    and last_close[pair] > (ema_values[pair] or math.inf)
                )
                _apply_trend_filter(bot, candle.open, account, buy_allowed)
            else:
                buy_allowed = True
            for price in _path(candle, settings.path_mode):
                if settings.downside_freeze:
                    if price < bot.lower:
                        downside_frozen[pair] = True
                    elif downside_frozen[pair] and price >= bot.lower:
                        downside_frozen[pair] = False
                    effective_buy_allowed = buy_allowed and not downside_frozen[pair]
                    _apply_trend_filter(bot, price, account, effective_buy_allowed)
                else:
                    effective_buy_allowed = buy_allowed
                if _should_recenter(
                    bot, price, settings, effective_buy_allowed, downside_frozen[pair],
                ):
                    bot = _recenter(
                        pair, bot, account, price, clock, settings,
                        buy_blocked=not effective_buy_allowed,
                    )
                    bots[pair] = bot
                # 刚刚重建的机器人也必须继承当前门控，避免同一根 K 线重新挂买单。
                if not effective_buy_allowed:
                    _apply_trend_filter(bot, price, account, False)
                fills = bot.step(price, account)
                for fill in fills:
                    if fill.get("stoploss"):
                        stoploss_counts[pair] += 1
                        stoploss_profits[pair] += fill["profit"]
            last_close[pair] = candle.close
            if settings.trend_ema_period > 0:
                ema_values[pair] = _next_ema(
                    ema_values[pair], candle.close, settings.trend_ema_period,
                )
                ema_samples[pair] += 1
            balance = account.get(pair)
            equity_curves[pair].append(balance["quote"] + balance["base"] * candle.close)
        portfolio_curve.append(sum(
            account.get(pair)["quote"] + account.get(pair)["base"] * last_close[pair]
            for pair in pairs
        ))

    pair_results = []
    for pair in pairs:
        bot = bots[pair]
        final_equity = account.get(pair)["quote"] + account.get(pair)["base"] * last_close[pair]
        buy_hold_equity = (
            budget_per_pair * (1 - settings.fee_rate)
            * last_close[pair] / first_prices[pair]
        )
        pair_results.append(PairResult(
            pair=pair,
            initial_equity=budget_per_pair,
            final_equity=final_equity,
            return_pct=(final_equity / budget_per_pair - 1) * 100,
            buy_hold_equity=buy_hold_equity,
            buy_hold_return_pct=(buy_hold_equity / budget_per_pair - 1) * 100,
            max_drawdown_pct=_max_drawdown_pct(equity_curves[pair]),
            realized_profit=bot.realized_profit,
            total_fees=bot.total_fees,
            trade_count=bot.trade_count,
            stoploss_count=stoploss_counts[pair],
            stoploss_profit=stoploss_profits[pair],
            candles=len(aligned[pair]),
        ))

    final_equity = portfolio_curve[-1]
    buy_hold_equity = sum(result.buy_hold_equity for result in pair_results)
    return BacktestResult(
        start_ts=start_ts,
        end_ts=end_ts,
        interval=interval,
        settings=settings,
        initial_equity=settings.total_quote_budget,
        final_equity=final_equity,
        return_pct=(final_equity / settings.total_quote_budget - 1) * 100,
        buy_hold_equity=buy_hold_equity,
        buy_hold_return_pct=(buy_hold_equity / settings.total_quote_budget - 1) * 100,
        max_drawdown_pct=_max_drawdown_pct(portfolio_curve),
        realized_profit=sum(result.realized_profit for result in pair_results),
        total_fees=sum(result.total_fees for result in pair_results),
        trade_count=sum(result.trade_count for result in pair_results),
        stoploss_count=sum(result.stoploss_count for result in pair_results),
        stoploss_profit=sum(result.stoploss_profit for result in pair_results),
        pairs=pair_results,
    )


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _print_result(result: BacktestResult) -> None:
    print(
        f"经典网格 K 线回测 | {_format_ts(result.start_ts)} 至 "
        f"{_format_ts(result.end_ts)} | {result.interval}"
    )
    print(f"路径假设: {result.settings.path_mode} | 初始资金: {result.initial_equity:.2f} USDT")
    print(
        f"参数: 区间倍率 {result.settings.range_scale:.2f}× | "
        f"自动重心 {'开' if result.settings.auto_recenter else '关'} | "
        f"重心模式 {result.settings.recenter_mode} | 限时止损 {result.settings.stoploss_hours:g}h | "
        f"EMA 门控 {result.settings.trend_ema_period or '关'} | "
        f"初始基础币 {result.settings.initial_base_fraction:.0%} | "
        f"下破冻结 {'开' if result.settings.downside_freeze else '关'} | "
        f"层数 {result.settings.grids or '默认'}"
    )
    print(
        f"策略: {result.final_equity:.2f} USDT ({result.return_pct:+.2f}%) | "
        f"买入持有: {result.buy_hold_equity:.2f} USDT ({result.buy_hold_return_pct:+.2f}%) | "
        f"超额: {result.return_pct - result.buy_hold_return_pct:+.2f} pct"
    )
    print(
        f"最大回撤: {result.max_drawdown_pct:.2f}% | 已实现利润: {result.realized_profit:.4f} U | "
        f"手续费: {result.total_fees:.4f} U | 成交: {result.trade_count} | "
        f"止损: {result.stoploss_count} ({result.stoploss_profit:+.4f} U)"
    )
    for pair in result.pairs:
        print(
            f"  {pair.pair:<10} 策略 {pair.return_pct:+7.2f}% | 持有 {pair.buy_hold_return_pct:+7.2f}% | "
            f"回撤 {pair.max_drawdown_pct:6.2f}% | 成交 {pair.trade_count:4d} | 止损 {pair.stoploss_count}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="经典现货网格的 Gate K 线级回测")
    parser.add_argument("--days", type=int, default=90, help="回测窗口天数（默认：90）")
    parser.add_argument("--interval", default="15m", choices=sorted(_INTERVAL_SECONDS), help="K 线周期")
    parser.add_argument("--pairs", default=",".join(config.PAIRS), help="逗号分隔的 USDT 交易对")
    parser.add_argument("--budget", type=float, default=config.TOTAL_QUOTE_BUDGET, help="初始总 USDT")
    parser.add_argument("--path", choices=("directional", "close"), default="directional", help="K 线内成交路径假设")
    parser.add_argument("--no-recenter", action="store_true", help="关闭价格越界后的自动重心")
    parser.add_argument("--stoploss-hours", type=float, default=config.STUCK_STOPLOSS_HOURS,
                        help="水下限时止损小时数；0 表示关闭（默认：配置值）")
    parser.add_argument("--range-scale", type=float, default=1.0,
                        help="各币对默认网格区间的倍率（默认：1.0）")
    parser.add_argument("--trend-ema-period", type=int, default=0,
                        help="慢 EMA 门控周期；0 表示关闭（默认：0）")
    parser.add_argument("--up-only-recenter", action="store_true",
                        help="仅在向上突破时自动重心；向下破位冻结网格")
    parser.add_argument("--initial-base-fraction", type=float, default=0.0,
                        help="首根 K 线用于买入基础币的资金比例（0 至 1，默认：0）")
    parser.add_argument("--grids", type=int,
                        help="统一覆盖各币对网格层数（至少 3，默认沿用配置）")
    parser.add_argument("--downside-freeze", action="store_true",
                        help="跌破网格下界时撤掉买单，重新回到区间才补挂")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    if (args.days <= 0 or args.budget <= 0 or args.range_scale <= 0
            or args.stoploss_hours < 0 or args.trend_ema_period < 0
            or not 0 <= args.initial_base_fraction <= 1
            or (args.grids is not None and args.grids < 3)):
        parser.error("资金与区间倍率必须为正数；止损小时数/EMA 周期不能为负；初始基础币比例应在 0 至 1")

    end_ts = int(time.time())
    start_ts = end_ts - args.days * 24 * 60 * 60
    pairs = tuple(pair.strip().upper() for pair in args.pairs.split(",") if pair.strip())
    settings = BacktestSettings(
        pairs=pairs, total_quote_budget=args.budget, path_mode=args.path,
        auto_recenter=not args.no_recenter, stoploss_hours=args.stoploss_hours,
        range_scale=args.range_scale, trend_ema_period=args.trend_ema_period,
        recenter_mode="up_only" if args.up_only_recenter else "all",
        initial_base_fraction=args.initial_base_fraction,
        grids=args.grids, downside_freeze=args.downside_freeze,
    )
    candles = {
        pair: fetch_gate_candles(pair, args.interval, start_ts, end_ts)
        for pair in pairs
    }
    result = run_classic_backtest(candles, args.interval, settings)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
