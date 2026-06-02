"""Client — the main async context manager interface.

Usage::

    async with Client(storage=DiskStorage("/downloads")) as client:
        handle = await client.add_torrent_file("archlinux.iso.torrent")
        print(handle.name, handle.progress)
        await handle.announce()
        await handle.wait()

Events::

    @client.on(ClientEvent.TORRENT_ADDED)
    async def on_added(handle):
        print(f"Added: {handle.name}")


    @handle.on(TorrentEvent.PIECE_VERIFIED)
    async def on_piece(handle, piece_index):
        print(f"Got piece {piece_index}")
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from pathlib import Path
from types import TracebackType

from dataclasses import dataclass, field

from .choking import ChokingManager, PeerRates
from .discovery import LocalDiscovery
from .engine import EndgameState, _PeerStats, run_peer
from .events import ClientEvent, EventCallback, EventEmitter, TorrentEvent
from .network import NetworkConfig
from .peer import PeerConnection, PeerInfo, generate_peer_id
from .piece import PieceTracker
from .protocol import HANDSHAKE_LENGTH, Handshake
from .resume import load_resume, resume_path, save_resume
from .storage.base import StorageBackend
from .torrent import InfoHash, TorrentMeta, parse_torrent_bytes, parse_torrent_file
from .tracker import AnnounceRequest, AnnounceResponse, TrackerError, announce


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TorrentState(enum.Enum):
    """Lifecycle state of a loaded torrent."""

    STOPPED = "stopped"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Stats snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TorrentStats:
    """Point-in-time statistics for a torrent."""

    state: TorrentState
    """Current lifecycle state."""

    progress: float
    """Download progress as a fraction [0.0, 1.0]."""

    total_length: int
    """Total content size in bytes."""

    downloaded: int
    """Bytes verified and written."""

    uploaded: int
    """Bytes sent to peers."""

    peers_connected: int
    """Number of currently connected peers."""

    pieces_have: int
    """Pieces verified and stored."""

    pieces_total: int
    """Total number of pieces."""

    download_rate: float
    """Current download speed in bytes/sec."""

    upload_rate: float
    """Current upload speed in bytes/sec."""

    last_announce: float | None
    """Unix timestamp of last successful tracker announce, or None."""

    tracker_peers: int
    """Total peers returned by trackers."""


# ---------------------------------------------------------------------------
# TorrentHandle — the public per-torrent API
# ---------------------------------------------------------------------------


class TorrentHandle:
    """A live reference to a torrent loaded in the :class:`Client`.

    Returned by ``client.add_torrent*()``.  Provides stats, control,
    an awaitable completion future, and per-torrent event registration.

    Events
    ------
    Register per-torrent callbacks via :meth:`on` / :meth:`once`::

        @handle.on(TorrentEvent.PIECE_VERIFIED)
        async def on_piece(handle, piece_index): ...

    See :class:`TorrentEvent` for the full list.
    """

    __slots__ = ("_session",)

    def __init__(self, session: _TorrentSession) -> None:
        self._session = session

    # ----- identity ---------------------------------------------------------

    @property
    def info_hash(self) -> InfoHash:
        """20-byte SHA-1 info hash."""
        return self._session.meta.info_hash

    @property
    def meta(self) -> TorrentMeta:
        """Full torrent metadata."""
        return self._session.meta

    @property
    def name(self) -> str:
        """Torrent name (from info dict)."""
        return self._session.meta.info.name

    # ----- events -----------------------------------------------------------

    @property
    def events(self) -> EventEmitter:
        """The per-torrent event emitter."""
        return self._session.events

    def on(
        self, event: TorrentEvent, callback: EventCallback | None = None
    ) -> EventCallback:
        """Register a callback for a per-torrent event.

        Works as a decorator too::

            @handle.on(TorrentEvent.COMPLETED)
            async def done(handle): ...
        """
        if callback is not None:
            return self._session.events.on(event, callback)

        # Decorator form: handle.on(event) returns a registrar
        def decorator(fn: EventCallback) -> EventCallback:
            self._session.events.on(event, fn)
            return fn

        return decorator  # type: ignore[return-value]

    def once(
        self, event: TorrentEvent, callback: EventCallback | None = None
    ) -> EventCallback:
        """Like :meth:`on`, but fires only once."""
        if callback is not None:
            return self._session.events.once(event, callback)

        def decorator(fn: EventCallback) -> EventCallback:
            self._session.events.once(event, fn)
            return fn

        return decorator  # type: ignore[return-value]

    def off(self, event: TorrentEvent, callback: EventCallback) -> None:
        """Remove a callback."""
        self._session.events.off(event, callback)

    # ----- stats ------------------------------------------------------------

    @property
    def state(self) -> TorrentState:
        return self._session.state

    @property
    def progress(self) -> float:
        return self._session.tracker.progress

    def stats(self) -> TorrentStats:
        """Return a frozen snapshot of current statistics."""
        s = self._session
        return TorrentStats(
            state=s.state,
            progress=s.tracker.progress,
            total_length=s.meta.total_length,
            downloaded=s.bytes_downloaded,
            uploaded=s.bytes_uploaded,
            peers_connected=len(s.peers),
            pieces_have=len(s.tracker.have),
            pieces_total=s.tracker.piece_count,
            download_rate=s.download_rate,
            upload_rate=s.upload_rate,
            last_announce=s.last_announce_time,
            tracker_peers=s.tracker_peer_count,
        )

    # ----- control ----------------------------------------------------------

    async def announce(self, *, event: str = "") -> AnnounceResponse:
        """Force a tracker announce cycle.

        Parameters
        ----------
        event:
            Tracker event string (``"started"``, ``"stopped"``,
            ``"completed"``, or ``""`` for periodic).

        Returns the first successful :class:`AnnounceResponse`.
        Raises :class:`TrackerError` if all trackers fail.
        """
        return await self._session.do_announce(handle=self, event=event)

    async def start(self) -> None:
        """Start or resume downloading/seeding."""
        s = self._session
        if s.state in (TorrentState.DOWNLOADING, TorrentState.SEEDING):
            return
        # Check resume data (runs integrity verification once)
        await s.check_resume(self)
        old = s.state
        if s.tracker.is_complete:
            s.state = TorrentState.SEEDING
            s.done_event.set()
            s.choking.is_seeding = True
        else:
            s.state = TorrentState.DOWNLOADING
        await s.events.emit(
            TorrentEvent.STATE_CHANGED,
            self,
            old,
            s.state,
            suppress_errors=True,
        )
        # Start choking manager
        if s.choking_task is None or s.choking_task.done():
            s.choking_stop = asyncio.Event()
            s.choking_task = asyncio.create_task(
                s.choking.run(s.choking_stop),
                name="aiobt-choking",
            )

    async def stop(self) -> None:
        """Stop downloading/seeding and send a 'stopped' announce."""
        s = self._session
        if s.state == TorrentState.STOPPED:
            return
        # Stop choking manager
        if s.choking_stop is not None:
            s.choking_stop.set()
        if s.choking_task is not None and not s.choking_task.done():
            s.choking_task.cancel()
            try:
                await s.choking_task
            except asyncio.CancelledError:
                pass
            s.choking_task = None
        # Cancel the download task if running
        if s.task is not None and not s.task.done():
            s.task.cancel()
            try:
                await s.task
            except asyncio.CancelledError:
                pass
            s.task = None
        old = s.state
        s.state = TorrentState.STOPPED
        await s.events.emit(
            TorrentEvent.STATE_CHANGED,
            self,
            old,
            TorrentState.STOPPED,
            suppress_errors=True,
        )
        try:
            await self.announce(event="stopped")
        except TrackerError, OSError:
            pass  # best-effort

    async def wait(self) -> None:
        """Block until the torrent reaches 100% or is removed.

        Use :func:`asyncio.timeout` to bound the wait::

            async with asyncio.timeout(3600):
                await handle.wait()
        """
        await self._session.done_event.wait()

    def is_complete(self) -> bool:
        """Return *True* if all pieces are verified."""
        return self._session.tracker.is_complete

    # ----- dunder -----------------------------------------------------------

    def __repr__(self) -> str:
        pct = self.progress * 100
        return (
            f"<TorrentHandle {self.name!r} "
            f"state={self.state.value} "
            f"progress={pct:.1f}%>"
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TorrentHandle):
            return self.info_hash == other.info_hash
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.info_hash)


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Immutable configuration for :class:`Client`."""

    listen_port: int = 6881
    """Port to listen for incoming peer connections."""

    max_peers: int = 50
    """Maximum simultaneous peer connections per torrent."""

    peer_id: bytes = field(default_factory=generate_peer_id)
    """Our 20-byte peer ID (generated by default)."""

    request_timeout: float = 30.0
    """Seconds to wait for a block before re-requesting."""

    network: NetworkConfig = field(default_factory=NetworkConfig)
    """Network configuration (address families, LSD, bind addresses)."""

    state_dir: Path | None = None
    """Directory for resume data.  ``None`` disables resume persistence."""


