"""Download/upload engine — wires peers, pieces, and storage together.

Provides the core transfer loop:

-  :func:`run_peer` handles the full wire exchange for one connected peer
   (both seeding and leeching sides).
-  :func:`build_bitfield` constructs a ``Bitfield`` message from a
   :class:`PieceTracker`.
-  :func:`request_blocks` breaks a piece into 16 KiB block requests.

Features:

-  **Choking**: integrated with :class:`ChokingManager` — the engine
   no longer auto-unchokes.  Choked peers' requests are silently
   ignored.
-  **Endgame mode**: when all remaining pieces are pending, duplicate-
   request from every peer that has them.  Cancel sent on completion.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .choking import ChokingManager, PeerRates
from .events import TorrentEvent
from .peer import BLOCK_SIZE, PeerConnection
from .piece import PieceSpec, PieceTracker
from .protocol import (
    Bitfield,
    Cancel,
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

if TYPE_CHECKING:
    from .client import TorrentHandle


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


# ---------------------------------------------------------------------------
# Endgame tracking (session-level shared state)
# ---------------------------------------------------------------------------


class EndgameState:
    """Shared endgame state for one torrent session.

    When all remaining pieces are already pending on at least one peer,
    endgame activates: every peer that has a needed piece will also
    request it.  On verification, Cancel is broadcast to all other
    peers.
    """

    __slots__ = ("active", "pieces", "peer_requests")

    def __init__(self) -> None:
        self.active: bool = False
        self.pieces: set[int] = set()
        # piece_index -> set of (host, port) addrs that have outstanding requests
        self.peer_requests: dict[int, set[tuple[str, int]]] = {}

    def enter(self, pending_pieces: set[int]) -> None:
        """Activate endgame for *pending_pieces*."""
        self.active = True
        self.pieces = set(pending_pieces)
        for idx in pending_pieces:
            self.peer_requests.setdefault(idx, set())

    def record_request(self, piece_index: int, addr: tuple[str, int]) -> None:
        """Record that *addr* has outstanding requests for *piece_index*."""
        self.peer_requests.setdefault(piece_index, set()).add(addr)

    def piece_done(self, piece_index: int) -> set[tuple[str, int]]:
        """Mark *piece_index* complete; return addrs that need Cancel."""
        self.pieces.discard(piece_index)
        addrs = self.peer_requests.pop(piece_index, set())
        if not self.pieces:
            self.active = False
        return addrs


# ---------------------------------------------------------------------------
# Per-peer stats
# ---------------------------------------------------------------------------


class _PeerStats:
    """Mutable per-peer stats accumulator."""

    __slots__ = ("bytes_downloaded", "bytes_uploaded")

    def __init__(self) -> None:
        self.bytes_downloaded = 0
        self.bytes_uploaded = 0


# ---------------------------------------------------------------------------
# Main per-peer loop
# ---------------------------------------------------------------------------


async def run_peer(
    peer: PeerConnection,
    tracker: PieceTracker,
    storage: StorageBackend,
    piece_length: int,
    handle: TorrentHandle,
    done_event: asyncio.Event,
    stats: _PeerStats,
    rates: PeerRates | None = None,
    endgame: EndgameState | None = None,
    addr: tuple[str, int] = ("?", 0),
    choking_mgr: ChokingManager | None = None,
) -> None:
    """Run the full wire exchange for one connected peer.

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
    rates:
        Optional :class:`PeerRates` from the :class:`ChokingManager`.
        When None, the peer auto-unchokes on Interested (legacy mode).
    endgame:
        Optional shared :class:`EndgameState` for this session.
    addr:
        ``(host, port)`` of this peer for endgame tracking.
    """
    # Initialized before try so the finally block can always access them
    current_piece: int | None = None
    piece_blocks: dict[int, bytes] = {}
    piece_spec = None
    peer_pieces: set[int] = set()

    try:
        # 1. Send our bitfield
        bf = build_bitfield(tracker)
        await peer.send_message(bf)

        # State
        peer_choking = True
        am_interested = False

        while True:
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
                if rates is not None:
                    # Choking manager handles unchoke decisions
                    rates.peer_interested = True
                    if choking_mgr is not None:
                        choking_mgr.wake()
                else:
                    # Legacy mode: unchoke everyone
                    await peer.send_message(Unchoke())

            elif isinstance(msg, NotInterested):
                if rates is not None:
                    rates.peer_interested = False
                    if choking_mgr is not None:
                        choking_mgr.wake()

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
                        tracker, peer_pieces, endgame, addr
                    )
                    if current_piece is not None:
                        assert piece_spec is not None
                        await _request_piece_blocks(peer, piece_spec)

            elif isinstance(msg, Request):
                # Only serve if we're not choking this peer
                am_choking = rates.am_choking if rates is not None else False
                if am_choking:
                    continue  # silently ignore requests from choked peers
                if msg.index in tracker.have:
                    spec = tracker.spec(msg.index)
                    data = await storage.read(spec.offset + msg.begin, msg.length)
                    await peer.send_message(
                        Piece(index=msg.index, begin=msg.begin, block=data)
                    )
                    stats.bytes_uploaded += len(data)
                    if rates is not None:
                        rates.bytes_up_interval += len(data)

            elif isinstance(msg, Piece):
                if current_piece is not None and msg.index == current_piece:
                    piece_blocks[msg.begin] = msg.block
                    stats.bytes_downloaded += len(msg.block)
                    if rates is not None:
                        rates.bytes_down_interval += len(msg.block)

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

                            # Endgame: cancel this piece on other peers
                            if (
                                endgame is not None
                                and endgame.active
                                and current_piece in endgame.peer_requests
                            ):
                                cancel_addrs = endgame.piece_done(current_piece)
                                cancel_addrs.discard(addr)
                                # We can't send Cancel to other peers from here,
                                # but we remove them from endgame tracking.
                                # The session-level cancel broadcast is handled
                                # in _broadcast_endgame_cancel via the session.
                                await _broadcast_endgame_cancel(
                                    handle,
                                    current_piece,
                                    piece_spec,
                                    cancel_addrs,
                                )
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
                            await peer.send_message(NotInterested())
                            am_interested = False
                        elif not peer_choking:
                            # Request next piece
                            current_piece, piece_spec, piece_blocks = _start_piece(
                                tracker, peer_pieces, endgame, addr
                            )
                            if current_piece is not None:
                                assert piece_spec is not None
                                await _request_piece_blocks(peer, piece_spec)

            elif isinstance(msg, Cancel):
                # Acknowledged — we don't queue outgoing, so nothing to cancel
                pass

    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        # Peer disconnected
        pass
    finally:
        if current_piece is not None:
            tracker.mark_failed(current_piece)
        tracker.remove_availability(peer_pieces)
        if endgame is not None:
            # Clean up our endgame registrations
            for piece_set in endgame.peer_requests.values():
                piece_set.discard(addr)
        await peer.disconnect()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_piece(
    tracker: PieceTracker,
    peer_pieces: set[int],
    endgame: EndgameState | None = None,
    addr: tuple[str, int] = ("?", 0),
) -> tuple[int | None, PieceSpec | None, dict[int, bytes]]:
    """Pick the next piece to download from *peer_pieces*.

    If normal selection returns None but pieces remain, check for
    endgame activation.
    """
    idx = tracker.select_piece()

    # Find a piece this peer actually has
    while idx is not None and idx not in peer_pieces:
        tracker.mark_pending(idx)
        idx = tracker.select_piece()
        if idx is not None and idx not in peer_pieces:
            tracker.mark_failed(idx)
            idx = tracker.select_piece()

    if idx is not None:
        tracker.mark_pending(idx)
        spec = tracker.spec(idx)
        if endgame is not None:
            endgame.record_request(idx, addr)
        return idx, spec, {}

    # No piece from normal selection — check endgame
    if endgame is not None and not tracker.is_complete:
        if not endgame.active:
            # Activate endgame: all remaining = pending
            remaining = {i for i in range(tracker.piece_count) if i not in tracker.have}
            if remaining:
                endgame.enter(remaining)

        if endgame.active:
            # Grab a pending piece this peer has
            for pidx in endgame.pieces:
                if pidx in peer_pieces and pidx not in tracker.have:
                    spec = tracker.spec(pidx)
                    endgame.record_request(pidx, addr)
                    return pidx, spec, {}

    return None, None, {}


async def _request_piece_blocks(peer: PeerConnection, spec: PieceSpec) -> None:
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


async def _broadcast_endgame_cancel(
    handle: TorrentHandle,
    piece_index: int,
    piece_spec: PieceSpec,
    cancel_addrs: set[tuple[str, int]],
) -> None:
    """Send Cancel for all blocks of *piece_index* to peers in *cancel_addrs*."""
    if not cancel_addrs:
        return
    session = handle._session
    offset = 0
    while offset < piece_spec.length:
        block_len = min(BLOCK_SIZE, piece_spec.length - offset)
        cancel_msg = Cancel(index=piece_index, begin=offset, length=block_len)
        for addr in cancel_addrs:
            peer = session.peers.get(addr)
            if peer is not None and peer.is_connected:
                try:
                    await peer.send_message(cancel_msg)
                except (ConnectionError, OSError):
                    pass
        offset += block_len
