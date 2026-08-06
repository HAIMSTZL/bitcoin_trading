"""指标纯函数单元测试。

运行: .venv/bin/python -m pytest tests/ -v
或:   .venv/bin/python tests/test_indicators.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.indicators import (  # noqa: E402
    atr_percent,
    combine_signal,
    ema,
    kdj,
    macd,
    book_pressure,
    trade_pressure,
)


def test_ema():
    # k=2/(3+1)=0.5: seed=2, 4*.5+2*.5=3, 5*.5+3*.5=4
    assert ema([1, 2, 3, 4, 5], 3) == [2.0, 3.0, 4.0]
    assert ema([1, 2], 3) == []  # 数据不足


def test_macd_direction():
    up = macd([float(i) ** 1.5 for i in range(1, 80)])
    down = macd([-float(i) ** 1.5 for i in range(1, 80)])
    assert up["hist"] > 0
    assert down["hist"] < 0
    # 镜像对称
    assert abs(up["hist"] + down["hist"]) < 1e-9


def test_kdj_extremes():
    j_up = kdj([float(i) for i in range(1, 30)],
               [float(i) - 0.5 for i in range(1, 30)],
               [float(i) - 0.2 for i in range(1, 30)])
    j_dn = kdj([float(30 - i) for i in range(29)],
               [float(30 - i) - 0.5 for i in range(29)],
               [float(30 - i) - 0.2 for i in range(29)])
    assert j_up["k"] > 80 and j_dn["k"] < 20


def test_atr_percent():
    # 恒定价格 -> ATR 为 0
    flat = [[str(i), "100", "10.0", "10.0", "10.0", "10.0", "1", "true"]
            for i in range(30)]
    assert atr_percent(flat) == 0.0
    # 每根振幅 1% -> ATR% ≈ 1
    rows = [[str(i), "100", str(100 + i * 0.01), str(100.5 + i * 0.01),
             str(99.5 + i * 0.01), str(100 + i * 0.01), "1", "true"]
            for i in range(30)]
    assert 0.5 < atr_percent(rows) < 1.5


def test_book_and_trade_pressure():
    ob = {"bids": [["100", "2"]], "asks": [["101", "1"]]}
    assert book_pressure(ob) == 2.0
    trades = [{"side": "buy", "amount": "3"}, {"side": "sell", "amount": "1"}]
    assert trade_pressure(trades) == 3.0


def test_combine_signal():
    assert combine_signal(0.5, 70, 60, 2.0, 1.6)[0] == 1     # 全多
    assert combine_signal(-0.5, 30, 40, 0.4, 0.5)[0] == -1   # 全空
    assert combine_signal(0.1, 45, 55, 1.0, 1.1)[0] in (0, -1)  # 混合不偏多
    # 阈值语义：1.3 不计分（< 1.5），1.6 才计分
    assert combine_signal(-0.1, 40, 50, 1.3, 1.3)[0] == -1  # MACD↓+死叉=空
    assert combine_signal(-0.1, 40, 50, 1.6, 1.3)[0] == 0   # 盘口强多抵消为中性


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"✅ {name}")
    print("全部通过")
