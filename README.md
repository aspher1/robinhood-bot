# rhbot

A paper-only bot for BTC and ETH. It pretends to trade so you can see whether a slow rule beats just holding, after a realistic cost. It cannot place, change, or cancel a real Robinhood order. There is no code path that does that.

You do not need an API key. Prices come from Coinbase's public market data (Kraken is the other built-in source). Every simulated buy and sell pays 1% by default, which is about what a small Robinhood crypto account pays per side. Each strategy has its own $1,000 paper account, so they can be compared on the same footing.

## What it trades

Only `BTC-USD` and `ETH-USD`. Three sleeves:

| Sleeve | What it does |
| --- | --- |
| Buy and hold | Splits the cash between BTC and ETH once, then sits there. This is the benchmark. |
| Weekly DCA | Buys $25 of BTC and $25 of ETH once a week. |
| Daily trend | Once a day, holds a coin when its last closed daily price is at least 1% above the 20-day average. Otherwise it goes to cash. It waits 7 days before flipping again. |

The 20-day average and the 1% band were picked before any backtest. The 7-day wait is the risk floor. They are not the winners of a search. Change them only in a reviewed pull request, not because one week looked good.

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

Copy `config.example.yaml` to `config.yaml` if you want different limits. Decimals in that file must be quoted strings. You can lower a risk limit. You cannot raise it past the hard cap in code.

State lives in the directory you pass (default `./state`):

| File | What it is |
| --- | --- |
| `bot.sqlite` | Cash, positions, fills, and the audit log |
| `heartbeat.json` | Proof the loop finished recently |
| `KILL` | When this file exists, strategies cannot open or close risk |

## Commands

All of these print JSON. Exit codes are 0 (ok or not started yet), 1 (degraded), and 2 (critical, or the command was refused).

```bash
rhbot status --state-dir state
rhbot health --state-dir state
rhbot report --since 24h --state-dir state
rhbot report --since 7d --md --state-dir state
rhbot kill --reason "quotes look wrong" --state-dir state
rhbot resume --state-dir state
rhbot resume --ack --state-dir state
rhbot flatten --paper --state-dir state
rhbot selftest
rhbot audit verify --state-dir state
```

`status` and `health` answer "did it actually do something recently?": last successful cycle, age of the last quote, and error counts. A fresh heartbeat with a stale quote is not healthy.

`report` is P&L after costs for each sleeve, next to buy-and-hold.

`resume` will not clear the kill file if something else is critically wrong (a broken ledger, for example). A 10% drawdown kill also requires `--ack`. The bot never clears that kill by itself.

`flatten --paper` sells what the sleeves hold. The strategies will try to buy back on a later cycle unless the kill file is still in place. To stop the book: kill, then flatten.

Who is allowed to run which command is in `TEAM.md`. How the pieces fit is in `ARCHITECTURE.md`.

## Optional Robinhood quotes

Leave this off for v1. Public prices plus the 1% cost are enough.

If you later want Robinhood's own bid and ask, create a **read-only** key in Robinhood (web classic, crypto API settings) and export it only in the environment, never in the repo:

```bash
export RH_API_KEY='rh-api-...'
export RH_PRIVATE_KEY_BASE64='base64 of the 32-byte Ed25519 seed'
```

Then set `market_data: robinhood` in `config.yaml`. The bot will GET best bid/ask, sign the request, and stay under 100 calls a minute. Buys fill at the ask and sells at the bid, and the price is widened if that spread is tighter than the 1% floor. Daily candles still come from the public source, because Robinhood does not publish them. If those variables are unset, the bot keeps using public prices.

The quote client cannot place an order. A key that was created with trade permission still cannot trade through this program. Create it read-only anyway.

## Tests

The suite is offline. HTTP is mocked or skipped. It covers fees and fills, each strategy, position and exposure caps, the kill switch, drawdown, the audit hash chain, and a scan that fails if any bot code names a Robinhood order endpoint or issues an HTTP POST, PUT, PATCH, or DELETE. GitHub Actions runs `pytest` on push and on pull requests.

## Real money

Out of scope. Real money requires Randy's written approval. This repository does not contain a way to send an order.
