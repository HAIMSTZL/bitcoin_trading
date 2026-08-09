"""公共行情客户端与共享 ticker 缓存的纯逻辑测试。"""

import threading
import time
from types import SimpleNamespace

import pytest

from trading import config
from gate_api import GatePublicClient
from trading import engine as engine_module
from trading.grid import PaperAccount


def test_public_client_retries_transient_status(monkeypatch):
    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = "temporary"
            self.closed = False

        def json(self):
            return self._payload

        def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.trust_env = False
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response(503, {}) if self.calls == 1 else Response(200, [{"ok": True}])

        def close(self):
            pass

    client = GatePublicClient(retries=1, retry_backoff=0)
    session = Session()
    client.session = session
    monkeypatch.setattr("gate_api.client.time.sleep", lambda _: None)

    assert client.get("/spot/tickers") == [{"ok": True}]
    assert session.calls == 2


def test_ticker_cache_scopes_requests_to_pairs_and_reuses_each_price(monkeypatch):
    class Spot:
        def __init__(self):
            self.calls = []

        def list_tickers(self, pair=None):
            self.calls.append(pair)
            values = {"AAA_USDT": "1.5", "BBB_USDT": "2.5"}
            return [{"currency_pair": pair, "last": values[pair]}]

    spot = Spot()
    monkeypatch.setattr(engine_module, "_TICKER_CACHE", {"data": {}, "updated": {}})
    monkeypatch.setattr(engine_module.time, "time", lambda: 100.0)

    assert engine_module._fetch_tickers_cached(spot, ("AAA_USDT", "BBB_USDT")) == {
        "AAA_USDT": 1.5, "BBB_USDT": 2.5,
    }
    assert engine_module._fetch_tickers_cached(spot, ("AAA_USDT",)) == {"AAA_USDT": 1.5}
    assert spot.calls == ["AAA_USDT", "BBB_USDT"]
    assert engine_module._TICKER_TTL >= config.TICK_INTERVAL


def test_slow_pair_refresh_does_not_hold_cache_lock(monkeypatch):
    """网络阻塞只占用该币对后台任务，不能阻塞其他币对的缓存登记。"""
    class SlowSpot:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def list_tickers(self, pair):
            self.started.set()
            assert self.release.wait(2)
            return [{"currency_pair": pair, "last": "1"}]

    class FastSpot:
        def list_tickers(self, pair):
            return [{"currency_pair": pair, "last": "2"}]

    slow = SlowSpot()
    monkeypatch.setattr(engine_module, "_TICKER_CACHE", {
        "data": {}, "updated": {}, "failures": {}, "inflight": {},
    })
    _, slow_done = engine_module._schedule_ticker_refresh("AAA_USDT", slow)
    assert slow.started.wait(1)

    started = time.monotonic()
    _, fast_done = engine_module._schedule_ticker_refresh("BBB_USDT", FastSpot())
    assert time.monotonic() - started < 0.2
    assert fast_done and fast_done.wait(1)

    slow.release.set()
    assert slow_done and slow_done.wait(1)


def test_single_pair_failure_uses_recent_cached_price(monkeypatch):
    class FailingSpot:
        def list_tickers(self, pair):
            raise RuntimeError("temporary network failure")

    monkeypatch.setattr(engine_module, "_TICKER_CACHE", {
        "data": {"AAA_USDT": 1.5},
        "updated": {"AAA_USDT": time.time()},
        "failures": {}, "inflight": {},
    })
    monkeypatch.setattr(engine_module, "_TICKER_TTL", 0.0)
    prices = engine_module._fetch_tickers_cached(FailingSpot(), ("AAA_USDT",))
    assert prices == {"AAA_USDT": 1.5}


def test_background_bootstrap_can_opt_into_longer_initial_wait(monkeypatch):
    class DelayedSpot:
        def list_tickers(self, pair):
            time.sleep(0.03)
            return [{"currency_pair": pair, "last": "1.5"}]

    monkeypatch.setattr(engine_module, "_TICKER_CACHE", {
        "data": {}, "updated": {}, "failures": {}, "inflight": {},
    })

    assert engine_module._fetch_tickers_cached(
        DelayedSpot(), ("AAA_USDT",), initial_wait_sec=0.2,
    ) == {"AAA_USDT": 1.5}


