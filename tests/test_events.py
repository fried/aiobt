"""Tests for aiobt.events — EventEmitter, ClientEvent, TorrentEvent."""

from __future__ import annotations

import asyncio
import unittest

from aiobt.events import ClientEvent, EventEmitter, TorrentEvent


class TestEventEmitter(unittest.TestCase):
    """Core EventEmitter behavior."""

    def test_on_and_emit(self) -> None:
        results: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb(msg: str) -> None:
                results.append(msg)

            em.on(TorrentEvent.COMPLETED, cb)
            await em.emit(TorrentEvent.COMPLETED, "done")

        asyncio.run(run())
        self.assertEqual(results, ["done"])

    def test_multiple_listeners(self) -> None:
        results: list[int] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb1(x: int) -> None:
                results.append(x)

            async def cb2(x: int) -> None:
                results.append(x * 10)

            em.on(TorrentEvent.PIECE_VERIFIED, cb1)
            em.on(TorrentEvent.PIECE_VERIFIED, cb2)
            await em.emit(TorrentEvent.PIECE_VERIFIED, 5)

        asyncio.run(run())
        self.assertIn(5, results)
        self.assertIn(50, results)

    def test_off_removes_listener(self) -> None:
        results: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb(msg: str) -> None:
                results.append(msg)

            em.on(TorrentEvent.COMPLETED, cb)
            em.off(TorrentEvent.COMPLETED, cb)
            await em.emit(TorrentEvent.COMPLETED, "should not appear")

        asyncio.run(run())
        self.assertEqual(results, [])

    def test_off_nonexistent_is_silent(self) -> None:
        async def run() -> None:
            em = EventEmitter()

            async def cb() -> None:
                pass

            em.off(TorrentEvent.COMPLETED, cb)  # should not raise

        asyncio.run(run())

    def test_once_fires_once(self) -> None:
        results: list[int] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb(x: int) -> None:
                results.append(x)

            em.once(TorrentEvent.COMPLETED, cb)
            await em.emit(TorrentEvent.COMPLETED, 1)
            await em.emit(TorrentEvent.COMPLETED, 2)

        asyncio.run(run())
        self.assertEqual(results, [1])

    def test_emit_no_listeners(self) -> None:
        async def run() -> None:
            em = EventEmitter()
            await em.emit(TorrentEvent.COMPLETED)  # should not raise

        asyncio.run(run())

    def test_emit_propagates_error(self) -> None:
        async def run() -> None:
            em = EventEmitter()

            async def bad() -> None:
                raise ValueError("boom")

            em.on(TorrentEvent.ERROR, bad)
            with self.assertRaises(ValueError):
                await em.emit(TorrentEvent.ERROR)

        asyncio.run(run())

    def test_emit_suppress_errors(self) -> None:
        results: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def bad() -> None:
                raise ValueError("boom")

            async def good() -> None:
                results.append("ok")

            em.on(TorrentEvent.ERROR, bad)
            em.on(TorrentEvent.ERROR, good)
            # Both run; error is swallowed
            await em.emit(TorrentEvent.ERROR, suppress_errors=True)

        asyncio.run(run())
        self.assertEqual(results, ["ok"])

    def test_listener_count(self) -> None:
        async def run() -> None:
            em = EventEmitter()

            async def cb() -> None:
                pass

            self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 0)
            em.on(TorrentEvent.COMPLETED, cb)
            self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 1)
            em.on(TorrentEvent.COMPLETED, cb)
            self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 2)

        asyncio.run(run())

    def test_clear_specific_event(self) -> None:
        async def run() -> None:
            em = EventEmitter()

            async def cb() -> None:
                pass

            em.on(TorrentEvent.COMPLETED, cb)
            em.on(TorrentEvent.ERROR, cb)
            em.clear(TorrentEvent.COMPLETED)
            self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 0)
            self.assertEqual(em.listener_count(TorrentEvent.ERROR), 1)

        asyncio.run(run())

    def test_clear_all(self) -> None:
        async def run() -> None:
            em = EventEmitter()

            async def cb() -> None:
                pass

            em.on(TorrentEvent.COMPLETED, cb)
            em.on(TorrentEvent.ERROR, cb)
            em.clear()
            self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 0)
            self.assertEqual(em.listener_count(TorrentEvent.ERROR), 0)

        asyncio.run(run())

    def test_on_returns_callback_for_decorator(self) -> None:
        em = EventEmitter()

        @em.on(TorrentEvent.COMPLETED)
        async def my_cb() -> None:
            pass

        self.assertEqual(em.listener_count(TorrentEvent.COMPLETED), 1)

    def test_multiple_args_passed(self) -> None:
        results: list[tuple[object, ...]] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb(*args: object) -> None:
                results.append(args)

            em.on(TorrentEvent.STATE_CHANGED, cb)
            await em.emit(TorrentEvent.STATE_CHANGED, "handle", "old", "new")

        asyncio.run(run())
        self.assertEqual(results, [("handle", "old", "new")])

    def test_client_event_types(self) -> None:
        results: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb(name: str) -> None:
                results.append(name)

            em.on(ClientEvent.TORRENT_ADDED, cb)
            await em.emit(ClientEvent.TORRENT_ADDED, "test.torrent")

        asyncio.run(run())
        self.assertEqual(results, ["test.torrent"])

    def test_different_events_isolated(self) -> None:
        results: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def cb_a() -> None:
                results.append("a")

            async def cb_b() -> None:
                results.append("b")

            em.on(TorrentEvent.COMPLETED, cb_a)
            em.on(TorrentEvent.ERROR, cb_b)
            await em.emit(TorrentEvent.COMPLETED)

        asyncio.run(run())
        self.assertEqual(results, ["a"])

    def test_concurrent_execution(self) -> None:
        """Verify callbacks run concurrently, not sequentially."""
        order: list[str] = []

        async def run() -> None:
            em = EventEmitter()

            async def slow() -> None:
                order.append("slow_start")
                await asyncio.sleep(0.05)
                order.append("slow_end")

            async def fast() -> None:
                order.append("fast")

            em.on(TorrentEvent.COMPLETED, slow)
            em.on(TorrentEvent.COMPLETED, fast)
            await em.emit(TorrentEvent.COMPLETED)

        asyncio.run(run())
        # fast should appear before slow_end because they run concurrently
        self.assertIn("fast", order)
        self.assertIn("slow_end", order)
        fast_idx = order.index("fast")
        slow_end_idx = order.index("slow_end")
        self.assertLess(fast_idx, slow_end_idx)