# ---------------------------------------------------------------------------
# Internal per-torrent state
# ---------------------------------------------------------------------------


class _TorrentSession:
    """Internal mutable state for a single loaded torrent."""

    def __init__(
        self,
        meta: TorrentMeta,
        storage: StorageBackend,
        config: ClientConfig,
        parent_events: EventEmitter | None = None,
    ) -> None:
        self.meta = meta
        self.storage = storage
        self.config = config
        self.tracker = PieceTracker(
            piece_length=meta.info.piece_length,
            total_length=meta.total_length,
            pieces_raw=meta.info.pieces_raw,
        )
        self.peers: dict[tuple[str, int], PeerConnection] = {}
        self.task: asyncio.Task[None] | None = None
        self.state: TorrentState = TorrentState.STOPPED
        self.done_event: asyncio.Event = asyncio.Event()
        self.events: EventEmitter = EventEmitter(parent=parent_events)

        # Choking
        self.choking: ChokingManager = ChokingManager()
        self.choking_task: asyncio.Task[None] | None = None
        self.choking_stop: asyncio.Event | None = None

        # Endgame
        self.endgame: EndgameState = EndgameState()

        # Resume persistence
        self._state_dir: Path | None = config.state_dir
        self._resume_checked: bool = False
        if self._state_dir is not None:
            self.events.on(TorrentEvent.PIECE_VERIFIED, self._on_piece_verified)

        # BEP 12 tiered tracker list (mutable — successful URLs get promoted)
        self._tracker_tiers: list[list[str]] = _build_tracker_tiers(meta)

        # Stats counters
        self.bytes_downloaded: int = 0
        self.bytes_uploaded: int = 0
        self.download_rate: float = 0.0
        self.upload_rate: float = 0.0
        self.last_announce_time: float | None = None
        self.tracker_peer_count: int = 0

    # ----- resume -----------------------------------------------------------

    async def _on_piece_verified(self, handle: object, piece_index: int) -> None:
        """Event handler: save resume data after each verified piece."""
        await self._save_resume()

    async def _save_resume(self) -> None:
        """Persist current progress to the resume file."""
        if self._state_dir is None:
            return
        path = resume_path(self._state_dir, self.meta.info_hash)
        # Compute downloaded from verified pieces (always accurate)
        downloaded = sum(self.tracker.spec(i).length for i in self.tracker.have)
        await save_resume(
            path,
            info_hash=self.meta.info_hash,
            have=self.tracker.have,
            piece_count=self.tracker.piece_count,
            downloaded=downloaded,
            uploaded=self.bytes_uploaded,
        )

    async def check_resume(self, handle: object) -> None:
        """Load resume data and verify pieces against storage.

        Sets state to ``CHECKING`` during verification.  Only runs
        once per session.
        """
        if self._state_dir is None or self._resume_checked:
            return
        self._resume_checked = True

        path = resume_path(self._state_dir, self.meta.info_hash)
        data = load_resume(path, self.meta.info_hash)
        if data is None or not data.have:
            return

        old_state = self.state
        self.state = TorrentState.CHECKING
        await self.events.emit(
            TorrentEvent.STATE_CHANGED,
            handle,
            old_state,
            TorrentState.CHECKING,
            suppress_errors=True,
        )

        for idx in sorted(data.have):
            if idx >= self.tracker.piece_count:
                continue
            spec = self.tracker.spec(idx)
            try:
                piece_data = await self.storage.read(spec.offset, spec.length)
                if PieceTracker.verify_piece(piece_data, spec.hash):
                    self.tracker.mark_have(idx)
            except OSError, ValueError:
                pass  # piece unreadable or corrupt — will re-download

        # Update byte counters from verified pieces
        self.bytes_downloaded = sum(
            self.tracker.spec(i).length for i in self.tracker.have
        )
        if data.uploaded > self.bytes_uploaded:
            self.bytes_uploaded = data.uploaded

    # ----- announce ---------------------------------------------------------

    async def do_announce(
        self,
        *,
        handle: TorrentHandle,
        event: str = "",
    ) -> AnnounceResponse:
        """Run a BEP 12 tiered tracker announce.

        Tiers are tried in order; URLs within each tier are shuffled.
        On success the working URL is promoted to the front of its tier.
        """
        if not self._tracker_tiers:
            raise TrackerError("torrent has no tracker URLs")

        request = AnnounceRequest(
            info_hash=self.meta.info_hash,
            peer_id=self.config.peer_id,
            port=self.config.listen_port,
            left=self.meta.total_length - self.bytes_downloaded,
            uploaded=self.bytes_uploaded,
            downloaded=self.bytes_downloaded,
            event=event,
        )

        last_error: Exception | None = None
        for tier in self._tracker_tiers:
            # BEP 12: shuffle within tier (but keep first element stable
            # if it was promoted from a prior success)
            if len(tier) > 1:
                rest = tier[1:]
                random.shuffle(rest)
                tier[1:] = rest

            for url in list(tier):  # copy — we mutate tier on success
                try:
                    response = await announce(url, request)
                    self.last_announce_time = time.time()
                    self.tracker_peer_count = len(response.peers)
                    # BEP 12: promote successful URL to front of tier
                    if tier[0] != url:
                        tier.remove(url)
                        tier.insert(0, url)
                    await self.events.emit(
                        TorrentEvent.TRACKER_RESPONSE,
                        handle,
                        response,
                        suppress_errors=True,
                    )
                    return response
                except (TrackerError, OSError) as exc:
                    last_error = exc
                    await self.events.emit(
                        TorrentEvent.TRACKER_FAILED,
                        handle,
                        exc,
                        suppress_errors=True,
                    )
                    continue

        raise TrackerError(
            f"all trackers failed ({sum(len(t) for t in self._tracker_tiers)} URLs); "
            f"last error: {last_error}"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _build_tracker_tiers(meta: TorrentMeta) -> list[list[str]]:
    """Build BEP 12 tiered tracker list from torrent metadata.

    If ``announce_list`` is present, it takes precedence.  Otherwise
    the single ``announce`` URL is wrapped in a one-element tier.
    """
    if meta.announce_list:
        return [list(tier) for tier in meta.announce_list if tier]
    if meta.announce:
        return [[meta.announce]]
    return []


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """Async context manager for BitTorrent operations.

    Fires :class:`ClientEvent` callbacks registered via :meth:`on`.

    Parameters
    ----------
    storage:
        A :class:`~aiobt.storage.base.StorageBackend` implementation.
    config:
        Optional :class:`ClientConfig` (defaults are sensible).

    Example
    -------
    ::

        async with Client(storage=DiskStorage("/dl")) as client:
            client.on(ClientEvent.TORRENT_ADDED, my_callback)
            handle = await client.add_torrent_file("file.torrent")
            handle.on(TorrentEvent.COMPLETED, my_done_cb)
            await handle.wait()
    """

    def __init__(
        self,
        storage: StorageBackend,
        config: ClientConfig | None = None,
    ) -> None:
        if not isinstance(storage, StorageBackend):
            raise TypeError(
                f"storage must satisfy StorageBackend protocol, "
                f"got {type(storage).__name__}"
            )
        self._storage = storage
        self._config = config or ClientConfig()
        self._sessions: dict[InfoHash, _TorrentSession] = {}
        self._running = False
        self._server: asyncio.Server | None = None
        self._events = EventEmitter()
        self._peer_tasks: set[asyncio.Task[None]] = set()
        self._listen_port: int = 0

        # LSD
        self._lsd: LocalDiscovery | None = None
        self._lsd_task: asyncio.Task[None] | None = None

    # ----- events -----------------------------------------------------------

    @property
    def events(self) -> EventEmitter:
        """The client-level event emitter."""
        return self._events

    def on(
        self, event: ClientEvent | TorrentEvent, callback: EventCallback | None = None
    ) -> EventCallback:
        """Register a callback for a client-level or torrent-level event.

        :class:`ClientEvent` callbacks fire for client lifecycle.
        :class:`TorrentEvent` callbacks fire for **all** torrents —
        events bubble up from each torrent's emitter to the client::

            client.on(ClientEvent.TORRENT_ADDED, my_func)


            @client.on(TorrentEvent.PIECE_VERIFIED)
            async def on_piece(handle, piece_index): ...
        """
        if callback is not None:
            return self._events.on(event, callback)

        def decorator(fn: EventCallback) -> EventCallback:
            self._events.on(event, fn)
            return fn

        return decorator  # type: ignore[return-value]

    def once(
        self, event: ClientEvent | TorrentEvent, callback: EventCallback | None = None
    ) -> EventCallback:
        """Like :meth:`on`, but fires only once."""
        if callback is not None:
            return self._events.once(event, callback)

        def decorator(fn: EventCallback) -> EventCallback:
            self._events.once(event, fn)
            return fn

        return decorator  # type: ignore[return-value]

    def off(self, event: ClientEvent | TorrentEvent, callback: EventCallback) -> None:
        """Remove a callback."""
        self._events.off(event, callback)

    @property
    def listen_port(self) -> int:
        """Return the port the client is listening on (0 if not listening)."""
        return self._listen_port

    # ----- async context manager --------------------------------------------

    async def __aenter__(self) -> Client:
        self._running = True
        # Start TCP server for incoming peer connections
        self._server = await asyncio.start_server(
            self._handle_incoming,
            host="0.0.0.0",
            port=self._config.listen_port,
        )
        # Record the actual bound port (important when listen_port=0)
        socks = self._server.sockets
        if socks:
            self._listen_port = socks[0].getsockname()[1]

        # Start Local Service Discovery if enabled
        if self._config.network.lsd_enabled:
            try:
                self._lsd = LocalDiscovery(
                    listen_port=self._listen_port,
                    announce_interval=self._config.network.lsd_announce_interval,
                )
                await self._lsd.__aenter__()
                # Announce any already-registered torrents
                for info_hash in self._sessions:
                    self._lsd.announce(info_hash)
                # Consumer loop
                self._lsd_task = asyncio.create_task(
                    self._lsd_consumer_loop(),
                    name="aiobt-lsd-consumer",
                )
            except OSError:
                # LSD not available (e.g. container, no multicast)
                self._lsd = None

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._running = False

        # Stop LSD
        if self._lsd_task is not None and not self._lsd_task.done():
            self._lsd_task.cancel()
            try:
                await self._lsd_task
            except asyncio.CancelledError:
                pass
            self._lsd_task = None
        if self._lsd is not None:
            await self._lsd.__aexit__(None, None, None)
            self._lsd = None

        # Stop listening
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Cancel all choking tasks
        for session in self._sessions.values():
            if session.choking_stop is not None:
                session.choking_stop.set()
            if session.choking_task is not None and not session.choking_task.done():
                session.choking_task.cancel()

        # Cancel all torrent tasks
        for session in self._sessions.values():
            if session.task is not None and not session.task.done():
                session.task.cancel()

        # Cancel peer tasks
        for t in self._peer_tasks:
            if not t.done():
                t.cancel()

        # Wait for tasks to finish
        tasks = [s.task for s in self._sessions.values() if s.task is not None]
        tasks.extend(
            s.choking_task
            for s in self._sessions.values()
            if s.choking_task is not None
        )
        tasks.extend(self._peer_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._peer_tasks.clear()

        # Disconnect all peers
        for session in self._sessions.values():
            for peer in session.peers.values():
                await peer.disconnect()

        # Close storage for each session
        for session in self._sessions.values():
            await session.storage.close()

        self._events.clear()
        self._sessions.clear()

    # ----- public API -------------------------------------------------------

    async def add_torrent_file(
        self, path: str | Path, *, start: bool = False
    ) -> TorrentHandle:
        """Load a ``.torrent`` file and return a :class:`TorrentHandle`."""
        self._check_running()
        meta = parse_torrent_file(str(path))
        return await self._register(meta, start=start)

    async def add_torrent_bytes(
        self, data: bytes, *, start: bool = False
    ) -> TorrentHandle:
        """Load a torrent from raw bencoded *data*."""
        self._check_running()
        meta = parse_torrent_bytes(data)
        return await self._register(meta, start=start)

    async def add_torrent(
        self, meta: TorrentMeta, *, start: bool = False
    ) -> TorrentHandle:
        """Register an already-constructed :class:`TorrentMeta`.

        Use this when you've built a ``TorrentMeta`` via
        :func:`~aiobt.create.create_torrent` or received one from
        another source, instead of reading a ``.torrent`` file from disk.

        Parameters
        ----------
        start:
            If True, immediately transition to DOWNLOADING after registration.
        """
        self._check_running()
        return await self._register(meta, start=start)

    async def remove_torrent(
        self,
        handle: TorrentHandle,
        *,
        delete_data: bool = False,
    ) -> None:
        """Remove a torrent from the client.

        Parameters
        ----------
        handle:
            Handle returned by one of the ``add_torrent*`` methods.
        delete_data:
            If True, also delete downloaded data on disk.
        """
        self._check_running()
        session = self._sessions.pop(handle.info_hash, None)
        if session is None:
            return

        # Withdraw from LSD
        if self._lsd is not None:
            self._lsd.withdraw(handle.info_hash)

        # Stop choking
        if session.choking_stop is not None:
            session.choking_stop.set()
        if session.choking_task is not None and not session.choking_task.done():
            session.choking_task.cancel()
            try:
                await session.choking_task
            except asyncio.CancelledError:
                pass

        # Stop the task
        if session.task is not None and not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass

        # Disconnect peers
        for peer in session.peers.values():
            await peer.disconnect()

        # Signal waiters
        session.done_event.set()

        await self._events.emit(
            ClientEvent.TORRENT_REMOVED, handle, suppress_errors=True
        )

        if delete_data:
            await session.storage.close()

    def get_handle(self, info_hash: InfoHash) -> TorrentHandle | None:
        """Look up a torrent by info hash, or return *None*."""
        session = self._sessions.get(info_hash)
        if session is None:
            return None
        return TorrentHandle(session)

    def handles(self) -> list[TorrentHandle]:
        """Return handles for all loaded torrents."""
        return [TorrentHandle(s) for s in self._sessions.values()]

    async def add_peer(
        self,
        host: str,
        port: int,
        info_hash: InfoHash,
    ) -> None:
        """Manually connect to a peer for the given torrent.

        This is useful when LSD / trackers aren't available and you
        know the peer address directly (e.g. in tests).
        """
        self._check_running()
        session = self._sessions.get(info_hash)
        if session is None:
            raise ValueError("no torrent loaded for this info_hash")

        addr = (host, port)
        if addr in session.peers:
            return  # already connected

        info = PeerInfo(host=host, port=port)
        peer = PeerConnection(
            info=info,
            info_hash=info_hash,
            our_peer_id=self._config.peer_id,
        )
        try:
            await peer.connect(timeout=self._config.request_timeout)
        except OSError, asyncio.TimeoutError:
            return  # can't connect, silently skip

        session.peers[addr] = peer
        handle = TorrentHandle(session)
        await session.events.emit(
            TorrentEvent.PEER_CONNECTED, handle, addr, suppress_errors=True
        )
        # Register with choking manager
        rates = session.choking.register(addr, peer)
        stats = _PeerStats()
        task = asyncio.create_task(
            self._run_peer_wrapper(session, peer, handle, stats, addr, rates)
        )
        self._peer_tasks.add(task)
        task.add_done_callback(self._peer_tasks.discard)

    # ----- LSD integration --------------------------------------------------

    async def _lsd_consumer_loop(self) -> None:
        """Consume discovered peers from LSD and connect to them."""
        assert self._lsd is not None
        try:
            async for discovered in self._lsd.discovered_peers():
                if not self._running:
                    break
                session = self._sessions.get(discovered.info_hash)
                if session is None:
                    continue
                addr = (discovered.host, discovered.port)
                if addr in session.peers:
                    continue
                # Connect in the background, tracking the task
                task = asyncio.create_task(
                    self.add_peer(
                        discovered.host,
                        discovered.port,
                        discovered.info_hash,
                    )
                )
                self._peer_tasks.add(task)
                task.add_done_callback(self._peer_tasks.discard)
        except asyncio.CancelledError:
            pass

    # ----- internal ---------------------------------------------------------

    async def _register(
        self, meta: TorrentMeta, *, start: bool = False
    ) -> TorrentHandle:
        """Register a torrent and prepare its storage."""
        existing = self._sessions.get(meta.info_hash)
        if existing is not None:
            handle = TorrentHandle(existing)
            if start and existing.state == TorrentState.STOPPED:
                await handle.start()
            return handle

        session = _TorrentSession(
            meta=meta,
            storage=self._storage,
            config=self._config,
            parent_events=self._events,
        )
        await self._storage.open(meta.total_length, meta.info.piece_length)
        self._sessions[meta.info_hash] = session

        # Announce on LSD
        if self._lsd is not None:
            self._lsd.announce(meta.info_hash)

        handle = TorrentHandle(session)
        await self._events.emit(ClientEvent.TORRENT_ADDED, handle, suppress_errors=True)
        if start:
            await handle.start()
        return handle

    async def _handle_incoming(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming TCP connection from a peer."""
        try:
            # Read handshake
            data = await asyncio.wait_for(
                reader.readexactly(HANDSHAKE_LENGTH), timeout=10.0
            )
            hs = Handshake.from_bytes(data)

            session = self._sessions.get(hs.info_hash)
            if session is None:
                writer.close()
                await writer.wait_closed()
                return

            # Send our handshake back
            our_hs = Handshake(info_hash=hs.info_hash, peer_id=self._config.peer_id)
            writer.write(our_hs.to_bytes())
            await writer.drain()

            addr_info = writer.get_extra_info("peername")
            addr = (addr_info[0], addr_info[1]) if addr_info else ("?", 0)

            info = PeerInfo(host=addr[0], port=addr[1], peer_id=hs.peer_id)
            peer = PeerConnection(
                info=info,
                info_hash=hs.info_hash,
                our_peer_id=self._config.peer_id,
            )
            # Inject the already-connected streams
            peer._reader = reader
            peer._writer = writer

            session.peers[addr] = peer
            handle = TorrentHandle(session)
            await session.events.emit(
                TorrentEvent.PEER_CONNECTED, handle, addr, suppress_errors=True
            )
            # Register with choking manager
            rates = session.choking.register(addr, peer)
            stats = _PeerStats()
            task = asyncio.create_task(
                self._run_peer_wrapper(session, peer, handle, stats, addr, rates)
            )
            self._peer_tasks.add(task)
            task.add_done_callback(self._peer_tasks.discard)

        except (
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
            ValueError,
        ):
            writer.close()
            await writer.wait_closed()

    async def _run_peer_wrapper(
        self,
        session: _TorrentSession,
        peer: PeerConnection,
        handle: TorrentHandle,
        stats: _PeerStats,
        addr: tuple[str, int],
        rates: PeerRates | None = None,
    ) -> None:
        """Wrap run_peer to update session stats on completion."""
        try:
            await run_peer(
                peer=peer,
                tracker=session.tracker,
                storage=session.storage,
                piece_length=session.meta.info.piece_length,
                handle=handle,
                done_event=session.done_event,
                stats=stats,
                rates=rates,
                endgame=session.endgame,
                addr=addr,
                choking_mgr=session.choking,
            )
        finally:
            session.bytes_downloaded += stats.bytes_downloaded
            session.bytes_uploaded += stats.bytes_uploaded
            session.peers.pop(addr, None)
            session.choking.unregister(addr)
            await session.events.emit(
                TorrentEvent.PEER_DISCONNECTED,
                handle,
                addr,
                suppress_errors=True,
            )

    def _check_running(self) -> None:
        if not self._running:
            raise RuntimeError("Client must be used as an async context manager")
