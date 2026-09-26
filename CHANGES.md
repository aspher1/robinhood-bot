# Changes

## 0.1.0

Paper-only BTC/ETH bot.

- Three sleeves, each starting at $1,000: buy-and-hold, weekly DCA ($25 per coin), and a daily trend filter (20-day average, 1% band, 5-day minimum hold). Those trend settings were chosen up front and are not fitted.
- Public Coinbase prices by default, Kraken as the other public source. No API key required. Fills on those mids pay a configurable 1% per side.
- Optional Robinhood best-bid/ask adapter. It is off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs read-only GET requests, budgets 100 requests per minute, and never adds the extra 1% on top of an inclusive bid or ask. Candles still come from the public source.
- Risk checks before every simulated fill: symbol allowlist, long-only, position cap, total exposure, daily turnover, trade count, daily and max drawdown, stale quotes, and the kill file. Hard caps are constants. Config can only tighten them. Drawdown writes `state/KILL`.
- `rhbot` commands: `run`, `status`, `health`, `report`, `kill`, `resume`, `flatten --paper`, `selftest`, `audit verify`. JSON on stdout. Heartbeat file written every cycle.
- Append-only hash-chained decision and fill log. `audit verify` recomputes it.
- Offline pytest suite and GitHub Actions. One test fails the build if the package references a Robinhood order endpoint or an HTTP write.
- `LiveBroker` raises on every call. There is no live order path.

### Left open

- No live Robinhood key was available here, so the quote adapter is covered with fixtures and the signature vector, not a production account.
- Selftest runs in a throwaway directory. It exercises the same kill functions without flipping the running bot's kill file.
- No email or SMS. The operator reads JSON (or `report --md`) and escalates.
- No multi-year candle downloader beyond what the public daily endpoint returns (a few hundred bars).
- No path from paper results to real money. That still requires Randy's written approval, and the code for it is intentionally absent.
