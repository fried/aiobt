"""Tests for aiobt.tracker — HTTP and UDP tracker protocol."""

from __future__ import annotations

import asyncio
import struct
import unittest

from aiobt.tracker import (
    AnnounceRequest,
    TrackerError,
    _ACTION_ANNOUNCE,
    _ACTION_CONNECT,
    _ACTION_ERROR,
    _EVENT_COMPLETED,
    _EVENT_MAP,
    _EVENT_NONE,
    _EVENT_STARTED,
    _EVENT_STOPPED,
    _UDPTrackerProtocol,
    _new_transaction_id,
    _parse_compact_peers,
    _parse_dict_peers,
    _parse_http_response,
    _parse_udp_announce_response,
    parse_tracker_url,
    udp_connect,
)
from aiobt.bencode import encode

# ---------------------------------------------------------------------------
# AnnounceRequest model tests
# ---------------------------------------------------------------------------


class TestAnnounceRequest(unittest.TestCase):
    """Test AnnounceRequest dataclass."""

    def test_defaults(self) -> None:
        req = AnnounceRequest(
            info_hash=b"\x00" * 20,
            peer_id=b"\x01" * 20,
            port=6881,
        )
        self.assertEqual(req.uploaded, 0)
        self.assertEqual(req.downloaded, 0)
        self.assertEqual(req.left, 0)
        self.assertTrue(req.compact)
        self.assertEqual(req.event, "")
        self.assertEqual(req.numwant, 50)
        # key should be a random 32-bit int
        self.assertIsInstance(req.key, int)
        self.assertGreaterEqual(req.key, 0)
        self.assertLessEqual(req.key, 0xFFFFFFFF)

    def test_custom_event(self) -> None:
        req = AnnounceRequest(
            info_hash=b"\x00" * 20,
            peer_id=b"\x01" * 20,
            port=6881,
            event="started",
        )
        self.assertEqual(req.event, "started")


# ---------------------------------------------------------------------------
# Compact peer parsing
# ---------------------------------------------------------------------------


class TestParseCompactPeers(unittest.TestCase):
    """Test _parse_compact_peers."""

    def test_single_peer(self) -> None:
        # 192.168.1.1:6881
        data = bytes([192, 168, 1, 1]) + struct.pack("!H", 6881)
        peers = _parse_compact_peers(data)
        self.assertEqual(peers, [("192.168.1.1", 6881)])

    def test_multiple_peers(self) -> None:
        data = b""
        data += bytes([10, 0, 0, 1]) + struct.pack("!H", 6881)
        data += bytes([10, 0, 0, 2]) + struct.pack("!H", 6882)
        data += bytes([10, 0, 0, 3]) + struct.pack("!H", 51413)
        peers = _parse_compact_peers(data)
        self.assertEqual(len(peers), 3)
        self.assertEqual(peers[0], ("10.0.0.1", 6881))
        self.assertEqual(peers[1], ("10.0.0.2", 6882))
        self.assertEqual(peers[2], ("10.0.0.3", 51413))

    def test_empty(self) -> None:
        self.assertEqual(_parse_compact_peers(b""), [])

    def test_truncated_ignored(self) -> None:
        # 5 bytes — not enough for a full peer entry
        data = bytes([10, 0, 0, 1, 0])
        peers = _parse_compact_peers(data)
        self.assertEqual(peers, [])


# ---------------------------------------------------------------------------
# Dict peer parsing
# ---------------------------------------------------------------------------


class TestParseDictPeers(unittest.TestCase):
    """Test _parse_dict_peers."""

    def test_single_peer(self) -> None:
        peers = _parse_dict_peers([{b"ip": b"192.168.1.1", b"port": 6881}])
        self.assertEqual(peers, [("192.168.1.1", 6881)])

    def test_skips_invalid(self) -> None:
        peers = _parse_dict_peers(
            [
                {b"ip": b"10.0.0.1", b"port": 6881},
                "garbage",
                {b"ip": 123, b"port": 456},  # ip not bytes
                {b"ip": b"10.0.0.2", b"port": 6882},
            ]
        )
        self.assertEqual(len(peers), 2)

    def test_empty(self) -> None:
        self.assertEqual(_parse_dict_peers([]), [])


