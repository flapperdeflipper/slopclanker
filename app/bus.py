"""In-process event bus for SSE and wait — bounded per subscriber.

Single-process by design (one add-on, one uvicorn). Reconnects recover
via the `since` cursor against the persisted, hash-chained events table;
the bus only carries live traffic.
"""

import asyncio
import json

MAX_QUEUE = 256


class _Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


bus = _Bus()


def sse_frame(event: dict) -> str:
    return (
        f"id: {event['id']}\nevent: {event['verb']}\ndata: "
        f"{json.dumps(event, default=str)}\n\n"
    )
