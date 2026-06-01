"""Tests for aiobt.torrent — torrent file parsing and data models."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import later.unittest

from aiobt.bencode import DecodeError, encode
from aiobt.torrent import (
    FileEntry,
    TorrentInfo,
    parse_torrent_bytes,
    parse_torrent_file,
)


def _create_single_torrent(directory: Path) -> Path:
    """Create a minimal single-file .torrent in *directory*."""
    content = b"Hello, BitTorrent!" * 100  # 1800 bytes
    piece_length = 512
    pieces = b""

    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + piece_length]
        pieces += hashlib.sha1(chunk).digest()
        offset += piece_length

    info: dict[bytes, object] = {
        b"name": b"hello.txt",
        b"piece length": piece_length,
        b"pieces": pieces,
        b"length": len(content),
    }

    torrent: dict[bytes, object] = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": info,
        b"comment": b"test torrent",
        b"created by": b"aiobt-tests",
        b"creation date": 1717100000,
    }

    torrent_path = directory / "test.torrent"
    torrent_path.write_bytes(encode(torrent))  # type: ignore[arg-type]
    return torrent_path


def _create_multi_torrent(directory: Path) -> Path:
    """Create a minimal multi-file .torrent in *directory*."""
    file1 = b"File one content " * 50  # 850 bytes
    file2 = b"Second file data " * 100  # 1700 bytes
    content = file1 + file2

    piece_length = 1024
    pieces = b""

    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + piece_length]
        pieces += hashlib.sha1(chunk).digest()
        offset += piece_length

    info: dict[bytes, object] = {
        b"name": b"test_dir",
        b"piece length": piece_length,
        b"pieces": pieces,
        b"files": [
            {b"length": len(file1), b"path": [b"subdir", b"file1.txt"]},
            {b"length": len(file2), b"path": [b"file2.txt"]},
        ],
    }

    torrent: dict[bytes, object] = {
        b"announce": b"http://tracker.example.com/announce",
        b"announce-list": [
            [b"http://tracker1.example.com/announce"],
            [
                b"http://tracker2.example.com/announce",
                b"http://tracker3.example.com/announce",
            ],
        ],
        b"info": info,
    }

    torrent_path = directory / "multi.torrent"
    torrent_path.write_bytes(encode(torrent))  # type: ignore[arg-type]
    return torrent_path


class TestFileEntry(later.unittest.TestCase):
    def test_frozen(self) -> None:
        entry = FileEntry(path=("dir", "file.txt"), length=1024)
        with self.assertRaises(AttributeError):
            entry.length = 0  # type: ignore[misc]

    def test_relative_path(self) -> None:
        entry = FileEntry(path=("subdir", "nested", "file.dat"), length=42)
        self.assertEqual(str(entry.relative_path), "subdir/nested/file.dat")


class TestTorrentInfo(later.unittest.TestCase):
    def test_single_file(self) -> None:
        pieces = hashlib.sha1(b"x" * 512).digest() * 2
        info = TorrentInfo(
            name="test.bin",
            piece_length=512,
            pieces_raw=pieces,
            length=1024,
        )
        self.assertTrue(info.is_single_file)
        self.assertEqual(info.piece_count, 2)
        self.assertEqual(info.total_length, 1024)
        self.assertEqual(len(info.piece_hash(0)), 20)
        self.assertEqual(len(info.piece_hash(1)), 20)

    def test_multi_file(self) -> None:
        pieces = hashlib.sha1(b"y" * 256).digest()
        files = (
            FileEntry(path=("a.txt",), length=100),
            FileEntry(path=("b.txt",), length=156),
        )
        info = TorrentInfo(
            name="test_dir",
            piece_length=256,
            pieces_raw=pieces,
            files=files,
        )
        self.assertFalse(info.is_single_file)
        self.assertEqual(info.total_length, 256)
        self.assertEqual(info.piece_count, 1)

    def test_piece_hash_out_of_range(self) -> None:
        pieces = hashlib.sha1(b"z").digest()
        info = TorrentInfo(
            name="x",
            piece_length=64,
            pieces_raw=pieces,
            length=64,
        )
        with self.assertRaises(IndexError):
            info.piece_hash(1)

    def test_bad_pieces_length(self) -> None:
        info = TorrentInfo(
            name="x",
            piece_length=64,
            pieces_raw=b"\x00" * 15,  # not a multiple of 20
            length=64,
        )
        with self.assertRaisesRegex(ValueError, "not a multiple"):
            info.piece_count


class TestParseSingleFile(later.unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.torrent_path = _create_single_torrent(self.tmp_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def test_basic(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        self.assertEqual(meta.info.name, "hello.txt")
        self.assertTrue(meta.info.is_single_file)
        self.assertEqual(meta.info.length, 1800)
        self.assertEqual(meta.info.piece_length, 512)
        self.assertEqual(meta.announce, "http://tracker.example.com/announce")
        self.assertEqual(meta.comment, "test torrent")
        self.assertEqual(meta.created_by, "aiobt-tests")
        self.assertEqual(meta.creation_date, 1717100000)
        self.assertEqual(len(meta.info_hash), 20)

    def test_piece_count(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        # 1800 bytes / 512 piece_length = 3 full + 1 partial = 4 pieces
        self.assertEqual(meta.piece_count, 4)

    def test_total_length(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        self.assertEqual(meta.total_length, 1800)


class TestParseMultiFile(later.unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.torrent_path = _create_multi_torrent(self.tmp_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def test_basic(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        self.assertEqual(meta.info.name, "test_dir")
        self.assertFalse(meta.info.is_single_file)
        self.assertIsNotNone(meta.info.files)
        self.assertEqual(len(meta.info.files), 2)

    def test_file_entries(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        self.assertIsNotNone(meta.info.files)
        f1, f2 = meta.info.files
        self.assertEqual(f1.path, ("subdir", "file1.txt"))
        self.assertEqual(f2.path, ("file2.txt",))
        self.assertEqual(f1.length, 850)
        self.assertEqual(f2.length, 1700)

    def test_announce_list(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        self.assertIsNotNone(meta.announce_list)
        self.assertEqual(len(meta.announce_list), 2)
        self.assertEqual(
            meta.announce_list[0], ("http://tracker1.example.com/announce",)
        )
        self.assertEqual(
            meta.announce_list[1],
            (
                "http://tracker2.example.com/announce",
                "http://tracker3.example.com/announce",
            ),
        )

    def test_tracker_urls_deduped(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        urls = meta.tracker_urls()
        self.assertEqual(
            len(urls), len(set(urls)), "tracker URLs should be deduplicated"
        )
        self.assertIn("http://tracker.example.com/announce", urls)
        self.assertIn("http://tracker1.example.com/announce", urls)


class TestInfoHash(later.unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.torrent_path = _create_single_torrent(self.tmp_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def test_deterministic(self) -> None:
        meta1 = parse_torrent_file(str(self.torrent_path))
        meta2 = parse_torrent_file(str(self.torrent_path))
        self.assertEqual(meta1.info_hash, meta2.info_hash)

    def test_matches_manual_hash(self) -> None:
        meta = parse_torrent_file(str(self.torrent_path))
        # Re-encode the info dict and hash it manually
        raw = self.torrent_path.read_bytes()
        from aiobt.bencode import decode

        top = decode(raw)
        self.assertIsInstance(top, dict)
        info_bytes = encode(top[b"info"])
        expected = hashlib.sha1(info_bytes).digest()
        self.assertEqual(meta.info_hash, expected)


class TestParseEdgeCases(later.unittest.TestCase):
    def test_not_a_dict(self) -> None:
        with self.assertRaisesRegex(DecodeError, "must be a dict"):
            parse_torrent_bytes(encode(42))

    def test_missing_info(self) -> None:
        with self.assertRaisesRegex(DecodeError, "missing required"):
            parse_torrent_bytes(encode({b"announce": b"http://x"}))

    def test_frozen(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            torrent_path = _create_single_torrent(Path(tmp_dir.name))
            meta = parse_torrent_file(str(torrent_path))
            with self.assertRaises(AttributeError):
                meta.announce = "changed"  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                meta.info.name = "changed"  # type: ignore[misc]
        finally:
            tmp_dir.cleanup()
