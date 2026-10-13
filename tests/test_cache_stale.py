"""Tests for stale-while-revalidate cache logic (_fetch_with_lock).

P0 — The cache layer protects every API endpoint. If it breaks, users see
errors, stale responses are lost, and concurrent requests may pile up on
upstream APIs. These tests verify the core cache behaviours:
  - Fresh data returned on cache miss
  - Cached data returned on hit
  - Stale data served when upstream fails (graceful degradation)
  - Per-key locks prevent cross-key blocking
"""

import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache between tests."""
    import server

    original_cache = server._cache.copy()
    original_locks = server._cache_locks.copy()
    server._cache.clear()
    server._cache_locks.clear()
    yield
    server._cache.clear()
    server._cache_locks.clear()
    server._cache.update(original_cache)
    server._cache_locks.update(original_locks)


class TestFetchWithLockCacheMiss:
    """Cache miss: fetch_fn is called and result is stored."""

    def test_returns_fresh_data_on_miss(self):
        import server

        fetch_fn = MagicMock(return_value={"vehicles": [1, 2, 3]})
        result = server._fetch_with_lock("test_key", 10, fetch_fn)

        assert result == {"vehicles": [1, 2, 3]}
        fetch_fn.assert_called_once()

    def test_stores_data_in_cache_after_fetch(self):
        import server

        server._fetch_with_lock("store_key", 10, lambda: "stored_value")

        cached, hit = server._get_cached("store_key", 10)
        assert hit is True
        assert cached == "stored_value"


class TestFetchWithLockCacheHit:
    """Cache hit: fetch_fn is NOT called, cached data is returned."""

    def test_returns_cached_data_without_calling_fetch(self):
        import server

        server._set_cached("hit_key", {"cached": True})
        fetch_fn = MagicMock(return_value={"fresh": True})

        result = server._fetch_with_lock("hit_key", 10, fetch_fn)

        assert result == {"cached": True}
        fetch_fn.assert_not_called()

    def test_expired_cache_triggers_new_fetch(self):
        import server

        server._cache["expired_key"] = {"data": "old", "time": time.time() - 20}
        fetch_fn = MagicMock(return_value="new_data")

        result = server._fetch_with_lock("expired_key", 10, fetch_fn)

        assert result == "new_data"
        fetch_fn.assert_called_once()


class TestFetchWithLockStaleOnError:
    """Upstream failure: stale data is served instead of raising."""

    def test_returns_stale_data_when_fetch_raises(self):
        import server

        # Seed stale data (expired)
        server._cache["fail_key"] = {"data": "stale_value", "time": time.time() - 100}

        def failing_fetch():
            raise ConnectionError("upstream down")

        result = server._fetch_with_lock("fail_key", 10, failing_fetch)
        assert result == "stale_value"

    def test_raises_when_no_stale_data_and_fetch_fails(self):
        import server

        def failing_fetch():
            raise ConnectionError("upstream down")

        with pytest.raises(ConnectionError, match="upstream down"):
            server._fetch_with_lock("no_stale_key", 10, failing_fetch)


class TestPerKeyLocks:
    """Per-key locks: different keys don't block each other."""

    def test_different_keys_use_different_locks(self):
        import server

        lock_a = server._get_lock("key_a")
        lock_b = server._get_lock("key_b")

        assert lock_a is not lock_b

    def test_same_key_returns_same_lock(self):
        import server

        lock1 = server._get_lock("same_key")
        lock2 = server._get_lock("same_key")

        assert lock1 is lock2

    def test_concurrent_different_keys_dont_block(self):
        """Two threads fetching different keys should run concurrently."""
        import server

        barrier = threading.Barrier(2, timeout=5)
        results = {}

        def slow_fetch(key, value):
            def fetch():
                barrier.wait()  # Both threads must reach here
                return value
            results[key] = server._fetch_with_lock(key, 10, fetch)

        t1 = threading.Thread(target=slow_fetch, args=("k1", "v1"))
        t2 = threading.Thread(target=slow_fetch, args=("k2", "v2"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results["k1"] == "v1"
        assert results["k2"] == "v2"

    def test_stale_served_when_another_thread_holds_lock(self):
        """If another thread holds the lock, stale data is returned."""
        import server

        # Seed stale data
        server._cache["busy_key"] = {"data": "stale", "time": time.time() - 100}

        lock = server._get_lock("busy_key")
        lock.acquire()  # Simulate another thread holding the lock

        try:
            result = server._fetch_with_lock(
                "busy_key", 10, lambda: "should_not_be_called"
            )
            assert result == "stale"
        finally:
            lock.release()


class TestGetCachedHelpers:
    """Unit tests for _get_cached, _get_stale, _set_cached."""

    def test_get_cached_returns_miss_for_empty_cache(self):
        import server

        data, hit = server._get_cached("nonexistent", 10)
        assert hit is False
        assert data is None

    def test_get_stale_returns_none_for_empty_cache(self):
        import server

        assert server._get_stale("nonexistent") is None

    def test_get_stale_returns_data_regardless_of_age(self):
        import server

        server._cache["old"] = {"data": "ancient", "time": 0}
        assert server._get_stale("old") == "ancient"
