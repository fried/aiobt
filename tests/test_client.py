"""Tests for TorrentHandle and the Client public API."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from aiobt.client import Client, TorrentHandle, TorrentState, TorrentStats
from aiobt.create import create_torrent
from aiobt.events import TorrentEvent
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


class TestClientTorrentEventBubbling(unittest.TestCase):
    """TorrentEvents registered on the Client fire for all torrents."""

    def test_state_changed_bubbles_to_client(self) -> None:
        meta, path = _make_torrent()
        try:
            results: list[tuple[str, object, object]] = []

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:

                    async def on_state(handle, old, new):
                        results.append((handle.name, old, new))

                    client.on(TorrentEvent.STATE_CHANGED, on_state)
                    handle = await client.add_torrent(meta)
                    await handle.start()

            asyncio.run(run())
            # check_resume emits STOPPED→CHECKING, then start emits CHECKING→DOWNLOADING
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0][0], meta.info.name)
            self.assertEqual(results[0][1], TorrentState.STOPPED)
            self.assertEqual(results[0][2], TorrentState.CHECKING)
            self.assertEqual(results[1][0], meta.info.name)
            self.assertEqual(results[1][1], TorrentState.CHECKING)
            self.assertEqual(results[1][2], TorrentState.DOWNLOADING)
        finally:
            path.unlink()

    def test_client_torrent_event_fires_for_multiple_torrents(self) -> None:
        meta1, path1 = _make_torrent()
        # Create a second distinct torrent
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f2:
            f2.write(os.urandom(30_000))
            f2.flush()
            path2 = Path(f2.name)
        meta2 = create_torrent(path2, trackers=["udp://t:6969/announce"])
        try:
            names: list[str] = []

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:

                    async def on_state(handle, old, new):
                        names.append(handle.name)

                    client.on(TorrentEvent.STATE_CHANGED, on_state)
                    h1 = await client.add_torrent(meta1)
                    h2 = await client.add_torrent(meta2)
                    await h1.start()
                    await h2.start()

            asyncio.run(run())
            # 2 state changes per torrent: STOPPED→CHECKING and CHECKING→DOWNLOADING
            self.assertEqual(len(names), 4)
            self.assertIn(meta1.info.name, names)
            self.assertIn(meta2.info.name, names)
        finally:
            path1.unlink()
            path2.unlink()

    def test_handle_and_client_both_fire(self) -> None:
        """Per-torrent and client-level listeners both fire."""
        meta, path = _make_torrent()
        try:
            sources: list[str] = []

            async def run() -> None:
                async with Client(storage=_MemoryStorage()) as client:

                    async def client_cb(handle, old, new):
                        sources.append("client")

                    async def handle_cb(handle, old, new):
                        sources.append("handle")

                    client.on(TorrentEvent.STATE_CHANGED, client_cb)
                    handle = await client.add_torrent(meta)
                    handle.on(TorrentEvent.STATE_CHANGED, handle_cb)
                    await handle.start()

            asyncio.run(run())
            self.assertIn("client", sources)
            self.assertIn("handle", sources)
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
