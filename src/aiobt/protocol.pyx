# cython: language_level=3str
# cython: boundscheck=False
# cython: wraparound=False
"""BitTorrent wire protocol (BEP 3) — Cython-optimized variant.

This module has the **same public interface** as ``protocol.py``.
When compiled, Python's import system prefers the ``.so`` over
the ``.py`` fallback automatically.

Cython constraints: no ``match``, no PEP 695 ``type``, no walrus,
no ``from __future__ import annotations``.
"""

import struct
from collections.abc import Buffer
from typing import Union

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# C-level constants for fast comparison
# ---------------------------------------------------------------------------

cdef int _MSG_CHOKE = 0
cdef int _MSG_UNCHOKE = 1
cdef int _MSG_INTERESTED = 2
cdef int _MSG_NOT_INTERESTED = 3
cdef int _MSG_HAVE = 4
cdef int _MSG_BITFIELD = 5
cdef int _MSG_REQUEST = 6
cdef int _MSG_PIECE = 7
cdef int _MSG_CANCEL = 8

# Python-visible constants (same names as protocol.py)
MSG_CHOKE = _MSG_CHOKE
MSG_UNCHOKE = _MSG_UNCHOKE
MSG_INTERESTED = _MSG_INTERESTED
MSG_NOT_INTERESTED = _MSG_NOT_INTERESTED
MSG_HAVE = _MSG_HAVE
MSG_BITFIELD = _MSG_BITFIELD
MSG_REQUEST = _MSG_REQUEST
MSG_PIECE = _MSG_PIECE
MSG_CANCEL = _MSG_CANCEL

# ---------------------------------------------------------------------------
# Type aliases (Cython-compatible — no PEP 695)
# ---------------------------------------------------------------------------

PeerId = bytes
"""20-byte peer identifier."""

InfoHash = bytes
"""20-byte SHA-1 of the bencoded info dict."""

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

PROTOCOL_STRING = b"BitTorrent protocol"
HANDSHAKE_LENGTH = 1 + 19 + 8 + 20 + 20  # 68 bytes

cdef int _HANDSHAKE_LENGTH = 68

# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Handshake:
    """The initial handshake exchanged between peers."""

    info_hash: object  # InfoHash
    peer_id: object    # PeerId
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
    def from_bytes(cls, data):
        cdef const unsigned char[:] buf
        cdef Py_ssize_t buf_len
        cdef int pstrlen

        raw = bytes(data)
        buf = raw
        buf_len = len(raw)

        if buf_len < _HANDSHAKE_LENGTH:
            raise ValueError(
                f"handshake too short: {buf_len} < {_HANDSHAKE_LENGTH}"
            )

        pstrlen = buf[0]
        if pstrlen != 19:
            raise ValueError(f"unexpected pstrlen: {pstrlen}")

        pstr = raw[1:20]
        if pstr != PROTOCOL_STRING:
            raise ValueError(f"unexpected protocol string: {pstr!r}")

        reserved = raw[20:28]
        info_hash = raw[28:48]
        peer_id = raw[48:68]
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
    msg_id: int = field(default=_MSG_CHOKE, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Unchoke:
    msg_id: int = field(default=_MSG_UNCHOKE, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Interested:
    msg_id: int = field(default=_MSG_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class NotInterested:
    msg_id: int = field(default=_MSG_NOT_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return struct.pack("!IB", 1, self.msg_id)


@dataclass(frozen=True, slots=True)
class Have:
    """Notify that we have piece *index*."""

    index: int

    def to_bytes(self) -> bytes:
        return struct.pack("!IBI", 5, _MSG_HAVE, self.index)


@dataclass(frozen=True, slots=True)
class Bitfield:
    """Bitfield of pieces the peer has."""

    data: bytes

    def to_bytes(self) -> bytes:
        cdef Py_ssize_t length = 1 + len(self.data)
        return struct.pack("!IB", length, _MSG_BITFIELD) + self.data

    def has_piece(self, int index) -> bool:
        cdef Py_ssize_t byte_index = index >> 3
        cdef int bit_offset
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
            "!IBIII", 13, _MSG_REQUEST, self.index, self.begin, self.length
        )


@dataclass(frozen=True, slots=True)
class Piece:
    """A block of piece data."""

    index: int
    begin: int
    block: bytes

    def to_bytes(self) -> bytes:
        cdef Py_ssize_t length = 9 + len(self.block)
        header = struct.pack("!IBII", length, _MSG_PIECE, self.index, self.begin)
        return header + self.block


@dataclass(frozen=True, slots=True)
class Cancel:
    """Cancel a previously sent request."""

    index: int
    begin: int
    length: int

    def to_bytes(self) -> bytes:
        return struct.pack(
            "!IBIII", 13, _MSG_CANCEL, self.index, self.begin, self.length
        )


# Union of all message types (Cython-compatible — no PEP 695)
PeerMessage = Union[
    KeepAlive,
    Choke,
    Unchoke,
    Interested,
    NotInterested,
    Have,
    Bitfield,
    Request,
    Piece,
    Cancel,
]


# ---------------------------------------------------------------------------
# Message parsing — hot path
# ---------------------------------------------------------------------------


def parse_message(data: bytes):
    """Parse a single peer message from *data* (without the 4-byte length prefix)."""
    cdef Py_ssize_t data_len = len(data)
    cdef int msg_id

    if data_len == 0:
        return KeepAlive()

    msg_id = data[0]
    payload = data[1:]

    if msg_id == _MSG_CHOKE:
        return Choke()
    elif msg_id == _MSG_UNCHOKE:
        return Unchoke()
    elif msg_id == _MSG_INTERESTED:
        return Interested()
    elif msg_id == _MSG_NOT_INTERESTED:
        return NotInterested()
    elif msg_id == _MSG_HAVE:
        (index,) = struct.unpack("!I", payload[:4])
        return Have(index=index)
    elif msg_id == _MSG_BITFIELD:
        return Bitfield(data=payload)
    elif msg_id == _MSG_REQUEST:
        index, begin, length = struct.unpack("!III", payload[:12])
        return Request(index=index, begin=begin, length=length)
    elif msg_id == _MSG_PIECE:
        index, begin = struct.unpack("!II", payload[:8])
        return Piece(index=index, begin=begin, block=payload[8:])
    elif msg_id == _MSG_CANCEL:
        index, begin, length = struct.unpack("!III", payload[:12])
        return Cancel(index=index, begin=begin, length=length)
    else:
        raise ValueError(f"unknown message id: {msg_id}")
