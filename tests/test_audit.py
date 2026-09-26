import sqlite3

import pytest

from tests.conftest import engine, snapshot


def test_hash_chain_verifies_and_detects_tampering(tmp_path, now):
    bot = engine(tmp_path)
    bot.run_once(now=now, snapshot=snapshot(now))
    ok, detail = bot.ledger.verify_chain()
    assert ok, detail
    assert bot.ledger.event_count() > 0

    raw = sqlite3.connect(bot.ledger.path)
    with pytest.raises(sqlite3.Error, match="append-only"):
        raw.execute("UPDATE events SET payload = '{\"tampered\":true}' WHERE seq = 1")
    raw.close()
    ok_after, _ = bot.ledger.verify_chain()
    assert ok_after

    head = bot.ledger.head_hash()
    bot.ledger.conn.execute(
        "INSERT INTO events(ts, kind, payload, prev_hash, hash) VALUES(?, ?, ?, ?, ?)",
        (now.isoformat(), "tamper", "{}", head, "deadbeef"),
    )
    broken, message = bot.ledger.verify_chain()
    assert not broken
    assert "hash mismatch" in message

    with pytest.raises(sqlite3.Error, match="append-only"):
        bot.ledger.conn.execute("UPDATE events SET kind = 'nope' WHERE seq = 1")
    with pytest.raises(sqlite3.Error, match="append-only"):
        bot.ledger.conn.execute("DELETE FROM fills WHERE id = 1")
    bot.ledger.close()


def test_large_audit_chain_and_per_book_counts(tmp_path, now):
    bot = engine(tmp_path)
    with bot.ledger.transaction():
        for index in range(600):
            bot.ledger.append_event(
                "decision",
                {"sleeve": "trend_daily" if index % 2 else "dca_weekly"},
                now,
            )

    assert bot.ledger.verify_chain() == (True, "600 events")
    assert bot.ledger.count_events("decision") == 600
    assert bot.ledger.count_events("decision", "trend_daily") == 300
    assert bot.ledger.count_events("decision", "dca_weekly") == 300
    assert bot.ledger.count_events("decision", "buy_and_hold") == 0
    bot.ledger.close()
