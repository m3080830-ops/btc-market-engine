# MEXC Futures integration — BTC-only live execution

## Scope and invariants

- Live execution is restricted to `BTC_USDT` on the MEXC **Futures** account.
- Other instruments remain PAPER/website statistics only.
- `strategy.py`, PAPER signal generation, M1 swing trailing and stop calculations are not changed by the live adapter.
- No AI/OpenAI calls are used by the engine.
- Live execution is enabled only when `MEXC_LIVE_TRADING_ENABLED=true` and both API credentials are present.
- API keys belong in the Deplexo service environment, never in source control or chat. Grant Futures read/trade permissions only; disable withdrawals.

## Startup preflight (read-only)

Before the executor is marked READY, it checks:

1. The Futures account is in Hedge Mode (`positionMode=1`). One-Way Mode is deliberately rejected.
2. Available USDT in the Futures wallet is positive.
3. BTC_USDT contract size, volume unit and minimum volume are valid.
4. The account fee information is readable.
5. Both LONG and SHORT Futures leverage and risk tiers are available.
6. Current BTC_USDT Futures positions can be read.

Preflight does not create, amend or cancel orders. An already-open position does not disable synchronization/protection; a new entry is separately blocked while a BTC position or unresolved live execution exists.

## Entry and risk

- A live order is considered only for the strategy's next-M15-candle entry, while that entry candle is current. Historical PAPER setups are not replayed as live orders after restart.
- The live adapter uses the MEXC Futures market-order payload builder, including numeric `side` and explicit `sideType` / `positionType` fields.
- 1R is 10% of available Futures USDT balance. Position quantity is derived from entry-to-initial-SL price risk and exchange contract/volume/leverage limits.
- The adapter does not silently substitute Spot balance or use ETH/SOL for live execution.
- After a submission timeout or ambiguous server/network failure, the execution is recorded as UNKNOWN and is not blindly resubmitted. The exchange position state must be reconciled first.

## Protection and synchronization

- The adapter reconciles BTC_USDT Futures positions and confirms/places the initial SL protection.
- Strategy-computed tighter SL values are passed to the adapter; the live adapter must not loosen an existing stop.
- Protection/synchronization failures are logged with safe API error detail and retried on subsequent refreshes. Do not treat an order acknowledgement alone as proof that a position is fully protected.

## Diagnostics

Logs use the `MEXC_LIVE` prefix. Relevant events include:

- `status=READY` — read-only preflight succeeded; this does not mean an order was placed.
- `ENTRY_SKIPPED` — entry was blocked, with a reason.
- `ENTRY_FAILED` — order request failed; `outcome=REJECTED` means a definite rejection, while `UNKNOWN` means the outcome must be reconciled.
- `ENTRY_ORDER_ACCEPTED` — MEXC accepted the order request; reconcile the resulting position and SL.
- `POSITION_SYNC_ERROR`, `SL_PROTECTION_RETRY`, `SL_UPDATE_FAILED` — synchronization/protection diagnostics.

Only the API error code and a short sanitized API message are logged. API keys, secret keys, signatures and request headers must never be logged.

## Deployment checklist

1. Review and merge the current BTC-only MEXC execution pull request.
2. Confirm Deplexo uses the repository's `main` branch and the new commit.
3. Keep `MEXC_LIVE_TRADING_ENABLED=true`; no new environment variable is required by this change.
4. Redeploy from Deplexo.
5. Confirm startup logs show `MEXC_LIVE status=READY` and inspect preflight values.
6. On the next real BTC setup, verify `ENTRY_ORDER_ACCEPTED`, an actual BTC_USDT Futures position, and `INITIAL_SL_CONFIRMED` before treating the live path as validated.

CI tests are offline and must never submit real orders. A successful syntax/import/unit-test run is not proof of live MEXC order permission or execution.
