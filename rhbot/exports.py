"""Read-only, fixed-schema CSV exports of paper ledger history."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path

from rhbot.config import Settings
from rhbot.ledger import Ledger, parse_ts
from rhbot.ops import utcnow
from rhbot.status import parse_since

FILL_COLUMNS = (
    "id", "ts", "book", "sleeve", "symbol", "side", "qty", "mid",
    "fill_price", "notional", "cost", "cash_delta", "client_order_id", "reason",
)
EQUITY_COLUMNS = ("id", "ts", "book", "sleeve", "equity", "cash", "drawdown")
_TEXT_COLUMNS = frozenset(("sleeve", "symbol", "side", "client_order_id", "reason"))
_TABLES = {
    "fills": (("fills", "paper"), ("shadow_fills", "shadow")),
    "equity": (("equity_snapshots", "paper"), ("shadow_equity_snapshots", "shadow")),
}


def _safe_text(value: str) -> str:
    """Prefix spreadsheet formulas with an apostrophe, including leading whitespace."""
    stripped = value.lstrip("\ufeff\t\r\n ")
    return "'" + value if stripped.startswith(("=", "+", "-", "@")) else value


def build_csv(
    settings: Settings,
    dataset: str,
    since_text: str = "30d",
    *,
    include_shadow: bool = False,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Return CSV text and row count for inclusive [now-since, now] ledger records.

    Numeric fields are copied as stored, with no floating-point conversion.
    Untrusted text beginning with a spreadsheet formula sigil, including after
    leading whitespace, is prefixed with an apostrophe before CSV quoting.
    Only fixed allowlisted tables and columns are read. No ledger is created.
    """
    if dataset not in _TABLES:
        raise ValueError("dataset must be fills or equity")
    window = parse_since(since_text)
    end = now if now is not None else utcnow()
    if end.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    end = end.astimezone(timezone.utc)
    start = end - window
    columns = FILL_COLUMNS if dataset == "fills" else EQUITY_COLUMNS
    records: list[tuple[datetime, int, int, dict[str, str]]] = []
    if (Path(settings.state_dir) / "bot.sqlite").is_file():
        ledger = Ledger(settings, readonly=True)
        try:
            tables = _TABLES[dataset] if include_shadow else _TABLES[dataset][:1]
            for book_index, (table, book) in enumerate(tables):
                # Table names come only from _TABLES, never user input.
                for row in ledger.conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                    ts = parse_ts(str(row["ts"]))
                    if not start <= ts <= end:
                        continue
                    output = {
                        key: (
                            str(row[key]) if key in row.keys() and row[key] is not None else ""
                        )
                        for key in columns
                        if key != "book"
                    }
                    output["book"] = book
                    for key in _TEXT_COLUMNS.intersection(output):
                        output[key] = _safe_text(output[key])
                    records.append((ts, int(row["id"]), book_index, output))
        finally:
            ledger.close()
    records.sort(key=lambda item: (item[0], item[1], item[2]))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(row for _ts, _id, _book, row in records)
    return stream.getvalue(), len(records)
