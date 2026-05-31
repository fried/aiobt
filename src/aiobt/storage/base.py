"""Abstract storage backend protocol.

Any object that satisfies :class:`StorageBackend` can be plugged into
:class:`~aiobt.client.BitTorrentClient`.  The protocol uses
:func:`typing.runtime_checkable` so backends can be validated with
``isinstance`` at initialization time.

All I/O methods are async.  Built-in implementations use
:class:`~aiobt.storage.queue.FileQueue` to dispatch blocking filesystem
calls through the default executor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that all storage backends must satisfy.

    Methods
    -------
    open
        Prepare storage for a torrent with the given geometry.
    close
        Flush and release resources.
    read
        Read *length* bytes starting at *offset* in the linear piece space.
    write
        Write *data* at *offset* in the linear piece space.
    """

    async def open(self, total_length: int, piece_length: int) -> None:
        """Initialize storage for a torrent.

        Parameters
        ----------
        total_length:
            Sum of all file lengths in the torrent.
        piece_length:
            Nominal piece size in bytes (last piece may be shorter).
        """
        ...

    async def close(self) -> None:
        """Flush pending writes and release file handles."""
        ...

    async def read(self, offset: int, length: int) -> bytes:
        """Read *length* bytes starting at byte *offset*.

        The offset is relative to the beginning of the linear torrent
        data (piece 0, byte 0).
        """
        ...

    async def write(self, offset: int, data: bytes) -> None:
        """Write *data* starting at byte *offset*.

        The offset is relative to the beginning of the linear torrent
        data (piece 0, byte 0).
        """
        ...
