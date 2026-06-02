"""Resume data persistence for crash recovery.

Saves the set of verified pieces to disk so downloads can resume after
a restart without re-downloading completed data.

File format: bencoded dictionary stored at
``{state_dir}/{info_hash_hex}.resume``.

Writes are atomic (write-to-tmp, ``os.replace``).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dataclasses import dataclass

from .bencode import DecodeError, decode, encode


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResumeData:
    """Loaded resume state for a torrent."""

    info_hash: bytes
    """Expected info hash (validated against saved data)."""

    have: frozenset[int]
    """Piece indices marked as complete in the saved bitfield."""

    downloaded: int = 0
    """Cumulative bytes downloaded (informational)."""

    uploaded: int = 0
    """Cumulative bytes uploaded (informational)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resume_path(state_dir: Path, info_hash: bytes) -> Path:
    """Return the canonical resume file path for a torrent."""
    return state_dir / f"{info_hash.hex()}.resume"


def _have_to_bitfield(have: frozenset[int], piece_count: int) -> bytes:
    """Encode *have* set into a compact bitfield."""
    nbytes = (piece_count + 7) // 8
    buf = bytearray(nbytes)
    for idx in have:
        buf[idx >> 3] |= 1 << (7 - (idx & 7))
    return bytes(buf)


def _bitfield_to_have(data: bytes, piece_count: int) -> frozenset[int]:
    """Decode a bitfield into a frozenset of piece indices."""
    have: set[int] = set()
    for i in range(piece_count):
        byte_idx, bit_idx = divmod(i, 8)
        if byte_idx < len(data) and data[byte_idx] & (1 << (7 - bit_idx)):
            have.add(i)
    return frozenset(have)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _sync_write(tmp: Path, final: Path, data: bytes) -> None:
    """Blocking atomic write: tmp → rename."""
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    os.replace(str(tmp), str(final))


async def save_resume(
    path: Path,
    *,
    info_hash: bytes,
    have: frozenset[int],
    piece_count: int,
    downloaded: int = 0,
    uploaded: int = 0,
) -> None:
    """Atomically save resume data to *path*."""
    bf = _have_to_bitfield(have, piece_count)
    payload = encode(
        {
            b"info_hash": info_hash,
            b"bitfield": bf,
            b"piece_count": piece_count,
            b"downloaded": downloaded,
            b"uploaded": uploaded,
        }
    )
    tmp = path.with_suffix(".tmp")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync_write, tmp, path, payload)


def load_resume(path: Path, info_hash: bytes) -> ResumeData | None:
    """Load resume data from *path*, returning ``None`` on any error.

    Returns ``None`` if the file is missing, corrupt, or the stored
    info hash doesn't match *info_hash*.
    """
    try:
        raw = decode(path.read_bytes())
    except (FileNotFoundError, DecodeError, OSError):
        return None

    if not isinstance(raw, dict):
        return None

    saved_hash = raw.get(b"info_hash")
    if saved_hash != info_hash:
        return None

    piece_count = raw.get(b"piece_count", 0)
    if not isinstance(piece_count, int) or piece_count <= 0:
        return None

    bf = raw.get(b"bitfield", b"")
    if not isinstance(bf, bytes):
        return None

    have = _bitfield_to_have(bf, piece_count)

    downloaded = raw.get(b"downloaded", 0)
    uploaded = raw.get(b"uploaded", 0)

    return ResumeData(
        info_hash=info_hash,
        have=have,
        downloaded=downloaded if isinstance(downloaded, int) else 0,
        uploaded=uploaded if isinstance(uploaded, int) else 0,
    )
