"""Stub. There is no live trading path in this package."""

from __future__ import annotations

from rhbot.errors import LiveTradingDisabled


_MESSAGE = (
    "Live trading is disabled. Real money requires Randy's written approval "
    "and is not implemented."
)


class LiveBroker:
    """Every method raises. This class does not open a network connection.

    There is no constructor flag, config key, or environment variable that
    makes these methods send an order. The engine never instantiates this class.
    """

    def submit(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabled(_MESSAGE)

    def cancel(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabled(_MESSAGE)

    def amend(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabled(_MESSAGE)
