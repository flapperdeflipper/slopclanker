"""In-process event bus: fan-out of townhall happenings to live listeners.

Everything that happens — an events-table row via ``store._log``, a chat
message, a moderation delete — is published here on its way to SSE
subscribers (``GET /api/stream``). Subscribers get bounded queues that
drop the oldest entry on overflow, so a slow listener can never block a
writer and never sees a stale backlog either.

Threading: REST handlers run on the event loop, but sync MCP tools run
in worker threads. The first ``subscribe`` binds the serving loop;
``publish`` from any other thread trampolines delivery onto it via
``call_soon_threadsafe`` (asyncio queues are not thread-safe), while
on-loop publishes deliver immediately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

Queue = asyncio.Queue[dict[str, Any]]
Predicate = Callable[[dict[str, Any]], bool]
Subscriber = tuple[Queue, Predicate]

MAX_QUEUE = 256


class Bus:
    """Minimal fan-out; no persistence (the events table is the record)."""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def ensure_bound(self) -> None:
        """Bind the serving loop; rebinds if the old one is gone (tests,
        embedded restarts)."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.get_running_loop()

    def subscribe(self, predicate: Predicate | None = None) -> Subscriber:
        self.ensure_bound()
        queue: Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        sub = (queue, predicate)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, event: dict[str, Any]) -> None:
        loop = self._loop
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is None or current is loop:
            self._deliver(event)
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            pass  # serving loop already shut down

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue, predicate in list(self._subscribers):
            if predicate is not None and not predicate(event):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


bus = Bus()
