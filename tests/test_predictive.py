import math

import numpy as np

from trading.backtest import Candle
from trading.predictive import (
    PredictiveSettings,
    RidgeReturnModel,
    _feature_matrix,
    _rebalance,
    _training_set,
    load_market_snapshot,
    save_market_snapshot,
)


def _candles(count: int, *, future_jump: float = 0.0) -> list[Candle]:
    rows = []
    for index in range(count):
        close = 100 * math.exp(index * 0.001)
        if index >= 90:
            close *= 1 + future_jump
        rows.append(Candle(
            index * 3600, close * 0.999, close * 1.003, close * 0.997, close,
            volume=1000 + index,
        ))
    return rows


def test_features_do_not_change_when_only_future_candles_change():
    plain = _feature_matrix(_candles(120))
    changed = _feature_matrix(_candles(120, future_jump=0.5))
    # 第 89 根的指标只能看见它自己和之前的 K 线，不可读取第 90 根后的跳涨。
    assert np.allclose(plain[89], changed[89], equal_nan=True)
    assert not np.allclose(plain[95], changed[95], equal_nan=True)


def test_training_set_excludes_labels_not_realized_at_decision_time():
    # 每个标签直接写成自身索引，以便断言 now=90、horizon=3 时不可能带入 >87 的标签。
    features = {"BTC_USDT": np.ones((100, 14)), "ETH_USDT": np.ones((100, 14))}
    labels = {
        "BTC_USDT": np.arange(100, dtype=float),
        "ETH_USDT": np.arange(100, dtype=float),
    }
    settings = PredictiveSettings(
        pairs=("BTC_USDT", "ETH_USDT"), horizon_bars=3, train_bars=80,
    )
    _, y = _training_set(features, labels, settings.pairs, 90, settings)
    assert y.max() == 87


def test_ridge_model_learns_simple_regularized_relationship():
    x = np.column_stack((np.linspace(-1, 1, 100), np.ones(100)))
    y = 0.01 + 0.02 * x[:, 0]
    model = RidgeReturnModel(alpha=0.01).fit(x, y)
    prediction = model.predict(np.array([[0.5, 1.0]]))[0]
    assert abs(prediction - 0.02) < 0.001


def test_rebalance_sells_before_buying_and_charges_both_sides():
    quote, base, fees, turnover, trades = _rebalance(
        0.0, {"BTC_USDT": 1.0, "ETH_USDT": 0.0}, ("ETH_USDT",),
        {"BTC_USDT": 100.0, "ETH_USDT": 100.0}, fee_rate=0.001,
        slippage_rate=0.0,
    )
    assert trades == 2
    assert fees > 0
    assert turnover > 199
    assert base["BTC_USDT"] == 0
    assert base["ETH_USDT"] > 0
    assert quote >= 0


def test_market_snapshot_round_trip(tmp_path):
    path = tmp_path / "snapshot.json"
    original = {"BTC_USDT": [Candle(1, 1, 2, 0.5, 1.5, volume=7)]}
    save_market_snapshot(path, original)
    assert load_market_snapshot(path, ("BTC_USDT",)) == original
