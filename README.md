# rhbot

A paper-only bot for BTC and ETH. It pretends to trade so you can see whether a slow rule beats just holding, after a realistic cost. It cannot place, change, or cancel a real Robinhood order. There is no code path that does that.

You do not need an API key. Marks, fills, and the spread cap use Coinbase's public bid/ask. Kraken is diagnostic only. Every simulated buy and sell pays 1% by default, which is about what a small Robinhood crypto account pays per side. Each strategy has its own $1,000 paper account, so they can be compared on the same footing.

## What it trades

Only `BTC-USD` and `ETH-USD`. Three sleeves:

| Sleeve | What it does |
| --- | --- |
| Buy and hold | Splits the cash between BTC and ETH once, then sits there. This is the benchmark. |
| Weekly DCA | Buys one coin every 7 days, BTC then ETH, at $19.23. |
| Daily trend | Once a day, holds a coin when its last closed daily price is at least 2% above the 200-day average. Otherwise it goes to cash. It waits 7 days before selling, and it can buy again the next day. |

The 200-day average and the 2% band were picked before any backtest. The 7-day hold is the risk floor. They are not the winners of a search. Change them only in a reviewed pull request, not because one week looked good.

## Install

Python 3.12.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Check that the safety paths work. This does not touch the network and does not arm a kill switch on your real state directory:

```bash
rhbot selftest
```

Run the tests:

```bash
python -m pytest
```

## Run

```bash
rhbot run --state-dir state
```

That loop wakes every 60 seconds, pulls public prices, lets each sleeve decide, runs the risk checks, and writes `state/heartbeat.json`. One JSON line per cycle goes to stdout. Stop it with Ctrl-C. A systemd unit you can adapt is in `deploy/rhbot.service`.

Start paper with an empty `state/` directory. An existing ledger keeps its all-time peak, paper day 1, and fills.

The live loop checks drawdown on every cycle. `rhbot audit replay` checks that same rule once a day, at the closed daily price. A missing Coinbase quote stops the whole cycle before any sleeve trades.

Copy `config.example.yaml` to `config.yaml` if you want different limits. Decimals in that file must be quoted strings. You can lower a risk limit. You cannot raise it past the hard cap in code.

State lives in the directory you pass (default `./state`):

| File | What it is |
| --- | --- |
| `bot.sqlite` | Cash, positions, fills, and the audit log |
| `heartbeat.json` | Proof the loop finished recently |
| `KILL` | When this file exists, strategies cannot open new risk. Flatten can still sell |

## Commands

Status, health, report, kill, ack-drawdown, resume, flatten, selftest, and audit print JSON. `dashboard` prints HTML and `export` prints CSV. Pass `--out` to write a file instead. Exit codes are 0 (ok or not started yet), 1 (degraded), and 2 (critical, or the command was refused).

```bash
rhbot status --state-dir state
rhbot health --state-dir state
rhbot report --since 24h --state-dir state
rhbot report --since 7d --md --state-dir state
rhbot dashboard --since 7d --out dashboard.html --state-dir state
rhbot export trades --out trades.csv --state-dir state
rhbot export equity --out equity.csv --state-dir state
rhbot kill --reason "quotes look wrong" --state-dir state
rhbot ack-drawdown --strategy trend_daily --by operator --note "reviewed the paper drawdown" --state-dir state
rhbot resume --ack --human-code "$CODE" --state-dir state
rhbot flatten --paper --state-dir state
rhbot selftest
rhbot audit verify --state-dir state
rhbot audit replay --since 7d --state-dir state
```

`$CODE` is the secret in `RHBOT_HUMAN_RESUME_FILE`. Resume without `--ack` and that `--human-code` does not clear a kill.

`status` and `health` answer "did it actually do something recently?": last successful cycle, the quote age from the last cycle, and error counts. A quote that was fresh during the cycle stays acceptable until the next loop window. The risk engine still rejects a quote older than 30 seconds at order time.

`report` is P&L after costs for each sleeve, next to buy-and-hold.

`dashboard`, `export trades`, and `export equity` read the paper ledger only. They do not start the bot, fetch prices, or change bot state. No API key. The HTML page has three separate sections: Saved results (recorded equity, P&L, and trades), Stale health (heartbeat or quotes are old; the numbers are still the last saved marks), and Incomplete data (no ledger yet, a book has no equity snapshots, reconcile failed, or a kill flatten did not finish). Returns and the buy-and-hold benchmark on the page are percent, the same percent `rhbot report` already prints. They are not dollars. A −1% cost is −1%, not −100% and not −$1. The equity CSV column `drawdown_fraction` keeps the stored fraction (0.10 means 10% off the peak), not a percent. CSV timestamps stay ISO. The page clock is `YYYY-MM-DD HH:MM:SS UTC`. `rhbot export trades` is simulated fills. `rhbot export equity` is equity snapshots. Omit `--out` to print to stdout. `--since` on dashboard matches report (`24h`, `7d`, `30d`).

`resume` will not clear the kill file if something else is critically wrong (a broken ledger, for example). Reported drawdown is mark-to-bid equity against that book's all-time peak. The peak only rises. It applies to the trend and DCA books only, and these limits are paper-only: they must not be carried into a live phase. A 10% drop freezes that book's new buys until the operator runs `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. That acknowledgement does not move the peak or the restart baseline. Buys are allowed again while drawdown is still at or below −10%, and the book re-arms only after drawdown recovers above −10%. Sells stay allowed and nothing is force-sold. A 40% drop flattens that book and requires `rhbot resume --ack --human-code <code>`, matching `RHBOT_HUMAN_RESUME_FILE`. That human restart does not move the all-time peak. It records the book's mark-to-bid equity as `restart_baseline`. After that, the 10% pause and the 40% shutoff use the greater of that baseline and the highest equity since the restart. A new all-time high makes that reference the peak again. Nothing restarts the book on its own. The operator may trip that kill and must not clear it. `ack-drawdown` does not clear a kill. The hard caps are `freeze_drawdown_pct` (10%) and `kill_drawdown_pct` (40%).

`report` includes `trend_daily_shadow` and `dca_weekly_shadow` (the same strategies without the freeze or the kill) and `overlay_impact`. Buy-and-hold is the benchmark and has no overlay. `status` and `report` list each book's state, drawdown, peak, and acknowledgement.

`flatten --paper` sells what the sleeves hold. The strategies will try to buy back on a later cycle unless the kill file is still in place. To stop the book: kill, then flatten.

Who is allowed to run which command is in `TEAM.md`. How the pieces fit is in `ARCHITECTURE.md`.

## Quotes

v1 paper uses Coinbase public bid/ask. Set `market_data: public` and `public_provider: coinbase`. A missing Coinbase quote fails the whole cycle closed: health goes critical (`quote_hard_stop`) and that cycle fills nothing. A quote 30 seconds old or older, or one with no bid or ask, denies new orders and sets the same hard stop. The bot does not fall back to Robinhood or Kraken. A reduce_only kill or flatten sell can still use the last valid bid/ask.

Kraken parsers are diagnostic only. They do not price marks, fills, or the spread cap.

## Tests

The suite is offline. HTTP is mocked or skipped. It covers fees and fills, each strategy, position and exposure caps, the kill switch, drawdown, the audit hash chain, and a scan that fails if any bot code names a Robinhood order endpoint or issues an HTTP POST, PUT, PATCH, or DELETE. GitHub Actions runs `pytest` on push and on pull requests.

## Real money

Out of scope. Real money requires Randy's written approval. This repository does not contain a way to send an order.