class TestEventEnumValues(unittest.TestCase):
    """Ensure enum members have stable string values."""

    def test_client_events(self) -> None:
        self.assertEqual(ClientEvent.TORRENT_ADDED.value, "torrent_added")
        self.assertEqual(ClientEvent.TORRENT_REMOVED.value, "torrent_removed")
        self.assertEqual(ClientEvent.TORRENT_COMPLETED.value, "torrent_completed")
        self.assertEqual(ClientEvent.TORRENT_ERROR.value, "torrent_error")

    def test_torrent_events(self) -> None:
        self.assertEqual(TorrentEvent.STATE_CHANGED.value, "state_changed")
        self.assertEqual(TorrentEvent.PIECE_VERIFIED.value, "piece_verified")
        self.assertEqual(TorrentEvent.PEER_CONNECTED.value, "peer_connected")
        self.assertEqual(TorrentEvent.PEER_DISCONNECTED.value, "peer_disconnected")
        self.assertEqual(TorrentEvent.TRACKER_RESPONSE.value, "tracker_response")
        self.assertEqual(TorrentEvent.TRACKER_FAILED.value, "tracker_failed")
        self.assertEqual(TorrentEvent.COMPLETED.value, "completed")
        self.assertEqual(TorrentEvent.ERROR.value, "error")


if __name__ == "__main__":
    unittest.main()
