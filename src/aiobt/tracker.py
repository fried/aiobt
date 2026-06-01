"""Tracker announce — HTTP and UDP (BEP 3, BEP 15).

Handles communication with BitTorrent trackers to obtain peer lists.
"""

from __future__ import annotations

import asyncio
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
# UDP tracker (BEP 15) — skeleton
# ---------------------------------------------------------------------------

# Magic constant for UDP tracker protocol
_UDP_MAGIC = 0x41727101980

# Action codes
_ACTION_CONNECT = 0
_ACTION_ANNOUNCE = 1
_ACTION_SCRAPE = 2
_ACTION_ERROR = 3


class TrackerError(Exception):
    """Raised when a tracker returns an error."""


async def udp_announce(
    host: str,
    port: int,
    request: AnnounceRequest,
) -> AnnounceResponse:
    """Perform a UDP tracker announce (BEP 15).

    .. todo:: Full implementation with connect → announce flow.
    """
    # Step 1: Connect
    transaction_id = randint(0, 0xFFFFFFFF)
    connect_payload = struct.pack("!QII", _UDP_MAGIC, _ACTION_CONNECT, transaction_id)

    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _UDPTrackerProtocol(),
        remote_addr=(host, port),
    )

    try:
        transport.sendto(connect_payload)
        # TODO: implement timeout/retry and full announce cycle
        raise NotImplementedError("UDP tracker announce not yet implemented")
    finally:
        transport.close()


class _UDPTrackerProtocol(asyncio.DatagramProtocol):
    """Minimal UDP protocol handler for tracker communication."""

    def __init__(self) -> None:
        self.response: asyncio.Future[bytes] = (
            asyncio.get_running_loop().create_future()
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self.response.done():
            self.response.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.response.done():
            self.response.set_exception(exc)
