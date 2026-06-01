"""Tests for endgame mode."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import later.unittest

from aiobt import Client, ClientConfig, TorrentMeta, TorrentState
from aiobt.create import create_torrent
from aiobt.engine import EndgameState
from aiobt.network import NetworkConfig
from aiobt.storage import CompactStorage


class EndgameStateTest(later.unittest.TestCase):
    """Unit tests for EndgameState tracking."""

    async def test_enter_endgame(self) -> None:
        """EndgameState.enter() activates endgame with correct pieces."""
        eg = EndgameState()
        self.assertFalse(eg.active)

        eg.enter({0, 1, 2})
        self.assertTrue(eg.active)
        self.assertEqual(eg.pieces, {0, 1, 2})

    async def test_record_request(self) -> None:
        """record_request tracks which addrs have requests for a piece."""
        eg = EndgameState()
        eg.enter({5, 6})
        eg.record_request(5, ("127.0.0.1", 6001))
        eg.record_request(5, ("127.0.0.1", 6002))
        eg.record_request(6, ("127.0.0.1", 6001))

        self.assertEqual(
            eg.peer_requests[5],
            {("127.0.0.1", 6001), ("127.0.0.1", 6002)},
        )
        self.assertEqual(
            eg.peer_requests[6],
            {("127.0.0.1", 6001)},
        )

    async def test_piece_done_returns_cancel_addrs(self) -> None:
        """piece_done() removes the piece and returns addrs needing Cancel."""
        eg = EndgameState()
        eg.enter({5, 6})
        eg.record_request(5, ("127.0.0.1", 6001))
        eg.record_request(5, ("127.0.0.1", 6002))

        addrs = eg.piece_done(5)
        self.assertEqual(addrs, {("127.0.0.1", 6001), ("127.0.0.1", 6002)})
        self.assertNotIn(5, eg.pieces)
        self.assertTrue(eg.active)  # piece 6 still pending

    async def test_endgame_deactivates_when_all_done(self) -> None:
        """Endgame deactivates when the last piece is completed."""
        eg = EndgameState()
        eg.enter({5})
        eg.record_request(5, ("127.0.0.1", 6001))

        eg.piece_done(5)
        self.assertFalse(eg.active)
        self.assertEqual(eg.pieces, set())


class EndgameTransferTest(later.unittest.TestCase):
    """Integration test: endgame mode during multi-peer transfer."""

    async def test_endgame_with_two_seeders(self) -> None:
        """Two seeders each have a subset of pieces — endgame fills the gaps.

        Seeder A has pieces 0-7, seeder B has pieces 8-15.
        The leecher starts downloading from A (pieces 0-7 available).
        When connecting to B, pieces 8-15 become available.
        Endgame should activate when all remaining pieces are pending.
        """
        data_size = 1 * 1024 * 1024  # 1 MiB
        original = os.urandom(data_size)
        piece_length = 64 * 1024  # 16 pieces

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)

            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)

            meta: TorrentMeta = create_torrent(
                path=[str(src_file)],
                piece_length=piece_length,
            )
            info_hash = meta.info_hash
            piece_count = meta.piece_count

            # Two seeders: each has all pieces (both are full seeders)
            sa_dir = base_path / "seeder_a"
            sb_dir = base_path / "seeder_b"
            leech_dir = base_path / "leecher"

            sa_storage = CompactStorage(sa_dir)
            sb_storage = CompactStorage(sb_dir)
            leech_storage = CompactStorage(leech_dir)

            # Pre-populate both seeders with full data
            for st in (sa_storage, sb_storage):
                await st.open(meta.total_length, piece_length)
                await st.prepare(info_hash.hex())
                await st.write(0, original)

            await leech_storage.open(meta.total_length, piece_length)
            await leech_storage.prepare(info_hash.hex())

            no_lsd = NetworkConfig(lsd_enabled=False)
            sa_cfg = ClientConfig(listen_port=0, network=no_lsd)
            sb_cfg = ClientConfig(listen_port=0, network=no_lsd)
            leech_cfg = ClientConfig(listen_port=0, network=no_lsd)

            async with (
                Client(storage=sa_storage, config=sa_cfg) as seeder_a,
                Client(storage=sb_storage, config=sb_cfg) as seeder_b,
                Client(storage=leech_storage, config=leech_cfg) as leecher,
            ):
                sa_h = await seeder_a.add_torrent(meta)
                sb_h = await seeder_b.add_torrent(meta)
                l_h = await leecher.add_torrent(meta)

                # Mark all pieces as have on both seeders
                for i in range(piece_count):
                    sa_h._session.tracker.mark_have(i)
                    sb_h._session.tracker.mark_have(i)

                await sa_h.start()
                await sb_h.start()
                await l_h.start()

                self.assertEqual(sa_h.state, TorrentState.SEEDING)
                self.assertEqual(sb_h.state, TorrentState.SEEDING)
                self.assertEqual(l_h.state, TorrentState.DOWNLOADING)

                # Connect leecher to both seeders
                await leecher.add_peer("127.0.0.1", seeder_a.listen_port, info_hash)
                await leecher.add_peer("127.0.0.1", seeder_b.listen_port, info_hash)

                async with asyncio.timeout(30):
                    await l_h.wait()

                self.assertTrue(l_h.is_complete())
                result = await leech_storage.read(0, data_size)
                self.assertEqual(result, original)
