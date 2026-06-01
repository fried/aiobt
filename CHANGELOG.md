# Changelog

## 26.6.0 — 2026-06-01

First public release.

### Features

- Full BitTorrent wire protocol (BEP 3) with tit-for-tat choking
- HTTP and UDP tracker announce (BEP 3, BEP 15)
- Multi-tracker support with tiered announce (BEP 12)
- Local Service Discovery via multicast (BEP 26)
- Endgame mode for fast completion of final pieces
- Resume persistence — save and restore download progress with SHA-1 verification
- Pluggable storage backends (DiskStorage for multi-file, CompactStorage for single-blob)
- Async event system with parent bubbling (ClientEvent, TorrentEvent)
- Torrent creation from local files with auto piece-size selection
- DSCP traffic classification
- Optional Cython acceleration for bencode, protocol, and piece modules
- Zero runtime dependencies — pure Python stdlib

### Technical

- Requires Python 3.14+
- 291 tests via `later.unittest`
- Meson build backend with optional Cython compilation
- Full type annotations with `py.typed` marker
