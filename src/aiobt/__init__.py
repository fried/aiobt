"""aiobt — Pure Python asyncio BitTorrent client library.

>>> async with Client(storage=DiskStorage("/downloads")) as client:
...     handle = await client.add_torrent_file("archlinux.iso.torrent")
...     stats = handle.stats()
...     await handle.wait()
"""

from ._compiled import CYTHON_MODULES, compilation_status, is_compiled
from ._version import __version__
from .client import Client, ClientConfig, TorrentHandle, TorrentState, TorrentStats
from .create import create_torrent, optimal_piece_size, torrent_to_bytes
from .discovery import DiscoveredPeer, LocalDiscovery, LSDAnnounce
from .events import ClientEvent, EventEmitter, TorrentEvent
from .resume import ResumeData, load_resume, resume_path, save_resume
from .network import (
    AddressFamily,
    DSCPValue,
    NetworkConfig,
    apply_dscp,
    detect_address_families,
    dscp_to_tos,
    resolve_families,
)
from .torrent import FileEntry, TorrentInfo, TorrentMeta
from .tracker import (
    AnnounceRequest,
    AnnounceResponse,
    TrackerError,
    announce,
    http_announce,
    parse_tracker_url,
    udp_announce,
)

__all__ = [
    "__version__",
    "AddressFamily",
    "announce",
    "AnnounceRequest",
    "AnnounceResponse",
    "apply_dscp",
    "Client",
    "ClientConfig",
    "ClientEvent",
    "compilation_status",
    "create_torrent",
    "CYTHON_MODULES",
    "detect_address_families",
    "DiscoveredPeer",
    "DSCPValue",
    "dscp_to_tos",
    "EventEmitter",
    "FileEntry",
    "http_announce",
    "is_compiled",
    "LocalDiscovery",
    "LSDAnnounce",
    "NetworkConfig",
    "optimal_piece_size",
    "parse_tracker_url",
    "resolve_families",
    "ResumeData",
    "resume_path",
    "load_resume",
    "save_resume",
    "TorrentHandle",
    "TorrentInfo",
    "TorrentMeta",
    "TorrentState",
    "TorrentStats",
    "torrent_to_bytes",
    "TorrentEvent",
    "TrackerError",
    "udp_announce",
]
