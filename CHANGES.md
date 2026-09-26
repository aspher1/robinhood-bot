# Changes

## 0.1.0

Paper-only BTC/ETH bot.

- Three sleeves, each starting at $1,000: buy-and-hold, weekly DCA ($25 per coin), and a daily trend filter (20-day average, 1% band, 7-day minimum hold). The average and the band were chosen up front and are not fitted. The hold is the risk floor.
- Public Coinbase prices by default, Kraken as the other public source. No API key required. Fills cost at least 1% per side. Config can raise that cost and cannot lower it.
- Optional Robinhood best-bid/ask adapter. It is off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs read-only GET requests and budgets 100 requests per minute. An inclusive quote wider than 1% per side is used as-is. A tighter one is widened to the 1% floor. Candles still come from the public source.
- Risk checks before every simulated fill. Hard caps in code, which config may only tighten: BTC-USD and ETH-USD, long-only spot, no margin or shorting, 50% per coin, 100% total exposure, 50% per trade, $10 minimum, 2 strategy trades per day, 100% daily turnover, 7-day minimum hold, quotes older than 30 seconds rejected, spread wider than 2% per side skipped. The same client order id returns the original fill. One strategy order per symbol per day.
- Loss policy, paper only: a 4% UTC-day loss blocks new buys until the next UTC day. Drawdown is measured on the combined portfolio peak. Hard caps are `pause_drawdown_pct` 0.10 and `kill_drawdown_pct` 0.40. Config may only tighten them. At 10% the engine raises an alert and freezes new buys, including weekly DCA, until the operator runs `rhbot ack-drawdown --reason "..."`. That acknowledgement is an audit event with the actor and the reason. It does not reset the peak. The freeze re-arms only after drawdown recovers above −10% and then falls below it again. Exits and sells stay allowed, and nothing is force-sold. At 40% it writes `state/KILL`, flattens the paper book, and requires a human-only `rhbot resume --ack`. That acknowledgement does not rebase the peak either. The peak rises only on a new equity high. There is no auto-resume. Automation may trip a kill and cannot clear it. The operator must not clear that kill. The 5% and 7.5% exposure-cut tiers are removed. These looser drawdown limits must not be carried into any live phase.
- Client order ids are deterministic: sleeve, symbol, side, and UTC day. A second submit of the same daily intent returns the original fill. `rhbot status` and the heartbeat expose `buy_pause`, `kill_switch`, `peak_equity`, `drawdown_pct`, `ack_required`, and `rearm_eligible`. Alerts stay JSON-only.
- `rhbot report` scores buy-and-hold from a no-overlay shadow ledger (same strategies and risk checks, without the 10% freeze or the 40% kill) and includes that shadow book, with the live-minus-shadow equity delta, so the overlay's effect is visible. `status` and `report` list freeze acknowledgements.
- Every risk denial, drawdown freeze, and kill trip is its own event in the hash-chained audit log and in the append-only trade log, with the reason and the limit that was hit. `rhbot report` shows those blocks separately from a fill-replay mismatch. Offline replay uses the same engine and the same risk checks.
- `rhbot` commands: `run`, `status`, `health`, `report`, `kill`, `resume`, `ack-drawdown`, `flatten --paper`, `selftest`, `audit verify`. JSON on stdout. Heartbeat file written every cycle.
- Append-only hash-chained decision and fill log. `audit verify` recomputes it.
- Offline pytest suite and GitHub Actions. One test fails the build if the package references a Robinhood order endpoint or an HTTP write.
- `LiveBroker` raises on every call. There is no live order path. No config flag or environment variable can enable one. `.github/CODEOWNERS` covers the risk module, the hard caps, and the broker directory.

### Left open

- No live Robinhood key was available here, so the quote adapter is covered with fixtures and the signature vector, not a production account.
- Selftest runs in a throwaway directory. It exercises the same kill functions without flipping the running bot's kill file.
- No email or SMS. The operator reads JSON (or `report --md`) and escalates.
- No multi-year candle downloader beyond what the public daily endpoint returns (a few hundred bars).
- No path from paper results to real money. That still requires Randy's written approval, and the code for it is intentionally absent.
