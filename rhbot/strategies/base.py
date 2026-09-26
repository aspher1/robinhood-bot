"""Strategy interface. A strategy returns intents. It does not touch the ledger."""

from __future__ import annotations

from typing import Protocol

from rhbot.models import Fill, MarketSnapshot, OrderIntent


class Strategy(Protocol):
    name: str

    def initial_state(self) -> dict: ...

    def decide(
        self,
        view: MarketSnapshot,
        state: dict,
        positions: dict,
        cash: object,
        equity: object,
        now: object,
    ) -> tuple[list[OrderIntent], dict, str]: ...

    def commit(
        self,
        state: dict,
        fills: list[Fill],
        positions: dict,
        now: object,
    ) -> dict: ...
