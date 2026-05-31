"""Compact single-blob storage backend.

Stores the entire torrent — even multi-file torrents — as a single
contiguous file on disk.  This dramatically simplifies storage
management for distribution services (seeding servers, CDN nodes)
where you care about *pieces* rather than the original file layout.

The blob file name is the hex-encoded info-hash, stored under a
configurable root directory.  Reading the original file layout back
out requires the torrent metadata, but for pure seeding this is
the most efficient representation since piece offsets map directly
to file offsets: ``piece_offset = piece_index * piece_length``.
"""

from __future__ import annotations

from pathlib import Path

from .queue import FileQueue


class CompactStorage:
    """Single-blob storage for distribution services.

    Parameters
    ----------
    root:
        Directory under which blob files are stored.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._queue = FileQueue()
        self._blob_path: Path | None = None
        self._total_length: int = 0
        self._piece_length: int = 0

    async def open(self, total_length: int, piece_length: int) -> None:
        self._total_length = total_length
        self._piece_length = piece_length

    async def prepare(self, info_hash_hex: str) -> None:
        """Create or open the blob file for the given torrent.

        Parameters
        ----------
        info_hash_hex:
            Hex-encoded info-hash, used as the blob file name.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._blob_path = self._root / f"{info_hash_hex}.blob"
        await self._queue.ensure_file(self._blob_path, self._total_length)

    async def read(self, offset: int, length: int) -> bytes:
        """Read *length* bytes starting at byte *offset*."""
        self._check_ready()
        assert self._blob_path is not None
        return await self._queue.read(self._blob_path, offset, length)

    async def write(self, offset: int, data: bytes) -> None:
        """Write *data* starting at byte *offset*."""
        self._check_ready()
        assert self._blob_path is not None
        await self._queue.write(self._blob_path, offset, data)

    async def close(self) -> None:
        """Release resources."""
        self._blob_path = None

    # ----- helpers ----------------------------------------------------------

    def _check_ready(self) -> None:
        if self._blob_path is None:
            raise RuntimeError(
                "CompactStorage not prepared — call prepare(info_hash_hex) first"
            )
