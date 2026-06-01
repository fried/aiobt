"""Tests for resume data persistence and integrity verification."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import later.unittest

from aiobt.create import create_torrent
from aiobt.resume import (
    ResumeData,
    _bitfield_to_have,
    _have_to_bitfield,
    load_resume,
    resume_path,
    save_resume,
)


class BitfieldRoundtripTest(later.unittest.TestCase):
    """Test bitfield ↔ have-set conversion."""

    async def test_empty(self) -> None:
        have: frozenset[int] = frozenset()
        bf = _have_to_bitfield(have, 16)
        self.assertEqual(len(bf), 2)
        self.assertEqual(_bitfield_to_have(bf, 16), have)

    async def test_all_set(self) -> None:
        have = frozenset(range(16))
        bf = _have_to_bitfield(have, 16)
        self.assertEqual(bf, b"\xff\xff")
        self.assertEqual(_bitfield_to_have(bf, 16), have)

    async def test_partial(self) -> None:
        have = frozenset({0, 3, 7, 15})
        bf = _have_to_bitfield(have, 16)
        result = _bitfield_to_have(bf, 16)
        self.assertEqual(result, have)

    async def test_non_byte_aligned(self) -> None:
        """Piece count not a multiple of 8."""
        have = frozenset({0, 4})
        bf = _have_to_bitfield(have, 5)
        self.assertEqual(len(bf), 1)
        result = _bitfield_to_have(bf, 5)
        self.assertEqual(result, have)

    async def test_large(self) -> None:
        have = frozenset({0, 100, 255, 999})
        bf = _have_to_bitfield(have, 1000)
        result = _bitfield_to_have(bf, 1000)
        self.assertEqual(result, have)


class SaveLoadTest(later.unittest.TestCase):
    """Test save/load roundtrip."""

    async def test_roundtrip(self) -> None:
        info_hash = os.urandom(20)
        have = frozenset({0, 3, 7, 15})

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.resume"
            await save_resume(
                path,
                info_hash=info_hash,
                have=have,
                piece_count=16,
                downloaded=65536,
                uploaded=1024,
            )

            data = load_resume(path, info_hash)
            self.assertIsNotNone(data)
            self.assertEqual(data.info_hash, info_hash)
            self.assertEqual(data.have, have)
            self.assertEqual(data.downloaded, 65536)
            self.assertEqual(data.uploaded, 1024)

    async def test_missing_file(self) -> None:
        data = load_resume(Path("/nonexistent/path"), os.urandom(20))
        self.assertIsNone(data)

    async def test_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.resume"
            path.write_bytes(b"not valid bencode !@#$")
            data = load_resume(path, os.urandom(20))
            self.assertIsNone(data)

    async def test_wrong_info_hash(self) -> None:
        hash_a = os.urandom(20)
        hash_b = os.urandom(20)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.resume"
            await save_resume(
                path,
                info_hash=hash_a,
                have=frozenset({0}),
                piece_count=8,
            )
            data = load_resume(path, hash_b)
            self.assertIsNone(data)

    async def test_atomic_overwrite(self) -> None:
        """Saving again overwrites the previous data."""
        info_hash = os.urandom(20)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.resume"
            await save_resume(
                path,
                info_hash=info_hash,
                have=frozenset({0, 1}),
                piece_count=8,
            )
            await save_resume(
                path,
                info_hash=info_hash,
                have=frozenset({0, 1, 2, 3}),
                piece_count=8,
                downloaded=999,
            )
            data = load_resume(path, info_hash)
            self.assertEqual(data.have, frozenset({0, 1, 2, 3}))
            self.assertEqual(data.downloaded, 999)

    async def test_creates_parent_dirs(self) -> None:
        info_hash = os.urandom(20)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "dir" / "test.resume"
            await save_resume(
                path,
                info_hash=info_hash,
                have=frozenset({0}),
                piece_count=1,
            )
            self.assertTrue(path.exists())


class ResumePathTest(later.unittest.TestCase):
    async def test_hex_naming(self) -> None:
        info_hash = bytes.fromhex("abcdef0123456789abcdef0123456789abcdef01")
        path = resume_path(Path("/state"), info_hash)
        self.assertEqual(
            path, Path("/state/abcdef0123456789abcdef0123456789abcdef01.resume")
        )


class ResumeIntegrationTest(later.unittest.TestCase):
    """Integration: resume check verifies pieces from storage."""

    async def test_resume_skips_completed_pieces(self) -> None:
        """After resume, already-verified pieces are not re-downloaded."""
        from aiobt import Client, ClientConfig, TorrentState
        from aiobt.network import NetworkConfig
        from aiobt.storage import CompactStorage

        data_size = 256 * 1024  # 256 KiB
        original = os.urandom(data_size)
        piece_length = 64 * 1024  # 4 pieces

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            state_dir = base_path / "state"
            state_dir.mkdir()

            # Create torrent from temp file
            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)
            meta = create_torrent(path=[str(src_file)], piece_length=piece_length)
            info_hash = meta.info_hash

            # --- Step 1: Simulate a partial download (pieces 0, 1 done) ---
            stor1 = CompactStorage(base_path / "dl")
            await stor1.open(meta.total_length, piece_length)
            await stor1.prepare(info_hash.hex())
            # Write first two pieces
            await stor1.write(0, original[: 2 * piece_length])

            # Save resume data claiming pieces 0 and 1
            rp = resume_path(state_dir, info_hash)
            await save_resume(
                rp,
                info_hash=info_hash,
                have=frozenset({0, 1}),
                piece_count=meta.piece_count,
                downloaded=2 * piece_length,
            )
            await stor1.close()

            # --- Step 2: "Restart" with same storage, check resume ---
            stor2 = CompactStorage(base_path / "dl")
            await stor2.open(meta.total_length, piece_length)
            await stor2.prepare(info_hash.hex())

            no_lsd = NetworkConfig(lsd_enabled=False)
            cfg = ClientConfig(listen_port=0, network=no_lsd, state_dir=state_dir)

            async with Client(storage=stor2, config=cfg) as client:
                handle = await client.add_torrent(meta)
                # start() triggers check_resume
                await handle.start()

                # Pieces 0 and 1 should be recovered
                self.assertIn(0, handle._session.tracker.have)
                self.assertIn(1, handle._session.tracker.have)
                self.assertNotIn(2, handle._session.tracker.have)
                self.assertNotIn(3, handle._session.tracker.have)

                # State should be DOWNLOADING (not complete)
                self.assertEqual(handle.state, TorrentState.DOWNLOADING)

                # Downloaded bytes should reflect 2 verified pieces
                self.assertEqual(handle._session.bytes_downloaded, 2 * piece_length)

    async def test_resume_corrupt_piece_rejected(self) -> None:
        """Corrupt piece data is not loaded on resume."""
        from aiobt import Client, ClientConfig
        from aiobt.network import NetworkConfig
        from aiobt.storage import CompactStorage

        data_size = 128 * 1024
        original = os.urandom(data_size)
        piece_length = 64 * 1024

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            state_dir = base_path / "state"
            state_dir.mkdir()

            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)
            meta = create_torrent(path=[str(src_file)], piece_length=piece_length)

            stor = CompactStorage(base_path / "dl")
            await stor.open(meta.total_length, piece_length)
            await stor.prepare(meta.info_hash.hex())
            # Write correct piece 0, corrupt piece 1
            await stor.write(0, original[:piece_length])
            await stor.write(piece_length, b"\x00" * piece_length)

            rp = resume_path(state_dir, meta.info_hash)
            await save_resume(
                rp,
                info_hash=meta.info_hash,
                have=frozenset({0, 1}),
                piece_count=meta.piece_count,
            )

            no_lsd = NetworkConfig(lsd_enabled=False)
            cfg = ClientConfig(listen_port=0, network=no_lsd, state_dir=state_dir)
            async with Client(storage=stor, config=cfg) as client:
                handle = await client.add_torrent(meta)
                await handle.start()

                # Piece 0 passes verification, piece 1 fails
                self.assertIn(0, handle._session.tracker.have)
                self.assertNotIn(1, handle._session.tracker.have)

    async def test_resume_all_complete_enters_seeding(self) -> None:
        """If all pieces verify, handle goes to SEEDING."""
        from aiobt import Client, ClientConfig, TorrentState
        from aiobt.network import NetworkConfig
        from aiobt.storage import CompactStorage

        data_size = 128 * 1024
        original = os.urandom(data_size)
        piece_length = 64 * 1024

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            state_dir = base_path / "state"
            state_dir.mkdir()

            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)
            meta = create_torrent(path=[str(src_file)], piece_length=piece_length)

            stor = CompactStorage(base_path / "dl")
            await stor.open(meta.total_length, piece_length)
            await stor.prepare(meta.info_hash.hex())
            await stor.write(0, original)  # all data correct

            rp = resume_path(state_dir, meta.info_hash)
            await save_resume(
                rp,
                info_hash=meta.info_hash,
                have=frozenset(range(meta.piece_count)),
                piece_count=meta.piece_count,
            )

            no_lsd = NetworkConfig(lsd_enabled=False)
            cfg = ClientConfig(listen_port=0, network=no_lsd, state_dir=state_dir)
            async with Client(storage=stor, config=cfg) as client:
                handle = await client.add_torrent(meta, start=True)

                self.assertTrue(handle.is_complete())
                self.assertEqual(handle.state, TorrentState.SEEDING)

    async def test_piece_verified_saves_resume(self) -> None:
        """PIECE_VERIFIED event triggers resume save."""
        from aiobt import Client, ClientConfig, TorrentState
        from aiobt.network import NetworkConfig
        from aiobt.storage import CompactStorage

        data_size = 256 * 1024
        original = os.urandom(data_size)
        piece_length = 64 * 1024

        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            state_dir = base_path / "state"
            state_dir.mkdir()

            src_file = base_path / "src" / "payload.bin"
            src_file.parent.mkdir()
            src_file.write_bytes(original)
            meta = create_torrent(path=[str(src_file)], piece_length=piece_length)

            # Seeder with all data
            seeder_stor = CompactStorage(base_path / "seeder")
            await seeder_stor.open(meta.total_length, piece_length)
            await seeder_stor.prepare(meta.info_hash.hex())
            await seeder_stor.write(0, original)

            # Leecher with resume enabled
            leech_stor = CompactStorage(base_path / "leech")
            await leech_stor.open(meta.total_length, piece_length)
            await leech_stor.prepare(meta.info_hash.hex())

            no_lsd = NetworkConfig(lsd_enabled=False)
            seeder_cfg = ClientConfig(listen_port=0, network=no_lsd)
            leech_cfg = ClientConfig(listen_port=0, network=no_lsd, state_dir=state_dir)

            async with (
                Client(storage=seeder_stor, config=seeder_cfg) as seeder,
                Client(storage=leech_stor, config=leech_cfg) as leecher,
            ):
                s_handle = await seeder.add_torrent(meta)
                for i in range(meta.piece_count):
                    s_handle._session.tracker.mark_have(i)
                await s_handle.start()

                l_handle = await leecher.add_torrent(meta, start=True)
                await leecher.add_peer("127.0.0.1", seeder.listen_port, meta.info_hash)

                async with asyncio.timeout(30):
                    await l_handle.wait()

                self.assertTrue(l_handle.is_complete())

                # Resume file should exist now
                rp = resume_path(state_dir, meta.info_hash)
                self.assertTrue(rp.exists())

                # Load it back — should have all pieces
                data = load_resume(rp, meta.info_hash)
                self.assertIsNotNone(data)
                self.assertEqual(data.have, frozenset(range(meta.piece_count)))
