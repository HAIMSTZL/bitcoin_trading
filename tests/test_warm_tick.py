"""恢复/重启交易后首个 tick 使用预热预算等行情的测试。"""

import threading
import time

from trading import engine as engine_module
from trading import predictive_engine as predictive_module


def test_grid_resume_warms_next_tick():
    engine = engine_module.Engine.__new__(engine_module.Engine)
    engine._stopped = False
    engine._paused = threading.Event()
    engine._paused.set()
    engine._warm_next_tick = False
    engine._ready = threading.Event()
    engine._ready.set()
    engine._event = lambda *args, **kwargs: None

    engine.resume()
    assert engine._warm_next_tick is True

    # 首个 tick 用预热预算；之后回到运行时预算
    waits = []
    engine._fetch_prices = lambda initial_wait_sec=1.0: waits.append(initial_wait_sec) or {}
    engine._check_circuit_breaker = lambda: None
    engine._update_indicators = lambda: None
    engine._maybe_rebalance = lambda: None
    engine._maybe_fill_slot = lambda: None
    engine._save_bot_states = lambda: None
    engine._maybe_health_check = lambda: None
    engine._cb_global = False
    engine._cb_pairs = {}
    engine.bots = {}
    engine.prices = {}
    engine._last_snapshot = time.time()  # 跳过权益快照分支

    engine._tick_body()
    engine._tick_body()
    assert waits == [engine_module._TICKER_BOOTSTRAP_WAIT_SEC,
                     engine_module._TICKER_INITIAL_WAIT_SEC]


def test_predictive_resume_warms_next_tick(monkeypatch):
    engine = predictive_module.PredictivePaperEngine.__new__(
        predictive_module.PredictivePaperEngine)
    engine._stopped = False
    engine._paused = threading.Event()
    engine._paused.set()
    engine._warm_next_tick = False
    engine._ready = threading.Event()
    engine._ready.set()
    engine._event = lambda *args, **kwargs: None

    engine.resume()
    assert engine._warm_next_tick is True

    waits = []
    def fake_fetch(spot, pairs, *, initial_wait_sec=engine_module._TICKER_INITIAL_WAIT_SEC):
        waits.append(initial_wait_sec)
        return {"AAA_USDT": 1.5}
    monkeypatch.setattr(predictive_module, "_fetch_tickers_cached", fake_fetch)

    engine.pairs = ["AAA_USDT"]
    engine._refresh_history = lambda: None
    engine._maybe_decide = lambda: None
    engine._save_state = lambda: None
    engine._maybe_health_check = lambda: None
    engine._last_snapshot = time.time()
    engine.base = {"AAA_USDT": 0.0}
    engine.realized_profit = {"AAA_USDT": 0.0}

    engine.tick()
    engine.tick()
    assert waits == [engine_module._TICKER_BOOTSTRAP_WAIT_SEC,
                     engine_module._TICKER_INITIAL_WAIT_SEC]
