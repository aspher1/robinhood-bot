# Architecture

Paper-only BTC and ETH. Three independent sleeves share one process and one database. They do not share cash. Each starts at the configured bankroll, default $1,000.

```
Coinbase public bid/ask (no key) --> engine cycle --> strategy intents
                                                     |
                                                     v
                                              risk engine (code)
                                                     |
                                                     v
                                              paper fills + SQLite
                                                     |
                                                     v
                                         hash-chained decision log
```

The long-lived loop is `rhbot run`. It wakes every `loop_seconds` (default 60), writes `state/heartbeat.json`, and prints one JSON line. The heartbeat is not just "process alive": it carries the last successful cycle, the last decision, and the last fresh quote. Paper starts with a fresh `state/` directory. Reusing an old one keeps the old peak, paper day 1, and fills.

The live loop checks drawdown every cycle. Replay checks it once a day, using the closed daily price as the bid and ask. A missing Coinbase quote raises before any sleeve trades, so that cycle fills nothing.

## Market data

`PublicMarketData` needs no API key. v1 paper uses Coinbase public bid/ask for marks, fills, and the spread cap (`market_data: public`, `public_provider: coinbase`). Daily bars are cached in SQLite. The signal uses only candles that have already closed, so the current partial day is not treated as a close. Kraken OHLC and ticker parsers remain for diagnostics. The engine does not price orders from them.

Fills pay `cost_per_side` (hard floor 1% on a buy and 1% on a sell). That stands in for a small-account spread. The bid and ask are the quote: if either side is missing, or either side is more than 2% from the mid, the order is denied. The spread is not added on top of the 1%.

A missing Coinbase quote fails the whole cycle closed: the engine records `quote_hard_stop` and raises before any sleeve trades, so nothing in that cycle is filled. A quote 30 seconds old or older, or a quote with no bid/ask, denies new orders and sets the same hard stop. The bot does not switch to Robinhood, Kraken, or a last trade. A reduce_only kill or flatten sell may still use the last valid bid/ask. Robinhood best-bid/ask is not selected, including when the API key is missing.

`LiveBroker` exists so the seam is obvious. Every method raises `LiveTradingDisabled`. It does not open a socket. The engine never constructs it. `mode: live` is rejected, and `RHBOT_LIVE`, `RHBOT_MODE=live`, `LIVE_TRADING`, and `ENABLE_LIVE_TRADING` are rejected too. None of those can place an order.

## Strategies

Each strategy is a pure function from bars, quotes, positions, and its own state to order intents. The engine is the only thing that fills.

| Sleeve | Behavior |
| --- | --- |
| `buy_and_hold` | On the first cycle, split cash across BTC and ETH and then hold. |
| `dca_weekly` | Buy `dca_notional` (default $19.23) of one coin every 7 days from paper day 1, BTC then ETH. |
| `trend_daily` | Once per UTC day, hold a symbol when its last closed daily close is above its N-day average by the no-trade band; otherwise go to cash. BTC and ETH keep separate cash. An entry is that coin's cash, capped by the existing 50% per trade, 50% per coin, and 100% total limits after the 1% cost. Under $10 is skipped. A minimum hold applies in both directions after the first entry. |

N and the band were chosen before any backtest: 200 days and 2%. The minimum hold is the risk floor, 7 days. `rhbot/backtest.py` replays those same functions over bars you already have, one cycle per closed day. It does not search parameters. The live loop runs the drawdown check every cycle; replay runs it at that daily close.

## Risk

`rhbot/risk.py` runs inside every paper fill, including a direct broker call. On any unexpected error it denies the order. The checks, in order:

