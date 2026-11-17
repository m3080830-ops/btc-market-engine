"""Account-scoped execution primitives.

This module is deliberately not wired into the live engine yet. It provides a
small, testable boundary for the staged multi-account migration. Credentials
are runtime-only values; this module does not persist, fetch, log, or transmit
them. Strategy and signal generation remain outside this module.
"""
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

SUPPORTED_SYMBOLS = frozenset({"BTC_USDT", "ETH_USDT"})


@dataclass(frozen=True, order=True)
class ExecutionScope:
    """Unique execution namespace for one account and one instrument."""

    account_id: str
    symbol: str

    def __post_init__(self):
        account_id = str(self.account_id).strip()
        symbol = str(self.symbol).strip().upper()
        if not account_id:
            raise ValueError("account_id is required")
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported execution symbol")
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "symbol", symbol)


@dataclass(frozen=True)
class AccountExecutionContext:
    """Credentials and identity for exactly one account/instrument executor.

    repr=False prevents accidental disclosure of API keys in routine object
    representations and test failure output. Callers must still never log keys.
    """

    account_id: str
    symbol: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self):
        scope = ExecutionScope(self.account_id, self.symbol)
        access_key = str(self.access_key).strip()
        secret_key = str(self.secret_key).strip()
        if not access_key or not secret_key:
            raise ValueError("account API credentials are required")
        object.__setattr__(self, "account_id", scope.account_id)
        object.__setattr__(self, "symbol", scope.symbol)
        object.__setattr__(self, "access_key", access_key)
        object.__setattr__(self, "secret_key", secret_key)

    @property
    def scope(self) -> ExecutionScope:
        return ExecutionScope(self.account_id, self.symbol)


class AccountExecutorRegistry:
    """Thread-safe exact-scope registry; it never falls back across accounts."""

    def __init__(self):
        self._executors: dict[ExecutionScope, Any] = {}
        self._lock = RLock()

    def register(self, scope: ExecutionScope, executor: Any) -> None:
        if not isinstance(scope, ExecutionScope):
            raise TypeError("scope must be an ExecutionScope")
        if executor is None:
            raise ValueError("executor is required")
        with self._lock:
            if scope in self._executors:
                raise ValueError("executor already registered for this account and symbol")
            self._executors[scope] = executor

    def get(self, scope: ExecutionScope) -> Any:
        if not isinstance(scope, ExecutionScope):
            raise TypeError("scope must be an ExecutionScope")
        with self._lock:
            try:
                return self._executors[scope]
            except KeyError:
                raise KeyError("no executor registered for this account and symbol") from None

    def unregister(self, scope: ExecutionScope) -> Any:
        if not isinstance(scope, ExecutionScope):
            raise TypeError("scope must be an ExecutionScope")
        with self._lock:
            try:
                return self._executors.pop(scope)
            except KeyError:
                raise KeyError("no executor registered for this account and symbol") from None

    def __len__(self) -> int:
        with self._lock:
            return len(self._executors)
