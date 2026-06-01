"""Peer connection management.

Manages individual TCP connections to BitTorrent peers, handling the
handshake, message framing, and connection lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct

import attrs

from .protocol import (
    HANDSHAKE_LENGTH,
    Handshake,
    PeerMessage,
    parse_message,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLOCK_SIZE = 2**14  # 16 KiB — standard block request size
MAX_PENDING_REQUESTS = 5  # Max outstanding requests per peer


# ---------------------------------------------------------------------------
# Peer state
# ---------------------------------------------------------------------------


@attrs.frozen
class PeerState:
    """Snapshot of a peer's current state — immutable."""

    am_choking: bool = True
    am_interested: bool = False
    peer_choking: bool = True
    peer_interested: bool = False


@attrs.frozen
class PeerInfo:
    """Identifying information for a peer."""

    host: str
    port: int
    peer_id: bytes | None = None


# ---------------------------------------------------------------------------
# Peer connection
# ---------------------------------------------------------------------------


class PeerConnection:
    """Manages a single TCP connection to a BitTorrent peer.

    Parameters
    ----------
    info:
        Address and optional peer ID.
    info_hash:
        The 20-byte info hash of the torrent.
    our_peer_id:
        Our 20-byte peer ID.
    """

    def __init__(
        self,
        info: PeerInfo,
        info_hash: bytes,
        our_peer_id: bytes,
    ) -> None:
        self._info = info
        self._info_hash = info_hash
        self._our_peer_id = our_peer_id
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._state = PeerState()

    @property
    def info(self) -> PeerInfo:
        return self._info

    @property
    def state(self) -> PeerState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 10.0) -> None:
        """Open TCP connection and perform the handshake."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._info.host, self._info.port),
            timeout=timeout,
        )
        await self._send_handshake()
        await self._receive_handshake()

    async def disconnect(self) -> None:
        """Close the connection gracefully."""
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None

    async def send_message(self, msg: PeerMessage) -> None:
        """Send a protocol message to the peer."""
        if self._writer is None:
            raise RuntimeError("not connected")
        data = msg.to_bytes()
        self._writer.write(data)
        await self._writer.drain()

    async def receive_message(self) -> PeerMessage:
        """Read and parse the next protocol message from the peer.

        Blocks until a complete message is available.
        """
        if self._reader is None:
            raise RuntimeError("not connected")

        # Read 4-byte length prefix
        length_bytes = await self._reader.readexactly(4)
        (length,) = struct.unpack("!I", length_bytes)

        if length == 0:
            from .protocol import KeepAlive

            return KeepAlive()

        payload = await self._reader.readexactly(length)
        return parse_message(payload)

    # ----- internal ---------------------------------------------------------

    async def _send_handshake(self) -> None:
        assert self._writer is not None
        hs = Handshake(info_hash=self._info_hash, peer_id=self._our_peer_id)
        self._writer.write(hs.to_bytes())
        await self._writer.drain()

    async def _receive_handshake(self) -> Handshake:
        assert self._reader is not None
        data = await self._reader.readexactly(HANDSHAKE_LENGTH)
        hs = Handshake.from_bytes(data)
        if hs.info_hash != self._info_hash:
            await self.disconnect()
            raise ValueError(
                f"info hash mismatch: expected {self._info_hash.hex()}, "
                f"got {hs.info_hash.hex()}"
            )
        return hs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_peer_id() -> bytes:
    """Generate a 20-byte peer ID using Azureus-style convention.

    Format: ``-AB0100-<12 random bytes>``
    """
    prefix = b"-AB0100-"
    suffix = os.urandom(12)
    return prefix + suffix
