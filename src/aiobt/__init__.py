"""aiobt — Pure Python asyncio BitTorrent client library.

>>> async with Client(storage=DiskStorage("/downloads")) as client:
...     torrent = await client.add_torrent_file("archlinux.iso.torrent")
...     await client.download(torrent.info_hash)
"""

from ._compiled import CYTHON_MODULES, compilation_status, is_compiled
from ._version import __version__
from .client import Client, ClientConfig
from .discovery import DiscoveredPeer, LocalDiscovery, LSDAnnounce
from .network import (
    AddressFamily,
    DSCPValue,
    NetworkConfig,
    apply_dscp,
    detect_address_families,
    dscp_to_tos,
    resolve_families,
)
from .create import create_torrent, optimal_piece_size, torrent_to_bytes
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
    "compilation_status",
    "create_torrent",
    "CYTHON_MODULES",
    "detect_address_families",
    "DiscoveredPeer",
    "DSCPValue",
    "dscp_to_tos",
    "FileEntry",
    "http_announce",
    "is_compiled",
    "LocalDiscovery",
    "LSDAnnounce",
    "NetworkConfig",
    "optimal_piece_size",
    "parse_tracker_url",
    "resolve_families",
    "TorrentInfo",
    "TorrentMeta",
    "torrent_to_bytes",
    "TrackerError",
    "udp_announce",
]
