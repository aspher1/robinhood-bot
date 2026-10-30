import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from rhbot.cli import main
from rhbot.dashboard import STALE_REASONS, build_dashboard, format_ts, percent_text
from rhbot.status import assess, build_report

from tests.conftest import engine, make_settings, snapshot

RECORDED = "Recorded paper data only. No prices were fetched to build this page."
TRADES_HEADER = (
    "ts,sleeve,symbol,side,qty,fill_price_usd,cash_delta_usd,"
    "cost_usd,notional_usd,reason,client_order_id"
)
EQUITY_HEADER = "ts,sleeve,equity_usd,cash_usd,drawdown_fraction"
_KINDS = {
    "saved-results": "saved",
    "stale-health": "stale",
    "incomplete-data": "incomplete",
}
_HEADINGS = {
    "saved-results": "Saved results",
    "stale-health": "Stale health",
    "incomplete-data": "Incomplete data",
}


def test_format_ts_is_utc_clock():
    assert format_ts("2026-03-16T15:00:00+00:00") == "2026-03-16 15:00:00 UTC"


def test_empty_dashboard_is_incomplete_and_creates_nothing(tmp_path, capsys):
    html_path = tmp_path / "dashboard.html"
    code, payload = _json_main(
        ["dashboard", "--state-dir", str(tmp_path), "--out", str(html_path)],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    assert Path(payload["wrote"]).resolve() == html_path.resolve()
    assert payload["bytes"] == html_path.stat().st_size
    assert payload["bytes"] > 0
    flags = _assert_page(html_path.read_text(encoding="utf-8"))
    assert flags["incomplete"] == "yes"
    assert flags["saved"] == "no"
    assert flags["stale"] == "no"
    page_text = html_path.read_text(encoding="utf-8")
    assert "not_started" in page_text or "no saved" in page_text.lower()
    assert not (tmp_path / "bot.sqlite").exists()

    stdout_code = main(["dashboard", "--state-dir", str(tmp_path)])
    stdout = capsys.readouterr().out
    assert stdout_code == 0
    assert not stdout.lstrip().startswith("{")
    stdout_flags = _assert_page(stdout)
    assert stdout_flags["incomplete"] == "yes"
    assert stdout_flags["saved"] == "no"
    assert stdout_flags["stale"] == "no"
    assert not (tmp_path / "bot.sqlite").exists()

    settings = make_settings(tmp_path)
    page = build_dashboard(settings, "24h")
    _assert_model(page, settings)
    assert page["saved"]["present"] is False
    assert page["stale"]["present"] is False
    assert page["incomplete"]["present"] is True
    assert not (tmp_path / "bot.sqlite").exists()

    trades_path = tmp_path / "trades.csv"
    equity_path = tmp_path / "equity.csv"
    assert main(["export", "trades", "--state-dir", str(tmp_path), "--out", str(trades_path)]) == 0
    capsys.readouterr()
    assert main(["export", "equity", "--state-dir", str(tmp_path), "--out", str(equity_path)]) == 0
    capsys.readouterr()
    assert _nonempty_lines(trades_path) == [TRADES_HEADER]
    assert _nonempty_lines(equity_path) == [EQUITY_HEADER]
    assert not (tmp_path / "bot.sqlite").exists()
    assert not (tmp_path / "heartbeat.json").exists()
    assert {path.name for path in tmp_path.iterdir()} == {
        "dashboard.html",
        "trades.csv",
        "equity.csv",
    }


def test_dashboard_reads_records_without_network_or_ledger_writes(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    _cycle(state)
    db = state / "bot.sqlite"
    heartbeat = state / "heartbeat.json"
    assert heartbeat.is_file()
    events = _event_count(db)
    assert events > 0
    db_mtime, db_size = _mtime_size(db)
    heartbeat_mtime = heartbeat.stat().st_mtime_ns

    def explode(*_args, **_kwargs):
        raise AssertionError("network was used")

    monkeypatch.setattr("httpx.Client", explode)
    monkeypatch.setattr("httpx.get", explode)

    out = tmp_path / "out"
    out.mkdir()
    html_path = out / "dashboard.html"
    trades_path = out / "trades.csv"
    equity_path = out / "equity.csv"
    for path in (html_path, trades_path, equity_path):
        assert state.resolve() not in path.resolve().parents

    code, payload = _json_main(
        ["dashboard", "--state-dir", str(state), "--since", "24h", "--out", str(html_path)],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    assert Path(payload["wrote"]).resolve() == html_path.resolve()
    assert payload["bytes"] == html_path.stat().st_size
    assert main(["export", "trades", "--state-dir", str(state), "--out", str(trades_path)]) == 0
    capsys.readouterr()
    assert main(["export", "equity", "--state-dir", str(state), "--out", str(equity_path)]) == 0
    capsys.readouterr()

    assert _mtime_size(db) == (db_mtime, db_size)
    assert heartbeat.stat().st_mtime_ns == heartbeat_mtime
    assert _event_count(db) == events

    html = html_path.read_text(encoding="utf-8")
    flags = _assert_page(html)
    assert flags["saved"] == "yes"
    assert flags["stale"] == "no"
    assert flags["incomplete"] == "no"

    settings = make_settings(state)
    report = build_report(settings, "24h")
    expected = report["sleeves"]["buy_and_hold"]["since_start"]["return_pct"]
    page = build_dashboard(settings, "24h")
    _assert_model(page, settings)
    assert page["saved"]["present"] is True
    assert page["saved"]["benchmark"]["unit"] == "percent"
    assert page["saved"]["benchmark"]["return_pct"] == expected

    tag = _benchmark_return(html)
    assert _attr(tag, "data-unit") == "percent"
    assert _attr(tag, "data-value") == expected
    visible = _inner_text(tag)
    assert visible == percent_text(expected)
    assert "%" in visible
    assert "$" not in tag
    assert re.search(r'<p\b[^>]*\bid="benchmark-unit"[^>]*>\s*percent\s*</p>', html)

    trade_header, trade_rows = _csv_table(trades_path)
    assert trade_header == TRADES_HEADER
    assert len(trade_rows) >= 1
    assert len(trade_rows) == _scalar(db, "SELECT COUNT(*) FROM fills")
    assert all(row["side"] in {"buy", "sell"} for row in trade_rows)

    equity_header, equity_rows = _csv_table(equity_path)
    assert equity_header == EQUITY_HEADER
    assert any(row["sleeve"] == "buy_and_hold" for row in equity_rows)
    stored = _pairs(db, "SELECT sleeve, drawdown FROM equity_snapshots")
    exported = [(row["sleeve"], row["drawdown_fraction"]) for row in equity_rows]
    assert stored
    assert sorted(exported) == sorted(stored)
    for value in (row["drawdown_fraction"] for row in equity_rows):
        assert "%" not in value
        assert re.fullmatch(r"-?\d+(\.\d+)?", value)
    assert "%" not in equity_path.read_text(encoding="utf-8")


def test_stale_health_is_separate_from_saved_results(tmp_path, capsys):
    state = tmp_path / "state"
    _cycle(state)
    heartbeat = state / "heartbeat.json"
    body = json.loads(heartbeat.read_text(encoding="utf-8"))
    body["ts"] = "2020-01-01T00:00:00+00:00"
    heartbeat.write_text(json.dumps(body), encoding="utf-8")
    assert json.loads(heartbeat.read_text(encoding="utf-8"))["ts"] == "2020-01-01T00:00:00+00:00"

    html_path = tmp_path / "out" / "dashboard.html"
    html_path.parent.mkdir()
    code, payload = _json_main(
        ["dashboard", "--state-dir", str(state), "--since", "24h", "--out", str(html_path)],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    html = html_path.read_text(encoding="utf-8")
    flags = _assert_page(html)
    assert flags["saved"] == "yes"
    assert flags["stale"] == "yes"
    assert flags["incomplete"] == "no"
    stale = _section(html, "stale-health")
    assert "heartbeat_stale" in stale
    saved = _section(html, "saved-results")
    assert "buy_and_hold" in saved
    assert re.search(r"\$\s*\d", saved)

    settings = make_settings(state)
    page = build_dashboard(settings, "24h")
    _assert_model(page, settings)
    assert page["saved"]["present"] is True
    assert page["stale"]["present"] is True
    assert page["incomplete"]["present"] is False
    assert "heartbeat_stale" in page["stale"]["reasons"]
    equity = build_report(settings, "24h")["sleeves"]["buy_and_hold"]["equity"]
    assert equity in saved
    assert "$" in saved


def test_missing_snapshots_are_incomplete_while_trades_remain(tmp_path, capsys):
    state = tmp_path / "state"
    _cycle(state)
    db = state / "bot.sqlite"
    conn = sqlite3.connect(db)
    fills = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert fills >= 1
    conn.execute("DELETE FROM equity_snapshots")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == fills
    conn.close()

    html_path = tmp_path / "dashboard.html"
    code, payload = _json_main(
        ["dashboard", "--state-dir", str(state), "--since", "24h", "--out", str(html_path)],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    html = html_path.read_text(encoding="utf-8")
    flags = _assert_page(html)
    assert flags["saved"] == "yes"
    assert flags["incomplete"] == "yes"
    assert flags["stale"] == "no"
    assert "no_equity_snapshots" in _section(html, "incomplete-data")

    settings = make_settings(state)
    page = build_dashboard(settings, "24h")
    _assert_model(page, settings)
    assert page["saved"]["present"] is True
    assert page["incomplete"]["present"] is True
    assert page["recorded_only"] is True

    trades_path = tmp_path / "trades.csv"
    assert main(["export", "trades", "--state-dir", str(state), "--out", str(trades_path)]) == 0
    capsys.readouterr()
    header, rows = _csv_table(trades_path)
    assert header == TRADES_HEADER
    assert len(rows) == fills


def test_timeline_uses_formatted_timestamps(tmp_path, capsys, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    bot.ledger.close()
    raw = "2026-03-16T15:00:00+00:00"
    display = "2026-03-16 15:00:00 UTC"
    assert now.isoformat() == raw
    assert format_ts(raw) == display

    html_path = tmp_path / "dashboard.html"
    code, payload = _json_main(
        ["dashboard", "--state-dir", str(tmp_path), "--out", str(html_path)],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    html = html_path.read_text(encoding="utf-8")
    flags = _assert_page(html)
    assert flags["saved"] == "yes"
    _assert_display_time(html, display)

    stdout_code = main(["dashboard", "--state-dir", str(tmp_path)])
    stdout = capsys.readouterr().out
    assert stdout_code == 0
    assert not stdout.lstrip().startswith("{")
    assert RECORDED in stdout
    _assert_display_time(stdout, display)

    settings = make_settings(tmp_path)
    page = build_dashboard(settings, "24h")
    _assert_model(page, settings)
    assert page["saved"]["present"] is True


def test_bad_since_is_refused(tmp_path, capsys):
    code, body = _json_main(
        ["dashboard", "--since", "nope", "--state-dir", str(tmp_path)],
        capsys,
    )
    assert code == 2
    assert body["ok"] is False
    assert not (tmp_path / "bot.sqlite").exists()


def _cycle(state: Path) -> None:
    bot = engine(state)
    wall = datetime.now(timezone.utc)
    bot.run_once(now=wall, snapshot=snapshot(wall))
    bot.ledger.close()


def _json_main(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


def _section(html: str, section_id: str) -> str:
    match = re.search(
        rf'<section\b[^>]*\bid="{re.escape(section_id)}"[^>]*>.*?</section>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, section_id
    return match.group(0)


def present(html: str, section_id: str) -> str:
    block = _section(html, section_id)
    open_tag, body = block.split(">", 1)
    assert f'data-kind="{_KINDS[section_id]}"' in open_tag
    found = re.search(r'data-present="(yes|no)"', open_tag)
    assert found is not None, open_tag
    title = _HEADINGS[section_id]
    assert re.search(rf"<h[1-6]\b[^>]*>\s*{re.escape(title)}\s*</h[1-6]>", body)
    return found.group(1)


def _assert_page(html: str) -> dict[str, str]:
    assert RECORDED in html
    flags = {
        "saved": present(html, "saved-results"),
        "stale": present(html, "stale-health"),
        "incomplete": present(html, "incomplete-data"),
    }
    _section(html, "decision-timeline")
    return flags


def _assert_model(page: dict, settings, since: str = "24h", now=None) -> None:
    assert page["recorded_only"] is True
    assert page["saved"]["benchmark"]["unit"] == "percent"
    stale = sorted(set(assess(settings, now=now)["reasons"]) & set(STALE_REASONS))
    assert page["stale"]["reasons"] == stale
    assert page["stale"]["present"] is (len(page["stale"]["reasons"]) > 0)
    sleeve = build_report(settings, since, now=now)["sleeves"].get("buy_and_hold")
    if sleeve is not None:
        assert page["saved"]["benchmark"]["return_pct"] == sleeve["since_start"]["return_pct"]


def _assert_display_time(html: str, display: str) -> None:
    block = _section(html, "decision-timeline")
    assert display in block
    found = False
    for attrs, inner in re.findall(r"<time\b([^>]*)>(.*?)</time>", block, flags=re.DOTALL):
        if not re.search(r'\bdatetime="[^"]+"', attrs):
            continue
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if text == display:
            found = True
    assert found


def _benchmark_return(html: str) -> str:
    match = re.search(
        r'<p\b[^>]*\bid="benchmark-return"[^>]*>.*?</p>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag)
    assert match is not None, name
    return match.group(1)


def _inner_text(tag: str) -> str:
    return re.sub(r"<[^>]+>", "", tag).strip()


def _nonempty_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _csv_table(path: Path) -> tuple[str, list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames is not None
    return lines[0], rows


def _connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def _event_count(db: Path) -> int:
    conn = _connect_ro(db)
    try:
        return _scalar(db, "SELECT COUNT(*) FROM events", conn)
    finally:
        conn.close()


def _scalar(db: Path, sql: str, conn: sqlite3.Connection | None = None) -> int:
    owned = conn is None
    if conn is None:
        conn = _connect_ro(db)
    try:
        row = conn.execute(sql).fetchone()
    finally:
        if owned:
            conn.close()
    assert row is not None
    return int(row[0])


def _pairs(db: Path, sql: str) -> list[tuple[str, str]]:
    conn = _connect_ro(db)
    try:
        return [(str(row[0]), str(row[1])) for row in conn.execute(sql)]
    finally:
        conn.close()


def _mtime_size(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_size
