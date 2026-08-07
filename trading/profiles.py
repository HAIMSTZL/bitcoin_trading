"""多策略档案（模拟盘 A/B 对照）。

每个策略是独立的引擎实例：独立虚拟账户、独立 SQLite 数据库、独立配置开关。
通过 Web 面板顶部选项卡切换查看。

全局性安全参数（熔断、费率、单笔限额等）仍在 config.py，不随策略变化。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import config


@dataclass
class Profile:
    name: str
    label: str
    pairs: tuple[str, ...]
    kind: str = "grid"             # grid | predictive；predictive 仅支持模拟盘
    use_signal_filter: bool = True   # 指标信号过滤
    adaptive_range: bool = True      # ATR 自适应区间
    dynamic_allocation: bool = True  # 波动率动态分配 + 定期再平衡
    slot_rotation: bool = True       # 空仓槽位筛选换币
    auto_screen: bool = False        # 启动即全市场筛选 Top-N 建仓（忽略 pairs）

    @property
    def db_path(self) -> str:
        # rotation 沿用历史库，保持数据连续；其余策略独立成库
        if self.name == "rotation":
            return config.DB_PATH
        return os.path.join(config.DATA_DIR, f"trading_{self.name}.db")


PROFILES: dict[str, Profile] = {
    # 对照组：固定三币对、固定区间、均分、不换币（接近 main 分支行为）
    "classic": Profile(
        name="classic", label="经典网格",
        pairs=config.PAIRS,
        use_signal_filter=True, adaptive_range=False,
        dynamic_allocation=False, slot_rotation=False,
    ),
    # 完整版：全部机制 + 空仓筛选换币（当前分支）
    "rotation": Profile(
        name="rotation", label="筛选轮换",
        pairs=config.PAIRS,
        use_signal_filter=True, adaptive_range=True,
        dynamic_allocation=True, slot_rotation=True,
    ),
    # 激进组：无信号过滤双向硬跑 + 自适应区间 + 换币
    "aggressive": Profile(
        name="aggressive", label="激进轮动",
        pairs=config.PAIRS,
        use_signal_filter=False, adaptive_range=True,
        dynamic_allocation=True, slot_rotation=True,
    ),
    # 猎手组：启动即全市场筛选 Top3 建仓，激进风格（无信号过滤）
    "hunter": Profile(
        name="hunter", label="猎手精选",
        pairs=config.PAIRS,  # 仅作槽位数量与兜底，启动时会被筛选结果替换
        use_signal_filter=False, adaptive_range=True,
        dynamic_allocation=True, slot_rotation=True,
        auto_screen=True,
    ),
    # 预测候选：不是网格，纯 USDT 起步、long/flat、多币种等权；由独立 paper 引擎执行。
    "predictive": Profile(
        name="predictive", label="预测轮动（研究）",
        pairs=config.PREDICTIVE_PAIRS, kind="predictive",
        use_signal_filter=False, adaptive_range=False,
        dynamic_allocation=False, slot_rotation=False,
    ),
}


def enabled_profiles() -> dict[str, Profile]:
    """环境变量 STRATEGIES=classic,rotation 选择启用哪些，默认全部。"""
    raw = os.environ.get("STRATEGIES", "").strip()
    if not raw:
        return PROFILES
    names = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise ValueError(f"未知策略: {unknown}，可选: {list(PROFILES)}")
    if not names:
        raise ValueError("STRATEGIES 为空")
    return {n: PROFILES[n] for n in names}
