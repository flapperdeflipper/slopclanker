"""In-process event bus: fan-out of townhall happenings to live listeners.

Everything that happens — an events-table row via ``store._log``, a chat
message, a moderation delete — is published here on its way to SSE
subscribers (``GET /api/stream``). Pure asyncio: publishers are plain
functions (the store runs inside the event loop), subscribers get bounded
queues that drop the oldest entry on overflow, so a slow listener can
never block a writer and never sees a stale backlog either.
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

    def subscribe(self, predicate: Predicate | None = None) -> Subscriber:
        queue: Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        sub = (queue, predicate)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, event: dict[str, Any]) -> None:
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
