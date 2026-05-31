# aiobt

Pure Python asyncio BitTorrent client library.

## Features

- **Fully async** — built on `asyncio` from the ground up with an async context manager interface
- **Zero bloat** — only runtime dependency is `attrs`
- **Pluggable storage** — swap the filesystem backend for S3, databases, or anything else
- **Compact mode** — store multi-file torrents as a single blob for distribution services
- **LAN peer discovery** — BEP 26 Local Service Discovery via multicast, no tracker needed on local networks
- **Cython-ready** — hot paths like bencode are structured for optional Cython compilation
- **Modern Python** — requires 3.14+, uses `type` aliases, `match`, `TaskGroup`, frozen attrs classes
- **Type-safe** — fully typed with `py.typed` marker, checked with pyrefly

## Installation

```bash
pip install aiobt
```

## Quick Start

```python
import asyncio
from aiobt import BitTorrentClient
from aiobt.storage import DiskStorage

async def main() -> None:
    storage = DiskStorage("/tmp/downloads")

    async with BitTorrentClient(storage=storage) as client:
        torrent = await client.add_torrent_file("archlinux-2026.05.01-x86_64.iso.torrent")
        print(f"Downloading: {torrent.info.name}")
        print(f"Size: {torrent.total_length} bytes")
        print(f"Pieces: {torrent.piece_count}")

        await client.download(torrent.info_hash)

asyncio.run(main())
```

## Compact Storage for Distribution

For seeding servers or CDN nodes where you want simple file management,
use `CompactStorage` to store even multi-file torrents as a single blob:

```python
from aiobt import BitTorrentClient
from aiobt.storage import CompactStorage

async with BitTorrentClient(storage=CompactStorage("/srv/torrents")) as client:
    # Multi-file torrent stored as one file on disk
    torrent = await client.add_torrent_file("linux-distro.torrent")
    await client.seed(torrent.info_hash)
```

## Custom Storage Backends

Implement the `StorageBackend` protocol to plug in any storage:

```python
from aiobt.storage import StorageBackend

class S3Storage:
    """Store torrent data in S3."""

    async def open(self, total_length: int, piece_length: int) -> None:
        self._bucket = await create_bucket()

    async def read(self, offset: int, length: int) -> bytes:
        return await self._bucket.get_range(offset, length)

    async def write(self, offset: int, data: bytes) -> None:
        await self._bucket.put_range(offset, data)

    async def close(self) -> None:
        await self._bucket.close()
```

## Local Peer Discovery (BEP 26)

Find peers on the local network without a tracker — ideal for LAN
parties, office setups, or air-gapped environments:

```python
import asyncio
from aiobt import LocalDiscovery

async def main() -> None:
    async with LocalDiscovery(listen_port=6881) as lsd:
        # Announce a torrent we're serving
        lsd.announce(info_hash)

        # Discover peers on the LAN
        async for peer in lsd.discovered_peers():
            print(f"LAN peer: {peer.host}:{peer.port}")
            # connect and exchange pieces...

asyncio.run(main())
```

`LocalDiscovery` uses IPv4 multicast (`239.192.152.143:6771`) with
optional IPv6 support. It automatically filters out its own
announcements via a per-instance cookie.

## Architecture

```
aiobt/
├── client.py       # BitTorrentClient async context manager
├── bencode.py      # Bencode codec (Cython-ready)
├── torrent.py      # Torrent metadata — frozen attrs models
├── discovery.py    # Local Service Discovery — BEP 26 multicast
├── peer.py         # Peer connection management
├── tracker.py      # HTTP + UDP tracker announce
├── protocol.py     # BitTorrent wire protocol (BEP 3)
├── piece.py        # Piece selection, verification, assembly
└── storage/
    ├── base.py     # StorageBackend protocol
    ├── disk.py     # Standard multi-file on-disk storage
    ├── compact.py  # Single-blob storage for distribution
    └── queue.py    # Executor-backed filesystem I/O queue
```

## Development

```bash
git clone https://github.com/fried/aiobt.git
cd aiobt
pip install -e ".[dev]"

# Run tests
python -m unittest discover tests

# Format
black src/ tests/

# Type check
pyrefly check src/
```

## Optional Cython Compilation

The `bencode` module can be compiled with Cython for ~10x faster
torrent file parsing:

```bash
python build_cython.py
```

Verify it loaded:

```python
from aiobt import is_compiled, compilation_status
print(compilation_status())  # {'bencode': True}
```

Both `.so` and `.py` are always shipped — Python prefers the compiled
extension but falls back to pure Python automatically.

See `cython/README.md` for details.

## CI / CD

- **CI** runs on every push and PR: black formatting, pyrefly type
  checking, pure Python tests, and Cython compile + test.
- **Release** on `v*` tags: builds sdist, pure Python wheel, and
  platform-specific compiled wheels (Linux, macOS arm64/x86_64,
  Windows), then publishes to PyPI via trusted publishing.

## License

MIT
