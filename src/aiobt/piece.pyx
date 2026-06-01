# cython: language_level=3str
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""Piece management, selection, and verification — Cython-optimized variant.

This module has the **same public interface** as ``piece.py``.
When compiled, Python's import system prefers the ``.so`` over
the ``.py`` fallback automatically.

Cython constraints: no ``match``, no PEP 695 ``type``, no walrus,
no ``from __future__ import annotations``.
"""

import hashlib
from collections.abc import Set

from dataclasses import dataclass

# SHA-1 digest length
cdef int _SHA1_LEN = 20


@dataclass(frozen=True, slots=True)
class PieceSpec:
    """Immutable specification for a single piece."""

    index: int
    """Zero-based piece index."""

    offset: int
    """Byte offset of this piece in the linear torrent data."""

    length: int
    """Length of this piece in bytes (last piece may be shorter)."""

    hash: bytes
    """Expected 20-byte SHA-1 digest."""


class PieceTracker:
    """Tracks download progress and piece availability.

    Parameters
    ----------
    piece_length:
        Nominal piece size in bytes.
    total_length:
        Total torrent size in bytes.
    pieces_raw:
        Concatenated 20-byte SHA-1 hashes for all pieces.
    """

    def __init__(
        self,
        Py_ssize_t piece_length,
        Py_ssize_t total_length,
        bytes pieces_raw not None,
    ) -> None:
        cdef Py_ssize_t full
        cdef Py_ssize_t remainder

        self._piece_length = piece_length
        self._total_length = total_length
        self._pieces_raw = pieces_raw

        # Compute piece count
        full = total_length // piece_length
        remainder = total_length % piece_length
        if remainder:
            self._piece_count = <int>(full + 1)
        else:
            self._piece_count = <int>full

        # Build piece specs
        self._specs = self._build_specs()

        # Tracking sets
        self._have = set()
        self._pending = set()
        self._failed = set()

        # Peer availability: piece_index -> number of peers that have it
        self._availability = {}

    @property
    def piece_count(self) -> int:
        return self._piece_count

    @property
    def have(self):
        """Indices of pieces we have and verified."""
        return frozenset(self._have)

    @property
    def pending(self):
        """Indices of pieces currently being downloaded."""
        return frozenset(self._pending)

    @property
    def is_complete(self) -> bool:
        return len(self._have) == self._piece_count

    @property
    def progress(self) -> float:
        """Download progress as a fraction [0.0, 1.0]."""
        if self._piece_count == 0:
            return 1.0
        return <double>len(self._have) / <double>self._piece_count

    def spec(self, int index):
        """Return the :class:`PieceSpec` for piece *index*."""
        return self._specs[index]

    def mark_have(self, int index) -> None:
        """Mark a piece as downloaded and verified."""
        self._have.add(index)
        self._pending.discard(index)
        self._failed.discard(index)

    def mark_pending(self, int index) -> None:
        """Mark a piece as being downloaded."""
        self._pending.add(index)

    def mark_failed(self, int index) -> None:
        """Mark a piece as failed verification."""
        self._pending.discard(index)
        self._failed.add(index)

    def update_availability(self, set peer_pieces not None) -> None:
        """Update availability from a peer's bitfield."""
        cdef int idx
        cdef int current
        cdef dict avail = self._availability
        for idx in peer_pieces:
            current = avail.get(idx, 0)
            avail[idx] = current + 1

    def remove_availability(self, set peer_pieces not None) -> None:
        """Decrement availability when a peer disconnects."""
        cdef int idx
        cdef int count
        cdef dict avail = self._availability
        for idx in peer_pieces:
            count = avail.get(idx, 0)
            if count <= 1:
                avail.pop(idx, None)
            else:
                avail[idx] = count - 1

    def select_piece(self):
        """Select the next piece to download using rarest-first.

        Returns ``None`` if all pieces are either have or pending.
        """
        cdef Py_ssize_t i
        cdef int pc = self._piece_count
        cdef int best_avail = 0
        cdef int avail_val
        cdef int best_idx = -1

        cdef set have = self._have
        cdef set pending = self._pending
        cdef dict avail = self._availability

        # Single pass: find the candidate with lowest availability
        for i in range(pc):
            if i in have or i in pending:
                continue
            avail_val = <int>avail.get(i, 0)
            if best_idx == -1 or avail_val < best_avail:
                best_avail = avail_val
                best_idx = <int>i

        if best_idx == -1:
            return None
        return best_idx

    @staticmethod
    def verify_piece(bytes data not None, bytes expected_hash not None) -> bool:
        """Verify piece *data* against its expected SHA-1 *hash*."""
        return hashlib.sha1(data).digest() == expected_hash

    # ----- internal ---------------------------------------------------------

    def _build_specs(self):
        cdef Py_ssize_t offset = 0
        cdef Py_ssize_t i
        cdef int pc = self._piece_count
        cdef Py_ssize_t piece_len = self._piece_length
        cdef Py_ssize_t total = self._total_length
        cdef Py_ssize_t length
        cdef Py_ssize_t hash_start

        cdef const unsigned char[:] raw = self._pieces_raw
        specs = []

        for i in range(pc):
            length = total - offset
            if length > piece_len:
                length = piece_len
            hash_start = i * _SHA1_LEN
            piece_hash = bytes(raw[hash_start:hash_start + _SHA1_LEN])
            specs.append(
                PieceSpec(index=<int>i, offset=<int>offset, length=<int>length, hash=piece_hash)
            )
            offset += length

        return tuple(specs)
