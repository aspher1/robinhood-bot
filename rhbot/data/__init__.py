"""Market-data adapters. v1 paper quotes are Coinbase public bid/ask."""

from rhbot.data.public import PublicMarketData
from rhbot.data.robinhood import RobinhoodMarketData

__all__ = ["PublicMarketData", "RobinhoodMarketData"]
