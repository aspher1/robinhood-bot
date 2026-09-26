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
from rhbot.errors import OrderRejected
from rhbot.ledger import Ledger
from rhbot.models import MarketSnapshot, OrderIntent
from rhbot.money import D, money_str, q8
from rhbot.ops import engage_kill, iso, kill_active, read_kill, sd_notify, utcnow, write_heartbeat
from rhbot.risk import (
    RiskContext,
    RiskEngine,
    exposure_limit_pct,
    kill_reason,
    peak_drawdown,
    target_sell_notional,
)
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
        self._enforce_loss_policy(market_now, snapshot)
        equities: dict[str, str] = {}
        for strategy in self.strategies:
            equity = self._run_sleeve(strategy, market_now, snapshot)
            equities[strategy.name] = money_str(equity)
        self.ledger.set_meta("last_decision_at", iso(wall))
        self.ledger.log_event(
            "cycle",
            {"equities": equities, "kill_switch": kill_active(self.settings.state_dir)},
            market_now,
        )
        self.ledger.set_meta("audit_head", self.ledger.head_hash())
        return {
            "ts": iso(market_now),
            "kill_switch": kill_active(self.settings.state_dir),
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
                    row = self.ledger.sleeve_row(strategy.name)
                    self._trip_drawdown(
                        exc.reasons[0],
                        market_now,
                        snapshot,
                        strategy.name,
                        equity=self.mark(strategy.name, snapshot),
                        peak=D(row["peak_equity"]),
                    )
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

    def _enforce_loss_policy(self, market_now: datetime, snapshot: MarketSnapshot) -> None:
        """10% drawdown kills and flattens. 5% and 7.5% sell the book down."""
        for strategy in self.strategies:
            self.ledger.ensure_sleeve(strategy.name, market_now, strategy.initial_state())
            equity = self.mark(strategy.name, snapshot)
            _day_start, peak = self.ledger.mark_equity(strategy.name, equity, market_now)
            reason = kill_reason(equity, peak, self.settings)
            if reason:
                self._trip_drawdown(
                    reason, market_now, snapshot, strategy.name, equity=equity, peak=peak
                )
                return
        if kill_active(self.settings.state_dir):
            return
        for strategy in self.strategies:
            equity = self.mark(strategy.name, snapshot)
            peak = D(self.ledger.sleeve_row(strategy.name)["peak_equity"])
            cap = exposure_limit_pct(self.settings, equity, peak)
            if cap < self.settings.max_total_exposure_pct:
                self._sell_down(strategy.name, snapshot, market_now, cap)

    def _trip_drawdown(
        self,
        reason: str,
        market_now: datetime,
        snapshot: MarketSnapshot,
        sleeve: str,
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
                sleeve=sleeve,
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

    def _sell_down(
        self,
        sleeve: str,
        snapshot: MarketSnapshot,
        market_now: datetime,
        cap_pct,
    ) -> None:
        positions = self.ledger.positions(sleeve)
        if not positions:
            return
        mids = {}
        total = Decimal(0)
        for symbol, qty in positions.items():
            quote = snapshot.quotes.get(symbol)
            if quote is None or quote.mid <= 0:
                return
            mids[symbol] = quote.mid
            total += qty * quote.mid
        equity = self.ledger.cash(sleeve) + total
        sell_notional = target_sell_notional(
            total, equity, cap_pct, self.settings.cost_per_side
        )
        if sell_notional <= 0 or total <= 0:
            return
        sold = False
        for symbol, qty in list(positions.items()):
            value = qty * mids[symbol]
            portion = sell_notional * (value / total)
            sell_qty = q8(portion / mids[symbol])
            if sell_qty <= 0:
                continue
            if sell_qty > qty:
                sell_qty = qty
            if (
                q8(sell_qty * mids[symbol]) < self.settings.min_order_notional
                and sell_qty < qty
            ):
                continue
            intent = OrderIntent(symbol, "sell", "exposure_cut", base_quantity=sell_qty)
            try:
                self.broker.submit(
                    sleeve,
                    intent,
                    self._client_id(sleeve, intent, market_now),
                    self._context(sleeve, snapshot, market_now),
                    market_now,
                    reduce_only=True,
                )
            except OrderRejected:
                continue
            sold = True
        if sold:
            self.ledger.log_event(
                "exposure_cut",
                {"sleeve": sleeve, "cap_pct": format(cap_pct, "f")},
                market_now,
            )

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
