# aiobt

[![CI](https://github.com/fried/aiobt/actions/workflows/ci.yml/badge.svg)](https://github.com/fried/aiobt/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/fried/aiobt/branch/main/graph/badge.svg)](https://codecov.io/gh/fried/aiobt)
[![PyPI](https://img.shields.io/pypi/v/aiobt)](https://pypi.org/project/aiobt/)
[![Python](https://img.shields.io/pypi/pyversions/aiobt)](https://pypi.org/project/aiobt/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/fried/aiobt/blob/main/LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Pure Python asyncio BitTorrent client library.

## Features

- **Fully async** — built on `asyncio` from the ground up with an async context manager interface
- **Zero dependencies** — pure stdlib, no runtime dependencies
- **BEP 3 wire protocol** — full handshake, piece exchange, tit-for-tat choking algorithm
- **Multi-tracker** — BEP 12 tiered announce-list with automatic failover
- **HTTP + UDP trackers** — BEP 3 HTTP and BEP 15 compact UDP announce
- **LAN peer discovery** — BEP 26 Local Service Discovery via multicast
- **Endgame mode** — duplicate-requests final pieces across all peers for fast completion
- **Resume persistence** — save and restore download progress with SHA-1 verification
- **Pluggable storage** — swap the filesystem backend for S3, databases, or anything else
- **Compact mode** — store multi-file torrents as a single blob for distribution services
- **Async event system** — `ClientEvent` and `TorrentEvent` enums with parent bubbling
- **Torrent creation** — build `.torrent` files from local content with auto piece-size selection
- **DSCP traffic classification** — mark BitTorrent traffic at the IP layer
- **Cython-ready** — hot paths (bencode, protocol, piece) structured for optional Cython compilation
- **Modern Python** — requires 3.14+, uses `type` aliases, `match`, `TaskGroup`, frozen dataclasses
- **Type-safe** — fully typed with `py.typed` marker, checked with pyrefly

## Installation

```bash
pip install aiobt
```

## Quick Start

```python
import asyncio
from aiobt import Client
from aiobt.storage import DiskStorage

async def main() -> None:
    storage = DiskStorage("/tmp/downloads")

    async with Client(storage=storage) as client:
        handle = await client.add_torrent_file(
            "archlinux-2026.05.01-x86_64.iso.torrent",
            start=True,
        )
        print(f"Downloading: {handle.name}")
        print(f"Progress: {handle.progress:.1%}")

        # Wait for download to complete
        await handle.wait()
        print(f"Done! State: {handle.state}")

asyncio.run(main())
```

## Events

Register callbacks for client and torrent lifecycle events.  Events use
enum types — no string matching:

```python
from aiobt import Client, ClientEvent, TorrentEvent

async with Client(storage=storage) as client:

    @client.on(ClientEvent.TORRENT_COMPLETED)
    async def on_complete(handle):
        print(f"Finished: {handle.name}")

    handle = await client.add_torrent_file("linux.torrent", start=True)

    @handle.on(TorrentEvent.PIECE_VERIFIED)
    async def on_piece(handle, piece_index):
        print(f"Piece {piece_index} verified")

    @handle.on(TorrentEvent.PEER_CONNECTED)
    async def on_peer(handle, peer_addr):
        print(f"Connected to {peer_addr}")

    await handle.wait()
```

Torrent events bubble up to the client, so a single client-level
listener covers all torrents:

```python
@client.on(TorrentEvent.PIECE_VERIFIED)
async def on_any_piece(handle, piece_index):
    print(f"{handle.name}: piece {piece_index}")
```

### Event Types

**ClientEvent**: `TORRENT_ADDED`, `TORRENT_REMOVED`, `TORRENT_COMPLETED`, `TORRENT_ERROR`

**TorrentEvent**: `STATE_CHANGED`, `PIECE_VERIFIED`, `PEER_CONNECTED`, `PEER_DISCONNECTED`, `TRACKER_RESPONSE`, `TRACKER_FAILED`, `COMPLETED`, `ERROR`

## Resume Persistence

Downloads resume automatically after a restart.  Pass `state_dir` to
`ClientConfig` to enable:

```python
from pathlib import Path
from aiobt import Client, ClientConfig
from aiobt.storage import DiskStorage

config = ClientConfig(state_dir=Path("/tmp/aiobt-state"))

async with Client(storage=DiskStorage("/tmp/downloads"), config=config) as client:
    handle = await client.add_torrent_file("large-file.torrent", start=True)
    # Progress is saved as pieces complete.
    # On restart, verified pieces are restored from disk — no re-download.
    await handle.wait()
```

Resume data is bencoded, stored at `{state_dir}/{info_hash_hex}.resume`,
and written atomically.  On startup, each claimed piece is SHA-1-verified
against torrent data before being marked as complete.

## Compact Storage for Distribution

For seeding servers or CDN nodes where you want simple file management,
use `CompactStorage` to store even multi-file torrents as a single blob:

```python
from aiobt import Client
from aiobt.storage import CompactStorage

async with Client(storage=CompactStorage("/srv/torrents")) as client:
    handle = await client.add_torrent_file("linux-distro.torrent", start=True)
    await handle.wait()  # All files stored as one blob on disk
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

asyncio.run(main())
```

LSD is also integrated into `Client` — enable it via `NetworkConfig`:

```python
from aiobt import Client, ClientConfig
from aiobt.network import NetworkConfig

config = ClientConfig(network=NetworkConfig(lsd_enabled=True))
async with Client(storage=storage, config=config) as client:
    ...  # LSD runs automatically, discovered peers are connected
```

`LocalDiscovery` uses IPv4 multicast (`239.192.152.143:6771`) with
optional IPv6 support. It automatically filters out its own
announcements via a per-instance cookie.

## Torrent Creation

Create `.torrent` files from local content:

```python
from aiobt import create_torrent, torrent_to_bytes

meta = create_torrent(
    path="/path/to/files",
    trackers=["http://tracker.example.com/announce"],
    comment="My torrent",
)

# Write to disk
data = torrent_to_bytes(meta)
with open("my.torrent", "wb") as f:
    f.write(data)
```

Piece size is selected automatically based on total content size,
targeting ~1,500 pieces with power-of-two sizes between 16 KiB and 16 MiB.

## BEP Support

| BEP | Name | Status |
|-----|------|--------|
| [BEP 3](https://www.bittorrent.org/beps/bep_0003.html) | The BitTorrent Protocol | ✅ Wire protocol, piece exchange, tit-for-tat choking |
| [BEP 12](https://www.bittorrent.org/beps/bep_0012.html) | Multitracker Metadata Extension | ✅ Tiered announce-list with shuffle and promotion |
| [BEP 15](https://www.bittorrent.org/beps/bep_0015.html) | UDP Tracker Protocol | ✅ Compact UDP announce with connection ID caching |
| [BEP 26](https://www.bittorrent.org/beps/bep_0026.html) | Zeroconf Peer Discovery | ✅ IPv4/IPv6 multicast LSD with cookie filtering |

## Architecture

```
aiobt/
├── client.py       # Client async context manager, TorrentHandle, ClientConfig
├── engine.py       # Download/upload loop, endgame mode, block assembly
├── choking.py      # BEP 3 tit-for-tat choking algorithm
├── protocol.py     # Wire protocol messages (Handshake, Choke, Request, Piece, ...)
├── peer.py         # PeerConnection TCP stream wrapper
├── piece.py        # PieceTracker with rarest-first selection
├── tracker.py      # HTTP + UDP (BEP 15) tracker announce
├── discovery.py    # Local Service Discovery — BEP 26 multicast
├── torrent.py      # Torrent metadata parsing (single/multi-file, BEP 12)
├── bencode.py      # Bencode codec (Cython-ready)
├── events.py       # Async EventEmitter with parent bubbling
├── resume.py       # Bencoded resume data persistence, atomic writes
├── create.py       # Torrent creation from files on disk
├── network.py      # DSCP, address family detection, NetworkConfig
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

# Run tests (uses later.unittest for async test support)
python -m unittest discover tests

# Format + lint
ruff format src/ tests/
ruff check src/ tests/

# Type check
pyrefly check src/
```

## Optional Cython Compilation

The `bencode`, `protocol`, and `piece` modules ship with Cython variants
(`.pyx`) that compile automatically when building from source via the
Meson backend:

```bash
pip install meson-python meson ninja cython
pip install -e ".[dev]" --no-build-isolation
```

Verify it loaded:

```python
from aiobt import is_compiled, compilation_status
print(compilation_status())  # {'bencode': True, 'protocol': True, 'piece': True}
```

Both `.so` and `.py` are always shipped — Python prefers the compiled
extension but falls back to pure Python automatically.

## CI / CD

- **CI** runs on every push and PR: ruff formatting + lint, pyrefly type
  checking, pure Python tests, and Cython compile + test.
- **Release** on `v*` tags: builds sdist and platform wheels
  (Linux, macOS arm64, Windows) with Cython, tests them,
  then publishes to PyPI via trusted publishing.

## License

MIT
