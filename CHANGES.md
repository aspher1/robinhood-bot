# Changes

## 0.1.0

Paper-only BTC/ETH bot.

- Three sleeves, each starting at $1,000: buy-and-hold, weekly DCA ($25 per coin), and a daily trend filter (20-day average, 1% band, 7-day minimum hold). The average and the band were chosen up front and are not fitted. The hold is the risk floor.
- Public Coinbase prices by default, Kraken as the other public source. No API key required. Fills cost at least 1% per side. Config can raise that cost and cannot lower it.
- Optional Robinhood best-bid/ask adapter. It is off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs read-only GET requests and budgets 100 requests per minute. An inclusive quote wider than 1% per side is used as-is. A tighter one is widened to the 1% floor. Candles still come from the public source.
- Risk checks before every simulated fill. Hard caps in code, which config may only tighten: BTC-USD and ETH-USD, long-only spot, no margin or shorting, 50% per coin, 100% total exposure, 50% per trade, $10 minimum, 2 strategy trades per day, 100% daily turnover, 7-day minimum hold, quotes older than 30 seconds rejected, spread wider than 2% per side skipped. The same client order id returns the original fill. One strategy order per symbol per day.
- Loss policy: a 4% UTC-day loss blocks new buys until the next UTC day. Drawdown from peak of 5% cuts exposure to 50%, 7.5% cuts it to 25%, and 10% writes `state/KILL` and flattens the paper book. Clearing that kill takes `rhbot resume --ack`. The process never clears it on its own.
- `rhbot` commands: `run`, `status`, `health`, `report`, `kill`, `resume`, `flatten --paper`, `selftest`, `audit verify`. JSON on stdout. Heartbeat file written every cycle.
- Append-only hash-chained decision and fill log. `audit verify` recomputes it.
- Offline pytest suite and GitHub Actions. One test fails the build if the package references a Robinhood order endpoint or an HTTP write.
- `LiveBroker` raises on every call. There is no live order path. No config flag or environment variable can enable one. `.github/CODEOWNERS` covers the risk module, the hard caps, and the broker directory.

### Left open

- No live Robinhood key was available here, so the quote adapter is covered with fixtures and the signature vector, not a production account.
- Selftest runs in a throwaway directory. It exercises the same kill functions without flipping the running bot's kill file.
- No email or SMS. The operator reads JSON (or `report --md`) and escalates.
- No multi-year candle downloader beyond what the public daily endpoint returns (a few hundred bars).
- No path from paper results to real money. That still requires Randy's written approval, and the code for it is intentionally absent.
