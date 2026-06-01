# cython: language_level=3str
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""Bencode encoding and decoding — Cython-optimized variant.

This module has the **same public interface** as ``bencode.py``.
When compiled, Python's import system prefers the ``.so`` over
the ``.py`` fallback automatically.

Cython constraints: no ``match``, no PEP 695 ``type``, no walrus
in complex expressions.  Use ``cdef`` locals and typed memoryview
access for speed.
"""

from collections.abc import Mapping, Sequence
from typing import Union

# Public type alias — Cython-compatible form
BencodeValue = Union[int, bytes, "list[BencodeValue]", "dict[bytes, BencodeValue]"]

# Sentinels — typed as C ints for fast comparison
cdef int _CHR_I = 105      # ord("i")
cdef int _CHR_L = 108      # ord("l")
cdef int _CHR_D = 100      # ord("d")
cdef int _CHR_E = 101      # ord("e")
cdef int _CHR_COLON = 58   # ord(":")
cdef int _CHR_0 = 48       # ord("0")
cdef int _CHR_9 = 57       # ord("9")


class BencodeError(Exception):
    """Base exception for bencode operations."""


class DecodeError(BencodeError):
    """Raised when bencoded data cannot be decoded."""


class EncodeError(BencodeError):
    """Raised when a value cannot be bencoded."""


# ===================================================================
# Decoding
# ===================================================================


def decode(data):
    """Decode a single bencoded value from *data*.

    Raises :class:`DecodeError` on malformed input.
    """
    if not data:
        raise DecodeError("empty data")
    cdef const unsigned char[:] buf = _to_buffer(data)
    cdef Py_ssize_t buf_len = len(buf)
    cdef Py_ssize_t pos = 0
    value, pos = _decode_any(buf, buf_len, pos)
    if pos != buf_len:
        raise DecodeError(f"trailing data at position {pos}")
    return value


def decode_all(data):
    """Decode *all* concatenated bencoded values from *data*."""
    cdef const unsigned char[:] buf = _to_buffer(data)
    cdef Py_ssize_t buf_len = len(buf)
    cdef Py_ssize_t pos = 0
    values = []
    while pos < buf_len:
        value, pos = _decode_any(buf, buf_len, pos)
        values.append(value)
    return values


cdef inline const unsigned char[:] _to_buffer(data):
    """Coerce input to a typed memoryview."""
    if isinstance(data, memoryview):
        return data.cast('B')
    return data


# --- internal dispatch -----------------------------------------------------


cdef _decode_any(const unsigned char[:] buf, Py_ssize_t buf_len, Py_ssize_t pos):
    """Dispatch to the correct decoder based on the leading byte."""
    cdef unsigned char lead
    if pos >= buf_len:
        raise DecodeError(f"unexpected end of data at position {pos}")

    lead = buf[pos]
    if lead == _CHR_I:
        return _decode_int(buf, buf_len, pos)
    elif lead == _CHR_L:
        return _decode_list(buf, buf_len, pos)
    elif lead == _CHR_D:
        return _decode_dict(buf, buf_len, pos)
    elif _CHR_0 <= lead <= _CHR_9:
        return _decode_bytes(buf, buf_len, pos)
    else:
        raise DecodeError(f"unexpected byte {chr(lead)!r} at position {pos}")


cdef _decode_int(const unsigned char[:] buf, Py_ssize_t buf_len, Py_ssize_t pos):
    """Decode ``i<integer>e`` starting at *pos*."""
    cdef Py_ssize_t end
    pos += 1  # skip 'i'
    end = _find_byte(buf, buf_len, _CHR_E, pos)
    raw = bytes(buf[pos:end])

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


cdef _decode_bytes(const unsigned char[:] buf, Py_ssize_t buf_len, Py_ssize_t pos):
    """Decode ``<length>:<data>`` starting at *pos*."""
    cdef Py_ssize_t colon, length, start, end

    colon = _find_byte(buf, buf_len, _CHR_COLON, pos)
    raw_len = bytes(buf[pos:colon])

    if len(raw_len) > 1 and raw_len[0:1] == b"0":
        raise DecodeError(f"leading zero in string length: {raw_len!r}")

    try:
        length = int(raw_len)
    except ValueError as exc:
        raise DecodeError(f"invalid string length: {raw_len!r}") from exc

    start = colon + 1
    end = start + length
    if end > buf_len:
        raise DecodeError(
            f"string of length {length} extends past end of data "
            f"(position {start}, buffer length {buf_len})"
        )
    return bytes(buf[start:end]), end


cdef _decode_list(const unsigned char[:] buf, Py_ssize_t buf_len, Py_ssize_t pos):
    """Decode ``l<items>e`` starting at *pos*."""
    pos += 1  # skip 'l'
    items = []
    while True:
        if pos >= buf_len:
            raise DecodeError("unterminated list")
        if buf[pos] == _CHR_E:
            return items, pos + 1
        value, pos = _decode_any(buf, buf_len, pos)
        items.append(value)


cdef _decode_dict(const unsigned char[:] buf, Py_ssize_t buf_len, Py_ssize_t pos):
    """Decode ``d<key><value>...e`` starting at *pos*."""
    pos += 1  # skip 'd'
    result = {}
    prev_key = None

    while True:
        if pos >= buf_len:
            raise DecodeError("unterminated dict")
        if buf[pos] == _CHR_E:
            return result, pos + 1

        if not (_CHR_0 <= buf[pos] <= _CHR_9):
            raise DecodeError(
                f"dict key must be a byte string, "
                f"got {chr(buf[pos])!r} at position {pos}"
            )
        key, pos = _decode_bytes(buf, buf_len, pos)

        if prev_key is not None and key <= prev_key:
            raise DecodeError(f"dict keys out of order: {prev_key!r} >= {key!r}")
        prev_key = key

        value, pos = _decode_any(buf, buf_len, pos)
        result[key] = value


cdef inline Py_ssize_t _find_byte(
    const unsigned char[:] buf,
    Py_ssize_t buf_len,
    int byte,
    Py_ssize_t start,
) except -1:
    """Return the index of *byte* in *buf* starting from *start*."""
    cdef Py_ssize_t i
    for i in range(start, buf_len):
        if buf[i] == byte:
            return i
    raise DecodeError(f"expected {chr(byte)!r} not found after position {start}")


# ===================================================================
# Encoding
# ===================================================================


def encode(value) -> bytes:
    """Encode a Python value into bencode format.

    Accepted types: ``int``, ``bytes``, ``list`` (or any ``Sequence``
    of bencodable values), ``dict`` (or any ``Mapping`` with ``bytes``
    keys and bencodable values).

    Raises :class:`EncodeError` on unsupported types.
    """
    parts = []
    _encode_any(value, parts)
    return b"".join(parts)


cdef _encode_any(object value, list parts):
    """Encode *value*, appending byte fragments to *parts*."""
    if isinstance(value, int):
        _encode_int(value, parts)
    elif isinstance(value, bytes):
        _encode_bytes(value, parts)
    elif isinstance(value, list):
        _encode_list(value, parts)
    elif isinstance(value, dict):
        _encode_dict(value, parts)
    else:
        raise EncodeError(f"unsupported type: {type(value).__name__}")


cdef inline _encode_int(object value, list parts):
    parts.append(b"i" + str(value).encode("ascii") + b"e")


cdef inline _encode_bytes(bytes value, list parts):
    parts.append(str(len(value)).encode("ascii") + b":" + value)


cdef _encode_list(object value, list parts):
    parts.append(b"l")
    for item in value:
        _encode_any(item, parts)
    parts.append(b"e")


cdef _encode_dict(object value, list parts):
    parts.append(b"d")
    for key in sorted(value.keys()):
        if not isinstance(key, bytes):
            raise EncodeError(f"dict key must be bytes, got {type(key).__name__}")
        _encode_bytes(key, parts)
        _encode_any(value[key], parts)
    parts.append(b"e")
