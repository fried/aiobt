"""Tests for Local Service Discovery (BEP 26)."""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from aiobt.discovery import (
    DiscoveredPeer,
    LSD_MCAST_ADDR_V4,
    LSD_PORT,
    LSDAnnounce,
    LocalDiscovery,
    _generate_cookie,
    format_announce,
    parse_announce,
)


# ---------------------------------------------------------------------------
# Cookie generation
# ---------------------------------------------------------------------------


class TestGenerateCookie:
    def test_returns_string(self) -> None:
        cookie = _generate_cookie()
        assert isinstance(cookie, str)

    def test_length(self) -> None:
        cookie = _generate_cookie()
        assert len(cookie) == 16

    def test_hex_chars(self) -> None:
        cookie = _generate_cookie()
        # Should be all hex characters
        int(cookie, 16)  # raises ValueError if not valid hex

    def test_unique(self) -> None:
        cookies = {_generate_cookie() for _ in range(100)}
        # Extremely unlikely to get duplicates with 16 hex chars
        assert len(cookies) == 100


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


class TestFormatAnnounce:
    def test_single_infohash(self) -> None:
        ih = bytes.fromhex("aabbccdd" * 5)
        msg = format_announce(
            listen_port=6881,
            info_hashes=(ih,),
            cookie="deadbeef12345678",
        )
        text = msg.decode("ascii")

        assert text.startswith("BT-SEARCH * HTTP/1.1\r\n")
        assert f"Host: {LSD_MCAST_ADDR_V4}:{LSD_PORT}\r\n" in text
        assert "Port: 6881\r\n" in text
        assert f"Infohash: {'aabbccdd' * 5}\r\n" in text
        assert "cookie: deadbeef12345678\r\n" in text
        assert text.endswith("\r\n\r\n")

    def test_multiple_infohashes(self) -> None:
        ih1 = bytes(20)
        ih2 = bytes(range(20))
        msg = format_announce(
            listen_port=51413,
            info_hashes=(ih1, ih2),
            cookie="abc123",
        )
        text = msg.decode("ascii")

        assert "Port: 51413\r\n" in text
        assert f"Infohash: {ih1.hex()}\r\n" in text
        assert f"Infohash: {ih2.hex()}\r\n" in text

    def test_custom_host(self) -> None:
        ih = bytes(20)
        msg = format_announce(
            listen_port=6881,
            info_hashes=(ih,),
            cookie="test",
            host="[ff15::efc0:988f]",
        )
        text = msg.decode("ascii")
        assert "Host: [ff15::efc0:988f]:6771\r\n" in text


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


class TestParseAnnounce:
    def _build_raw(
        self,
        port: int = 6881,
        info_hashes: tuple[bytes, ...] = (bytes(20),),
        cookie: str = "test",
    ) -> bytes:
        return format_announce(
            listen_port=port,
            info_hashes=info_hashes,
            cookie=cookie,
        )

    def test_single_hash(self) -> None:
        ih = hashlib.sha1(b"test").digest()
        raw = self._build_raw(port=7000, info_hashes=(ih,), cookie="mycookie")
        results = parse_announce(raw, "192.168.1.42")

        assert len(results) == 1
        ann = results[0]
        assert isinstance(ann, LSDAnnounce)
        assert ann.host == "192.168.1.42"
        assert ann.port == 7000
        assert ann.info_hash == ih
        assert ann.cookie == "mycookie"

    def test_multiple_hashes(self) -> None:
        ih1 = bytes(20)
        ih2 = bytes(range(20))
        raw = self._build_raw(info_hashes=(ih1, ih2))
        results = parse_announce(raw, "10.0.0.1")

        assert len(results) == 2
        assert results[0].info_hash == ih1
        assert results[1].info_hash == ih2

    def test_frozen(self) -> None:
        ih = bytes(20)
        raw = self._build_raw(info_hashes=(ih,))
        results = parse_announce(raw, "10.0.0.1")

        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            results[0].port = 9999  # type: ignore[misc]

    def test_malformed_returns_empty(self) -> None:
        assert parse_announce(b"garbage data", "1.2.3.4") == []
        assert parse_announce(b"", "1.2.3.4") == []

    def test_missing_port_returns_empty(self) -> None:
        raw = (
            b"BT-SEARCH * HTTP/1.1\r\n"
            b"Host: 239.192.152.143:6771\r\n"
            b"Infohash: " + (b"aa" * 20) + b"\r\n"
            b"cookie: test\r\n"
            b"\r\n"
        )
        assert parse_announce(raw, "1.2.3.4") == []

    def test_missing_infohash_returns_empty(self) -> None:
        raw = (
            b"BT-SEARCH * HTTP/1.1\r\n"
            b"Host: 239.192.152.143:6771\r\n"
            b"Port: 6881\r\n"
            b"cookie: test\r\n"
            b"\r\n"
        )
        assert parse_announce(raw, "1.2.3.4") == []

    def test_invalid_hex_hash_skipped(self) -> None:
        raw = (
            b"BT-SEARCH * HTTP/1.1\r\n"
            b"Host: 239.192.152.143:6771\r\n"
            b"Port: 6881\r\n"
            b"Infohash: not_hex_at_all_nope_xyz\r\n"
            b"Infohash: " + (b"bb" * 20) + b"\r\n"
            b"cookie: test\r\n"
            b"\r\n"
        )
        results = parse_announce(raw, "1.2.3.4")
        assert len(results) == 1
        assert results[0].info_hash == bytes.fromhex("bb" * 20)

    def test_wrong_length_hash_skipped(self) -> None:
        raw = (
            b"BT-SEARCH * HTTP/1.1\r\n"
            b"Host: 239.192.152.143:6771\r\n"
            b"Port: 6881\r\n"
            b"Infohash: aabb\r\n"  # too short
            b"Infohash: " + (b"cc" * 20) + b"\r\n"
            b"cookie: test\r\n"
            b"\r\n"
        )
        results = parse_announce(raw, "1.2.3.4")
        assert len(results) == 1
        assert results[0].info_hash == bytes.fromhex("cc" * 20)

    def test_case_insensitive_headers(self) -> None:
        raw = (
            b"BT-SEARCH * HTTP/1.1\r\n"
            b"HOST: 239.192.152.143:6771\r\n"
            b"PORT: 6881\r\n"
            b"INFOHASH: " + (b"dd" * 20) + b"\r\n"
            b"COOKIE: testcookie\r\n"
            b"\r\n"
        )
        results = parse_announce(raw, "1.2.3.4")
        assert len(results) == 1
        assert results[0].port == 6881
        assert results[0].cookie == "testcookie"


