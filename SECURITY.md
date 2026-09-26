# Security

This bot is paper-only. It does not place, amend, or cancel real orders.

## Secrets

- Do not commit API keys, private keys, or `.env` files.
- A read-only Robinhood quote key, if you create one later, lives only in environment variables: `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64`.
- The process never reads those values from the repository.
- Create that key with read-only actions. This code still cannot send an order with it: the quote client signs GET requests for best bid/ask only, and any other path or method is refused.
- Operators and AI agents do not read, print, or copy those variables.

## Live trading

There is no live order path. `LiveBroker` raises `LiveTradingDisabled` on every call and does not open a socket. The engine never constructs it.

`mode` accepts only `paper`. Setting `RHBOT_LIVE`, `RHBOT_MODE=live`, `LIVE_TRADING`, or `ENABLE_LIVE_TRADING` makes startup fail. No config flag turns live trading on. The test `test_package_has_no_live_order_path` fails CI if the package names a Robinhood order endpoint or issues an HTTP POST, PUT, PATCH, or DELETE.

## Kill switch and drawdown freeze

`state/KILL` stops new simulated risk. If the file is missing, unreadable, or not valid JSON, treat a present file as on. The engine never deletes it.

A FROZEN strategy book blocks that book's new buys, including a due DCA buy. Sells still work. Buy-and-hold has no overlay. The operator acknowledges one book with `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. That writes a `freeze_ack` audit event. It does not move the peak. Buys are allowed again while drawdown is still at or below −10%. The book re-arms only after drawdown recovers above −10%.

Drawdown is mark-to-bid equity divided by that book's running peak, minus one. These limits are paper-only and must not be carried into any live phase. The hard caps are `freeze_drawdown_pct` (0.10) and `kill_drawdown_pct` (0.40). Config may only tighten them, and the freeze must stay below the kill.

- −10% sets that book to FROZEN and does not sell. The operator may acknowledge it. That acknowledgement does not move the peak.
- −40% sets that book to KILLED and flattens it. It does not write the process-wide `state/KILL`, so the other book is not blocked. This kill is human-only: `rhbot resume --ack --human-code`. A manual `rhbot kill` still writes `state/KILL` and blocks every book.

Clearing that kill takes `rhbot resume --ack --human-code <code>`. The code must match the secret in `RHBOT_HUMAN_RESUME_FILE`. Without it the command exits 2. Resume does not move the peak. Automation may trip a kill and cannot clear it. The operator must not run that resume for a drawdown kill. A manual `rhbot kill` can be cleared with `rhbot resume` once health is otherwise fine. Resume still refuses while some other health check is critical. `ack-drawdown` does not clear a kill. Alerts stay in the status and heartbeat JSON. There is no email, SMS, or webhook.

## Ownership

`.github/CODEOWNERS` names the reviewer for `rhbot/risk.py`, the hard caps in `rhbot/config.py`, and `rhbot/brokers/`. Config may only tighten those caps.

## Real money

Real money requires Randy's written approval. It is out of scope. There is no live order code to turn on.

## Audit

Decisions, fills, kills, and resumes are appended to a hash-chained SQLite log. Updates and deletes on that log are rejected by the database. `rhbot audit verify` recomputes the chain. Do not hand-edit `state/bot.sqlite`.
