from types import SimpleNamespace

from trading import config
from trading.backtest import Candle
from trading.predictive import PredictiveSettings, save_market_snapshot
from trading.predictive_engine import PredictivePaperEngine
from trading.profiles import PROFILES


def _paper_engine_for_rebalance() -> tuple[PredictivePaperEngine, list[dict]]:
    """构造不联网的最小纸盘对象，仅测试资金与成交计算。"""
    engine = PredictivePaperEngine.__new__(PredictivePaperEngine)
    engine.pairs = ["AAA_USDT", "BBB_USDT"]
    engine.settings = PredictiveSettings(
        pairs=("AAA_USDT", "BBB_USDT"), total_quote_budget=100,
        fee_rate=0.001, slippage_bps=10, max_positions=2,
    )
    engine.prices = {"AAA_USDT": 100.0, "BBB_USDT": 100.0}
    engine.quote = 100.0
    engine.base = {"AAA_USDT": 0.0, "BBB_USDT": 0.0}
    engine.avg_cost = {"AAA_USDT": None, "BBB_USDT": None}
    engine.realized_profit = {"AAA_USDT": 0.0, "BBB_USDT": 0.0}
    engine.trade_count = {"AAA_USDT": 0, "BBB_USDT": 0}
    engine.total_fees = 0.0
    engine.target = ()
    fills: list[dict] = []
    engine._record_fill = fills.append
    engine._event = lambda *args, **kwargs: None
    return engine, fills


def test_predictive_profile_is_separate_and_paper_specific():
    profile = PROFILES["predictive"]
    assert profile.kind == "predictive"
    assert len(profile.pairs) == 10
    assert "BTC_USDT" in profile.pairs


def test_predictive_rebalance_charges_costs_and_can_return_to_usdt():
    engine, fills = _paper_engine_for_rebalance()
    engine._rebalance(("AAA_USDT",))
    assert [fill["side"] for fill in fills] == ["buy"]
    assert engine.base["AAA_USDT"] > 0
    assert engine.quote >= 0
    assert engine.total_fees > 0

    fills.clear()
    engine._rebalance(())
    assert [fill["side"] for fill in fills] == ["sell"]
    assert engine.base["AAA_USDT"] == 0
    # 双边费用/滑点后，回到现金的权益应低于 100，且不会产生负现金。
    assert 0 < engine.quote < 100
    assert engine.quote >= 0


def test_predictive_rebalance_records_signal_and_ticker_audit_data():
    engine, fills = _paper_engine_for_rebalance()
    audit = {
        "signal_candle_ts": 123,
        "decision_ts": 124.5,
        "ticker_observed_at": 124.4,
        "price_source": "live_ticker_after_closed_candle",
    }
    engine._rebalance(("AAA_USDT",), audit)
    assert fills[0]["market_mid"] == 100.0
    assert {key: fills[0][key] for key in audit} == audit


def test_predictive_engine_constructor_is_nonblocking_and_keyless(tmp_path, monkeypatch):
    """构造阶段只打开本地状态；网络预热必须留给后台线程。"""
    monkeypatch.setattr(config, "PREDICTIVE_CACHE_PATH", str(tmp_path / "missing-cache.json"))
    profile = SimpleNamespace(
        kind="predictive", pairs=("AAA_USDT", "BBB_USDT"),
        db_path=str(tmp_path / "paper.db"), name="predictive", label="test",
    )
    engine = PredictivePaperEngine(profile)
    try:
        assert engine.run_status == "initializing"
        assert engine.prices == {"AAA_USDT": 0.0, "BBB_USDT": 0.0}
        assert engine.candles == {}
    finally:
        engine.stop()


def test_predictive_cache_is_loaded_without_network(tmp_path):
    path = tmp_path / "predictive.json"
    pairs = ("AAA_USDT", "BBB_USDT")
    candles = {
        pair: [Candle(i * 3600, 1, 1, 1, 1) for i in range(200)]
        for pair in pairs
    }
    save_market_snapshot(path, candles)
    engine = PredictivePaperEngine.__new__(PredictivePaperEngine)
    engine.pairs = list(pairs)
    engine.settings = PredictiveSettings(pairs=pairs, train_bars=72, horizon_bars=2)
    engine._cache_path = path
    engine.candles = {}
    events = []
    engine._event = lambda *args, **kwargs: events.append((args, kwargs))

    engine._load_cached_history()

    assert len(engine.candles["AAA_USDT"]) == 200
    assert any(args[1] == "predictive_cache" for args, _ in events)
