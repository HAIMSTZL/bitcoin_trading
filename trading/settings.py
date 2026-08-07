"""运行参数注册表：Web 面板可视化配置的参数清单、校验与持久化。

- 改动即时生效（引擎每轮读取 config 模块属性）；
- 持久化到 trading/data/overrides.json，重启后自动加载（run.py 启动时调用
  load_overrides()）；该文件在 gitignore 目录内，仅本地保存；
- 所有参数对所有策略实例全局生效。
"""

from __future__ import annotations

import json
import os

from . import config

OVERRIDES_PATH = os.path.join(config.DATA_DIR, "overrides.json")

# key, 分组, 显示名, 说明（悬停气泡）, min, max, step
SCHEMA = [
    # ---- 网格 ----
    ("TICK_INTERVAL", "网格", "行情轮询间隔(秒)",
     "每隔多少秒拉取最新价并撮合一次。越小成交越及时、漏单越少，但接口请求越频繁。",
     1, 30, 1),
    ("PAPER_FEE_RATE", "网格", "模拟手续费率",
     "单边手续费率（0.001 = 0.1%）。让模拟盘利润贴近实盘口径；利润=扣费后净利。",
     0, 0.01, 0.0005),
    ("RANGE_PCT_MIN", "网格", "自适应区间下限",
     "网格区间最小幅度（占中轴价比例）。21 档时单格间距≈2×下限÷20，必须大于双边手续费 0.2% 才有净利润。",
     0.01, 0.2, 0.005),
    ("RANGE_PCT_MAX", "网格", "自适应区间上限",
     "网格区间最大幅度。波动剧烈时区间最多加宽到此值。",
     0.05, 0.5, 0.01),
    ("ADAPTIVE_RANGE_MULT", "网格", "区间波动系数",
     "自适应区间 = 系数 × ATR%。系数越大区间越宽：成交少而稳；越小越窄：成交频繁但易被击穿。",
     2, 30, 1),
    # ---- 信号 ----
    ("INDICATOR_INTERVAL", "信号", "指标刷新周期(秒)",
     "MACD/KDJ/盘口/主动买卖量的刷新间隔。太短会被市场噪音主导。",
     30, 600, 10),
    ("SIGNAL_CONFIRM_COUNT", "信号", "信号确认次数",
     "趋势信号需连续 N 次同向才翻转（防抖）。越大信号越稳但越迟钝。",
     1, 5, 1),
    ("SIGNAL_COOLDOWN", "信号", "信号冷却期(秒)",
     "一次信号翻转后，该秒内不再允许翻转。与确认次数共同抑制抖动。",
     0, 3600, 30),
    ("DEPTH_RATIO_THRESHOLD", "信号", "盘口量比阈值",
     "盘口前 10 档买/卖量比超过此值才计分。越大对瞬时噪音越不敏感。",
     1.1, 3, 0.1),
    ("TRADE_RATIO_THRESHOLD", "信号", "主动买卖量比阈值",
     "最近 60 笔主动买/卖成交量比超过此值才计分。",
     1.1, 3, 0.1),
    # ---- 再平衡 ----
    ("REBALANCE_INTERVAL", "再平衡", "再平衡周期(秒)",
     "每隔多久检查一次各币对子弹分配。600=10 分钟。",
     60, 7200, 60),
    ("REBALANCE_MIN_DRIFT", "再平衡", "再平衡触发阈值",
     "目标权重偏离超过子弹池的该比例才真正调仓，避免频繁折腾。",
     0.01, 0.5, 0.01),
    # ---- 止损 ----
    ("STUCK_STOPLOSS_HOURS", "止损", "水下止损时限(小时)",
     "现价低于持仓平均成本且持续该时长无高于成本的成交 → 市价清仓止损。0 = 永不亏卖（死等回本）。",
     0, 72, 1),
    ("STOPLOSS_COOLDOWN_MIN", "止损", "止损冷却(分钟)",
     "止损清仓后该币对多少分钟内不接新买单，防止刚割完立刻接回。",
     0, 1440, 10),
    # ---- 熔断 ----
    ("CB_DROP_PCT", "熔断", "单币对熔断回撤(%)",
     "现价相对窗口内最高点回撤超过该值 → 冻结该币对交易。",
     1, 20, 0.5),
    ("CB_WINDOW_MIN", "熔断", "熔断窗口(分钟)",
     "回撤统计的时间窗口长度。",
     5, 120, 5),
    ("CB_GLOBAL_BTC_PCT", "熔断", "大盘熔断回撤(%)",
     "BTC 在窗口内回撤超过该值 → 全系统停止交易，等待企稳自动恢复。",
     2, 30, 0.5),
    # ---- 筛选 ----
    ("SCREEN_INTERVAL", "筛选", "空仓重筛周期(秒)",
     "槽位空仓但没筛到合格候选时，多久重筛一次。3600=1 小时。",
     300, 86400, 300),
    ("SCREEN_MIN_SCORE", "筛选", "候选及格线(分)",
     "精筛综合分（深度/点差/ATR甜区/热度稳定性）达到该值才可补位。宁缺毋滥的严格程度。",
     0, 100, 5),
]

_KEYS = {k for k, *_ in SCHEMA}


def current() -> list[dict]:
    """全部参数的当前值（供 Web 面板渲染）。"""
    return [
        {"key": k, "group": g, "label": label, "desc": desc,
         "min": mn, "max": mx, "step": st,
         "value": getattr(config, k)}
        for k, g, label, desc, mn, mx, st in SCHEMA
    ]


def apply(updates: dict) -> list[str]:
    """应用参数修改，返回被接受的 key 列表。非法项抛 ValueError。"""
    meta = {k: (mn, mx) for k, _, _, _, mn, mx, _ in SCHEMA}
    accepted = []
    for k, v in (updates or {}).items():
        if k not in _KEYS:
            raise ValueError(f"未知参数: {k}")
        mn, mx = meta[k]
        old = getattr(config, k)
        v = type(old)(float(v))
        if not (mn <= v <= mx):
            raise ValueError(f"{k}={v} 超出允许范围 [{mn}, {mx}]")
        setattr(config, k, v)
        accepted.append(k)
    if accepted:
        _save()
    return accepted


def _save() -> None:
    data = {k: getattr(config, k) for k in _KEYS}
    os.makedirs(os.path.dirname(OVERRIDES_PATH), exist_ok=True)
    with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_overrides() -> None:
    """启动时加载本地保存的参数覆盖（run.py 调用）。"""
    try:
        with open(OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for k, v in data.items():
        if k in _KEYS:
            setattr(config, k, type(getattr(config, k))(v))
