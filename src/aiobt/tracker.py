"""Tracker announce — HTTP and UDP (BEP 3, BEP 15).

Handles communication with BitTorrent trackers to obtain peer lists.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import urllib.parse
from collections.abc import Sequence
from random import randint

from dataclasses import dataclass, field

from .bencode import decode, DecodeError

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

type PeerAddress = tuple[str, int]
"""(host, port) tuple for a peer."""


@dataclass(frozen=True, slots=True)
class AnnounceRequest:
    """Parameters for a tracker announce."""

    info_hash: bytes
    peer_id: bytes
    port: int
    uploaded: int = 0
    downloaded: int = 0
    left: int = 0
    compact: bool = True
    event: str = ""  # "started", "completed", "stopped", or ""
    numwant: int = 50
    key: int = field(default_factory=lambda: randint(0, 0xFFFFFFFF))
    """Random key identifying this client to the tracker (BEP 15)."""


@dataclass(frozen=True, slots=True)
class AnnounceResponse:
    """Parsed tracker announce response."""

    interval: int
    """Seconds between re-announces."""

    peers: tuple[PeerAddress, ...]
    """Peer addresses returned by the tracker."""

    complete: int = 0
    """Number of seeders (optional)."""

    incomplete: int = 0
    """Number of leechers (optional)."""


class TrackerError(Exception):
    """Raised when a tracker returns an error."""


# ---------------------------------------------------------------------------
# UDP tracker constants (BEP 15)
# ---------------------------------------------------------------------------

_UDP_MAGIC: int = 0x41727101980

# Action codes
_ACTION_CONNECT: int = 0
_ACTION_ANNOUNCE: int = 1
_ACTION_SCRAPE: int = 2
_ACTION_ERROR: int = 3

# Event codes for UDP announce
_EVENT_NONE: int = 0
_EVENT_COMPLETED: int = 1
_EVENT_STARTED: int = 2
_EVENT_STOPPED: int = 3

_EVENT_MAP: dict[str, int] = {
    "": _EVENT_NONE,
    "completed": _EVENT_COMPLETED,
    "started": _EVENT_STARTED,
    "stopped": _EVENT_STOPPED,
}

# BEP 15 timeout: 15 * 2^n seconds, n = 0..8
_UDP_BASE_TIMEOUT: float = 15.0
_UDP_MAX_RETRIES: int = 8

# Connection ID expiry (BEP 15: 1 minute)
_CONNECTION_ID_LIFETIME: float = 60.0


# ---------------------------------------------------------------------------
# HTTP tracker (BEP 3)
# ---------------------------------------------------------------------------


async def http_announce(
    url: str,
    request: AnnounceRequest,
) -> AnnounceResponse:
    """Perform an HTTP tracker announce.

    Uses :mod:`urllib.request` in the default executor to avoid
    pulling in ``aiohttp`` as a dependency.
    """
    params = {
        "info_hash": request.info_hash,
        "peer_id": request.peer_id,
        "port": str(request.port),
        "uploaded": str(request.uploaded),
        "downloaded": str(request.downloaded),
        "left": str(request.left),
        "compact": "1" if request.compact else "0",
        "numwant": str(request.numwant),
    }
    if request.event:
        params["event"] = request.event

    query = urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote  # type: ignore[arg-type]
    )
    full_url = f"{url}?{query}"

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _sync_http_get, full_url)

    return _parse_http_response(data)


def _sync_http_get(url: str) -> bytes:
    """Blocking HTTP GET — runs in executor."""
    import urllib.request

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "aiobt/0.1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _parse_http_response(data: bytes) -> AnnounceResponse:
    """Parse a bencoded HTTP tracker response."""
    decoded = decode(data)
    if not isinstance(decoded, dict):
        raise DecodeError("tracker response is not a dict")

    # Check for tracker error
    failure = decoded.get(b"failure reason")
    if failure is not None:
        reason = (
            failure.decode("utf-8", errors="replace")
            if isinstance(failure, bytes)
            else str(failure)
        )
        raise TrackerError(reason)

    interval_val = decoded.get(b"interval")
    if not isinstance(interval_val, int):
        raise DecodeError("missing or invalid 'interval'")

    peers_raw = decoded.get(b"peers", b"")
    peers: list[PeerAddress]

    if isinstance(peers_raw, bytes):
        # Compact format: 6 bytes per peer (4 IP + 2 port)
        peers = _parse_compact_peers(peers_raw)
    elif isinstance(peers_raw, list):
        # Dictionary format
        peers = _parse_dict_peers(peers_raw)  # type: ignore[arg-type]
    else:
        peers = []

    complete = decoded.get(b"complete", 0)
    incomplete = decoded.get(b"incomplete", 0)

    return AnnounceResponse(
        interval=interval_val,
        peers=tuple(peers),
        complete=complete if isinstance(complete, int) else 0,
        incomplete=incomplete if isinstance(incomplete, int) else 0,
    )


def _parse_compact_peers(data: bytes) -> list[PeerAddress]:
    """Parse compact peer format (6 bytes per peer)."""
    peers: list[PeerAddress] = []
    for i in range(0, len(data), 6):
        if i + 6 > len(data):
            break
        ip = ".".join(str(b) for b in data[i : i + 4])
        port = struct.unpack("!H", data[i + 4 : i + 6])[0]
        peers.append((ip, port))
    return peers


def _parse_dict_peers(peers_list: list[object]) -> list[PeerAddress]:
    """Parse dictionary-style peer list."""
    peers: list[PeerAddress] = []
    for entry in peers_list:
        if not isinstance(entry, dict):
            continue
        ip_raw = entry.get(b"ip")
        port_raw = entry.get(b"port")
        if isinstance(ip_raw, bytes) and isinstance(port_raw, int):
            peers.append((ip_raw.decode("ascii"), port_raw))
    return peers


# ---------------------------------------------------------------------------
# UDP tracker (BEP 15) — full implementation
# ---------------------------------------------------------------------------


class _UDPTrackerProtocol(asyncio.DatagramProtocol):
    """Async datagram protocol for UDP tracker communication.

    Each received datagram resolves a per-transaction-id future so
    the caller can ``await`` individual responses.
    """

    def __init__(self) -> None:
        self._waiters: dict[int, asyncio.Future[bytes]] = {}
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # Minimum response: 4 bytes action + 4 bytes transaction_id
        if len(data) < 8:
            return
        transaction_id = struct.unpack_from("!I", data, 4)[0]
        fut = self._waiters.get(transaction_id)
        if fut is not None and not fut.done():
            fut.set_result(data)

    def error_received(self, exc: Exception) -> None:
        # Wake all waiters so they can retry or fail
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(
                    OSError("UDP transport closed") if exc is None else exc
                )

    def send(self, data: bytes) -> None:
        """Send a datagram."""
        if self._transport is None:
            raise RuntimeError("transport not connected")
        self._transport.sendto(data)

    def expect(self, transaction_id: int) -> asyncio.Future[bytes]:
        """Register a future for *transaction_id* and return it."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        self._waiters[transaction_id] = fut
        return fut

    def cancel(self, transaction_id: int) -> None:
        """Remove a waiter without resolving it."""
        fut = self._waiters.pop(transaction_id, None)
        if fut is not None and not fut.done():
            fut.cancel()


