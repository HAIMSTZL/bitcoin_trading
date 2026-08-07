"""现货网格策略 + 模拟账户。

网格逻辑（经典低买高卖）：
- 在 [lower, upper] 区间均匀布置 N 个价格档位；
- 启动价下方的档位挂买单，上方的档位挂卖单（需要基础币库存）；
- 买单成交后，在其上一档挂出同数量卖单；卖单成交后，在其下一档挂回买单；
- 每笔卖单成交相对其配对买价（初始库存以启动价为成本基准）计入已实现 USDT 利润。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import config as _cfg


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
        geometric: bool = True,
    ):
        if not (lower > 0 and upper > lower and grids >= 3):
            raise ValueError("非法网格参数")
        self.pair = pair
        self.lower = lower
        self.upper = upper
        if geometric:
            # 等百分比间距：每格收益率一致，跨价位更均匀
            ratio = (upper / lower) ** (1 / (grids - 1))
            self.levels = [lower * ratio ** i for i in range(grids)]
        else:
            step = (upper - lower) / (grids - 1)
            self.levels = [lower + step * i for i in range(grids)]
        self.quote_budget = quote_budget
        self.base_budget = base_budget
        self.fee_rate = fee_rate  # 单边手续费率（模拟盘按成交金额扣）

        # idx -> 挂单 {side, price, quote_amount, base_amount}
        # 利润按持仓移动平均成本 avg_cost 结转（含买入手续费）
        self.orders: dict[int, dict[str, Any]] = {}
        self.start_price: Optional[float] = None
        self.realized_profit = 0.0  # USDT 口径（已扣手续费）
        self.trade_count = 0
        self.total_fees = 0.0  # 累计手续费（USDT）
        self.ever_held = False  # 是否曾持有仓位（补位筛选的触发前提）
        # 持仓移动平均成本（USDT/个，含买费）：买入时加权更新，清空时归 None。
        # 重建/补挂不改写它——成本属于存货，不属于订单。
        self.avg_cost: Optional[float] = None
        # 水下计时：现价低于平均成本的持续起点（None=不在水下）
        self.underwater_since: Optional[float] = None
        # 止损冷却截止时间：此时间前不接新买单
        self.no_buy_until: float = 0.0
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
        if base_per > 0 and self.avg_cost is None:
            self.avg_cost = price  # 初始库存以启动市值为成本基准（已有成本不覆盖）
        for i in sell_levels:
            # 不亏卖：档位低于持仓平均成本时不挂（等价格回到成本上方）
            if (base_per > 0 and not self._blocked("sell")
                    and (self.avg_cost is None or self.levels[i] > self.avg_cost)):
                self.orders[i] = {
                    "side": "sell",
                    "price": self.levels[i],
                    "base_amount": base_per,
                }
                self._notify_order(self.orders[i])

    # ------------------------------------------------------------------
    def _blocked(self, side: str) -> bool:
        """趋势信号拦截：偏空(-1)不接买单，偏多(+1)不卖；止损冷却期不接买单。"""
        if side == "buy" and time.time() < self.no_buy_until:
            self.blocked_count += 1
            return True
        if side == "buy" and self.signal == -1:
            self.blocked_count += 1
            return True
        if side == "sell" and self.signal == 1:
            self.blocked_count += 1
            return True
        return False

    # ------------------------------------------------------------------
    def _check_stoploss(self, price: float, bal: dict,
                        record: Optional[Callable[[dict], None]],
                        fills: list[dict]) -> None:
        """水下限时持有：超过 STUCK_STOPLOSS_HOURS 无高于成本的成交 → 市价止损。"""
        underwater = (bal["base"] > 0 and self.avg_cost is not None
                      and price < self.avg_cost)
        if not underwater:
            self.underwater_since = None
            return
        if self.underwater_since is None:
            self.underwater_since = time.time()
            return
        hours = _cfg.STUCK_STOPLOSS_HOURS
        if hours <= 0:
            return  # 关闭止损
        if time.time() - self.underwater_since < hours * 3600:
            return
        # 触发止损：全部持仓按现价卖出（亏损落账）
        amount = bal["base"]
        fee = amount * price * self.fee_rate
        proceeds = amount * price - fee
        profit = proceeds - amount * self.avg_cost
        self.total_fees += fee
        self.realized_profit += profit
        bal["base"] = 0.0
        bal["quote"] += proceeds
        self.avg_cost = None
        self.underwater_since = None
        self.no_buy_until = time.time() + _cfg.STOPLOSS_COOLDOWN_MIN * 60
        # 止损后卖单已无意义，撤掉；买单保留待补位/冷却后被替换或恢复
        self.orders = {i: o for i, o in self.orders.items() if o["side"] != "sell"}
        fill = {
            "pair": self.pair, "side": "sell", "price": price,
            "amount": amount, "quote": proceeds, "profit": profit,
            "fee": fee, "stoploss": True,
        }
        self._on_fill(fill, record, fills)

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
        self._check_stoploss(price, bal, record, fills)  # 水下超时先止损

        for idx, order in list(self.orders.items()):
            if idx not in self.orders:  # 可能已被本轮处理
                continue
            if order["side"] == "buy" and price <= order["price"]:
                if bal["quote"] + 1e-12 < order["quote_amount"]:
                    continue  # 预算已用尽，挂单保留等待（模拟盘一般不会发生）
                fee = order["quote_amount"] * self.fee_rate
                base_got = (order["quote_amount"] - fee) / order["price"]
                self.total_fees += fee
                old_base = bal["base"]
                new_base = old_base + base_got
                # 移动平均成本：含买入手续费（quote_amount 是实际付出的全部 USDT）
                self.avg_cost = (
                    old_base * (self.avg_cost or order["price"]) + order["quote_amount"]
                ) / new_base
                bal["quote"] -= order["quote_amount"]
                bal["base"] = new_base
                del self.orders[idx]
                fill = {
                    "pair": self.pair, "side": "buy", "price": order["price"],
                    "amount": base_got, "quote": order["quote_amount"],
                    "profit": 0.0, "fee": fee,
                }
                self._on_fill(fill, record, fills)
                self.ever_held = True
                # 上一档挂出卖单
                self._place_sell(idx + 1, base_got)
            elif order["side"] == "sell" and price >= order["price"]:
                if bal["base"] + 1e-12 < order["base_amount"]:
                    continue
                gross = order["base_amount"] * order["price"]
                fee = gross * self.fee_rate
                proceeds = gross - fee
                self.total_fees += fee
                bal["base"] -= order["base_amount"]
                bal["quote"] += proceeds
                cost_per = self.avg_cost if self.avg_cost is not None else order["price"]
                profit = proceeds - order["base_amount"] * cost_per
                self.realized_profit += profit
                del self.orders[idx]
                if bal["base"] <= 0:
                    self.avg_cost = None
                fill = {
                    "pair": self.pair, "side": "sell", "price": order["price"],
                    "amount": order["base_amount"], "quote": proceeds,
                    "profit": profit, "fee": fee,
                }
                self._on_fill(fill, record, fills)
                # 下一档挂回买单
                self._place_buy(idx - 1, order["base_amount"])
                # 清仓收尾：剩余持仓已不足一个网格批且没有卖单了 → 直接全卖扫尾归 0
                self._maybe_sweep(price, bal, record, fills)

        return fills

    # ------------------------------------------------------------------
    def _maybe_sweep(
        self,
        price: float,
        bal: dict,
        record: Optional[Callable[[dict], None]],
        fills: list[dict],
    ) -> None:
        remaining = bal["base"]
        if remaining <= 0 or remaining * price >= _cfg.SWEEP_DUST_USDT:
            return
        if any(o["side"] == "sell" for o in self.orders.values()):
            return  # 还有卖单在路上，让正常网格处理
        # 以略低于现价挂扫尾卖单，下一个 tick 必成交；利润按平均成本结转
        sweep_price = price * 0.999
        fee = remaining * sweep_price * self.fee_rate
        proceeds = remaining * sweep_price - fee
        cost_per = self.avg_cost if self.avg_cost is not None else price
        profit = proceeds - remaining * cost_per
        self.total_fees += fee
        self.realized_profit += profit
        bal["base"] -= remaining
        bal["quote"] += proceeds
        self.avg_cost = None
        fill = {
            "pair": self.pair, "side": "sell", "price": sweep_price,
            "amount": remaining, "quote": proceeds, "profit": profit,
            "fee": fee, "sweep": True,
        }
        self._on_fill(fill, record, fills)

    # ------------------------------------------------------------------
    def _place_sell(self, idx: int, base_amount: float) -> None:
        if idx >= len(self.levels) or idx in self.orders or self._blocked("sell"):
            return  # 超出区间顶部：卖出后不再补单（利润落袋）；偏多信号：不卖飞
        self.orders[idx] = {
            "side": "sell",
            "price": self.levels[idx],
            "base_amount": base_amount,
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
        """重建卖单侧（买单保持不变）：信号解封后补挂。不改写持仓成本（avg_cost）。"""
        self.orders = {i: o for i, o in self.orders.items() if o["side"] != "sell"}
        bal = account.get(self.pair)
        sell_levels = [i for i, p in enumerate(self.levels)
                       if p > price and i not in self.orders]
        if not sell_levels:
            return
        base_per = bal["base"] / len(sell_levels)
        for i in sell_levels:
            # 不亏卖：低于持仓平均成本的档位不挂
            if (base_per > 0 and not self._blocked("sell")
                    and (self.avg_cost is None or self.levels[i] > self.avg_cost)):
                self.orders[i] = {
                    "side": "sell",
                    "price": self.levels[i],
                    "base_amount": base_per,
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
            "ever_held": self.ever_held,
            "avg_cost": self.avg_cost,
            "underwater_since": self.underwater_since,
            "no_buy_until": self.no_buy_until,
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
        bot.ever_held = data.get("ever_held", False)
        bot.avg_cost = data.get("avg_cost")
        bot.underwater_since = data.get("underwater_since")
        bot.no_buy_until = data.get("no_buy_until", 0.0)
        # 旧存档无 avg_cost：有持仓时以启动价兜底
        if bot.avg_cost is None and data.get("base", 0) > 0:
            bot.avg_cost = data.get("start_price")
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