def test_enqueue_failure_clears_inflight_event(monkeypatch):
    """worker 创建/投递失败不能把币对永久锁死在 inflight。"""
    monkeypatch.setattr(engine_module, "_TICKER_CACHE", {
        "data": {}, "updated": {}, "failures": {}, "inflight": {},
    })
    monkeypatch.setattr(
        engine_module, "_enqueue_ticker_refresh",
        lambda *args: (_ for _ in ()).throw(RuntimeError("worker unavailable")),
    )

    with pytest.raises(RuntimeError, match="worker unavailable"):
        engine_module._schedule_ticker_refresh("AAA_USDT", None)
    assert engine_module._TICKER_CACHE["inflight"] == {}


def test_grid_engine_initialization_can_complete_after_web_is_available():
    """构造函数不应再同步依赖 ticker；成功预热后才写入权益基准。"""
    engine = engine_module.Engine.__new__(engine_module.Engine)
    engine.profile = SimpleNamespace(pairs=("AAA_USDT",))
    engine.mode = "paper"
    engine.account = PaperAccount()
    engine.bots = {}
    engine.prices = {}
    engine.executor = None
    engine.pairs = ["AAA_USDT"]
    engine._init_atr = {}
    engine._ready = threading.Event()
    engine._initializing = True
    engine._init_error = "previous failure"
    engine._initial_equity_persisted = False
    engine._initial_total = 0.0
    writes = []
    engine.store = SimpleNamespace(set_meta=lambda key, value: writes.append((key, value)))
    engine._event = lambda *args, **kwargs: None

    def init_bots():
        engine.prices = {"AAA_USDT": 2.0}
        engine.account.init_pair("AAA_USDT", quote=7.0, base=1.5)

    engine._init_bots = init_bots
    engine._initialize()

    assert engine._ready.is_set()
    assert not engine._initializing
    assert engine._init_error is None
    assert engine._initial_total == 10.0
    assert writes == [("initial_equity", "10.0")]


def test_grid_state_uses_full_trade_ledger_after_slot_rotation(tmp_path):
    """当前 bots 不再包含已被轮换淘汰的币对，账户总账仍必须包含其损益。"""
    store = engine_module.Store(str(tmp_path / "rotation.db"))
    store.record_trade("paper", "RETIRED_USDT", "sell", 10.0, 1.0, 9.8, -1.0, 0.2)
    store.record_trade("paper", "CURRENT_USDT", "sell", 10.0, 1.0, 9.9, 0.3, 0.1)

    bot = SimpleNamespace(state=lambda price, account: {
        "equity": 299.0, "realized_profit": 0.3, "total_fees": 0.1,
        "trade_count": 1, "orders": [],
    }, start_price=10.0)
    engine = engine_module.Engine.__new__(engine_module.Engine)
    engine.mode = "paper"
    engine.bots = {"CURRENT_USDT": bot}
    engine.prices = {"CURRENT_USDT": 10.0}
    engine.account = object()
    engine._initial_total = 300.0
    engine.profile = SimpleNamespace(name="rotation", label="筛选轮换", use_signal_filter=True)
    engine._stopped = False
    engine._ready = threading.Event()
    engine._ready.set()
    engine._paused = threading.Event()
    engine._init_error = None
    engine._cb_global = False
    engine._cb_pairs = {}
    engine._api_outage = False
    engine._last_success = None
    engine.started_at = 0.0
    engine.last_tick = None
    engine.last_error = None
    engine.pairs = ["CURRENT_USDT"]
    engine.indicators = SimpleNamespace(get=lambda pair: {})
    engine.store = store

    state = engine.state()

    assert state["total_pnl"] == pytest.approx(-1.0)
    assert state["total_realized_profit"] == pytest.approx(-0.7)
    assert state["total_unrealized_profit"] == pytest.approx(-0.3)
    assert state["total_fees"] == pytest.approx(0.3)
    assert state["total_trade_count"] == 2
    store.close()