def _new_transaction_id() -> int:
    """Generate a random 32-bit transaction ID."""
    return struct.unpack("!I", os.urandom(4))[0]


async def _udp_request(
    protocol: _UDPTrackerProtocol,
    payload: bytes,
    transaction_id: int,
    *,
    max_retries: int = _UDP_MAX_RETRIES,
) -> bytes:
    """Send *payload* and wait for a response with exponential backoff.

    BEP 15 specifies timeout = 15 * 2^n seconds for attempt n (0–8).
    """
    for n in range(max_retries + 1):
        timeout = _UDP_BASE_TIMEOUT * (2**n)
        fut = protocol.expect(transaction_id)
        try:
            protocol.send(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            protocol.cancel(transaction_id)
            continue
        except Exception:
            protocol.cancel(transaction_id)
            raise

    raise TimeoutError(f"UDP tracker did not respond after {max_retries + 1} attempts")


async def udp_connect(
    protocol: _UDPTrackerProtocol,
) -> int:
    """BEP 15 connect handshake.  Returns a 64-bit connection ID."""
    transaction_id = _new_transaction_id()

    # Connect request: 8 magic + 4 action(0) + 4 transaction_id = 16 bytes
    payload = struct.pack("!QII", _UDP_MAGIC, _ACTION_CONNECT, transaction_id)

    data = await _udp_request(protocol, payload, transaction_id)

    # Connect response: 4 action + 4 transaction_id + 8 connection_id = 16 bytes
    # Error response: 4 action + 4 transaction_id + message (≥8 bytes)
    if len(data) < 8:
        raise TrackerError(f"connect response too short: {len(data)} bytes")

    action, resp_tid = struct.unpack_from("!II", data[:8])

    if resp_tid != transaction_id:
        raise TrackerError(
            f"transaction ID mismatch: sent {transaction_id}, got {resp_tid}"
        )

    if action == _ACTION_ERROR:
        msg = data[8:].decode("utf-8", errors="replace")
        raise TrackerError(f"tracker error on connect: {msg}")

    if action != _ACTION_CONNECT:
        raise TrackerError(f"unexpected action in connect response: {action}")

    if len(data) < 16:
        raise TrackerError(f"connect response too short: {len(data)} bytes")

    connection_id = struct.unpack_from("!Q", data, 8)[0]
    return connection_id


async def udp_announce(
    host: str,
    port: int,
    request: AnnounceRequest,
    *,
    ip_address: int = 0,
) -> AnnounceResponse:
    """Perform a full UDP tracker announce (BEP 15).

    Handles the connect → announce flow with automatic retries and
    exponential backoff per the spec.

    Parameters
    ----------
    host:
        Tracker hostname or IP.
    port:
        Tracker UDP port.
    request:
        Announce parameters.
    ip_address:
        Optional IPv4 address as a 32-bit integer.  0 = use source address.
    """
    loop = asyncio.get_running_loop()

    transport, protocol = await loop.create_datagram_endpoint(
        _UDPTrackerProtocol,
        remote_addr=(host, port),
        family=socket.AF_INET,
    )

    try:
        # Step 1: Connect
        connection_id = await udp_connect(protocol)

        # Step 2: Announce
        transaction_id = _new_transaction_id()
        event_code = _EVENT_MAP.get(request.event, _EVENT_NONE)

        # Announce request: 98 bytes
        # 8 connection_id + 4 action(1) + 4 transaction_id
        # + 20 info_hash + 20 peer_id
        # + 8 downloaded + 8 left + 8 uploaded
        # + 4 event + 4 ip + 4 key + 4 numwant + 2 port
        payload = struct.pack(
            "!QII"  # connection_id, action, transaction_id
            "20s20s"  # info_hash, peer_id
            "qqq"  # downloaded, left, uploaded (signed 64-bit)
            "IiIiH",  # event, ip, key, numwant, port
            connection_id,
            _ACTION_ANNOUNCE,
            transaction_id,
            request.info_hash,
            request.peer_id,
            request.downloaded,
            request.left,
            request.uploaded,
            event_code,
            ip_address,
            request.key,
            request.numwant,
            request.port,
        )

        data = await _udp_request(protocol, payload, transaction_id)
        return _parse_udp_announce_response(data, transaction_id)

    finally:
        transport.close()


def _parse_udp_announce_response(data: bytes, expected_tid: int) -> AnnounceResponse:
    """Parse a BEP 15 announce response.

    Response layout (≥20 bytes):
        4  action
        4  transaction_id
        4  interval
        4  leechers
        4  seeders
        6× peers (4 IP + 2 port each)
    """
    if len(data) < 20:
        raise TrackerError(f"announce response too short: {len(data)} bytes")

    action, tid, interval, leechers, seeders = struct.unpack("!IIIII", data[:20])

    if tid != expected_tid:
        raise TrackerError(
            f"transaction ID mismatch: expected {expected_tid}, got {tid}"
        )

    if action == _ACTION_ERROR:
        msg = data[8:].decode("utf-8", errors="replace")
        raise TrackerError(f"tracker error on announce: {msg}")

    if action != _ACTION_ANNOUNCE:
        raise TrackerError(f"unexpected action in announce response: {action}")

    # Parse compact peer list from remaining bytes
    peers = _parse_compact_peers(data[20:])

    return AnnounceResponse(
        interval=interval,
        peers=tuple(peers),
        complete=seeders,
        incomplete=leechers,
    )


def parse_tracker_url(url: str) -> tuple[str, str, int]:
    """Parse a tracker URL into (scheme, host, port).

    Supports ``http://``, ``https://``, and ``udp://`` schemes.
    Returns default ports when omitted: 80 for HTTP, 443 for HTTPS,
    6969 for UDP.
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"no hostname in tracker URL: {url!r}")

    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    elif scheme == "udp":
        port = 6969
    else:
        port = 80

    return scheme, host, port


async def announce(
    url: str,
    request: AnnounceRequest,
) -> AnnounceResponse:
    """Smart announce — dispatches to HTTP or UDP based on the URL scheme.

    This is the recommended entry point for tracker communication.
    """
    scheme, host, port = parse_tracker_url(url)

    if scheme in ("http", "https"):
        return await http_announce(url, request)
    elif scheme == "udp":
        return await udp_announce(host, port, request)
    else:
        raise ValueError(f"unsupported tracker scheme: {scheme!r}")
