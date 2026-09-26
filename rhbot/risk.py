"""Fail-closed checks that run before every simulated fill."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from rhbot.config import ALLOWED_SYMBOLS, Settings
from rhbot.errors import OrderRejected
from rhbot.models import RISK_REDUCTION_REASONS, OrderIntent, Quote
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
    overlay_state: str = "NONE"
    opened_at: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class LimitHit:
    """One risk check that failed, and the limit it was measured against."""

    reason: str
    limit_name: str
    limit: str
    observed: str = ""


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str]
    kill: bool = False
    breaches: tuple[LimitHit, ...] = ()


def _text(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return ""
    return str(value)


def deny(
    code: str,
    limit_name: str,
    limit: object,
    observed: object = "",
    *,
    kill: bool = False,
    detail: str | None = None,
) -> RiskDecision:
    """A denial. ``code`` is the stable reason. ``detail`` is the sentence stored beside it."""
    return RiskDecision(
        False,
        [detail or code],
        kill=kill,
        breaches=(LimitHit(code, limit_name, _text(limit), _text(observed)),),
    )


def _decision(hit: LimitHit, *, kill: bool = False) -> RiskDecision:
    return RiskDecision(False, [hit.reason], kill=kill, breaches=(hit,))


def _ratio(numerator: Decimal, denominator: Decimal) -> str:
    if denominator <= 0:
        return ""
    return format(q8(numerator / denominator), "f")


def peak_drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    if peak <= 0 or equity >= peak:
        return Decimal(0)
    return (peak - equity) / peak


def daily_loss(equity: Decimal, day_start: Decimal) -> Decimal:
    if day_start <= 0 or equity >= day_start:
        return Decimal(0)
    return (day_start - equity) / day_start


def signed_drawdown(equity: Decimal, peak: Decimal) -> Decimal:
    if peak <= 0:
        return Decimal(0)
    return equity / peak - Decimal(1)


def kill_reason(equity: Decimal, peak: Decimal, settings: Settings) -> str | None:
    """40% paper-only hard kill on one strategy book's mark-to-bid equity.

    These drawdown limits must not be carried into a live phase.
    """
    dd = signed_drawdown(equity, peak)
    if dd <= -settings.kill_drawdown_pct:
        return f"max_drawdown {q8(dd)} <= -{settings.kill_drawdown_pct}"
    return None


def daily_buy_block(equity: Decimal, day_start: Decimal, settings: Settings) -> str | None:
    loss = daily_loss(equity, day_start)
    if loss >= settings.max_daily_loss_pct:
        return f"daily_loss {q8(loss)} >= {settings.max_daily_loss_pct}"
    return None


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
        ignore_overlay: bool = False,
    ) -> RiskDecision:
        try:
            return self._evaluate(
                intent,
                ctx,
                client_order_id,
                reduce_only=reduce_only,
                ignore_overlay=ignore_overlay,
            )
        except OrderRejected as exc:
            code = exc.reasons[0] if exc.reasons else "rejected"
            return deny(code, code, "deny", kill=exc.kill)
        except Exception as exc:
            return deny(
                "fail_closed",
                "fail_closed",
                "deny",
                type(exc).__name__,
                detail=f"fail_closed:{type(exc).__name__}",
            )

    def _evaluate(
        self,
        intent: OrderIntent,
        ctx: RiskContext,
        client_order_id: str,
        *,
        reduce_only: bool,
        ignore_overlay: bool = False,
    ) -> RiskDecision:
        settings = self.settings
        if client_order_id in ctx.known_client_ids:
            return deny("duplicate_client_order_id", "client_order_id", "unique")

        if intent.symbol not in ALLOWED_SYMBOLS or intent.symbol not in settings.symbols:
            return deny("symbol_not_allowed", "symbols", ",".join(ALLOWED_SYMBOLS), intent.symbol)

        if intent.side not in ("buy", "sell"):
            return deny("side_not_allowed", "side", "buy,sell", intent.side)

        position = ctx.positions.get(intent.symbol, Decimal(0))
        if intent.side == "sell":
            if intent.base_quantity is None or intent.base_quantity <= 0:
                return deny("sell_quantity", "base_quantity", "positive")
            if q8(intent.base_quantity) > q8(position):
                return deny("short_not_allowed", "allow_short", "false", intent.base_quantity)
        elif intent.side == "buy":
            if reduce_only:
                return deny("reduce_only_sells", "reduce_only", "sell")
            if intent.quote_amount is None or intent.quote_amount <= 0:
                return deny("buy_amount", "quote_amount", "positive")

        quote = ctx.quotes.get(intent.symbol)
        fresh = self._fresh(quote, ctx.now)
        if fresh is not None:
            return _decision(fresh)
        assert quote is not None
        spread = self._spread(quote)
        if spread is not None:
            return _decision(spread)

        # The no-overlay shadow book skips only these two drawdown controls.
        if not ignore_overlay and kill_active(settings.state_dir) and not reduce_only:
            return deny("kill_switch", "kill_switch", "engaged")
        if not ignore_overlay and ctx.overlay_state == "KILLED" and not reduce_only:
            return deny("killed", "overlay_state", "KILLED", ctx.overlay_state)

        if reduce_only:
            return RiskDecision(True, [])

        if intent.symbol in ctx.ordered_symbols_today:
            return deny("one_order_per_symbol_per_bar", "orders_per_symbol_per_bar", "1", intent.symbol)

        if intent.side == "buy":
            # Paper-only freeze. Sells above this check still go through.
            # Weekly DCA buys are new entries and are blocked with every other buy.
            # The engine raises the freeze from combined peak equity.
            if not ignore_overlay and ctx.overlay_state == "FROZEN":
                return deny(
                    "freeze",
                    "freeze_drawdown_pct",
                    settings.freeze_drawdown_pct,
                    "frozen",
                )
            loss = daily_loss(ctx.equity, ctx.day_start_equity)
            if loss >= settings.max_daily_loss_pct:
                return deny(
                    "daily_loss",
                    "max_daily_loss_pct",
                    settings.max_daily_loss_pct,
                    q8(loss),
                    detail=f"daily_loss {q8(loss)} >= {settings.max_daily_loss_pct}",
                )
            assert intent.quote_amount is not None
            if intent.quote_amount < settings.min_order_notional:
                return deny(
                    "below_min_notional",
                    "min_order_notional",
                    settings.min_order_notional,
                    intent.quote_amount,
                )
            if intent.quote_amount > ctx.cash + EPS:
                return deny("insufficient_cash", "cash", ctx.cash, intent.quote_amount)
            # Cap the coin's mid value, not the cash spent. The cash spent is
            # higher by the per-side cost, and blocking that would make a 50%
            # position impossible.
            qty, _px, _cash, _cost, notional = plan_fill(intent, quote, settings.cost_per_side)
            limit = settings.max_total_exposure_pct
            trade_cap_pct = min(settings.max_position_pct, limit)
            cap = ctx.equity * trade_cap_pct + EPS
            if notional > cap:
                return deny("per_trade_cap", "per_trade_cap_pct", trade_cap_pct, _ratio(notional, ctx.equity))
            projected = (position + qty) * quote.mid
            if projected > cap:
                return deny("position_cap", "max_position_pct", trade_cap_pct, _ratio(projected, ctx.equity))
            exposure = self._exposure_after_buy(ctx, intent.symbol, projected)
            exposure_cap = ctx.equity * limit + EPS
            if exposure > exposure_cap:
                return deny("exposure_cap", "max_total_exposure_pct", limit, _ratio(exposure, ctx.equity))
            turnover_cap = ctx.day_start_equity * settings.max_daily_turnover_pct + EPS
            if ctx.turnover_today + intent.quote_amount > turnover_cap:
                return deny(
                    "turnover_cap",
                    "max_daily_turnover_pct",
                    settings.max_daily_turnover_pct,
                    intent.quote_amount,
                )
            if ctx.trades_today >= settings.max_trades_per_day:
                return deny(
                    "max_trades_per_day",
                    "max_trades_per_day",
                    settings.max_trades_per_day,
                    ctx.trades_today,
                )
            return RiskDecision(True, [])

        # Strategy sells still count as trades. Caps do not block a shrink.
        # The 7-day hold is a hard cap here. Flatten reasons skip this function
        # via reduce_only above.
        assert intent.base_quantity is not None
        if intent.reason not in RISK_REDUCTION_REASONS:
            opened = ctx.opened_at.get(intent.symbol)
            if opened is not None:
                age_days = Decimal(str((ctx.now - opened).total_seconds())) / Decimal(86400)
                if age_days < settings.min_hold_days:
                    return deny(
                        "min_hold",
                        "min_hold_days",
                        settings.min_hold_days,
                        q8(age_days),
                    )
        notional = q8(intent.base_quantity * quote.mid)
        if notional < settings.min_order_notional:
            return deny(
                "below_min_notional",
                "min_order_notional",
                settings.min_order_notional,
                notional,
            )
        if ctx.trades_today >= settings.max_trades_per_day:
            return deny(
                "max_trades_per_day",
                "max_trades_per_day",
                settings.max_trades_per_day,
                ctx.trades_today,
            )
        return RiskDecision(True, [])

    def _fresh(self, quote: Quote | None, now: datetime) -> LimitHit | None:
        if quote is None:
            return LimitHit("missing_quote", "quote", "required", "missing")
        if not quote.ts_trusted:
            return LimitHit("untrusted_quote_ts", "quote_ts", "market", "untrusted")
        if quote.ts.tzinfo is None or now.tzinfo is None:
            return LimitHit("naive_timestamp", "quote_ts", "timezone-aware", "naive")
        age = (now - quote.ts).total_seconds()
        if age < -5:
            return LimitHit("quote_from_the_future", "max_quote_age_seconds", "0", str(int(age)))
        if age > self.settings.max_quote_age_seconds:
            return LimitHit(
                "stale_quote",
                "max_quote_age_seconds",
                str(self.settings.max_quote_age_seconds),
                str(int(age)),
            )
        return None

    def _spread(self, quote: Quote) -> LimitHit | None:
        if quote.bid is None or quote.ask is None:
            return None
        if quote.ask < quote.bid:
            return LimitHit("crossed_quote", "spread", "bid<=ask", "crossed")
        if quote.mid <= 0:
            return LimitHit("bad_mid", "mid", "positive", _text(quote.mid))
        buy_side = (quote.ask - quote.mid) / quote.mid
        sell_side = (quote.mid - quote.bid) / quote.mid
        wider = max(buy_side, sell_side)
        if wider > self.settings.max_spread_per_side:
            return LimitHit(
                "spread_too_wide",
                "max_spread_per_side",
                _text(self.settings.max_spread_per_side),
                _text(q8(wider)),
            )
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
