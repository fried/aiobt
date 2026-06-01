"""Integration test: three-client transfer via direct peer connection.

Flow
----
1.  Generate 1 MiB of random data.
2.  Write to a temp file, ``create_torrent()`` to get ``TorrentMeta``.
3.  **Seeder**: ``CompactStorage`` pre-populated with the data, all
    pieces marked ``have``.
4.  **Leecher 1** and **Leecher 2**: empty ``CompactStorage``.
5.  All three clients listen on port ``0`` (OS-assigned).
6.  All add the same torrent.  Seeder is started (seeding).
    Leechers are started (downloading).
7.  Leechers call ``add_peer(seeder_host, seeder_port)`` to discover
    the seeder directly (no multicast required).
8.  Wait for both leechers to complete (bounded by timeout).
9.  Read back data from both leecher storages, assert ``== original``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import later.unittest

from aiobt import Client, ClientConfig, TorrentMeta, TorrentState
from aiobt.create import create_torrent
from aiobt.storage import CompactStorage


class TransferTest(later.unittest.TestCase):
    """End-to-end transfer: seeder → two leechers."""

    async def test_three_client_transfer(self) -> None:
        """Seeder shares 1 MiB of random data to two leechers."""
        data_size = 1 * 1024 * 1024  # 1 MiB
        original = os.urandom(data_size)

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)

            # ── create torrent metadata from a temp file ──────────────
            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)

            meta: TorrentMeta = create_torrent(
                path=[str(src_file)],
                piece_length=64 * 1024,  # 64 KiB pieces → 16 pieces
            )
            info_hash = meta.info_hash
            piece_count = meta.piece_count

            # ── prepare three separate storage directories ────────────
            seeder_dir = base_path / "seeder"
            leech1_dir = base_path / "leech1"
            leech2_dir = base_path / "leech2"

            seeder_storage = CompactStorage(seeder_dir)
            leech1_storage = CompactStorage(leech1_dir)
            leech2_storage = CompactStorage(leech2_dir)

            # Pre-populate seeder storage
            await seeder_storage.open(meta.total_length, meta.info.piece_length)
            await seeder_storage.prepare(info_hash.hex())
            await seeder_storage.write(0, original)

            # Open leecher storage
            await leech1_storage.open(meta.total_length, meta.info.piece_length)
            await leech1_storage.prepare(info_hash.hex())
            await leech2_storage.open(meta.total_length, meta.info.piece_length)
            await leech2_storage.prepare(info_hash.hex())

            # ── create clients (port 0 = OS-assigned) ────────────────
            seeder_cfg = ClientConfig(listen_port=0)
            leech1_cfg = ClientConfig(listen_port=0)
            leech2_cfg = ClientConfig(listen_port=0)

            async with (
                Client(storage=seeder_storage, config=seeder_cfg) as seeder,
                Client(storage=leech1_storage, config=leech1_cfg) as leech1,
                Client(storage=leech2_storage, config=leech2_cfg) as leech2,
            ):
                # ── add torrent to all three ──────────────────────────
                s_handle = await seeder.add_torrent(meta)
                l1_handle = await leech1.add_torrent(meta)
                l2_handle = await leech2.add_torrent(meta)

                # Pre-mark all pieces as have on the seeder
                for i in range(piece_count):
                    s_handle._session.tracker.mark_have(i)

                # Start all (seeder should be SEEDING, leechers DOWNLOADING)
                await s_handle.start()
                await l1_handle.start()
                await l2_handle.start()

                self.assertEqual(s_handle.state, TorrentState.SEEDING)
                self.assertEqual(l1_handle.state, TorrentState.DOWNLOADING)
                self.assertEqual(l2_handle.state, TorrentState.DOWNLOADING)

                # ── connect leechers to seeder ────────────────────────
                seeder_port = seeder.listen_port
                self.assertGreater(seeder_port, 0)

                await leech1.add_peer("127.0.0.1", seeder_port, info_hash)
                await leech2.add_peer("127.0.0.1", seeder_port, info_hash)

                # ── wait for completion (30s timeout) ─────────────────
                async with asyncio.timeout(30):
                    await asyncio.gather(
                        l1_handle.wait(),
                        l2_handle.wait(),
                    )

                # ── verify ────────────────────────────────────────────
                self.assertTrue(l1_handle.is_complete())
                self.assertTrue(l2_handle.is_complete())

                l1_data = await leech1_storage.read(0, data_size)
                l2_data = await leech2_storage.read(0, data_size)

                self.assertEqual(l1_data, original)
                self.assertEqual(l2_data, original)

                # Sanity: check progress is 1.0
                self.assertAlmostEqual(l1_handle.progress, 1.0)
                self.assertAlmostEqual(l2_handle.progress, 1.0)
