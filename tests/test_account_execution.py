import unittest

from account_execution import (
    AccountExecutionContext,
    AccountExecutorRegistry,
    ExecutionScope,
)


class ExecutionScopeTests(unittest.TestCase):
    def test_scope_normalizes_symbol_and_requires_account(self):
        self.assertEqual(
            ExecutionScope(" customer-1 ", "btc_usdt"),
            ExecutionScope("customer-1", "BTC_USDT"),
        )
        with self.assertRaisesRegex(ValueError, "account_id"):
            ExecutionScope(" ", "BTC_USDT")

    def test_scope_rejects_unsupported_symbol(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ExecutionScope("customer-1", "SOL_USDT")

    def test_context_requires_both_account_credentials(self):
        with self.assertRaisesRegex(ValueError, "credentials"):
            AccountExecutionContext("customer-1", "BTC_USDT", "", "secret")

    def test_context_repr_does_not_disclose_credentials(self):
        context = AccountExecutionContext(
            "customer-1", "BTC_USDT", "public-api-key", "private-secret"
        )
        rendered = repr(context)
        self.assertNotIn("public-api-key", rendered)
        self.assertNotIn("private-secret", rendered)
        self.assertEqual(context.scope, ExecutionScope("customer-1", "BTC_USDT"))

    def test_registry_keeps_accounts_and_symbols_isolated(self):
        registry = AccountExecutorRegistry()
        account_a_btc = ExecutionScope("account-a", "BTC_USDT")
        account_b_btc = ExecutionScope("account-b", "BTC_USDT")
        account_a_eth = ExecutionScope("account-a", "ETH_USDT")
        btc_a, btc_b, eth_a = object(), object(), object()

        registry.register(account_a_btc, btc_a)
        registry.register(account_b_btc, btc_b)
        registry.register(account_a_eth, eth_a)

        self.assertIs(registry.get(account_a_btc), btc_a)
        self.assertIs(registry.get(account_b_btc), btc_b)
        self.assertIs(registry.get(account_a_eth), eth_a)
        self.assertEqual(len(registry), 3)

    def test_registry_has_no_cross_account_fallback(self):
        registry = AccountExecutorRegistry()
        registry.register(ExecutionScope("account-a", "BTC_USDT"), object())
        with self.assertRaisesRegex(KeyError, "no executor"):
            registry.get(ExecutionScope("account-b", "BTC_USDT"))

    def test_registry_rejects_duplicate_scope(self):
        registry = AccountExecutorRegistry()
        scope = ExecutionScope("account-a", "BTC_USDT")
        registry.register(scope, object())
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(scope, object())

    def test_unregister_only_removes_the_exact_account_scope(self):
        registry = AccountExecutorRegistry()
        scope_a = ExecutionScope("account-a", "BTC_USDT")
        scope_b = ExecutionScope("account-b", "BTC_USDT")
        executor_a, executor_b = object(), object()
        registry.register(scope_a, executor_a)
        registry.register(scope_b, executor_b)

        self.assertIs(registry.unregister(scope_a), executor_a)
        self.assertIs(registry.get(scope_b), executor_b)
        self.assertEqual(len(registry), 1)


if __name__ == "__main__":
    unittest.main()
