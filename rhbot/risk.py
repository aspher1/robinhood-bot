"""Fail-closed checks that run before every simulated fill."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from rhbot.config import ALLOWED_SYMBOLS, Settings
from rhbot.errors import OrderRejected
from rhbot.models import OrderIntent, Quote
from rhbot.money import q8
from rhbot.ops import kill_active
from rhbot.pricing import plan_fill

EPS = Decimal("0.01")


@dataclass
class RiskContext:
    now: datetime
    sleeve: str
    equity: Decimal
    cash: Decimal
    positions: dict[str, Decimal]
    quotes: dict[str, Quote]
    day_start_equity: Decimal
    peak_equity: Decimal
    trades_today: int
    turnover_today: Decimal
    known_client_ids: set[str]
    ordered_symbols_today: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str]
    kill: bool = False


def peak_drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    if peak <= 0 or equity >= peak:
        return Decimal(0)
    return (peak - equity) / peak


def daily_loss(equity: Decimal, day_start: Decimal) -> Decimal:
    if day_start <= 0 or equity >= day_start:
        return Decimal(0)
    return (day_start - equity) / day_start


def kill_reason(equity: Decimal, peak: Decimal, settings: Settings) -> str | None:
    dd = peak_drawdown(equity, peak)
    if dd >= settings.max_drawdown_pct:
        return f"max_drawdown {q8(dd)} >= {settings.max_drawdown_pct}"
    return None


def daily_buy_block(equity: Decimal, day_start: Decimal, settings: Settings) -> str | None:
    loss = daily_loss(equity, day_start)
    if loss >= settings.max_daily_loss_pct:
        return f"daily_loss {q8(loss)} >= {settings.max_daily_loss_pct}"
    return None


def exposure_limit_pct(settings: Settings, equity: Decimal, peak: Decimal) -> Decimal:
    """Total exposure allowed right now, as a fraction of equity."""
    cap = settings.max_total_exposure_pct
    dd = peak_drawdown(equity, peak)
    if dd >= settings.dd_cut_quarter:
        cap = min(cap, settings.exposure_cap_at_quarter)
    elif dd >= settings.dd_cut_half:
        cap = min(cap, settings.exposure_cap_at_half)
    return cap


def target_sell_notional(
    position_value: Decimal,
    equity: Decimal,
    cap_pct: Decimal,
    cost_per_side: Decimal,
) -> Decimal:
    """Mid notional to sell so the book left behind fits the cap after the sell cost."""
    if position_value <= 0:
        return Decimal(0)
    if cap_pct <= 0:
        return position_value
    allowed = equity * cap_pct
    if position_value <= allowed:
        return Decimal(0)
    denom = Decimal(1) - (cap_pct * cost_per_side)
    if denom <= 0:
        return position_value
    return (position_value - allowed) / denom


class RiskEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(
        self,
        intent: OrderIntent,
        ctx: RiskContext,
        client_order_id: str,
        *,
        reduce_only: bool = False,
    ) -> RiskDecision:
        try:
            return self._evaluate(intent, ctx, client_order_id, reduce_only=reduce_only)
        except OrderRejected as exc:
            return RiskDecision(False, exc.reasons, kill=exc.kill)
        except Exception as exc:
            return RiskDecision(False, [f"fail_closed:{type(exc).__name__}"], kill=False)

    def _evaluate(
        self,
        intent: OrderIntent,
        ctx: RiskContext,
        client_order_id: str,
        *,
        reduce_only: bool,
    ) -> RiskDecision:
        settings = self.settings
        if client_order_id in ctx.known_client_ids:
            return RiskDecision(False, ["duplicate_client_order_id"])

        if intent.symbol not in ALLOWED_SYMBOLS or intent.symbol not in settings.symbols:
            return RiskDecision(False, ["symbol_not_allowed"])

        if intent.side not in ("buy", "sell"):
            return RiskDecision(False, ["side_not_allowed"])

        position = ctx.positions.get(intent.symbol, Decimal(0))
        if intent.side == "sell":
            if intent.base_quantity is None or intent.base_quantity <= 0:
                return RiskDecision(False, ["sell_quantity"])
            if q8(intent.base_quantity) > q8(position):
                return RiskDecision(False, ["short_not_allowed"])
        elif intent.side == "buy":
            if reduce_only:
                return RiskDecision(False, ["reduce_only_sells"])
            if intent.quote_amount is None or intent.quote_amount <= 0:
                return RiskDecision(False, ["buy_amount"])

        quote = ctx.quotes.get(intent.symbol)
        fresh = self._fresh(quote, ctx.now)
        if fresh is not None:
            return RiskDecision(False, [fresh])
        assert quote is not None
        spread = self._spread(quote)
        if spread is not None:
            return RiskDecision(False, [spread])

        if kill_active(settings.state_dir) and not reduce_only:
            return RiskDecision(False, ["kill_switch"])

        if reduce_only:
            return RiskDecision(True, [])

        breach = kill_reason(ctx.equity, ctx.peak_equity, settings)
        if breach:
            return RiskDecision(False, [breach], kill=True)

        if intent.symbol in ctx.ordered_symbols_today:
            return RiskDecision(False, ["one_order_per_symbol_per_bar"])

        if intent.side == "buy":
            blocked = daily_buy_block(ctx.equity, ctx.day_start_equity, settings)
            if blocked:
                return RiskDecision(False, [blocked])
            assert intent.quote_amount is not None
            if intent.quote_amount < settings.min_order_notional:
                return RiskDecision(False, ["below_min_notional"])
            if intent.quote_amount > ctx.cash + EPS:
                return RiskDecision(False, ["insufficient_cash"])
            # Cap the coin's mid value, not the cash spent. The cash spent is
            # higher by the per-side cost, and blocking that would make a 50%
            # position impossible.
            qty, _px, _cash, _cost, notional = plan_fill(intent, quote, settings.cost_per_side)
            limit = exposure_limit_pct(settings, ctx.equity, ctx.peak_equity)
            trade_cap_pct = min(settings.max_position_pct, limit)
            cap = ctx.equity * trade_cap_pct + EPS
            if notional > cap:
                return RiskDecision(False, ["per_trade_cap"])
            projected = (position + qty) * quote.mid
            if projected > cap:
                return RiskDecision(False, ["position_cap"])
            exposure = self._exposure_after_buy(ctx, intent.symbol, projected)
            exposure_cap = ctx.equity * limit + EPS
            if exposure > exposure_cap:
                return RiskDecision(False, ["exposure_cap"])
            turnover_cap = ctx.day_start_equity * settings.max_daily_turnover_pct + EPS
            if ctx.turnover_today + intent.quote_amount > turnover_cap:
                return RiskDecision(False, ["turnover_cap"])
            if ctx.trades_today >= settings.max_trades_per_day:
                return RiskDecision(False, ["max_trades_per_day"])
            return RiskDecision(True, [])

        # Strategy sells still count as trades. Caps do not block a shrink.
        assert intent.base_quantity is not None
        notional = q8(intent.base_quantity * quote.mid)
        if notional < settings.min_order_notional:
            return RiskDecision(False, ["below_min_notional"])
        if ctx.trades_today >= settings.max_trades_per_day:
            return RiskDecision(False, ["max_trades_per_day"])
        return RiskDecision(True, [])

    def _fresh(self, quote: Quote | None, now: datetime) -> str | None:
        if quote is None:
            return "missing_quote"
        if quote.ts.tzinfo is None or now.tzinfo is None:
            return "naive_timestamp"
        age = (now - quote.ts).total_seconds()
        if age < -5:
            return "quote_from_the_future"
        if age > self.settings.max_quote_age_seconds:
            return "stale_quote"
        return None

    def _spread(self, quote: Quote) -> str | None:
        if quote.bid is None or quote.ask is None:
            return None
        if quote.ask < quote.bid:
            return "crossed_quote"
        if quote.mid <= 0:
            return "bad_mid"
        buy_side = (quote.ask - quote.mid) / quote.mid
        sell_side = (quote.mid - quote.bid) / quote.mid
        if buy_side > self.settings.max_spread_per_side or sell_side > self.settings.max_spread_per_side:
            return "spread_too_wide"
        return None

    def _exposure_after_buy(self, ctx: RiskContext, symbol: str, projected_value: Decimal) -> Decimal:
        total = Decimal(0)
        symbols = set(ctx.positions) | {symbol}
        for name in symbols:
            mark = ctx.quotes.get(name)
            if mark is None:
                raise OrderRejected(["missing_quote"])
            if name == symbol:
                total += projected_value
            else:
                total += ctx.positions.get(name, Decimal(0)) * mark.mid
        return total
