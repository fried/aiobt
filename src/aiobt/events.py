"""Async event system for client and torrent lifecycle events.

Listeners are async callables registered on a :class:`Client` or
:class:`TorrentHandle`.  When an event fires, all registered callbacks
for that event type run concurrently via :func:`asyncio.gather`.

Event types are plain enum members — no subclassing or string matching.

Usage::

    async def on_piece(handle: TorrentHandle, piece_index: int) -> None:
        print(f"{handle.name}: got piece {piece_index}")


    client.on(ClientEvent.TORRENT_ADDED, on_added)
    handle.on(TorrentEvent.PIECE_VERIFIED, on_piece)
"""

from __future__ import annotations

import asyncio
import enum
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any


# ---------------------------------------------------------------------------
# Event enums
# ---------------------------------------------------------------------------


class ClientEvent(enum.Enum):
    """Events fired on the :class:`Client` itself."""

    TORRENT_ADDED = "torrent_added"
    """A torrent was added.  ``callback(handle)``"""

    TORRENT_REMOVED = "torrent_removed"
    """A torrent was removed.  ``callback(handle)``"""

    TORRENT_COMPLETED = "torrent_completed"
    """A torrent finished downloading.  ``callback(handle)``"""

    TORRENT_ERROR = "torrent_error"
    """A torrent hit an unrecoverable error.  ``callback(handle, error)``"""


class TorrentEvent(enum.Enum):
    """Events fired on a :class:`TorrentHandle`."""

    STATE_CHANGED = "state_changed"
    """The torrent changed state.  ``callback(handle, old_state, new_state)``"""

    PIECE_VERIFIED = "piece_verified"
    """A piece was downloaded and passed hash check.  ``callback(handle, piece_index)``"""

    PEER_CONNECTED = "peer_connected"
    """A peer connected.  ``callback(handle, peer_addr)``"""

    PEER_DISCONNECTED = "peer_disconnected"
    """A peer disconnected.  ``callback(handle, peer_addr)``"""

    TRACKER_RESPONSE = "tracker_response"
    """A tracker announce succeeded.  ``callback(handle, response)``"""

    TRACKER_FAILED = "tracker_failed"
    """A tracker announce failed.  ``callback(handle, error)``"""

    COMPLETED = "completed"
    """All pieces verified — download is done.  ``callback(handle)``"""

    ERROR = "error"
    """An error occurred.  ``callback(handle, error)``"""


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

type EventCallback = Callable[..., Coroutine[Any, Any, None]]
"""An async callable that receives event-specific arguments."""

type EventType = ClientEvent | TorrentEvent
"""Union of all event enum types."""


# ---------------------------------------------------------------------------
# EventEmitter
# ---------------------------------------------------------------------------


class EventEmitter:
    """Mixin/standalone async event emitter.

    Supports :meth:`on` (register), :meth:`off` (unregister),
    :meth:`once` (one-shot), and :meth:`emit` (fire).

    All callbacks are awaited concurrently.  Exceptions in one callback
    do not prevent others from running — they are collected and the
    first is re-raised after all callbacks finish (unless
    ``suppress_errors=True`` was passed to :meth:`emit`).
    """

    __slots__ = ("_listeners",)

    def __init__(self) -> None:
        self._listeners: defaultdict[EventType, list[EventCallback]] = defaultdict(list)

    def on(
        self, event: EventType, callback: EventCallback | None = None
    ) -> EventCallback:
        """Register *callback* for *event*.  Returns *callback* for decorator use.

        Example::

            @emitter.on(TorrentEvent.COMPLETED)
            async def on_done(handle):
                print("done!")
        """
        if callback is not None:
            self._listeners[event].append(callback)
            return callback

        # Decorator form: emitter.on(event) returns a registrar
        def decorator(fn: EventCallback) -> EventCallback:
            self._listeners[event].append(fn)
            return fn

        return decorator  # type: ignore[return-value]

    def off(self, event: EventType, callback: EventCallback) -> None:
        """Remove *callback* from *event*.

        Silent no-op if *callback* is not registered.
        """
        try:
            self._listeners[event].remove(callback)
        except ValueError:
            pass

    def once(
        self, event: EventType, callback: EventCallback | None = None
    ) -> EventCallback:
        """Register *callback* to fire once, then auto-unregister.

        Returns a wrapper (the actual registered function), but also
        stores a reference to the original so :meth:`off` with the
        original works too.  Works as a decorator when called with
        only the event.
        """
        if callback is None:
            # Decorator form
            def decorator(fn: EventCallback) -> EventCallback:
                self.once(event, fn)
                return fn

            return decorator  # type: ignore[return-value]

        async def wrapper(*args: Any, **kwargs: Any) -> None:
            self.off(event, wrapper)
            await callback(*args, **kwargs)

        # Let off(event, original_callback) work too
        wrapper._original = callback  # type: ignore[attr-defined]
        self._listeners[event].append(wrapper)
        return callback

    async def emit(
        self,
        event: EventType,
        *args: Any,
        suppress_errors: bool = False,
    ) -> None:
        """Fire *event*, passing ``*args`` to every registered callback.

        Callbacks run concurrently via :func:`asyncio.gather`.
        If *suppress_errors* is False (default), the first exception
        raised by any callback is re-raised after all complete.
        """
        callbacks = self._listeners.get(event)
        if not callbacks:
            return

        # Snapshot the list so mutations during emit are safe
        snapshot = list(callbacks)
        results = await asyncio.gather(
            *(cb(*args) for cb in snapshot),
            return_exceptions=True,
        )

        if not suppress_errors:
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    def listener_count(self, event: EventType) -> int:
        """Return how many callbacks are registered for *event*."""
        return len(self._listeners.get(event, []))

    def clear(self, event: EventType | None = None) -> None:
        """Remove all listeners, or all listeners for a specific *event*."""
        if event is None:
            self._listeners.clear()
        else:
            self._listeners.pop(event, None)
