"""历史行情研究系统：证券身份、交易日历、双时态行情版本。

核心概念：

- 业务时间（business time / as_of_date）：行情实际发生的交易日。
- 收到时间（received_at / txn time）：数据商把这条数据送到本系统的时间。
- 任何修订都是“追加一条新版本”，从不覆盖旧值，因此可以在事后重放
  “某一时点实际能看到的数据”，回测不会误用后来才补齐或更正的价格。
"""

from .calendar import TradingCalendar
from .dataset import MarketDataset, open_dataset
from .instruments import Instrument
from .quotes import Quote, QuoteVersion

__all__ = [
    "Instrument",
    "TradingCalendar",
    "MarketDataset",
    "open_dataset",
    "Quote",
    "QuoteVersion",
]

__version__ = "0.1.0"
