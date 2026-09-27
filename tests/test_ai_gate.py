"""AI advisory gate on trend_daily. Every subprocess is faked; nothing leaves the machine."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

import rhbot.ai_gate as ai_gate
from rhbot.ai_gate import (
    APPROVE,
    KILL_FILE,
    VETO,
    AIGate,
    GateContext,
    Verdict,
    build_command,
    build_context,
    build_prompt,
    child_env,
    parse_output,
    review,
    run_command,
)
from rhbot.backtest import replay
from rhbot.config import AIGateSettings, Settings
from rhbot.engine import Engine
from rhbot.models import OrderIntent

from tests.conftest import make_bars, make_settings, padded_closes, snapshot

APPROVE_TEXT = "DECISION: APPROVE\nCONFIDENCE: 0.82\nREASON: Price has held above the average for days.\n"
VETO_TEXT = "DECISION: VETO\nCONFIDENCE: 0.7\nREASON: The move is a one-bar spike.\n"
ENTER = OrderIntent("BTC-USD", "buy", "trend_entry", quote_amount=Decimal("250"))


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not start a real process")

    monkeypatch.setattr(ai_gate.subprocess, "Popen", refuse)
    monkeypatch.setattr(ai_gate.shutil, "which", lambda name: None)


def _context(symbol="BTC-USD", action="ENTER") -> GateContext:
    return GateContext(
        utc_date="2026-03-16",
        symbol=symbol,
        action=action,
        last_close=Decimal("12"),
        sma_distance_pct=Decimal("19.00"),
        last_returns_pct=(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("20.00")),
        position="flat",
        entry_price=None,
        book_drawdown_pct=Decimal("0"),
        overlay_state="ARMED",
    )


class FakeRunner:
    """Stands in for run_command. Each entry is a CompletedProcess or an exception."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin, timeout, env, cwd):
        self.calls.append({"argv": argv, "stdin": stdin, "timeout": timeout, "env": env, "cwd": cwd})
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["x"], code, stdout, stderr)


def _gate(runner, *, found=("cursor", "codex"), **config) -> AIGate:
    return AIGate(
        AIGateSettings(**config),
        runner=runner,
        resolver=lambda backend: f"/fake/{backend}" if backend in found else None,
    )


class FakeGate:
    """Engine-level stand-in that records what it was asked."""

    def __init__(self, decision=APPROVE, *, retry=False):
        self.decision = decision
        self.retry = retry
        self.asked: list[tuple[OrderIntent, GateContext]] = []

    def review(self, intent, context, *, calls_used=0, deadline=None):
        del calls_used, deadline
        self.asked.append((intent, context))
        return Verdict(
            decision=self.decision,
            confidence=0.9 if self.decision == APPROVE else 0.0,
            reason=f"fake {self.decision.lower()}",
            backend="cursor" if not self.retry else "none",
            latency_ms=5,
            backend_calls=0 if self.retry else 1,
            retry=self.retry,
        )


def _hot(now):
    """Both coins 20% over their 200-day average: trend wants to enter both."""
    return snapshot(now, mid="12", closes=padded_closes("12"))


def _events(bot, kind):
    rows = bot.ledger.conn.execute(
        "SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,)
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _fills(bot, sleeve):
    return [dict(row) for row in bot.ledger.fills_for(sleeve)]


# --- parsing and backends ---------------------------------------------------


def test_approve_is_parsed():
    runner = FakeRunner(_done(APPROVE_TEXT))
    verdict = _gate(runner).review(ENTER, _context())
    assert verdict.decision == APPROVE and verdict.approved
    assert verdict.confidence == pytest.approx(0.82)
    assert verdict.reason == "Price has held above the average for days."
    assert verdict.backend == "cursor"
    assert verdict.backend_calls == 1
    assert verdict.latency_ms >= 0


def test_veto_is_parsed():
    verdict = _gate(FakeRunner(_done(VETO_TEXT))).review(ENTER, _context())
    assert verdict.decision == VETO and not verdict.approved
    assert verdict.confidence == pytest.approx(0.7)
    assert verdict.reason == "The move is a one-bar spike."


def test_lenient_parse_accepts_markdown_and_percent():
    parsed = parse_output("Sure.\n**DECISION:** approve\n- CONFIDENCE: 85%\nREASON: Steady climb. Extra words.")
    assert parsed is not None
    assert parsed.decision == APPROVE
    assert parsed.confidence == pytest.approx(0.85)
    assert parsed.reason == "Steady climb."


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I think this looks fine.",
        "DECISION: APPROVE\nREASON: missing confidence",
        "DECISION: MAYBE\nCONFIDENCE: 0.5\nREASON: unsure.",
        "DECISION: <APPROVE|VETO>\nCONFIDENCE: 0.5\nREASON: template echo.",
        "DECISION: APPROVE\nCONFIDENCE: 250\nREASON: out of range.",
        "DECISION: APPROVE\nCONFIDENCE: 0.9\nREASON:   ",
    ],
)
def test_unparseable_output_is_a_veto(text):
    assert parse_output(text) is None
    verdict = _gate(FakeRunner(_done(text)), found=("cursor",)).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "unparseable" in verdict.reason


