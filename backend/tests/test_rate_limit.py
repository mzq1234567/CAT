"""Tests for the sliding-window rate limiter (Step 7)."""
from __future__ import annotations

from app.security.rate_limit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    rl = RateLimiter(max_requests=3, window_seconds=60, time_func=clock)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False  # 4th within window blocked


def test_window_slides():
    clock = FakeClock()
    rl = RateLimiter(max_requests=2, window_seconds=60, time_func=clock)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    clock.advance(61)  # window passed
    assert rl.allow("k") is True


def test_keys_are_isolated():
    clock = FakeClock()
    rl = RateLimiter(max_requests=1, window_seconds=60, time_func=clock)
    assert rl.allow("tenantA:userA") is True
    assert rl.allow("tenantB:userB") is True  # different key, not affected
    assert rl.allow("tenantA:userA") is False


def test_retry_after_is_positive_when_blocked():
    clock = FakeClock()
    rl = RateLimiter(max_requests=1, window_seconds=60, time_func=clock)
    rl.allow("k")
    assert rl.allow("k") is False
    assert rl.retry_after("k") > 0
