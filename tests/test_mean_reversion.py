from trading.backtest import Candle
from trading.mean_reversion import MeanReversionSettings, run_mean_reversion_backtest


def test_rsi_signal_enters_on_next_open_and_takes_profit():
    candles = [
        Candle(0, 100, 100, 100, 100),
        Candle(60, 100, 100, 90, 90),  # RSI(1) = 0，收盘后产生信号
        Candle(120, 95, 97, 94, 96),  # 下一根开盘买入，随后触及 1% 止盈
    ]
    result = run_mean_reversion_backtest(
        {"T_USDT": candles},
        MeanReversionSettings(
            pairs=("T_USDT",), total_quote_budget=100, fee_rate=0,
            rsi_period=1, rsi_threshold=30, take_profit_pct=0.01,
            stop_loss_pct=0.05, max_hold_bars=10,
        ),
    )
    assert result.trade_count == 2
    assert result.return_pct > 0


def test_rsi_strategy_rejects_invalid_parameters():
    try:
        run_mean_reversion_backtest(
            {"T_USDT": [Candle(0, 1, 1, 1, 1)]},
            MeanReversionSettings(pairs=("T_USDT",), rsi_period=0),
        )
    except ValueError as error:
        assert "RSI" in str(error)
    else:
        raise AssertionError("应拒绝非法 RSI 周期")
