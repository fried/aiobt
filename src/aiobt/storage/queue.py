"""Executor-backed filesystem I/O queue.

All blocking file operations are dispatched through the running event
loop's default executor via :pymethod:`asyncio.loop.run_in_executor`.
The queue serializes writes to prevent interleaving, while reads can
proceed concurrently.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import attrs


@attrs.frozen
class _ReadOp:
    """Immutable descriptor for a read operation."""

    path: Path
    offset: int
    length: int


@attrs.frozen
class _WriteOp:
    """Immutable descriptor for a write operation."""

    path: Path
    offset: int
    data: bytes


class FileQueue:
    """Serializes filesystem I/O through :func:`asyncio.get_running_loop().run_in_executor`.

    Writes are serialized through an :class:`asyncio.Lock` so that
    concurrent piece writes never interleave.  Reads are allowed to
    proceed concurrently (they only acquire the lock if you explicitly
    request it via *exclusive*).
    """

    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()

    # ----- public API -------------------------------------------------------

    async def read(self, path: Path, offset: int, length: int) -> bytes:
        """Read *length* bytes from *path* at *offset*."""
        loop = asyncio.get_running_loop()
        op = _ReadOp(path=path, offset=offset, length=length)
        return await loop.run_in_executor(None, self._sync_read, op)

    async def write(self, path: Path, offset: int, data: bytes) -> None:
        """Write *data* to *path* at *offset*.

        Writes are serialized — only one write executes at a time.
        """
        loop = asyncio.get_running_loop()
        op = _WriteOp(path=path, offset=offset, data=data)
        async with self._write_lock:
            await loop.run_in_executor(None, self._sync_write, op)

    async def ensure_file(self, path: Path, length: int) -> None:
        """Create *path* and pre-allocate *length* bytes if it does not exist."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_ensure, path, length)

    async def file_size(self, path: Path) -> int:
        """Return the size of *path* in bytes."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_size, path)

    # ----- synchronous helpers (run in executor) ----------------------------

    @staticmethod
    def _sync_read(op: _ReadOp) -> bytes:
        with open(op.path, "rb") as fh:
            fh.seek(op.offset)
            data = fh.read(op.length)
        if len(data) != op.length:
            raise OSError(
                f"short read: expected {op.length} bytes, got {len(data)} "
                f"from {op.path} at offset {op.offset}"
            )
        return data

    @staticmethod
    def _sync_write(op: _WriteOp) -> None:
        # Open in r+b to write into an existing file without truncating.
        # Falls back to wb if the file doesn't exist yet.
        try:
            fh = open(op.path, "r+b")
        except FileNotFoundError:
            fh = open(op.path, "wb")
        try:
            fh.seek(op.offset)
            fh.write(op.data)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()

    @staticmethod
    def _sync_ensure(path: Path, length: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "wb") as fh:
                fh.truncate(length)

    @staticmethod
    def _sync_size(path: Path) -> int:
        return path.stat().st_size
