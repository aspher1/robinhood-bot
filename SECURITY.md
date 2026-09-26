# Security

This bot is paper-only. It does not place, amend, or cancel real orders.

## Secrets

- Do not commit API keys, private keys, or `.env` files.
- A read-only Robinhood quote key, if you create one later, lives only in environment variables: `RH_API_KEY` and `RH_PRIVATE_KEY_BASE64`.
- The process never reads those values from the repository.
- Create that key with read-only actions. This code still cannot send an order with it: the quote client signs GET requests for best bid/ask only, and any other path or method is refused.
- Operators and AI agents do not read, print, or copy those variables.

## Kill switch

`state/KILL` stops new simulated risk. If the file is missing, unreadable, or not valid JSON, treat a present file as on. Deleting it is `rhbot resume`, and resume refuses while some other health check is critical.

## Real money

Real money requires Randy's written approval. It is out of scope. There is no live order code to turn on.

## Audit

Decisions, fills, kills, and resumes are appended to a hash-chained SQLite log. Updates and deletes on that log are rejected by the database. `rhbot audit verify` recomputes the chain. Do not hand-edit `state/bot.sqlite`.
