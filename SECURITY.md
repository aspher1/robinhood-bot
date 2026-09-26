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

`state/DRAWDOWN_FREEZE` blocks new paper buys, including weekly DCA. Sells still work. The engine never deletes that file. The AI operator clears it with `rhbot ack-drawdown --reason "..."`. That writes a `drawdown_ack` audit event with actor `operator` and the reason. It does not reset the drawdown peak. The freeze re-arms only after drawdown recovers above −10% and then falls below it again.

Drawdown is measured on the combined portfolio peak. These limits are paper-only and must not be carried into any live phase. The hard caps are `pause_drawdown_pct` (0.10) and `kill_drawdown_pct` (0.40). Config may only tighten them.

- 10% writes `state/DRAWDOWN_FREEZE` and does not sell. The operator may acknowledge it. That acknowledgement does not reset the peak.
- 40% writes `state/KILL` with `ack_required` and sells the paper positions. This kill is human-only.

Clearing that kill takes a human `rhbot resume --ack`. That acknowledgement does not rebase sleeve peaks or the combined peak. The peak rises only on a new equity high. Automation may trip a kill and cannot clear it. The operator must not run `resume --ack` for a drawdown kill. A manual `rhbot kill` can be cleared with `rhbot resume` once health is otherwise fine. Resume still refuses while some other health check is critical. `ack-drawdown` does not clear a kill. Alerts stay in the status and heartbeat JSON. There is no email, SMS, or webhook.

## Ownership

`.github/CODEOWNERS` names the reviewer for `rhbot/risk.py`, the hard caps in `rhbot/config.py`, and `rhbot/brokers/`. Config may only tighten those caps.

## Real money

Real money requires Randy's written approval. It is out of scope. There is no live order code to turn on.

## Audit

Decisions, fills, kills, and resumes are appended to a hash-chained SQLite log. Updates and deletes on that log are rejected by the database. `rhbot audit verify` recomputes the chain. Do not hand-edit `state/bot.sqlite`.
