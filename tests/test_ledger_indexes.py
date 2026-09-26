from rhbot.ledger import Ledger
from tests.conftest import make_settings


def test_reopen_existing_ledger_adds_indexes_for_daily_fill_queries(tmp_path):
    settings = make_settings(tmp_path)
    ledger = Ledger(settings)
    ledger.conn.execute("DROP INDEX fills_sleeve_day_idx")
    ledger.conn.execute("DROP INDEX shadow_fills_sleeve_day_idx")
    ledger.close()

    ledger = Ledger(settings)
    for table, index in (
        ("fills", "fills_sleeve_day_idx"),
        ("shadow_fills", "shadow_fills_sleeve_day_idx"),
    ):
        plan = ledger.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT notional FROM {table} "
            "WHERE sleeve=? AND substr(ts, 1, 10)=?",
            ("trend_daily", "2026-03-16"),
        ).fetchall()
        assert any(index in str(row["detail"]) for row in plan)
    ledger.close()
