"""DOGE staged trend-recovery 策略的无前视与仓位控制测试。"""

import pytest

from trading.backtest import Candle
from trading.doge_trend import (
    DogeTrendSettings,
    run_doge_trend_backtest,
    run_doge_walk_forward,
)


def test_oversold_entry_stages_then_ema_recovery_adds_to_full_position():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 100, 100),
        Candle(120, 100, 100, 90, 90),  # RSI(2) 超卖，收盘后才生成试探仓位信号
        Candle(180, 90, 95, 90, 95),    # 下一根开盘买 50%，收盘上穿 EMA(2)
        Candle(240, 95, 96, 94, 95),    # 再下一根开盘加至满仓
    ]
    result = run_doge_trend_backtest(
        candles,
        DogeTrendSettings(
            total_quote_budget=100,
            fee_rate=0,
            slippage_bps=0,
            rsi_period=2,
            rsi_threshold=30,
            initial_fraction=0.5,
            confirmation_ema_period=2,
            take_profit_pct=0.2,
            stop_loss_pct=0.2,
            max_hold_bars=20,
        ),
    )
    assert result.entry_count == 1
    assert result.add_count == 1
    assert result.trade_count == 2


def test_stop_loss_exits_at_the_next_available_ohlc_price():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 100, 100),
        Candle(120, 100, 100, 90, 90),
        Candle(180, 90, 91, 80, 82),  # 试探仓位开仓后同根触及 10% 止损
    ]
    result = run_doge_trend_backtest(
        candles,
        DogeTrendSettings(
            total_quote_budget=100,
            fee_rate=0,
            slippage_bps=0,
            rsi_period=2,
            rsi_threshold=30,
            initial_fraction=0.5,
            confirmation_ema_period=2,
            take_profit_pct=0.5,
            stop_loss_pct=0.1,
            max_hold_bars=20,
        ),
    )
    assert result.entry_count == 1
    assert result.stop_count == 1
    assert result.exit_count == 1


def test_stop_loss_requires_rsi_rearm_before_another_oversold_entry():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 100, 100),
        Candle(120, 100, 100, 90, 90),  # 产生超卖信号
        Candle(180, 90, 91, 80, 82),    # 开仓后止损，收盘 RSI 仍低
        Candle(240, 82, 83, 75, 78),    # RSI 继续低，不能再次产生试探信号
        Candle(300, 78, 79, 70, 72),
    ]
    result = run_doge_trend_backtest(
        candles,
        DogeTrendSettings(
            total_quote_budget=100,
            fee_rate=0,
            slippage_bps=0,
            rsi_period=2,
            rsi_threshold=30,
            confirmation_ema_period=2,
            take_profit_pct=0.5,
            stop_loss_pct=0.1,
            max_hold_bars=20,
        ),
    )
    assert result.stop_count == 1
    assert result.entry_count == 1


def test_strategy_rejects_non_doge_pair_and_walk_forward_resets_capital():
    candles = [Candle(index * 60, 1, 1, 1, 1) for index in range(10)]
    with pytest.raises(ValueError, match="DOGE_USDT"):
        run_doge_trend_backtest(candles, DogeTrendSettings(pair="BTC_USDT"))

    report = run_doge_walk_forward(
        candles,
        DogeTrendSettings(rsi_period=2, confirmation_ema_period=2),
        development_fraction=0.6,
    )
    assert report.development.initial_equity == report.validation.initial_equity
    assert report.development.end_ts < report.validation.start_ts
