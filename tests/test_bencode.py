"""Tests for aiobt.bencode — encode and decode."""

from __future__ import annotations

import pytest

from aiobt.bencode import (
    BencodeError,
    DecodeError,
    EncodeError,
    decode,
    decode_all,
    encode,
)


# ===================================================================
# Integers
# ===================================================================


class TestDecodeInt:
    def test_zero(self) -> None:
        assert decode(b"i0e") == 0

    def test_positive(self) -> None:
        assert decode(b"i42e") == 42

    def test_negative(self) -> None:
        assert decode(b"i-7e") == -7

    def test_large(self) -> None:
        assert decode(b"i9999999999999e") == 9999999999999

    def test_negative_zero_rejected(self) -> None:
        with pytest.raises(DecodeError, match="negative zero"):
            decode(b"i-0e")

    def test_leading_zero_rejected(self) -> None:
        with pytest.raises(DecodeError, match="leading zero"):
            decode(b"i03e")

    def test_empty_integer_rejected(self) -> None:
        with pytest.raises(DecodeError):
            decode(b"ie")


class TestEncodeInt:
    def test_zero(self) -> None:
        assert encode(0) == b"i0e"

    def test_positive(self) -> None:
        assert encode(42) == b"i42e"

    def test_negative(self) -> None:
        assert encode(-7) == b"i-7e"


# ===================================================================
# Byte strings
# ===================================================================


class TestDecodeBytes:
    def test_empty(self) -> None:
        assert decode(b"0:") == b""

    def test_simple(self) -> None:
        assert decode(b"4:spam") == b"spam"

    def test_binary(self) -> None:
        assert decode(b"3:\x00\x01\x02") == b"\x00\x01\x02"

    def test_length_mismatch(self) -> None:
        with pytest.raises(DecodeError, match="extends past"):
            decode(b"10:short")

    def test_leading_zero_length_rejected(self) -> None:
        with pytest.raises(DecodeError, match="leading zero"):
            decode(b"04:spam")


class TestEncodeBytes:
    def test_empty(self) -> None:
        assert encode(b"") == b"0:"

    def test_simple(self) -> None:
        assert encode(b"spam") == b"4:spam"

    def test_binary(self) -> None:
        assert encode(b"\xff\x00") == b"2:\xff\x00"


# ===================================================================
# Lists
# ===================================================================


class TestDecodeList:
    def test_empty(self) -> None:
        assert decode(b"le") == []

    def test_ints(self) -> None:
        assert decode(b"li1ei2ei3ee") == [1, 2, 3]

    def test_mixed(self) -> None:
        assert decode(b"li42e4:spame") == [42, b"spam"]

    def test_nested(self) -> None:
        assert decode(b"lli1eeli2eee") == [[1], [2]]

    def test_unterminated(self) -> None:
        with pytest.raises(DecodeError, match="unterminated"):
            decode(b"li1e")


class TestEncodeList:
    def test_empty(self) -> None:
        assert encode([]) == b"le"

    def test_ints(self) -> None:
        assert encode([1, 2, 3]) == b"li1ei2ei3ee"

    def test_nested(self) -> None:
        assert encode([[1], [2]]) == b"lli1eeli2eee"


# ===================================================================
# Dictionaries
# ===================================================================


class TestDecodeDict:
    def test_empty(self) -> None:
        assert decode(b"de") == {}

    def test_simple(self) -> None:
        assert decode(b"d3:cow3:moo4:spam4:eggse") == {
            b"cow": b"moo",
            b"spam": b"eggs",
        }

    def test_nested(self) -> None:
        result = decode(b"d4:dictd3:keyi42eee")
        assert result == {b"dict": {b"key": 42}}

    def test_keys_must_be_sorted(self) -> None:
        # "z" comes after "a" — this should be valid
        assert decode(b"d1:ai1e1:zi2ee") == {b"a": 1, b"z": 2}

    def test_unsorted_keys_rejected(self) -> None:
        with pytest.raises(DecodeError, match="out of order"):
            decode(b"d1:zi1e1:ai2ee")

    def test_duplicate_keys_rejected(self) -> None:
        with pytest.raises(DecodeError, match="out of order"):
            decode(b"d1:ai1e1:ai2ee")


class TestEncodeDict:
    def test_empty(self) -> None:
        assert encode({}) == b"de"

    def test_sorts_keys(self) -> None:
        result = encode({b"z": 1, b"a": 2})
        assert result == b"d1:ai2e1:zi1ee"

    def test_non_bytes_key_rejected(self) -> None:
        with pytest.raises(EncodeError, match="dict key must be bytes"):
            encode({"string_key": 1})  # type: ignore[dict-item]


# ===================================================================
# Round-trip
# ===================================================================


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            0,
            -1,
            42,
            b"",
            b"hello",
            [],
            [1, b"two", [3]],
            {},
            {b"key": b"value"},
            {b"a": {b"b": [1, 2, 3]}},
        ],
    )
    def test_round_trip(self, value: object) -> None:
        assert decode(encode(value)) == value  # type: ignore[arg-type]


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    def test_empty_data(self) -> None:
        with pytest.raises(DecodeError, match="empty data"):
            decode(b"")

    def test_trailing_data(self) -> None:
        with pytest.raises(DecodeError, match="trailing data"):
            decode(b"i42ei0e")

    def test_decode_all(self) -> None:
        result = decode_all(b"i1ei2ei3e")
        assert result == [1, 2, 3]

    def test_memoryview_input(self) -> None:
        data = memoryview(b"4:test")
        assert decode(data) == b"test"

    def test_bytearray_input(self) -> None:
        data = bytearray(b"i99e")
        assert decode(data) == 99
