"""经典网格 K 线级回测的纯逻辑测试。"""

from trading.backtest import (
    BacktestSettings,
    Candle,
    _max_drawdown_pct,
    parse_gate_candles,
    run_classic_backtest,
)


def test_parse_gate_candles_sorts_deduplicates_and_drops_unfinished():
    rows = [
        ["2", "0", "11", "12", "10", "11", "0", "true"],
        ["1", "0", "10", "11", "9", "10", "0", "true"],
        ["2", "0", "12", "13", "11", "12", "0", "true"],
        ["3", "0", "13", "14", "12", "13", "0", "false"],
    ]
    candles = parse_gate_candles(rows)
    assert [c.ts for c in candles] == [1, 2]
    assert candles[1].close == 12


def test_max_drawdown_uses_running_equity_peak():
    assert _max_drawdown_pct([100, 120, 90, 108]) == 25.0


def test_backtest_uses_common_window_and_generates_round_trip():
    # 价格先跌过一档再涨回，方向性路径可以完成一次低买高卖。
    btc = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 101, 98, 101),
        Candle(120, 101, 101, 101, 101),
    ]
    eth = [
        Candle(60, 100, 101, 98, 101),
        Candle(120, 101, 101, 101, 101),
        Candle(180, 101, 101, 101, 101),
    ]
    result = run_classic_backtest(
        {"BTC_USDT": btc, "ETH_USDT": eth}, "1m",
        BacktestSettings(
            pairs=("BTC_USDT", "ETH_USDT"), total_quote_budget=100,
            fee_rate=0.0, auto_recenter=False, path_mode="directional",
        ),
    )
    assert result.start_ts == 60
    assert result.end_ts == 120
    assert result.trade_count > 0
    assert result.final_equity > result.initial_equity


def test_ema_filter_waits_and_blocks_falling_market_buys():
    """慢 EMA 未形成或价格低于它时，候选策略不应在下跌中建立多头。"""
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 94, 94),
        Candle(120, 94, 94, 90, 90),
        Candle(180, 90, 90, 86, 86),
    ]
    plain = run_classic_backtest(
        {"T_USDT": candles}, "1m",
        BacktestSettings(pairs=("T_USDT",), total_quote_budget=100, auto_recenter=False),
    )
    filtered = run_classic_backtest(
        {"T_USDT": candles}, "1m",
        BacktestSettings(
            pairs=("T_USDT",), total_quote_budget=100, auto_recenter=False,
            trend_ema_period=2,
        ),
    )
    assert plain.trade_count > 0
    assert filtered.trade_count == 0
    assert filtered.final_equity == filtered.initial_equity


def test_initial_base_fraction_creates_two_sided_grid_inventory():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 104, 96, 100),
    ]
    result = run_classic_backtest(
        {"T_USDT": candles}, "1m",
        BacktestSettings(
            pairs=("T_USDT",), total_quote_budget=100, fee_rate=0,
            auto_recenter=False, initial_base_fraction=0.5,
        ),
    )
    # 价格上下穿越时，初始库存允许先卖后买；纯 USDT 起步不能产生这类卖单。
    assert result.trade_count > 0


def test_downside_freeze_cancels_buys_after_lower_range_break():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 94, 94),
    ]
    unprotected = run_classic_backtest(
        {"T_USDT": candles}, "1m",
        BacktestSettings(pairs=("T_USDT",), total_quote_budget=100, auto_recenter=False),
    )
    protected = run_classic_backtest(
        {"T_USDT": candles}, "1m",
        BacktestSettings(
            pairs=("T_USDT",), total_quote_budget=100, auto_recenter=False,
            downside_freeze=True,
        ),
    )
    assert unprotected.trade_count > 0
    assert protected.trade_count == 0
