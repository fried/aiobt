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
    NetworkConfig,
    detect_address_families,
    resolve_families,
)
from .torrent import FileEntry, TorrentInfo, TorrentMeta

__all__ = [
    "__version__",
    "AddressFamily",
    "Client",
    "ClientConfig",
    "CYTHON_MODULES",
    "DiscoveredPeer",
    "FileEntry",
    "LocalDiscovery",
    "LSDAnnounce",
    "NetworkConfig",
    "TorrentInfo",
    "TorrentMeta",
    "compilation_status",
    "detect_address_families",
    "is_compiled",
    "resolve_families",
]
