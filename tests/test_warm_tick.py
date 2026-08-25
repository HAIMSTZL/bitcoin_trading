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
    engine._fetch_prices = lambda initial_wait_sec=1.0, allow_partial=False: waits.append(initial_wait_sec) or {}
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


def test_grid_tick_publishes_actual_flow_decision():
    """流程图只能消费本次 tick 的真实撮合结果，不能由前端定时循环伪造。"""
    class Bot:
        lower, upper = 1.0, 2.0

        def __init__(self):
            self.fills = [{
                "pair": "AAA_USDT", "side": "buy", "price": 1.5, "profit": 0.0,
            }]

        def step(self, price, account, record):
            return list(self.fills)

    engine = engine_module.Engine.__new__(engine_module.Engine)
    bot = Bot()
    engine._warm_next_tick = False
    engine._fetch_prices = lambda initial_wait_sec=1.0, allow_partial=False: {"AAA_USDT": 1.5, "BTC_USDT": 1.0}
    engine._check_circuit_breaker = lambda: None
    engine._update_indicators = lambda: None
    engine._maybe_rebalance = lambda: None
    engine._maybe_fill_slot = lambda: None
    engine._save_bot_states = lambda: None
    engine._maybe_health_check = lambda: None
    engine._record_fill = lambda fill: None
    engine._cb_global = False
    engine._cb_pairs = {}
    engine.bots = {"AAA_USDT": bot}
    engine.prices = {}
    engine.account = object()
    engine._last_snapshot = time.time()
    engine._decision_seq = 0
    engine._last_decision = None

    engine._tick_body()

    assert engine._last_decision["id"] == 1
    assert engine._last_decision["kind"] == "fill"
    assert engine._last_decision["checked_pairs"] == 1
    assert engine._last_decision["fills"] == [{
        "pair": "AAA_USDT", "side": "buy", "price": 1.5, "profit": 0.0,
    }]

    bot.fills = []
    engine._tick_body()
    assert engine._last_decision["id"] == 2
    assert engine._last_decision["kind"] == "wait"


def test_grid_tick_skips_only_the_missing_altcoin_ticker():
    """局部行情缺失不得把整个网格组升级成 tick_error 或停止撮合。"""
    class Bot:
        lower, upper = 1.0, 2.0

        def __init__(self):
            self.calls = 0

        def step(self, price, account, record):
            self.calls += 1
            return []

    engine = engine_module.Engine.__new__(engine_module.Engine)
    aaa, bbb = Bot(), Bot()
    events = []
    engine._warm_next_tick = False
    engine._fetch_prices = lambda initial_wait_sec=1.0, allow_partial=False: {
        "BTC_USDT": 100.0, "AAA_USDT": 1.5,
    }
    engine._check_circuit_breaker = lambda: None
    engine._update_indicators = lambda: None
    engine._maybe_rebalance = lambda: None
    engine._maybe_fill_slot = lambda: None
    engine._save_bot_states = lambda: None
    engine._maybe_health_check = lambda: None
    engine._record_fill = lambda fill: None
    engine._event = lambda *args, **kwargs: events.append((args, kwargs))
    engine._cb_global = False
    engine._cb_pairs = {}
    engine.pairs = ["AAA_USDT", "BBB_USDT"]
    engine.bots = {"AAA_USDT": aaa, "BBB_USDT": bbb}
    engine.prices = {}
    engine.account = object()
    engine._last_snapshot = time.time()
    engine._decision_seq = 0
    engine._last_decision = None
    engine._price_partial_missing = ()
    engine._price_partial_since = None
    engine._last_price_partial_event = 0.0
    engine.last_error = None

    assert engine._tick_body() is True
    assert aaa.calls == 1
    assert bbb.calls == 0
    assert engine._last_decision["skipped_pairs"] == ["BBB_USDT"]
    assert engine._price_partial_missing == ("BBB_USDT",)
    assert any(args[1] == "market_data_partial" for args, _ in events)


def test_grid_tick_pauses_safely_when_btc_risk_reference_is_missing():
    """BTC 缺失时不能绕过全局熔断基准，即使其他币对有报价也不撮合。"""
    class Bot:
        lower, upper = 1.0, 2.0

        def __init__(self):
            self.calls = 0

        def step(self, price, account, record):
            self.calls += 1
            return []

    engine = engine_module.Engine.__new__(engine_module.Engine)
    bot = Bot()
    engine._warm_next_tick = False
    engine._fetch_prices = lambda initial_wait_sec=1.0, allow_partial=False: {"AAA_USDT": 1.5}
    engine._check_circuit_breaker = lambda: (_ for _ in ()).throw(AssertionError("BTC missing"))
    engine._update_indicators = lambda: None
    engine._maybe_rebalance = lambda: None
    engine._maybe_fill_slot = lambda: None
    engine._save_bot_states = lambda: None
    engine._maybe_health_check = lambda: None
    engine._event = lambda *args, **kwargs: None
    engine._cb_global = False
    engine._cb_pairs = {}
    engine.pairs = ["AAA_USDT"]
    engine.bots = {"AAA_USDT": bot}
    engine.prices = {}
    engine.account = object()
    engine._last_snapshot = time.time()
    engine._decision_seq = 0
    engine._last_decision = None
    engine._price_partial_missing = ()
    engine._price_partial_since = None
    engine._last_price_partial_event = 0.0
    engine.last_error = None

    assert engine._tick_body() is False
    assert bot.calls == 0
    assert engine.last_error == "ticker 无可用交易价格；已暂停本轮网格撮合"


def test_predictive_resume_warms_next_tick(monkeypatch):
    engine = predictive_module.PredictivePaperEngine.__new__(
        predictive_module.PredictivePaperEngine)
    engine._stopped = False
    engine._paused = threading.Event()
    engine._paused.set()
    engine._warm_next_tick = False
    engine._price_partial_missing = ()
    engine._price_partial_since = None
    engine._last_price_partial_event = 0.0
    engine.last_error = None
    engine._ready = threading.Event()
    engine._ready.set()
    engine._event = lambda *args, **kwargs: None

    engine.resume()
    assert engine._warm_next_tick is True

    waits = []
    def fake_fetch(spot, pairs, *, initial_wait_sec=engine_module._TICKER_INITIAL_WAIT_SEC,
                   allow_partial=False):
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
    assert waits == [predictive_module._PREDICTIVE_TICKER_WARM_WAIT_SEC,
                     engine_module._TICKER_INITIAL_WAIT_SEC]
