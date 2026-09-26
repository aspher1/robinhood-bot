"""One cycle: mark, check drawdown, ask each strategy, risk-check, paper-fill."""

from __future__ import annotations

import json
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal

from rhbot.brokers.paper import PaperBroker
from rhbot.config import Settings, reject_live_env
from rhbot.data.public import PublicMarketData
from rhbot.data.robinhood import RobinhoodMarketData
from rhbot.errors import DataError, OrderRejected
from rhbot.ledger import Ledger
from rhbot.models import RISK_REDUCTION_REASONS, Fill, MarketSnapshot, OrderIntent
from rhbot.money import D, money_str, q8
from rhbot.overlay import OVERLAY_BOOKS, SHADOW_NAMES, mark_to_bid_equity, signed_drawdown
from rhbot.pricing import plan_fill
from rhbot.ops import (
    engage_freeze,
    freeze_active,
    iso,
    kill_active,
    read_kill,
    resume_needs_ack,
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
        self._store_closed_bars(snapshot, market_now)
        if not self.ledger.get_meta("paper_day1"):
            self.ledger.set_meta("paper_day1", iso(market_now))
        if kill_active(self.settings.state_dir):
            self.ledger.cancel_open_orders(market_now, "kill_switch")
        self._enforce_overlays(market_now, snapshot)
        equities: dict[str, str] = {}
        for strategy in self.strategies:
            equity = self._run_sleeve(strategy, market_now, snapshot)
            if strategy.name in SHADOW_NAMES:
                self._run_shadow_sleeve(strategy, market_now, snapshot)
            equities[strategy.name] = money_str(equity)
        self._note_portfolio_peak()
        self.ledger.set_meta("last_decision_at", iso(wall))
        self.ledger.log_event(
            "cycle",
            {
                "equities": equities,
                "kill_switch": kill_active(self.settings.state_dir),
                "buy_pause": self._any_frozen(),
            },
            market_now,
        )
        self.ledger.set_meta("audit_head", self.ledger.head_hash())
        return {
            "ts": iso(market_now),
            "kill_switch": kill_active(self.settings.state_dir),
            "buy_pause": self._any_frozen(),
            "drawdown_freeze": self._any_frozen(),
            "equities": equities,
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

    def _note_portfolio_peak(self) -> None:
        """Combined high-water mark. It only rises. Resume does not write it."""
        total = Decimal(0)
        for name in self.ledger.sleeve_names():
            total += D(self.ledger.sleeve_row(name)["last_equity"])
        total = q8(total)
        stored = self.ledger.get_meta("portfolio_peak")
        peak = total if not stored else max(D(stored), total)
        self.ledger.set_meta("portfolio_peak", money_str(peak))

    def mark(self, sleeve: str, snapshot: MarketSnapshot):
        equity = self.ledger.cash(sleeve)
        for symbol, qty in self.ledger.positions(sleeve).items():
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                raise RuntimeError(f"no quote to mark {symbol}")
            equity += qty * quote.mid
        return q8(equity)

    def mark_to_bid(self, sleeve: str, snapshot: MarketSnapshot):
        return mark_to_bid_equity(
            self.ledger.cash(sleeve),
            self.ledger.positions(sleeve),
            snapshot.quotes,
            self.settings.cost_per_side,
        )

    def heartbeat_body(self) -> dict:
        buy_pause = self._any_frozen()
        kill = read_kill(self.settings.state_dir)
        worst_dd = Decimal(0)
        peak_equity = None
        book_resume = False
        for name in OVERLAY_BOOKS:
            row = self.ledger.overlay_row(name)
            if row is None:
                continue
            dd = D(row["dd"])
            if peak_equity is None or dd < worst_dd:
                worst_dd = dd
                peak_equity = str(row["peak"])
            if str(row["state"]) == "KILLED" and not str(row["kill_acked_peak"] or ""):
                book_resume = True
        return {
            "kill_switch": kill is not None,
            "buy_pause": buy_pause,
            "drawdown_freeze": buy_pause,
            "peak_equity": peak_equity,
            "drawdown_pct": format(q8(worst_dd), "f"),
            "ack_required": (kill is not None and resume_needs_ack(kill)) or book_resume or buy_pause,
            "rearm_eligible": (not buy_pause) and worst_dd > -self.settings.freeze_drawdown_pct,
            "overlay": self._overlay_public(),
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
        if strategy.name == "dca_weekly":
            day1 = self.ledger.get_meta("paper_day1")
            if day1 and not state.get("day1"):
                state = {**state, "day1": day1}
        if kill_active(self.settings.state_dir):
            orders: list[OrderIntent] = []
            new_state = state
            reason = "kill_switch"
        else:
            orders, new_state, reason = strategy.decide(
                snapshot, state, positions, cash, equity, market_now
            )
        if strategy.name == "dca_weekly":
            new_state, orders, reason = self._dca_freeze_skip(
                new_state, orders, reason, market_now
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
        filled: list[Fill] = []
        fresh: list[Fill] = []
        planned_strategy = 0
        for intent in orders:
            if kill_active(self.settings.state_dir):
                break
            client_order_id = self._client_id(strategy.name, intent, market_now)
            ctx = self._context(strategy.name, snapshot, market_now)
            ctx.trades_today += planned_strategy
            try:
                planned = self.broker.plan(
                    strategy.name,
                    intent,
                    client_order_id,
                    ctx,
                    market_now,
                )
            except OrderRejected:
                continue
            filled.append(planned)
            if self.ledger.get_fill(planned.client_order_id) is None:
                fresh.append(planned)
                if intent.reason not in RISK_REDUCTION_REASONS:
                    planned_strategy += 1
        with self.ledger.transaction():
            for fill in fresh:
                if self.ledger.get_fill(fill.client_order_id) is None:
                    self.ledger.apply_fill(fill)
            positions = self.ledger.positions(strategy.name)
            final_state = strategy.commit(new_state, filled, positions, market_now)
            self.ledger.save_strategy_state(strategy.name, final_state, commit=False)
        equity = self.mark(strategy.name, snapshot)
        self.ledger.mark_equity(strategy.name, equity, market_now)
        self.ledger.snapshot(strategy.name, equity, market_now)
        return equity

    def _run_shadow_sleeve(self, strategy, market_now: datetime, snapshot: MarketSnapshot) -> None:
        """Same strategy and caps, without the 10% freeze or the 40% kill.

        Other risk checks still apply. These books are excluded from scoring.
        """
        shadow = SHADOW_NAMES[strategy.name]
        self.ledger.ensure_shadow_sleeve(shadow, market_now, strategy.initial_state())
        equity = self.shadow_mark(shadow, snapshot)
        self.ledger.mark_shadow_equity(shadow, equity, market_now)
        state = self.ledger.shadow_strategy_state(shadow) or strategy.initial_state()
        if strategy.name == "dca_weekly":
            day1 = self.ledger.get_meta("paper_day1")
            if day1 and not state.get("day1"):
                state = {**state, "day1": day1}
        positions = self.ledger.shadow_positions(shadow)
        cash = self.ledger.shadow_cash(shadow)
        orders, new_state, _reason = strategy.decide(
            snapshot, state, positions, cash, equity, market_now
        )
        filled: list[Fill] = []
        for intent in orders:
            client_order_id = self._client_id(shadow, intent, market_now)
            existing = self.ledger.get_shadow_fill(client_order_id)
            if existing is not None:
                filled.append(existing)
                continue
            decision = self.risk.evaluate(
                intent,
                self._shadow_context(shadow, snapshot, market_now),
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
                sleeve=shadow,
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
        positions = self.ledger.shadow_positions(shadow)
        self.ledger.save_shadow_strategy_state(
            shadow, strategy.commit(new_state, filled, positions, market_now)
        )
        equity = self.shadow_mark(shadow, snapshot)
        self.ledger.mark_shadow_equity(shadow, equity, market_now)
        self.ledger.shadow_snapshot(shadow, equity, market_now)

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
            trades_today=self.ledger.shadow_strategy_trades_today(sleeve, day),
            turnover_today=turnover,
            known_client_ids=self.ledger.shadow_known_client_ids(),
            ordered_symbols_today=self.ledger.shadow_symbols_ordered_on(sleeve, day),
            overlay_state="ARMED",
            opened_at=self.ledger.position_opened_at(sleeve, shadow=True),
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
            trades_today=self.ledger.book_strategy_trades_today(sleeve, day),
            turnover_today=turnover,
            known_client_ids=self.ledger.known_client_ids(),
            ordered_symbols_today=self.ledger.symbols_ordered_on(sleeve, day),
            overlay_state=self._overlay_state(sleeve),
            opened_at=self.ledger.position_opened_at(sleeve),
        )

    def _overlay_state(self, sleeve: str) -> str:
        if sleeve not in OVERLAY_BOOKS:
            return "NONE"
        row = self.ledger.overlay_row(sleeve)
        if row is None:
            return "ARMED"
        return str(row["state"])

    def _any_frozen(self) -> bool:
        return any(self._overlay_state(name) == "FROZEN" for name in OVERLAY_BOOKS)

    def _overlay_public(self) -> dict:
        out = {}
        for name in OVERLAY_BOOKS:
            row = self.ledger.overlay_row(name)
            if row is None:
                continue
            out[name] = {
                "state": str(row["state"]),
                "dd": str(row["dd"]),
                "peak": str(row["peak"]),
                "equity": str(row["equity"]),
            }
        return out

    def _store_closed_bars(self, snapshot: MarketSnapshot, market_now: datetime) -> None:
        bars = [bar for series in snapshot.bars.values() for bar in series]
        if bars:
            self.ledger.upsert_candles(bars, fetched_at=market_now)

    def _dca_freeze_skip(
        self,
        state: dict,
        orders: list[OrderIntent],
        reason: str,
        market_now: datetime,
    ) -> tuple[dict, list[OrderIntent], str]:
        """A due DCA buy during a freeze is denied and is not caught up later."""
        if self._overlay_state("dca_weekly") != "FROZEN" or not orders:
            return state, orders, reason
        index = state.get("seen_index")
        skipped = [int(item) for item in (state.get("skipped_indexes") or [])]
        if index is None or int(index) in skipped:
            return state, [], "freeze"
        skipped.append(int(index))
        updated = {**state, "skipped_indexes": skipped}
        for intent in orders:
            self.ledger.record_risk_event(
                "risk_denial",
                market_now,
                sleeve="dca_weekly",
                symbol=intent.symbol,
                side=intent.side,
                reason="freeze",
                limit_name="freeze_drawdown_pct",
                limit_value=format(self.settings.freeze_drawdown_pct, "f"),
                observed="frozen",
                client_order_id="",
                detail="freeze",
            )
        return updated, [], "freeze"

    def _enforce_overlays(self, market_now: datetime, snapshot: MarketSnapshot) -> None:
        """Option B on trend_daily and dca_weekly only. Never touches buy_and_hold.

        DD = mark-to-bid equity / running peak − 1. An ack does not move the peak.
        The engine never clears state/KILL.
        """
        for name in OVERLAY_BOOKS:
            strategy = next(item for item in self.strategies if item.name == name)
            self.ledger.ensure_sleeve(name, market_now, strategy.initial_state())
            self.ledger.ensure_overlay(name)
            equity = self.mark_to_bid(name, snapshot)
            row = self.ledger.overlay_row(name)
            assert row is not None
            peak = D(row["peak"])
            if peak <= 0 or equity > peak:
                peak = equity
            dd = signed_drawdown(equity, peak)
            state = str(row["state"])
            fields = {
                "peak": money_str(peak),
                "equity": money_str(equity),
                "dd": format(q8(dd), "f"),
            }
            acked = str(row["kill_acked_peak"] or "")
            if dd <= -self.settings.kill_drawdown_pct and acked != money_str(peak):
                if state != "KILLED":
                    self._kill_book(name, market_now, snapshot, equity, peak, dd)
                else:
                    self.ledger.save_overlay(name, fields)
                continue
            if state == "KILLED":
                # Human resume records the peak. Until then the book stays
                # killed even if the mark recovers. One book's kill does not
                # write the process-wide kill file.
                recovered = (
                    bool(acked)
                    and not kill_active(self.settings.state_dir)
                    and dd > -self.settings.kill_drawdown_pct
                )
                if not recovered:
                    self.ledger.save_overlay(name, fields)
                    continue
                fields["state"] = "ARMED"
                fields["kill_acked_peak"] = ""
                state = "ARMED"
                self.ledger.save_overlay(name, fields)
                self.ledger.log_event(
                    "kill_rearm",
                    {
                        "sleeve": name,
                        "equity": money_str(equity),
                        "peak": money_str(peak),
                        "dd": format(q8(dd), "f"),
                    },
                    market_now,
                )
            if state == "ARMED" and dd <= -self.settings.freeze_drawdown_pct:
                fields.update(
                    {
                        "state": "FROZEN",
                        "trip_dd": format(q8(dd), "f"),
                        "trip_equity": money_str(equity),
                        "trip_peak": money_str(peak),
                        "trip_ts": iso(market_now),
                    }
                )
                self.ledger.save_overlay(name, fields)
                self.ledger.record_risk_event(
                    "freeze_trip",
                    market_now,
                    sleeve=name,
                    symbol="",
                    side="",
                    reason="freeze",
                    limit_name="freeze_drawdown_pct",
                    limit_value=format(self.settings.freeze_drawdown_pct, "f"),
                    observed=format(q8(dd), "f"),
                    client_order_id="",
                    detail="freeze",
                    extra={
                        "equity": money_str(equity),
                        "peak": money_str(peak),
                        "dd": format(q8(dd), "f"),
                    },
                )
                continue
            if state == "ACKED" and dd > -self.settings.freeze_drawdown_pct:
                fields["state"] = "ARMED"
                self.ledger.save_overlay(name, fields)
                self.ledger.log_event(
                    "freeze_rearm",
                    {
                        "sleeve": name,
                        "equity": money_str(equity),
                        "peak": money_str(peak),
                        "dd": format(q8(dd), "f"),
                    },
                    market_now,
                )
                continue
            self.ledger.save_overlay(name, fields)

    def _kill_book(
        self,
        name: str,
        market_now: datetime,
        snapshot: MarketSnapshot,
        equity: Decimal,
        peak: Decimal,
        dd: Decimal,
    ) -> None:
        observed = format(q8(dd), "f")
        self.ledger.save_overlay(
            name,
            {
                "state": "KILLED",
                "peak": money_str(peak),
                "equity": money_str(equity),
                "dd": observed,
                "trip_dd": observed,
                "trip_equity": money_str(equity),
                "trip_peak": money_str(peak),
                "trip_ts": iso(market_now),
            },
        )
        reason = f"max_drawdown {observed} <= -{self.settings.kill_drawdown_pct}"
        self.ledger.record_risk_event(
            "kill_trip",
            market_now,
            sleeve=name,
            symbol="",
            side="",
            reason="max_drawdown",
            limit_name="kill_drawdown_pct",
            limit_value=format(self.settings.kill_drawdown_pct, "f"),
            observed=observed,
            client_order_id="",
            detail=reason,
            ack_required=True,
            extra={"equity": money_str(equity), "peak": money_str(peak), "dd": observed},
        )
        self._flatten_book(name, market_now, snapshot)

    def _flatten_book(self, name: str, market_now: datetime, snapshot: MarketSnapshot) -> None:
        errors: list[str] = []
        for symbol, qty in list(self.ledger.positions(name).items()):
            intent = OrderIntent(symbol, "sell", "drawdown_flatten", base_quantity=qty)
            client_order_id = self._client_id(name, intent, market_now)
            try:
                fill = self.broker.submit(
                    name,
                    intent,
                    client_order_id,
                    self._context(name, snapshot, market_now),
                    market_now,
                    reduce_only=True,
                )
            except OrderRejected as exc:
                errors.append(f"{symbol}: {'; '.join(exc.reasons)}")
                continue
            if fill.reason != intent.reason:
                errors.append(f"{symbol}: reused {fill.reason} fill {fill.client_order_id}")
        equity = self.mark(name, snapshot)
        self.ledger.mark_equity(name, equity, market_now)
        left = sorted(symbol for symbol, qty in self.ledger.positions(name).items() if qty > 0)
        if left or errors:
            detail = (
                f"drawdown_flatten incomplete for {name}: "
                f"{'; '.join(errors) if errors else 'positions remain'} left={left}"
            )
            self.ledger.set_meta("last_error", detail[:400])
            raise RuntimeError(detail)

    def _require_quotes(self, snapshot: MarketSnapshot) -> None:
        missing = [symbol for symbol in self.settings.symbols if symbol not in snapshot.quotes]
        if missing:
            raise RuntimeError(f"missing quotes for {missing}")

    def _note_quotes(self, snapshot: MarketSnapshot, market_now: datetime, wall: datetime) -> None:
        oldest = min(snapshot.quotes[symbol].ts for symbol in self.settings.symbols)
        self.ledger.set_meta("last_quote_ts", iso(oldest))
        fresh = True
        ages: list[float] = []
        for symbol in self.settings.symbols:
            age = (market_now - snapshot.quotes[symbol].ts).total_seconds()
            ages.append(age)
            if age > self.settings.max_quote_age_seconds or age < -5:
                fresh = False
        self.ledger.set_meta("last_quote_age_at_cycle_s", str(int(max(ages) if ages else 0)))
        if fresh:
            self.ledger.set_meta("last_quote_ok_at", iso(wall))
            if snapshot.source == "robinhood":
                self.ledger.set_meta("last_auth_ok_at", iso(wall))

    def _client_id(self, sleeve: str, intent: OrderIntent, market_now: datetime) -> str:
        """sleeve:symbol:side:decision_key. The key is the UTC bar date, or the DCA index."""
        base = sleeve.removesuffix("_shadow")
        if base == "dca_weekly":
            from rhbot.ledger import parse_ts
            from rhbot.strategies.dca import schedule_index

            raw = self.ledger.get_meta("paper_day1")
            key = str(schedule_index(market_now, parse_ts(raw))) if raw else ensure_utc(market_now).date().isoformat()
        else:
            key = ensure_utc(market_now).date().isoformat()
        order_id = f"{sleeve}:{intent.symbol}:{intent.side}:{key}"
        # A same-day strategy sell must not satisfy a later risk-reduction sell.
        if intent.reason in RISK_REDUCTION_REASONS:
            return f"{order_id}:{intent.reason}"
        return order_id


def _intent_payload(intent: OrderIntent) -> dict:
    return {
        "symbol": intent.symbol,
        "side": intent.side,
        "reason": intent.reason,
        "quote_amount": None if intent.quote_amount is None else format(intent.quote_amount, "f"),
        "base_quantity": None if intent.base_quantity is None else format(intent.base_quantity, "f"),
    }
