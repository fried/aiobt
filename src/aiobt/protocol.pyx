# cython: language_level=3str
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""BitTorrent wire protocol (BEP 3) — Cython-optimized variant.

This module has the **same public interface** as ``protocol.py``.
When compiled, Python's import system prefers the ``.so`` over
the ``.py`` fallback automatically.

Cython constraints: no ``match``, no PEP 695 ``type``, no walrus,
no ``from __future__ import annotations``.
"""

import struct
from struct import unpack_from as _unpack_from
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
# Pre-computed constant byte strings for simple messages
# ---------------------------------------------------------------------------

cdef bytes _KEEPALIVE_BYTES = struct.pack("!I", 0)
cdef bytes _CHOKE_BYTES = struct.pack("!IB", 1, 0)
cdef bytes _UNCHOKE_BYTES = struct.pack("!IB", 1, 1)
cdef bytes _INTERESTED_BYTES = struct.pack("!IB", 1, 2)
cdef bytes _NOT_INTERESTED_BYTES = struct.pack("!IB", 1, 3)

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
HANDSHAKE_LENGTH = 68  # 1 + 19 + 8 + 20 + 20

cdef int _HANDSHAKE_LENGTH = 68

# Pre-computed handshake prefix: pstrlen(1) + pstr(19) = 20 bytes
cdef bytes _HANDSHAKE_PREFIX = bytes([19]) + b"BitTorrent protocol"

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
        # Single allocation via bytearray instead of 5-way concatenation
        cdef bytearray buf = bytearray(68)
        buf[0:20] = _HANDSHAKE_PREFIX
        buf[20:28] = self.reserved
        buf[28:48] = self.info_hash
        buf[48:68] = self.peer_id
        return bytes(buf)

    @classmethod
    def from_bytes(cls, bytes data not None):
        cdef const unsigned char[:] buf = data
        cdef Py_ssize_t buf_len = len(data)
        cdef int pstrlen

        if buf_len < _HANDSHAKE_LENGTH:
            raise ValueError(
                f"handshake too short: {buf_len} < {_HANDSHAKE_LENGTH}"
            )

        pstrlen = buf[0]
        if pstrlen != 19:
            raise ValueError(f"unexpected pstrlen: {pstrlen}")

        if data[1:20] != PROTOCOL_STRING:
            raise ValueError(f"unexpected protocol string: {data[1:20]!r}")

        return cls(
            info_hash=data[28:48],
            peer_id=data[48:68],
            reserved=data[20:28],
        )


# ---------------------------------------------------------------------------
# Peer messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """Keep-alive: length-prefixed zero-length message."""

    def to_bytes(self) -> bytes:
        return _KEEPALIVE_BYTES


@dataclass(frozen=True, slots=True)
class Choke:
    msg_id: int = field(default=_MSG_CHOKE, init=False)

    def to_bytes(self) -> bytes:
        return _CHOKE_BYTES


@dataclass(frozen=True, slots=True)
class Unchoke:
    msg_id: int = field(default=_MSG_UNCHOKE, init=False)

    def to_bytes(self) -> bytes:
        return _UNCHOKE_BYTES


@dataclass(frozen=True, slots=True)
class Interested:
    msg_id: int = field(default=_MSG_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return _INTERESTED_BYTES


@dataclass(frozen=True, slots=True)
class NotInterested:
    msg_id: int = field(default=_MSG_NOT_INTERESTED, init=False)

    def to_bytes(self) -> bytes:
        return _NOT_INTERESTED_BYTES


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
        cdef const unsigned char[:] buf = self.data
        if byte_index >= len(buf):
            return False
        bit_offset = 7 - (index & 7)
        return (buf[byte_index] >> bit_offset) & 1


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
        return struct.pack("!IBII", length, _MSG_PIECE, self.index, self.begin) + self.block


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


def parse_message(bytes data not None):
    """Parse a single peer message from *data* (without the 4-byte length prefix)."""
    cdef Py_ssize_t data_len = len(data)
    cdef int msg_id
    cdef unsigned int index, begin, length

    if data_len == 0:
        return KeepAlive()

    msg_id = data[0]

    if msg_id == _MSG_CHOKE:
        return Choke()
    elif msg_id == _MSG_UNCHOKE:
        return Unchoke()
    elif msg_id == _MSG_INTERESTED:
        return Interested()
    elif msg_id == _MSG_NOT_INTERESTED:
        return NotInterested()
    elif msg_id == _MSG_HAVE:
        # unpack_from reads directly — no payload slice needed
        (index,) = _unpack_from("!I", data, 1)
        return Have(index=index)
    elif msg_id == _MSG_BITFIELD:
        return Bitfield(data=data[1:])
    elif msg_id == _MSG_REQUEST:
        index, begin, length = _unpack_from("!III", data, 1)
        return Request(index=index, begin=begin, length=length)
    elif msg_id == _MSG_PIECE:
        index, begin = _unpack_from("!II", data, 1)
        return Piece(index=index, begin=begin, block=data[9:])
    elif msg_id == _MSG_CANCEL:
        index, begin, length = _unpack_from("!III", data, 1)
        return Cancel(index=index, begin=begin, length=length)
    else:
        raise ValueError(f"unknown message id: {msg_id}")
