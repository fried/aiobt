"""BitTorrent wire protocol (BEP 3).

Defines message types, parsing, and serialization for the peer wire
protocol.  All messages are represented as frozen dataclasses.
"""

from __future__ import annotations

import struct
from collections.abc import Buffer

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

type PeerId = bytes
"""20-byte peer identifier."""

type InfoHash = bytes
"""20-byte SHA-1 of the bencoded info dict."""

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

PROTOCOL_STRING = b"BitTorrent protocol"
HANDSHAKE_LENGTH = 1 + 19 + 8 + 20 + 20  # 68 bytes

# Message IDs (BEP 3)
MSG_CHOKE = 0
MSG_UNCHOKE = 1
MSG_INTERESTED = 2
MSG_NOT_INTERESTED = 3
MSG_HAVE = 4
MSG_BITFIELD = 5
MSG_REQUEST = 6
MSG_PIECE = 7
MSG_CANCEL = 8

# Keep-alive is a zero-length message (no ID)

# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Handshake:
    """The initial handshake exchanged between peers."""

    info_hash: InfoHash
    peer_id: PeerId
    reserved: bytes = b"\x00" * 8

    def to_bytes(self) -> bytes:
        return (
            bytes([19])
            + PROTOCOL_STRING
            + self.reserved
            + self.info_hash
            + self.peer_id
        )

    @classmethod
    def from_bytes(cls, data: Buffer) -> Handshake:
        buf = memoryview(data)
        if len(buf) < HANDSHAKE_LENGTH:
            raise ValueError(f"handshake too short: {len(buf)} < {HANDSHAKE_LENGTH}")
        pstrlen = buf[0]
        if pstrlen != 19:
            raise ValueError(f"unexpected pstrlen: {pstrlen}")
        pstr = bytes(buf[1:20])
        if pstr != PROTOCOL_STRING:
            raise ValueError(f"unexpected protocol string: {pstr!r}")
        reserved = bytes(buf[20:28])
        info_hash = bytes(buf[28:48])
        peer_id = bytes(buf[48:68])
        return cls(info_hash=info_hash, peer_id=peer_id, reserved=reserved)


# ---------------------------------------------------------------------------
# Peer messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """Keep-alive: length-prefixed zero-length message."""

    def to_bytes(self) -> bytes:
        return struct.pack("!I", 0)


@dataclass(frozen=True, slots=True)
class Choke:
    msg_id: int = field(default=MSG_CHOKE, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Unchoke:
    msg_id: int = field(default=MSG_UNCHOKE, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Interested:
    msg_id: int = field(default=MSG_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class NotInterested:
    msg_id: int = field(default=MSG_NOT_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Have:
    """Notify that we have piece *index*."""

    index: int

    def to_bytes(self) -> bytes:
        return struct.pack("!IBI", 5, MSG_HAVE, self.index)


@dataclass(frozen=True, slots=True)
class Bitfield:
    """Bitfield of pieces the peer has."""

    data: bytes

    def to_bytes(self) -> bytes:
        length = 1 + len(self.data)
        return struct.pack("!IB", length, MSG_BITFIELD) + self.data

    def has_piece(self, index: int) -> bool:
        byte_index = index >> 3
        if byte_index >= len(self.data):
            return False
        bit_offset = 7 - (index & 7)
        return bool(self.data[byte_index] & (1 << bit_offset))


@dataclass(frozen=True, slots=True)
class Request:
    """Request a block from a piece."""

    index: int
    begin: int
    length: int

    def to_bytes(self) -> bytes:
        return struct.pack(
            "!IBIII", 13, MSG_REQUEST, self.index, self.begin, self.length
        )


@dataclass(frozen=True, slots=True)
class Piece:
    """A block of piece data."""

    index: int
    begin: int
    block: bytes

    def to_bytes(self) -> bytes:
        length = 9 + len(self.block)
        header = struct.pack("!IBII", length, MSG_PIECE, self.index, self.begin)
        return header + self.block


@dataclass(frozen=True, slots=True)
class Cancel:
    """Cancel a previously sent request."""

    index: int
    begin: int
    length: int

    def to_bytes(self) -> bytes:
        return struct.pack(
            "!IBIII", 13, MSG_CANCEL, self.index, self.begin, self.length
        )


# Union of all message types
type PeerMessage = (
    KeepAlive
    | Choke
    | Unchoke
    | Interested
    | NotInterested
    | Have
    | Bitfield
    | Request
    | Piece
    | Cancel
)


def parse_message(data: bytes) -> PeerMessage:
    """Parse a single peer message from *data* (without the 4-byte length prefix)."""
    if not data:
        return KeepAlive()

    msg_id = data[0]
    payload = data[1:]

    match msg_id:
        case 0:
            return Choke()
        case 1:
            return Unchoke()
        case 2:
            return Interested()
        case 3:
            return NotInterested()
        case 4:
            (index,) = struct.unpack("!I", payload[:4])
            return Have(index=index)
        case 5:
            return Bitfield(data=payload)
        case 6:
            index, begin, length = struct.unpack("!III", payload[:12])
            return Request(index=index, begin=begin, length=length)
        case 7:
            index, begin = struct.unpack("!II", payload[:8])
            return Piece(index=index, begin=begin, block=payload[8:])
        case 8:
            index, begin, length = struct.unpack("!III", payload[:12])
            return Cancel(index=index, begin=begin, length=length)
        case _:
            raise ValueError(f"unknown message id: {msg_id}")
