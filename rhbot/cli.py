"""Operator CLI. Every command prints JSON except ``report --md``."""

from __future__ import annotations

import argparse
import json
import sys

from rhbot import __version__
from rhbot.config import load_settings
from rhbot.engine import Engine
from rhbot.ledger import Ledger
from rhbot.ops import (
    clear_freeze,
    clear_kill,
    engage_kill,
    freeze_active,
    kill_active,
    read_heartbeat,
    read_kill,
    resume_needs_ack,
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
    resume.set_defaults(func=cmd_resume)

    ack = sub.add_parser(
        "ack-drawdown",
        parents=[common],
        help="Operator acknowledgement of a paper drawdown freeze. Does not clear a kill.",
    )
    ack.add_argument(
        "--reason",
        required=True,
        help="Why the operator is acknowledging this freeze. Recorded in the audit log.",
    )
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


def cmd_resume(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, args.state_dir)
    if not kill_active(settings.state_dir):
        _emit({"ok": True, "kill_switch": False, "detail": "already_clear"})
        return 0
    payload = read_kill(settings.state_dir)
    needs_ack = resume_needs_ack(payload)
    if needs_ack and not args.ack:
        _emit(
            {
                "ok": False,
                "error": "this kill requires rhbot resume --ack",
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
    if needs_ack and (settings.state_dir / "bot.sqlite").exists():
        # Remember this peak so the same episode does not flatten again.
        # Sleeve peaks and the portfolio peak stay where they are.
        ledger = Ledger(settings)
        try:
            peak = ledger.get_meta("portfolio_peak") or ""
            if peak:
                ledger.set_meta("kill_ack_peak", peak)
        finally:
            ledger.close()
    clear_kill(settings.state_dir)
    actor = "human" if args.ack else "operator"
    _log_if_db(
        settings,
        "resume",
        {"by": actor, "ack": bool(args.ack), "peak_unchanged": True},
    )
    _emit({"ok": True, "kill_switch": False, "ack": bool(args.ack)})
    return 0


def cmd_ack_drawdown(args: argparse.Namespace) -> int:
    """Clear a paper buy-freeze. Does not touch the kill file or the drawdown peak.

    The actor is the AI operator. A 40% kill still requires a human
    ``rhbot resume --ack``. Neither command moves the portfolio peak.
    """
    reason = str(args.reason).strip()
    if not reason:
        _emit({"ok": False, "error": "ack-drawdown requires a reason"})
        return 2
    settings = load_settings(args.config, args.state_dir)
    if not freeze_active(settings.state_dir):
        _emit({"ok": True, "drawdown_freeze": False, "detail": "already_clear"})
        return 0
    clear_freeze(settings.state_dir)
    peak = ""
    actor = "operator"
    if (settings.state_dir / "bot.sqlite").exists():
        ledger = Ledger(settings)
        try:
            peak = ledger.get_meta("portfolio_peak") or ""
            if peak:
                ledger.set_meta("drawdown_ack_peak", peak)
            ledger.log_event(
                "drawdown_ack",
                {"actor": actor, "reason": reason, "peak": peak},
                utcnow(),
            )
        finally:
            ledger.close()
    _emit(
        {
            "ok": True,
            "drawdown_freeze": False,
            "actor": actor,
            "reason": reason,
            "ack_peak": peak,
            "peak_unchanged": True,
            "kill_switch": kill_active(settings.state_dir),
        }
    )
    return 0


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
    ok = not result["errors"]
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
