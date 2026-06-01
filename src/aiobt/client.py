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
import time
from pathlib import Path
from types import TracebackType

from dataclasses import dataclass, field

from .events import ClientEvent, EventEmitter, TorrentEvent
from .network import NetworkConfig
from .peer import PeerConnection, generate_peer_id
from .piece import PieceTracker
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

    def on(self, event: TorrentEvent, callback: object = None) -> object:
        """Register a callback for a per-torrent event.

        Works as a decorator too::

            @handle.on(TorrentEvent.COMPLETED)
            async def done(handle): ...
        """
        if callback is not None:
            return self._session.events.on(event, callback)

        # Decorator form: handle.on(event) returns a registrar
        def decorator(fn: object) -> object:
            self._session.events.on(event, fn)
            return fn

        return decorator

    def once(self, event: TorrentEvent, callback: object = None) -> object:
        """Like :meth:`on`, but fires only once."""
        if callback is not None:
            return self._session.events.once(event, callback)

        def decorator(fn: object) -> object:
            self._session.events.once(event, fn)
            return fn

        return decorator

    def off(self, event: TorrentEvent, callback: object) -> None:
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
        """Start or resume downloading/seeding.

        .. todo:: Wire into the full download loop.
        """
        s = self._session
        if s.state in (TorrentState.DOWNLOADING, TorrentState.SEEDING):
            return
        old = s.state
        s.state = TorrentState.DOWNLOADING
        await s.events.emit(
            TorrentEvent.STATE_CHANGED,
            self,
            old,
            TorrentState.DOWNLOADING,
            suppress_errors=True,
        )
        # TODO: spawn download/seed task

    async def stop(self) -> None:
        """Stop downloading/seeding and send a 'stopped' announce."""
        s = self._session
        if s.state == TorrentState.STOPPED:
            return
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
        except (TrackerError, OSError):  # fmt: skip
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

        # Stats counters
        self.bytes_downloaded: int = 0
        self.bytes_uploaded: int = 0
        self.download_rate: float = 0.0
        self.upload_rate: float = 0.0
        self.last_announce_time: float | None = None
        self.tracker_peer_count: int = 0

    async def do_announce(
        self,
        *,
        handle: TorrentHandle,
        event: str = "",
    ) -> AnnounceResponse:
        """Run a tracker announce against all known tracker URLs."""
        urls = self.meta.tracker_urls()
        if not urls:
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
        for url in urls:
            try:
                response = await announce(url, request)
                self.last_announce_time = time.time()
                self.tracker_peer_count = len(response.peers)
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

        raise TrackerError(f"all {len(urls)} trackers failed; last error: {last_error}")


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

    # ----- events -----------------------------------------------------------

    @property
    def events(self) -> EventEmitter:
        """The client-level event emitter."""
        return self._events

    def on(self, event: ClientEvent | TorrentEvent, callback: object = None) -> object:
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

        def decorator(fn: object) -> object:
            self._events.on(event, fn)
            return fn

        return decorator

    def once(
        self, event: ClientEvent | TorrentEvent, callback: object = None
    ) -> object:
        """Like :meth:`on`, but fires only once."""
        if callback is not None:
            return self._events.once(event, callback)

        def decorator(fn: object) -> object:
            self._events.once(event, fn)
            return fn

        return decorator

    def off(self, event: ClientEvent | TorrentEvent, callback: object) -> None:
        """Remove a callback."""
        self._events.off(event, callback)

    # ----- async context manager --------------------------------------------

    async def __aenter__(self) -> Client:
        self._running = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._running = False

        # Stop listening
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Cancel all torrent tasks
        for session in self._sessions.values():
            if session.task is not None and not session.task.done():
                session.task.cancel()

        # Wait for tasks to finish
        tasks = [s.task for s in self._sessions.values() if s.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            # TODO: delete storage files on disk

    def get_handle(self, info_hash: InfoHash) -> TorrentHandle | None:
        """Look up a torrent by info hash, or return *None*."""
        session = self._sessions.get(info_hash)
        if session is None:
            return None
        return TorrentHandle(session)

    def handles(self) -> list[TorrentHandle]:
        """Return handles for all loaded torrents."""
        return [TorrentHandle(s) for s in self._sessions.values()]

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

        handle = TorrentHandle(session)
        await self._events.emit(ClientEvent.TORRENT_ADDED, handle, suppress_errors=True)
        if start:
            await handle.start()
        return handle

    def _check_running(self) -> None:
        if not self._running:
            raise RuntimeError("Client must be used as an async context manager")
