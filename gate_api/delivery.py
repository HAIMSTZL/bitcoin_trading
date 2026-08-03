"""交割合约（Delivery Futures）只读接口封装。"""

from __future__ import annotations

from typing import Any, Optional

from .client import GateClient


class DeliveryAPI:
    """settle: 结算币种，通常为 usdt。"""

    def __init__(self, client: GateClient, settle: str = "usdt"):
        self._c = client
        self.settle = settle.lower()

    def _p(self, suffix: str) -> str:
        return f"/delivery/{self.settle}{suffix}"

    # ---- 公共 ----
    def list_contracts(self) -> Any:
        """查询全部交割合约信息。"""
        return self._c.get(self._p("/contracts"))

    def get_contract(self, contract: str) -> Any:
        """查询单个交割合约信息。"""
        return self._c.get(self._p(f"/contracts/{contract}"))

    def list_tickers(self, contract: Optional[str] = None) -> Any:
        """查询交割合约行情 ticker。"""
        return self._c.get(self._p("/tickers"), {"contract": contract})

    # ---- 私有（只读）----
    def get_account(self) -> Any:
        """查询交割合约账户资产。"""
        return self._c.get(self._p("/accounts"))

    def list_account_book(self, limit: int = 100) -> Any:
        """查询交割合约账户账单。"""
        return self._c.get(self._p("/account_book"), {"limit": limit})

    def list_positions(self, limit: int = 100) -> Any:
        """查询交割合约持仓。"""
        return self._c.get(self._p("/positions"), {"limit": limit})

    def get_position(self, contract: str) -> Any:
        """查询单个交割合约持仓。"""
        return self._c.get(self._p(f"/positions/{contract}"))

    def list_orders(
        self,
        status: str = "open",
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割合约订单。status: open/finished。

        注意：Gate 对 open 状态的订单列表不支持 limit 参数。
        """
        params: dict = {"status": status, "contract": contract}
        if status != "open":
            params["limit"] = limit
        return self._c.get(self._p("/orders"), params)

    def list_my_trades(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割合约个人成交历史。"""
        return self._c.get(
            self._p("/my_trades"), {"contract": contract, "limit": limit}
        )

    def list_position_close(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割合约平仓历史。"""
        return self._c.get(
            self._p("/position_close"), {"contract": contract, "limit": limit}
        )

    def list_settlements(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割结算历史。"""
        return self._c.get(
            self._p("/settlements"), {"contract": contract, "limit": limit}
        )

    def list_liquidates(
        self,
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割合约强平历史。"""
        return self._c.get(
            self._p("/liquidates"), {"contract": contract, "limit": limit}
        )

    def list_price_orders(
        self,
        status: str = "open",
        contract: Optional[str] = None,
        limit: int = 100,
    ) -> Any:
        """查询交割合约计划委托。status: open/finished。"""
        return self._c.get(
            self._p("/price_orders"),
            {"status": status, "contract": contract, "limit": limit},
        )
