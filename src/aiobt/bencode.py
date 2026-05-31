"""Bencode encoding and decoding.

Pure-Python implementation of the BitTorrent bencode serialization format
(BEP 3). Structured as module-level functions with simple types for optional
Cython compilation — see ``cython/README.md``.

Bencode supports four data types:

- Byte strings: ``<length>:<data>``
- Integers: ``i<integer>e``
- Lists: ``l<items>e``
- Dictionaries: ``d<key><value>...e`` (keys must be byte strings, sorted)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# ---------------------------------------------------------------------------
# Public type alias — recursive via PEP 695 ``type`` statement (3.12+)
# ---------------------------------------------------------------------------

type BencodeValue = int | bytes | list[BencodeValue] | dict[bytes, BencodeValue]

# Sentinels used by the decoder
_CHR_I = ord("i")  # 105
_CHR_L = ord("l")  # 108
_CHR_D = ord("d")  # 100
_CHR_E = ord("e")  # 101
_CHR_COLON = ord(":")  # 58
_DIGITS = frozenset(b"0123456789")


class BencodeError(Exception):
    """Base exception for bencode operations."""


class DecodeError(BencodeError):
    """Raised when bencoded data cannot be decoded."""


class EncodeError(BencodeError):
    """Raised when a value cannot be bencoded."""


# ===================================================================
# Decoding
# ===================================================================


def decode(data: bytes | bytearray | memoryview) -> BencodeValue:
    """Decode a single bencoded value from *data*.

    Raises :class:`DecodeError` on malformed input.
    """
    if not data:
        raise DecodeError("empty data")
    buf = memoryview(data) if not isinstance(data, memoryview) else data
    value, pos = _decode_any(buf, 0)
    if pos != len(buf):
        raise DecodeError(f"trailing data at position {pos}")
    return value


def decode_all(data: bytes | bytearray | memoryview) -> list[BencodeValue]:
    """Decode *all* concatenated bencoded values from *data*."""
    buf = memoryview(data) if not isinstance(data, memoryview) else data
    values: list[BencodeValue] = []
    pos = 0
    while pos < len(buf):
        value, pos = _decode_any(buf, pos)
        values.append(value)
    return values


# --- internal dispatch -----------------------------------------------------


def _decode_any(buf: memoryview, pos: int) -> tuple[BencodeValue, int]:
    """Dispatch to the correct decoder based on the leading byte."""
    if pos >= len(buf):
        raise DecodeError(f"unexpected end of data at position {pos}")

    lead = buf[pos]
    match lead:
        case b if b == _CHR_I:
            return _decode_int(buf, pos)
        case b if b == _CHR_L:
            return _decode_list(buf, pos)
        case b if b == _CHR_D:
            return _decode_dict(buf, pos)
        case b if b in _DIGITS:
            return _decode_bytes(buf, pos)
        case _:
            raise DecodeError(
                f"unexpected byte {chr(lead)!r} at position {pos}"
            )


def _decode_int(buf: memoryview, pos: int) -> tuple[int, int]:
    """Decode ``i<integer>e`` starting at *pos*."""
    # skip 'i'
    pos += 1
    end = _find_byte(buf, _CHR_E, pos)
    raw = bytes(buf[pos:end])

    # Validate: no leading zeros (except i0e), no empty, no i-0e
    if not raw:
        raise DecodeError("empty integer")
    if raw == b"-0":
        raise DecodeError("negative zero is not allowed")
    if len(raw) > 1 and raw[0:1] == b"0":
        raise DecodeError(f"leading zero in integer: {raw!r}")
    if len(raw) > 1 and raw[0:2] == b"-0":
        raise DecodeError(f"leading zero in negative integer: {raw!r}")

    try:
        value = int(raw)
    except ValueError as exc:
        raise DecodeError(f"invalid integer: {raw!r}") from exc

    return value, end + 1


def _decode_bytes(buf: memoryview, pos: int) -> tuple[bytes, int]:
    """Decode ``<length>:<data>`` starting at *pos*."""
    colon = _find_byte(buf, _CHR_COLON, pos)
    raw_len = bytes(buf[pos:colon])

    # No leading zeros in length (except "0:")
    if len(raw_len) > 1 and raw_len[0:1] == b"0":
        raise DecodeError(f"leading zero in string length: {raw_len!r}")

    try:
        length = int(raw_len)
    except ValueError as exc:
        raise DecodeError(f"invalid string length: {raw_len!r}") from exc

    start = colon + 1
    end = start + length
    if end > len(buf):
        raise DecodeError(
            f"string of length {length} extends past end of data "
            f"(position {start}, buffer length {len(buf)})"
        )
    return bytes(buf[start:end]), end


def _decode_list(buf: memoryview, pos: int) -> tuple[list[BencodeValue], int]:
    """Decode ``l<items>e`` starting at *pos*."""
    pos += 1  # skip 'l'
    items: list[BencodeValue] = []
    while True:
        if pos >= len(buf):
            raise DecodeError("unterminated list")
        if buf[pos] == _CHR_E:
            return items, pos + 1
        value, pos = _decode_any(buf, pos)
        items.append(value)


def _decode_dict(
    buf: memoryview, pos: int
) -> tuple[dict[bytes, BencodeValue], int]:
    """Decode ``d<key><value>...e`` starting at *pos*.

    Keys must be byte strings in sorted order per the spec.
    """
    pos += 1  # skip 'd'
    result: dict[bytes, BencodeValue] = {}
    prev_key: bytes | None = None

    while True:
        if pos >= len(buf):
            raise DecodeError("unterminated dict")
        if buf[pos] == _CHR_E:
            return result, pos + 1

        # Keys must be byte strings
        if buf[pos] not in _DIGITS:
            raise DecodeError(
                f"dict key must be a byte string, "
                f"got {chr(buf[pos])!r} at position {pos}"
            )
        key, pos = _decode_bytes(buf, pos)

        # Enforce sorted key order
        if prev_key is not None and key <= prev_key:
            raise DecodeError(
                f"dict keys out of order: {prev_key!r} >= {key!r}"
            )
        prev_key = key

        value, pos = _decode_any(buf, pos)
        result[key] = value


def _find_byte(buf: memoryview, byte: int, start: int) -> int:
    """Return the index of *byte* in *buf* starting from *start*."""
    for i in range(start, len(buf)):
        if buf[i] == byte:
            return i
    raise DecodeError(
        f"expected {chr(byte)!r} not found after position {start}"
    )


# ===================================================================
# Encoding
# ===================================================================


def encode(value: BencodeValue) -> bytes:
    """Encode a Python value into bencode format.

    Accepted types: ``int``, ``bytes``, ``list`` (or any ``Sequence``
    of bencodable values), ``dict`` (or any ``Mapping`` with ``bytes``
    keys and bencodable values).

    Raises :class:`EncodeError` on unsupported types.
    """
    parts: list[bytes] = []
    _encode_any(value, parts)
    return b"".join(parts)


def _encode_any(value: BencodeValue, parts: list[bytes]) -> None:
    """Encode *value*, appending byte fragments to *parts*."""
    match value:
        case int():
            _encode_int(value, parts)
        case bytes():
            _encode_bytes(value, parts)
        case list():
            _encode_list(value, parts)
        case dict():
            _encode_dict(value, parts)
        case _:
            raise EncodeError(f"unsupported type: {type(value).__name__}")


def _encode_int(value: int, parts: list[bytes]) -> None:
    parts.append(b"i")
    parts.append(str(value).encode("ascii"))
    parts.append(b"e")


def _encode_bytes(value: bytes, parts: list[bytes]) -> None:
    parts.append(str(len(value)).encode("ascii"))
    parts.append(b":")
    parts.append(value)


def _encode_list(value: Sequence[BencodeValue], parts: list[bytes]) -> None:
    parts.append(b"l")
    for item in value:
        _encode_any(item, parts)
    parts.append(b"e")


def _encode_dict(
    value: Mapping[bytes, BencodeValue], parts: list[bytes]
) -> None:
    parts.append(b"d")
    for key in sorted(value.keys()):
        if not isinstance(key, bytes):
            raise EncodeError(
                f"dict key must be bytes, got {type(key).__name__}"
            )
        _encode_bytes(key, parts)
        _encode_any(value[key], parts)
    parts.append(b"e")
