"""Download/upload engine — wires peers, pieces, and storage together.

Provides the core transfer loop:

-  :func:`run_peer` handles the full wire exchange for one connected peer
   (both seeding and leeching sides).
-  :func:`build_bitfield` constructs a ``Bitfield`` message from a
   :class:`PieceTracker`.
-  :func:`request_blocks` breaks a piece into 16 KiB block requests.

The engine is intentionally simple:

-  No choking algorithm — everyone is unchoked immediately.
-  No endgame mode — sequential block requests within each piece.
-  Peers that disconnect are silently dropped (the connection manager
   will supply replacements).
"""

from __future__ import annotations

import asyncio

from .events import TorrentEvent
from .peer import BLOCK_SIZE, PeerConnection
from .piece import PieceTracker
from .protocol import (
    Bitfield,
    Choke,
    Have,
    Interested,
    KeepAlive,
    NotInterested,
    Piece,
    Request,
    Unchoke,
)
from .storage.base import StorageBackend


def build_bitfield(tracker: PieceTracker) -> Bitfield:
    """Build a :class:`Bitfield` message from *tracker*'s ``have`` set."""
    pc = tracker.piece_count
    nbytes = (pc + 7) // 8
    buf = bytearray(nbytes)
    have = tracker.have
    for idx in have:
        buf[idx >> 3] |= 1 << (7 - (idx & 7))
    return Bitfield(data=bytes(buf))


def _bitfield_to_set(bf: Bitfield, piece_count: int) -> set[int]:
    """Convert a Bitfield message to a set of piece indices."""
    result: set[int] = set()
    for i in range(piece_count):
        if bf.has_piece(i):
            result.add(i)
    return result


async def run_peer(
    peer: PeerConnection,
    tracker: PieceTracker,
    storage: StorageBackend,
    piece_length: int,
    handle: object,
    done_event: asyncio.Event,
    stats: _PeerStats,
) -> None:
    """Run the full wire exchange for one connected peer.

    This coroutine is the main per-peer loop.  It sends our bitfield,
    processes incoming messages, and drives piece downloads when we are
    a leecher.

    Parameters
    ----------
    peer:
        An already-connected :class:`PeerConnection` (handshake done).
    tracker:
        The torrent's :class:`PieceTracker`.
    storage:
        The torrent's :class:`StorageBackend`.
    piece_length:
        Nominal piece size in bytes.
    handle:
        The :class:`TorrentHandle` for event emission.
    done_event:
        Set when the download completes.
    stats:
        Mutable stats accumulator.
    """
    try:
        # 1. Send our bitfield
        bf = build_bitfield(tracker)
        await peer.send_message(bf)

        # State
        peer_choking = True
        peer_interested = False
        am_interested = False
        peer_pieces: set[int] = set()

        # Piece download state
        current_piece: int | None = None
        piece_blocks: dict[int, bytes] = {}  # begin -> data
        piece_spec = None

        while True:
            # Check completion
            if tracker.is_complete and not peer_interested:
                # We're done downloading; stay connected to seed
                pass

            msg = await peer.receive_message()

            if isinstance(msg, KeepAlive):
                continue

            elif isinstance(msg, Bitfield):
                peer_pieces = _bitfield_to_set(msg, tracker.piece_count)
                tracker.update_availability(peer_pieces)
                # If we need pieces, express interest
                if not tracker.is_complete:
                    await peer.send_message(Interested())
                    am_interested = True

            elif isinstance(msg, Have):
                peer_pieces.add(msg.index)
                tracker.update_availability({msg.index})
                if not tracker.is_complete and not am_interested:
                    await peer.send_message(Interested())
                    am_interested = True

            elif isinstance(msg, Interested):
                peer_interested = True
                # Simple: unchoke everyone
                await peer.send_message(Unchoke())

            elif isinstance(msg, NotInterested):
                peer_interested = False

            elif isinstance(msg, Choke):
                peer_choking = True
                # Re-mark current piece as not pending so it can be retried
                if current_piece is not None:
                    tracker.mark_failed(current_piece)
                    current_piece = None
                    piece_blocks.clear()
                    piece_spec = None

            elif isinstance(msg, Unchoke):
                peer_choking = False
                # Start requesting if we need pieces
                if not tracker.is_complete and current_piece is None:
                    current_piece, piece_spec, piece_blocks = _start_piece(
                        tracker, peer_pieces
                    )
                    if current_piece is not None:
                        await _request_piece_blocks(peer, piece_spec)

            elif isinstance(msg, Request):
                # Seeder: serve the requested block
                if msg.index in tracker.have:
                    spec = tracker.spec(msg.index)
                    data = await storage.read(spec.offset + msg.begin, msg.length)
                    await peer.send_message(
                        Piece(index=msg.index, begin=msg.begin, block=data)
                    )
                    stats.bytes_uploaded += len(data)

            elif isinstance(msg, Piece):
                if current_piece is not None and msg.index == current_piece:
                    piece_blocks[msg.begin] = msg.block
                    stats.bytes_downloaded += len(msg.block)

                    # Check if we have all blocks for this piece
                    if piece_spec is not None and _piece_complete(
                        piece_blocks, piece_spec.length
                    ):
                        # Assemble and verify
                        assembled = _assemble_piece(piece_blocks, piece_spec.length)
                        if PieceTracker.verify_piece(assembled, piece_spec.hash):
                            await storage.write(piece_spec.offset, assembled)
                            tracker.mark_have(current_piece)
                            await handle._session.events.emit(
                                TorrentEvent.PIECE_VERIFIED,
                                handle,
                                current_piece,
                                suppress_errors=True,
                            )
                            # Notify peer we have this piece
                            await peer.send_message(Have(index=current_piece))
                        else:
                            tracker.mark_failed(current_piece)

                        # Move to next piece
                        current_piece = None
                        piece_blocks.clear()
                        piece_spec = None

                        if tracker.is_complete:
                            done_event.set()
                            await handle._session.events.emit(
                                TorrentEvent.COMPLETED,
                                handle,
                                suppress_errors=True,
                            )
                            # Send NotInterested
                            await peer.send_message(NotInterested())
                            am_interested = False
                        elif not peer_choking:
                            # Request next piece
                            current_piece, piece_spec, piece_blocks = _start_piece(
                                tracker, peer_pieces
                            )
                            if current_piece is not None:
                                await _request_piece_blocks(peer, piece_spec)

    except asyncio.IncompleteReadError, ConnectionError, OSError:
        # Peer disconnected
        pass
    finally:
        if current_piece is not None:
            tracker.mark_failed(current_piece)
        tracker.remove_availability(peer_pieces)
        await peer.disconnect()


