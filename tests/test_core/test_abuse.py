"""The rate limiter and the client address it is keyed on."""

from __future__ import annotations

from app.core.abuse import UNKNOWN_IP, RateLimiter, client_ip


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_limit_holds_within_a_window_and_resets_after_it() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 10.0, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [True, True, False]
    assert limiter.hit("b")  # keys are counted apart
    clock.now = 10.0
    assert limiter.hit("a")


def test_memory_stays_bounded_and_live_keys_survive_a_prune() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 10.0, max_keys=100, clock=clock)
    for i in range(100):
        limiter.hit(f"old{i}")
    clock.now = 10.0  # every window above has ended
    limiter.hit("live")
    assert len(limiter) == 1
    for i in range(1000):
        limiter.hit(f"new{i}")
    assert len(limiter) <= 100


def test_a_prune_frees_a_tenth_even_when_few_windows_have_ended() -> None:
    # A prune scans every key; trimming only to the cap would rescan on every new key.
    clock = Clock()
    limiter = RateLimiter(1, 10.0, max_keys=100, clock=clock)
    limiter.hit("first")
    clock.now = 5.0
    for i in range(99):
        limiter.hit(f"live{i}")
    clock.now = 10.0  # only "first" has ended
    limiter.hit("new")
    assert len(limiter) <= 91


def test_forget_and_clear() -> None:
    limiter = RateLimiter(1, 10.0)
    limiter.hit("a")
    limiter.forget("a")
    assert limiter.hit("a")
    limiter.clear()
    assert len(limiter) == 0


def test_client_ip_takes_the_rightmost_forwarded_entry() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")],
        "client": ("6.6.6.6", 1),
    }
    assert client_ip(scope) == "1.2.3.4"
    two_headers = {"headers": [(b"X-Forwarded-For", b"6.6.6.6"), (b"x-forwarded-for", b"5.5.5.5")]}
    assert client_ip(two_headers) == "5.5.5.5"


def test_client_ip_prefers_cf_connecting_ip() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4"), (b"CF-Connecting-IP", b" 7.7.7.7 ")],
        "client": ("6.6.6.6", 1),
    }
    assert client_ip(scope) == "7.7.7.7"
    empty = {"headers": [(b"cf-connecting-ip", b" "), (b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")]}
    assert client_ip(empty) == "1.2.3.4"


def test_client_ip_falls_back_to_the_peer() -> None:
    assert client_ip({"headers": [], "client": ("9.9.9.9", 1)}) == "9.9.9.9"
    assert client_ip({"headers": [(b"x-forwarded-for", b" ")], "client": None}) == UNKNOWN_IP
    assert client_ip({}) == UNKNOWN_IP
