"""Tests for TorrentHandle and the Client public API."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from aiobt.client import Client, TorrentHandle, TorrentState, TorrentStats
from aiobt.create import create_torrent
from aiobt.storage.base import StorageBackend
from aiobt.torrent import TorrentMeta


# ---------------------------------------------------------------------------
# Stub storage backend
# ---------------------------------------------------------------------------


class _MemoryStorage:
    """Minimal in-memory StorageBackend for tests."""

    def __init__(self) -> None:
        self._buf = bytearray()

    async def open(self, total_length: int, piece_length: int) -> None:
        self._buf = bytearray(total_length)

    async def close(self) -> None:
        self._buf.clear()

    async def read(self, offset: int, length: int) -> bytes:
        return bytes(self._buf[offset : offset + length])

    async def write(self, offset: int, data: bytes) -> None:
        self._buf[offset : offset + len(data)] = data


# Prove it satisfies the protocol
assert isinstance(_MemoryStorage(), StorageBackend)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_torrent() -> tuple[TorrentMeta, Path]:
    """Create a temp file + torrent meta, return (meta, temp_path)."""
    f = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    data = os.urandom(20_000)
    f.write(data)
    f.flush()
    f.close()
    path = Path(f.name)
    meta = create_torrent(
        path,
        trackers=["udp://tracker.example.com:6969/announce"],
        comment="test",
    )
    return meta, path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTorrentHandle(unittest.TestCase):
    """Test TorrentHandle returned by add_torrent*()."""

    def test_add_torrent_returns_handle(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertIsInstance(handle, TorrentHandle)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_handle_identity(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(handle.info_hash, meta.info_hash)
                    self.assertEqual(handle.name, meta.info.name)
                    self.assertIs(handle.meta, meta)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_handle_initial_state(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(handle.state, TorrentState.STOPPED)
                    self.assertEqual(handle.progress, 0.0)
                    self.assertFalse(handle.is_complete())

            asyncio.run(run())
        finally:
            path.unlink()

    def test_handle_stats_snapshot(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    stats = handle.stats()
                    self.assertIsInstance(stats, TorrentStats)
                    self.assertEqual(stats.state, TorrentState.STOPPED)
                    self.assertEqual(stats.progress, 0.0)
                    self.assertEqual(stats.total_length, meta.total_length)
                    self.assertEqual(stats.downloaded, 0)
                    self.assertEqual(stats.uploaded, 0)
                    self.assertEqual(stats.peers_connected, 0)
                    self.assertEqual(stats.pieces_have, 0)
                    self.assertEqual(stats.pieces_total, meta.info.piece_count)
                    self.assertIsNone(stats.last_announce)
                    self.assertEqual(stats.tracker_peers, 0)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_handle_repr(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    r = repr(handle)
                    self.assertIn(meta.info.name, r)
                    self.assertIn("stopped", r)
                    self.assertIn("0.0%", r)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_handle_equality_and_hash(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    h1 = await client.add_torrent(meta)
                    h2 = await client.add_torrent(meta)  # duplicate add
                    self.assertEqual(h1, h2)
                    self.assertEqual(hash(h1), hash(h2))
                    # Can be used in sets
                    s = {h1, h2}
                    self.assertEqual(len(s), 1)

            asyncio.run(run())
        finally:
            path.unlink()


class TestClientAddMethods(unittest.TestCase):
    """Test all three add_torrent*() entry points."""

    def test_add_torrent_meta(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(handle.info_hash, meta.info_hash)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_add_torrent_bytes(self) -> None:
        meta, path = _make_torrent()
        try:
            raw = meta.to_bytes()

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent_bytes(raw)
                    self.assertEqual(handle.info_hash, meta.info_hash)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_add_torrent_file(self) -> None:
        meta, path = _make_torrent()
        try:
            torrent_file = Path(tempfile.mktemp(suffix=".torrent"))
            meta.write(torrent_file)

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent_file(torrent_file)
                    self.assertEqual(handle.info_hash, meta.info_hash)

            asyncio.run(run())
        finally:
            path.unlink()
            torrent_file.unlink(missing_ok=True)

    def test_duplicate_add_returns_same_handle(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    h1 = await client.add_torrent(meta)
                    h2 = await client.add_torrent(meta)
                    self.assertEqual(h1, h2)

            asyncio.run(run())
        finally:
            path.unlink()


class TestClientLookup(unittest.TestCase):
    """Test get_handle() and handles()."""

    def test_get_handle(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    found = client.get_handle(meta.info_hash)
                    self.assertIsNotNone(found)
                    self.assertEqual(found, handle)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_get_handle_missing(self) -> None:
        async def run() -> None:
            async with Client(storage=_MemoryStorage()) as client:
                self.assertIsNone(client.get_handle(b"\x00" * 20))

        asyncio.run(run())

    def test_handles_list(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    self.assertEqual(len(client.handles()), 0)
                    await client.add_torrent(meta)
                    self.assertEqual(len(client.handles()), 1)

            asyncio.run(run())
        finally:
            path.unlink()


class TestRemoveTorrent(unittest.TestCase):
    """Test remove_torrent()."""

    def test_remove(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(len(client.handles()), 1)
                    await client.remove_torrent(handle)
                    self.assertEqual(len(client.handles()), 0)
                    self.assertIsNone(client.get_handle(meta.info_hash))

            asyncio.run(run())
        finally:
            path.unlink()

    def test_remove_nonexistent(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    await client.remove_torrent(handle)
                    # Second remove is a no-op
                    await client.remove_torrent(handle)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_remove_signals_waiters(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)

                    # Start a waiter task
                    async def waiter() -> bool:
                        async with asyncio.timeout(5.0):
                            await handle.wait()
                        return True

                    task = asyncio.create_task(waiter())
                    # Give the event loop a tick
                    await asyncio.sleep(0)
                    # Remove should unblock the waiter
                    await client.remove_torrent(handle)
                    result = await asyncio.wait_for(task, timeout=2.0)
                    self.assertTrue(result)

            asyncio.run(run())
        finally:
            path.unlink()


class TestTorrentHandleControl(unittest.TestCase):
    """Test start/stop on TorrentHandle."""

    def test_start_sets_downloading(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(handle.state, TorrentState.STOPPED)
                    await handle.start()
                    self.assertEqual(handle.state, TorrentState.DOWNLOADING)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_stop_sets_stopped(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    await handle.start()
                    await handle.stop()
                    self.assertEqual(handle.state, TorrentState.STOPPED)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_stop_when_already_stopped(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    await handle.stop()  # no-op, shouldn't error

            asyncio.run(run())
        finally:
            path.unlink()


class TestAddTorrentStart(unittest.TestCase):
    """Test start= kwarg on add_torrent*()."""

    def test_start_false_stays_stopped(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    self.assertEqual(handle.state, TorrentState.STOPPED)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_start_true_begins_downloading(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta, start=True)
                    self.assertEqual(handle.state, TorrentState.DOWNLOADING)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_start_on_duplicate_starts_stopped(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    h1 = await client.add_torrent(meta)
                    self.assertEqual(h1.state, TorrentState.STOPPED)
                    h2 = await client.add_torrent(meta, start=True)
                    self.assertEqual(h2.state, TorrentState.DOWNLOADING)

            asyncio.run(run())
        finally:
            path.unlink()

    def test_start_on_duplicate_already_running_noop(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    h1 = await client.add_torrent(meta, start=True)
                    self.assertEqual(h1.state, TorrentState.DOWNLOADING)
                    # Re-add with start=True — already running, stays DOWNLOADING
                    h2 = await client.add_torrent(meta, start=True)
                    self.assertEqual(h2.state, TorrentState.DOWNLOADING)

            asyncio.run(run())
        finally:
            path.unlink()


class TestClientContextManager(unittest.TestCase):
    """Test Client lifecycle."""

    def test_not_running_raises(self) -> None:
        async def run() -> None:
            client = Client(storage=_MemoryStorage())
            meta, path = _make_torrent()
            try:
                with self.assertRaises(RuntimeError):
                    await client.add_torrent(meta)
            finally:
                path.unlink()

        asyncio.run(run())


class TestWaitTimeout(unittest.TestCase):
    """Test handle.wait() with asyncio.timeout."""

    def test_wait_times_out(self) -> None:
        meta, path = _make_torrent()
        try:

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:
                    handle = await client.add_torrent(meta)
                    with self.assertRaises(TimeoutError):
                        async with asyncio.timeout(0.05):
                            await handle.wait()

            asyncio.run(run())
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
