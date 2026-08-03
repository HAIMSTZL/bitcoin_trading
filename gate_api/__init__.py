"""Gate API v4 封装（只读场景为主）。

密钥从环境变量 MY_GATE_KEY / MY_GATE_SECRET 读取，严禁硬编码。

用法::

    from gate_api import GateClient
    from gate_api.spot import SpotAPI

    client = GateClient()
    spot = SpotAPI(client)
    print(spot.list_accounts())
"""

from .client import GateApiError, GateClient
from .delivery import DeliveryAPI
from .futures import FuturesAPI
from .spot import SpotAPI
from .subaccount import SubAccountAPI
from .trading_bot import TradingBotAPI
from .wallet import WalletAPI

__all__ = [
    "GateClient",
    "GateApiError",
    "SpotAPI",
    "FuturesAPI",
    "DeliveryAPI",
    "WalletAPI",
    "SubAccountAPI",
    "TradingBotAPI",
]
