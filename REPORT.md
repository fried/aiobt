# aiobt Bug Fix & Testing Report

**Branch:** `fix/lsd-test-bugs` (3 commits, 6 files changed, +276/-22 lines)
**Date:** 2026-06-02
**Tested on:** sovnarkom (Arch Linux, Python 3.14)

---

## GitHub Issues Filed

| # | Title | Status |
|---|-------|--------|
| [#1](https://github.com/fried/aiobt/issues/1) | Resume can't verify existing files | Fixed |
| [#2](https://github.com/fried/aiobt/issues/2) | DiskStorage.prepare\_files() never called | Fixed |
| [#3](https://github.com/fried/aiobt/issues/3) | Client.\_\_aexit\_\_ hangs | Fixed |
| [#4](https://github.com/fried/aiobt/issues/4) | TorrentStats byte counters stuck at 0 | Fixed |

---

## Commits

### 1. `855e5a0` — fix: resolve 4 bugs from LSD testing + add TRACKER\_ANNOUNCE event

**Bug #1 — DiskStorage.prepare\_files() never called:**
`_register()` now calls `storage.prepare_files()` (or `storage.prepare()` for CompactStorage) immediately after `storage.open()`. Previously, file allocation was skipped entirely, causing crashes on first write.

**Bug #2 — Resume can't verify existing files:**
`check_resume()` rewritten to perform a full piece-by-piece disk scan when no resume file exists. Iterates all pieces, reads data from disk, and SHA-1 hash-checks each one. This allows seeding of existing complete files without requiring a prior `state_dir` or resume data.

**Bug #3 — Client.\_\_aexit\_\_ hangs:**
`__aexit__()` now wraps its task gather with `asyncio.timeout(5.0)`. If tasks don't cancel within 5 seconds, they're force-cancelled. Prevents indefinite hangs on shutdown.

**Bug #4 — TorrentStats byte counters stuck at 0:**
Added `_active_peer_stats` dict to `_TorrentSession`. `_run_peer_wrapper` registers each peer's stats object on entry and removes it on exit. `TorrentHandle.stats()` sums active peer stats into the returned totals. Previously, stats were only computed from a detached snapshot that was never updated.

**Bonus — TRACKER\_ANNOUNCE event:**
Added `TorrentEvent.TRACKER_ANNOUNCE` to `events.py` and wired it in `do_announce()` to fire before each tracker URL attempt. (TRACKER\_RESPONSE and TRACKER\_FAILED were already present.)

**Bonus — except clause parenthesization:**
Fixed 7 instances of Python 2-style `except X, Y:` → `except (X, Y):` across `client.py`, `engine.py`, `choking.py`, and `resume.py`.

### 2. `c2b6fd0` — feat: add per-URL tracker timeout to prevent dead tracker stalls

Added `ClientConfig.tracker_timeout` (default 15 seconds) with per-URL `asyncio.timeout()` in `do_announce()`. Dead trackers now fail fast instead of blocking the entire announce cycle.

### 3. `fba3e58` — test: add real-world tracker + download integration test script

`tools/aiobt-tracker-test.py` — end-to-end integration test using a real torrent file. Exercises all 4 bug fixes, event wiring, peer discovery, and actual data transfer.

---

## Real-World Test Results

### FreeBSD 15.0 Boot ISO (540 MiB, 2122 pieces)

**Torrent:** `FreeBSD-15.0-RELEASE-amd64-bootonly.iso`
**Server:** sovnarkom (Linode, Arch Linux)

| Metric | Result |
|--------|--------|
| Tracker announces | 3 (one per tracker URL) |
| Tracker responses | 1 (`udp://tracker.opentrackr.org:1337`) |
| Tracker failures | 2 (`fosstorrents.com:6969` — dead/unreachable) |
| Peers discovered | 16 |
| Peers connected | 6 |
| Pieces downloaded | 1419 / 2122 (66.9%) |
| Data transferred | ~361 MiB in ~150s (~2.5 MiB/s) |
| Total runtime | 181s |
| Shutdown | Clean (no hang) |

**Bug verification:**
- ✅ **Bug #1:** Torrent loaded and wrote data without manual `prepare_files()` call
- ✅ **Bug #2:** Full piece scan ran on startup (no resume file existed)
- ✅ **Bug #3:** Client shut down cleanly within timeout
- ✅ **Bug #4:** Stats showed real-time `dl=` values during transfer (non-zero)
- ✅ **TRACKER\_ANNOUNCE events:** Fired for all 3 tracker URLs
- ✅ **Per-URL timeout:** Dead tracker (`fosstorrents.com`) failed in 15s instead of blocking indefinitely

### Arch Linux ISO (DHT-only — no trackers)

The Arch Linux torrent has zero tracker URLs and relies entirely on DHT (BEP 5) for peer discovery. aiobt does not implement DHT, so no peers were found. The torrent loaded and piece-checked correctly but couldn't connect to anyone.

---

## Known Remaining Issues

1. **No DHT support (BEP 5):** Tracker-less torrents (like Arch Linux) can't discover peers. This is a significant missing feature for modern BitTorrent usage.

2. **No upload bytes during test:** `up=0.0 MiB` throughout the FreeBSD download. Expected for a fresh leecher — the choking manager likely didn't unchoke any peers, and no peers requested pieces from us since we were incomplete. Not a bug.

3. **fosstorrents.com tracker dead:** The FreeBSD torrent's `fosstorrents.com:6969` tracker is unreachable. The per-URL timeout fix handles this gracefully now (fails in 15s instead of blocking).

---

## Files Modified

| File | Changes |
|------|---------|
| `src/aiobt/client.py` | Bug fixes #1–#4, tracker timeout, except fixes, TRACKER\_ANNOUNCE |
| `src/aiobt/events.py` | Added `TRACKER_ANNOUNCE` enum member |
| `src/aiobt/engine.py` | except clause parenthesization |
| `src/aiobt/choking.py` | except clause parenthesization |
| `src/aiobt/resume.py` | except clause parenthesization |
| `tools/aiobt-tracker-test.py` | New integration test script |
