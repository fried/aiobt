"""Tests for aiobt.create — torrent file creation and piece-size selection."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from aiobt.create import (
    _MIN_PIECE_SIZE,
    _MAX_PIECE_SIZE,
    _hash_pieces,
    _scan_path,
    create_torrent,
    optimal_piece_size,
    torrent_to_bytes,
)
from aiobt.torrent import parse_torrent_bytes, TorrentMeta

# ---------------------------------------------------------------------------
# optimal_piece_size
# ---------------------------------------------------------------------------


class TestOptimalPieceSize(unittest.TestCase):
    """Test automatic piece size selection."""

    def test_tiny_file_gets_minimum(self) -> None:
        self.assertEqual(optimal_piece_size(1024), _MIN_PIECE_SIZE)

    def test_zero_gets_minimum(self) -> None:
        self.assertEqual(optimal_piece_size(0), _MIN_PIECE_SIZE)

    def test_negative_gets_minimum(self) -> None:
        self.assertEqual(optimal_piece_size(-100), _MIN_PIECE_SIZE)

    def test_always_power_of_two(self) -> None:
        sizes = [
            1024,
            1_000_000,
            100_000_000,
            700_000_000,
            4_000_000_000,
            50_000_000_000,
            500_000_000_000,
        ]
        for s in sizes:
            ps = optimal_piece_size(s)
            self.assertEqual(ps & (ps - 1), 0, f"Not power of 2 for {s}: {ps}")

    def test_capped_at_max(self) -> None:
        ps = optimal_piece_size(500_000_000_000)
        self.assertLessEqual(ps, _MAX_PIECE_SIZE)

    def test_floor_at_min(self) -> None:
        ps = optimal_piece_size(100)
        self.assertGreaterEqual(ps, _MIN_PIECE_SIZE)

    def test_700mb(self) -> None:
        ps = optimal_piece_size(700 * 1024 * 1024)
        self.assertEqual(ps, 512 * 1024)  # 512 KiB

    def test_4gb(self) -> None:
        ps = optimal_piece_size(4_000_000_000)
        self.assertEqual(ps, 4 * 1024 * 1024)  # 4 MiB


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


class TestScanPath(unittest.TestCase):
    """Test _scan_path for single files and directories."""

    def test_single_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"hello world")
            f.flush()
            path = Path(f.name)
        try:
            name, specs = _scan_path(path)
            self.assertEqual(name, path.name)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].length, 11)
            self.assertEqual(specs[0].torrent_path, (path.name,))
        finally:
            path.unlink()

    def test_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "mydir"
            root.mkdir()
            (root / "a.txt").write_text("aaaa")
            (root / "b.txt").write_text("bbbbbb")
            sub = root / "sub"
            sub.mkdir()
            (sub / "c.txt").write_text("cc")

            name, specs = _scan_path(root)
            self.assertEqual(name, "mydir")
            self.assertEqual(len(specs), 3)
            # Sorted order: a.txt, b.txt, sub/c.txt
            paths = [s.torrent_path for s in specs]
            self.assertIn(("a.txt",), paths)
            self.assertIn(("b.txt",), paths)
            self.assertIn(("sub", "c.txt"), paths)

    def test_skips_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "test"
            root.mkdir()
            (root / "visible.txt").write_text("yes")
            (root / ".hidden").write_text("no")

            _, specs = _scan_path(root)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].torrent_path, ("visible.txt",))

    def test_skips_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "test"
            root.mkdir()
            (root / "full.txt").write_text("content")
            (root / "empty.txt").write_text("")

            _, specs = _scan_path(root)
            self.assertEqual(len(specs), 1)

    def test_nonexistent_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            _scan_path(Path("/nonexistent/path/xyz"))


# ---------------------------------------------------------------------------
# Piece hashing
# ---------------------------------------------------------------------------


class TestHashPieces(unittest.TestCase):
    """Test _hash_pieces correctness."""

    def test_single_piece(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            data = b"A" * 100
            f.write(data)
            f.flush()
            path = Path(f.name)
        try:
            from aiobt.create import _FileSpec

            specs = [_FileSpec(disk_path=path, torrent_path=("f.bin",), length=100)]
            pieces = _hash_pieces(specs, 1024)
            self.assertEqual(len(pieces), 20)  # one SHA-1
            self.assertEqual(pieces, hashlib.sha1(data).digest())
        finally:
            path.unlink()

    def test_multiple_pieces(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            data = b"X" * 100
            f.write(data)
            f.flush()
            path = Path(f.name)
        try:
            from aiobt.create import _FileSpec

            specs = [_FileSpec(disk_path=path, torrent_path=("f.bin",), length=100)]
            # Piece size 30 → 4 pieces (30+30+30+10)
            pieces = _hash_pieces(specs, 30)
            self.assertEqual(len(pieces), 4 * 20)
            # First piece should hash 30 X's
            self.assertEqual(pieces[:20], hashlib.sha1(b"X" * 30).digest())
        finally:
            path.unlink()

    def test_cross_file_piece(self) -> None:
        """Pieces that span two files."""
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "a.bin"
            p2 = Path(d) / "b.bin"
            p1.write_bytes(b"A" * 10)
            p2.write_bytes(b"B" * 10)

            from aiobt.create import _FileSpec

            specs = [
                _FileSpec(disk_path=p1, torrent_path=("a.bin",), length=10),
                _FileSpec(disk_path=p2, torrent_path=("b.bin",), length=10),
            ]
            # Piece size 16 → piece 1 = 10 A's + 6 B's, piece 2 = 4 B's
            pieces = _hash_pieces(specs, 16)
            self.assertEqual(len(pieces), 2 * 20)
            expected_p1 = hashlib.sha1(b"A" * 10 + b"B" * 6).digest()
            expected_p2 = hashlib.sha1(b"B" * 4).digest()
            self.assertEqual(pieces[:20], expected_p1)
            self.assertEqual(pieces[20:40], expected_p2)


# ---------------------------------------------------------------------------
# create_torrent — single file
# ---------------------------------------------------------------------------


class TestCreateTorrentSingleFile(unittest.TestCase):
    """Test create_torrent with a single file."""

    def test_basic(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".iso", delete=False) as f:
            data = os.urandom(50_000)
            f.write(data)
            f.flush()
            path = Path(f.name)
        try:
            meta = create_torrent(path)
            self.assertIsInstance(meta, TorrentMeta)
            self.assertTrue(meta.info.is_single_file)
            self.assertEqual(meta.info.length, 50_000)
            self.assertIsNone(meta.info.files)
            self.assertEqual(meta.info.name, path.name)
            self.assertIsNotNone(meta.info_hash)
            self.assertEqual(len(meta.info_hash), 20)
            self.assertEqual(meta.created_by, "aiobt")
            self.assertIsNotNone(meta.creation_date)
        finally:
            path.unlink()

    def test_with_tracker(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            f.flush()
            path = Path(f.name)
        try:
            meta = create_torrent(
                path,
                trackers=["udp://tracker.example.com:6969/announce"],
            )
            self.assertEqual(meta.announce, "udp://tracker.example.com:6969/announce")
            self.assertIsNotNone(meta.announce_list)
        finally:
            path.unlink()

    def test_custom_piece_length(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(os.urandom(100_000))
            f.flush()
            path = Path(f.name)
        try:
            meta = create_torrent(path, piece_length=32768)
            self.assertEqual(meta.info.piece_length, 32768)
        finally:
            path.unlink()

    def test_invalid_piece_length(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x")
            f.flush()
            path = Path(f.name)
        try:
            with self.assertRaises(ValueError):
                create_torrent(path, piece_length=1000)  # not power of 2
            with self.assertRaises(ValueError):
                create_torrent(path, piece_length=1024)  # below 16 KiB
        finally:
            path.unlink()

    def test_private_flag(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"private torrent")
            f.flush()
            path = Path(f.name)
        try:
            meta = create_torrent(path, private=True)
            self.assertTrue(meta.info.private)
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# create_torrent — directory (multi-file)
# ---------------------------------------------------------------------------


class TestCreateTorrentMultiFile(unittest.TestCase):
    """Test create_torrent with directories."""

    def test_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "album"
            root.mkdir()
            (root / "track01.flac").write_bytes(os.urandom(10_000))
            (root / "track02.flac").write_bytes(os.urandom(10_000))
            (root / "cover.jpg").write_bytes(os.urandom(5_000))

            meta = create_torrent(root)
            self.assertFalse(meta.info.is_single_file)
            self.assertIsNone(meta.info.length)
            self.assertIsNotNone(meta.info.files)
            self.assertEqual(len(meta.info.files), 3)
            self.assertEqual(meta.info.name, "album")
            self.assertEqual(meta.info.total_length, 25_000)

    def test_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "project"
            root.mkdir()
            sub = root / "src"
            sub.mkdir()
            (root / "README.md").write_bytes(b"hello")
            (sub / "main.py").write_bytes(b"print('hi')")

            meta = create_torrent(root)
            paths = [f.path for f in meta.info.files]
            self.assertIn(("README.md",), paths)
            self.assertIn(("src", "main.py"), paths)

    def test_multi_tier_trackers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "test"
            root.mkdir()
            (root / "f.txt").write_bytes(b"data")

            meta = create_torrent(
                root,
                trackers=[
                    ["udp://primary:6969", "udp://backup:6969"],
                    ["http://fallback:8080/announce"],
                ],
            )
            self.assertEqual(meta.announce, "udp://primary:6969")
            self.assertEqual(len(meta.announce_list), 2)
            self.assertEqual(len(meta.announce_list[0]), 2)
            self.assertEqual(len(meta.announce_list[1]), 1)


# ---------------------------------------------------------------------------
# Round-trip: create → serialize → parse
# ---------------------------------------------------------------------------


class TestRoundTrip(unittest.TestCase):
    """Create a torrent, serialize it, parse it back, verify equality."""

    def test_single_file_round_trip(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            data = os.urandom(20_000)
            f.write(data)
            f.flush()
            path = Path(f.name)
        try:
            original = create_torrent(
                path,
                trackers=["udp://tracker.example.com:6969/announce"],
                comment="test torrent",
            )
            raw = original.to_bytes()
            parsed = parse_torrent_bytes(raw)

            self.assertEqual(parsed.info_hash, original.info_hash)
            self.assertEqual(parsed.info.name, original.info.name)
            self.assertEqual(parsed.info.piece_length, original.info.piece_length)
            self.assertEqual(parsed.info.length, original.info.length)
            self.assertEqual(parsed.info.pieces_raw, original.info.pieces_raw)
            self.assertEqual(parsed.announce, original.announce)
            self.assertEqual(parsed.comment, original.comment)
            self.assertEqual(parsed.created_by, original.created_by)
        finally:
            path.unlink()

    def test_multi_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "bundle"
            root.mkdir()
            (root / "a.bin").write_bytes(os.urandom(5_000))
            (root / "b.bin").write_bytes(os.urandom(5_000))

            original = create_torrent(root, comment="multi-file test")
            raw = original.to_bytes()
            parsed = parse_torrent_bytes(raw)

            self.assertEqual(parsed.info_hash, original.info_hash)
            self.assertFalse(parsed.info.is_single_file)
            self.assertEqual(len(parsed.info.files), 2)
            self.assertEqual(parsed.info.total_length, original.info.total_length)

    def test_write_and_read_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.bin"
            src.write_bytes(os.urandom(10_000))
            torrent_path = Path(d) / "test.torrent"

            original = create_torrent(src)
            original.write(torrent_path)

            self.assertTrue(torrent_path.exists())
            parsed = parse_torrent_bytes(torrent_path.read_bytes())
            self.assertEqual(parsed.info_hash, original.info_hash)


# ---------------------------------------------------------------------------
# Multiple explicit paths
# ---------------------------------------------------------------------------


class TestCreateTorrentMultiplePaths(unittest.TestCase):
    """Test create_torrent with a list of individual paths."""

    def test_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f1 = Path(d) / "a.txt"
            f2 = Path(d) / "b.txt"
            f1.write_bytes(b"aaa")
            f2.write_bytes(b"bbb")

            meta = create_torrent([f1, f2])
            self.assertFalse(meta.info.is_single_file)
            self.assertEqual(len(meta.info.files), 2)
            self.assertEqual(meta.info.total_length, 6)


if __name__ == "__main__":
    unittest.main()
