"""网格策略单元测试（含手续费、扫尾、信号拦截、盈亏基准）。

运行: .venv/bin/python tests/test_grid.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.grid import GridBot, PaperAccount  # noqa: E402


def make_bot(quote=100.0, base=0.0, fee=0.0):
    acc = PaperAccount()
    acc.init_pair("T_USDT", quote, base)
    # 测试用算术网格（等价差），触发价可预测
    bot = GridBot("T_USDT", 95, 105, 11, quote, base, fee_rate=fee, geometric=False)
    return bot, acc


def test_grid_roundtrip_profit():
    """低买高卖一个回合应产生正利润。"""
    bot, acc = make_bot()
    bot.start(102.0, acc)
    fills = []
    bot.step(100.5, acc, None)   # 触发 101 档买单
    bot.step(102.5, acc, lambda f: fills.append(f))  # 触发补在 102 的卖单
    sells = [f for f in fills if f["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["profit"] > 0
    assert bot.realized_profit > 0


def test_fee_deduction():
    """手续费：买入得币变少、卖出得 U 变少、利润为净利。"""
    bot, acc = make_bot(fee=0.001)
    bot.start(102.0, acc)
    fills = []
    bot.step(100.5, acc, lambda f: fills.append(f))
    buy = [f for f in fills if f["side"] == "buy"][0]
    assert abs(buy["fee"] - buy["quote"] * 0.001) < 1e-9
    assert abs(buy["amount"] - (buy["quote"] - buy["fee"]) / buy["price"]) < 1e-9
    fills.clear()
    bot.step(102.5, acc, lambda f: fills.append(f))
    sell = [f for f in fills if f["side"] == "sell"][0]
    # 新口径：净利 = 卖出净额(已扣卖费) - 买入实际成本(含买费)
    assert abs(sell["profit"] - (sell["quote"] - buy["quote"])) < 1e-9
    # 权益口径校验：quote 增量 = 净利（利润与账户变动一致）
    assert abs(bot.realized_profit - sell["profit"]) < 1e-9
    assert bot.total_fees > 0


def test_signal_blocking():
    """偏空不挂买单，偏多不挂卖单。"""
    bot, acc = make_bot()
    bot.signal = -1
    bot.start(102.0, acc)
    assert all(o["side"] != "buy" for o in bot.orders.values())
    assert bot.blocked_count > 0
    bot2, acc2 = make_bot(base=10.0)
    bot2.signal = 1
    bot2.start(98.0, acc2)
    assert all(o["side"] != "sell" for o in bot2.orders.values())


def test_sweep_dust():
    """粉尘收尾：无卖单且持仓 < 阈值时全卖归 0。"""
    bot, acc = make_bot(fee=0.001)
    bal = acc.get("T_USDT")
    bal["base"] = 0.0001  # 粉尘
    fills = []
    bot._maybe_sweep(100.0, bal, None, fills)
    assert bal["base"] == 0 and len(fills) == 1 and fills[0]["sweep"] is True


def test_capital_adjust_baseline():
    """再平衡调拨不应污染单币对盈亏。"""
    bot, acc = make_bot()
    bot.start(100.0, acc)
    assert abs(bot.state(100.0, acc)["pnl"]) < 1e-9
    acc.get("T_USDT")["quote"] = 70.0
    bot.capital_adjust += 70.0 - 100.0
    assert abs(bot.state(100.0, acc)["pnl"]) < 1e-9


def test_serialization_roundtrip():
    bot, acc = make_bot(fee=0.001)
    bot.start(100.0, acc)
    bot.step(99.0, acc)
    bot.ever_held = True
    d = bot.to_dict(acc)
    bot2 = GridBot.from_dict(d, acc)
    assert bot2.orders == bot.orders
    assert bot2.realized_profit == bot.realized_profit
    assert bot2.ever_held is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✅ {name}")
    print("全部通过")
