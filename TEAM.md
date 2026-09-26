# Operating team

The bot is operated by an AI team lead, **Coin Ceo Bot**, and five agents that report to it. The team watches, reports, and stops the paper book. It does not decide trades. Strategies are fixed functions. Every simulated order is checked by the risk engine in code.

Real money requires Randy's written approval and is out of scope. No role can grant that approval.

## What nobody may do

- Read, copy, or commit `RH_API_KEY` or `RH_PRIVATE_KEY_BASE64`.
- Place, amend, or cancel a real order, or ask anyone to. There is no code path for it.
- Use a browser session on Robinhood.
- Edit `rhbot/risk.py`, the hard caps in `rhbot/config.py`, or `rhbot/strategies/` without an approved change. A losing stretch is not a reason to retune.
- Turn on live trading. `mode` cannot be set to anything but `paper`.

## Coin Ceo Bot (team lead)

Coordinates the five agents. Reads status and the daily report. Escalates a stuck critical health check to Randy. May tell Operations to kill the bot. Does not calculate position size, does not hold keys, and does not approve real money.

## Research

Owns the strategy specs: what the daily trend filter, weekly DCA, and buy-and-hold benchmark are supposed to do, including the parameters that were chosen up front (20-day average, 1% band, $25 weekly buy). The minimum hold cannot be shorter than the 7-day risk floor.

Uses `rhbot report` and this file's sibling `ARCHITECTURE.md`. May propose a parameter change as a reviewed pull request. May not edit strategy or risk code, and may not pick a new window because one backtest looked best.

## Engineering

Reviews pull requests and writes change requests. May change code only through review. A change to `rhbot/risk.py`, the hard caps in `rhbot/config.py`, or `rhbot/brokers/` needs Risk's review. Those paths are listed in `.github/CODEOWNERS`. May not self-approve those changes, may not enable a live broker, and may not weaken the test that rejects order endpoints.

## Risk

Reviews limits. May tighten them in `config.yaml` (smaller position size, tighter drawdown, fewer trades, fresher-data requirement). The program rejects a config that loosens a hard cap.

May not loosen a limit, hold credentials, resume the bot while health is critical for a reason other than the kill switch, or "fix" a drawdown by raising the cap.

Drawdown is measured on the combined portfolio peak. These limits are paper-only and must not be carried into a live phase. At 10% the engine writes `state/DRAWDOWN_FREEZE` and blocks new buys until a person runs `rhbot ack-drawdown`. Sells stay allowed and nothing is force-sold. At 40% it writes `state/KILL` and flattens the paper book. That is code, not a judgment call. Clearing that kill takes `rhbot resume --ack` from a person. Automation may trip the kill and cannot clear it. A 4% loss on the UTC day blocks new buys until the next UTC day.

## Operations

Watches the process and is the only role that routinely restarts it.

| Action | Command or file |
| --- | --- |
| Is it up, and did it do something recently? | `rhbot status`, `rhbot health` |
| Stop new simulated orders | `rhbot kill --reason "..."` which creates `state/KILL` |
| Allow orders again after health is clear | `rhbot resume` |
| Acknowledge a 10% paper drawdown so new buys can resume | `rhbot ack-drawdown` |
| Allow orders again after a 40% paper drawdown kill | `rhbot resume --ack` |
| Preflight, including the kill path | `rhbot selftest` |
| Sell the paper book | `rhbot flatten --paper` (kill first if it should stay sold) |
| Process actually looping | `state/heartbeat.json` |
| Restart | the process supervisor (`deploy/rhbot.service` is a starting point) |

Suggested rhythm: health every 15 minutes; if health is critical twice in a row, restart once; if it is still critical after that, kill and escalate to Coin Ceo Bot and Randy. Do not restart in a loop.

May not edit strategy or risk code, read keys, or resume over a broken ledger.

## Reporting

Writes the P&L story. Uses `rhbot report --since 24h` (also `7d` and `30d`) and `rhbot audit verify`. Daily note to Randy is the 24h report in plain language. Weekly note adds the 7-day report, selftest (Operations runs it), and the audit check.

May not change positions, edit code, or treat the paper ledger as a live brokerage statement. P&L is net of the configured per-side cost.
