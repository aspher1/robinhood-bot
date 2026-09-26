# Architecture

Paper-only BTC and ETH. Three independent sleeves share one process and one database. They do not share cash. Each starts at the configured bankroll, default $1,000.

```
public prices (no key) ----\
                            +--> engine cycle --> strategy intents
optional RH bid/ask -------/                         |
                                                     v
                                              risk engine (code)
                                                     |
                                                     v
                                              paper fills + SQLite
                                                     |
                                                     v
                                         hash-chained decision log
```

The long-lived loop is `rhbot run`. It wakes every `loop_seconds` (default 60), writes `state/heartbeat.json`, and prints one JSON line. The heartbeat is not just "process alive": it carries the last successful cycle, the last decision, and the last fresh quote.

## Market data

`PublicMarketData` is the default. It needs no API key. Coinbase public ticker and daily candles are the default source; Kraken public OHLC and ticker are the alternate (`public_provider`). Daily bars are cached in SQLite. The signal uses only candles that have already closed, so the current partial day is not treated as a close.

Fills against these mids pay `cost_per_side` (hard floor 1% on a buy and 1% on a sell). That stands in for Robinhood's small-account spread. A quoted bid and ask is a sanity check: if either side is more than 2% from the mid, the order is skipped. The spread is not added on top of the 1%. An inclusive Robinhood quote that is already wider than 1% per side is filled at that bid or ask. A tighter inclusive quote is widened to the 1% floor.

`RobinhoodMarketData` is optional and off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs GET requests for `/api/v1/crypto/marketdata/best_bid_ask/` only, with a 100-request-per-minute budget and a clock-skew check against the 30-second signing window. Buys then fill at `ask_inclusive_of_buy_spread` and sells at `bid_inclusive_of_sell_spread`, and the extra 1% is not applied again. Candles still come from the public source, because the Robinhood API does not publish OHLC. If you ask for Robinhood quotes but set neither variable, the bot stays on public prices and records `public_fallback`. If you set only one variable, it refuses to start.

`LiveBroker` exists so the seam is obvious. Every method raises `LiveTradingDisabled`. It does not open a socket. The engine never constructs it. `mode: live` is rejected, and `RHBOT_LIVE`, `RHBOT_MODE=live`, `LIVE_TRADING`, and `ENABLE_LIVE_TRADING` are rejected too. None of those can place an order.

## Strategies

Each strategy is a pure function from bars, quotes, positions, and its own state to order intents. The engine is the only thing that fills.

| Sleeve | Behavior |
| --- | --- |
| `buy_and_hold` | On the first cycle, split cash across BTC and ETH and then hold. |
| `dca_weekly` | Buy `dca_notional` (default $19.23) of one coin every 7 days from paper day 1, BTC then ETH. |
| `trend_daily` | Once per UTC day, hold a symbol when its last closed daily close is above its N-day average by the no-trade band; otherwise go to cash. A minimum hold applies in both directions after the first entry. |

N and the band were chosen before any backtest: 200 days and 2%. The minimum hold is the risk floor, 7 days. `rhbot/backtest.py` replays those same functions over bars you already have. It does not search parameters.

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
| 40% (`kill_drawdown_pct`) | Writes `state/KILL` with `ack_required`, logs a `kill_trip`, and flattens that book. |

There is no 5% or 7.5% exposure cut. A 4% loss versus the UTC day-start equity still blocks new buys in that sleeve until the next UTC day. It does not freeze, kill, or sell. The engine never clears `state/KILL`. Drawdown uses mark-to-bid equity against that book's own peak.

The two acknowledgements are not interchangeable:

- The AI operator may acknowledge a **10% freeze** with `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. The audit event is `freeze_ack`. `rhbot status` and `rhbot report` include the book's state, drawdown, peak, last trip, last ack, and ack delay. The command does not move the peak. After it, buys are allowed while drawdown is still at or below −10%. The book returns to ARMED only after drawdown recovers above −10%. The next crossing freezes again.
- A **40% kill** is human-only. Automation may trip it and must never clear it. The only clear path is `rhbot resume --ack --human-code <code>`, and the code must match `RHBOT_HUMAN_RESUME_FILE`. That acknowledgement does not move the book's peak. Resume records the current peak on the killed book so the same episode does not flatten again. It does not add cash. The operator must not run it. `ack-drawdown` does not clear `state/KILL`.

Other critical health problems still block resume.

Each cycle also fills a no-overlay shadow ledger: the same strategies and the same risk checks, except the 10% freeze and the 40% kill. The shadow book is not flattened when the kill trips. `rhbot report` scores the buy-and-hold benchmark from that shadow sleeve, and lists every shadow sleeve under `no_overlay` with `overlay_effect` (live equity minus shadow equity) so the overlay is visible.

## Ledger and audit

`state/bot.sqlite` holds sleeves, positions, fills, open-order rows, equity snapshots, candle cache, and the `events` table. Each event stores the previous row's hash and `sha256(prev + payload)`. Database triggers reject updates and deletes on `events`, `fills`, and `trade_log`. `rhbot audit verify` replays the chain and, when a heartbeat exists, checks that its `audit_head` matches the log. Health also replays cash and positions from fills.

A risk denial is an audit event of kind `risk_denial` and a matching `trade_log` row. A 10% paper freeze is kind `freeze_trip`, with the limit `freeze_drawdown_pct`, plus equity, peak, and drawdown. An acknowledgement is kind `freeze_ack`. A 40% paper kill is kind `kill_trip`, with `ack_required`, the reason `max_drawdown`, and the limit `kill_drawdown_pct`. `rhbot status` and the heartbeat distinguish `buy_pause` from `kill_switch` and include each book's overlay state. Client order ids are `sleeve:symbol:side:decision_key`, with no random suffix. `rhbot report` lists denials, freezes, acknowledgements, and kills on their own, plus `trend_daily_shadow` and `dca_weekly_shadow`. A fidelity mismatch is a separate check: stored cash or positions do not match a replay of the fills. Offline replay calls the same `Engine.run_once` path in a temporary directory. It refuses the live state dir and does not clear a kill.

## Operator surface

`rhbot` prints JSON and uses exit codes 0 (ok or idle), 1 (degraded), and 2 (critical or bad input). `report --md` is the one command that prints Markdown instead. See `TEAM.md` for which role runs which command.

`rhbot selftest` validates config, checks the read-only signature helper, buys and flattens a throwaway book, trips the kill path, and verifies the hash chain. It uses a temporary directory so a drill cannot stop the running book.

## Out of scope

Stocks, options, margin, shorting, intraday signals, model-chosen trades, unofficial Robinhood clients, and any real order.
