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

Fills against these mids pay `cost_per_side` (default 1% on a buy and 1% on a sell). That stands in for Robinhood's small-account spread. A quoted bid and ask from the public exchange is only a sanity check. It is not added on top of the 1%.

`RobinhoodMarketData` is optional and off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs GET requests for `/api/v1/crypto/marketdata/best_bid_ask/` only, with a 100-request-per-minute budget and a clock-skew check against the 30-second signing window. Buys then fill at `ask_inclusive_of_buy_spread` and sells at `bid_inclusive_of_sell_spread`, and the extra 1% is not applied again. Candles still come from the public source, because the Robinhood API does not publish OHLC. If you ask for Robinhood quotes but set neither variable, the bot stays on public prices and records `public_fallback`. If you set only one variable, it refuses to start.

`LiveBroker` exists so the seam is obvious. Every method raises `LiveTradingDisabled`. It does not open a socket.

## Strategies

Each strategy is a pure function from bars, quotes, positions, and its own state to order intents. The engine is the only thing that fills.

| Sleeve | Behavior |
| --- | --- |
| `buy_and_hold` | On the first cycle, split cash across BTC and ETH and then hold. |
| `dca_weekly` | Buy `dca_notional` (default $25) of each symbol once per ISO week. |
| `trend_daily` | Once per UTC day, hold a symbol when its last closed daily close is above its N-day average by the no-trade band; otherwise go to cash. A minimum hold applies in both directions after the first entry. |

N, the band, and the minimum hold are constants chosen before any backtest: 20 days, 1%, 5 days. `rhbot/backtest.py` replays those same functions over bars you already have. It does not search parameters.

## Risk

`rhbot/risk.py` runs inside every paper fill, including a direct broker call. On any unexpected error it denies the order. The checks, in order:

1. Duplicate client id.
2. Symbol allowlist (BTC-USD, ETH-USD only).
3. Long-only size (a sell cannot exceed the position).
4. Quote present, not from the future, and not older than `max_quote_age_seconds`.
5. Spread sanity when bid and ask are present.
6. Kill file. A present file blocks new risk. `rhbot flatten --paper` may still sell.
7. Daily drawdown and max drawdown. A breach denies the order and asks the engine to write `state/KILL`.
8. Minimum notional, cash on hand, per-symbol cap, total exposure, daily turnover, and trades per day.

Hard caps live in `HARD_CAPS` in `rhbot/config.py`. Config may only tighten them. The per-symbol cap is applied to the position's value at the mid, not to the cash spent, because the 1% cost would otherwise make a full-size position impossible.

Default limits: 50% of sleeve equity per coin, 100% total exposure, 5% daily drawdown, 10% max drawdown, 4 fills per sleeve per day, quotes older than 120 seconds rejected (the ceiling is 180).

Crossing a drawdown limit stops new simulated buys and sells from the strategies. It does not auto-sell. Flatten is a separate operator command and still refuses a stale quote.

## Ledger and audit

`state/bot.sqlite` holds sleeves, positions, fills, open-order rows, equity snapshots, candle cache, and the `events` table. Each event stores the previous row's hash and `sha256(prev + payload)`. Database triggers reject updates and deletes on `events` and `fills`. `rhbot audit verify` replays the chain and, when a heartbeat exists, checks that its `audit_head` matches the log. Health also replays cash and positions from fills.

## Operator surface

`rhbot` prints JSON and uses exit codes 0 (ok or idle), 1 (degraded), and 2 (critical or bad input). `report --md` is the one command that prints Markdown instead. See `TEAM.md` for which role runs which command.

`rhbot selftest` validates config, checks the read-only signature helper, buys and flattens a throwaway book, trips the kill path, and verifies the hash chain. It uses a temporary directory so a drill cannot stop the running book.

## Out of scope

Stocks, options, margin, shorting, intraday signals, model-chosen trades, unofficial Robinhood clients, and any real order.
