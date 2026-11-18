# Historical MEXC Futures research branch

This isolated branch does not alter production strategy, live adapter, deployment configuration, or production database.

Run Actions → MEXC historical backtest → Run workflow. It downloads public MEXC contract candles for BTC_USDT and ETH_USDT on Min1, Min15, Min60, Hour4 and Day1. M1 covers the selected test period; higher timeframes include warm-up data from 2026-06-01. The end timestamp is exclusive. Pages contain up to 2,000 candles. CSV files and a manifest are validated and uploaded as a 30-day artifact.

Important: this initial workflow downloads and validates source data only. It does not claim performance metrics. A trustworthy simulator must replay the exact historical signal/entry/SL and M1 trailing logic without future leakage, use conservative same-candle stop handling, and model fees/slippage explicitly. Public OHLC candles are not the production SQLite trade history and cannot reproduce actual exchange fills.

No API keys, credentials, order endpoints, live trading, or AI calls are used.
