"""Piece management, selection, and verification.

Tracks which pieces have been downloaded, verified, and are available
for upload.  Provides piece selection strategies (rarest-first is the
default per BEP 3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Set

from dataclasses import dataclass, field


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
        piece_length: int,
        total_length: int,
        pieces_raw: bytes,
    ) -> None:
        self._piece_length = piece_length
        self._total_length = total_length
        self._pieces_raw = pieces_raw

        # Compute piece count
        full, remainder = divmod(total_length, piece_length)
        self._piece_count = full + (1 if remainder else 0)

        # Build piece specs
        self._specs: tuple[PieceSpec, ...] = self._build_specs()

        # Tracking sets
        self._have: set[int] = set()
        self._pending: set[int] = set()
        self._failed: set[int] = set()

        # Peer availability: piece_index -> number of peers that have it
        self._availability: dict[int, int] = {}

    @property
    def piece_count(self) -> int:
        return self._piece_count

    @property
    def have(self) -> frozenset[int]:
        """Indices of pieces we have and verified."""
        return frozenset(self._have)

    @property
    def pending(self) -> frozenset[int]:
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
        return len(self._have) / self._piece_count

    def spec(self, index: int) -> PieceSpec:
        """Return the :class:`PieceSpec` for piece *index*."""
        return self._specs[index]

    def mark_have(self, index: int) -> None:
        """Mark a piece as downloaded and verified."""
        self._have.add(index)
        self._pending.discard(index)
        self._failed.discard(index)

    def mark_pending(self, index: int) -> None:
        """Mark a piece as being downloaded."""
        self._pending.add(index)

    def mark_failed(self, index: int) -> None:
        """Mark a piece as failed verification."""
        self._pending.discard(index)
        self._failed.add(index)

    def update_availability(self, peer_pieces: Set[int]) -> None:
        """Update availability from a peer's bitfield."""
        for idx in peer_pieces:
            self._availability[idx] = self._availability.get(idx, 0) + 1

    def remove_availability(self, peer_pieces: Set[int]) -> None:
        """Decrement availability when a peer disconnects."""
        for idx in peer_pieces:
            count = self._availability.get(idx, 0)
            if count <= 1:
                self._availability.pop(idx, None)
            else:
                self._availability[idx] = count - 1

    def select_piece(self) -> int | None:
        """Select the next piece to download using rarest-first.

        Returns ``None`` if all pieces are either have or pending.
        """
        candidates = [
            i
            for i in range(self._piece_count)
            if i not in self._have and i not in self._pending
        ]
        if not candidates:
            return None

        # Rarest first: sort by availability, break ties randomly
        candidates.sort(key=lambda i: self._availability.get(i, 0))
        return candidates[0]

    @staticmethod
    def verify_piece(data: bytes, expected_hash: bytes) -> bool:
        """Verify piece *data* against its expected SHA-1 *hash*."""
        return hashlib.sha1(data).digest() == expected_hash

    # ----- internal ---------------------------------------------------------

    def _build_specs(self) -> tuple[PieceSpec, ...]:
        specs: list[PieceSpec] = []
        offset = 0
        for i in range(self._piece_count):
            length = min(self._piece_length, self._total_length - offset)
            piece_hash = self._pieces_raw[i * 20 : (i + 1) * 20]
            specs.append(
                PieceSpec(index=i, offset=offset, length=length, hash=piece_hash)
            )
            offset += length
        return tuple(specs)
