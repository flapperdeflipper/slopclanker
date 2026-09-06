"""Tiny in-process fixed-window rate limiter."""

import time

_hits: dict[str, tuple[float, int]] = {}


def allow(key: str, limit: int, window: float = 3600.0) -> bool:
    """Count a hit for key; True while under limit within the window."""
    now = time.time()
    started, count = _hits.get(key, (now, 0))
    if now - started >= window:
        started, count = now, 0
    count += 1
    _hits[key] = (started, count)
    return count <= limit


def reset() -> None:
    _hits.clear()