def test_timeout_is_a_veto():
    runner = FakeRunner(
        subprocess.TimeoutExpired(["agent"], 60),
        subprocess.TimeoutExpired(["codex"], 60),
    )
    verdict = _gate(runner).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "timeout" in verdict.reason
    assert verdict.backend_calls == 2
    assert not verdict.retry


def test_missing_binary_is_a_veto_and_starts_nothing():
    runner = FakeRunner()
    verdict = _gate(runner, found=()).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "cursor: binary not found" in verdict.reason
    assert "codex: binary not found" in verdict.reason
    assert runner.calls == []
    assert verdict.backend_calls == 0


@pytest.mark.parametrize(
    "result",
    [
        _done(stderr="Error: You've hit your usage limit. Try again later.", code=1),
        _done(stdout="Error: rate limit exceeded (429 Too Many Requests)"),
        _done(stdout=APPROVE_TEXT, stderr="stream error: 429 Too Many Requests; retrying"),
        _done(stderr="insufficient_quota", code=2),
    ],
)
def test_quota_error_text_is_a_veto(result):
    verdict = _gate(FakeRunner(result), found=("cursor",)).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "quota" in verdict.reason


def test_bare_try_again_notice_is_a_veto():
    # ChatGPT-style quota notices carry no quota keyword; they must still veto.
    result = _done(stdout=APPROVE_TEXT, stderr="try again at 1:07 PM")
    verdict = _gate(FakeRunner(result), found=("cursor",)).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "quota" in verdict.reason


def test_nonzero_exit_and_spawn_errors_are_vetoes():
    runner = FakeRunner(_done(stdout=APPROVE_TEXT, stderr="boom", code=3), OSError("exec format error"))
    verdict = _gate(runner).review(ENTER, _context())
    assert verdict.decision == VETO
    assert "cursor: exit 3" in verdict.reason
    assert "codex: subprocess error OSError" in verdict.reason


def test_codex_is_the_fallback_when_cursor_fails():
    runner = FakeRunner(_done(stderr="quota exceeded", code=1), _done(APPROVE_TEXT))
    verdict = _gate(runner).review(ENTER, _context())
    assert verdict.approved
    assert verdict.backend == "codex"
    assert verdict.backend_calls == 2
    assert verdict.failures == ("cursor: quota or rate limit",)


def test_backend_order_follows_config():
    runner = FakeRunner(_done(VETO_TEXT))
    verdict = _gate(runner, backends=["codex", "cursor"]).review(ENTER, _context())
    assert verdict.backend == "codex"
    assert runner.calls[0]["argv"][0] == "/fake/codex"


def test_daily_cap_is_enforced():
    runner = FakeRunner(_done(APPROVE_TEXT))
    gate = _gate(runner, max_calls_per_day=10)
    capped = gate.review(ENTER, _context(), calls_used=10)
    assert capped.decision == VETO
    assert "daily cap of 10" in capped.reason
    assert runner.calls == []
    assert gate.review(ENTER, _context(), calls_used=9).approved


def test_cap_stops_the_fallback_too():
    runner = FakeRunner(_done("garbage"))
    verdict = _gate(runner, max_calls_per_day=1).review(ENTER, _context())
    assert verdict.decision == VETO
    assert len(runner.calls) == 1
    assert "daily cap" in verdict.reason


def test_exhausted_cycle_budget_is_a_retryable_veto():
    runner = FakeRunner()
    gate = _gate(runner)
    verdict = gate.review(ENTER, _context(), deadline=gate._clock() - 1)
    assert verdict.decision == VETO
    assert verdict.retry
    assert runner.calls == []


