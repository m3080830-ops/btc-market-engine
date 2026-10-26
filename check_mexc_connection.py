"""Read-only MEXC Futures API connectivity check. Never places orders."""
import json
import sys
from decimal import Decimal

from mexc_futures import MexcFuturesReadOnlyClient, MexcAPIError, MexcSafetyError

def main():
    try:
        client = MexcFuturesReadOnlyClient()
        rates = client.get_fee_rates("BTC_USDT")
        # Print only fee values; never print API keys or request headers.
        print(json.dumps({
            "connection": "OK",
            "endpoint": "MEXC Futures private fee-rate",
            "symbol": "BTC_USDT",
            "realMakerFee": str(rates["realMakerFee"]),
            "realTakerFee": str(rates["realTakerFee"]),
            "trading_enabled": False,
            "orders_placed": False,
        }, ensure_ascii=False))
        return 0
    except MexcSafetyError as exc:
        print(json.dumps({"connection":"NOT_CONFIRMED","error":str(exc),"orders_placed":False}))
        return 2
    except MexcAPIError as exc:
        print(json.dumps({"connection":"FAILED","error":str(exc),"orders_placed":False}))
        return 1
    except Exception as exc:
        print(json.dumps({"connection":"FAILED","error":type(exc).__name__,"orders_placed":False}))
        return 1

if __name__ == "__main__":
    sys.exit(main())
