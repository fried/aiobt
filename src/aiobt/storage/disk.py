"""Standard on-disk storage backend.

Stores torrent data using the original directory/file layout.  Each
file in the torrent is created at its expected path under a root
directory.  Pieces that span file boundaries are split across the
relevant files automatically.

All blocking I/O is dispatched through :class:`~aiobt.storage.queue.FileQueue`.
"""

from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass, field

from ..torrent import FileEntry
from .queue import FileQueue

# ---------------------------------------------------------------------------
# Internal mapping: which region of the linear piece space maps to which file
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileSlice:
    """A contiguous byte range within the linear torrent data that
    belongs to a single file on disk."""

    path: Path
    """Absolute path to the file."""

    file_offset: int
    """Byte offset within the file where this slice starts."""

    torrent_offset: int
    """Byte offset within the linear torrent data."""

    length: int
    """Length of this slice in bytes."""


class DiskStorage:
    """Multi-file on-disk storage using the original torrent layout.

    Parameters
    ----------
    root:
        Directory under which files are created.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._queue = FileQueue()
        self._slices: list[_FileSlice] = []
        self._total_length: int = 0
        self._piece_length: int = 0

    async def open(self, total_length: int, piece_length: int) -> None:
        self._total_length = total_length
        self._piece_length = piece_length

    async def prepare_files(
        self, files: tuple[FileEntry, ...] | None, name: str
    ) -> None:
        """Pre-allocate all files and build the offset map.

        Must be called after :meth:`open` and before any reads/writes.
        """
        self._slices.clear()

        if files is not None:
            # Multi-file mode
            offset = 0
            for entry in files:
                file_path = self._root / name / entry.relative_path
                self._slices.append(
                    _FileSlice(
                        path=file_path,
                        file_offset=0,
                        torrent_offset=offset,
                        length=entry.length,
                    )
                )
                await self._queue.ensure_file(file_path, entry.length)
                offset += entry.length
        else:
            # Single-file mode
            file_path = self._root / name
            self._slices.append(
                _FileSlice(
                    path=file_path,
                    file_offset=0,
                    torrent_offset=0,
                    length=self._total_length,
                )
            )
            await self._queue.ensure_file(file_path, self._total_length)

    async def read(self, offset: int, length: int) -> bytes:
        """Read *length* bytes starting at torrent byte *offset*.

        Handles reads that span multiple files transparently.
        """
        chunks: list[bytes] = []
        remaining = length

        for sl in self._slices_for(offset, length):
            # How far into *this* file does the read start?
            into_slice = max(0, offset - sl.torrent_offset)
            file_off = sl.file_offset + into_slice
            available = sl.length - into_slice
            to_read = min(remaining, available)

            data = await self._queue.read(sl.path, file_off, to_read)
            chunks.append(data)
            remaining -= to_read
            offset += to_read

            if remaining <= 0:
                break

        return b"".join(chunks)

    async def write(self, offset: int, data: bytes) -> None:
        """Write *data* at torrent byte *offset*.

        Handles writes that span multiple files transparently.
        """
        pos = 0
        remaining = len(data)

        for sl in self._slices_for(offset, len(data)):
            into_slice = max(0, offset - sl.torrent_offset)
            file_off = sl.file_offset + into_slice
            available = sl.length - into_slice
            to_write = min(remaining, available)

            await self._queue.write(sl.path, file_off, data[pos : pos + to_write])
            pos += to_write
            remaining -= to_write
            offset += to_write

            if remaining <= 0:
                break

    async def close(self) -> None:
        """Release resources (no-op for disk storage)."""
        self._slices.clear()

    # ----- internal helpers -------------------------------------------------

    def _slices_for(self, offset: int, length: int) -> list[_FileSlice]:
        """Return the file slices overlapping ``[offset, offset+length)``."""
        end = offset + length
        return [
            sl
            for sl in self._slices
            if sl.torrent_offset < end and sl.torrent_offset + sl.length > offset
        ]
