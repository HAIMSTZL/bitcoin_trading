"""现货网格策略 + 模拟账户。

网格逻辑（经典低买高卖）：
- 在 [lower, upper] 区间均匀布置 N 个价格档位；
- 启动价下方的档位挂买单，上方的档位挂卖单（需要基础币库存）；
- 买单成交后，在其上一档挂出同数量卖单；卖单成交后，在其下一档挂回买单；
- 每笔卖单成交相对其配对买价（初始库存以启动价为成本基准）计入已实现 USDT 利润。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class PaperAccount:
    """按交易对隔离的虚拟账户。quote=USDT, base=基础币。"""

    def __init__(self) -> None:
        self.balances: dict[str, dict[str, float]] = {}

    def init_pair(self, pair: str, quote: float, base: float) -> None:
        self.balances[pair] = {"quote": float(quote), "base": float(base)}

    def get(self, pair: str) -> dict[str, float]:
        return self.balances[pair]


class GridBot:
    def __init__(
        self,
        pair: str,
        lower: float,
        upper: float,
        grids: int,
        quote_budget: float,
        base_budget: float,
        fee_rate: float = 0.0,
    ):
        if not (lower > 0 and upper > lower and grids >= 3):
            raise ValueError("非法网格参数")
        self.pair = pair
        self.lower = lower
        self.upper = upper
        step = (upper - lower) / (grids - 1)
        self.levels = [lower + step * i for i in range(grids)]
        self.quote_budget = quote_budget
        self.base_budget = base_budget
        self.fee_rate = fee_rate  # 单边手续费率（模拟盘按成交金额扣）

        # idx -> 挂单 {side, price, quote_amount, base_amount, buy_price}
        self.orders: dict[int, dict[str, Any]] = {}
        self.start_price: Optional[float] = None
        self.realized_profit = 0.0  # USDT 口径（已扣手续费）
        self.trade_count = 0
        self.total_fees = 0.0  # 累计手续费（USDT）
        self.regime = "ranging"  # 行情状态（引擎写入）：ranging/trend_up/trend_down
        # 再平衡资金调拨累计（USDT 净流入）：盈亏基准随之调整，
        # 避免资金调拨被误计为交易亏损（引擎在再平衡时写入）
        self.capital_adjust = 0.0
        # 趋势信号（引擎每个 tick 写入）：+1 偏多 / 0 中性 / -1 偏空
        # 偏空暂停挂买单（不接飞刀），偏多暂停挂卖单（不卖飞）
        self.signal = 0
        self.blocked_count = 0  # 被信号拦截的挂单次数
        # 挂单事件回调（由引擎注入，用于日志记录）；签名为 fn(order_dict)
        self.on_order: Optional[Callable[[dict], None]] = None

    def _notify_order(self, order: dict) -> None:
        if self.on_order:
            self.on_order({"pair": self.pair, **order})

    # ------------------------------------------------------------------
    def start(self, price: float, account: PaperAccount) -> None:
        """按启动价初始化挂单。启动价作为初始库存卖单的成本基准。"""
        self.start_price = price
        bal = account.get(self.pair)

        buy_levels = [i for i, p in enumerate(self.levels) if p < price]
        sell_levels = [i for i, p in enumerate(self.levels) if p > price]

        quote_per = self.quote_budget / len(buy_levels) if buy_levels else 0.0
        quote_per = min(quote_per, bal["quote"])
        base_per = self.base_budget / len(sell_levels) if sell_levels else 0.0
        base_per = min(base_per, bal["base"])

        for i in buy_levels:
            if quote_per > 0 and not self._blocked("buy"):
                self.orders[i] = {
                    "side": "buy",
                    "price": self.levels[i],
                    "quote_amount": quote_per,
                    "base_amount": quote_per / self.levels[i],
                }
                self._notify_order(self.orders[i])
        for i in sell_levels:
            if base_per > 0 and not self._blocked("sell"):
                self.orders[i] = {
                    "side": "sell",
                    "price": self.levels[i],
                    "base_amount": base_per,
                    "buy_price": price,  # 初始库存以启动价为成本基准
                }
                self._notify_order(self.orders[i])

    # ------------------------------------------------------------------
    def _blocked(self, side: str) -> bool:
        """趋势信号拦截：偏空(-1)不接买单，偏多(+1)不卖。"""
        if side == "buy" and self.signal == -1:
            self.blocked_count += 1
            return True
        if side == "sell" and self.signal == 1:
            self.blocked_count += 1
            return True
        return False

    # ------------------------------------------------------------------
    def step(
        self,
        price: float,
        account: PaperAccount,
        record: Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """用最新成交价撮合一次，返回本次成交列表。"""
        fills: list[dict] = []
        bal = account.get(self.pair)

        for idx, order in list(self.orders.items()):
            if idx not in self.orders:  # 可能已被本轮处理
                continue
            if order["side"] == "buy" and price <= order["price"]:
                if bal["quote"] + 1e-12 < order["quote_amount"]:
                    continue  # 预算已用尽，挂单保留等待（模拟盘一般不会发生）
                fee = order["quote_amount"] * self.fee_rate
                base_got = (order["quote_amount"] - fee) / order["price"]
                self.total_fees += fee
                bal["quote"] -= order["quote_amount"]
                bal["base"] += base_got
                del self.orders[idx]
                fill = {
                    "pair": self.pair, "side": "buy", "price": order["price"],
                    "amount": base_got, "quote": order["quote_amount"],
                    "profit": 0.0, "fee": fee,
                }
                self._on_fill(fill, record, fills)
                # 上一档挂出卖单
                self._place_sell(idx + 1, base_got, order["price"])
            elif order["side"] == "sell" and price >= order["price"]:
                if bal["base"] + 1e-12 < order["base_amount"]:
                    continue
                gross = order["base_amount"] * order["price"]
                fee = gross * self.fee_rate
                proceeds = gross - fee
                self.total_fees += fee
                bal["base"] -= order["base_amount"]
                bal["quote"] += proceeds
                profit = proceeds - order["base_amount"] * order.get("buy_price", order["price"])
                self.realized_profit += profit
                del self.orders[idx]
                fill = {
                    "pair": self.pair, "side": "sell", "price": order["price"],
                    "amount": order["base_amount"], "quote": proceeds,
                    "profit": profit, "fee": fee,
                }
                self._on_fill(fill, record, fills)
                # 下一档挂回买单
                self._place_buy(idx - 1, order["base_amount"])

        return fills

    # ------------------------------------------------------------------
    def _place_sell(self, idx: int, base_amount: float, buy_price: float) -> None:
        if idx >= len(self.levels) or idx in self.orders or self._blocked("sell"):
            return  # 超出区间顶部：卖出后不再补单（利润落袋）；偏多信号：不卖飞
        self.orders[idx] = {
            "side": "sell",
            "price": self.levels[idx],
            "base_amount": base_amount,
            "buy_price": buy_price,
        }
        self._notify_order(self.orders[idx])

    def rebuild_buys(self, price: float, account: PaperAccount) -> None:
        """重建买单侧（卖单与成本基准保持不变）：子弹补充/再平衡时使用。"""
        self.orders = {i: o for i, o in self.orders.items() if o["side"] != "buy"}
        bal = account.get(self.pair)
        buy_levels = [i for i, p in enumerate(self.levels)
                      if p < price and i not in self.orders]
        if not buy_levels:
            return
        quote_per = bal["quote"] / len(buy_levels)
        for i in buy_levels:
            if quote_per > 0 and not self._blocked("buy"):
                self.orders[i] = {
                    "side": "buy",
                    "price": self.levels[i],
                    "quote_amount": quote_per,
                    "base_amount": quote_per / self.levels[i],
                }
                self._notify_order(self.orders[i])

    def rebuild_sells(self, price: float, account: PaperAccount) -> None:
        """重建卖单侧（买单保持不变）：信号解封后补挂。成本基准以现价计。"""
        self.orders = {i: o for i, o in self.orders.items() if o["side"] != "sell"}
        bal = account.get(self.pair)
        sell_levels = [i for i, p in enumerate(self.levels)
                       if p > price and i not in self.orders]
        if not sell_levels:
            return
        base_per = bal["base"] / len(sell_levels)
        for i in sell_levels:
            if base_per > 0 and not self._blocked("sell"):
                self.orders[i] = {
                    "side": "sell",
                    "price": self.levels[i],
                    "base_amount": base_per,
                    "buy_price": price,  # 以现价为成本基准
                }
                self._notify_order(self.orders[i])

    def _place_buy(self, idx: int, base_amount: float) -> None:
        if idx < 0 or idx in self.orders or self._blocked("buy"):
            return  # 超出区间底部：买入后等待即可；偏空信号：不接飞刀
        price = self.levels[idx]
        self.orders[idx] = {
            "side": "buy",
            "price": price,
            "quote_amount": base_amount * price,
            "base_amount": base_amount,
        }
        self._notify_order(self.orders[idx])

    def _on_fill(
        self,
        fill: dict,
        record: Optional[Callable[[dict], None]],
        fills: list[dict],
    ) -> None:
        self.trade_count += 1
        fills.append(fill)
        if record:
            record(fill)

    # ------------------------------------------------------------------
    # 序列化（模拟盘状态持久化，重启后恢复）
    # ------------------------------------------------------------------
    def to_dict(self, account: PaperAccount) -> dict:
        bal = account.get(self.pair)
        return {
            "pair": self.pair,
            "lower": self.lower,
            "upper": self.upper,
            "grids": len(self.levels),
            "quote_budget": self.quote_budget,
            "base_budget": self.base_budget,
            "start_price": self.start_price,
            "realized_profit": self.realized_profit,
            "trade_count": self.trade_count,
            "blocked_count": self.blocked_count,
            "total_fees": self.total_fees,
            "capital_adjust": self.capital_adjust,
            "orders": {str(i): o for i, o in self.orders.items()},
            "quote": bal["quote"],
            "base": bal["base"],
        }

    @classmethod
    def from_dict(cls, data: dict, account: PaperAccount) -> "GridBot":
        bot = cls(
            data["pair"], data["lower"], data["upper"], data["grids"],
            data["quote_budget"], data["base_budget"],
        )
        bot.start_price = data["start_price"]
        bot.realized_profit = data["realized_profit"]
        bot.trade_count = data["trade_count"]
        bot.blocked_count = data.get("blocked_count", 0)
        bot.total_fees = data.get("total_fees", 0.0)
        bot.capital_adjust = data.get("capital_adjust", 0.0)
        bot.orders = {int(i): o for i, o in data["orders"].items()}
        account.init_pair(data["pair"], data["quote"], data["base"])
        return bot

    # ------------------------------------------------------------------
    def state(self, price: float, account: PaperAccount) -> dict:
        bal = account.get(self.pair)
        equity = bal["quote"] + bal["base"] * price
        initial_equity = (self.quote_budget + self.capital_adjust
                          + self.base_budget * (self.start_price or price))
        return {
            "pair": self.pair,
            "price": price,
            "lower": self.lower,
            "upper": self.upper,
            "levels": self.levels,
            "quote": bal["quote"],
            "base": bal["base"],
            "equity": equity,
            "initial_equity": initial_equity,
            "pnl": equity - initial_equity,
            "realized_profit": self.realized_profit,
            "trade_count": self.trade_count,
            "signal": self.signal,
            "regime": self.regime,
            "blocked_count": self.blocked_count,
            "total_fees": self.total_fees,
            "orders": [
                {"side": o["side"], "price": o["price"], "base_amount": o["base_amount"]}
                for _, o in sorted(list(self.orders.items()))
            ],
        }
