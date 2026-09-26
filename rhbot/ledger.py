"""SQLite sleeves, fills, and an append-only hash-chained event log."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from rhbot.config import Settings
from rhbot.errors import OrderRejected
from rhbot.models import RISK_REDUCTION_REASONS, Bar, Fill
from rhbot.money import D, canonical, money_str, q8
from rhbot.ops import iso, utcnow

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleeves (
    name TEXT PRIMARY KEY,
    cash TEXT NOT NULL,
    starting_cash TEXT NOT NULL,
    peak_equity TEXT NOT NULL,
    day_start_equity TEXT NOT NULL,
    day_start_date TEXT NOT NULL,
    last_equity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL,
    PRIMARY KEY (sleeve, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    qty_delta TEXT NOT NULL,
    mid TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    cash_delta TEXT NOT NULL,
    cost TEXT NOT NULL,
    notional TEXT NOT NULL,
    ts TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL,
    drawdown TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_state (
    sleeve TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    ts TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (symbol, source, ts)
);
CREATE TABLE IF NOT EXISTS overlay_books (
    sleeve TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    peak TEXT NOT NULL,
    equity TEXT NOT NULL,
    dd TEXT NOT NULL,
    trip_dd TEXT,
    trip_equity TEXT,
    trip_peak TEXT,
    trip_ts TEXT,
    ack_ts TEXT,
    ack_by TEXT,
    ack_note TEXT,
    kill_acked_peak TEXT
);
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER IF NOT EXISTS fills_no_update
BEFORE UPDATE ON fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete
BEFORE DELETE ON fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    reason TEXT NOT NULL,
    limit_name TEXT NOT NULL,
    limit_value TEXT NOT NULL,
    observed TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trade_log_no_update
BEFORE UPDATE ON trade_log
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER IF NOT EXISTS trade_log_no_delete
BEFORE DELETE ON trade_log
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TABLE IF NOT EXISTS shadow_sleeves (
    name TEXT PRIMARY KEY,
    cash TEXT NOT NULL,
    starting_cash TEXT NOT NULL,
    day_start_equity TEXT NOT NULL,
    day_start_date TEXT NOT NULL,
    last_equity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_positions (
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL,
    PRIMARY KEY (sleeve, symbol)
);
CREATE TABLE IF NOT EXISTS shadow_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    qty_delta TEXT NOT NULL,
    mid TEXT NOT NULL,
    fill_price TEXT NOT NULL,
    cash_delta TEXT NOT NULL,
    cost TEXT NOT NULL,
    notional TEXT NOT NULL,
    ts TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_strategy_state (
    sleeve TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS shadow_fills_no_update
BEFORE UPDATE ON shadow_fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE TRIGGER IF NOT EXISTS shadow_fills_no_delete
BEFORE DELETE ON shadow_fills
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
"""


