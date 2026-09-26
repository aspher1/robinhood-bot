"""One cycle: mark, check drawdown, ask each strategy, risk-check, paper-fill."""

from __future__ import annotations

import json
import signal
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from rhbot.brokers.paper import PaperBroker
from rhbot.config import Settings, reject_live_env
from rhbot.data.public import PublicMarketData
from rhbot.data.robinhood import RobinhoodMarketData
from rhbot.errors import DataError, OrderRejected
from rhbot.ledger import Ledger
from rhbot.models import Fill, MarketSnapshot, OrderIntent
from rhbot.money import D, money_str, q8
from rhbot.pricing import plan_fill
from rhbot.ops import (
    engage_freeze,
    engage_kill,
    freeze_active,
    iso,
    kill_active,
    read_kill,
    sd_notify,
    utcnow,
    write_heartbeat,
)
from rhbot.risk import RiskContext, RiskEngine, kill_reason, peak_drawdown
from rhbot.strategies import build_strategies


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc)


class Engine:
    def __init__(self, settings: Settings):
        reject_live_env()
        if settings.mode != "paper":
            from rhbot.errors import ConfigError

            raise ConfigError("mode must be paper")
        self.settings = settings
        self.ledger = Ledger(settings)
        self.risk = RiskEngine(settings)
        self.broker = PaperBroker(settings, self.ledger, self.risk)
        self.strategies = build_strategies(settings)
        self.public = PublicMarketData(settings.public_provider)
        self.quotes = self._select_quotes()

    def _select_quotes(self):
        if self.settings.market_data == "robinhood":
            robinhood = RobinhoodMarketData.from_env()
            if robinhood is not None:
                self.ledger.set_meta("quote_source", "robinhood")
                return robinhood
            self.ledger.set_meta("quote_source", "public_fallback")
            return self.public
        self.ledger.set_meta("quote_source", self.settings.public_provider)
        return self.public

    def run_once(self, now: datetime | None = None, snapshot: MarketSnapshot | None = None) -> dict:
        market_now = ensure_utc(now or utcnow())
        wall = utcnow()
        try:
            if snapshot is None:
                snapshot = self.load_market()
            summary = self.cycle(market_now, wall, snapshot)
            self.ledger.set_meta("consecutive_failures", "0")
            self.ledger.set_meta("last_loop_ok_at", iso(wall))
            self.ledger.set_meta("last_successful_action_at", iso(wall))
            self.ledger.set_meta("last_error", "")
            return summary
        except Exception as exc:
            self.ledger.bump("error_loop")
            failures = self.ledger.meta_int("consecutive_failures") + 1
            self.ledger.set_meta("consecutive_failures", str(failures))
            self.ledger.set_meta("last_error", f"{type(exc).__name__}: {exc}"[:400])
            raise
        finally:
            write_heartbeat(self.settings.state_dir, self.heartbeat_body())

    def load_market(self) -> MarketSnapshot:
        provider = self.settings.public_provider
        bars: dict = {}
        try:
            for symbol in self.settings.symbols:
                fetched = self.public.fetch_daily_bars(symbol)
                self.ledger.upsert_candles(fetched)
                bars[symbol] = fetched
        except Exception:
            self.ledger.bump("error_fetch")
            bars = {}
            for symbol in self.settings.symbols:
                cached = self.ledger.load_candles(symbol, provider)
                if not cached:
                    raise
                bars[symbol] = cached
        try:
            quotes = self.quotes.fetch_quotes(list(self.settings.symbols))
        except Exception:
            self.ledger.bump("error_fetch")
            raise
        return MarketSnapshot(bars=bars, quotes=quotes, source=getattr(self.quotes, "name", provider))

    def cycle(self, market_now: datetime, wall: datetime, snapshot: MarketSnapshot) -> dict:
        self._require_quotes(snapshot)
        self._note_quotes(snapshot, market_now, wall)
        if kill_active(self.settings.state_dir):
            self.ledger.cancel_open_orders(market_now, "kill_switch")
        _equity, portfolio_peak = self._enforce_loss_policy(market_now, snapshot)
        equities: dict[str, str] = {}
        for strategy in self.strategies:
            equity = self._run_sleeve(strategy, market_now, snapshot)
            self._run_shadow_sleeve(strategy, market_now, snapshot)
            equities[strategy.name] = money_str(equity)
        self.ledger.set_meta("last_decision_at", iso(wall))
        self.ledger.log_event(
            "cycle",
            {
                "equities": equities,
                "portfolio_peak": money_str(portfolio_peak),
                "kill_switch": kill_active(self.settings.state_dir),
                "drawdown_freeze": freeze_active(self.settings.state_dir),
            },
            market_now,
        )
        self.ledger.set_meta("audit_head", self.ledger.head_hash())
        return {
            "ts": iso(market_now),
            "kill_switch": kill_active(self.settings.state_dir),
            "drawdown_freeze": freeze_active(self.settings.state_dir),
            "equities": equities,
            "portfolio_peak": money_str(portfolio_peak),
            "quote_source": snapshot.source,
        }

    def flatten(
        self,
        now: datetime | None = None,
        snapshot: MarketSnapshot | None = None,
        *,
        reason: str = "flatten",
    ) -> dict:
        market_now = ensure_utc(now or utcnow())
        if snapshot is None:
            snapshot = self.load_market()
        self._require_quotes(snapshot)
        self._note_quotes(snapshot, market_now, utcnow())
        fills_out = []
        errors = []
        for strategy in self.strategies:
            self.ledger.ensure_sleeve(strategy.name, market_now, strategy.initial_state())
            sleeve_fills = []
            for symbol, qty in list(self.ledger.positions(strategy.name).items()):
                intent = OrderIntent(symbol, "sell", reason, base_quantity=qty)
                client_order_id = self._client_id(strategy.name, intent, market_now)
                try:
                    fill = self.broker.submit(
                        strategy.name,
                        intent,
                        client_order_id,
                        self._context(strategy.name, snapshot, market_now),
                        market_now,
                        reduce_only=True,
                    )
                except OrderRejected as exc:
                    errors.append(
                        {"sleeve": strategy.name, "symbol": symbol, "reasons": exc.reasons}
                    )
                    continue
                sleeve_fills.append(fill)
                fills_out.append(fill.event_payload())
            positions = self.ledger.positions(strategy.name)
            state = self.ledger.strategy_state(strategy.name) or strategy.initial_state()
            self.ledger.save_strategy_state(
                strategy.name, strategy.commit(state, sleeve_fills, positions, market_now)
            )
            equity = self.mark(strategy.name, snapshot)
            self.ledger.mark_equity(strategy.name, equity, market_now)
            self.ledger.snapshot(strategy.name, equity, market_now)
        self.ledger.log_event(
            "flatten",
            {"fills": len(fills_out), "errors": len(errors)},
            market_now,
        )
        self.ledger.set_meta("last_successful_action_at", iso(utcnow()))
        write_heartbeat(self.settings.state_dir, self.heartbeat_body())
        return {"fills": fills_out, "errors": errors}

    def serve(self, *, once: bool = False, sleep=time.sleep) -> None:
        sd_notify("READY=1")
        stop = {"flag": False}

        def _stop(signum, frame):
            del signum, frame
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        while not stop["flag"]:
            try:
                summary = self.run_once()
                print(json.dumps({"event": "loop", "ok": True, **summary}), flush=True)
            except Exception as exc:
                print(
                    json.dumps({"event": "loop", "ok": False, "error": f"{type(exc).__name__}: {exc}"}),
                    flush=True,
                )
            sd_notify("WATCHDOG=1")
            if once or stop["flag"]:
                break
            sleep(self.settings.loop_seconds)

    def mark(self, sleeve: str, snapshot: MarketSnapshot):
        equity = self.ledger.cash(sleeve)
        for symbol, qty in self.ledger.positions(sleeve).items():
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                raise RuntimeError(f"no quote to mark {symbol}")
            equity += qty * quote.mid
        return q8(equity)

    def heartbeat_body(self) -> dict:
        return {
            "kill_switch": kill_active(self.settings.state_dir),
            "drawdown_freeze": freeze_active(self.settings.state_dir),
            "last_decision_at": self.ledger.get_meta("last_decision_at"),
            "last_quote_ok_at": self.ledger.get_meta("last_quote_ok_at"),
            "last_loop_ok_at": self.ledger.get_meta("last_loop_ok_at"),
            "last_successful_action_at": self.ledger.get_meta("last_successful_action_at"),
            "last_quote_ts": self.ledger.get_meta("last_quote_ts"),
            "last_auth_ok_at": self.ledger.get_meta("last_auth_ok_at"),
            "last_error": self.ledger.get_meta("last_error") or "",
            "audit_head": self.ledger.head_hash(),
            "quote_source": self.ledger.get_meta("quote_source") or "",
            "consecutive_failures": self.ledger.meta_int("consecutive_failures"),
        }

    def _run_sleeve(self, strategy, market_now: datetime, snapshot: MarketSnapshot):
        self.ledger.ensure_sleeve(strategy.name, market_now, strategy.initial_state())
        equity = self.mark(strategy.name, snapshot)
        self.ledger.mark_equity(strategy.name, equity, market_now)
        state = self.ledger.strategy_state(strategy.name) or strategy.initial_state()
        positions = self.ledger.positions(strategy.name)
        cash = self.ledger.cash(strategy.name)
        if kill_active(self.settings.state_dir):
            orders: list[OrderIntent] = []
            new_state = state
            reason = "kill_switch"
        else:
            orders, new_state, reason = strategy.decide(
                snapshot, state, positions, cash, equity, market_now
            )
        self.ledger.log_event(
            "decision",
            {
                "sleeve": strategy.name,
                "reason": reason,
                "orders": [_intent_payload(item) for item in orders],
            },
            market_now,
        )
        filled = []
        for intent in orders:
            if kill_active(self.settings.state_dir):
                break
            client_order_id = self._client_id(strategy.name, intent, market_now)
            try:
                fill = self.broker.submit(
                    strategy.name,
                    intent,
                    client_order_id,
                    self._context(strategy.name, snapshot, market_now),
                    market_now,
                )
            except OrderRejected as exc:
                if exc.kill:
                    equity, peak = self._mark_portfolio(market_now, snapshot)
                    self._trip_drawdown(exc.reasons[0], market_now, snapshot, equity=equity, peak=peak)
                continue
            filled.append(fill)
        positions = self.ledger.positions(strategy.name)
        self.ledger.save_strategy_state(
            strategy.name, strategy.commit(new_state, filled, positions, market_now)
        )
        equity = self.mark(strategy.name, snapshot)
        self.ledger.mark_equity(strategy.name, equity, market_now)
        self.ledger.snapshot(strategy.name, equity, market_now)
        return equity

    def _run_shadow_sleeve(self, strategy, market_now: datetime, snapshot: MarketSnapshot) -> None:
        """Same strategy, without the 10% freeze or the 40% kill.

        Other risk checks still apply. This book is the buy-and-hold benchmark
        and the report's no-overlay ledger. It is never flattened by a kill.
        """
        self.ledger.ensure_shadow_sleeve(strategy.name, market_now, strategy.initial_state())
        equity = self.shadow_mark(strategy.name, snapshot)
        self.ledger.mark_shadow_equity(strategy.name, equity, market_now)
        state = self.ledger.shadow_strategy_state(strategy.name) or strategy.initial_state()
        positions = self.ledger.shadow_positions(strategy.name)
        cash = self.ledger.shadow_cash(strategy.name)
        orders, new_state, _reason = strategy.decide(
            snapshot, state, positions, cash, equity, market_now
        )
        filled: list[Fill] = []
        for intent in orders:
            client_order_id = "shadow-" + self._client_id(strategy.name, intent, market_now)
            decision = self.risk.evaluate(
                intent,
                self._shadow_context(strategy.name, snapshot, market_now),
                client_order_id,
                ignore_overlay=True,
            )
            if not decision.allowed:
                continue
            quote = snapshot.quotes.get(intent.symbol)
            if quote is None:
                continue
            try:
                qty, price, cash_delta, cost, notional = plan_fill(
                    intent, quote, self.settings.cost_per_side
                )
            except DataError:
                continue
            qty_delta = qty if intent.side == "buy" else -qty
            fill = Fill(
                sleeve=strategy.name,
                symbol=intent.symbol,
                side=intent.side,
                qty=qty,
                qty_delta=qty_delta,
                mid=quote.mid,
                fill_price=price,
                cash_delta=cash_delta,
                cost=cost,
                notional=notional,
                ts=market_now,
                client_order_id=client_order_id,
                reason=intent.reason,
            )
            try:
                self.ledger.commit_shadow_fill(fill)
            except OrderRejected:
                continue
            filled.append(fill)
        positions = self.ledger.shadow_positions(strategy.name)
        self.ledger.save_shadow_strategy_state(
            strategy.name, strategy.commit(new_state, filled, positions, market_now)
        )
        equity = self.shadow_mark(strategy.name, snapshot)
        self.ledger.mark_shadow_equity(strategy.name, equity, market_now)
        self.ledger.shadow_snapshot(strategy.name, equity, market_now)

    def shadow_mark(self, sleeve: str, snapshot: MarketSnapshot):
        equity = self.ledger.shadow_cash(sleeve)
        for symbol, qty in self.ledger.shadow_positions(sleeve).items():
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                raise RuntimeError(f"no quote to mark shadow {symbol}")
            equity += qty * quote.mid
        return q8(equity)

    def _shadow_context(self, sleeve: str, snapshot: MarketSnapshot, market_now: datetime) -> RiskContext:
        row = self.ledger.shadow_sleeve_row(sleeve)
        day = market_now.date().isoformat()
        _fills, turnover = self.ledger.shadow_activity_today(sleeve, day)
        return RiskContext(
            now=market_now,
            sleeve=sleeve,
            equity=self.shadow_mark(sleeve, snapshot),
            cash=self.ledger.shadow_cash(sleeve),
            positions=self.ledger.shadow_positions(sleeve),
            quotes=snapshot.quotes,
            day_start_equity=q8(row["day_start_equity"]),
            peak_equity=q8(row["last_equity"]),
            trades_today=self.ledger.shadow_strategy_trades_today(day),
            turnover_today=turnover,
            known_client_ids=self.ledger.shadow_known_client_ids(),
            ordered_symbols_today=self.ledger.shadow_symbols_ordered_on(sleeve, day),
        )

    def _context(self, sleeve: str, snapshot: MarketSnapshot, market_now: datetime) -> RiskContext:
        row = self.ledger.sleeve_row(sleeve)
        day = market_now.date().isoformat()
        _fills, turnover = self.ledger.activity_today(sleeve, day)
        return RiskContext(
            now=market_now,
            sleeve=sleeve,
            equity=self.mark(sleeve, snapshot),
            cash=self.ledger.cash(sleeve),
            positions=self.ledger.positions(sleeve),
            quotes=snapshot.quotes,
            day_start_equity=q8(row["day_start_equity"]),
            peak_equity=q8(row["peak_equity"]),
            trades_today=self.ledger.strategy_trades_today(day),
            turnover_today=turnover,
            known_client_ids=self.ledger.known_client_ids(),
            ordered_symbols_today=self.ledger.symbols_ordered_on(sleeve, day),
        )

    def _mark_portfolio(self, market_now: datetime, snapshot: MarketSnapshot) -> tuple[Decimal, Decimal]:
        """Sum of sleeve marks, and the combined high-water mark.

        The peak is portfolio-wide. A freeze acknowledgement does not rebase it.
        """
        total = Decimal(0)
        for strategy in self.strategies:
            self.ledger.ensure_sleeve(strategy.name, market_now, strategy.initial_state())
            equity = self.mark(strategy.name, snapshot)
            self.ledger.mark_equity(strategy.name, equity, market_now)
            total += equity
        total = q8(total)
        stored = self.ledger.get_meta("portfolio_peak")
        peak = D(stored) if stored else total
        if total > peak:
            peak = total
        self.ledger.set_meta("portfolio_peak", money_str(peak))
        self.ledger.set_meta("portfolio_equity", money_str(total))
        return total, peak

    def _enforce_loss_policy(
        self, market_now: datetime, snapshot: MarketSnapshot
    ) -> tuple[Decimal, Decimal]:
        """Paper-only drawdown on the combined portfolio peak.

        At 10% from that peak, raise an alert and freeze new buys, including
        weekly DCA. Exits stay allowed and nothing is force-sold. Acknowledging
        the freeze does not rebase this peak. The freeze re-arms only after
        drawdown recovers above the line and then falls through it again.
        At 40%, flatten and write KILL. These looser limits must not be carried
        into a live phase. This method never clears the freeze file or the kill
        file. The AI operator may acknowledge the freeze. Only a human may
        clear the kill, with ``rhbot resume --ack``.
        """
        equity, peak = self._mark_portfolio(market_now, snapshot)
        dd = peak_drawdown(equity, peak)
        reason = kill_reason(equity, peak, self.settings)
        if reason:
            self._trip_drawdown(reason, market_now, snapshot, equity=equity, peak=peak)
            return equity, peak
        if kill_active(self.settings.state_dir):
            return equity, peak
        if dd >= self.settings.drawdown_freeze_pct:
            self._raise_freeze(market_now, equity, peak, dd)
        elif not freeze_active(self.settings.state_dir) and self.ledger.get_meta("drawdown_ack_peak"):
            # Recovered above the freeze line. The next breach of this or a new peak may alert.
            # The portfolio peak itself is left where it is.
            self.ledger.set_meta("drawdown_ack_peak", "")
        return equity, peak

    def _raise_freeze(self, market_now: datetime, equity: Decimal, peak: Decimal, dd: Decimal) -> None:
        if freeze_active(self.settings.state_dir):
            return
        if (self.ledger.get_meta("drawdown_ack_peak") or "") == money_str(peak):
            return
        observed = format(q8(dd), "f")
        reason = f"drawdown_freeze {observed} >= {self.settings.drawdown_freeze_pct}"
        engage_freeze(self.settings.state_dir, reason, "risk")
        self.ledger.record_risk_event(
            "drawdown_freeze",
            market_now,
            sleeve="portfolio",
            symbol="",
            side="",
            reason="drawdown_freeze",
            limit_name="drawdown_freeze_pct",
            limit_value=format(self.settings.drawdown_freeze_pct, "f"),
            observed=observed,
            client_order_id="",
            detail=reason,
        )

    def _trip_drawdown(
        self,
        reason: str,
        market_now: datetime,
        snapshot: MarketSnapshot,
        *,
        equity: Decimal,
        peak: Decimal,
    ) -> None:
        before = read_kill(self.settings.state_dir)
        already_acked = bool(before and before.get("ack_required"))
        engage_kill(self.settings.state_dir, reason, "risk", ack_required=True)
        if not already_acked:
            observed = format(q8(peak_drawdown(equity, peak)), "f")
            self.ledger.record_risk_event(
                "kill_trip",
                market_now,
                sleeve="portfolio",
                symbol="",
                side="",
                reason="max_drawdown",
                limit_name="max_drawdown_pct",
                limit_value=format(self.settings.max_drawdown_pct, "f"),
                observed=observed,
                client_order_id="",
                detail=reason,
                ack_required=True,
            )
            self.ledger.cancel_open_orders(market_now, "drawdown")
        held = any(self.ledger.positions(strategy.name) for strategy in self.strategies)
        if held:
            self.flatten(now=market_now, snapshot=snapshot, reason="drawdown_flatten")

    def _require_quotes(self, snapshot: MarketSnapshot) -> None:
        missing = [symbol for symbol in self.settings.symbols if symbol not in snapshot.quotes]
        if missing:
            raise RuntimeError(f"missing quotes for {missing}")

    def _note_quotes(self, snapshot: MarketSnapshot, market_now: datetime, wall: datetime) -> None:
        oldest = min(snapshot.quotes[symbol].ts for symbol in self.settings.symbols)
        self.ledger.set_meta("last_quote_ts", iso(oldest))
        fresh = True
        for symbol in self.settings.symbols:
            age = (market_now - snapshot.quotes[symbol].ts).total_seconds()
            if age > self.settings.max_quote_age_seconds or age < -5:
                fresh = False
        if fresh:
            self.ledger.set_meta("last_quote_ok_at", iso(wall))
            if snapshot.source == "robinhood":
                self.ledger.set_meta("last_auth_ok_at", iso(wall))

    def _client_id(self, sleeve: str, intent: OrderIntent, market_now: datetime) -> str:
        stamp = market_now.strftime("%Y%m%dT%H%M%S")
        return f"{sleeve}-{intent.symbol}-{intent.side}-{stamp}-{uuid.uuid4().hex[:12]}"


def _intent_payload(intent: OrderIntent) -> dict:
    return {
        "symbol": intent.symbol,
        "side": intent.side,
        "reason": intent.reason,
        "quote_amount": None if intent.quote_amount is None else format(intent.quote_amount, "f"),
        "base_quantity": None if intent.base_quantity is None else format(intent.base_quantity, "f"),
    }
