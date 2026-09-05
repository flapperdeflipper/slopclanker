"""In-process wake-up bus for the SSE push layer.

Every mutation commits its events-table rows inside a request handler on
the event-loop thread; the handler's ``_db()`` block then calls
:func:`wake` and each connected ``/api/events/stream`` listener re-queries
the events table. No payloads cross the bus -- it is a "look again"
signal, so a missed or duplicated wake is harmless: listeners dedupe by
event id and the keepalive timeout re-queries anyway.
"""

import asyncio

_waiters: set[asyncio.Event] = set()


def register() -> asyncio.Event:
    """Create and register a waiter; callers must :func:`unregister` it."""
    event = asyncio.Event()
    _waiters.add(event)
    return event


def unregister(event: asyncio.Event) -> None:
    _waiters.discard(event)


def wake() -> None:
    """Set all waiters. Safe from inside the event loop; a no-op when no
    loop is running (direct store use from scripts cannot serve SSE)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_soon(_set_all)


def _set_all() -> None:
    for event in list(_waiters):
        event.set()
