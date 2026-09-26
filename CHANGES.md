# Changes

## 0.1.0

Paper-only BTC/ETH bot.

- Three sleeves, each starting at $1,000. Buy-and-hold deploys once. The daily trend uses a 200-day average and a 2% band, with a 7-day minimum hold and no re-entry cooldown. Each coin's sleeve buys its full cash on entry and sells the position on exit. Weekly DCA is disabled until Randy sets the amount (`dca_amount_pending_owner_decision`). Its schedule is every 7 days from paper day 1. The average and the band were chosen up front and are not fitted. The hold is the risk floor.
- Public Coinbase prices by default, Kraken as the other public source. No API key required. Fills cost at least 1% per side. Config can raise that cost and cannot lower it.
- Optional Robinhood best-bid/ask adapter. It is off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs read-only GET requests and budgets 100 requests per minute. An inclusive quote wider than 1% per side is used as-is. A tighter one is widened to the 1% floor. Candles still come from the public source.
- Risk checks before every simulated fill. Hard caps in code, which config may only tighten: BTC-USD and ETH-USD, long-only spot, no margin or shorting, 50% per coin, 100% total exposure, 50% per trade, $10 minimum, 2 strategy trades per day, 100% daily turnover, 7-day minimum hold, quotes older than 30 seconds rejected, spread wider than 2% per side skipped. The same client order id returns the original fill. One strategy order per symbol per day.
- Loss policy, paper only (F-001). A 4% UTC-day loss blocks new buys until the next UTC day. Drawdown is mark-to-bid equity divided by that book's running peak, minus one. It applies to `trend_daily` and `dca_weekly` only. Buy-and-hold is never frozen, killed, or flattened by it. Hard caps are `freeze_drawdown_pct` 0.10 and `kill_drawdown_pct` 0.40. Config may only tighten them, and the freeze must stay below the kill. At −10% the book goes FROZEN, logs `freeze_trip` (equity, peak, dd), and denies new trend and DCA buys with reason `freeze`. Exits stay allowed. Nothing is force-sold. The operator acknowledges with `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. That does not move the peak and does not fill skipped entries. Buys are allowed again while still at or below −10%. The book re-arms only after drawdown recovers above −10%, and the next crossing freezes again. The ack is refused unless reconcile passes, the book is FROZEN, and the stored trip drawdown matches a recomputation within 1e-6. At −40% that book logs `kill_trip`, flattens at the 1% cost floor with reason `drawdown_flatten`, and writes `state/KILL`. Resume is human-only: `rhbot resume --ack --human-code <code>` must match the secret in `RHBOT_HUMAN_RESUME_FILE`. Without it the command exits 2. Resume does not move the peak. A manual kill still clears with `rhbot resume` once health is otherwise fine. The 5% and 7.5% exposure cuts stay removed. These limits must not be carried into any live phase.
- Client order ids are `sleeve:symbol:side:decision_key` (F-006). The key is the UTC bar date, or the DCA schedule index. The fill and the strategy-state update commit in one transaction. A rerun returns the original fill.
- `rhbot status`, health, and the heartbeat show each overlay book's state, drawdown, peak, last trip, last ack, and ack delay (F-001, F-004). Quote health uses the cycle's quote age plus the age of `last_quote_ok_at`, not a raw now-minus-quote-time check. The risk engine still rejects quotes older than 30 seconds at order time. Kraken quote time comes from the public trades feed; a missing trade time is untrusted and buys are denied (F-007).
- `trend_daily_shadow` and `dca_weekly_shadow` run the same code and the same caps without the overlay (F-011). They are excluded from the scored book. The report shows overlaid equity, shadow equity, and `overlay_impact`.
- Every freeze, kill, and risk denial is its own hash-chained event, with equity, peak, and drawdown on overlay transitions. `rhbot audit replay --since 7d|30d` replays stored closed candles in a temp directory and exits 2 on a decision or fill mismatch (F-010). `replay()` refuses a state dir that already has a ledger or is the service dir (F-005). The candle cache stores a bar only when it was already closed (F-009).
- `rhbot` commands: `run`, `status`, `health`, `report`, `kill`, `resume`, `ack-drawdown`, `flatten --paper`, `selftest`, `audit verify`, `audit replay`. JSON on stdout. Heartbeat file written every cycle. Alerts stay JSON-only.
- Append-only hash-chained decision and fill log. `audit verify` recomputes it.
- Offline pytest suite and GitHub Actions. One test fails the build if the package references a Robinhood order endpoint or an HTTP write.
- `LiveBroker` raises on every call. There is no live order path. No config flag or environment variable can enable one. `.github/CODEOWNERS` covers the risk module, the hard caps, and the broker directory.

### Audit round 1

Done: F-001, F-002 items 1–4, F-004, F-005, F-006, F-007, F-008, F-009, F-010, F-011, F-012, F-013, F-014 (LiveBroker is not re-exported).

Not done, waiting on Randy: F-003 (the 2-trades/day cap stays global; `book_strategy_trades_today` exists and is not wired). F-002 item 5 (DCA amount; `dca_weekly` stays disabled with reason `dca_amount_pending_owner_decision`).

Not done, optional: F-015 through F-019.

### Left open

- No live Robinhood key was available here, so the quote adapter is covered with fixtures and the signature vector, not a production account.
- Selftest runs in a throwaway directory. It exercises the same kill functions without flipping the running bot's kill file.
- No email or SMS. The operator reads JSON (or `report --md`) and escalates.
- No multi-year candle downloader beyond what the public daily endpoint returns (a few hundred bars).
- No path from paper results to real money. That still requires Randy's written approval, and the code for it is intentionally absent.
