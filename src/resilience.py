"""Fault-tolerance primitives: timeouts, exponential backoff w/ jitter, and circuit breaker."""

from __future__ import annotations

import asyncio
import functools
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

import httpx

DB_TIMEOUT_SECONDS: float = 2.0
REST_API_TIMEOUT_SECONDS: float = 3.0

T = TypeVar("T")


class CircuitState(str, Enum):
    """Finite state machine states for the circuit breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class UpstreamTimeoutError(TimeoutError):
    """Raised when a connector call exceeds its configured deadline."""


class TransientUpstreamError(RuntimeError):
    """Raised on retryable upstream errors (HTTP 429, HTTP 503, network glitches)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CircuitBreakerOpenError(RuntimeError):
    """Raised immediately when the circuit breaker is OPEN and failing fast."""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        self.name = name
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"Circuit breaker '{name}' is OPEN; retry after {self.retry_after_seconds:.2f}s."
        )


class CircuitBreaker:
    """Async-safe circuit breaker tripping after consecutive failures."""

    def __init__(
        self,
        name: str = "itsm_api",
        failure_threshold: int = 3,
        recovery_timeout: float = 15.0,
        time_fn: Callable[[], float] = time.monotonic,
        is_failure: Callable[[BaseException], bool] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._time_fn = time_fn
        # Predicate deciding which exceptions count against upstream health. Client-side
        # errors (e.g. HTTP 4xx from a malformed request) should not trip the breaker.
        self._is_failure = is_failure or (lambda exc: True)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False
        self._lock = asyncio.Lock()

    def _sync_state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            elapsed = self._time_fn() - self._opened_at
            if elapsed >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = False
        return self._state

    @property
    def state(self) -> CircuitState:
        """Current circuit breaker state after evaluating recovery window."""
        return self._sync_state()

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time view of breaker health for dashboards and status endpoints."""
        state = self._sync_state()
        retry_after = 0.0
        if state == CircuitState.OPEN and self._opened_at is not None:
            retry_after = max(0.0, self.recovery_timeout - (self._time_fn() - self._opened_at))
        return {
            "name": self.name,
            "state": state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_seconds": self.recovery_timeout,
            "retry_after_seconds": round(retry_after, 2),
        }

    def reset(self) -> None:
        """Reset breaker back to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        self._half_open_in_flight = False

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute an async callable protected by the circuit breaker."""
        async with self._lock:
            current = self._sync_state()
            if current == CircuitState.OPEN:
                elapsed = self._time_fn() - (self._opened_at or self._time_fn())
                raise CircuitBreakerOpenError(
                    self.name, retry_after_seconds=self.recovery_timeout - elapsed
                )
            if current == CircuitState.HALF_OPEN:
                if self._half_open_in_flight:
                    raise CircuitBreakerOpenError(self.name, retry_after_seconds=0.5)
                self._half_open_in_flight = True

        try:
            result = await func(*args, **kwargs)
        except asyncio.CancelledError:
            # Caller cancelled: release the half-open probe slot without judging health
            self._half_open_in_flight = False
            raise
        except Exception as exc:
            async with self._lock:
                self._half_open_in_flight = False
                if not self._is_failure(exc):
                    # Upstream responded coherently (e.g. 4xx); it is healthy
                    if self._state == CircuitState.HALF_OPEN:
                        self._state = CircuitState.CLOSED
                        self._opened_at = None
                    self._failure_count = 0
                elif self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    self._opened_at = self._time_fn()
                    self._failure_count = self.failure_threshold
                else:
                    self._failure_count += 1
                    if self._failure_count >= self.failure_threshold:
                        self._state = CircuitState.OPEN
                        self._opened_at = self._time_fn()
            raise

        async with self._lock:
            self._half_open_in_flight = False
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None
        return result


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (TransientUpstreamError, UpstreamTimeoutError)):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 503)
    return False


def retry_with_backoff(
    *,
    max_retries: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    jitter: float = 0.03,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Exponential backoff with jitter retry decorator for transient upstream errors."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    if not _is_retryable_exception(exc) or attempt >= max_retries:
                        raise
                    delay = min(max_delay, base_delay * (2**attempt))
                    if jitter > 0:
                        delay += random.uniform(0.0, jitter)
                    attempt += 1
                    await sleep_fn(delay)

        return wrapper

    return decorator


async def execute_with_timeout(
    coro: Awaitable[T],
    timeout_seconds: float,
    source_name: str,
) -> T:
    """Await a coroutine with a hard deadline, raising UpstreamTimeoutError on expiry."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise UpstreamTimeoutError(
            f"{source_name} timed out after {timeout_seconds:.1f}s"
        ) from exc
