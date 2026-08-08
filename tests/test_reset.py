"""模拟盘仓位重置（Web 参数面板入口）的测试。"""

import threading

import pytest

from trading import engine as engine_module
from trading.store import Store


def _stub_store(calls):
    return type("StubStore", (), {
        "clear_bot_states": lambda self: calls.append("bot_states"),
        "clear_trades": lambda self: calls.append("trades"),
        "clear_equity_snapshots": lambda self: calls.append("equity"),
        "delete_meta": lambda self, key: calls.append(f"meta:{key}"),
    })()


def _make_grid_engine(mode="paper"):
    engine = engine_module.Engine.__new__(engine_module.Engine)
    engine.mode = mode
    engine._paused = threading.Event()
    engine._tick_lock = threading.Lock()
    engine._ready = threading.Event()
    engine._ready.set()
    engine._budget_override = None
    engine._initial_total = 500.0
    engine._initial_equity_persisted = True
    engine._initializing = False
    engine._init_error = "old"
    engine._next_init_attempt = 999.0
    engine._stopped = False
    engine._event = lambda *args, **kwargs: None
    return engine


def test_grid_reset_clears_state_and_reinitializes():
    calls = []
    engine = _make_grid_engine()
    engine.store = _stub_store(calls)

    status = engine.reset_paper(300.0)

    assert status == "initializing"
    assert calls == ["bot_states", "trades", "equity", "meta:initial_equity"]
    assert engine._budget_override == 300.0
    assert engine._initial_total == 0.0
    assert not engine._initial_equity_persisted
    assert not engine._ready.is_set()  # 引擎线程将重新建仓
    assert engine._paused.is_set()  # 重置后待命，需手动开始
    assert engine._init_error is None
    assert engine._next_init_attempt == 0.0


def test_grid_reset_rejects_live_mode():
    engine = _make_grid_engine(mode="live")
    engine.store = _stub_store([])
    with pytest.raises(RuntimeError, match="实盘"):
        engine.reset_paper(300.0)


@pytest.mark.parametrize("bad", [0, -1, 2_000_000, "abc", None, True])
def test_grid_reset_rejects_invalid_budget(bad):
    engine = _make_grid_engine()
    engine.store = _stub_store([])
    with pytest.raises((ValueError, TypeError)):
        engine.reset_paper(bad)


def test_grid_allocate_quotes_uses_override():
    engine = _make_grid_engine()
    engine.profile = type("P", (), {"dynamic_allocation": False})()
    engine.pairs = ["AAA_USDT", "BBB_USDT"]
    engine._budget_override = 100.0
    assert engine._allocate_quotes() == {"AAA_USDT": 50.0, "BBB_USDT": 50.0}


def test_predictive_reset_clears_positions():
    from trading.predictive_engine import PredictivePaperEngine

    engine = PredictivePaperEngine.__new__(PredictivePaperEngine)
    engine.pairs = ["AAA_USDT", "BBB_USDT"]
    engine.quote = 180.0
    engine.base = {"AAA_USDT": 1.5, "BBB_USDT": 2.0}
    engine.avg_cost = {"AAA_USDT": 10.0, "BBB_USDT": None}
    engine.realized_profit = {"AAA_USDT": 3.0, "BBB_USDT": -1.0}
    engine.trade_count = {"AAA_USDT": 5, "BBB_USDT": 2}
    engine.total_fees = 0.7
    engine.target = ("AAA_USDT",)
    engine.predictions = {"AAA_USDT": 0.01}
    engine.last_decision_candle_ts = 123
    engine.last_refit_candle_ts = 100
    engine._initial_total = 200.0
    engine._paused = threading.Event()
    engine._tick_lock = threading.Lock()
    engine._stopped = False
    engine._ready = threading.Event()
    engine._ready.set()
    events = []
    engine._event = lambda *args, **kwargs: events.append(args)
    saved = []
    engine._save_state = lambda: saved.append(True)
    calls = []
    engine.store = _stub_store(calls)

    status = engine.reset_paper(233.0)

    assert status == "paused"
    assert engine.quote == 233.0
    assert engine.base == {"AAA_USDT": 0.0, "BBB_USDT": 0.0}
    assert engine.avg_cost == {"AAA_USDT": None, "BBB_USDT": None}
    assert all(v == 0.0 for v in engine.realized_profit.values())
    assert all(v == 0 for v in engine.trade_count.values())
    assert engine.total_fees == 0.0
    assert engine.target == ()
    assert engine.predictions == {}
    assert engine.last_decision_candle_ts == 0
    assert engine._initial_total == 233.0
    assert calls == ["bot_states", "trades", "equity"]
    assert saved == [True]
    assert engine._paused.is_set()
    assert any(a[1] == "paper_reset" for a in events)


def test_store_clear_methods(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.record_trade("paper", "AAA_USDT", "buy", 1.0, 1.0, 1.0)
    store.record_equity(100.0, 0.0, {})
    store.save_bot_state("AAA_USDT", {"quote": 1})
    store.set_meta("initial_equity", "100")

    store.clear_trades()
    store.clear_equity_snapshots()
    store.clear_bot_states()
    store.delete_meta("initial_equity")

    assert store.recent_trades() == []
    assert store.equity_history() == []
    assert store.load_bot_states() == {}
    assert store.get_meta("initial_equity") is None
    store.close()
