"""Tests for aiobt.torrent — torrent file parsing and data models."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiobt.bencode import encode
from aiobt.torrent import (
    FileEntry,
    TorrentInfo,
    TorrentMeta,
    parse_torrent_bytes,
    parse_torrent_file,
)


class TestFileEntry:
    def test_frozen(self) -> None:
        entry = FileEntry(path=("dir", "file.txt"), length=1024)
        with pytest.raises(AttributeError):
            entry.length = 0  # type: ignore[misc]

    def test_relative_path(self) -> None:
        entry = FileEntry(path=("subdir", "nested", "file.dat"), length=42)
        assert str(entry.relative_path) == "subdir/nested/file.dat"


class TestTorrentInfo:
    def test_single_file(self) -> None:
        pieces = hashlib.sha1(b"x" * 512).digest() * 2
        info = TorrentInfo(
            name="test.bin",
            piece_length=512,
            pieces_raw=pieces,
            length=1024,
        )
        assert info.is_single_file is True
        assert info.piece_count == 2
        assert info.total_length == 1024
        assert len(info.piece_hash(0)) == 20
        assert len(info.piece_hash(1)) == 20

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
        assert info.is_single_file is False
        assert info.total_length == 256
        assert info.piece_count == 1

    def test_piece_hash_out_of_range(self) -> None:
        pieces = hashlib.sha1(b"z").digest()
        info = TorrentInfo(
            name="x",
            piece_length=64,
            pieces_raw=pieces,
            length=64,
        )
        with pytest.raises(IndexError):
            info.piece_hash(1)

    def test_bad_pieces_length(self) -> None:
        info = TorrentInfo(
            name="x",
            piece_length=64,
            pieces_raw=b"\x00" * 15,  # not a multiple of 20
            length=64,
        )
        with pytest.raises(ValueError, match="not a multiple"):
            info.piece_count


class TestParseSingleFile:
    def test_basic(self, tmp_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_torrent))
        assert meta.info.name == "hello.txt"
        assert meta.info.is_single_file is True
        assert meta.info.length == 1800
        assert meta.info.piece_length == 512
        assert meta.announce == "http://tracker.example.com/announce"
        assert meta.comment == "test torrent"
        assert meta.created_by == "aiobt-tests"
        assert meta.creation_date == 1717100000
        assert len(meta.info_hash) == 20

    def test_piece_count(self, tmp_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_torrent))
        # 1800 bytes / 512 piece_length = 3 full + 1 partial = 4 pieces
        assert meta.piece_count == 4

    def test_total_length(self, tmp_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_torrent))
        assert meta.total_length == 1800


class TestParseMultiFile:
    def test_basic(self, tmp_multi_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_multi_torrent))
        assert meta.info.name == "test_dir"
        assert meta.info.is_single_file is False
        assert meta.info.files is not None
        assert len(meta.info.files) == 2

    def test_file_entries(self, tmp_multi_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_multi_torrent))
        assert meta.info.files is not None
        f1, f2 = meta.info.files
        assert f1.path == ("subdir", "file1.txt")
        assert f2.path == ("file2.txt",)
        assert f1.length == 850
        assert f2.length == 1700

    def test_announce_list(self, tmp_multi_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_multi_torrent))
        assert meta.announce_list is not None
        assert len(meta.announce_list) == 2
        assert meta.announce_list[0] == ("http://tracker1.example.com/announce",)
        assert meta.announce_list[1] == (
            "http://tracker2.example.com/announce",
            "http://tracker3.example.com/announce",
        )

    def test_tracker_urls_deduped(self, tmp_multi_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_multi_torrent))
        urls = meta.tracker_urls()
        assert len(urls) == len(set(urls)), "tracker URLs should be deduplicated"
        assert "http://tracker.example.com/announce" in urls
        assert "http://tracker1.example.com/announce" in urls


class TestInfoHash:
    def test_deterministic(self, tmp_torrent: Path) -> None:
        meta1 = parse_torrent_file(str(tmp_torrent))
        meta2 = parse_torrent_file(str(tmp_torrent))
        assert meta1.info_hash == meta2.info_hash

    def test_matches_manual_hash(self, tmp_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_torrent))
        # Re-encode the info dict and hash it manually
        raw = tmp_torrent.read_bytes()
        from aiobt.bencode import decode

        top = decode(raw)
        assert isinstance(top, dict)
        info_bytes = encode(top[b"info"])
        expected = hashlib.sha1(info_bytes).digest()
        assert meta.info_hash == expected


class TestParseEdgeCases:
    def test_not_a_dict(self) -> None:
        from aiobt.bencode import DecodeError

        with pytest.raises(DecodeError, match="must be a dict"):
            parse_torrent_bytes(encode(42))

    def test_missing_info(self) -> None:
        from aiobt.bencode import DecodeError

        with pytest.raises(DecodeError, match="missing required"):
            parse_torrent_bytes(encode({b"announce": b"http://x"}))

    def test_frozen(self, tmp_torrent: Path) -> None:
        meta = parse_torrent_file(str(tmp_torrent))
        with pytest.raises(AttributeError):
            meta.announce = "changed"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            meta.info.name = "changed"  # type: ignore[misc]
