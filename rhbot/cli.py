"""Operator CLI. Every command prints JSON except ``report --md``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from rhbot import __version__
from rhbot.config import load_settings
from rhbot.engine import Engine
from rhbot.ledger import Ledger
from rhbot.ops import (
    clear_freeze,
    clear_kill,
    engage_kill,
    freeze_active,
    iso,
    kill_active,
    read_heartbeat,
    utcnow,
)
from rhbot.status import (
    assess,
    audit_verify,
    build_report,
    exit_code,
    parse_since,
    render_markdown,
)
from rhbot.selftest import run_selftest


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="Path to config.yaml")
    common.add_argument(
        "--state-dir",
        default=None,
        help="Directory for the ledger, heartbeat, and kill file",
    )
    parser = argparse.ArgumentParser(prog="rhbot", description="Paper-only BTC/ETH bot")
    parser.add_argument("--version", action="version", version=f"rhbot {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", parents=[common], help="Portfolio, heartbeat, and health")
    status.add_argument("--json", action="store_true", help="JSON output (the default)")
    status.set_defaults(func=cmd_status)

    health = sub.add_parser("health", parents=[common], help="Did the bot actually do something recently?")
    health.add_argument("--json", action="store_true", help="JSON output (the default)")
    health.set_defaults(func=cmd_health)

    report = sub.add_parser("report", parents=[common], help="Per-sleeve P&L versus buy and hold")
    report.add_argument("--since", default="24h", help="Window such as 24h, 7d, or 30d")
    report.add_argument("--json", action="store_true", help="JSON output (the default)")
    report.add_argument("--md", action="store_true", help="Print Markdown instead of JSON")
    report.set_defaults(func=cmd_report)

    kill = sub.add_parser("kill", parents=[common], help="Stop new simulated orders")
    kill.add_argument("--reason", required=True)
    kill.set_defaults(func=cmd_kill)

    resume = sub.add_parser("resume", parents=[common], help="Clear the kill file when health allows it")
    resume.add_argument(
        "--ack",
        action="store_true",
        help="Required after a 40% drawdown kill. Human acknowledgement. Does not move the peak.",
    )
    resume.add_argument(
        "--human-code",
        default=None,
        help="Secret from RHBOT_HUMAN_RESUME_FILE. Required for a drawdown kill.",
    )
    resume.set_defaults(func=cmd_resume)

    ack = sub.add_parser(
        "ack-drawdown",
        parents=[common],
        help="Acknowledge a paper freeze on one strategy book. Does not clear a kill or move the peak.",
    )
    ack.add_argument("--strategy", required=True, help="trend_daily or dca_weekly")
    ack.add_argument("--by", required=True, choices=("operator", "randy"))
    ack.add_argument("--note", required=True, help="Why this freeze is being acknowledged.")
    ack.set_defaults(func=cmd_ack_drawdown)

    flatten = sub.add_parser("flatten", parents=[common], help="Sell paper positions")
    flatten.add_argument("--paper", action="store_true", help="Required. Confirms this is the paper book.")
    flatten.set_defaults(func=cmd_flatten)

    selftest = sub.add_parser("selftest", parents=[common], help="Offline preflight, including the kill path")
    selftest.set_defaults(func=cmd_selftest)

    audit = sub.add_parser("audit", parents=[common], help="Audit log")
    audit_sub = audit.add_subparsers(dest="audit_cmd", required=True)
    verify = audit_sub.add_parser("verify", parents=[common], help="Check the hash chain")
    verify.set_defaults(func=cmd_audit)
    replay = audit_sub.add_parser("replay", parents=[common], help="Diff live decisions against a temp replay")
    replay.add_argument("--since", required=True, choices=("7d", "30d"))
    replay.set_defaults(func=cmd_audit_replay)

    run = sub.add_parser("run", parents=[common], help="Long-lived paper loop")
    run.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


def cmd_status(args: argparse.Namespace) -> int:
    body = assess(load_settings(args.config, args.state_dir))
    _emit(body)
    return exit_code(str(body["health"]))


def cmd_health(args: argparse.Namespace) -> int:
    body = assess(load_settings(args.config, args.state_dir))
    slim = {
        "health": body["health"],
        "reasons": body["reasons"],
        "checks": body["checks"],
        "running": body["running"],
        "kill_switch": body["kill_switch"],
        "last_heartbeat_at": body["last_heartbeat_at"],
        "last_decision_at": body["last_decision_at"],
        "last_quote_ok_at": body["last_quote_ok_at"],
        "last_auth_ok_at": body["last_auth_ok_at"],
        "last_successful_action_at": body["last_successful_action_at"],
        "seconds_since_successful_action": body["seconds_since_successful_action"],
        "data_age_seconds": body["data_age_seconds"],
        "error_counts": body["error_counts"],
    }
    _emit(slim)
    return exit_code(str(body["health"]))


def cmd_report(args: argparse.Namespace) -> int:
    try:
        parse_since(args.since)
    except ValueError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    body = build_report(load_settings(args.config, args.state_dir), args.since)
    if args.md:
        print(render_markdown(body))
    else:
        _emit(body)
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, args.state_dir)
    payload = engage_kill(settings.state_dir, args.reason, "operator")
    _log_if_db(settings, "kill", {"reason": args.reason, "by": "operator"})
    _emit({"ok": True, "kill_switch": True, "kill": payload})
    return 0


def _books_awaiting_human_resume(settings) -> list[str]:
    """Overlay books flattened by their own −40% kill and not yet human-acked."""
    if not (settings.state_dir / "bot.sqlite").exists():
        return []
    from rhbot.overlay import OVERLAY_BOOKS

    ledger = Ledger(settings)
    try:
        waiting = []
        for name in OVERLAY_BOOKS:
            row = ledger.overlay_row(name)
            if row is None:
                continue
            if str(row["state"]) == "KILLED" and not str(row["kill_acked_peak"] or ""):
                waiting.append(name)
        return waiting
    finally:
        ledger.close()


def cmd_resume(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, args.state_dir)
    global_on = kill_active(settings.state_dir)
    waiting = _books_awaiting_human_resume(settings)
    if not global_on and not waiting:
        _emit({"ok": True, "kill_switch": False, "detail": "already_clear"})
        return 0
    code_ok = _human_code_ok(args.human_code)
    # Every process-wide kill file, and every book still waiting on a human,
    # takes the same ack. A manual kill is not a lighter path.
    needs_ack = bool(waiting) or global_on
    if needs_ack and not args.ack:
        _emit(
            {
                "ok": False,
                "error": "this kill requires rhbot resume --ack --human-code",
                "ack_required": True,
            }
        )
        return 2
    if needs_ack and not code_ok:
        _emit(
            {
                "ok": False,
                "error": "resume requires --human-code matching RHBOT_HUMAN_RESUME_FILE",
                "ack_required": True,
            }
        )
        return 2
    body = assess(settings)
    blockers = [reason for reason in body["reasons"] if reason != "kill_switch"]
    if needs_ack:
        # The drawdown, and the daily loss that comes with it, are what --ack accepts.
        # The buy block still lasts until the next UTC day. Other critical checks do not.
        blockers = [
            reason
            for reason in blockers
            if reason not in ("drawdown_breach", "drawdown_freeze", "daily_loss")
        ]
    if body["health"] == "critical" and blockers:
        _emit(
            {
                "ok": False,
                "error": "refusing to resume while health is critical",
                "reasons": blockers,
            }
        )
        return 2
    baselines: dict[str, str] = {}
    if needs_ack and (settings.state_dir / "bot.sqlite").exists():
        # The all-time peak stays where it is. The restart baseline is this
        # book's mark-to-bid equity now, and later 10% and 40% lines use
        # max(baseline, the highest equity since this restart).
        ledger = Ledger(settings)
        try:
            from rhbot.money import money_str
            from rhbot.overlay import OVERLAY_BOOKS

            restarted_at = utcnow()
            anchor = _last_cycle_ts(ledger) or iso(restarted_at)
            for name in OVERLAY_BOOKS:
                row = ledger.overlay_row(name)
                if row is None or str(row["state"]) != "KILLED":
                    continue
                equity = _book_equity_at_bid(ledger, name)
                peak = str(row["peak"])
                recorded = money_str(equity)
                ledger.save_overlay(
                    name,
                    {
                        "kill_acked_peak": peak,
                        "restart_baseline": recorded,
                        "restart_high": recorded,
                    },
                )
                ledger.log_event(
                    "restart_baseline",
                    {
                        "sleeve": name,
                        "restart_baseline": recorded,
                        "peak": peak,
                        "ts": iso(restarted_at),
                        "anchor_ts": anchor,
                    },
                    restarted_at,
                )
                baselines[name] = recorded
        finally:
            ledger.close()
    if global_on:
        clear_kill(settings.state_dir)
    actor = "human" if code_ok else "operator"
    _log_if_db(
        settings,
        "resume",
        {"by": actor, "ack": bool(args.ack), "peak_unchanged": True},
    )
    _emit(
        {
            "ok": True,
            "kill_switch": False,
            "ack": bool(args.ack),
            "restart_baselines": baselines,
        }
    )
    return 0


def _last_cycle_ts(ledger: Ledger) -> str:
    row = ledger.conn.execute(
        "SELECT ts FROM events WHERE kind='cycle' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return ""
    return str(row["ts"])


def _book_equity_at_bid(ledger: Ledger, sleeve: str):
    """Mark-to-bid equity at the human restart. A flat book is its cash."""
    from rhbot.money import D, q8
    from rhbot.overlay import mark_to_bid_equity

    cash = ledger.cash(sleeve)
    positions = ledger.positions(sleeve)
    if not positions:
        return q8(cash)
    quotes = _last_valid_quotes(ledger)
    try:
        return mark_to_bid_equity(cash, positions, quotes, ledger.settings.cost_per_side)
    except RuntimeError:
        row = ledger.overlay_row(sleeve)
        if row is not None and row["equity"]:
            return D(row["equity"])
        return q8(cash)


def _last_valid_quotes(ledger: Ledger) -> dict:
    import json

    from rhbot.ledger import parse_ts
    from rhbot.models import Quote
    from rhbot.money import D

    raw = ledger.get_meta("last_valid_quotes")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    quotes = {}
    for symbol, item in payload.items():
        if not isinstance(item, dict) or item.get("source") in ("kraken", "robinhood"):
            continue
        try:
            quotes[str(symbol)] = Quote(
                symbol=str(symbol),
                ts=parse_ts(str(item["ts"])),
                mid=D(item["mid"]),
                bid=D(item["bid"]) if item.get("bid") else None,
                ask=D(item["ask"]) if item.get("ask") else None,
                source=str(item.get("source") or "coinbase"),
                spread_included=bool(item.get("spread_included")),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return quotes


def _human_code_ok(code: str | None) -> bool:
    path = os.environ.get("RHBOT_HUMAN_RESUME_FILE", "").strip()
    if not path or not code:
        return False
    file_path = Path(path)
    if not file_path.is_file():
        return False
    secret = file_path.read_text(encoding="utf-8").strip()
    return bool(secret) and secret == str(code).strip()


def cmd_ack_drawdown(args: argparse.Namespace) -> int:
    """Acknowledge one frozen book. Does not move the peak or clear a kill."""
    from rhbot.money import D
    from rhbot.overlay import OVERLAY_BOOKS, signed_drawdown

    note = str(args.note).strip()
    if not note:
        _emit({"ok": False, "error": "ack-drawdown requires a note"})
        return 2
    if args.strategy not in OVERLAY_BOOKS:
        _emit({"ok": False, "error": "strategy is not an overlay book"})
        return 2
    settings = load_settings(args.config, args.state_dir)
    if not (settings.state_dir / "bot.sqlite").exists():
        _emit({"ok": False, "error": "no ledger"})
        return 2
    ledger = Ledger(settings)
    try:
        row = ledger.overlay_row(args.strategy)
        if row is None or str(row["state"]) != "FROZEN":
            _emit({"ok": False, "error": "book is not FROZEN", "strategy": args.strategy})
            return 2
        ok, detail = ledger.reconcile(args.strategy)
        if not ok:
            _emit({"ok": False, "error": "reconcile failed", "detail": detail})
            return 2
        trip_equity = D(row["trip_equity"] or "0")
        trip_peak = D(row["trip_peak"] or "0")
        trip_dd = D(row["trip_dd"] or "0")
        recomputed = signed_drawdown(trip_equity, trip_peak)
        if abs(recomputed - trip_dd) > Decimal("0.000001"):
            _emit(
                {
                    "ok": False,
                    "error": "recomputed drawdown does not match the trip",
                    "recomputed_dd": format(recomputed, "f"),
                    "trip_dd": format(trip_dd, "f"),
                }
            )
            return 2
        peak = str(row["peak"])
        ledger.save_overlay(
            args.strategy,
            {
                "state": "ACKED",
                "ack_ts": iso(utcnow()),
                "ack_by": args.by,
                "ack_note": note,
            },
        )
        ledger.log_event(
            "freeze_ack",
            {
                "sleeve": args.strategy,
                "by": args.by,
                "note": note,
                "equity": str(row["equity"]),
                "peak": peak,
                "dd": str(row["dd"]),
                "notify_randy": True,
            },
            utcnow(),
        )
    finally:
        ledger.close()
    _emit(
        {
            "ok": True,
            "notify_randy": True,
            "strategy": args.strategy,
            "by": args.by,
            "note": note,
            "state": "ACKED",
            "peak_unchanged": True,
            "peak": peak,
            "kill_switch": kill_active(settings.state_dir),
        }
    )
    return 0


def cmd_audit_replay(args: argparse.Namespace) -> int:
    from rhbot.backtest import diff_live

    settings = load_settings(args.config, args.state_dir)
    try:
        body = diff_live(settings, args.since)
    except Exception as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2
    _emit(body)
    return 0 if body.get("ok") else 2


def cmd_flatten(args: argparse.Namespace) -> int:
    if not args.paper:
        _emit({"ok": False, "error": "refusing to flatten without --paper"})
        return 2
    settings = load_settings(args.config, args.state_dir)
    engine = Engine(settings)
    try:
        result = engine.flatten()
    finally:
        engine.ledger.close()
    ok = not result["errors"] and not result.get("remaining")
    _emit({"ok": ok, **result})
    return 0 if ok else 2


def cmd_selftest(args: argparse.Namespace) -> int:
    result = run_selftest(load_settings(args.config, args.state_dir))
    _emit(result)
    return 0 if result["ok"] else 2


def cmd_audit(args: argparse.Namespace) -> int:
    del args.audit_cmd
    result = audit_verify(load_settings(args.config, args.state_dir))
    _emit(result)
    return 0 if result["ok"] else 2


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, args.state_dir)
    engine = Engine(settings)
    try:
        engine.serve(once=bool(args.once))
    finally:
        engine.ledger.close()
    return 0


def _log_if_db(settings, kind: str, data: dict) -> None:
    path = settings.state_dir / "bot.sqlite"
    if not path.exists():
        return
    ledger = Ledger(settings)
    try:
        ledger.log_event(kind, data, utcnow())
        heartbeat = read_heartbeat(settings.state_dir)
        if heartbeat and not heartbeat.get("_unreadable"):
            from rhbot.money import canonical
            from rhbot.ops import atomic_write

            heartbeat["audit_head"] = ledger.head_hash()
            atomic_write(settings.state_dir / "heartbeat.json", canonical(heartbeat))
    finally:
        ledger.close()


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


if __name__ == "__main__":
    sys.exit(main())
