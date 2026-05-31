"""aiobt — Pure Python asyncio BitTorrent client library.

>>> async with BitTorrentClient(storage=DiskStorage("/downloads")) as client:
...     torrent = await client.add_torrent_file("archlinux.iso.torrent")
...     await client.download(torrent.info_hash)
"""

from ._compiled import CYTHON_MODULES, compilation_status, is_compiled
from ._version import __version__
from .client import BitTorrentClient, ClientConfig
from .discovery import DiscoveredPeer, LocalDiscovery, LSDAnnounce
from .torrent import FileEntry, TorrentInfo, TorrentMeta

__all__ = [
    "__version__",
    "BitTorrentClient",
    "ClientConfig",
    "CYTHON_MODULES",
    "DiscoveredPeer",
    "FileEntry",
    "LocalDiscovery",
    "LSDAnnounce",
    "TorrentInfo",
    "TorrentMeta",
    "compilation_status",
    "is_compiled",
]
