"""Advisory veto on trend_daily intents. It never creates, sizes, or changes an order.

The engine asks this module about a trend entry in the live loop only. Exits bypass the gate.
A model may answer APPROVE or VETO. Anything else, including a timeout, a
missing binary, a quota error, or the daily call cap, is a VETO. This module
never raises into the engine. Risk checks still run after an approval.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from rhbot.config import FROZEN_SMA_WINDOW, AIGateSettings
from rhbot.models import Bar, OrderIntent
from rhbot.strategies.trend import closed_bars

APPROVE = "APPROVE"
VETO = "VETO"

# Present in the state dir: the gate is off and trend runs as it did without it.
KILL_FILE = "AI_GATE_OFF"

# Paths move across VM rebuilds. The first executable hit wins, then PATH.
CANDIDATE_PATHS: dict[str, tuple[str, ...]] = {
    "cursor": (
        "~/.local/bin/agent",
        "~/.local/bin/cursor-agent",
        "/usr/local/bin/agent",
        "/usr/local/bin/cursor-agent",
        "/usr/bin/agent",
        "/usr/bin/cursor-agent",
    ),
    "codex": (
        "/usr/bin/codex",
        "/usr/local/bin/codex",
        "~/.local/bin/codex",
        "/opt/hatch-image/bin/codex",
    ),
}
PATH_NAMES: dict[str, tuple[str, ...]] = {
    "cursor": ("agent", "cursor-agent"),
    "codex": ("codex",),
}

# The child never sees the quote key, the human resume secret, or bot settings.
_SCRUBBED_ENV_PREFIXES = ("RH_", "RHBOT_")
# A backend is not started with less time than this left in the cycle budget.
MIN_ATTEMPT_S = 5.0

_QUOTA = re.compile(
    r"rate[ _-]?limit|quota|usage limit|too many requests|\b429\b|limit exceeded|out of credits|try again",
    re.I,
)
_FIELD = re.compile(
    r"^[ \t>*_`#-]*(DECISION|CONFIDENCE|REASON)[ \t*_`]*[:=](.*)$",
    re.I | re.M,
)
_NUMBER = re.compile(r"[-+]?\d*\.?\d+")


@dataclass(frozen=True)
class GateContext:
    utc_date: str
    symbol: str
    action: str
    last_close: Decimal | None
    sma_distance_pct: Decimal | None
    last_returns_pct: tuple[Decimal, ...]
    position: str
    entry_price: Decimal | None
    book_drawdown_pct: Decimal
    overlay_state: str


@dataclass(frozen=True)
class Verdict:
    decision: str
    confidence: float
    reason: str
    backend: str
    latency_ms: int
    backend_calls: int = 0
    # True only when no backend was asked because the cycle budget ran out.
    # The engine lets a later cycle the same day ask again.
    retry: bool = False
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return self.decision == APPROVE

    def event_payload(self) -> dict:
        return {
            "decision": self.decision,
            "confidence": f"{self.confidence:.2f}",
            "reason": self.reason,
            "backend": self.backend,
            "latency_ms": self.latency_ms,
            "backend_calls": self.backend_calls,
            "retry": self.retry,
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class Parsed:
    decision: str
    confidence: float
    reason: str


def action_for(intent: OrderIntent) -> str:
    # Defensive-only: the engine gates entries, so this always returns "ENTER"
    # in practice. The EXIT branch of build_prompt is kept for completeness.
    return "ENTER" if intent.side == "buy" else "EXIT"


def _pct(value: Decimal) -> Decimal:
    return (value * Decimal(100)).quantize(Decimal("0.01"))


def build_context(
    intent: OrderIntent,
    *,
    bars: list[Bar],
    now: datetime,
    position_qty: Decimal,
    entry_price: Decimal | None,
    book_drawdown: Decimal,
    overlay_state: str,
) -> GateContext:
    """Facts for the prompt, from closed daily bars only."""
    closed = closed_bars(bars, now)
    last_close = closed[-1].close if closed else None
    distance = None
    if len(closed) >= FROZEN_SMA_WINDOW:
        window = closed[-FROZEN_SMA_WINDOW:]
        sma = sum((bar.close for bar in window), Decimal(0)) / Decimal(len(window))
        if sma > 0:
            distance = _pct(window[-1].close / sma - Decimal(1))
    returns: list[Decimal] = []
    recent = closed[-6:]
    for before, after in zip(recent, recent[1:]):
        if before.close > 0:
            returns.append(_pct(after.close / before.close - Decimal(1)))
    long = position_qty > 0
    return GateContext(
        utc_date=now.astimezone(timezone.utc).date().isoformat(),
        symbol=intent.symbol,
        action=action_for(intent),
        last_close=last_close,
        sma_distance_pct=distance,
        last_returns_pct=tuple(returns),
        position="long" if long else "flat",
        entry_price=entry_price if long else None,
        book_drawdown_pct=_pct(book_drawdown),
        overlay_state=overlay_state,
    )


def _signed(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:+.2f}%"


def build_prompt(context: GateContext) -> str:
    if context.action == "ENTER":
        action = "ENTER (buy with this coin's cash; the sleeve is flat in it)"
    else:
        action = "EXIT (sell the whole position and go to cash)"
    if context.position == "long":
        entry = "unknown" if context.entry_price is None else format(context.entry_price, "f")
        position = f"long, entry price {entry}"
    else:
        position = "flat"
    returns = ", ".join(_signed(item) for item in context.last_returns_pct) or "unknown"
    close = "unknown" if context.last_close is None else format(context.last_close, "f")
    return "\n".join(
        [
            "You review one proposed paper trade for a BTC/ETH daily trend-following sleeve.",
            "You are an advisor, not a trader. You cannot place, size, or change orders.",
            "You may only approve the proposal as it is, or veto it. A veto means the",
            "sleeve keeps its current position today and the rule asks again tomorrow.",
            "",
            "Rules:",
            "- Momentum entries need sustained upward pressure over several days, not a one-bar spike.",
            "- When in doubt, veto.",
            "- Use only the facts below. Do not run tools, read files, or browse.",
            "",
            "Facts:",
            f"UTC date: {context.utc_date}",
            f"Symbol: {context.symbol}",
            f"Proposed action: {action}",
            f"Last closed daily close: {close}",
            f"Distance from 200-day SMA: {_signed(context.sma_distance_pct)}",
            f"Last 5 daily returns, oldest to newest: {returns}",
            f"Current position: {position}",
            f"Book drawdown from peak: {_signed(context.book_drawdown_pct)}",
            f"Book overlay state: {context.overlay_state}",
            "",
            "Reply with exactly these three lines and nothing else:",
            "DECISION: <APPROVE|VETO>",
            "CONFIDENCE: <number from 0.0 to 1.0>",
            "REASON: <one sentence>",
        ]
    )


def _clean(value: str) -> str:
    return value.strip().strip("*_`\"'<> ").strip()


def parse_output(text: str) -> Parsed | None:
    """Last DECISION, CONFIDENCE, and REASON lines win. Missing or ambiguous is None."""
    found: dict[str, str] = {}
    for match in _FIELD.finditer(text or ""):
        found[match.group(1).upper()] = match.group(2)
    if set(found) != {"DECISION", "CONFIDENCE", "REASON"}:
        return None
    raw_decision = _clean(found["DECISION"]).upper()
    tokens = set(re.findall(r"APPROVE|VETO", raw_decision))
    if len(tokens) != 1 or not raw_decision.startswith(next(iter(tokens))):
        return None
    number = _NUMBER.search(found["CONFIDENCE"])
    if number is None:
        return None
    confidence = float(number.group(0))
    if "%" in found["CONFIDENCE"] or 1 < confidence <= 100:
        confidence /= 100
    if not 0 <= confidence <= 1:
        return None
    reason = _clean(found["REASON"])
    if not reason:
        return None
    sentence = re.split(r"(?<=[.!?])\s", reason, maxsplit=1)[0]
    return Parsed(next(iter(tokens)), confidence, sentence[:300])


def _field_lines_removed(text: str) -> str:
    return _FIELD.sub("", text or "")


def classify(result: subprocess.CompletedProcess) -> tuple[str | None, Parsed | None]:
    """Return (problem, parsed). A quota message anywhere outside the answer is a problem."""
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if _QUOTA.search(stderr) or _QUOTA.search(_field_lines_removed(stdout)):
        return "quota or rate limit", None
    if result.returncode != 0:
        tail = " ".join(stderr.split())[-160:]
        return f"exit {result.returncode}" + (f": {tail}" if tail else ""), None
    parsed = parse_output(stdout)
    if parsed is None:
        return "unparseable output", None
    return None, parsed


def resolve_binary(backend: str) -> str | None:
    for raw in CANDIDATE_PATHS.get(backend, ()):
        path = Path(raw).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for name in PATH_NAMES.get(backend, ()):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_command(
    backend: str, binary: str, prompt: str, model: str | None
) -> tuple[list[str], str | None]:
    """argv and stdin for one backend."""
    if backend == "cursor":
        argv = [binary, "-p", "--trust", "--output-format", "text"]
        if model:
            argv += ["--model", model]
        return argv + [prompt], None
    if backend == "codex":
        # This codex build reads the prompt from stdin and refuses an
        # untrusted directory without --skip-git-repo-check.
        argv = [binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
        if model:
            argv += ["--model", model]
        return argv + ["-"], prompt
    raise ValueError(f"unknown backend {backend}")


def child_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_SCRUBBED_ENV_PREFIXES)
    }


def run_command(
    argv: list[str],
    *,
    stdin: str | None,
    timeout: float,
    env: dict[str, str],
    cwd: str,
) -> subprocess.CompletedProcess:
    """Run in its own process group so a timeout kills the whole tree."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


