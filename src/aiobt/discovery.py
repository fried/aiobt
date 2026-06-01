"""Local Service Discovery — BEP 26.

Multicast-based peer discovery on the local network.  Peers announce
their active info-hashes to a well-known multicast group so that
neighbours on the same LAN can connect directly without a tracker.

Usage::

    async with LocalDiscovery(listen_port=6881) as lsd:
        lsd.announce(info_hash)

        async for peer in lsd.discovered_peers():
            print(f"Found {peer.host}:{peer.port} for {peer.info_hash.hex()}")

Or as part of :class:`~aiobt.client.Client`::

    async with Client(storage=storage, lsd=True) as client:
        ...  # LSD runs automatically
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import struct
from collections.abc import AsyncIterator, Set
from types import TracebackType

import attrs

# ---------------------------------------------------------------------------
# Constants — BEP 26
# ---------------------------------------------------------------------------

LSD_MCAST_ADDR_V4: str = "239.192.152.143"
"""IPv4 multicast group for Local Service Discovery."""

LSD_MCAST_ADDR_V6: str = "ff15::efc0:988f"
"""IPv6 multicast group for Local Service Discovery."""

LSD_PORT: int = 6771
"""UDP port used by Local Service Discovery."""

_DEFAULT_ANNOUNCE_INTERVAL: float = 300.0  # 5 minutes
"""Default seconds between periodic announce rounds."""

_MAX_ANNOUNCE_BATCH: int = 50
"""Maximum info-hashes per announcement packet."""


# ---------------------------------------------------------------------------
# Data models — frozen attrs
# ---------------------------------------------------------------------------


@attrs.frozen
class LSDAnnounce:
    """A parsed Local Service Discovery announcement."""

    host: str
    """Source IP of the announcing peer."""

    port: int
    """BitTorrent listen port of the announcing peer."""

    info_hash: bytes
    """20-byte info-hash being announced."""

    cookie: str
    """Unique cookie identifying the announcing client instance."""


@attrs.frozen
class DiscoveredPeer:
    """A peer discovered via Local Service Discovery."""

    host: str
    """IP address of the peer on the LAN."""

    port: int
    """BitTorrent listen port."""

    info_hash: bytes
    """20-byte info-hash the peer is serving."""


# ---------------------------------------------------------------------------
# Message formatting / parsing — BEP 26
# ---------------------------------------------------------------------------

_CRLF = "\r\n"


def format_announce(
    *,
    listen_port: int,
    info_hashes: tuple[bytes, ...],
    cookie: str,
    host: str = LSD_MCAST_ADDR_V4,
) -> bytes:
    """Build a BEP 26 announce message for one or more info-hashes.

    Each info-hash gets its own ``Infohash:`` header in one packet
    (BEP 26 allows multiple per message).
    """
    lines: list[str] = [
        f"BT-SEARCH * HTTP/1.1",
        f"Host: {host}:{LSD_PORT}",
        f"Port: {listen_port}",
    ]
    for ih in info_hashes:
        lines.append(f"Infohash: {ih.hex()}")
    lines.append(f"cookie: {cookie}")
    lines.append("")  # blank line terminates headers
    lines.append("")  # trailing CRLF
    return _CRLF.join(lines).encode("ascii")


def parse_announce(data: bytes, source_host: str) -> list[LSDAnnounce]:
    """Parse a BEP 26 announce message.

    Returns one :class:`LSDAnnounce` per ``Infohash:`` header found.
    If the message is malformed, returns an empty list rather than
    raising — LSD is best-effort.
    """
    try:
        text = data.decode("ascii", errors="replace")
    except Exception:
        return []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # First line must be the request line
    if not lines or not lines[0].startswith("BT-SEARCH"):
        return []

    port: int | None = None
    cookie: str = ""
    info_hashes: list[bytes] = []

    for line in lines[1:]:
        if not line or line.isspace():
            break  # end of headers

        match line.split(":", maxsplit=1):
            case [key, value]:
                key_lower = key.strip().lower()
                val = value.strip()
                match key_lower:
                    case "port":
                        try:
                            port = int(val)
                        except ValueError:
                            return []
                    case "infohash":
                        try:
                            info_hashes.append(bytes.fromhex(val))
                        except ValueError:
                            continue  # skip malformed hash, try others
                    case "cookie":
                        cookie = val
            case _:
                continue

    if port is None or not info_hashes:
        return []

    return [
        LSDAnnounce(
            host=source_host,
            port=port,
            info_hash=ih,
            cookie=cookie,
        )
        for ih in info_hashes
        if len(ih) == 20
    ]


# ---------------------------------------------------------------------------
# UDP multicast protocol
# ---------------------------------------------------------------------------


class _LSDProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram protocol for LSD multicast receive."""

    __slots__ = ("_queue", "_transport")

    def __init__(self, queue: asyncio.Queue[tuple[bytes, str]]) -> None:
        self._queue = queue
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # addr is (host, port) — we only need the host
        try:
            self._queue.put_nowait((data, addr[0]))
        except asyncio.QueueFull:
            pass  # drop if consumer is too slow

    def error_received(self, exc: Exception) -> None:
        # Non-fatal for UDP; log in future
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None


