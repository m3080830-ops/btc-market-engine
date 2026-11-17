import contextlib
import io
import os
import unittest
from unittest import mock

from account_execution import AccountExecutionContext
from mexc_futures_live import MexcFuturesLive, MexcLiveError


class MexcAccountCredentialTests(unittest.TestCase):
    def make_adapter(self, *args, **kwargs):
        with mock.patch.object(MexcFuturesLive, "_init_db"):
            return MexcFuturesLive(":memory:", *args, **kwargs)

    def test_explicit_context_injects_only_its_account_credentials(self):
        context_a = AccountExecutionContext("account-a", "BTC_USDT", "key-a", "secret-a")
        context_b = AccountExecutionContext("account-b", "BTC_USDT", "key-b", "secret-b")
        adapter_a = self.make_adapter(account_context=context_a, enabled=True)
        adapter_b = self.make_adapter(account_context=context_b, enabled=True)
        self.assertEqual((adapter_a.access_key, adapter_a.secret_key), ("key-a", "secret-a"))
        self.assertEqual((adapter_b.access_key, adapter_b.secret_key), ("key-b", "secret-b"))
        self.assertTrue(adapter_a.enabled)
        self.assertTrue(adapter_b.enabled)

    def test_account_context_does_not_enable_trading_unless_explicitly_enabled(self):
        context = AccountExecutionContext("account-a", "BTC_USDT", "key-a", "secret-a")
        adapter = self.make_adapter(account_context=context)
        self.assertFalse(adapter.enabled)
        self.assertEqual(adapter.access_key, "key-a")

    def test_context_symbol_must_match_adapter_symbol(self):
        context = AccountExecutionContext("account-a", "ETH_USDT", "key-a", "secret-a")
        with self.assertRaisesRegex(MexcLiveError, "account execution context"):
            self.make_adapter("BTC_USDT", account_context=context, enabled=True)

    def test_explicit_enabled_flag_requires_context(self):
        with self.assertRaisesRegex(MexcLiveError, "requires an account"):
            self.make_adapter(enabled=True)

    def test_legacy_environment_credentials_and_enable_flag_are_preserved(self):
        env = {
            "MEXC_ACCESS_KEY": "legacy-key",
            "MEXC_SECRET_KEY": "legacy-secret",
            "MEXC_LIVE_TRADING_ENABLED": "true",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = self.make_adapter()
        self.assertEqual(adapter.access_key, "legacy-key")
        self.assertEqual(adapter.secret_key, "legacy-secret")
        self.assertTrue(adapter.enabled)

    def test_explicit_context_never_falls_back_to_environment_credentials(self):
        env = {
            "MEXC_ACCESS_KEY": "legacy-key",
            "MEXC_SECRET_KEY": "legacy-secret",
            "MEXC_LIVE_TRADING_ENABLED": "true",
        }
        context = AccountExecutionContext("account-a", "BTC_USDT", "account-key", "account-secret")
        with mock.patch.dict(os.environ, env, clear=True):
            adapter = self.make_adapter(account_context=context, enabled=True)
        self.assertEqual(adapter.access_key, "account-key")
        self.assertEqual(adapter.secret_key, "account-secret")

    def test_adapter_initialization_does_not_print_credentials(self):
        context = AccountExecutionContext(
            "account-a", "BTC_USDT", "private-key-marker", "private-secret-marker"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.make_adapter(account_context=context, enabled=True)
        self.assertNotIn("private-key-marker", output.getvalue())
        self.assertNotIn("private-secret-marker", output.getvalue())


if __name__ == "__main__":
    unittest.main()
