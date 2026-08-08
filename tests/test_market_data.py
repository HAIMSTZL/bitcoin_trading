"""公共行情客户端与共享 ticker 缓存的纯逻辑测试。"""

from gate_api import GatePublicClient
from trading import engine as engine_module


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
