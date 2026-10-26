# MEXC Futures integration — staged rollout

## Current phase: read-only only
This branch adds an isolated API foundation and pure sizing/fee helpers. It does **not** place, cancel, or amend orders and is not wired into the strategy/engine loop.

## Required safety sequence before any live order support
1. Put `MEXC_ACCESS_KEY` and `MEXC_SECRET_KEY` in Google Cloud Secret Manager / service environment. Never commit secrets or paste them into chat. Use API permissions only for reading and trading Futures; disable withdrawals; bind the key to the service's static egress IP where possible.
2. Query the account's real fee rate for BTC_USDT and ETH_USDT via `GET /api/v1/private/account/tiered_fee_rate/v2`. The app screenshot shows maker 0% and taker 0.02%, but MEXC's published API fee announcement states API orders use a separate fee schedule (published maker 0.06%, taker 0.08%). Treat 0.02% as a temporary assumption only; never use it for live sizing until the account-specific endpoint confirms it.
3. Query account balance, contract metadata (contract size, volume step, price tick, min/max volume, allowed leverage), open positions, open orders, and current protection state before order logic is implemented.
4. Keep strategy.py unchanged. Convert its existing entry/SL signal to an execution intent in a separate module. Validate symbol, direction, entry/SL, stop distance, stale signal age, volume precision, min/max order volume, available margin, risk cap, and fee-adjusted risk before submission.
5. Use a persistent SQLite execution ledger with unique client order IDs/idempotency keys and explicit states (PREPARED, SUBMITTING, ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, PROTECTION_PENDING, PROTECTED, CLOSING, CLOSED, ERROR). Never blindly retry a create-order request after a timeout; reconcile by client ID/order list/positions first to avoid duplicate entries.
6. After every order response or timeout, reconcile exchange positions and fills. Confirm the stop-loss exists and matches actual filled volume; if protection fails, halt new entries and run a defined emergency-close path only after explicit design/testing.
7. Manage partial fills, duplicate signals, restarts, rate limits, clock drift, network errors, API errors, exchange maintenance, liquidation/forced close, manual positions, funding, fees, and symbol/contract metadata changes.
8. Run unit tests, mocked API tests, replay/backtest comparisons, and read-only live validation first. Then enable order placement behind a separate explicit feature flag with a low-volume controlled pilot. The feature flag currently must remain false.

## Risk and accounting
The agreed working assumption is 1R risk budget = 10% of current Futures wallet balance. This is distinct from using 10% as margin; do not silently substitute one for the other. Position size must be derived from entry-to-SL loss plus estimated entry/exit fees, then rounded down to the exchange volume step. Reconcile actual fees and fills from exchange records and report gross PnL, commissions, funding, and net PnL separately.

The current `mexc_futures.py` helper is a foundation, not production-ready order execution. Validate contract units/size from official contract metadata before using its volume-sizing helper for a real contract.
