"""Tests for retry_async utility."""

import asyncio
import pytest

from voidx.tools.retry import retry_async


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_async(
            coro_fn, max_attempts=3, base_delay=0.01, max_delay=0.1,
            jitter=False, label="test",
        )
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        result = await retry_async(
            coro_fn, max_attempts=3, base_delay=0.01, max_delay=0.1,
            jitter=False, label="test",
        )
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_raises(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("always fail")

        with pytest.raises(ValueError, match="always fail"):
            await retry_async(
                coro_fn, max_attempts=3, base_delay=0.01, max_delay=0.1,
                jitter=False, label="test",
            )
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_on_filters_exceptions(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError):
            await retry_async(
                coro_fn, max_attempts=3, base_delay=0.01, max_delay=0.1,
                jitter=False, label="test", retry_on=ValueError,
            )
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_tuple_matches(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise KeyError("retryable")
            return "ok"

        result = await retry_async(
            coro_fn, max_attempts=3, base_delay=0.01, max_delay=0.1,
            jitter=False, label="test", retry_on=(ValueError, KeyError),
        )
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_max_attempts_one_no_retry(self):
        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("fail")

        with pytest.raises(ValueError):
            await retry_async(
                coro_fn, max_attempts=1, base_delay=0.01, max_delay=0.1,
                jitter=False, label="test",
            )
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_exponential_backoff_delay(self, monkeypatch):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        await retry_async(
            coro_fn, max_attempts=3, base_delay=1.0, max_delay=10.0,
            jitter=False, label="test",
        )
        assert delays == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_max_delay_caps_backoff(self, monkeypatch):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        call_count = 0

        async def coro_fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("fail")

        with pytest.raises(ValueError):
            await retry_async(
                coro_fn, max_attempts=5, base_delay=1.0, max_delay=3.0,
                jitter=False, label="test",
            )
        assert delays == [1.0, 2.0, 3.0, 3.0]

    @pytest.mark.asyncio
    async def test_jitter_reduces_delay(self, monkeypatch):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        monkeypatch.setattr("voidx.tools.retry.random.random", lambda: 0.0)

        async def coro_fn():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            await retry_async(
                coro_fn, max_attempts=2, base_delay=2.0, max_delay=10.0,
                jitter=True, label="test",
            )
        assert len(delays) == 1
        assert delays[0] == 1.0
