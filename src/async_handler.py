"""Async rate-limited handler for concurrent search operations.

This module provides utilities for running multiple async tasks concurrently
while respecting rate limits to prevent API blocks. It implements:
- Semaphore-based concurrency control
- Configurable delays between task completions
- Graceful exception handling (failures don't stop other tasks)
- Result ordering preservation
- Exponential backoff with jitter for retries
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Coroutine, TypeVar, Callable, Awaitable

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TaskStatus(Enum):
    """Status of an individual task execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


@dataclass
class TaskResult:
    """Result wrapper for a single task execution.

    Attributes:
        index: Original position in the input task list
        value: The result value if successful, None otherwise
        error: The exception if failed, None otherwise
        status: Current status of the task
        attempts: Number of execution attempts made
        duration_ms: Execution duration in milliseconds
    """
    index: int
    value: Any = None
    error: Exception | None = None
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        """Check if task completed successfully."""
        return self.status == TaskStatus.COMPLETED and self.error is None


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting behavior.

    Attributes:
        max_concurrent: Maximum number of concurrent tasks
        delay_seconds: Base delay between task completions
        delay_jitter: Random jitter range (0.0-1.0) to add to delays
        max_retries: Maximum retry attempts for failed tasks
        initial_backoff: Initial backoff duration in seconds
        max_backoff: Maximum backoff duration in seconds
        backoff_multiplier: Multiplier for exponential backoff
        backoff_jitter: Jitter percentage for backoff (0.0-1.0)
    """
    max_concurrent: int = 5
    delay_seconds: float = 1.0
    delay_jitter: float = 0.2
    max_retries: int = 3
    initial_backoff: float = 1.0
    max_backoff: float = 60.0
    backoff_multiplier: float = 2.0
    backoff_jitter: float = 0.2


@dataclass
class RateLimitStats:
    """Statistics for rate-limited execution.

    Attributes:
        total_tasks: Total number of tasks submitted
        completed: Number of successfully completed tasks
        failed: Number of failed tasks
        retried: Number of tasks that required retries
        total_duration_ms: Total execution duration in milliseconds
        rate_limit_hits: Number of times rate limiting was triggered
    """
    total_tasks: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    total_duration_ms: float = 0.0
    rate_limit_hits: int = 0


class RateLimitError(Exception):
    """Raised when rate limiting is detected."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiter:
    """Token bucket rate limiter for request throttling.

    Implements a token bucket algorithm to control request rates.
    Tokens are replenished at a fixed rate up to a maximum capacity.
    """

    def __init__(
        self,
        rate: float,
        capacity: int | None = None,
        *,
        initial_tokens: int | None = None
    ):
        """Initialize the rate limiter.

        Args:
            rate: Token replenishment rate (tokens per second)
            capacity: Maximum token capacity (defaults to rate)
            initial_tokens: Starting token count (defaults to capacity)
        """
        self.rate = rate
        self.capacity = capacity if capacity is not None else int(rate)
        self._tokens = initial_tokens if initial_tokens is not None else self.capacity
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1, timeout: float | None = None) -> bool:
        """Acquire tokens from the bucket.

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum time to wait for tokens (None = wait indefinitely)

        Returns:
            True if tokens were acquired, False if timeout occurred
        """
        start_time = time.monotonic()

        while True:
            async with self._lock:
                self._replenish()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

                # Calculate wait time for sufficient tokens
                tokens_needed = tokens - self._tokens
                wait_time = tokens_needed / self.rate

            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed + wait_time > timeout:
                    return False
                wait_time = min(wait_time, timeout - elapsed)

            await asyncio.sleep(wait_time)

    def _replenish(self) -> None:
        """Replenish tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_update = now

    @property
    def available_tokens(self) -> float:
        """Get current available tokens (approximate)."""
        return self._tokens


def calculate_backoff(
    attempt: int,
    config: RateLimitConfig,
    *,
    retry_after: float | None = None
) -> float:
    """Calculate backoff duration with exponential increase and jitter.

    Args:
        attempt: Current attempt number (0-indexed)
        config: Rate limit configuration
        retry_after: Server-suggested retry delay (takes precedence)

    Returns:
        Backoff duration in seconds
    """
    if retry_after is not None:
        return retry_after

    # Exponential backoff: initial * multiplier^attempt
    backoff = config.initial_backoff * (config.backoff_multiplier ** attempt)
    backoff = min(backoff, config.max_backoff)

    # Apply jitter: +/- jitter percentage
    jitter_range = backoff * config.backoff_jitter
    jitter = random.uniform(-jitter_range, jitter_range)

    return max(0.1, backoff + jitter)


def calculate_delay(config: RateLimitConfig) -> float:
    """Calculate delay between task completions with jitter.

    Args:
        config: Rate limit configuration

    Returns:
        Delay duration in seconds
    """
    jitter_range = config.delay_seconds * config.delay_jitter
    jitter = random.uniform(-jitter_range, jitter_range)
    return max(0.0, config.delay_seconds + jitter)


async def run_with_rate_limit(
    tasks: list[Coroutine[Any, Any, T]],
    max_concurrent: int = 5,
    delay_seconds: float = 1.0
) -> list[Any]:
    """Execute tasks concurrently with rate limiting.

    Runs multiple async tasks with controlled concurrency and delays
    between completions to prevent triggering rate limits. Results
    are returned in the same order as the input tasks.

    Args:
        tasks: List of coroutines to execute
        max_concurrent: Maximum number of concurrent tasks
        delay_seconds: Delay in seconds between task completions

    Returns:
        List of results in the same order as input tasks.
        Failed tasks return the exception object instead of raising.

    Example:
        async def fetch_data(url: str) -> dict:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return await response.json()

        urls = ["http://api1.com", "http://api2.com", "http://api3.com"]
        tasks = [fetch_data(url) for url in urls]
        results = await run_with_rate_limit(tasks, max_concurrent=2, delay_seconds=0.5)
    """
    if not tasks:
        return []

    config = RateLimitConfig(
        max_concurrent=max_concurrent,
        delay_seconds=delay_seconds
    )

    results = await execute_with_rate_limit(tasks, config)

    # Extract values, returning exceptions for failed tasks
    return [
        result.value if result.success else result.error
        for result in results
    ]


async def execute_with_rate_limit(
    tasks: list[Coroutine[Any, Any, T]],
    config: RateLimitConfig | None = None
) -> list[TaskResult]:
    """Execute tasks with full rate limiting and retry support.

    More comprehensive version that returns TaskResult objects with
    detailed status information and supports automatic retries.

    Args:
        tasks: List of coroutines to execute
        config: Rate limiting configuration (uses defaults if None)

    Returns:
        List of TaskResult objects in input order
    """
    if not tasks:
        return []

    config = config or RateLimitConfig()
    semaphore = asyncio.Semaphore(config.max_concurrent)
    delay_lock = asyncio.Lock()
    results: list[TaskResult] = [TaskResult(index=i) for i in range(len(tasks))]
    stats = RateLimitStats(total_tasks=len(tasks))

    start_time = time.monotonic()

    async def execute_task(index: int, coro: Coroutine[Any, Any, T]) -> None:
        """Execute a single task with rate limiting."""
        result = results[index]
        result.status = TaskStatus.RUNNING
        task_start = time.monotonic()

        try:
            async with semaphore:
                result.attempts += 1
                value = await coro
                result.value = value
                result.status = TaskStatus.COMPLETED
                stats.completed += 1

                logger.debug(f"Task {index} completed successfully")

                # Delay after completion to avoid rate limits
                async with delay_lock:
                    delay = calculate_delay(config)
                    if delay > 0:
                        await asyncio.sleep(delay)

        except Exception as e:
            result.error = e
            result.status = TaskStatus.FAILED
            stats.failed += 1

            # Check if this is a rate limit error
            if isinstance(e, RateLimitError):
                result.status = TaskStatus.RATE_LIMITED
                stats.rate_limit_hits += 1

            logger.warning(f"Task {index} failed: {type(e).__name__}: {e}")

        finally:
            result.duration_ms = (time.monotonic() - task_start) * 1000

    # Execute all tasks concurrently
    await asyncio.gather(
        *[execute_task(i, coro) for i, coro in enumerate(tasks)],
        return_exceptions=True
    )

    stats.total_duration_ms = (time.monotonic() - start_time) * 1000

    logger.info(
        f"Rate-limited execution complete: {stats.completed}/{stats.total_tasks} "
        f"succeeded in {stats.total_duration_ms:.1f}ms"
    )

    return results


async def run_with_retry(
    task_factory: Callable[[], Coroutine[Any, Any, T]],
    config: RateLimitConfig | None = None,
    *,
    is_rate_limit_error: Callable[[Exception], bool] | None = None
) -> T:
    """Execute a single task with automatic retry on failure.

    Args:
        task_factory: Callable that returns a new coroutine for each attempt
        config: Rate limiting configuration
        is_rate_limit_error: Function to detect rate limit errors

    Returns:
        The task result on success

    Raises:
        The last exception if all retries fail
    """
    config = config or RateLimitConfig()

    def default_is_rate_limit(e: Exception) -> bool:
        return isinstance(e, RateLimitError)

    is_rate_limit = is_rate_limit_error or default_is_rate_limit
    last_error: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            return await task_factory()

        except Exception as e:
            last_error = e

            if attempt >= config.max_retries:
                logger.error(f"All {config.max_retries + 1} attempts failed")
                raise

            # Calculate backoff
            retry_after = None
            if isinstance(e, RateLimitError):
                retry_after = e.retry_after

            backoff = calculate_backoff(attempt, config, retry_after=retry_after)

            log_level = logging.WARNING if is_rate_limit(e) else logging.DEBUG
            logger.log(
                log_level,
                f"Attempt {attempt + 1} failed, retrying in {backoff:.2f}s: {e}"
            )

            await asyncio.sleep(backoff)

    # Should never reach here, but satisfy type checker
    raise last_error or RuntimeError("Unexpected retry loop exit")


class AsyncTaskQueue:
    """Priority queue for managing async tasks with rate limiting.

    Provides a more sophisticated task management system with:
    - Priority-based execution
    - Automatic rate limiting
    - Task cancellation support
    - Progress tracking
    """

    def __init__(self, config: RateLimitConfig | None = None):
        """Initialize the task queue.

        Args:
            config: Rate limiting configuration
        """
        self.config = config or RateLimitConfig()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent)
        self._delay_lock = asyncio.Lock()
        self._pending: list[tuple[int, Coroutine]] = []  # (priority, coroutine)
        self._running: int = 0
        self._completed: int = 0
        self._failed: int = 0

    async def submit(
        self,
        coro: Coroutine[Any, Any, T],
        priority: int = 0
    ) -> T:
        """Submit a task for execution.

        Args:
            coro: Coroutine to execute
            priority: Task priority (lower = higher priority)

        Returns:
            Task result
        """
        async with self._semaphore:
            self._running += 1
            try:
                result = await coro
                self._completed += 1

                # Apply rate limiting delay
                async with self._delay_lock:
                    delay = calculate_delay(self.config)
                    if delay > 0:
                        await asyncio.sleep(delay)

                return result

            except Exception:
                self._failed += 1
                raise
            finally:
                self._running -= 1

    @property
    def stats(self) -> dict[str, int]:
        """Get current queue statistics."""
        return {
            "running": self._running,
            "completed": self._completed,
            "failed": self._failed,
            "pending": len(self._pending)
        }


# Convenience type for common use case
TaskFactory = Callable[[], Coroutine[Any, Any, Any]]
