# Changes

## 0.1.0

Paper-only BTC/ETH bot.

- Three sleeves, each starting at $1,000. Buy-and-hold deploys once. The daily trend uses a 200-day average and a 2% band, with a 7-day minimum hold and no re-entry cooldown. Each coin's sleeve buys its full cash on entry and sells the position on exit. Weekly DCA (F-002 item 5, Randy's decision) buys one coin every 7 days from paper day 1, BTC then ETH, at $19.23 (`$1,000 / 52`, rounded down to the cent). That clears the $10 minimum, which is unchanged. The average and the band were chosen up front and are not fitted. The hold is the risk floor.
- Public Coinbase prices by default, Kraken as the other public source. No API key required. Fills cost at least 1% per side. Config can raise that cost and cannot lower it.
- Optional Robinhood best-bid/ask adapter. It is off unless `market_data: robinhood` and both `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64` are set in the environment. It signs read-only GET requests and budgets 100 requests per minute. An inclusive quote wider than 1% per side is used as-is. A tighter one is widened to the 1% floor. Candles still come from the public source.
- Risk checks before every simulated fill. Hard caps in code, which config may only tighten: BTC-USD and ETH-USD, long-only spot, no margin or shorting, 50% per coin, 100% total exposure, 50% per trade, $10 minimum, 2 strategy trades per day per book (F-003, Randy's decision; risk-reduction sells do not count), 100% daily turnover, 7-day minimum hold, quotes older than 30 seconds rejected, spread wider than 2% per side skipped. The same client order id returns the original fill. One strategy order per symbol per day. There is no 5% or 7.5% forced sell.
- Loss policy, paper only (F-001). A 4% UTC-day loss blocks new buys until the next UTC day. Drawdown is mark-to-bid equity divided by that book's running peak, minus one. It applies to `trend_daily` and `dca_weekly` only. Buy-and-hold is never frozen, killed, or flattened by it. Hard caps are `freeze_drawdown_pct` 0.10 and `kill_drawdown_pct` 0.40. Config may only tighten them, and the freeze must stay below the kill. At −10% the book goes FROZEN, logs `freeze_trip` (equity, peak, dd), and denies new trend and DCA buys with reason `freeze`. Exits stay allowed. Nothing is force-sold. The operator acknowledges with `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. That does not move the peak and does not fill skipped entries. Buys are allowed again while still at or below −10%. The book re-arms only after drawdown recovers above −10%, and the next crossing freezes again. The ack is refused unless reconcile passes, the book is FROZEN, and the stored trip drawdown matches a recomputation within 1e-6. At −40% that book logs `kill_trip`, flattens at the 1% cost floor with reason `drawdown_flatten`, and stays KILLED. It does not write the process-wide `state/KILL`. If that flatten leaves a position, the next cycle retries it and health stays critical (`kill_flatten_incomplete`) until the book is flat. Resume is human-only: `rhbot resume --ack --human-code <code>` must match the secret in `RHBOT_HUMAN_RESUME_FILE`. Without it the command exits 2. Resume does not move the peak. A manual kill still clears with `rhbot resume` once health is otherwise fine. The 5% and 7.5% exposure cuts stay removed. These limits must not be carried into any live phase.
- Client order ids are `sleeve:symbol:side:decision_key` (F-006). The key is the UTC bar date, or the DCA schedule index. The fill and the strategy-state update commit in one transaction. A rerun returns the original fill.
- `rhbot status`, health, and the heartbeat show each overlay book's state, drawdown, peak, last trip, last ack, and ack delay (F-001, F-004). Quote health uses the cycle's quote age plus the age of `last_quote_ok_at`, not a raw now-minus-quote-time check. The risk engine still rejects quotes older than 30 seconds at order time. Kraken quote time comes from the public trades feed; a missing trade time is untrusted and buys are denied (F-007).
- `trend_daily_shadow` and `dca_weekly_shadow` run the same code and the same caps without the overlay (F-011). They are excluded from the scored book. The report shows overlaid equity, shadow equity, and `overlay_impact`.
- Every freeze, kill, and risk denial is its own hash-chained event, with equity, peak, and drawdown on overlay transitions. `rhbot audit replay --since 7d|30d` replays stored closed candles in a temp directory and exits 2 on a decision or fill mismatch (F-010). `replay()` refuses a state dir that already has a ledger or is the service dir (F-005). The candle cache stores a bar only when it was already closed (F-009).
- `rhbot` commands: `run`, `status`, `health`, `report`, `kill`, `resume`, `ack-drawdown`, `flatten --paper`, `selftest`, `audit verify`, `audit replay`. JSON on stdout. Heartbeat file written every cycle. Alerts stay JSON-only.
- Append-only hash-chained decision and fill log. `audit verify` recomputes it.
- Offline pytest suite and GitHub Actions. One test fails the build if the package references a Robinhood order endpoint or an HTTP write.
- `LiveBroker` raises on every call. There is no live order path. No config flag or environment variable can enable one. `.github/CODEOWNERS` covers the risk module, the hard caps, and the broker directory.

### Audit round 1

Done: F-001, F-002 (including item 5: one $19.23 buy per week, BTC then ETH), F-003 (2 trades/day per book, risk-reduction exempt), F-004, F-005, F-006, F-007, F-008, F-009, F-010, F-011, F-012, F-013, F-014 (LiveBroker is not re-exported).

Randy decided the two items that were on hold. F-003 is per book, and the cap stays 2. F-002 item 5 enables `dca_weekly` at $19.23. The $10 minimum is unchanged. Ack still does not move the peak. The −40% resume is still human-only. The 5% and 7.5% exposure sell-downs stay removed.

### Audit round 2

Already in place from round 1, and still true: resume does not rebase `portfolio_peak` or any sleeve `peak_equity` (peaks only rise on a new high); freeze and kill are per book on mark-to-bid and skip buy-and-hold; SMA 200 and a 2% band with no flat-time block; DCA on days 1, 8, 15, 22 at $19.23; the trade cap is 2 per book; quote health uses the cycle's quote age; client order ids have no random suffix.

Added: an open order older than one loop is critical `open_order_stale`, and reconcile names its `client_order_id` (F-020). After a human resume, the same flattened mark does not kill again. Once that book is back above −40% of the original peak, a later cross of that same peak kills again. It does not need another 40% off the flattened equity.

### Audit round 3

F-021: `shadow_strategy_trades_today(sleeve, day)` counts one shadow book. The cap stays 2. On a day when buy-and-hold, DCA, and trend all signal, trend's shadow still buys both coins after DCA's shadow has traded. `overlay_effect` is zero when the overlay did not block anyone. A third order on that same shadow book is denied. Buy-and-hold stays the benchmark and has no shadow book.

### Audit round 4

F-022: a risk-reduction sell's client order id includes its reason (`drawdown_flatten`, `flatten`, or `exposure_cut`). Returning an earlier fill is allowed only when the reason matches. A same-day `trend_exit` is not reused as the flatten. If the flatten leaves a position, health is critical `kill_flatten_incomplete` and the next cycle retries (F-023).

### Audit iteration 5

Already closed on this branch after `50fead7` (the audited head): F-022 (risk-reduction sells use their own client id and a leftover position fails closed), F-003 (2 trades/day per book), F-002 item 5 (`dca_weekly` buys one coin at $19.23, BTC then ETH), F-021 (shadow trade cap is per book).

F-023: a −40% trip sets that book to KILLED and flattens it. It does not write `state/KILL`, so the other overlay book is not blocked by the file. Buy-and-hold is untouched. If the first flatten leaves quantity, the next cycle retries `drawdown_flatten` for that book only. Until the book is flat, health is critical `kill_flatten_incomplete` and `rhbot resume --ack --human-code` stays blocked. Peaks are not reset. After the book is flat, resume is still human-only: `rhbot resume --ack --human-code` matching `RHBOT_HUMAN_RESUME_FILE`. F-022 was already closed: the flatten client id includes `drawdown_flatten`.

Not done, optional: F-015 through F-019.

### Left open

- No live Robinhood key was available here, so the quote adapter is covered with fixtures and the signature vector, not a production account.
- Selftest runs in a throwaway directory. It exercises the same kill functions without flipping the running bot's kill file.
- No email or SMS. The operator reads JSON (or `report --md`) and escalates.
- No multi-year candle downloader beyond what the public daily endpoint returns (a few hundred bars).
- No path from paper results to real money. That still requires Randy's written approval, and the code for it is intentionally absent.
