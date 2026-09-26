# BTC & ETH Paper Trading Bot

This is a practice bot for Bitcoin and Ethereum. It watches prices, makes **simulated** buys and sells with virtual cash, and shows how three fixed approaches compare after estimated trading costs. You can inspect its decisions and results without risking money. It cannot place, change, or cancel a real order.

Each approach starts with its own virtual $1,000. The bot uses public Coinbase bid and ask prices to value positions and simulate fills, so no account or API key is needed. Kraken is diagnostic only and is not a fallback for missing Coinbase quotes. By default, the bot checks prices every 60 seconds, applies the fixed rules and risk checks, records pretend fills, and produces status and performance reports. Simulated trades include at least a 1% cost per side. Results are an experiment, not a promise of profit.

You can also save a visual dashboard to open in any browser, or export the recorded trades and balance history to a spreadsheet. Both work from saved paper records and require no network connection.

## The three approaches

The bot only simulates spot trades in `BTC-USD` and `ETH-USD`. Each approach has a separate paper portfolio:

| Approach | In plain language |
| --- | --- |
| Buy and hold | Splits the virtual cash between BTC and ETH, then holds them. This is the comparison baseline. |
| Weekly DCA | Makes one simulated $19.23 purchase every 7 days, alternating BTC and ETH. |
| Daily trend | Checks yesterday's closing price against a 200-day average. It buys when the close is more than 2% above the average and sells when it is more than 2% below, after a minimum 7-day hold. Between those levels, it keeps its current position. |

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

## See the results in your browser

After the bot has recorded some paper cycles, save a dashboard:

```bash
rhbot dashboard --since 7d --state-dir state --output reports/paper-7d.html
```

Open `reports/paper-7d.html` in your browser. It shows each strategy's recorded equity, cash, return after costs, positions, equity history, recent simulated trades, and decision activity. It also shows health, quote age, and whether a strategy is frozen or killed. The comparison books show what the same strategies recorded without the drawdown overlay.

This is a saved snapshot. Generate a new file to see newer results. Health and balances reflect the latest saved state; `--since` selects the performance and history window. The bot can continue recording during generation, so the sections are read separately rather than captured as one database transaction. Charts show up to 360 actual observations per strategy, including the last observation before the window when available. Sampling is labeled and can omit intermediate highs and lows. The recent-fill and activity tables show up to 50 records each. No browser scripts, hosted service, or external assets are needed.

Before the first cycle, the dashboard shows an empty state. Generating it does not start the bot or create a trading ledger.

## Export to a spreadsheet

```bash
rhbot export fills --since 30d --state-dir state --output reports/fills-30d.csv
rhbot export equity --since 30d --include-shadow --state-dir state --output reports/equity-30d.csv
```

`fills` includes the recorded side, quantity, price, trading cost, cash change, reason, and order identifier. `equity` includes saved cash and portfolio values over time. Decimal values retain their stored precision. The `book` column distinguishes `paper` from the optional `shadow` comparison books. Shadow equity has a blank drawdown column because the shadow table does not store that value. Formula-like text is prefixed with an apostrophe so spreadsheet software treats it as text.

Exports include records within the requested UTC window through generation time. Both commands print a JSON receipt with the output path; CSV exports also report the row count. They create missing output folders, refuse to overwrite an existing file, and require output outside the bot's state directory. Choose a new filename for each snapshot.

## Commands

All of these print JSON. Exit codes are 0 (ok or not started yet), 1 (degraded), and 2 (critical, or the command was refused).

```bash
rhbot status --state-dir state
rhbot health --state-dir state
rhbot report --since 24h --state-dir state
rhbot report --since 7d --md --state-dir state
rhbot kill --reason "quotes look wrong" --state-dir state
rhbot ack-drawdown --strategy trend_daily --by operator --note "reviewed the paper drawdown" --state-dir state
rhbot resume --ack --human-code "$CODE" --state-dir state
rhbot flatten --paper --state-dir state
rhbot selftest
rhbot audit verify --state-dir state
rhbot audit replay --since 7d --state-dir state
```

Before a kill needs clearing, Randy should create a private, nonempty text file outside the repository containing the human resume code. Set `RHBOT_HUMAN_RESUME_FILE` to that file's path in the environment of the `rhbot resume` command; `$CODE` in the example must match its contents. Keep the file and code out of git and bot automation. Only Randy should run resume. Clearing either a manual `state/KILL` or a 40% per-book kill requires `--ack` and `--human-code`. The command refuses to resume if another critical health problem remains.

`status` and `health` answer "did it actually do something recently?": last successful cycle, the quote age from the last cycle, and error counts. A quote that was fresh during the cycle stays acceptable until the next loop window. The risk engine still rejects a quote older than 30 seconds at order time.

`report` is P&L after costs for each sleeve, next to buy-and-hold.

`resume` will not clear the kill file if something else is critically wrong (a broken ledger, for example). Reported drawdown is mark-to-bid equity against that book's all-time peak. The peak only rises. It applies to the trend and DCA books only, and these limits are paper-only: they must not be carried into a live phase. A 10% drop freezes that book's new buys until the operator runs `rhbot ack-drawdown --strategy <name> --by operator|randy --note "..."`. That acknowledgement does not move the peak or the restart baseline. Buys are allowed again while drawdown is still at or below −10%, and the book re-arms only after drawdown recovers above −10%. Sells stay allowed and nothing is force-sold. A 40% drop flattens that book and requires `rhbot resume --ack --human-code <code>`, matching `RHBOT_HUMAN_RESUME_FILE`. That human restart does not move the all-time peak. It records the book's mark-to-bid equity as `restart_baseline`. After that, the 10% pause and the 40% shutoff use the greater of that baseline and the highest equity since the restart. A new all-time high makes that reference the peak again. Nothing restarts the book on its own. The operator may trip that kill and must not clear it. `ack-drawdown` does not clear a kill. The hard caps are `freeze_drawdown_pct` (10%) and `kill_drawdown_pct` (40%).

`report` includes `trend_daily_shadow` and `dca_weekly_shadow` (the same strategies without the freeze or the kill) and `overlay_impact`. Buy-and-hold is the benchmark and has no overlay. `status` and `report` list each book's state, drawdown, peak, and acknowledgement.

`flatten --paper` sells what the sleeves hold. The strategies will try to buy back on a later cycle unless the kill file is still in place. To stop the book: kill, then flatten.

Who is allowed to run which command is in `TEAM.md`. How the pieces fit is in `ARCHITECTURE.md`.

## Quotes

v1 paper uses Coinbase public bid/ask. Set `market_data: public` and `public_provider: coinbase`. A missing Coinbase quote fails the whole cycle closed: health goes critical (`quote_hard_stop`) and that cycle fills nothing. A quote 30 seconds old or older, or one with no bid or ask, denies new orders and sets the same hard stop. The bot does not fall back to Robinhood or Kraken. A reduce_only kill or flatten sell can still use the last valid bid/ask.

Kraken parsers are diagnostic only. They do not price marks, fills, or the spread cap.

## Tests

The tests use offline snapshots or mocked HTTP. The suite covers fees and fills, each strategy, position and exposure caps, the kill switch, drawdown, the audit hash chain, and a scan that fails if any bot code names a Robinhood order endpoint or issues an HTTP POST, PUT, PATCH, or DELETE. GitHub Actions runs `pytest` on push and on pull requests.

## Real money

Out of scope. Real money requires Randy's written approval. This repository does not contain a way to send an order.