class _PeerStats:
    """Mutable per-peer stats accumulator."""

    __slots__ = ("bytes_downloaded", "bytes_uploaded")

    def __init__(self) -> None:
        self.bytes_downloaded = 0
        self.bytes_uploaded = 0


def _start_piece(
    tracker: PieceTracker,
    peer_pieces: set[int],
) -> tuple[int | None, object, dict[int, bytes]]:
    """Pick the next piece to download from *peer_pieces*."""
    idx = tracker.select_piece()
    while idx is not None and idx not in peer_pieces:
        # The peer doesn't have this piece, skip it
        # (select_piece returns rarest globally, but this peer may not have it)
        tracker.mark_pending(idx)
        idx = tracker.select_piece()
        if idx is not None and idx not in peer_pieces:
            tracker.mark_failed(idx)  # un-pend, try next
            idx = tracker.select_piece()

    if idx is None:
        return None, None, {}

    tracker.mark_pending(idx)
    spec = tracker.spec(idx)
    return idx, spec, {}


async def _request_piece_blocks(peer: PeerConnection, spec: object) -> None:
    """Send Request messages for all blocks in a piece."""
    offset = 0
    while offset < spec.length:
        block_len = min(BLOCK_SIZE, spec.length - offset)
        await peer.send_message(
            Request(index=spec.index, begin=offset, length=block_len)
        )
        offset += block_len


def _piece_complete(blocks: dict[int, bytes], piece_length: int) -> bool:
    """Check if all blocks for a piece have arrived."""
    received = sum(len(b) for b in blocks.values())
    return received >= piece_length


def _assemble_piece(blocks: dict[int, bytes], piece_length: int) -> bytes:
    """Reassemble piece data from received blocks in order."""
    result = bytearray(piece_length)
    for begin, data in sorted(blocks.items()):
        result[begin : begin + len(data)] = data
    return bytes(result)
