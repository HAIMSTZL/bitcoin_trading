from types import SimpleNamespace

from trading.predictive import PredictiveSettings
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