1. Duplicate client id. Submitting the same id again returns the original fill.
2. Symbol allowlist (BTC-USD, ETH-USD only). Spot, long only. No margin, leverage, or shorting.
3. Long-only size (a sell cannot exceed the position).
4. Quote present, not from the future, and not older than 30 seconds.
5. Spread, when bid and ask are present. Skip the order if either side is more than 2% from the mid.
6. Kill file. A present file blocks new risk. `rhbot flatten --paper` may still sell.
7. One order per symbol per sleeve per UTC day. A second strategy order on that symbol is denied.
8. Drawdown freeze. While a strategy book is FROZEN, its new buys are denied. Sells still work. Buy-and-hold has no overlay.
9. Daily loss. At 4% from the UTC day-start equity, new buys are denied until the next UTC day. Sells still work.
10. Minimum notional ($10), cash on hand, per-trade cap (50% of equity), per-symbol cap, total exposure, daily turnover, and the per-book trade cap.

Hard caps live in `HARD_CAPS` in `rhbot/config.py`. Config may only tighten them. The per-symbol cap is applied to the position's value at the mid, not to the cash spent, because the 1% cost would otherwise make a full-size position impossible.

Default limits, which are also the ceilings: 50% of sleeve equity per coin, 100% total exposure, 50% per trade, $10 minimum, 2 strategy fills per day per book (risk-reduction sells do not count), 100% daily turnover, 7-day minimum hold, quotes older than 30 seconds rejected, spread wider than 2% per side skipped.

Drawdown is mark-to-bid equity divided by that book's running peak, minus one. It applies to trend and DCA only. These two thresholds are paper-only. They are looser than a live book should use, and they must not be carried into any live phase. Config may only tighten them.

| Drawdown from that book's peak | What the engine does |
| --- | --- |
| 10% (`freeze_drawdown_pct`) | Sets that book to FROZEN, logs `freeze_trip`, and blocks its new buys until `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. Exits stay allowed. Nothing is force-sold. |
| 40% (`kill_drawdown_pct`) | Sets that book to KILLED, logs a `kill_trip`, and flattens that book only. A leftover position is retried on the next cycle. Until that book is flat, health is critical (`kill_flatten_incomplete`) and resume stays blocked. It does not write the process-wide `state/KILL`, so the other book is not blocked. Human resume is still required for the killed book. Resume does not move the all-time peak. It records mark-to-bid equity as `restart_baseline`. Later 10% and 40% lines use max(that baseline, the highest equity since the restart). A new all-time high uses the peak again. Nothing restarts itself. |

There is no 5% or 7.5% exposure cut. A 4% loss versus the UTC day-start equity still blocks new buys in that sleeve until the next UTC day. It does not freeze, kill, or sell. Sleeve equity, the day-start mark, the portfolio peak, and the report all use the same mark-to-bid as that drawdown. The engine never clears `state/KILL`. Drawdown uses mark-to-bid equity against that book's own peak.

The two acknowledgements are not interchangeable:

- The AI operator may acknowledge a **10% freeze** with `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. The audit event is `freeze_ack`. `rhbot status` and `rhbot report` include the book's state, drawdown from the all-time peak, peak, last trip, last ack, and ack delay. The command does not move the peak or the restart baseline. After it, buys are allowed while the active baseline drawdown is still at or below −10%. The book returns to ARMED only after that drawdown recovers above −10%. The next crossing freezes again. Weekly DCA buys wait during the pause and the skipped week is not caught up.
- A **40% kill** is human-only and stays on that book. Automation may trip it and must never clear it. It does not create the process-wide `state/KILL` file, so the other overlay book can still trade. The only clear path is `rhbot resume --ack --human-code <code>`, and the code must match `RHBOT_HUMAN_RESUME_FILE`. That acknowledgement does not move the book's all-time peak. It records that book's mark-to-bid equity as `restart_baseline` and writes a `restart_baseline` audit event with the timestamp and the old peak. Later 10% and 40% lines use the greater of that baseline and the highest equity since the restart. A new all-time high above the old peak makes the reference the peak again. It does not add cash. The operator must not run it. `ack-drawdown` does not clear a kill and does not move the baseline. A manual `rhbot kill` still writes `state/KILL` and blocks every book.