# ---------------------------------------------------------------------------
# DiscoveredPeer model
# ---------------------------------------------------------------------------


class TestDiscoveredPeer:
    def test_frozen(self) -> None:
        peer = DiscoveredPeer(
            host="192.168.1.100",
            port=51413,
            info_hash=bytes(20),
        )
        assert peer.host == "192.168.1.100"
        assert peer.port == 51413

        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            peer.host = "10.0.0.1"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LocalDiscovery — unit tests (no real multicast)
# ---------------------------------------------------------------------------


class TestLocalDiscovery:
    def test_announce_validates_length(self) -> None:
        lsd = LocalDiscovery(listen_port=6881)
        with pytest.raises(ValueError, match="20 bytes"):
            lsd.announce(b"short")

    def test_announce_and_withdraw(self) -> None:
        lsd = LocalDiscovery(listen_port=6881)
        ih = bytes(20)
        lsd.announce(ih)
        assert ih in lsd.active_hashes

        lsd.withdraw(ih)
        assert ih not in lsd.active_hashes

    def test_withdraw_nonexistent_no_error(self) -> None:
        lsd = LocalDiscovery(listen_port=6881)
        lsd.withdraw(bytes(20))  # should not raise

    def test_active_hashes_is_frozenset(self) -> None:
        lsd = LocalDiscovery(listen_port=6881)
        lsd.announce(bytes(20))
        hashes = lsd.active_hashes
        assert isinstance(hashes, frozenset)

    def test_cookie_unique_per_instance(self) -> None:
        lsd1 = LocalDiscovery(listen_port=6881)
        lsd2 = LocalDiscovery(listen_port=6881)
        assert lsd1._cookie != lsd2._cookie


# ---------------------------------------------------------------------------
# Round-trip: format → parse
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_single_hash_round_trip(self) -> None:
        ih = hashlib.sha1(b"archlinux.iso").digest()
        raw = format_announce(
            listen_port=51413,
            info_hashes=(ih,),
            cookie="roundtrip_test",
        )
        results = parse_announce(raw, "192.168.1.200")

        assert len(results) == 1
        assert results[0].port == 51413
        assert results[0].info_hash == ih
        assert results[0].cookie == "roundtrip_test"
        assert results[0].host == "192.168.1.200"

    def test_multi_hash_round_trip(self) -> None:
        hashes = tuple(hashlib.sha1(f"torrent-{i}".encode()).digest() for i in range(5))
        raw = format_announce(
            listen_port=6881,
            info_hashes=hashes,
            cookie="multi",
        )
        results = parse_announce(raw, "10.0.0.50")

        assert len(results) == 5
        for i, result in enumerate(results):
            assert result.info_hash == hashes[i]
            assert result.port == 6881
            assert result.cookie == "multi"


import attrs  # noqa: E402 — needed for FrozenInstanceError checks above
