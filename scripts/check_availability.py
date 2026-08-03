#!/usr/bin/env python3
"""Gate API v4 各模块只读接口可用性测试。

- 只调用 GET 只读接口，绝不进行任何下单/撤单/划转等写操作。
- 密钥从环境变量 MY_GATE_KEY / MY_GATE_SECRET 读取。
- 用法: .venv/bin/python scripts/check_availability.py
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

sys.path.insert(0, ".")  # 允许从项目根目录直接运行

from gate_api import (  # noqa: E402
    DeliveryAPI,
    FuturesAPI,
    GateApiError,
    GateClient,
    SpotAPI,
    SubAccountAPI,
    TradingBotAPI,
    WalletAPI,
)

OK = "✅"
FAIL = "❌"
SKIP = "⚠️ "


def summarize(data: Any) -> str:
    """生成简短的返回摘要。"""
    if isinstance(data, list):
        return f"list[{len(data)}]"
    if isinstance(data, dict):
        keys = list(data.keys())[:5]
        return f"dict{{{', '.join(map(str, keys))}{'...' if len(data) > 5 else ''}}}"
    return repr(data)[:80]


def run_case(name: str, fn: Callable[[], Any]) -> str:
    """返回 'pass' / 'fail' / 'skip'。"""
    try:
        data = fn()
        print(f"{OK} {name:<48} {summarize(data)}")
        return "pass"
    except GateApiError as e:
        if e.label == "USER_NOT_FOUND":
            # 账户未入金/未开通（如合约账户从未划转资金），属账户状态而非接口不可用
            print(f"{SKIP} {name:<48} 账户未开通: {e}")
            return "skip"
        print(f"{FAIL} {name:<48} {e}")
        return "fail"
    except Exception as e:  # 网络错误等
        print(f"{FAIL} {name:<48} {type(e).__name__}: {e}")
        return "fail"


def main() -> int:
    client = GateClient()
    spot = SpotAPI(client)
    wallet = WalletAPI(client)
    futures = FuturesAPI(client, settle="usdt")
    delivery = DeliveryAPI(client, settle="usdt")
    sub = SubAccountAPI(client)
    bot = TradingBotAPI(client)

    cases: list[tuple[str, Callable[[], Any]]] = [
        # ---- 现货 ----
        ("spot.list_currencies", spot.list_currencies),
        ("spot.get_currency(BTC)", lambda: spot.get_currency("BTC")),
        ("spot.get_currency_pair(BTC_USDT)", lambda: spot.get_currency_pair("BTC_USDT")),
        ("spot.list_tickers(BTC_USDT)", lambda: spot.list_tickers("BTC_USDT")),
        ("spot.list_accounts", spot.list_accounts),
        ("spot.list_account_book", spot.list_account_book),
        ("spot.list_open_orders", spot.list_open_orders),
        ("spot.list_orders(BTC_USDT,open)", lambda: spot.list_orders("BTC_USDT", "open")),
        ("spot.list_orders(BTC_USDT,finished)", lambda: spot.list_orders("BTC_USDT", "finished")),
        ("spot.list_my_trades", spot.list_my_trades),
        ("spot.get_fee", spot.get_fee),
        ("spot.list_price_orders(open)", lambda: spot.list_price_orders("open")),
        # ---- 钱包 ----
        ("wallet.get_total_balance", wallet.get_total_balance),
        ("wallet.get_deposit_address(BTC)", lambda: wallet.get_deposit_address("BTC")),
        ("wallet.list_currency_chains(USDT)", lambda: wallet.list_currency_chains("USDT")),
        ("wallet.list_deposits", wallet.list_deposits),
        ("wallet.list_withdrawals", wallet.list_withdrawals),
        ("wallet.get_withdraw_status", wallet.get_withdraw_status),
        ("wallet.list_sub_account_transfers", wallet.list_sub_account_transfers),
        ("wallet.list_push", wallet.list_push),
        ("wallet.list_sub_account_balances", wallet.list_sub_account_balances),
        ("wallet.list_small_balance", wallet.list_small_balance),
        ("wallet.list_small_balance_history", wallet.list_small_balance_history),
        ("wallet.list_saved_address", wallet.list_saved_address),
        ("wallet.get_fee", wallet.get_fee),
        # ---- 永续合约 ----
        ("futures.list_contracts", futures.list_contracts),
        ("futures.get_contract(BTC_USDT)", lambda: futures.get_contract("BTC_USDT")),
        ("futures.list_tickers(BTC_USDT)", lambda: futures.list_tickers("BTC_USDT")),
        ("futures.get_account", futures.get_account),
        ("futures.list_account_book", futures.list_account_book),
        ("futures.list_positions", futures.list_positions),
        ("futures.list_orders(open)", lambda: futures.list_orders("open")),
        ("futures.list_orders(finished)", lambda: futures.list_orders("finished")),
        ("futures.list_my_trades", futures.list_my_trades),
        ("futures.list_position_close", futures.list_position_close),
        ("futures.list_liquidates", futures.list_liquidates),
        ("futures.list_price_orders(open)", lambda: futures.list_price_orders("open")),
        # ---- 交割合约 ----
        ("delivery.list_contracts", delivery.list_contracts),
        ("delivery.get_account", delivery.get_account),
        ("delivery.list_account_book", delivery.list_account_book),
        ("delivery.list_positions", delivery.list_positions),
        ("delivery.list_orders(open)", lambda: delivery.list_orders("open")),
        ("delivery.list_orders(finished)", lambda: delivery.list_orders("finished")),
        ("delivery.list_my_trades", delivery.list_my_trades),
        ("delivery.list_position_close", delivery.list_position_close),
        ("delivery.list_settlements", delivery.list_settlements),
        ("delivery.list_liquidates", delivery.list_liquidates),
        ("delivery.list_price_orders(open)", lambda: delivery.list_price_orders("open")),
        # ---- 子账户/托管 ----
        ("subaccount.list_sub_accounts", sub.list_sub_accounts),
        ("subaccount.list_sub_account_transfers", sub.list_sub_account_transfers),
        # ---- 交易机器人 ----
        ("bot.list_strategy_recommend", bot.list_strategy_recommend),
        ("bot.list_running_bots", bot.list_running_bots),
    ]

    # 动态用例：若有运行中的机器人，再查其详情；若有子账户，再查其 API Key 列表
    try:
        running = bot.list_running_bots()
        items = running.get("list") or running.get("items") or (
            running if isinstance(running, list) else []
        )
        if items:
            first = items[0]
            sid = str(first.get("strategy_id") or first.get("id"))
            stype = first.get("strategy_type")
            if sid and stype:
                cases.append(
                    (
                        f"bot.get_bot_detail({sid})",
                        lambda: bot.get_bot_detail(sid, stype),
                    )
                )
    except Exception:
        pass  # running 列表查询失败时，动态用例跳过（上面静态用例会报告）

    try:
        subs = sub.list_sub_accounts()
        if subs:
            uid = subs[0].get("user_id")
            if uid:
                cases.append(
                    (
                        f"subaccount.list_sub_account_keys({uid})",
                        lambda: sub.list_sub_account_keys(uid),
                    )
                )
    except Exception:
        pass

    print(f"共 {len(cases)} 个只读接口待测试\n")
    passed = 0
    skipped: list[str] = []
    failed: list[str] = []
    for name, fn in cases:
        result = run_case(name, fn)
        if result == "pass":
            passed += 1
        elif result == "skip":
            skipped.append(name)
        else:
            failed.append(name)
        time.sleep(0.15)  # 控制频率，避免触发限流

    print(f"\n结果: {passed}/{len(cases)} 通过, {len(skipped)} 个跳过, {len(failed)} 个失败")
    if skipped:
        print("跳过的接口（账户未入金/未开通，非接口问题）:")
        for n in skipped:
            print(f"  - {n}")
    if failed:
        print("失败的接口:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