# ---------------------------------------------------------------------------
# HTTP response parsing
# ---------------------------------------------------------------------------


class TestParseHttpResponse(unittest.TestCase):
    """Test _parse_http_response."""

    def test_compact_response(self) -> None:
        peer_data = bytes([10, 0, 0, 1]) + struct.pack("!H", 6881)
        resp_dict = {
            b"interval": 1800,
            b"peers": peer_data,
            b"complete": 10,
            b"incomplete": 5,
        }
        resp = _parse_http_response(encode(resp_dict))
        self.assertEqual(resp.interval, 1800)
        self.assertEqual(resp.peers, (("10.0.0.1", 6881),))
        self.assertEqual(resp.complete, 10)
        self.assertEqual(resp.incomplete, 5)

    def test_dict_format_peers(self) -> None:
        resp_dict = {
            b"interval": 900,
            b"peers": [
                {b"ip": b"10.0.0.1", b"port": 6881},
            ],
        }
        resp = _parse_http_response(encode(resp_dict))
        self.assertEqual(resp.peers, (("10.0.0.1", 6881),))

    def test_failure_reason(self) -> None:
        resp_dict = {b"failure reason": b"info_hash not found"}
        with self.assertRaises(TrackerError) as ctx:
            _parse_http_response(encode(resp_dict))
        self.assertIn("info_hash not found", str(ctx.exception))

    def test_missing_interval(self) -> None:
        resp_dict = {b"peers": b""}
        from aiobt.bencode import DecodeError

        with self.assertRaises(DecodeError):
            _parse_http_response(encode(resp_dict))

    def test_not_a_dict(self) -> None:
        from aiobt.bencode import DecodeError

        with self.assertRaises(DecodeError):
            _parse_http_response(encode(42))


# ---------------------------------------------------------------------------
# Event map
# ---------------------------------------------------------------------------


class TestEventMap(unittest.TestCase):
    """Test UDP event code mapping."""

    def test_empty_is_none(self) -> None:
        self.assertEqual(_EVENT_MAP[""], _EVENT_NONE)

    def test_started(self) -> None:
        self.assertEqual(_EVENT_MAP["started"], _EVENT_STARTED)

    def test_completed(self) -> None:
        self.assertEqual(_EVENT_MAP["completed"], _EVENT_COMPLETED)

    def test_stopped(self) -> None:
        self.assertEqual(_EVENT_MAP["stopped"], _EVENT_STOPPED)


# ---------------------------------------------------------------------------
# Transaction ID
# ---------------------------------------------------------------------------


class TestNewTransactionId(unittest.TestCase):
    def test_is_32_bit(self) -> None:
        for _ in range(100):
            tid = _new_transaction_id()
            self.assertGreaterEqual(tid, 0)
            self.assertLessEqual(tid, 0xFFFFFFFF)

    def test_randomness(self) -> None:
        """Multiple IDs should not all be the same."""
        ids = {_new_transaction_id() for _ in range(50)}
        self.assertGreater(len(ids), 1)


# ---------------------------------------------------------------------------
# UDP protocol object
# ---------------------------------------------------------------------------