def test_timeout_passed_to_the_runner_is_the_remaining_budget():
    runner = FakeRunner(_done(APPROVE_TEXT))
    _gate(runner, timeout_s=30).review(ENTER, _context())
    assert 0 < runner.calls[0]["timeout"] <= 30


def test_context_that_does_not_match_the_intent_is_a_veto():
    runner = FakeRunner(_done(APPROVE_TEXT))
    verdict = _gate(runner).review(ENTER, _context(symbol="ETH-USD"))
    assert verdict.decision == VETO
    assert runner.calls == []


def test_cursor_and_codex_invocations():
    argv, stdin = build_command("cursor", "/x/agent", "PROMPT", None)
    assert argv == ["/x/agent", "-p", "--trust", "--output-format", "text", "PROMPT"]
    assert stdin is None
    argv, stdin = build_command("codex", "/x/codex", "PROMPT", "some-model")
    assert argv[:2] == ["/x/codex", "exec"]
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--model") + 1] == "some-model"
    assert argv[-1] == "-"
    assert stdin == "PROMPT"


def test_binaries_resolve_from_candidate_lists(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setitem(ai_gate.CANDIDATE_PATHS, "codex", (str(tmp_path / "missing"), str(fake)))
    assert ai_gate.resolve_binary("codex") == str(fake)
    monkeypatch.setitem(ai_gate.CANDIDATE_PATHS, "cursor", (str(tmp_path / "nope"),))
    assert ai_gate.resolve_binary("cursor") is None


def test_child_env_drops_keys_and_bot_settings(monkeypatch):
    monkeypatch.setenv("RH_API_KEY", "secret")
    monkeypatch.setenv("RH_PRIVATE_KEY_BASE64", "secret")
    monkeypatch.setenv("RHBOT_HUMAN_RESUME_FILE", "/tmp/code")
    monkeypatch.setenv("HOME", "/home/test")
    env = child_env()
    assert "RH_API_KEY" not in env
    assert "RH_PRIVATE_KEY_BASE64" not in env
    assert "RHBOT_HUMAN_RESUME_FILE" not in env
    assert env["HOME"] == "/home/test"
    runner = FakeRunner(_done(APPROVE_TEXT))
    _gate(runner).review(ENTER, _context())
    assert "RH_API_KEY" not in runner.calls[0]["env"]


def test_default_runner_kills_the_process_group_on_timeout(monkeypatch):
    killed = []

    class SlowProc:
        pid = 4242
        returncode = None

        def communicate(self, stdin=None, timeout=None):
            raise subprocess.TimeoutExpired(["agent"], timeout)

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(ai_gate.subprocess, "Popen", lambda *a, **k: SlowProc())
    monkeypatch.setattr(ai_gate.os, "killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(["agent"], stdin=None, timeout=1, env={}, cwd=".")
    assert killed == [4242]


def test_module_review_uses_the_default_chain_and_fails_closed(monkeypatch):
    monkeypatch.setattr(ai_gate, "resolve_binary", lambda backend: None)
    verdict = review(ENTER, _context())
    assert verdict.decision == VETO
    assert "binary not found" in verdict.reason


def test_prompt_carries_the_required_context(now):
    bars = make_bars("BTC-USD", padded_closes("12"), now - timedelta(days=1))
    context = build_context(
        ENTER,
        bars=bars,
        now=now,
        position_qty=Decimal(0),
        entry_price=None,
        book_drawdown=Decimal("-0.031"),
        overlay_state="ARMED",
    )
    # SMA = (199 * 10 + 12) / 200 = 10.01; 12 / 10.01 - 1 = 19.88%.
    assert context.sma_distance_pct == Decimal("19.88")
    assert context.last_returns_pct[-1] == Decimal("20.00")
    assert len(context.last_returns_pct) == 5
    prompt = build_prompt(context)
    for needle in (
        "UTC date: 2026-03-16",
        "Symbol: BTC-USD",
        "Proposed action: ENTER",
        "Last closed daily close: 12",
        "Distance from 200-day SMA: +19.88%",
        "Last 5 daily returns",
        "+20.00%",
        "Current position: flat",
        "Book drawdown from peak: -3.10%",
        "Book overlay state: ARMED",
        "advisor, not a trader",
        "one-bar spike",
        "When in doubt, veto",
        "DECISION: <APPROVE|VETO>",
        "CONFIDENCE:",
        "REASON:",
    ):
        assert needle in prompt, needle


def test_exit_context_names_the_entry_price(now):
    exit_intent = OrderIntent("ETH-USD", "sell", "trend_exit", base_quantity=Decimal("1"))
    bars = make_bars("ETH-USD", padded_closes("8"), now - timedelta(days=1))
    context = build_context(
        exit_intent,
        bars=bars,
        now=now,
        position_qty=Decimal("1"),
        entry_price=Decimal("12.5"),
        book_drawdown=Decimal("0"),
        overlay_state="FROZEN",
    )
    prompt = build_prompt(context)
    assert "Proposed action: EXIT" in prompt
    assert "Current position: long, entry price 12.5" in prompt
    assert "Book overlay state: FROZEN" in prompt


# --- config -----------------------------------------------------------------


def test_config_defaults_and_bounds(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.ai_gate.enabled is True
    assert settings.ai_gate.backends == ("cursor", "codex")
    assert settings.ai_gate.timeout_s == 60
    assert settings.ai_gate.max_calls_per_day == 10
    for bad in (
        {"backends": []},
        {"backends": ["gpt"]},
        {"backends": ["cursor", "cursor"]},
        {"timeout_s": 600},
        {"max_calls_per_day": -1},
        {"api_key": "x"},
    ):
        with pytest.raises(ValueError):
            Settings(state_dir=tmp_path, ai_gate=bad)


def test_example_config_documents_the_gate(tmp_path):
    from pathlib import Path

    from rhbot.config import load_settings

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    settings = load_settings(str(example), str(tmp_path))
    assert settings.ai_gate.enabled is True
    assert settings.ai_gate.max_calls_per_day == 10


# --- engine seam ------------------------------------------------------------


def test_vetoed_intent_produces_no_order_or_fill(tmp_path, now):
    gate = FakeGate(VETO)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.run_once(now=now, snapshot=_hot(now))
    assert [intent.symbol for intent, _ in gate.asked] == ["BTC-USD", "ETH-USD"]
    assert _fills(bot, "trend_daily") == []
    assert bot.ledger.positions("trend_daily") == {}
    assert bot.ledger.cash("trend_daily") == Decimal("1000")
    assert bot.ledger.open_orders() == []
    verdicts = _events(bot, "ai_gate")
    assert [(item["symbol"], item["decision"]) for item in verdicts] == [
        ("BTC-USD", VETO),
        ("ETH-USD", VETO),
    ]
    for item in verdicts:
        assert item["sleeve"] == "trend_daily"
        assert item["action"] == "ENTER"
        assert item["backend"] == "cursor"
        assert item["reason"] == "fake veto"
        assert "confidence" in item and "latency_ms" in item
    # The shadow book reuses the live book's verdicts instead of calling the
    # AI again: a live veto keeps the shadow flat too.
    assert bot.ledger.shadow_positions("trend_daily_shadow") == {}
    # A veto settles today's decision: a later cycle the same day does not ask again.
    bot.run_once(now=now + timedelta(minutes=5), snapshot=_hot(now + timedelta(minutes=5)))
    assert len(gate.asked) == 2
    assert _fills(bot, "trend_daily") == []
    ok, detail = bot.ledger.verify_chain()
    assert ok, detail
    # Tomorrow the rule proposes again and the gate is asked again.
    later = now + timedelta(days=1)
    bot.run_once(now=later, snapshot=_hot(later))
    assert len(gate.asked) == 4
    bot.ledger.close()


def test_approved_intent_fills_exactly_as_proposed(tmp_path, now):
    ungated = Engine(make_settings(tmp_path / "plain"))
    ungated.run_once(now=now, snapshot=_hot(now))
    gated = Engine(make_settings(tmp_path / "gated"), ai_gate=FakeGate(APPROVE))
    gated.run_once(now=now, snapshot=_hot(now))

    def trend(bot):
        return [(row["symbol"], row["side"], row["notional"], row["qty"]) for row in _fills(bot, "trend_daily")]

    assert trend(gated) == trend(ungated)
    assert len(trend(gated)) == 2
    assert [item["decision"] for item in _events(gated, "ai_gate")] == [APPROVE, APPROVE]
    ungated.ledger.close()
    gated.ledger.close()


def test_shadow_reuses_live_verdicts_without_new_calls(tmp_path, now):
    # The shadow never launches a backend: it reuses the live book's logged
    # verdicts, so the AI is asked exactly once per entry while the shadow
    # still mirrors the gated decision.
    gate = FakeGate(APPROVE)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.run_once(now=now, snapshot=_hot(now))
    assert len(gate.asked) == 2
    assert {intent.symbol for intent, _ in gate.asked} == {"BTC-USD", "ETH-USD"}
    assert set(bot.ledger.shadow_positions("trend_daily_shadow")) == {"BTC-USD", "ETH-USD"}
    # A vetoed live entry keeps the shadow flat too, with no extra AI call.
    gate2 = FakeGate(VETO)
    bot2 = Engine(make_settings(tmp_path / "veto"), ai_gate=gate2)
    bot2.run_once(now=now, snapshot=_hot(now))
    assert len(gate2.asked) == 2
    assert bot2.ledger.shadow_positions("trend_daily_shadow") == {}
    bot.ledger.close()
    bot2.ledger.close()


def test_buy_and_hold_and_dca_are_never_gated(tmp_path, now):
    gate = FakeGate(VETO)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.run_once(now=now, snapshot=_hot(now))
    assert {intent.reason for intent, _ in gate.asked} == {"trend_entry"}
    assert bot.ledger.positions("buy_and_hold")
    assert [row["reason"] for row in _fills(bot, "dca_weekly")] == ["dca_buy"]
    assert {item["sleeve"] for item in _events(bot, "ai_gate")} == {"trend_daily"}
    bot.ledger.close()


def test_no_trend_intent_means_no_gate_call(tmp_path, now):
    gate = FakeGate(VETO)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.run_once(now=now, snapshot=snapshot(now))
    assert gate.asked == []
    assert bot.ledger.positions("buy_and_hold")
    assert _fills(bot, "dca_weekly")
    assert _events(bot, "ai_gate") == []
    bot.ledger.close()


def test_exits_bypass_the_gate(tmp_path, now):
    # Entries are gated, but exits are risk-reducing and must never be
    # blocked: even a vetoing gate is not consulted, and the exit fills.
    bot = Engine(make_settings(tmp_path))
    bot.run_once(now=now, snapshot=_hot(now))
    held = bot.ledger.positions("trend_daily")
    assert set(held) == {"BTC-USD", "ETH-USD"}
    gate = FakeGate(VETO)
    bot.ai_gate = gate
    exit_day = now + timedelta(days=8)
    cold = snapshot(exit_day, mid="8", closes=padded_closes("12", count=200) + ["8"] * 8)
    bot.run_once(now=exit_day, snapshot=cold)
    assert gate.asked == []
    assert [item["decision"] for item in _events(bot, "ai_gate")] == []
    assert bot.ledger.positions("trend_daily") == {}
    assert set(bot.ledger.shadow_positions("trend_daily_shadow")) == set()
    bot.ledger.close()


def test_daily_cap_counts_calls_from_the_audit_log(tmp_path, now):
    settings = make_settings(tmp_path, ai_gate={"max_calls_per_day": 1})
    runner = FakeRunner(_done(APPROVE_TEXT))
    gate = AIGate(settings.ai_gate, runner=runner, resolver=lambda backend: f"/fake/{backend}")
    bot = Engine(settings, ai_gate=gate)
    bot.run_once(now=now, snapshot=_hot(now))
    assert len(runner.calls) == 1
    verdicts = _events(bot, "ai_gate")
    assert [(item["symbol"], item["decision"]) for item in verdicts] == [
        ("BTC-USD", APPROVE),
        ("ETH-USD", VETO),
    ]
    assert "daily cap of 1" in verdicts[1]["reason"]
    assert [row["symbol"] for row in _fills(bot, "trend_daily")] == ["BTC-USD"]
    bot.ledger.close()
    # A restart does not reset the cap: it is read back from the ledger.
    again = Engine(settings, ai_gate=gate)
    assert again._ai_gate_events(now.date().isoformat())[0]["backend_calls"] == 1
    again.ledger.close()


def test_approval_is_reused_after_a_transient_denial(tmp_path, now):
    gate = FakeGate(APPROVE)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    stale = _hot(now)
    stale.quotes["BTC-USD"] = replace(stale.quotes["BTC-USD"], ts=now - timedelta(seconds=45))
    bot.run_once(now=now, snapshot=stale)
    assert "BTC-USD" not in bot.ledger.positions("trend_daily")
    asked = len(gate.asked)
    later = now + timedelta(minutes=1)
    bot.run_once(now=later, snapshot=_hot(later))
    assert len(gate.asked) == asked
    assert "BTC-USD" in bot.ledger.positions("trend_daily")
    cached = [item for item in _events(bot, "ai_gate") if item["backend"].startswith("cached:")]
    assert cached and cached[0]["symbol"] == "BTC-USD"
    bot.ledger.close()


def test_retryable_veto_lets_a_later_cycle_ask_again(tmp_path, now):
    gate = FakeGate(VETO, retry=True)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.run_once(now=now, snapshot=_hot(now))
    assert _fills(bot, "trend_daily") == []
    assert bot.ledger.strategy_state("trend_daily")["evaluated_on"] == {}
    gate.decision, gate.retry = APPROVE, False
    later = now + timedelta(minutes=1)
    bot.run_once(now=later, snapshot=_hot(later))
    assert len(_fills(bot, "trend_daily")) == 2
    bot.ledger.close()


def test_gate_error_fails_closed(tmp_path, now):
    class Broken:
        def review(self, *args, **kwargs):
            raise RuntimeError("boom")

    bot = Engine(make_settings(tmp_path), ai_gate=Broken())
    bot.run_once(now=now, snapshot=_hot(now))
    assert _fills(bot, "trend_daily") == []
    assert {item["decision"] for item in _events(bot, "ai_gate")} == {VETO}
    bot.ledger.close()


def test_kill_file_and_config_flag_turn_the_gate_off(tmp_path, now):
    gate = FakeGate(VETO)
    bot = Engine(make_settings(tmp_path / "file"), ai_gate=gate)
    (bot.settings.state_dir / KILL_FILE).write_text("", encoding="utf-8")
    bot.run_once(now=now, snapshot=_hot(now))
    assert gate.asked == []
    assert len(_fills(bot, "trend_daily")) == 2
    bot.ledger.close()

    off = Engine(make_settings(tmp_path / "flag", ai_gate={"enabled": False}), ai_gate=gate)
    off.run_once(now=now, snapshot=_hot(now))
    assert gate.asked == []
    assert len(_fills(off, "trend_daily")) == 2
    off.ledger.close()


def test_only_the_live_loop_attaches_a_gate(tmp_path, monkeypatch):
    import rhbot.engine as engine_module

    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.setattr(engine_module.signal, "signal", lambda *args: None)
    bot = Engine(make_settings(tmp_path / "on"))
    assert bot.ai_gate is None
    monkeypatch.setattr(bot, "run_once", lambda: {})
    bot.serve(once=True)
    assert isinstance(bot.ai_gate, AIGate)
    bot.ledger.close()

    off = Engine(make_settings(tmp_path / "off", ai_gate={"enabled": False}))
    monkeypatch.setattr(off, "run_once", lambda: {})
    off.serve(once=True)
    assert off.ai_gate is None
    off.ledger.close()


def test_gate_is_not_invoked_in_backtest_or_replay(tmp_path, now, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the gate must not run during replay")

    monkeypatch.setattr(AIGate, "review", refuse)
    monkeypatch.setattr(AIGate, "__init__", refuse)
    closes = padded_closes("12")
    bars = {
        symbol: make_bars(symbol, closes, now - timedelta(days=1))
        for symbol in ("BTC-USD", "ETH-USD")
    }
    replayed = replay(make_settings(tmp_path / "replay"), bars)
    try:
        assert replayed.ai_gate is None
        assert _fills(replayed, "trend_daily")
        assert _events(replayed, "ai_gate") == []
    finally:
        replayed.ledger.close()


def test_replay_mode_ignores_an_attached_gate(tmp_path, now):
    gate = FakeGate(VETO)
    bot = Engine(make_settings(tmp_path), ai_gate=gate)
    bot.ledger.set_meta("mode", "replay")
    bot.run_once(now=now, snapshot=_hot(now))
    assert gate.asked == []
    assert len(_fills(bot, "trend_daily")) == 2
    bot.ledger.close()
