# Gate API v4 接口说明文档

本项目对 [Gate API v4](https://www.gate.com/docs/developers/apiv4/en/) 做了 Python 封装，
**以只读（GET）接口为主**，用于账户、资产、行情、订单、持仓等信息的查询。

## 安全约定

- API Key / Secret **只从环境变量读取**，代码中严禁出现明文密钥：
  - `MY_GATE_KEY` — API Key
  - `MY_GATE_SECRET` — API Secret
- 本封装**不封装任何下单、撤单、划转、提现等写操作**，只提供查询能力。
- Base URL：`https://api.gateio.ws`，所有路径自动拼接 `/api/v4` 前缀。

## 认证签名规则

私有接口需要在请求头携带三个字段：

| Header | 说明 |
|--------|------|
| `KEY` | API Key |
| `Timestamp` | 当前 Unix 时间戳（秒，字符串） |
| `SIGN` | 签名，见下 |

签名串（`\n` 连接五行）：

```
HTTP_METHOD           # 如 GET
/api/v4{path}         # 如 /api/v4/spot/accounts
query_string          # urlencode 后的查询串，无参数为空串
sha512_hex(body)      # 请求体的 SHA512 hex；GET 为空字符串的 SHA512
timestamp             # 与 Timestamp 头一致
```

`SIGN = HMAC_SHA512(secret, 签名串).hexdigest()`

## 快速开始

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

export MY_GATE_KEY="你的Key"        # 不要提交到代码库
export MY_GATE_SECRET="你的Secret"

# 运行只读接口可用性测试
.venv/bin/python scripts/check_availability.py
```

```python
from gate_api import GateClient
from gate_api.spot import SpotAPI

client = GateClient()          # 自动读取环境变量
spot = SpotAPI(client)
accounts = spot.list_accounts()  # 现货账户资产
```

## 模块总览

| 模块 | 类 | 说明 |
|------|----|------|
| `gate_api.client` | `GateClient` | 签名 HTTP 客户端，所有模块共用 |
| `gate_api.spot` | `SpotAPI` | 现货 |
| `gate_api.wallet` | `WalletAPI` | 钱包 |
| `gate_api.futures` | `FuturesAPI` | 永续合约（settle 默认 `usdt`） |
| `gate_api.delivery` | `DeliveryAPI` | 交割合约（settle 默认 `usdt`） |
| `gate_api.subaccount` | `SubAccountAPI` | 子账户/托管 |
| `gate_api.trading_bot` | `TradingBotAPI` | 交易机器人（Bot/AIHub） |

> 合约类接口构造时可指定 `settle`（`usdt`/`btc`），如 `FuturesAPI(client, settle="usdt")`。

---

## 1. 现货 SpotAPI

| 方法 | HTTP 路径 | 功能 |
|------|-----------|------|
| `list_currencies()` | `GET /spot/currencies` | 查询所有币种信息 |
| `get_currency(currency)` | `GET /spot/currencies/{currency}` | 查询单个币种信息 |
| `list_currency_pairs()` | `GET /spot/currency_pairs` | 查询所有交易对规则（精度、最小下单量等） |
| `get_currency_pair(pair)` | `GET /spot/currency_pairs/{pair}` | 查询单个交易对规则 |
| `list_tickers(currency_pair=None)` | `GET /spot/tickers` | 查询行情 ticker（不传 pair 返回全部） |
| `list_accounts()` | `GET /spot/accounts` | 查询现货账户资产（可用/冻结） |
| `list_account_book(currency, limit, page)` | `GET /spot/account_book` | 查询现货账户账单流水 |
| `list_open_orders(page, limit)` | `GET /spot/open_orders` | 查询所有未成交挂单（按交易对汇总） |
| `list_orders(pair, status, limit, page)` | `GET /spot/orders` | 查询订单列表；`status=open/finished` |
| `get_order(order_id, pair)` | `GET /spot/orders/{order_id}` | 查询单个订单 |
| `list_my_trades(currency_pair, limit, page)` | `GET /spot/my_trades` | 查询个人成交历史 |
| `get_fee(currency_pair=None)` | `GET /spot/fee` | 查询交易手续费率 |
| `list_price_orders(status, limit)` | `GET /spot/price_orders` | 查询现货计划委托（止盈止损）订单 |

## 2. 钱包 WalletAPI

| 方法 | HTTP 路径 | 功能 |
|------|-----------|------|
| `get_total_balance(currency=None)` | `GET /wallet/total_balance` | 全账户总资产估值（默认折算 USDT，数据最多缓存 1 分钟） |
| `get_deposit_address(currency)` | `GET /wallet/deposit_address` | 查询币种充值地址（含多链地址） |
| `list_currency_chains(currency)` | `GET /wallet/currency_chains` | 查询币种支持的链及充提开关 |
| `list_deposits(limit)` | `GET /wallet/deposits` | 查询充值记录（默认近 7 天） |
| `list_withdrawals(limit)` | `GET /wallet/withdrawals` | 查询提现记录（默认近 7 天） |
| `get_withdraw_status(currency=None)` | `GET /wallet/withdraw_status` | 查询币种提现状态（费率、限额） |
| `list_sub_account_transfers(sub_uid, limit)` | `GET /wallet/sub_account_transfers` | 查询母子账户划转记录 |
| `list_push(transaction_type, limit)` | `GET /wallet/push` | 查询 UID 站内划转历史 |
| `list_sub_account_balances(limit, page)` | `GET /wallet/sub_account_balances` | 查询子账户余额（母账户视角） |
| `list_small_balance(currency=None)` | `GET /wallet/small_balance` | 查询可兑换的小额资产 |
| `list_small_balance_history(limit)` | `GET /wallet/small_balance_history` | 查询小额资产兑换历史 |
| `list_saved_address(currency, limit)` | `GET /wallet/saved_address` | 查询提现地址簿 |
| `get_fee(currency=None)` | `GET /wallet/fee` | 查询个人全业务费率（现货/合约/交割） |

## 3. 永续合约 FuturesAPI

| 方法 | HTTP 路径（`{settle}` 为结算币种） | 功能 |
|------|-----------|------|
| `list_contracts()` | `GET /futures/{settle}/contracts` | 查询全部合约信息 |
| `get_contract(contract)` | `GET /futures/{settle}/contracts/{contract}` | 查询单个合约（资金费率、风险限额等） |
| `list_tickers(contract=None)` | `GET /futures/{settle}/tickers` | 查询合约行情 |
| `get_account()` | `GET /futures/{settle}/accounts` | 查询合约账户资产 |
| `list_account_book(limit)` | `GET /futures/{settle}/account_book` | 查询合约账户流水 |
| `list_positions(limit)` | `GET /futures/{settle}/positions` | 查询当前持仓 |
| `get_position(contract)` | `GET /futures/{settle}/positions/{contract}` | 查询单合约持仓 |
| `list_orders(status, contract, limit)` | `GET /futures/{settle}/orders` | 查询订单；`status=open/finished` |
| `get_order(order_id)` | `GET /futures/{settle}/orders/{order_id}` | 查询单个订单 |
| `list_my_trades(contract, limit)` | `GET /futures/{settle}/my_trades` | 查询个人成交历史 |
| `list_position_close(contract, limit)` | `GET /futures/{settle}/position_close` | 查询平仓历史 |
| `list_liquidates(contract, limit)` | `GET /futures/{settle}/liquidates` | 查询强平历史 |
| `list_price_orders(status, contract, limit)` | `GET /futures/{settle}/price_orders` | 查询计划委托（止盈止损等） |

## 4. 交割合约 DeliveryAPI

| 方法 | HTTP 路径 | 功能 |
|------|-----------|------|
| `list_contracts()` | `GET /delivery/{settle}/contracts` | 查询全部交割合约 |
| `get_contract(contract)` | `GET /delivery/{settle}/contracts/{contract}` | 查询单个交割合约 |
| `list_tickers(contract=None)` | `GET /delivery/{settle}/tickers` | 查询行情 |
| `get_account()` | `GET /delivery/{settle}/accounts` | 查询账户资产 |
| `list_account_book(limit)` | `GET /delivery/{settle}/account_book` | 查询账户流水 |
| `list_positions(limit)` | `GET /delivery/{settle}/positions` | 查询持仓 |
| `get_position(contract)` | `GET /delivery/{settle}/positions/{contract}` | 查询单合约持仓 |
| `list_orders(status, contract, limit)` | `GET /delivery/{settle}/orders` | 查询订单；`status=open/finished`（open 时不支持 limit） |
| `list_my_trades(contract, limit)` | `GET /delivery/{settle}/my_trades` | 查询成交历史 |
| `list_position_close(contract, limit)` | `GET /delivery/{settle}/position_close` | 查询平仓历史 |
| `list_settlements(contract, limit)` | `GET /delivery/{settle}/settlements` | 查询交割结算历史 |
| `list_liquidates(contract, limit)` | `GET /delivery/{settle}/liquidates` | 查询强平历史 |
| `list_price_orders(status, contract, limit)` | `GET /delivery/{settle}/price_orders` | 查询计划委托 |

## 5. 子账户/托管 SubAccountAPI

Gate API Key 的"托管"权限对应母子账户托管场景：母账户通过 API 查询/管理其子账户。

| 方法 | HTTP 路径 | 功能 |
|------|-----------|------|
| `list_sub_accounts()` | `GET /sub_accounts` | 查询子账户列表 |
| `get_sub_account(user_id)` | `GET /sub_accounts/{user_id}` | 查询单个子账户信息 |
| `list_sub_account_keys(user_id)` | `GET /sub_accounts/{user_id}/keys` | 查询子账户 API Key 列表 |
| `list_sub_account_balances(sub_user_id, limit, page)` | `GET /wallet/sub_account_balances` | 查询子账户余额 |
| `list_sub_account_transfers(limit)` | `GET /wallet/sub_account_transfers` | 查询母子账户划转记录 |

## 6. 交易机器人 TradingBotAPI

Gate API v4 的 **Bot（AIHub）** 标签对应"交易机器人"权限。策略类型
`strategy_type` 取值：`spot_grid`（现货网格）、`margin_grid`（杠杆网格）、
`infinite_grid`（无限网格）、`futures_grid`（合约网格）、
`spot_martingale`（现货马丁格尔）、`contract_martingale`（合约马丁格尔）。

> 该模块返回结构为 `{code, message, data}` 信封，`code=200` 表示成功，
> 业务数据在 `data` 字段内（如 `data.items` / `data.total`）。

| 方法 | HTTP 路径 | 功能 |
|------|-----------|------|
| `list_strategy_recommend(market, strategy_type, limit)` | `GET /bot/strategy/recommend` | 查询 AI 推荐的机器人策略 |
| `list_running_bots(strategy_type, market, page, page_size)` | `GET /bot/portfolio/running` | 查询正在运行的机器人列表 |
| `get_bot_detail(strategy_id, strategy_type)` | `GET /bot/portfolio/detail` | 查询单个机器人详情 |

## 7. 跟单（Copy Trading）与托管（Custody）说明

- **跟单**：Gate API v4 **没有公开文档化的跟单 REST 接口**。API Key 权限枚举中
  存在 `copy` 范围，说明跟单接口属于内部/白名单接口，无法通过公开 API
  查询跟单账户或交易员信息，因此本项目不封装该模块。
- **托管**：同样没有独立的公开 custody 标签接口；对应能力是子账户托管
  （SubAccount + Wallet 子账户查询系列），已由 `SubAccountAPI` 覆盖。

---

## 可用性测试

`scripts/check_availability.py` 逐个调用上述只读接口并输出 ✅/❌ 报告：

- ✅ 接口返回 2xx；
- ⚠️ 账户状态导致的跳过（如合约账户从未入金，返回 `USER_NOT_FOUND`——属账户未开通而非接口不可用）；
- ❌ 真实的接口/权限/参数错误。

测试**只发起 GET 请求，绝不触发任何买卖或资金变动**。

## 常见错误说明

| 错误 label | 含义 |
|-----------|------|
| `USER_NOT_FOUND` | 对应账户（如合约账户）尚未创建，需先划转资金开通 |
| `INVALID_PARAM_VALUE` | 参数不合法（如不支持的 limit） |
| `INVALID_KEY` / `INVALID_SIGNATURE` | 密钥或签名错误，检查环境变量 |
| `FORBIDDEN` | API Key 未开通对应权限，需在 Gate 控制台勾选 |
