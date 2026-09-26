"""Operator views: status, health, report, audit. Read-only except where noted."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rhbot.config import Settings
from rhbot.ledger import Ledger, parse_ts
from rhbot.money import D, money_str, q8
from rhbot.ops import iso, read_freeze, read_heartbeat, read_kill, resume_needs_ack, utcnow
from rhbot.risk import daily_buy_block, kill_reason, peak_drawdown

SLEEVES = ("buy_and_hold", "dca_weekly", "trend_daily")
RANK = {"ok": 0, "idle_ok": 1, "degraded": 2, "critical": 3}


def _db_path(settings: Settings) -> Path:
    return Path(settings.state_dir) / "bot.sqlite"


def _age_seconds(ts: str | None, now: datetime) -> int | None:
    if not ts:
        return None
    try:
        then = parse_ts(ts)
    except ValueError:
        return None
    return int((now - then).total_seconds())


def _bump(level: str, reason: str, current: str, reasons: list[str]) -> str:
    if reason not in reasons:
        reasons.append(reason)
    if RANK[level] > RANK[current]:
        return level
    return current


def assess(settings: Settings, now: datetime | None = None) -> dict:
    """Health plus the status fields an operator needs in one document."""
    now = now or utcnow()
    reasons: list[str] = []
    level = "ok"
    checks: dict[str, dict] = {}
    heartbeat = read_heartbeat(settings.state_dir)
    kill = read_kill(settings.state_dir)
    freeze = read_freeze(settings.state_dir)
    portfolio_dd = Decimal(0)
    portfolio_peak = Decimal(0)
    acknowledged = False
    drawdown_acks: list[dict] = []
    db_exists = _db_path(settings).exists()
    limit = max(180, settings.loop_seconds * 3)

    if heartbeat and heartbeat.get("_unreadable"):
        level = _bump("critical", "heartbeat_unreadable", level, reasons)
        checks["heartbeat"] = {"ok": False}
    elif heartbeat and heartbeat.get("ts"):
        age = _age_seconds(str(heartbeat.get("ts")), now)
        fresh = age is not None and 0 <= age <= limit
        checks["heartbeat"] = {"ok": fresh, "age_seconds": age}
        if not fresh:
            level = _bump("critical", "heartbeat_stale", level, reasons)
    else:
        checks["heartbeat"] = {"ok": not db_exists, "age_seconds": None}
        if db_exists:
            level = _bump("critical", "no_heartbeat", level, reasons)

    if kill is not None:
        level = _bump("critical", "kill_switch", level, reasons)
        checks["kill_switch"] = {"ok": False, "engaged": True, "reason": kill.get("reason")}
    else:
        checks["kill_switch"] = {"ok": True, "engaged": False}

    positions: dict[str, dict[str, str]] = {}
    cash: dict[str, str] = {}
    equity: dict[str, str] = {}
    today_pnl: dict[str, str] = {}
    drawdowns: dict[str, str] = {}
    open_orders: list[dict] = []
    error_counts = {"fetch": 0, "loop": 0, "risk_reject": 0}
    quote_source = None
    last_decision_at = None
    last_quote_ok_at = None
    last_auth_ok_at = None
    last_successful = None
    last_quote_ts = None
    data_age = None

    if not db_exists:
        if level == "ok":
            level = "idle_ok"
            reasons.append("not_started")
        checks["reconciliation"] = {"ok": True, "skipped": True}
        checks["db_integrity"] = {"ok": True, "skipped": True}
        checks["quotes"] = {"ok": True, "skipped": True}
    else:
        ledger = Ledger(settings)
        try:
            integrity = ledger.integrity_ok()
            checks["db_integrity"] = {"ok": integrity}
            if not integrity:
                level = _bump("critical", "db_integrity", level, reasons)
            recon_ok = True
            recon_detail = "ok"
            for sleeve in ledger.sleeve_names():
                ok, detail = ledger.reconcile(sleeve)
                if not ok:
                    recon_ok = False
                    recon_detail = detail
                row = ledger.sleeve_row(sleeve)
                positions[sleeve] = {
                    symbol: money_str(qty) for symbol, qty in ledger.positions(sleeve).items()
                }
                cash[sleeve] = money_str(D(row["cash"]))
                equity[sleeve] = money_str(D(row["last_equity"]))
                today_pnl[sleeve] = money_str(D(row["last_equity"]) - D(row["day_start_equity"]))
                peak = D(row["peak_equity"])
                last_eq = D(row["last_equity"])
                dd = Decimal(0) if peak <= 0 else (peak - last_eq) / peak
                drawdowns[sleeve] = money_str(dd)
                blocked = daily_buy_block(last_eq, D(row["day_start_equity"]), settings)
                if blocked:
                    level = _bump("degraded", "daily_loss", level, reasons)
            checks["reconciliation"] = {"ok": recon_ok, "detail": recon_detail}
            if not recon_ok:
                level = _bump("critical", "reconciliation", level, reasons)
            if not ledger.sleeve_names() and heartbeat is None and level == "ok":
                level = "idle_ok"
                reasons.append("not_started")
            error_counts = {
                "fetch": ledger.meta_int("error_fetch"),
                "loop": ledger.meta_int("error_loop"),
                "risk_reject": ledger.meta_int("error_risk_reject"),
            }
            failures = ledger.meta_int("consecutive_failures")
            checks["errors"] = {"ok": failures == 0, "consecutive_failures": failures}
            if failures >= 3:
                level = _bump("critical", "repeated_failures", level, reasons)
            elif failures >= 1:
                level = _bump("degraded", "recent_failures", level, reasons)
            last_decision_at = ledger.get_meta("last_decision_at")
            last_quote_ok_at = ledger.get_meta("last_quote_ok_at")
            last_auth_ok_at = ledger.get_meta("last_auth_ok_at")
            last_successful = ledger.get_meta("last_successful_action_at")
            last_quote_ts = ledger.get_meta("last_quote_ts")
            quote_source = ledger.get_meta("quote_source")
            data_age = _age_seconds(last_quote_ts, now)
            quotes_ok = data_age is not None and 0 <= data_age <= settings.max_quote_age_seconds
            checks["quotes"] = {"ok": quotes_ok, "age_seconds": data_age, "source": quote_source}
            if ledger.event_count() and not quotes_ok:
                level = _bump("critical", "stale_market_data", level, reasons)
            action_age = _age_seconds(last_successful, now)
            checks["recent_action"] = {"ok": action_age is not None and action_age <= limit, "age_seconds": action_age}
            if heartbeat and action_age is None:
                level = _bump("degraded", "no_successful_action", level, reasons)
            elif heartbeat and action_age is not None and action_age > limit:
                level = _bump("critical", "loop_not_completing", level, reasons)
            decision_age = _age_seconds(last_decision_at, now)
            if heartbeat and decision_age is not None and decision_age > 36 * 3600:
                level = _bump("degraded", "no_recent_decision", level, reasons)
            open_orders = ledger.open_orders()
            if quote_source == "public_fallback":
                level = _bump("degraded", "robinhood_quotes_unavailable", level, reasons)
            combined = Decimal(0)
            for value in equity.values():
                combined += D(value)
            stored_peak = ledger.get_meta("portfolio_peak")
            portfolio_peak = D(stored_peak) if stored_peak else combined
            portfolio_dd = peak_drawdown(combined, portfolio_peak)
            # Paper-only combined drawdown. 40% is the hard kill. 10% is a buy freeze.
            # These limits must not be carried into a live phase.
            if kill_reason(combined, portfolio_peak, settings):
                level = _bump("critical", "drawdown_breach", level, reasons)
            acknowledged = (ledger.get_meta("drawdown_ack_peak") or "") == money_str(portfolio_peak)
            drawdown_acks = _ack_records(ledger, None)
        finally:
            ledger.close()

    freeze_alert = freeze is not None or (
        portfolio_dd >= settings.pause_drawdown_pct and not acknowledged
    )
    if freeze_alert:
        level = _bump("degraded", "drawdown_freeze", level, reasons)
        checks["drawdown_freeze"] = {
            "ok": False,
            "engaged": freeze is not None,
            "drawdown": money_str(portfolio_dd),
        }
    else:
        checks["drawdown_freeze"] = {
            "ok": True,
            "engaged": False,
            "drawdown": money_str(portfolio_dd),
        }

    free = shutil.disk_usage(settings.state_dir if Path(settings.state_dir).exists() else Path(".")).free
    if free < 100_000_000:
        level = _bump("critical", "low_disk", level, reasons)
        checks["disk"] = {"ok": False, "free_bytes": free}
    elif free < 500_000_000:
        level = _bump("degraded", "low_disk", level, reasons)
        checks["disk"] = {"ok": False, "free_bytes": free}
    else:
        checks["disk"] = {"ok": True, "free_bytes": free}

    total_pnl = Decimal(0)
    for value in today_pnl.values():
        total_pnl += D(value)
    worst = Decimal(0)
    for value in drawdowns.values():
        worst = max(worst, D(value))
    running = bool(heartbeat) and not heartbeat.get("_unreadable") and checks.get("heartbeat", {}).get("ok")
    return {
        "mode": "paper",
        "running": bool(running),
        "kill_switch": kill is not None,
        "buy_pause": freeze is not None,
        "peak_equity": money_str(portfolio_peak) if equity else None,
        "drawdown_pct": money_str(portfolio_dd),
        "ack_required": (kill is not None and resume_needs_ack(kill)) or freeze is not None,
        "rearm_eligible": freeze is None and portfolio_dd < settings.pause_drawdown_pct,
        "kill_reason": None if kill is None else kill.get("reason"),
        "drawdown_freeze": freeze is not None,
        "drawdown_acks": drawdown_acks,
        "last_heartbeat_at": None if not heartbeat else heartbeat.get("ts"),
        "last_decision_at": last_decision_at,
        "last_quote_ok_at": last_quote_ok_at,
        "last_auth_ok_at": last_auth_ok_at,
        "last_successful_action_at": last_successful,
        "seconds_since_successful_action": _age_seconds(last_successful, now),
        "data_age_seconds": data_age,
        "error_counts": error_counts,
        "positions": positions,
        "cash": cash,
        "equity": equity,
        "open_orders": open_orders,
        "today_pnl": {"total": money_str(total_pnl), "sleeves": today_pnl},
        "drawdown": {
            "worst": money_str(worst),
            "portfolio": money_str(portfolio_dd),
            "sleeves": drawdowns,
        },
        "quote_source": quote_source,
        "health": level,
        "reasons": reasons,
        "checks": checks,
    }


def parse_since(text: str) -> timedelta:
    import re

    match = re.fullmatch(r"(\d+)([hd])", text.strip())
    if not match:
        raise ValueError("since must look like 24h, 7d, or 30d")
    count = int(match.group(1))
    if count <= 0:
        raise ValueError("since must be positive")
    if match.group(2) == "h":
        return timedelta(hours=count)
    return timedelta(days=count)


def build_report(settings: Settings, since_text: str, now: datetime | None = None) -> dict:
    now = now or utcnow()
    window = parse_since(since_text)
    start = now - window
    if not _db_path(settings).exists():
        return {
            "since": since_text,
            "from": iso(start),
            "to": iso(now),
            "started": False,
            "starting_cash_per_sleeve": money_str(settings.starting_cash),
            "cost_per_side": format(settings.cost_per_side, "f"),
            "sleeves": {},
        }
    ledger = Ledger(settings)
    try:
        sleeves: dict[str, dict] = {}
        for name in SLEEVES:
            if name not in ledger.sleeve_names():
                continue
            sleeves[name] = _sleeve_report(ledger, name, start)
        shadow: dict[str, dict] = {}
        for name in SLEEVES:
            if name not in ledger.shadow_sleeve_names():
                continue
            shadow[name] = _shadow_report(ledger, name, start)
            real = sleeves.get(name)
            if real is None:
                shadow[name]["overlay_effect"] = None
                continue
            equity_delta = q8(D(real["equity"]) - D(shadow[name]["equity"]))
            return_delta = q8(
                D(real["since_start"]["return_pct"]) - D(shadow[name]["since_start"]["return_pct"])
            )
            shadow[name]["overlay_effect"] = {
                "equity_delta": money_str(equity_delta),
                "return_delta_pct": format(return_delta, "f"),
            }
        using_shadow_benchmark = "buy_and_hold" in shadow
        if using_shadow_benchmark:
            benchmark = shadow["buy_and_hold"]["window"]["return_pct"]
        else:
            benchmark = sleeves.get("buy_and_hold", {}).get("window", {}).get("return_pct")
        denials = []
        fidelity_ok = True
        for name, body in sleeves.items():
            if benchmark is None or (name == "buy_and_hold" and not using_shadow_benchmark):
                body["excess_return_vs_buy_and_hold_pct"] = None
            else:
                excess = D(body["window"]["return_pct"]) - D(benchmark)
                body["excess_return_vs_buy_and_hold_pct"] = format(q8(excess), "f")
            denials.extend(body["risk_denials"])
            if not body["fidelity"]["ok"]:
                fidelity_ok = False
        trips = _risk_records(ledger, "kill_trip", None, start)
        freezes = _risk_records(ledger, "drawdown_freeze", None, start)
        acks = _ack_records(ledger, start)
        return {
            "since": since_text,
            "from": iso(start),
            "to": iso(now),
            "started": True,
            "starting_cash_per_sleeve": money_str(settings.starting_cash),
            "cost_per_side": format(settings.cost_per_side, "f"),
            "risk_denials": denials,
            "drawdown_freezes": freezes,
            "drawdown_acks": acks,
            "kill_trips": trips,
            "fidelity_ok": fidelity_ok,
            "sleeves": sleeves,
            "no_overlay": {
                "benchmark": "buy_and_hold",
                "benchmark_scored_without_overlay": using_shadow_benchmark,
                "benchmark_return_pct": benchmark,
                "sleeves": shadow,
            },
        }
    finally:
        ledger.close()


def _sleeve_report(ledger: Ledger, sleeve: str, start: datetime) -> dict:
    row = ledger.sleeve_row(sleeve)
    starting = D(row["starting_cash"])
    last_equity = D(row["last_equity"])
    snaps = ledger.snapshots(sleeve)
    prior = None
    in_window = []
    for snap in snaps:
        ts = parse_ts(str(snap["ts"]))
        if ts < start:
            prior = snap
        else:
            in_window.append(snap)
    if prior is not None:
        start_equity = D(prior["equity"])
    else:
        start_equity = starting
    end_equity = D(in_window[-1]["equity"]) if in_window else last_equity
    window_pnl = q8(end_equity - start_equity)
    window_return = Decimal(0) if start_equity == 0 else (window_pnl / start_equity) * Decimal(100)
    peak = start_equity
    max_dd = Decimal(0)
    series = []
    if prior is not None:
        series.append(D(prior["equity"]))
    series.extend(D(snap["equity"]) for snap in in_window)
    if not series:
        series.append(last_equity)
    for equity in series:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    fees = Decimal(0)
    trades = 0
    for fill in ledger.fills_for(sleeve):
        if parse_ts(str(fill["ts"])) >= start:
            fees += D(fill["cost"])
            trades += 1
    denials = _risk_records(ledger, "risk_denial", sleeve, start)
    trips = _risk_records(ledger, "kill_trip", sleeve, start)
    freezes = _risk_records(ledger, "drawdown_freeze", sleeve, start)
    fidelity_ok, fidelity_detail = ledger.reconcile(sleeve)
    since_pnl = q8(last_equity - starting)
    since_return = Decimal(0) if starting == 0 else (since_pnl / starting) * Decimal(100)
    return {
        "equity": money_str(last_equity),
        "cash": money_str(D(row["cash"])),
        "since_start": {
            "pnl": money_str(since_pnl),
            "return_pct": format(q8(since_return), "f"),
        },
        "window": {
            "pnl": money_str(window_pnl),
            "return_pct": format(q8(window_return), "f"),
            "fees": money_str(fees),
            "trades": trades,
            "max_drawdown_pct": format(q8(max_dd * Decimal(100)), "f"),
        },
        "risk_denials": denials,
        "drawdown_freezes": freezes,
        "kill_trips": trips,
        "fidelity": {"ok": fidelity_ok, "detail": fidelity_detail},
    }


def _shadow_report(ledger: Ledger, sleeve: str, start: datetime) -> dict:
    """No-overlay sleeve: same strategy, without the 10% freeze or the 40% kill."""
    row = ledger.shadow_sleeve_row(sleeve)
    starting = D(row["starting_cash"])
    last_equity = D(row["last_equity"])
    snaps = ledger.shadow_snapshots(sleeve)
    prior = None
    in_window = []
    for snap in snaps:
        ts = parse_ts(str(snap["ts"]))
        if ts < start:
            prior = snap
        else:
            in_window.append(snap)
    if prior is not None:
        start_equity = D(prior["equity"])
    else:
        start_equity = starting
    end_equity = D(in_window[-1]["equity"]) if in_window else last_equity
    window_pnl = q8(end_equity - start_equity)
    window_return = Decimal(0) if start_equity == 0 else (window_pnl / start_equity) * Decimal(100)
    fees = Decimal(0)
    trades = 0
    for fill in ledger.shadow_fills_for(sleeve):
        if parse_ts(str(fill["ts"])) >= start:
            fees += D(fill["cost"])
            trades += 1
    fidelity_ok, fidelity_detail = ledger.reconcile_shadow(sleeve)
    since_pnl = q8(last_equity - starting)
    since_return = Decimal(0) if starting == 0 else (since_pnl / starting) * Decimal(100)
    return {
        "equity": money_str(last_equity),
        "cash": money_str(D(row["cash"])),
        "positions": {
            symbol: money_str(qty) for symbol, qty in ledger.shadow_positions(sleeve).items()
        },
        "since_start": {
            "pnl": money_str(since_pnl),
            "return_pct": format(q8(since_return), "f"),
        },
        "window": {
            "pnl": money_str(window_pnl),
            "return_pct": format(q8(window_return), "f"),
            "fees": money_str(fees),
            "trades": trades,
        },
        "fidelity": {"ok": fidelity_ok, "detail": fidelity_detail},
    }


def _ack_records(ledger: Ledger, start: datetime | None) -> list[dict]:
    """Operator acknowledgements of a paper drawdown freeze."""
    found = []
    for event in ledger.conn.execute(
        "SELECT payload FROM events WHERE kind='drawdown_ack' ORDER BY seq"
    ):
        payload = json.loads(event["payload"])
        if start is not None and parse_ts(str(payload["ts"])) < start:
            continue
        found.append(
            {
                "ts": payload.get("ts") or "",
                "actor": payload.get("actor") or payload.get("by") or "",
                "reason": payload.get("reason") or "",
                "peak": payload.get("peak") or "",
            }
        )
    return found


def _risk_records(
    ledger: Ledger, kind: str, sleeve: str | None, start: datetime
) -> list[dict]:
    """Audit rows for one sleeve, or every sleeve when ``sleeve`` is None.

    These are risk blocks, freezes, and kill trips, not fill-replay mismatches.
    """
    found = []
    for event in ledger.conn.execute("SELECT payload FROM events WHERE kind=?", (kind,)):
        payload = json.loads(event["payload"])
        if sleeve is not None and payload.get("sleeve") != sleeve:
            continue
        if parse_ts(str(payload["ts"])) < start:
            continue
        found.append(
            {
                "ts": payload["ts"],
                "sleeve": payload.get("sleeve") or "",
                "symbol": payload.get("symbol") or "",
                "side": payload.get("side") or "",
                "reason": payload.get("reason") or "",
                "limit_name": payload.get("limit_name") or "",
                "limit": payload.get("limit") or "",
                "observed": payload.get("observed") or "",
                "detail": payload.get("detail") or "",
                "ack_required": bool(payload.get("ack_required")),
            }
        )
    return found


def render_markdown(report: dict) -> str:
    lines = [
        f"# Paper report ({report['since']})",
        "",
        f"From {report['from']} to {report['to']}.",
        f"Starting cash per sleeve: {report['starting_cash_per_sleeve']}.",
        f"Cost per side: {report['cost_per_side']}.",
        "",
    ]
    if not report.get("started"):
        lines.append("The bot has not started, so there is no P&L yet.")
        return "\n".join(lines)
    lines.append(
        "| Sleeve | Window P&L | Return % | Fees | Trades | Max DD % | vs buy & hold | Risk blocks | Freezes | Kills | Fidelity |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, body in report["sleeves"].items():
        window = body["window"]
        excess = body.get("excess_return_vs_buy_and_hold_pct")
        fidelity = "ok" if body["fidelity"]["ok"] else "mismatch"
        lines.append(
            f"| {name} | {window['pnl']} | {window['return_pct']} | {window['fees']} | "
            f"{window['trades']} | {window['max_drawdown_pct']} | "
            f"{excess if excess is not None else 'benchmark'} | {len(body['risk_denials'])} | "
            f"{len(body.get('drawdown_freezes', []))} | {len(body['kill_trips'])} | {fidelity} |"
        )
    no_overlay = report.get("no_overlay") or {}
    if no_overlay.get("sleeves"):
        lines.append("")
        scored = "without the drawdown overlay" if no_overlay.get("benchmark_scored_without_overlay") else "from the live book"
        lines.append(
            f"Buy-and-hold benchmark is scored {scored}. "
            f"Window return: {no_overlay.get('benchmark_return_pct')}."
        )
        lines.append("")
        lines.append("| No-overlay sleeve | Equity | Return % since start | Overlay equity delta |")
        lines.append("| --- | --- | --- | --- |")
        for name, body in no_overlay["sleeves"].items():
            effect = body.get("overlay_effect") or {}
            lines.append(
                f"| {name} | {body['equity']} | {body['since_start']['return_pct']} | "
                f"{effect.get('equity_delta', '')} |"
            )
    if report.get("drawdown_acks"):
        lines.append("")
        lines.append("Drawdown freeze acknowledgements. These do not reset the peak and do not clear a kill.")
        for item in report["drawdown_acks"]:
            lines.append(f"- ack {item['ts']} actor {item['actor']}: {item['reason']} peak {item['peak']}")
    if report.get("risk_denials") or report.get("drawdown_freezes") or report.get("kill_trips"):
        lines.append("")
        lines.append(
            "Risk blocks, drawdown freezes, and kill trips are separate from a fidelity mismatch. "
            "A fidelity mismatch is a cash or position replay that does not match the fills."
        )
        for item in report.get("risk_denials", []):
            lines.append(
                f"- denial {item['sleeve']} {item['symbol']} {item['side']}: {item['reason']} "
                f"limit {item['limit_name']}={item['limit']} observed {item['observed']}"
            )
        for item in report.get("drawdown_freezes", []):
            lines.append(
                f"- freeze {item['sleeve']}: {item['reason']} "
                f"limit {item['limit_name']}={item['limit']} observed {item['observed']}"
            )
        for item in report.get("kill_trips", []):
            lines.append(
                f"- kill {item['sleeve']}: {item['reason']} "
                f"limit {item['limit_name']}={item['limit']} observed {item['observed']}"
            )
    lines.append("")
    return "\n".join(lines)


def audit_verify(settings: Settings) -> dict:
    if not _db_path(settings).exists():
        return {"ok": True, "events": 0, "detail": "not_started", "audit_head": None}
    ledger = Ledger(settings)
    try:
        ok, detail = ledger.verify_chain()
        head = ledger.head_hash()
        heartbeat = read_heartbeat(settings.state_dir)
        if (
            ok
            and heartbeat
            and not heartbeat.get("_unreadable")
            and heartbeat.get("audit_head")
            and heartbeat.get("audit_head") != head
        ):
            ok = False
            detail = "heartbeat audit head does not match the ledger"
        return {
            "ok": ok,
            "events": ledger.event_count(),
            "detail": detail,
            "audit_head": head,
        }
    finally:
        ledger.close()


def exit_code(health: str) -> int:
    if health in ("ok", "idle_ok"):
        return 0
    if health == "degraded":
        return 1
    return 2
