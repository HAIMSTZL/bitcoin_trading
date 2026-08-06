"""现货（Spot）只读接口封装。"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class SpotAPI:
    def __init__(self, client: GateClient):
        self._c = client

    # ---- 公共行情 ----
    def list_currencies(self) -> Any:
        """查询所有币种信息。"""
        return self._c.get("/spot/currencies")

    def get_currency(self, currency: str) -> Any:
        """查询单个币种信息。"""
        return self._c.get(f"/spot/currencies/{currency}")

    def list_currency_pairs(self) -> Any:
        """查询所有交易对规则。"""
        return self._c.get("/spot/currency_pairs")

    def get_currency_pair(self, pair: str) -> Any:
        """查询单个交易对规则，如 BTC_USDT。"""
        return self._c.get(f"/spot/currency_pairs/{pair}")

    def list_tickers(self, currency_pair: Optional[str] = None) -> Any:
        """查询行情 ticker。"""
        return self._c.get("/spot/tickers", {"currency_pair": currency_pair})

    def list_candlesticks(
        self,
        currency_pair: str,
        interval: str = "5m",
        limit: int = 100,
    ) -> Any:
        """查询 K 线。返回 [ts, 成交额, 收盘价, 最高价, 最低价, 开盘价, 成交量, 是否完结]。"""
        return self._c.get(
            "/spot/candlesticks",
            {"currency_pair": currency_pair, "interval": interval, "limit": limit},
        )

    def list_order_book(self, currency_pair: str, limit: int = 10) -> Any:
        """查询盘口深度（bids/asks）。"""
        return self._c.get(
            "/spot/order_book", {"currency_pair": currency_pair, "limit": limit}
        )

    def list_public_trades(self, currency_pair: str, limit: int = 100) -> Any:
        """查询市场最近成交（含主动方向 side）。"""
        return self._c.get(
            "/spot/trades", {"currency_pair": currency_pair, "limit": limit}
        )

    # ---- 私有（只读）----
    def list_accounts(self) -> Any:
        """查询现货账户资产。"""
        return self._c.get("/spot/accounts")

    def list_account_book(
        self,
        currency: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> Any:
        """查询现货账户账单（流水）。"""
        return self._c.get(
            "/spot/account_book",
            {"currency": currency, "limit": limit, "page": page},
        )

    def list_open_orders(self, page: int = 1, limit: int = 100) -> Any:
        """查询所有未成交挂单（按交易对汇总）。"""
        return self._c.get("/spot/open_orders", {"page": page, "limit": limit})

    def list_orders(
        self,
        currency_pair: str,
        status: str = "open",
        limit: int = 100,
        page: int = 1,
    ) -> Any:
        """查询订单列表。status: open=未成交, finished=已结束。"""
        return self._c.get(
            "/spot/orders",
            {
                "currency_pair": currency_pair,
                "status": status,
                "limit": limit,
                "page": page,
            },
        )

    def get_order(self, order_id: str, currency_pair: str) -> Any:
        """查询单个订单。"""
        return self._c.get(
            f"/spot/orders/{order_id}", {"currency_pair": currency_pair}
        )

    def list_my_trades(
        self,
        currency_pair: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> Any:
        """查询个人成交历史。"""
        return self._c.get(
            "/spot/my_trades",
            {"currency_pair": currency_pair, "limit": limit, "page": page},
        )

    def get_fee(self, currency_pair: Optional[str] = None) -> Any:
        """查询交易手续费率。"""
        return self._c.get("/spot/fee", {"currency_pair": currency_pair})

    def create_order(
        self,
        currency_pair: str,
        side: str,
        amount: str,
        order_type: str = "market",
        time_in_force: str = "ioc",
        price: Optional[str] = None,
    ) -> Any:
        """下单（写操作，谨慎调用）。

        :param side: buy/sell
        :param amount: 市价买单为 USDT 金额，卖单为基础币数量；限价单为基础币数量
        :param order_type: market/limit
        """
        body = {
            "currency_pair": currency_pair,
            "side": side,
            "type": order_type,
            "amount": amount,
            "time_in_force": time_in_force,
            "account": "spot",  # 显式现货账户，避免统一账户默认路由歧义
        }
        if price is not None:
            body["price"] = price
        return self._c.request("POST", "/spot/orders", body=body)

    def list_price_orders(self, status: str = "open", limit: int = 100) -> Any:
        """查询现货止盈止损（计划委托）订单。status: open/finished。"""
        return self._c.get(
            "/spot/price_orders", {"status": status, "limit": limit}
        )