Other critical health problems still block resume.

Each cycle also fills a no-overlay shadow ledger: the same strategies and the same risk checks, except the 10% freeze and the 40% kill. The shadow book is not flattened when the kill trips. `rhbot report` scores the buy-and-hold benchmark from that shadow sleeve, and lists every shadow sleeve under `no_overlay` with `overlay_effect` (live equity minus shadow equity) so the overlay is visible.

## Ledger and audit

`state/bot.sqlite` holds sleeves, positions, fills, open-order rows, equity snapshots, candle cache, and the `events` table. Each event stores the previous row's hash and `sha256(prev + payload)`. Database triggers reject updates and deletes on `events`, `fills`, and `trade_log`. `rhbot audit verify` replays the chain and, when a heartbeat exists, checks that its `audit_head` matches the log. Health also replays cash and positions from fills.

An open order older than one loop is critical (`open_order_stale`). Reconcile names its `client_order_id`. A fill closes its order in the same transaction, so a healthy book has none left open.

A risk denial is an audit event of kind `risk_denial` and a matching `trade_log` row. A 10% paper freeze is kind `freeze_trip`, with the limit `freeze_drawdown_pct`, plus equity, peak, and drawdown. An acknowledgement is kind `freeze_ack`. A 40% paper kill is kind `kill_trip`, with `ack_required`, the reason `max_drawdown`, and the limit `kill_drawdown_pct`. `rhbot status` and the heartbeat distinguish `buy_pause` from `kill_switch` and include each book's overlay state. Client order ids are `sleeve:symbol:side:decision_key`, with no random suffix. A risk-reduction sell appends its reason, so a same-day strategy sell cannot be returned as the kill flatten. If that flatten leaves a position, the next cycle retries `drawdown_flatten` for that book only. Health stays critical (`kill_flatten_incomplete`) and resume stays blocked until the book is flat. The health check and a hash-chained event name the sleeve, the symbols, and the remaining quantity. `rhbot status` and `rhbot report` open the ledger read-only. `rhbot report` lists denials, freezes, acknowledgements, and kills on their own, plus `trend_daily_shadow` and `dca_weekly_shadow`. A fidelity mismatch is a separate check: stored cash or positions do not match a replay of the fills. Offline replay calls the same `Engine.run_once` path in a temporary directory. It refuses the live state dir and does not clear a kill.

## Operator surface

`rhbot` prints JSON and uses exit codes 0 (ok or idle), 1 (degraded), and 2 (critical or bad input). `report --md` is the one command that prints Markdown instead. See `TEAM.md` for which role runs which command.

`rhbot dashboard`, `rhbot export trades`, and `rhbot export equity` read the paper ledger only. They do not start the bot, fetch prices, or change bot state. No API key. The HTML page has three separate sections: Saved results (recorded equity, P&L, and trades), Stale health (heartbeat or quotes are old; the numbers are still the last saved marks), and Incomplete data (no ledger yet, a book has no equity snapshots, reconcile failed, or a kill flatten did not finish). Returns and the buy-and-hold benchmark on the page are percent, the same percent `rhbot report` already prints. They are not dollars. The equity CSV column `drawdown_fraction` keeps the stored fraction (0.10 means 10% off the peak), not a percent. CSV timestamps stay ISO. The page clock is `YYYY-MM-DD HH:MM:SS UTC`. `export trades` is simulated fills. `export equity` is equity snapshots. Omit `--out` to print to stdout. `--since` on dashboard matches report (`24h`, `7d`, `30d`). `rhbot status` and `rhbot report` stay the place that shows each book's overlay drawdown.

`rhbot selftest` validates config, checks the read-only signature helper, buys and flattens a throwaway book, trips the kill path, and verifies the hash chain. It uses a temporary directory so a drill cannot stop the running book.

## Out of scope

Stocks, options, margin, shorting, intraday signals, model-chosen trades, unofficial Robinhood clients, and any real order.
