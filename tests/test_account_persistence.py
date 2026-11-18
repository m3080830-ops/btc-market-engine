import os
import tempfile
import unittest

from account_execution import ExecutionScope
from account_persistence import AccountRepositoryFactory, SQLiteAccountRepository


class AccountPersistenceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.factory = AccountRepositoryFactory(self.temp.name)

    def test_same_symbol_gets_different_database_for_each_account(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        b = self.factory.for_scope(ExecutionScope("client-b", "BTC_USDT"))
        self.assertNotEqual(a.db_path, b.db_path)

    def test_account_state_is_isolated(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        b = self.factory.for_scope(ExecutionScope("client-b", "BTC_USDT"))
        a.set_state("trailing.last_r", 1.7)
        self.assertEqual(a.get_state("trailing.last_r"), 1.7)
        self.assertIsNone(b.get_state("trailing.last_r"))

    def test_trades_positions_orders_stops_and_history_are_isolated(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "ETH_USDT"))
        b = self.factory.for_scope(ExecutionScope("client-b", "ETH_USDT"))
        for kind in ("trade", "position", "order", "stop", "audit"):
            a.put_record(kind, "same-exchange-id", {"owner": "client-a", "kind": kind})
        for kind in ("trade", "position", "order", "stop", "audit"):
            self.assertEqual(a.get_record(kind, "same-exchange-id")["owner"], "client-a")
            self.assertIsNone(b.get_record(kind, "same-exchange-id"))

    def test_same_record_id_is_valid_for_different_accounts(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        b = self.factory.for_scope(ExecutionScope("client-b", "BTC_USDT"))
        a.put_record("order", "exchange-123", {"volume": 1})
        b.put_record("order", "exchange-123", {"volume": 9})
        self.assertEqual(a.get_record("order", "exchange-123")["volume"], 1)
        self.assertEqual(b.get_record("order", "exchange-123")["volume"], 9)

    def test_database_file_cannot_be_rebound_to_another_scope(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        with self.assertRaisesRegex(ValueError, "different execution scope"):
            SQLiteAccountRepository(
                a.db_path, ExecutionScope("client-b", "BTC_USDT")
            )

    def test_replayed_event_id_is_ignored(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        self.assertTrue(a.append_event("signal-42", "ORDER_SUBMITTED", {"order": 1}))
        self.assertFalse(a.append_event("signal-42", "ORDER_SUBMITTED", {"order": 2}))
        self.assertEqual(len(a.list_events()), 1)
        self.assertEqual(a.list_events()[0]["payload"]["order"], 1)

    def test_credentials_cannot_be_persisted(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        with self.assertRaisesRegex(ValueError, "credential-like"):
            a.put_record("audit", "bad", {"nested": {"secret_key": "do-not-store"}})
        with self.assertRaisesRegex(ValueError, "credential-like"):
            a.set_state("bad", {"ApiKey": "do-not-store"})

    def test_record_type_and_size_are_validated(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        with self.assertRaises(ValueError):
            a.put_record("credentials", "x", {})
        with self.assertRaises(ValueError):
            a.list_events(1001)

    def test_database_permissions_are_restricted_on_posix(self):
        a = self.factory.for_scope(ExecutionScope("client-a", "BTC_USDT"))
        if os.name == "posix":
            self.assertEqual(os.stat(a.db_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