Runner = Callable[..., subprocess.CompletedProcess]


class AIGate:
    def __init__(
        self,
        config: AIGateSettings | None = None,
        *,
        runner: Runner | None = None,
        resolver: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config or AIGateSettings()
        self._runner = runner or run_command
        self._resolve = resolver or resolve_binary
        self._clock = clock

    def review(
        self,
        intent: OrderIntent,
        context: GateContext,
        *,
        calls_used: int = 0,
        deadline: float | None = None,
    ) -> Verdict:
        """APPROVE or VETO. Never raises."""
        started = self._clock()
        try:
            if context.symbol != intent.symbol or context.action != action_for(intent):
                return self._veto(started, "fail closed: context does not match the intent", 0, (), "none")
            return self._review(context, calls_used, deadline, started)
        except Exception as exc:
            return self._veto(started, f"fail closed: gate error {type(exc).__name__}", 0, (), "none")

    def _review(
        self,
        context: GateContext,
        calls_used: int,
        deadline: float | None,
        started: float,
    ) -> Verdict:
        if deadline is None:
            deadline = started + self.config.timeout_s
        prompt = build_prompt(context)
        failures: list[str] = []
        calls = 0
        backend_used = "none"
        out_of_time = False
        for backend in self.config.backends:
            if calls_used + calls >= self.config.max_calls_per_day:
                failures.append(f"daily cap of {self.config.max_calls_per_day} calls reached")
                break
            remaining = deadline - self._clock()
            if remaining < MIN_ATTEMPT_S:
                failures.append(f"{backend}: cycle time budget used up")
                out_of_time = True
                break
            binary = self._resolve(backend)
            if binary is None:
                failures.append(f"{backend}: binary not found")
                continue
            model = self.config.cursor_model if backend == "cursor" else self.config.codex_model
            argv, stdin = build_command(backend, binary, prompt, model)
            calls += 1
            backend_used = backend
            try:
                with tempfile.TemporaryDirectory(prefix="rhbot-ai-gate-") as scratch:
                    result = self._runner(
                        argv, stdin=stdin, timeout=remaining, env=child_env(), cwd=scratch
                    )
            except subprocess.TimeoutExpired:
                failures.append(f"{backend}: timeout after {remaining:.0f} s")
                continue
            except Exception as exc:
                failures.append(f"{backend}: subprocess error {type(exc).__name__}")
                continue
            problem, parsed = classify(result)
            if problem is not None or parsed is None:
                failures.append(f"{backend}: {problem}")
                continue
            return Verdict(
                decision=parsed.decision,
                confidence=parsed.confidence,
                reason=parsed.reason,
                backend=backend,
                latency_ms=self._elapsed_ms(started),
                backend_calls=calls,
                failures=tuple(failures),
            )
        detail = "; ".join(failures) or "no backend configured"
        verdict = self._veto(started, f"fail closed: {detail}", calls, tuple(failures), backend_used)
        if out_of_time and calls == 0:
            return replace(verdict, retry=True)
        return verdict

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))

    def _veto(
        self,
        started: float,
        reason: str,
        calls: int,
        failures: tuple[str, ...],
        backend: str,
    ) -> Verdict:
        return Verdict(
            decision=VETO,
            confidence=0.0,
            reason=reason[:400],
            backend=backend,
            latency_ms=self._elapsed_ms(started),
            backend_calls=calls,
            failures=failures,
        )


def review(
    intent: OrderIntent,
    context: GateContext,
    *,
    config: AIGateSettings | None = None,
    calls_used: int = 0,
    deadline: float | None = None,
    runner: Runner | None = None,
) -> Verdict:
    """One-shot review with the default backend chain."""
    return AIGate(config, runner=runner).review(
        intent, context, calls_used=calls_used, deadline=deadline
    )


def gate_disabled(settings_enabled: bool, state_dir: Path) -> bool:
    return not settings_enabled or (Path(state_dir) / KILL_FILE).exists()
