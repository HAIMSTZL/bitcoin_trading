from types import SimpleNamespace

from trading import config
from trading.backtest import Candle
from trading.doge_trend import DogeTrendSettings
from trading.doge_trend_engine import DogeTrendPaperEngine
from trading.profiles import PROFILES


def _paper_engine() -> tuple[DogeTrendPaperEngine, list[dict]]:
    """不联网的最小模拟盘对象，用于资金和信号时序测试。"""
    engine = DogeTrendPaperEngine.__new__(DogeTrendPaperEngine)
    engine.pair = "DOGE_USDT"
    engine.asset = "DOGE"
    engine.pairs = [engine.pair]
    engine.settings = DogeTrendSettings(total_quote_budget=100, fee_rate=0.001, slippage_bps=10)
    engine.prices = {engine.pair: 1.0}
    engine.quote = 100.0
    engine.base = engine.average_cost = engine.position_cost = 0.0
    engine.held_bars = 0
    engine.waiting_for_recovery = False
    engine.pending_target = None
    engine.rsi_rearmed = True
    engine.last_stop_candle_ts = 0
    engine.realized_profit = engine.total_fees = engine.total_slippage = engine.total_turnover = 0.0
    engine.trade_count = engine.entry_count = engine.add_count = engine.exit_count = 0
    engine.take_profit_count = engine.stop_count = engine.time_exit_count = 0
    engine.closed_trade_count = engine.winning_trade_count = 0
    engine.last_processed_candle_ts = 0
    engine.last_signal_candle_ts = 0
    engine.latest_rsi = engine.latest_ema = None
    engine._decision_pause_reason = None
    engine._price_observed_at = 123.0
    engine.candles = []
    fills: list[dict] = []
    engine._record_fill = fills.append
    engine._event = lambda *args, **kwargs: None
    return engine, fills


def test_btc_eth_dip_profiles_are_independent_single_coin_paper_profiles():
    for name, pair in (("btc_dip", "BTC_USDT"), ("eth_dip", "ETH_USDT")):
        profile = PROFILES[name]
        assert profile.kind == "doge_trend"
        assert profile.pairs == (pair,)
        assert profile.db_path.endswith(f"trading_{name}.db")


def test_doge_trend_staged_buy_then_full_exit_charges_costs():
    engine, fills = _paper_engine()
    assert engine._buy_to_target(0.5, 1.0, {"reason": "oversold_entry"})
    assert 0 < engine.base < 50
    assert 49 < engine.quote < 51
    assert engine.waiting_for_recovery is True
    assert engine.entry_count == 1

    assert engine._buy_to_target(1.0, 1.0, {"reason": "trend_confirmation"})
    assert engine.add_count == 1
    assert engine.waiting_for_recovery is False
    assert engine.quote >= 0

    # 价格上涨后全数退出；卖出价仍会扣除不利滑点与手续费。
    assert engine._sell_all(1.2, "take_profit")
    assert engine.base == 0
    assert engine.quote > 100
    assert engine.realized_profit > 0
    assert engine.take_profit_count == 1
    assert [fill["side"] for fill in fills] == ["buy", "buy", "sell"]


def test_doge_trend_only_processes_a_new_closed_candle_once():
    engine, _ = _paper_engine()
    engine.base = 10.0
    engine.last_processed_candle_ts = 10 * 3600
    engine.candles = [Candle(12 * 3600, 1, 1, 1, 1)]

    assert engine._observe_new_candle() is True
    assert engine.held_bars == 2
    assert engine._observe_new_candle() is False
    assert engine.held_bars == 2


def test_doge_trend_signals_half_position_on_closed_rsi_oversold():
    engine, events = _paper_engine()
    # 连续下跌使 20 期 RSI 为 0；最后一根已收盘 K 线才用于触发信号。
    closes = [2.0 - index * 0.02 for index in range(23)]
    engine.candles = [Candle(index * 3600, close, close, close, close)
                      for index, close in enumerate(closes)]
    engine._event = lambda *args, **kwargs: events.append((args, kwargs))

    engine._schedule_signal()

    assert engine.pending_target == 0.5
    assert engine.last_signal_candle_ts == engine.candles[-1].ts
    assert any(args[1] == "doge_trend_signal" for args, _ in events)


def test_doge_trend_confirms_to_full_position_only_after_ema_cross_and_profit():
    engine, _ = _paper_engine()
    # 前一根在 EMA 下方，末根上穿；且末根收盘高于实际成本。
    closes = [1.0] * 20 + [0.9, 1.1]
    engine.candles = [Candle(index * 3600, close, close, close, close)
                      for index, close in enumerate(closes)]
    engine.base = 50.0
    engine.average_cost = engine.position_cost = 1.0
    engine.waiting_for_recovery = True

    engine._schedule_signal()

    assert engine.pending_target == 1.0


def test_doge_trend_engine_waits_for_rsi_rearm_after_stop():
    engine, _ = _paper_engine()
    engine.rsi_rearmed = False
    engine.last_stop_candle_ts = 0
    closes = [2.0 - index * 0.02 for index in range(23)]
    engine.candles = [Candle(index * 3600, close, close, close, close)
                      for index, close in enumerate(closes)]

    engine._schedule_signal()

    assert engine.pending_target is None
    assert engine.rsi_rearmed is False


def test_doge_trend_constructor_is_nonblocking_and_paper_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOGE_TREND_CACHE_PATH", str(tmp_path / "missing-cache.json"))
    profile = SimpleNamespace(
        kind="doge_trend", pairs=("DOGE_USDT",), db_path=str(tmp_path / "paper.db"),
        name="doge_trend", label="test",
    )
    engine = DogeTrendPaperEngine(profile)
    try:
        assert engine.run_status == "initializing"
        assert engine.prices == {"DOGE_USDT": 0.0}
        assert engine.candles == []
        assert engine.state()["strategy_kind"] == "doge_trend"
    finally:
        engine.stop()


def test_btc_dip_constructor_uses_btc_and_its_own_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    profile = SimpleNamespace(
        kind="doge_trend", pairs=("BTC_USDT",), db_path=str(tmp_path / "btc.db"),
        name="btc_dip", label="BTC 低吸先锋",
    )
    engine = DogeTrendPaperEngine(profile)
    try:
        assert engine.pair == "BTC_USDT"
        assert engine.asset == "BTC"
        assert engine.prices == {"BTC_USDT": 0.0}
        assert engine._cache_path == tmp_path / "btc_dip_1h_cache.json"
    finally:
        engine.stop()


def test_doge_trend_reset_aligns_with_latest_preheated_candle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOGE_TREND_CACHE_PATH", str(tmp_path / "missing-cache.json"))
    profile = SimpleNamespace(
        kind="doge_trend", pairs=("DOGE_USDT",), db_path=str(tmp_path / "paper.db"),
        name="doge_trend", label="test",
    )
    engine = DogeTrendPaperEngine(profile)
    try:
        engine.candles = [Candle(123 * 3600, 1, 1, 1, 1)]
        engine.reset_paper(200)
        assert engine.last_processed_candle_ts == 123 * 3600
        assert engine.last_signal_candle_ts == 123 * 3600
        assert engine.quote == 200
    finally:
        engine.stop()