class TestUDPTrackerProtocol(unittest.TestCase):
    """Test _UDPTrackerProtocol in isolation."""

    def test_expect_and_receive(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            proto = _UDPTrackerProtocol()

            async def run() -> None:
                tid = 42
                fut = proto.expect(tid)
                # Simulate receiving a response
                data = struct.pack("!II", _ACTION_CONNECT, tid) + b"\x00" * 8
                proto.datagram_received(data, ("127.0.0.1", 6969))
                result = await fut
                self.assertEqual(result, data)

            loop.run_until_complete(run())
        finally:
            loop.close()

    def test_wrong_tid_ignored(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            proto = _UDPTrackerProtocol()

            async def run() -> None:
                tid = 42
                fut = proto.expect(tid)
                # Wrong transaction ID — should not resolve the future
                wrong_data = struct.pack("!II", 0, 99) + b"\x00" * 8
                proto.datagram_received(wrong_data, ("127.0.0.1", 6969))
                self.assertFalse(fut.done())
                proto.cancel(tid)

            loop.run_until_complete(run())
        finally:
            loop.close()

    def test_short_data_ignored(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            proto = _UDPTrackerProtocol()

            async def run() -> None:
                tid = 42
                fut = proto.expect(tid)
                proto.datagram_received(b"\x00\x01\x02", ("127.0.0.1", 6969))
                self.assertFalse(fut.done())
                proto.cancel(tid)

            loop.run_until_complete(run())
        finally:
            loop.close()

    def test_cancel(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            proto = _UDPTrackerProtocol()

            async def run() -> None:
                tid = 42
                fut = proto.expect(tid)
                proto.cancel(tid)
                self.assertTrue(fut.cancelled())

            loop.run_until_complete(run())
        finally:
            loop.close()

    def test_error_received_wakes_waiters(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            proto = _UDPTrackerProtocol()

            async def run() -> None:
                tid = 42
                fut = proto.expect(tid)
                proto.error_received(OSError("network down"))
                self.assertTrue(fut.done())
                with self.assertRaises(OSError):
                    fut.result()

            loop.run_until_complete(run())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# UDP announce response parsing
# ---------------------------------------------------------------------------


class TestParseUDPAnnounceResponse(unittest.TestCase):
    """Test _parse_udp_announce_response."""

    def _make_response(
        self,
        action: int,
        tid: int,
        interval: int,
        leechers: int,
        seeders: int,
        peer_data: bytes = b"",
    ) -> bytes:
        return (
            struct.pack("!IIIII", action, tid, interval, leechers, seeders) + peer_data
        )

    def test_basic(self) -> None:
        peer_data = bytes([10, 0, 0, 1]) + struct.pack("!H", 6881)
        data = self._make_response(_ACTION_ANNOUNCE, 123, 1800, 5, 10, peer_data)
        resp = _parse_udp_announce_response(data, 123)
        self.assertEqual(resp.interval, 1800)
        self.assertEqual(resp.incomplete, 5)
        self.assertEqual(resp.complete, 10)
        self.assertEqual(resp.peers, (("10.0.0.1", 6881),))

    def test_no_peers(self) -> None:
        data = self._make_response(_ACTION_ANNOUNCE, 456, 900, 0, 0)
        resp = _parse_udp_announce_response(data, 456)
        self.assertEqual(resp.peers, ())
        self.assertEqual(resp.interval, 900)

    def test_tid_mismatch(self) -> None:
        data = self._make_response(_ACTION_ANNOUNCE, 999, 900, 0, 0)
        with self.assertRaises(TrackerError):
            _parse_udp_announce_response(data, 123)

    def test_error_action(self) -> None:
        data = struct.pack("!II", _ACTION_ERROR, 123) + b"something broke"
        with self.assertRaises(TrackerError) as ctx:
            _parse_udp_announce_response(data, 123)
        self.assertIn("something broke", str(ctx.exception))

    def test_unexpected_action(self) -> None:
        data = self._make_response(_ACTION_CONNECT, 123, 900, 0, 0)
        with self.assertRaises(TrackerError):
            _parse_udp_announce_response(data, 123)

    def test_too_short(self) -> None:
        data = struct.pack("!II", _ACTION_ANNOUNCE, 123)
        with self.assertRaises(TrackerError):
            _parse_udp_announce_response(data, 123)

    def test_multiple_peers(self) -> None:
        peer_data = b""
        for i in range(5):
            peer_data += bytes([10, 0, 0, i + 1]) + struct.pack("!H", 6881 + i)
        data = self._make_response(_ACTION_ANNOUNCE, 42, 600, 2, 5, peer_data)
        resp = _parse_udp_announce_response(data, 42)
        self.assertEqual(len(resp.peers), 5)


# ---------------------------------------------------------------------------
# UDP connect response parsing (via loopback fake server)
# ---------------------------------------------------------------------------


class TestUDPConnect(unittest.TestCase):
    """Test udp_connect against a local fake tracker."""

    def test_connect_success(self) -> None:
        """Simulate a successful connect handshake."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._connect_success())
        finally:
            loop.close()

    async def _connect_success(self) -> None:
        proto = _UDPTrackerProtocol()
        connection_id = 0xDEADBEEF12345678

        # We need to intercept what udp_connect sends, so we mock
        # the protocol's send + expect
        captured_payload: bytes | None = None

        def fake_send(data: bytes) -> None:
            nonlocal captured_payload
            captured_payload = data

        proto.send = fake_send  # type: ignore[assignment]

        # Run connect in a task, then simulate the server response
        async def fake_server() -> None:
            # Wait for the connect request to be sent
            while captured_payload is None:
                await asyncio.sleep(0.01)

            # Parse the request to extract the transaction ID
            _magic, _action, tid = struct.unpack("!QII", captured_payload[:16])

            # Build the connect response
            response = struct.pack("!IIQ", _ACTION_CONNECT, tid, connection_id)

            # Deliver it
            proto.datagram_received(response, ("127.0.0.1", 6969))

        server_task = asyncio.create_task(fake_server())
        result = await udp_connect(proto)
        await server_task

        self.assertEqual(result, connection_id)

    def test_connect_error(self) -> None:
        """Tracker returns an error action on connect."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._connect_error())
        finally:
            loop.close()

    async def _connect_error(self) -> None:
        proto = _UDPTrackerProtocol()
        captured_payload: bytes | None = None

        def fake_send(data: bytes) -> None:
            nonlocal captured_payload
            captured_payload = data

        proto.send = fake_send  # type: ignore[assignment]

        async def fake_server() -> None:
            while captured_payload is None:
                await asyncio.sleep(0.01)
            _magic, _action, tid = struct.unpack("!QII", captured_payload[:16])
            response = struct.pack("!II", _ACTION_ERROR, tid) + b"go away"
            proto.datagram_received(response, ("127.0.0.1", 6969))

        server_task = asyncio.create_task(fake_server())
        with self.assertRaises(TrackerError) as ctx:
            await udp_connect(proto)
        await server_task
        self.assertIn("go away", str(ctx.exception))


# ---------------------------------------------------------------------------
# parse_tracker_url
# ---------------------------------------------------------------------------


class TestParseTrackerUrl(unittest.TestCase):
    """Test parse_tracker_url."""

    def test_http(self) -> None:
        scheme, host, port = parse_tracker_url("http://tracker.example.com/announce")
        self.assertEqual(scheme, "http")
        self.assertEqual(host, "tracker.example.com")
        self.assertEqual(port, 80)

    def test_https(self) -> None:
        scheme, host, port = parse_tracker_url("https://tracker.example.com/announce")
        self.assertEqual(scheme, "https")
        self.assertEqual(port, 443)

    def test_udp_default_port(self) -> None:
        scheme, host, port = parse_tracker_url("udp://tracker.example.com/announce")
        self.assertEqual(scheme, "udp")
        self.assertEqual(port, 6969)

    def test_explicit_port(self) -> None:
        scheme, host, port = parse_tracker_url(
            "udp://tracker.example.com:1337/announce"
        )
        self.assertEqual(port, 1337)

    def test_http_explicit_port(self) -> None:
        scheme, host, port = parse_tracker_url(
            "http://tracker.example.com:8080/announce"
        )
        self.assertEqual(port, 8080)

    def test_no_hostname(self) -> None:
        with self.assertRaises(ValueError):
            parse_tracker_url("udp:///announce")

    def test_ip_address(self) -> None:
        scheme, host, port = parse_tracker_url("udp://192.168.1.1:6969/announce")
        self.assertEqual(host, "192.168.1.1")
        self.assertEqual(port, 6969)


if __name__ == "__main__":
    unittest.main()