# ---------------------------------------------------------------------------
# LocalDiscovery — async context manager
# ---------------------------------------------------------------------------


def _generate_cookie() -> str:
    """Generate a random cookie for this client instance."""
    return hashlib.sha1(os.urandom(20)).hexdigest()[:16]


class LocalDiscovery:
    """Local Service Discovery (BEP 26) async context manager.

    Joins the LSD multicast group, announces active info-hashes
    periodically, and yields peers discovered on the LAN.

    Parameters
    ----------
    listen_port:
        The BitTorrent listen port to advertise.
    announce_interval:
        Seconds between periodic announce rounds (default 300).
    use_ipv6:
        Whether to also join the IPv6 multicast group.
    queue_size:
        Max buffered incoming datagrams before dropping.
    """

    def __init__(
        self,
        listen_port: int = 6881,
        announce_interval: float = _DEFAULT_ANNOUNCE_INTERVAL,
        *,
        use_ipv6: bool = False,
        queue_size: int = 256,
    ) -> None:
        self._listen_port = listen_port
        self._announce_interval = announce_interval
        self._use_ipv6 = use_ipv6
        self._cookie = _generate_cookie()

        self._info_hashes: set[bytes] = set()

        # Receive queue
        self._recv_queue: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(
            maxsize=queue_size,
        )
        # Discovered-peer queue for consumers
        self._peer_queue: asyncio.Queue[DiscoveredPeer] = asyncio.Queue()

        # Runtime handles
        self._recv_transport: asyncio.DatagramTransport | None = None
        self._send_transport: asyncio.DatagramTransport | None = None
        self._recv6_transport: asyncio.DatagramTransport | None = None
        self._send6_transport: asyncio.DatagramTransport | None = None
        self._announce_task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._running = False

    # ----- async context manager -------------------------------------------

    async def __aenter__(self) -> LocalDiscovery:
        await self._start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._stop()

    # ----- public API ------------------------------------------------------

    def announce(self, info_hash: bytes) -> None:
        """Register an info-hash for periodic LSD announcement.

        Can be called before or after entering the context manager.
        """
        if len(info_hash) != 20:
            raise ValueError(f"info_hash must be 20 bytes, got {len(info_hash)}")
        self._info_hashes.add(info_hash)

    def withdraw(self, info_hash: bytes) -> None:
        """Stop announcing an info-hash."""
        self._info_hashes.discard(info_hash)

    @property
    def active_hashes(self) -> frozenset[bytes]:
        """Currently announced info-hashes."""
        return frozenset(self._info_hashes)

    async def discovered_peers(self) -> AsyncIterator[DiscoveredPeer]:
        """Async iterator yielding peers as they are discovered.

        Blocks until a new peer is found or the context manager exits.
        """
        while self._running or not self._peer_queue.empty():
            try:
                peer = await asyncio.wait_for(self._peer_queue.get(), timeout=1.0)
                yield peer
            except TimeoutError:
                continue

    async def send_announce_now(self) -> None:
        """Immediately broadcast an announce for all active info-hashes."""
        if not self._info_hashes:
            return
        await self._send_announce(frozenset(self._info_hashes))

    # ----- internals -------------------------------------------------------

    async def _start(self) -> None:
        """Join multicast group and start background tasks."""
        self._running = True
        loop = asyncio.get_running_loop()

        # --- IPv4 receive socket (SO_REUSEADDR + multicast membership) ---
        recv_sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                recv_sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_REUSEPORT,
                    1,
                )
            except OSError:
                pass

        recv_sock.bind(("", LSD_PORT))

        # Join multicast group on all interfaces
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(LSD_MCAST_ADDR_V4),
            socket.inet_aton("0.0.0.0"),
        )
        recv_sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            mreq,
        )
        recv_sock.setblocking(False)

        self._recv_transport, _ = await loop.create_datagram_endpoint(
            lambda: _LSDProtocol(self._recv_queue),
            sock=recv_sock,
        )

        # --- IPv4 send socket (for outgoing announces) ---
        send_sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )
        send_sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_TTL,
            1,
        )
        send_sock.setblocking(False)

        self._send_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            sock=send_sock,
        )

        # --- IPv6 (optional) ---
        if self._use_ipv6:
            await self._setup_ipv6(loop)

        # --- background tasks ---
        self._listen_task = asyncio.create_task(
            self._listen_loop(),
            name="aiobt-lsd-listen",
        )
        self._announce_task = asyncio.create_task(
            self._announce_loop(),
            name="aiobt-lsd-announce",
        )

    async def _setup_ipv6(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set up IPv6 multicast receive and send sockets."""
        try:
            recv6_sock = socket.socket(
                socket.AF_INET6,
                socket.SOCK_DGRAM,
                socket.IPPROTO_UDP,
            )
            recv6_sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    recv6_sock.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_REUSEPORT,
                        1,
                    )
                except OSError:
                    pass

            recv6_sock.bind(("::", LSD_PORT))

            mreq6 = struct.pack(
                "16sI",
                socket.inet_pton(socket.AF_INET6, LSD_MCAST_ADDR_V6),
                0,  # interface index 0 = all
            )
            recv6_sock.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_JOIN_GROUP,
                mreq6,
            )
            recv6_sock.setblocking(False)

            self._recv6_transport, _ = await loop.create_datagram_endpoint(
                lambda: _LSDProtocol(self._recv_queue),
                sock=recv6_sock,
            )

            send6_sock = socket.socket(
                socket.AF_INET6,
                socket.SOCK_DGRAM,
                socket.IPPROTO_UDP,
            )
            send6_sock.setsockopt(
                socket.IPPROTO_IPV6,
                socket.IPV6_MULTICAST_HOPS,
                1,
            )
            send6_sock.setblocking(False)

            self._send6_transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                sock=send6_sock,
            )
        except OSError:
            # IPv6 not available — degrade gracefully
            pass

    async def _stop(self) -> None:
        """Leave multicast group and cancel background tasks."""
        self._running = False

        # Cancel tasks
        for task in (self._announce_task, self._listen_task):
            if task is not None and not task.done():
                task.cancel()

        tasks = [t for t in (self._announce_task, self._listen_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Close transports
        for transport in (
            self._recv_transport,
            self._send_transport,
            self._recv6_transport,
            self._send6_transport,
        ):
            if transport is not None:
                transport.close()

        self._recv_transport = None
        self._send_transport = None
        self._recv6_transport = None
        self._send6_transport = None
        self._announce_task = None
        self._listen_task = None

    async def _announce_loop(self) -> None:
        """Periodically announce all active info-hashes."""
        try:
            while self._running:
                if self._info_hashes:
                    await self._send_announce(frozenset(self._info_hashes))
                await asyncio.sleep(self._announce_interval)
        except asyncio.CancelledError:
            pass

    async def _send_announce(self, hashes: frozenset[bytes]) -> None:
        """Build and send announce packets for the given info-hashes.

        Batches into packets of up to :data:`_MAX_ANNOUNCE_BATCH`
        info-hashes each.
        """
        hash_list = list(hashes)

        for batch_start in range(0, len(hash_list), _MAX_ANNOUNCE_BATCH):
            batch = tuple(hash_list[batch_start : batch_start + _MAX_ANNOUNCE_BATCH])

            # IPv4
            msg_v4 = format_announce(
                listen_port=self._listen_port,
                info_hashes=batch,
                cookie=self._cookie,
                host=LSD_MCAST_ADDR_V4,
            )
            if self._send_transport is not None:
                self._send_transport.sendto(
                    msg_v4,
                    (LSD_MCAST_ADDR_V4, LSD_PORT),
                )

            # IPv6
            if self._send6_transport is not None:
                msg_v6 = format_announce(
                    listen_port=self._listen_port,
                    info_hashes=batch,
                    cookie=self._cookie,
                    host=f"[{LSD_MCAST_ADDR_V6}]",
                )
                self._send6_transport.sendto(
                    msg_v6,
                    (LSD_MCAST_ADDR_V6, LSD_PORT),
                )

    async def _listen_loop(self) -> None:
        """Process incoming datagrams and emit discovered peers."""
        try:
            while self._running:
                try:
                    data, source_host = await asyncio.wait_for(
                        self._recv_queue.get(),
                        timeout=1.0,
                    )
                except TimeoutError:
                    continue

                announces = parse_announce(data, source_host)
                for ann in announces:
                    # Skip our own announcements
                    if ann.cookie == self._cookie:
                        continue

                    # Only emit peers for info-hashes we care about
                    if ann.info_hash not in self._info_hashes:
                        continue

                    peer = DiscoveredPeer(
                        host=ann.host,
                        port=ann.port,
                        info_hash=ann.info_hash,
                    )
                    try:
                        self._peer_queue.put_nowait(peer)
                    except asyncio.QueueFull:
                        pass  # consumer too slow, drop oldest behavior
        except asyncio.CancelledError:
            pass
