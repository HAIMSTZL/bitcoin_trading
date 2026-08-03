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

    def list_price_orders(self, status: str = "open", limit: int = 100) -> Any:
        """查询现货止盈止损（计划委托）订单。status: open/finished。"""
        return self._c.get(
            "/spot/price_orders", {"status": status, "limit": limit}
        )
