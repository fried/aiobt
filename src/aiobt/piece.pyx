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

Typing strategy: only type args/locals where Cython can actually
eliminate Python overhead (tight loops, arithmetic, memoryview access).
For methods whose body is just builtin container ops (set.add,
dict.get), untyped args are faster — the container C API already
takes PyObject*, so typed args just add needless int<->PyLong
round-trips on the call boundary.
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

    def __init__(self, piece_length, total_length, pieces_raw) -> None:
        cdef Py_ssize_t pl = piece_length
        cdef Py_ssize_t tl = total_length
        cdef Py_ssize_t full
        cdef Py_ssize_t remainder

        self._piece_length = pl
        self._total_length = tl
        self._pieces_raw = pieces_raw

        # Compute piece count
        full = tl // pl
        remainder = tl % pl
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

    def spec(self, index):
        """Return the :class:`PieceSpec` for piece *index*."""
        return self._specs[index]

    # mark_have/pending/failed: untyped args — body is pure set ops
    # which take PyObject*. Typing index as C int adds a needless
    # PyLong->int->PyLong round-trip that's slower than the set calls.

    def mark_have(self, index) -> None:
        """Mark a piece as downloaded and verified."""
        self._have.add(index)
        self._pending.discard(index)
        self._failed.discard(index)

    def mark_pending(self, index) -> None:
        """Mark a piece as being downloaded."""
        self._pending.add(index)

    def mark_failed(self, index) -> None:
        """Mark a piece as failed verification."""
        self._pending.discard(index)
        self._failed.add(index)

    # update/remove_availability: untyped peer_pieces arg avoids
    # the isinstance(x, set) type check Cython inserts for typed set args.
    # The loop body is dict.get + dict.__setitem__ — already C.
    # We DO type the loop variable and dict local since those help
    # inside the loop without adding call-boundary overhead.

    def update_availability(self, peer_pieces) -> None:
        """Update availability from a peer's bitfield."""
        cdef dict avail = self._availability
        for idx in peer_pieces:
            avail[idx] = avail.get(idx, 0) + 1

    def remove_availability(self, peer_pieces) -> None:
        """Decrement availability when a peer disconnects."""
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
    def verify_piece(data, expected_hash) -> bool:
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

        # Plain bytes slicing — faster than memoryview for sequential
        # fixed-size chunks since it avoids memoryview creation overhead.
        raw = self._pieces_raw
        specs = []

        for i in range(pc):
            length = total - offset
            if length > piece_len:
                length = piece_len
            hash_start = i * _SHA1_LEN
            specs.append(
                PieceSpec(index=<int>i, offset=<int>offset, length=<int>length,
                          hash=raw[hash_start:hash_start + _SHA1_LEN])
            )
            offset += length

        return tuple(specs)
