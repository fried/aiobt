"""Tests for the BEP 3 choking algorithm."""

from __future__ import annotations

import asyncio

import later.unittest

from aiobt.choking import ChokingManager, PeerRates
from aiobt.protocol import Choke, Unchoke


class _FakePeer:
    """Minimal peer stub for choking tests."""

    def __init__(self) -> None:
        self.is_connected = True
        self.sent: list[object] = []

    async def send_message(self, msg: object) -> None:
        self.sent.append(msg)


class ChokingManagerTest(later.unittest.TestCase):
    """Unit tests for ChokingManager."""

    async def test_unchoke_top_4_interested(self) -> None:
        """Top 4 peers by download rate should be unchoked."""
        mgr = ChokingManager(
            max_unchoked=4, rechoke_interval=100.0, optimistic_interval=300.0
        )

        peers: list[tuple[tuple[str, int], _FakePeer, PeerRates]] = []
        for i in range(6):
            addr = ("127.0.0.1", 6000 + i)
            fake = _FakePeer()
            rates = mgr.register(addr, fake)
            rates.peer_interested = True
            # Higher port number = higher download rate
            rates.bytes_down_interval = (i + 1) * 1000
            peers.append((addr, fake, rates))

        await mgr._rechoke()

        # Top 4 by download rate: peers 2,3,4,5 (highest bytes_down)
        unchoked_addrs = set()
        for addr, fake, rates in peers:
            if not rates.am_choking:
                unchoked_addrs.add(addr)

        self.assertEqual(len(unchoked_addrs), 4)
        # Peers with lowest rates should still be choked
        self.assertTrue(peers[0][2].am_choking)
        self.assertTrue(peers[1][2].am_choking)
        # Peers with highest rates should be unchoked
        for _, _, rates in peers[2:]:
            self.assertFalse(rates.am_choking)

    async def test_choked_peer_gets_no_unchoke_message(self) -> None:
        """Peers not in the top N should remain choked."""
        mgr = ChokingManager(
            max_unchoked=1, rechoke_interval=100.0, optimistic_interval=300.0
        )

        fast_peer = _FakePeer()
        slow_peer = _FakePeer()
        fast_rates = mgr.register(("127.0.0.1", 6001), fast_peer)
        slow_rates = mgr.register(("127.0.0.1", 6002), slow_peer)
        fast_rates.peer_interested = True
        slow_rates.peer_interested = True
        fast_rates.bytes_down_interval = 10000
        slow_rates.bytes_down_interval = 100

        await mgr._rechoke()

        self.assertFalse(fast_rates.am_choking)
        self.assertTrue(slow_rates.am_choking)
        # Fast peer should have received Unchoke
        self.assertTrue(any(isinstance(m, Unchoke) for m in fast_peer.sent))
        # Slow peer should NOT have received Unchoke
        self.assertFalse(any(isinstance(m, Unchoke) for m in slow_peer.sent))

    async def test_not_interested_peers_ignored(self) -> None:
        """Peers that haven't sent Interested shouldn't be unchoked."""
        mgr = ChokingManager(max_unchoked=4, rechoke_interval=100.0)

        peer = _FakePeer()
        rates = mgr.register(("127.0.0.1", 6001), peer)
        rates.peer_interested = False
        rates.bytes_down_interval = 99999

        await mgr._rechoke()

        self.assertTrue(rates.am_choking)
        self.assertFalse(any(isinstance(m, Unchoke) for m in peer.sent))

    async def test_seeding_mode_ranks_by_upload(self) -> None:
        """When seeding, rank peers by bytes uploaded (not downloaded)."""
        mgr = ChokingManager(
            max_unchoked=1, rechoke_interval=100.0, optimistic_interval=300.0
        )
        mgr.is_seeding = True

        fast_up = _FakePeer()
        fast_down = _FakePeer()
        r1 = mgr.register(("127.0.0.1", 6001), fast_up)
        r2 = mgr.register(("127.0.0.1", 6002), fast_down)
        r1.peer_interested = True
        r2.peer_interested = True
        # fast_up has higher upload rate, fast_down has higher download rate
        r1.bytes_up_interval = 10000
        r1.bytes_down_interval = 100
        r2.bytes_up_interval = 100
        r2.bytes_down_interval = 10000

        await mgr._rechoke()

        self.assertFalse(r1.am_choking)  # unchoked (best uploader)
        self.assertTrue(r2.am_choking)  # choked

    async def test_optimistic_unchoke_rotation(self) -> None:
        """Optimistic unchoke should rotate to a choked+interested peer."""
        mgr = ChokingManager(
            max_unchoked=1,
            rechoke_interval=1.0,
            optimistic_interval=1.0,  # optimistic every cycle
        )

        top_peer = _FakePeer()
        bottom_peer = _FakePeer()
        r_top = mgr.register(("127.0.0.1", 6001), top_peer)
        r_bottom = mgr.register(("127.0.0.1", 6002), bottom_peer)
        r_top.peer_interested = True
        r_bottom.peer_interested = True
        r_top.bytes_down_interval = 10000
        r_bottom.bytes_down_interval = 100

        # First rechoke
        await mgr._rechoke()

        # r_top is regular unchoke, r_bottom is the only candidate
        # for optimistic unchoke
        self.assertFalse(r_top.am_choking)
        # With optimistic_interval=1 and rechoke_interval=1,
        # optimistic fires every cycle, so bottom should be unchoked too
        self.assertFalse(r_bottom.am_choking)

    async def test_wake_triggers_immediate_rechoke(self) -> None:
        """Calling wake() should trigger an immediate rechoke cycle."""
        mgr = ChokingManager(
            max_unchoked=4,
            rechoke_interval=600.0,  # long interval
        )
        stop_event = asyncio.Event()

        peer = _FakePeer()
        rates = mgr.register(("127.0.0.1", 6001), peer)

        # Start the manager
        task = asyncio.create_task(mgr.run(stop_event))

        # Wait for first rechoke to complete
        await asyncio.sleep(0.01)

        # Now mark peer as interested and wake
        rates.peer_interested = True
        rates.bytes_down_interval = 5000
        mgr.wake()

        # Give the manager time to process the wake
        await asyncio.sleep(0.05)

        # Should have been unchoked by the wake-triggered rechoke
        self.assertFalse(rates.am_choking)

        stop_event.set()
        await task

    async def test_interval_resets_counters(self) -> None:
        """Byte counters should be reset after each rechoke cycle."""
        mgr = ChokingManager(max_unchoked=4, rechoke_interval=100.0)

        peer = _FakePeer()
        rates = mgr.register(("127.0.0.1", 6001), peer)
        rates.peer_interested = True
        rates.bytes_down_interval = 5000
        rates.bytes_up_interval = 3000

        await mgr._rechoke()

        self.assertEqual(rates.bytes_down_interval, 0)
        self.assertEqual(rates.bytes_up_interval, 0)

    async def test_unregister_removes_peer(self) -> None:
        """Unregistered peers should not be considered in rechoke."""
        mgr = ChokingManager(max_unchoked=4, rechoke_interval=100.0)

        peer = _FakePeer()
        addr = ("127.0.0.1", 6001)
        rates = mgr.register(addr, peer)
        rates.peer_interested = True

        mgr.unregister(addr)

        # Should have no effect (no peers)
        await mgr._rechoke()
        self.assertTrue(rates.am_choking)  # never got unchoked