def chain_hash(prev: str, payload: str) -> str:
    return hashlib.sha256(f"{prev}|{payload}".encode("utf-8")).hexdigest()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Ledger:
    def __init__(self, settings: Settings, *, readonly: bool = False):
        self.settings = settings
        self.readonly = readonly
        self.path = Path(settings.state_dir) / "bot.sqlite"
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(candles)")}
        if "fetched_at" not in columns:
            self.conn.execute("ALTER TABLE candles ADD COLUMN fetched_at TEXT")
        self.set_meta("schema_version", "3")
        self.set_meta("starting_cash", money_str(settings.starting_cash))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def bump(self, key: str) -> int:
        current = int(self.get_meta(key, "0") or "0")
        current += 1
        self.set_meta(key, str(current))
        return current

    def meta_int(self, key: str) -> int:
        return int(self.get_meta(key, "0") or "0")

    def ensure_sleeve(self, name: str, now: datetime, initial_state: dict) -> None:
        row = self.conn.execute("SELECT name FROM sleeves WHERE name=?", (name,)).fetchone()
        if row is not None:
            return
        cash = money_str(self.settings.starting_cash)
        today = now.astimezone(timezone.utc).date().isoformat()
        self.conn.execute(
            """
            INSERT INTO sleeves
                (name, cash, starting_cash, peak_equity, day_start_equity, day_start_date, last_equity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, cash, cash, cash, cash, today, cash),
        )
        self.conn.execute(
            "INSERT INTO strategy_state(sleeve, state_json) VALUES(?, ?)",
            (name, canonical(initial_state)),
        )
        self.conn.commit()

    def cash(self, sleeve: str) -> Decimal:
        row = self.conn.execute("SELECT cash FROM sleeves WHERE name=?", (sleeve,)).fetchone()
        if row is None:
            raise KeyError(sleeve)
        return D(row["cash"])

    def starting_cash(self, sleeve: str) -> Decimal:
        row = self.conn.execute(
            "SELECT starting_cash FROM sleeves WHERE name=?", (sleeve,)
        ).fetchone()
        if row is None:
            raise KeyError(sleeve)
        return D(row["starting_cash"])

    def sleeve_row(self, sleeve: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM sleeves WHERE name=?", (sleeve,)).fetchone()
        if row is None:
            raise KeyError(sleeve)
        return row

    def sleeve_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sleeves ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]

    def positions(self, sleeve: str) -> dict[str, Decimal]:
        rows = self.conn.execute(
            "SELECT symbol, qty FROM positions WHERE sleeve=? ORDER BY symbol",
            (sleeve,),
        ).fetchall()
        out: dict[str, Decimal] = {}
        for row in rows:
            qty = D(row["qty"])
            if qty != 0:
                out[str(row["symbol"])] = qty
        return out

    def position_qty(self, sleeve: str, symbol: str) -> Decimal:
        row = self.conn.execute(
            "SELECT qty FROM positions WHERE sleeve=? AND symbol=?",
            (sleeve, symbol),
        ).fetchone()
        if row is None:
            return Decimal(0)
        return D(row["qty"])

    def strategy_state(self, sleeve: str) -> dict:
        row = self.conn.execute(
            "SELECT state_json FROM strategy_state WHERE sleeve=?", (sleeve,)
        ).fetchone()
        if row is None:
            return {}
        import json

        data = json.loads(row["state_json"])
        if not isinstance(data, dict):
            return {}
        return data

    def save_strategy_state(self, sleeve: str, state: dict, *, commit: bool = True) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_state(sleeve, state_json) VALUES(?, ?)
            ON CONFLICT(sleeve) DO UPDATE SET state_json=excluded.state_json
            """,
            (sleeve, canonical(state)),
        )
        if commit:
            self.conn.commit()

    def known_client_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT client_order_id FROM orders").fetchall()
        return {str(row["client_order_id"]) for row in rows}

    def activity_today(self, sleeve: str, day: str) -> tuple[int, Decimal]:
        rows = self.conn.execute(
            "SELECT notional FROM fills WHERE sleeve=? AND substr(ts, 1, 10)=?",
            (sleeve, day),
        ).fetchall()
        total = Decimal(0)
        for row in rows:
            total += D(row["notional"])
        return len(rows), q8(total)

    def strategy_trades_today(self, day: str) -> int:
        """All strategy fills today. The cap itself is per book."""
        placeholders = ",".join("?" for _ in RISK_REDUCTION_REASONS)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM fills
            WHERE substr(ts, 1, 10)=? AND reason NOT IN ({placeholders})
            """,
            (day, *RISK_REDUCTION_REASONS),
        ).fetchone()
        return int(row["n"])

    def book_strategy_trades_today(self, sleeve: str, day: str) -> int:
        """Strategy fills for one book today. Risk-reduction sells are exempt."""
        placeholders = ",".join("?" for _ in RISK_REDUCTION_REASONS)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM fills
            WHERE sleeve=? AND substr(ts, 1, 10)=? AND reason NOT IN ({placeholders})
            """,
            (sleeve, day, *RISK_REDUCTION_REASONS),
        ).fetchone()
        return int(row["n"])

    def symbols_ordered_on(self, sleeve: str, day: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM fills WHERE sleeve=? AND substr(ts, 1, 10)=?",
            (sleeve, day),
        ).fetchall()
        return {str(row["symbol"]) for row in rows}

    def get_fill(self, client_order_id: str) -> Fill | None:
        row = self.conn.execute(
            "SELECT * FROM fills WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return Fill(
            sleeve=str(row["sleeve"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            qty=D(row["qty"]),
            qty_delta=D(row["qty_delta"]),
            mid=D(row["mid"]),
            fill_price=D(row["fill_price"]),
            cash_delta=D(row["cash_delta"]),
            cost=D(row["cost"]),
            notional=D(row["notional"]),
            ts=parse_ts(str(row["ts"])),
            client_order_id=str(row["client_order_id"]),
            reason=str(row["reason"]),
        )

    def head_hash(self) -> str:
        row = self.conn.execute(
            "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return GENESIS
        return str(row["hash"])

    def append_event(self, kind: str, data: dict, ts: datetime) -> str:
        body_obj = {"kind": kind, "ts": iso(ts), **data}
        body = canonical(body_obj)
        prev = self.head_hash()
        digest = chain_hash(prev, body)
        self.conn.execute(
            "INSERT INTO events(ts, kind, payload, prev_hash, hash) VALUES(?, ?, ?, ?, ?)",
            (iso(ts), kind, body, prev, digest),
        )
        return digest

    def log_event(self, kind: str, data: dict, ts: datetime) -> str:
        """Append an event and commit it. Do not call this inside ``transaction()``."""
        with self.transaction():
            return self.append_event(kind, data, ts)

    def record_risk_event(
        self,
        kind: str,
        ts: datetime,
        *,
        sleeve: str,
        symbol: str,
        side: str,
        reason: str,
        limit_name: str,
        limit_value: str,
        observed: str,
        client_order_id: str,
        detail: str,
        ack_required: bool = False,
        extra: dict | None = None,
    ) -> None:
        """Write a risk denial, drawdown freeze, or kill trip to the trade log and the hash chain."""
        if kind not in ("risk_denial", "drawdown_freeze", "freeze_trip", "kill_trip"):
            raise ValueError(f"unknown risk event {kind}")
        payload = {
            "ack_required": ack_required,
            "client_order_id": client_order_id,
            "detail": detail,
            "limit": limit_value,
            "limit_name": limit_name,
            "observed": observed,
            "reason": reason,
            "side": side,
            "sleeve": sleeve,
            "symbol": symbol,
        }
        if kind == "kill_trip":
            payload["by"] = "risk"
        elif kind in ("drawdown_freeze", "freeze_trip"):
            payload["by"] = "risk"
            payload.pop("ack_required")
        else:
            payload.pop("ack_required")
        if extra:
            payload.update(extra)
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO trade_log(
                    ts, kind, sleeve, symbol, side, reason, limit_name,
                    limit_value, observed, client_order_id, detail
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(ts),
                    kind,
                    sleeve,
                    symbol,
                    side,
                    reason,
                    limit_name,
                    limit_value,
                    observed,
                    client_order_id,
                    detail,
                ),
            )
            self.append_event(kind, payload, ts)

    def insert_open_order(
        self,
        *,
        client_order_id: str,
        sleeve: str,
        symbol: str,
        side: str,
        ts: datetime,
        reason: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO orders(client_order_id, sleeve, symbol, side, status, ts, reason)
            VALUES(?, ?, ?, ?, 'open', ?, ?)
            """,
            (client_order_id, sleeve, symbol, side, iso(ts), reason),
        )

    def set_order_status(self, client_order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status=? WHERE client_order_id=?",
            (status, client_order_id),
        )

    def cancel_open_orders(self, ts: datetime, reason: str) -> int:
        rows = self.conn.execute(
            "SELECT client_order_id, sleeve, symbol FROM orders WHERE status='open'"
        ).fetchall()
        if not rows:
            return 0
        with self.transaction():
            for row in rows:
                self.set_order_status(str(row["client_order_id"]), "canceled")
                self.append_event(
                    "cancel",
                    {
                        "client_order_id": str(row["client_order_id"]),
                        "sleeve": str(row["sleeve"]),
                        "symbol": str(row["symbol"]),
                        "reason": reason,
                    },
                    ts,
                )
        return len(rows)

    def open_orders(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status='open' ORDER BY ts"
        ).fetchall()
        return [dict(row) for row in rows]

    def commit_fill(self, fill: Fill) -> None:
        with self.transaction():
            self.apply_fill(fill)

    def apply_fill(self, fill: Fill) -> None:
        """Write one fill. Caller owns the transaction."""
        self.insert_open_order(
            client_order_id=fill.client_order_id,
            sleeve=fill.sleeve,
            symbol=fill.symbol,
            side=fill.side,
            ts=fill.ts,
            reason=fill.reason,
        )
        cash = self.cash(fill.sleeve)
        new_cash = q8(cash + fill.cash_delta)
        if new_cash < 0:
            raise OrderRejected(["insufficient_cash"])
        new_pos = q8(self.position_qty(fill.sleeve, fill.symbol) + fill.qty_delta)
        if new_pos < 0:
            raise OrderRejected(["insufficient_position"])
        self.conn.execute(
            "UPDATE sleeves SET cash=? WHERE name=?",
            (money_str(new_cash), fill.sleeve),
        )
        self.conn.execute(
            """
            INSERT INTO positions(sleeve, symbol, qty) VALUES(?, ?, ?)
            ON CONFLICT(sleeve, symbol) DO UPDATE SET qty=excluded.qty
            """,
            (fill.sleeve, fill.symbol, money_str(new_pos)),
        )
        self.conn.execute(
            """
            INSERT INTO fills(
                sleeve, symbol, side, qty, qty_delta, mid, fill_price,
                cash_delta, cost, notional, ts, client_order_id, reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.sleeve,
                fill.symbol,
                fill.side,
                money_str(fill.qty),
                money_str(fill.qty_delta),
                money_str(fill.mid),
                money_str(fill.fill_price),
                money_str(fill.cash_delta),
                money_str(fill.cost),
                money_str(fill.notional),
                iso(fill.ts),
                fill.client_order_id,
                fill.reason,
            ),
        )
        self.set_order_status(fill.client_order_id, "filled")
        self.append_event("fill", fill.event_payload(), fill.ts)

    def position_opened_at(self, sleeve: str, *, shadow: bool = False) -> dict[str, datetime]:
        """First buy after the book was last flat, per coin."""
        table = "shadow_fills" if shadow else "fills"
        rows = self.conn.execute(
            f"SELECT symbol, qty_delta, ts FROM {table} WHERE sleeve=? ORDER BY id",
            (sleeve,),
        ).fetchall()
        qty: dict[str, Decimal] = {}
        opened: dict[str, datetime] = {}
        for row in rows:
            symbol = str(row["symbol"])
            before = qty.get(symbol, Decimal(0))
            after = q8(before + D(row["qty_delta"]))
            if before <= 0 and after > 0:
                opened[symbol] = parse_ts(str(row["ts"]))
            if after <= 0:
                opened.pop(symbol, None)
            qty[symbol] = after
        return {symbol: ts for symbol, ts in opened.items() if qty.get(symbol, Decimal(0)) > 0}

    def ensure_overlay(self, sleeve: str) -> None:
        self.conn.execute(
            """
            INSERT INTO overlay_books(sleeve, state, peak, equity, dd)
            VALUES(?, 'ARMED', '0', '0', '0')
            ON CONFLICT(sleeve) DO NOTHING
            """,
            (sleeve,),
        )
        self.conn.commit()

    def overlay_row(self, sleeve: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM overlay_books WHERE sleeve=?",
            (sleeve,),
        ).fetchone()

    def save_overlay(self, sleeve: str, fields: dict[str, str]) -> None:
        self.ensure_overlay(sleeve)
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.conn.execute(
            f"UPDATE overlay_books SET {assignments} WHERE sleeve=?",
            (*fields.values(), sleeve),
        )
        self.conn.commit()

    def mark_equity(self, sleeve: str, equity: Decimal, now: datetime) -> tuple[Decimal, Decimal]:
        """Update peak and the UTC day-start mark. Returns (day_start, peak)."""
        row = self.sleeve_row(sleeve)
        today = now.astimezone(timezone.utc).date().isoformat()
        peak = D(row["peak_equity"])
        day_start = D(row["day_start_equity"])
        if str(row["day_start_date"]) != today:
            day_start = q8(equity)
        if equity > peak:
            peak = q8(equity)
        self.conn.execute(
            """
            UPDATE sleeves
            SET peak_equity=?, day_start_equity=?, day_start_date=?, last_equity=?
            WHERE name=?
            """,
            (money_str(peak), money_str(day_start), today, money_str(equity), sleeve),
        )
        self.conn.commit()
        return day_start, peak

    def snapshot(self, sleeve: str, equity: Decimal, now: datetime) -> None:
        row = self.sleeve_row(sleeve)
        peak = D(row["peak_equity"])
        drawdown = Decimal(0) if peak <= 0 else q8((peak - equity) / peak)
        self.conn.execute(
            "INSERT INTO equity_snapshots(ts, sleeve, equity, cash, drawdown) VALUES(?, ?, ?, ?, ?)",
            (iso(now), sleeve, money_str(equity), row["cash"], money_str(drawdown)),
        )
        self.conn.commit()

    def snapshots(self, sleeve: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM equity_snapshots WHERE sleeve=? ORDER BY id",
                (sleeve,),
            ).fetchall()
        )

    def fills_for(self, sleeve: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM fills WHERE sleeve=? ORDER BY id",
                (sleeve,),
            ).fetchall()
        )

    def count_events(self, kind: str, sleeve: str | None = None) -> int:
        if sleeve is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind=?", (kind,)
            ).fetchone()
            return int(row["n"])
        rows = self.conn.execute(
            "SELECT payload FROM events WHERE kind=?", (kind,)
        ).fetchall()
        import json

        n = 0
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("sleeve") == sleeve:
                n += 1
        return n

    def replay(self, sleeve: str) -> tuple[Decimal, dict[str, Decimal]]:
        cash = self.starting_cash(sleeve)
        positions: dict[str, Decimal] = {}
        for row in self.fills_for(sleeve):
            cash = q8(cash + D(row["cash_delta"]))
            symbol = str(row["symbol"])
            positions[symbol] = q8(positions.get(symbol, Decimal(0)) + D(row["qty_delta"]))
        positions = {k: v for k, v in positions.items() if v != 0}
        return cash, positions

    def reconcile(self, sleeve: str, now: datetime | None = None) -> tuple[bool, str]:
        cash, positions = self.replay(sleeve)
        stored_cash = q8(self.cash(sleeve))
        stored_positions = {k: q8(v) for k, v in self.positions(sleeve).items()}
        if cash != stored_cash:
            return False, f"{sleeve} cash {stored_cash} != replay {cash}"
        if positions != stored_positions:
            return False, f"{sleeve} positions {stored_positions} != replay {positions}"
        fill_rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE sleeve=?", (sleeve,)
        ).fetchone()
        if int(fill_rows["n"]) != self.count_events("fill", sleeve):
            return False, f"{sleeve} fill rows do not match fill events"
        stale = self.stale_open_orders(sleeve, now or utcnow())
        if stale:
            ids = ", ".join(str(order["client_order_id"]) for order in stale)
            return False, f"{sleeve} open order {ids} is stale"
        return True, "ok"

    def stale_open_orders(self, sleeve: str, now: datetime) -> list[dict]:
        """Open orders for one book older than one loop."""
        limit = self.settings.loop_seconds
        stale = []
        for order in self.open_orders():
            if str(order["sleeve"]) != sleeve:
                continue
            age = (now - parse_ts(str(order["ts"]))).total_seconds()
            if age > limit:
                stale.append(order)
        return stale

    def ensure_shadow_sleeve(self, name: str, now: datetime, initial_state: dict) -> None:
        row = self.conn.execute(
            "SELECT name FROM shadow_sleeves WHERE name=?", (name,)
        ).fetchone()
        if row is not None:
            return
        cash = money_str(self.settings.starting_cash)
        today = now.astimezone(timezone.utc).date().isoformat()
        self.conn.execute(
            """
            INSERT INTO shadow_sleeves
                (name, cash, starting_cash, day_start_equity, day_start_date, last_equity)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, cash, cash, cash, today, cash),
        )
        self.conn.execute(
            "INSERT INTO shadow_strategy_state(sleeve, state_json) VALUES(?, ?)",
            (name, canonical(initial_state)),
        )
        self.conn.commit()

    def shadow_sleeve_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM shadow_sleeves ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]

    def shadow_sleeve_row(self, sleeve: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM shadow_sleeves WHERE name=?", (sleeve,)
        ).fetchone()
        if row is None:
            raise KeyError(sleeve)
        return row

    def shadow_cash(self, sleeve: str) -> Decimal:
        return D(self.shadow_sleeve_row(sleeve)["cash"])

    def shadow_positions(self, sleeve: str) -> dict[str, Decimal]:
        rows = self.conn.execute(
            "SELECT symbol, qty FROM shadow_positions WHERE sleeve=? ORDER BY symbol",
            (sleeve,),
        ).fetchall()
        out: dict[str, Decimal] = {}
        for row in rows:
            qty = D(row["qty"])
            if qty != 0:
                out[str(row["symbol"])] = qty
        return out

    def shadow_strategy_state(self, sleeve: str) -> dict:
        row = self.conn.execute(
            "SELECT state_json FROM shadow_strategy_state WHERE sleeve=?",
            (sleeve,),
        ).fetchone()
        if row is None:
            return {}
        import json

        data = json.loads(row["state_json"])
        if not isinstance(data, dict):
            return {}
        return data

    def save_shadow_strategy_state(self, sleeve: str, state: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO shadow_strategy_state(sleeve, state_json) VALUES(?, ?)
            ON CONFLICT(sleeve) DO UPDATE SET state_json=excluded.state_json
            """,
            (sleeve, canonical(state)),
        )
        self.conn.commit()

    def shadow_fills_for(self, sleeve: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM shadow_fills WHERE sleeve=? ORDER BY id",
                (sleeve,),
            ).fetchall()
        )

    def shadow_activity_today(self, sleeve: str, day: str) -> tuple[int, Decimal]:
        rows = self.conn.execute(
            "SELECT notional FROM shadow_fills WHERE sleeve=? AND substr(ts, 1, 10)=?",
            (sleeve, day),
        ).fetchall()
        total = Decimal(0)
        for row in rows:
            total += D(row["notional"])
        return len(rows), q8(total)

    def shadow_strategy_trades_today(self, sleeve: str, day: str) -> int:
        """Strategy fills for one shadow book today. Risk-reduction sells are exempt."""
        placeholders = ",".join("?" for _ in RISK_REDUCTION_REASONS)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM shadow_fills
            WHERE sleeve=? AND substr(ts, 1, 10)=? AND reason NOT IN ({placeholders})
            """,
            (sleeve, day, *RISK_REDUCTION_REASONS),
        ).fetchone()
        return int(row["n"])

    def shadow_symbols_ordered_on(self, sleeve: str, day: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM shadow_fills WHERE sleeve=? AND substr(ts, 1, 10)=?",
            (sleeve, day),
        ).fetchall()
        return {str(row["symbol"]) for row in rows}

    def shadow_known_client_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT client_order_id FROM shadow_fills").fetchall()
        return {str(row["client_order_id"]) for row in rows}

    def get_shadow_fill(self, client_order_id: str) -> Fill | None:
        row = self.conn.execute(
            "SELECT * FROM shadow_fills WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return Fill(
            sleeve=str(row["sleeve"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            qty=D(row["qty"]),
            qty_delta=D(row["qty_delta"]),
            mid=D(row["mid"]),
            fill_price=D(row["fill_price"]),
            cash_delta=D(row["cash_delta"]),
            cost=D(row["cost"]),
            notional=D(row["notional"]),
            ts=parse_ts(str(row["ts"])),
            client_order_id=str(row["client_order_id"]),
            reason=str(row["reason"]),
        )

    def commit_shadow_fill(self, fill: Fill) -> None:
        """Apply one strategy fill to the no-overlay book. Does not touch the live sleeves."""
        with self.transaction():
            cash = self.shadow_cash(fill.sleeve)
            new_cash = q8(cash + fill.cash_delta)
            if new_cash < 0:
                raise OrderRejected(["insufficient_cash"])
            current = self.shadow_positions(fill.sleeve).get(fill.symbol, Decimal(0))
            new_pos = q8(current + fill.qty_delta)
            if new_pos < 0:
                raise OrderRejected(["insufficient_position"])
            self.conn.execute(
                "UPDATE shadow_sleeves SET cash=? WHERE name=?",
                (money_str(new_cash), fill.sleeve),
            )
            self.conn.execute(
                """
                INSERT INTO shadow_positions(sleeve, symbol, qty) VALUES(?, ?, ?)
                ON CONFLICT(sleeve, symbol) DO UPDATE SET qty=excluded.qty
                """,
                (fill.sleeve, fill.symbol, money_str(new_pos)),
            )
            self.conn.execute(
                """
                INSERT INTO shadow_fills(
                    sleeve, symbol, side, qty, qty_delta, mid, fill_price,
                    cash_delta, cost, notional, ts, client_order_id, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.sleeve,
                    fill.symbol,
                    fill.side,
                    money_str(fill.qty),
                    money_str(fill.qty_delta),
                    money_str(fill.mid),
                    money_str(fill.fill_price),
                    money_str(fill.cash_delta),
                    money_str(fill.cost),
                    money_str(fill.notional),
                    iso(fill.ts),
                    fill.client_order_id,
                    fill.reason,
                ),
            )
            payload = fill.event_payload()
            payload["book"] = "no_overlay"
            self.append_event("shadow_fill", payload, fill.ts)

    def mark_shadow_equity(self, sleeve: str, equity: Decimal, now: datetime) -> Decimal:
        row = self.shadow_sleeve_row(sleeve)
        today = now.astimezone(timezone.utc).date().isoformat()
        day_start = D(row["day_start_equity"])
        if str(row["day_start_date"]) != today:
            day_start = q8(equity)
        self.conn.execute(
            """
            UPDATE shadow_sleeves
            SET day_start_equity=?, day_start_date=?, last_equity=?
            WHERE name=?
            """,
            (money_str(day_start), today, money_str(equity), sleeve),
        )
        self.conn.commit()
        return day_start

    def shadow_snapshot(self, sleeve: str, equity: Decimal, now: datetime) -> None:
        self.conn.execute(
            """
            INSERT INTO shadow_equity_snapshots(ts, sleeve, equity, cash)
            VALUES(?, ?, ?, ?)
            """,
            (iso(now), sleeve, money_str(equity), self.shadow_sleeve_row(sleeve)["cash"]),
        )
        self.conn.commit()

    def shadow_snapshots(self, sleeve: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM shadow_equity_snapshots WHERE sleeve=? ORDER BY id",
                (sleeve,),
            ).fetchall()
        )

    def reconcile_shadow(self, sleeve: str) -> tuple[bool, str]:
        cash = D(self.shadow_sleeve_row(sleeve)["starting_cash"])
        positions: dict[str, Decimal] = {}
        for row in self.shadow_fills_for(sleeve):
            cash = q8(cash + D(row["cash_delta"]))
            symbol = str(row["symbol"])
            positions[symbol] = q8(positions.get(symbol, Decimal(0)) + D(row["qty_delta"]))
        positions = {k: v for k, v in positions.items() if v != 0}
        stored_cash = q8(self.shadow_cash(sleeve))
        stored_positions = {k: q8(v) for k, v in self.shadow_positions(sleeve).items()}
        if cash != stored_cash:
            return False, f"shadow {sleeve} cash {stored_cash} != replay {cash}"
        if positions != stored_positions:
            return False, f"shadow {sleeve} positions {stored_positions} != replay {positions}"
        return True, "ok"

    def event_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def verify_chain(self) -> tuple[bool, str]:
        rows = self.conn.execute(
            "SELECT seq, payload, prev_hash, hash FROM events ORDER BY seq"
        ).fetchall()
        prev = GENESIS
        for row in rows:
            if str(row["prev_hash"]) != prev:
                return False, f"chain break at seq {row['seq']}"
            digest = chain_hash(prev, str(row["payload"]))
            if digest != str(row["hash"]):
                return False, f"hash mismatch at seq {row['seq']}"
            prev = str(row["hash"])
        return True, f"{len(rows)} events"

    def integrity_ok(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return row is not None and str(row[0]) == "ok"

    def upsert_candles(self, bars: list[Bar], *, fetched_at: datetime | None = None) -> None:
        """Store a bar only when it was already closed at fetch time."""
        from datetime import timedelta

        when = fetched_at or datetime.now(timezone.utc)
        for bar in bars:
            if bar.ts + timedelta(days=1) > when:
                continue
            self.conn.execute(
                """
                INSERT INTO candles(
                    symbol, source, ts, open, high, low, close, volume, fetched_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, source, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    fetched_at=excluded.fetched_at
                """,
                (
                    bar.symbol,
                    bar.source,
                    iso(bar.ts),
                    money_str(bar.open),
                    money_str(bar.high),
                    money_str(bar.low),
                    money_str(bar.close),
                    money_str(bar.volume),
                    iso(when),
                ),
            )
        self.conn.commit()

    def load_candles_any(self, symbol: str) -> list[Bar]:
        rows = self.conn.execute(
            "SELECT * FROM candles WHERE symbol=? ORDER BY ts",
            (symbol,),
        ).fetchall()
        return [_bar_from_row(row) for row in rows]

    def load_candles(self, symbol: str, source: str) -> list[Bar]:
        rows = self.conn.execute(
            "SELECT * FROM candles WHERE symbol=? AND source=? ORDER BY ts",
            (symbol, source),
        ).fetchall()
        return [_bar_from_row(row) for row in rows]


def _bar_from_row(row: sqlite3.Row) -> Bar:
    return Bar(
        symbol=str(row["symbol"]),
        ts=parse_ts(str(row["ts"])),
        open=D(row["open"]),
        high=D(row["high"]),
        low=D(row["low"]),
        close=D(row["close"]),
        volume=D(row["volume"]),
        source=str(row["source"]),
    )
