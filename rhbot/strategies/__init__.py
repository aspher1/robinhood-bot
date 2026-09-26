"""Three sleeves. Each one has its own cash and positions."""

from rhbot.config import Settings
from rhbot.strategies.buyhold import BuyAndHold
from rhbot.strategies.dca import DcaWeekly
from rhbot.strategies.trend import TrendDaily


def build_strategies(settings: Settings) -> list:
    return [
        BuyAndHold(settings),
        DcaWeekly(settings),
        TrendDaily(settings),
    ]


__all__ = ["BuyAndHold", "DcaWeekly", "TrendDaily", "build_strategies"]
