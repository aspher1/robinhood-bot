import sqlite3

import pytest

from tests.conftest import engine, snapshot


def test_hash_chain_verifies_and_detects_tampering(tmp_path, now):
    bot = engine(tmp_path, sma_window=3)
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
