import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from rhbot.config import Settings
from rhbot.exports import EQUITY_COLUMNS, FILL_COLUMNS, build_csv
from rhbot.ledger import Ledger

NOW = datetime(2026, 3, 16, 15, tzinfo=timezone.utc)


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def _record(ledger, table, ts, *, label, identifier):
    ledger.conn.execute(
        f"""INSERT INTO {table} (
            sleeve, symbol, side, qty, qty_delta, mid, fill_price,
            cash_delta, cost, notional, ts, client_order_id, reason
        ) VALUES (?, 'BTC-USD', 'buy', '0.00000001', '0.00000001',
            '123.00000001', '124.00000001', '-0.00000124', '0.00000001',
            '0.00000123', ?, ?, ?)""",
        (label, ts.isoformat(), identifier, '=SUM(1,2),"quoted"'),
    )


def test_missing_database_emits_headers_without_creating_state(tmp_path):
    settings = Settings(state_dir=tmp_path / "missing")
    text, count = build_csv(settings, "fills", now=NOW)
    assert text == ",".join(FILL_COLUMNS) + "\n"
    assert count == 0
    assert not settings.state_dir.exists()
    equity, count = build_csv(settings, "equity", now=NOW)
    assert equity == ",".join(EQUITY_COLUMNS) + "\n"
    assert count == 0


def test_fill_window_order_precision_escaping_shadow_and_readonly(tmp_path):
    settings = Settings(state_dir=tmp_path)
    ledger = Ledger(settings)
    start = NOW - timedelta(days=7)
    _record(ledger, "fills", start - timedelta(microseconds=1), label="outside", identifier="old")
    _record(ledger, "fills", NOW, label=" =2+2", identifier="@danger")
    _record(ledger, "fills", start, label="paper", identifier="paper-start")
    _record(ledger, "shadow_fills", start, label="shadow", identifier="shadow-start")
    _record(ledger, "shadow_fills", NOW + timedelta(microseconds=1), label="future", identifier="future")
    ledger.close()
    before = (tmp_path / "bot.sqlite").read_bytes()

    text, count = build_csv(settings, "fills", "7d", now=NOW)
    rows = _rows(text)
    assert count == 2
    assert [row["client_order_id"] for row in rows] == ["paper-start", "'@danger"]
    assert [row["book"] for row in rows] == ["paper", "paper"]
    assert rows[1]["sleeve"] == "' =2+2"
    assert rows[0]["qty"] == "0.00000001"
    assert rows[0]["mid"] == "123.00000001"
    assert rows[0]["reason"] == "'=SUM(1,2),\"quoted\""
    assert list(rows[0]) == list(FILL_COLUMNS)

    text, count = build_csv(settings, "fills", "7d", include_shadow=True, now=NOW)
    assert count == 3
    assert [(r["ts"], r["book"]) for r in _rows(text)] == [
        (start.isoformat(), "shadow"),
        (start.isoformat(), "paper"),
        (NOW.isoformat(), "paper"),
    ]
    assert (tmp_path / "bot.sqlite").read_bytes() == before


def test_equity_has_blank_shadow_drawdown_and_exact_values(tmp_path):
    settings = Settings(state_dir=tmp_path)
    ledger = Ledger(settings)
    ledger.conn.execute(
        "INSERT INTO equity_snapshots(ts, sleeve, equity, cash, drawdown) VALUES(?, ?, ?, ?, ?)",
        (NOW.isoformat(), "=paper", "100.00000001", "99.00000000", "0.01000000"),
    )
    ledger.conn.execute(
        "INSERT INTO shadow_equity_snapshots(ts, sleeve, equity, cash) VALUES(?, ?, ?, ?)",
        (NOW.isoformat(), "shadow", "101.00000002", "98.00000000"),
    )
    ledger.close()
    text, count = build_csv(settings, "equity", include_shadow=True, now=NOW)
    rows = _rows(text)
    assert count == 2
    assert list(rows[0]) == list(EQUITY_COLUMNS)
    assert rows[0]["sleeve"] == "'=paper"
    assert rows[0]["equity"] == "100.00000001"
    assert rows[0]["drawdown"] == "0.01000000"
    assert rows[1]["book"] == "shadow"
    assert rows[1]["drawdown"] == ""


@pytest.mark.parametrize("dataset,since,now", [
    ("events", "7d", NOW),
    ("fills", "forever", NOW),
    ("fills", "7d", NOW.replace(tzinfo=None)),
])
def test_invalid_requests_rejected_without_creating_state(tmp_path, dataset, since, now):
    settings = Settings(state_dir=tmp_path / "missing")
    with pytest.raises(ValueError):
        build_csv(settings, dataset, since, now=now)
    assert not settings.state_dir.exists()
